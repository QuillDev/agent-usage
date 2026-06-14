#!/usr/bin/env python3
"""agent-usage — report AI coding-agent usage *limits* for a status bar.

Tracks how much of each provider's rate-limit windows you've burned through
(the 5-hour window and the weekly/long window) so you can see, at a glance in
the bar, how close you are to being throttled — without opening a dashboard.

Providers
---------
- codex  : read locally from ~/.codex/sessions/**/*.jsonl (no network/auth).
           Codex writes a complete `rate_limits` object into every token_count
           event; we read the most recent one.
- claude : GET https://api.anthropic.com/api/oauth/usage using the OAuth token
           Claude Code stores in ~/.claude/.credentials.json.
- kimi    : GET https://api.kimi.com/coding/v1/usages using the kimi-code OAuth
            token in ~/.kimi-code/credentials/kimi-code.json. That access token
            is short-lived (~15 min); we refresh it via the refresh_token when
            expired and write the new token back atomically.

Cursor is intentionally not implemented: it only exposes usage through an
authenticated browser session (cookies), which has no clean headless path on
Linux. See README.

Output modes
------------
  agent-usage            one compact bar line, printed once
  agent-usage --watch    same line, reprinted every --interval seconds (for
                         ashell's CustomModule listen_cmd)
  agent-usage --detail   multi-line human-readable breakdown
  agent-usage --notify   send the breakdown as a desktop notification
  agent-usage --json     structured JSON for scripting

Stdlib only — no third-party dependencies.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HTTP_TIMEOUT = 15  # seconds
CLAUDE_CODE_VERSION = "2.1.0"  # User-Agent fallback for the Claude usage call


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Window:
    """One rate-limit window for a provider."""

    name: str  # short label: "5h", "7d", "opus", ...
    used_pct: float  # 0-100
    resets_at: float | None = None  # epoch seconds, or None if unknown


@dataclass
class ProviderUsage:
    tag: str  # short bar tag, e.g. "cc"
    name: str  # human name, e.g. "Claude Code"
    windows: list[Window] = field(default_factory=list)
    error: str | None = None  # set when creds exist but the fetch failed
    configured: bool = True  # False when the provider isn't set up at all

    @property
    def headline_pct(self) -> float | None:
        """The most-constrained window — the number that matters for the bar."""
        if not self.windows:
            return None
        return max(w.used_pct for w in self.windows)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _parse_reset(value) -> float | None:
    """Normalise a reset timestamp (epoch int/float or ISO-8601 string) to epoch."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # epoch-as-string?
        try:
            return float(s)
        except ValueError:
            pass
        # ISO-8601, possibly with fractional seconds and offset / trailing Z
        try:
            iso = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _http_json(req: urllib.request.Request) -> dict:
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read()
    return json.loads(body)


def _b64url_json(segment: str) -> dict:
    """Decode a base64url JWT segment into a dict (best effort)."""
    pad = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + pad))


def _fmt_eta(resets_at: float | None, now: float) -> str:
    if resets_at is None:
        return "?"
    delta = int(resets_at - now)
    if delta <= 0:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


