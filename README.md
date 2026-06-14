# agent-usage

A tiny, dependency-free reporter of **AI coding-agent usage limits** for a
status bar — how much of each provider's rate-limit windows (the rolling
5-hour window and the weekly/long window) you've burned through, so you can see
at a glance how close you are to being throttled without opening a dashboard.

Inspired by [CodexBar](https://github.com/steipete/CodexBar) (macOS menu bar),
reimplemented for Linux / Wayland. Designed to feed the
[ashell](https://github.com/MalpenZibo/ashell) bar via a `CustomModule` plus an
[eww](https://github.com/elkowar/eww) popup, but the JSON/text output works with
any bar (Waybar, etc.).

## Providers

| Provider | Source | Auth | Notes |
|----------|--------|------|-------|
| **Codex** (`cx`) | `~/.codex/sessions/**/*.jsonl` | none | Reads the `rate_limits` object Codex writes into every `token_count` event. Fully local. |
| **Claude Code** (`cc`) | `GET https://api.anthropic.com/api/oauth/usage` | OAuth token from `~/.claude/.credentials.json` (`claudeAiOauth.accessToken`) | Reports `five_hour`, `seven_day`, and (if present) the Opus weekly cap. Honours `CLAUDE_CONFIG_DIR`. |
| **Kimi** (`km`) | `GET https://api.kimi.com/coding/v1/usages` | OAuth token from `~/.kimi-code/credentials/kimi-code.json` | The access token is short-lived (~15 min); it's refreshed via the `refresh_token` against `https://auth.kimi.com/api/oauth/token` and written back atomically (preserving file mode `600`), the same way the kimi-code CLI does. |

**Cursor is not implemented.** Cursor only exposes usage through an
authenticated browser session (cookies); there's no clean, headless path for it
on Linux. PRs welcome.

This tool stores no secrets of its own. It reads the credential files the CLIs
already wrote and never copies tokens elsewhere. A provider that isn't installed
is silently skipped.

## Usage

```sh
agent-usage              # one compact line, e.g.  cc 14%  ·  cx 6%  ·  km 5%
agent-usage --watch      # Waybar-format JSON, reprinted every --interval seconds
agent-usage --interval 30
agent-usage --detail     # multi-line breakdown with reset ETAs
agent-usage --notify     # send the breakdown as a desktop notification
agent-usage --json       # structured output (all windows)
agent-usage --eww        # JSON keyed by provider, shaped for the eww popup
```

The headline percentage per provider is the **most-constrained** window — the
number that actually predicts a throttle. `--watch` emits an icon-only chip
(`{"text":"","alt":"ok|warn|alert","tooltip":...}`); `alt` is `alert` at ≥80% on
any window or on a fetch error, `warn` at ≥60%, else `ok`.

## Install (Nix flake)

```nix
{
  inputs.agent-usage.url = "github:QuillDev/agent-usage";

  # in your packages / home.packages:
  #   inputs.agent-usage.packages.${pkgs.system}.default
  # or apply inputs.agent-usage.overlays.default and use pkgs.agent-usage
}
```

Without Nix it's a single stdlib Python 3 script: put `agent_usage.py` on your
`PATH` as `agent-usage` (and have `notify-send` available for `--notify`).

## Bar integration

### ashell chip (icon-only) + eww popup

The bar shows just a gauge icon with an alert dot; clicking it toggles an eww
popup with a logo and 5h/weekly bars per provider. See
[`examples/`](examples/) for ready-to-adapt `eww.yuck`, `eww.scss`, and the
ashell `CustomModule` snippet. The provider logo SVGs ship in the package at
`$out/share/agent-usage/icons/`.

Note for cosmic-text bars (ashell): the chip icon is a Nerd Font glyph rendered
via font fallback, so a Nerd Font (e.g. `nerd-fonts.symbols-only`) must be
installed and discoverable when the bar starts.

### Any Waybar-compatible bar

Point a custom module's script at `agent-usage --watch`; it speaks the Waybar
custom `{"text","alt","tooltip"}` protocol.

## Requirements

- Python 3 (stdlib only)
- `notify-send` (libnotify) for `--notify`
- a Nerd Font for the bar glyph; `eww` for the popup (optional)

## Credits

- Data sources reverse-engineered from [CodexBar](https://github.com/steipete/CodexBar) (MIT).
- Provider logo SVGs in `icons/` are from CodexBar's resources / upstream brand
  marks, used for identification.

## License

MIT — see [LICENSE](LICENSE).