# --------------------------------------------------------------------------
# Provider: Codex (local, no auth)
# --------------------------------------------------------------------------
def fetch_codex() -> ProviderUsage:
    p = ProviderUsage(tag="cx", name="Codex")
    base = Path(os.environ.get("CODEX_HOME", _home() / ".codex"))
    sessions = base / "sessions"
    if not sessions.is_dir():
        p.configured = False
        return p

    files = sorted(sessions.rglob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        p.configured = False
        return p

    rl = None
    # Scan the few most recent sessions, newest line first, for a rate_limits blob.
    for f in files[:8]:
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            rl = _find_key(obj, "rate_limits")
            if rl:
                break
        if rl:
            break

    if not rl:
        p.error = "no data"
        return p

    primary = rl.get("primary") or {}
    secondary = rl.get("secondary") or {}
    if primary.get("used_percent") is not None:
        p.windows.append(Window("5h", float(primary["used_percent"]),
                                _parse_reset(primary.get("resets_at"))))
    if secondary.get("used_percent") is not None:
        p.windows.append(Window("7d", float(secondary["used_percent"]),
                                _parse_reset(secondary.get("resets_at"))))
    return p


def _find_key(obj, key):
    """Depth-first search for the first value under `key` in nested dicts/lists."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


# --------------------------------------------------------------------------
# Provider: Claude Code (OAuth usage API)
# --------------------------------------------------------------------------
def fetch_claude() -> ProviderUsage:
    p = ProviderUsage(tag="cc", name="Claude Code")
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    candidates = []
    if config_dir:
        candidates.append(Path(config_dir) / ".credentials.json")
    candidates.append(_home() / ".claude" / ".credentials.json")
    candidates.append(_home() / ".config" / "claude" / ".credentials.json")

    creds_path = next((c for c in candidates if c.is_file()), None)
    if creds_path is None:
        p.configured = False
        return p

    try:
        creds = json.loads(creds_path.read_text())
        token = creds["claudeAiOauth"]["accessToken"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
        p.error = "bad creds"
        return p

    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": f"claude-code/{CLAUDE_CODE_VERSION}",
        },
    )
    try:
        data = _http_json(req)
    except urllib.error.HTTPError as e:
        # 401 means the stored token expired; Claude Code refreshes it on next use.
        p.error = "expired" if e.code == 401 else f"http {e.code}"
        return p
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        p.error = "offline"
        return p

    def add(key: str, label: str):
        w = data.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            p.windows.append(Window(label, float(w["utilization"]),
                                    _parse_reset(w.get("resets_at"))))

    add("five_hour", "5h")
    add("seven_day", "7d")
    add("seven_day_opus", "opus")  # only present on plans with an Opus cap
    if not p.windows:
        p.error = "no data"
    return p


# --------------------------------------------------------------------------
# Provider: Kimi Code (OAuth usages API, with refresh)
# --------------------------------------------------------------------------
KIMI_USAGES_URL = "https://api.kimi.com/coding/v1/usages"
KIMI_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"


def _kimi_detail_pct(detail: dict) -> float | None:
    """Kimi reports limit/used/remaining (as numbers or numeric strings)."""
    def num(v):
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    limit = num(detail.get("limit"))
    used = num(detail.get("used"))
    remaining = num(detail.get("remaining"))
    if limit is None or limit <= 0:
        return None
    if used is None and remaining is not None:
        used = max(0.0, limit - remaining)
    if used is None:
        return None
    return used / limit * 100.0


def _kimi_detail_reset(detail: dict) -> float | None:
    for k in ("resetTime", "resetAt", "reset_time", "reset_at"):
        if detail.get(k) is not None:
            return _parse_reset(detail[k])
    return None


def _kimi_refresh(creds_path: Path, creds: dict) -> str:
    """Refresh the kimi-code access token and write it back atomically.

    Mirrors what the kimi-code CLI does itself, so the CLI keeps working with
    the rotated token. Raises on failure; the caller leaves the file untouched.
    """
    refresh_token = creds.get("refresh_token")
    access_token = creds.get("access_token", "")
    if not refresh_token:
        raise RuntimeError("no refresh_token")

    # client_id is embedded in the JWT payload (sub/client_id claim).
    client_id = None
    try:
        parts = access_token.split(".")
        if len(parts) >= 2:
            client_id = _b64url_json(parts[1]).get("client_id")
    except Exception:
        client_id = None

    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if client_id:
        form["client_id"] = client_id
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        KIMI_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    data = _http_json(req)
    new_access = data.get("access_token")
    if not new_access:
        raise RuntimeError("refresh response missing access_token")

    updated = dict(creds)
    updated["access_token"] = new_access
    if data.get("refresh_token"):
        updated["refresh_token"] = data["refresh_token"]
    if data.get("expires_in") is not None:
        updated["expires_in"] = data["expires_in"]
        updated["expires_at"] = int(time.time()) + int(data["expires_in"])
    if data.get("token_type"):
        updated["token_type"] = data["token_type"]

    tmp = creds_path.with_suffix(creds_path.suffix + ".tmp")
    tmp.write_text(json.dumps(updated, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, creds_path)
    return new_access


def fetch_kimi() -> ProviderUsage:
    p = ProviderUsage(tag="km", name="Kimi")
    creds_path = _home() / ".kimi-code" / "credentials" / "kimi-code.json"
    if not creds_path.is_file():
        p.configured = False
        return p

    try:
        creds = json.loads(creds_path.read_text())
    except (OSError, json.JSONDecodeError):
        p.error = "bad creds"
        return p

    token = creds.get("access_token")
    if not token:
        # Allow an explicit override (matches codexbar's KIMI_AUTH_TOKEN).
        token = os.environ.get("KIMI_AUTH_TOKEN")
    expires_at = creds.get("expires_at")

    # Refresh proactively if the short-lived token is expired (or about to be).
    if expires_at is not None and time.time() >= float(expires_at) - 60:
        try:
            token = _kimi_refresh(creds_path, creds)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError, RuntimeError):
            p.error = "refresh failed"
            return p

    if not token:
        p.error = "no token"
        return p

    req = urllib.request.Request(
        KIMI_USAGES_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        data = _http_json(req)
    except urllib.error.HTTPError as e:
        p.error = "expired" if e.code in (401, 403) else f"http {e.code}"
        return p
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        p.error = "offline"
        return p

    # Weekly quota lives in `usage`; the 5-hour window in `limits[0].detail`.
    # Append 5h first, then weekly, so the two bars order like the others.
    limits = data.get("limits")
    if isinstance(limits, list) and limits:
        detail = (limits[0] or {}).get("detail") or {}
        pct = _kimi_detail_pct(detail)
        if pct is not None:
            p.windows.append(Window("5h", pct, _kimi_detail_reset(detail)))
    usage = data.get("usage")
    if isinstance(usage, dict):
        pct = _kimi_detail_pct(usage)
        if pct is not None:
            # Kimi's weekly quota — labelled "7d" to match the others' long window.
            p.windows.append(Window("7d", pct, _kimi_detail_reset(usage)))

    if not p.windows:
        p.error = "no data"
    return p


# --------------------------------------------------------------------------
# Provider: Cursor (editor session token -> usage-summary API)
# --------------------------------------------------------------------------
def _cursor_token() -> str | None:
    """Read the Cursor editor's OAuth access token from its SQLite state DB."""
    import sqlite3

    db = _home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if not db.is_file():
        return None
    try:
        # immutable/read-only so we never lock the editor's live DB.
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken' LIMIT 1"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    return row[0].decode() if isinstance(row[0], bytes) else str(row[0])


def fetch_cursor() -> ProviderUsage:
    p = ProviderUsage(tag="cu", name="Cursor")
    token = _cursor_token()
    if not token:
        p.configured = False
        return p

    # userId = last segment (after '|') of the JWT `sub` claim.
    try:
        parts = token.split(".")
        sub = _b64url_json(parts[1]).get("sub", "") if len(parts) >= 2 else ""
        user_id = sub.split("|")[-1]
    except Exception:
        user_id = ""
    if not user_id:
        p.error = "bad token"
        return p

    cookie = f"WorkosCursorSessionToken={user_id}%3A%3A{token}"
    req = urllib.request.Request(
        "https://cursor.com/api/usage-summary",
        headers={"Accept": "application/json", "Cookie": cookie},
    )
    try:
        data = _http_json(req)
    except urllib.error.HTTPError as e:
        p.error = "expired" if e.code in (401, 403) else f"http {e.code}"
        return p
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        p.error = "offline"
        return p

    plan = ((data.get("individualUsage") or {}).get("plan")) or {}
    # Cursor's only window is the monthly billing cycle, but within it the
    # dashboard breaks usage into Auto-model vs named/API-model percentages.
    # Show those as the two bars; fall back to the combined total otherwise.
    reset = _parse_reset(data.get("billingCycleEnd"))

    def clamp(v):
        return max(0.0, min(100.0, float(v)))

    auto = plan.get("autoPercentUsed")
    api = plan.get("apiPercentUsed")
    if auto is not None or api is not None:
        if auto is not None:
            p.windows.append(Window("auto", clamp(auto), reset))
        if api is not None:
            p.windows.append(Window("api", clamp(api), reset))
        return p

    pct = plan.get("totalPercentUsed")
    if pct is None:
        used, limit = plan.get("used"), plan.get("limit")
        if used is not None and limit:
            pct = used / limit * 100.0
    if pct is None:
        p.error = "no data"
        return p
    p.windows.append(Window("mo", clamp(pct), reset))
    return p


# --------------------------------------------------------------------------
# Aggregation + rendering
# --------------------------------------------------------------------------
# Ordered registry of every known provider: tag -> (display name, fetcher).
PROVIDERS: dict[str, tuple[str, "callable"]] = {
    "cc": ("Claude Code", fetch_claude),
    "cx": ("Codex", fetch_codex),
    "km": ("Kimi", fetch_kimi),
    "cu": ("Cursor", fetch_cursor),
}
ALL_TAGS = list(PROVIDERS.keys())
DEFAULT_TAGS = ["cc", "cx"]


def resolve_selection(arg: str | None) -> list[str]:
    """Which providers to show: --providers / $AGENT_USAGE_PROVIDERS / default."""
    raw = arg or os.environ.get("AGENT_USAGE_PROVIDERS") or ""
    tags = [t.strip() for t in raw.split(",") if t.strip()]
    if not tags:
        return list(DEFAULT_TAGS)
    # keep only known tags, in the registry's canonical order
    return [t for t in ALL_TAGS if t in tags]


def collect(selected: list[str]) -> list[ProviderUsage]:
    results = []
    for tag in selected:
        name, fn = PROVIDERS[tag]
        try:
            results.append(fn())
        except Exception:  # never let one provider take down the bar
            results.append(ProviderUsage(tag=tag, name=name, error="crash"))
    return results


def render_bar(results: list[ProviderUsage]) -> str:
    """One compact line, e.g. `cc 12% · cx 9% · km 4%`."""
    parts = []
    for r in results:
        if not r.configured:
            continue
        if r.error:
            parts.append(f"{r.tag} !")
        else:
            pct = r.headline_pct
            parts.append(f"{r.tag} {round(pct)}%" if pct is not None else f"{r.tag} –")
    return "  ·  ".join(parts) if parts else "no agents"


def _alert_level(results: list[ProviderUsage]) -> str:
    """Worst status across providers, for the bar's icon/alert state."""
    if any(r.configured and r.error for r in results):
        return "alert"
    pcts = [r.headline_pct for r in results
            if r.configured and not r.error and r.headline_pct is not None]
    worst = max(pcts) if pcts else 0.0
    if worst >= 80:
        return "alert"
    if worst >= 60:
        return "warn"
    return "ok"


def render_waybar(results: list[ProviderUsage]) -> str:
    """Waybar-format JSON for ashell's CustomModule listen_cmd.

    ashell shows `text` as the label, maps `alt` through the `icons` regex,
    and matches `alt` against the module's `alert` regex for the red dot.
    """
    # The bar chip is icon-only (the full breakdown lives in the eww popup), so
    # `text` is empty; `alt` drives the alert dot and `tooltip` the hover text.
    # Compact separators so a bar's alert regex can match e.g. `"alt":"alert"`.
    return json.dumps({
        "text": "",
        "alt": _alert_level(results),
        "tooltip": render_detail(results),
    }, separators=(",", ":"))


def render_detail(results: list[ProviderUsage]) -> str:
    now = time.time()
    lines = []
    for r in results:
        if not r.configured:
            lines.append(f"{r.name:<12} not configured")
            continue
        if r.error:
            lines.append(f"{r.name:<12} {r.error}")
            continue
        cells = []
        for w in r.windows:
            cells.append(f"{w.name} {round(w.used_pct)}% (resets {_fmt_eta(w.resets_at, now)})")
        lines.append(f"{r.name:<12} " + "   ".join(cells))
    return "\n".join(lines)


def _win_state(pct: float | None) -> str:
    if pct is None:
        return "ok"
    if pct >= 80:
        return "alert"
    if pct >= 60:
        return "warn"
    return "ok"


def to_eww(results: list[ProviderUsage]) -> str:
    """JSON keyed by provider tag, shaped for the eww popup's fixed rows.

    Emits an entry for *every* known provider so the eww config's fixed rows
    can index it; `shown` reflects the --providers selection (rows for
    unselected providers stay hidden). Each provider exposes a 5-hour window
    (`w1`) and a longer window (`w2`: weekly, or Cursor's monthly cycle), each
    with its own colour state and reset ETA.
    """
    now = time.time()
    by_tag = {r.tag: r for r in results}

    def slot(w: Window | None) -> dict:
        if w is None:
            return {"label": "", "pct": 0, "state": "ok", "reset": "", "present": False}
        return {
            "label": w.name,
            "pct": round(w.used_pct),
            "state": _win_state(w.used_pct),
            "reset": _fmt_eta(w.resets_at, now),
            "present": True,
        }

    out = {}
    for tag in ALL_TAGS:
        name = PROVIDERS[tag][0]
        r = by_tag.get(tag)
        if r is None:  # not selected this run -> hidden placeholder
            empty = slot(None)
            out[tag] = {"name": name, "present": False, "shown": False,
                        "error": "", "w1": empty, "w2": empty}
            continue
        # Two bars, positional: each provider orders its windows primary-first
        # (5h→7d for Claude/Codex/Kimi, auto→api for Cursor).
        w1 = r.windows[0] if len(r.windows) >= 1 else None
        w2 = r.windows[1] if len(r.windows) >= 2 else None
        out[tag] = {
            "name": r.name,
            "present": r.configured,
            "shown": True,
            "error": r.error or "",
            "w1": slot(w1),
            "w2": slot(w2),
        }
    return json.dumps(out)


def to_json(results: list[ProviderUsage]) -> str:
    out = []
    for r in results:
        out.append({
            "tag": r.tag,
            "name": r.name,
            "configured": r.configured,
            "error": r.error,
            "headline_pct": r.headline_pct,
            "windows": [
                {"name": w.name, "used_pct": w.used_pct, "resets_at": w.resets_at}
                for w in r.windows
            ],
        })
    return json.dumps(out, indent=2)


def notify(results: list[ProviderUsage]) -> None:
    body = render_detail(results)
    try:
        subprocess.run(
            ["notify-send", "-a", "agent-usage", "-i", "utilities-system-monitor",
             "AI usage limits", body],
            check=False,
        )
    except FileNotFoundError:
        print(body)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AI coding-agent usage limits for a status bar")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--watch", action="store_true",
                   help="loop, printing the bar line every --interval seconds")
    g.add_argument("--detail", action="store_true", help="multi-line breakdown")
    g.add_argument("--notify", action="store_true", help="send a desktop notification")
    g.add_argument("--json", action="store_true", help="structured JSON output")
    g.add_argument("--eww", action="store_true", help="JSON shaped for the eww popup")
    ap.add_argument("--interval", type=int, default=60,
                    help="seconds between refreshes in --watch mode (default 60)")
    ap.add_argument("--providers", metavar="cc,cx,km,cu",
                    help="comma-separated providers to show (default: cc,cx; "
                         "also via $AGENT_USAGE_PROVIDERS). Known: "
                         + ",".join(ALL_TAGS))
    args = ap.parse_args(argv)
    selected = resolve_selection(args.providers)

    if args.watch:
        while True:
            try:
                print(render_waybar(collect(selected)), flush=True)
            except Exception:
                print(json.dumps({"text": "agents ?", "alt": "alert"}), flush=True)
            time.sleep(max(5, args.interval))

    results = collect(selected)
    if args.detail:
        print(render_detail(results))
    elif args.notify:
        notify(results)
    elif args.json:
        print(to_json(results))
    elif args.eww:
        print(to_eww(results))
    else:
        print(render_bar(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
