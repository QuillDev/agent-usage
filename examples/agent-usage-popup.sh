#!/usr/bin/env bash
# Toggle (or close) the eww agent-usage popup. Bind your bar's click action to
# this script (no args = toggle); the backdrop's onclick calls it with `close`.
# Polling in eww.yuck only runs while usage_open is true, so it idles when shut.
set -euo pipefail

case "${1:-toggle}" in
  close)
    eww close usage || true
    eww update usage_open=false || true
    ;;
  *)
    if eww active-windows 2>/dev/null | grep -q usage; then
      eww close usage
      eww update usage_open=false
    else
      # Centre the popup under the cursor (which is on the chip we clicked).
      # Hyprland-specific; without it, falls back to the right edge.
      pw=344
      mw=$(hyprctl monitors -j 2>/dev/null | jq -r 'first(.[]|select(.focused))|(.width/.scale)|floor' 2>/dev/null)
      cx=$(hyprctl cursorpos 2>/dev/null | tr -dc '0-9,' | cut -d, -f1)
      [ -n "$mw" ] || mw=1920
      [ -n "$cx" ] || cx=$((mw - 2))
      x=$((cx - pw / 2))
      [ "$x" -lt 6 ] && x=6
      max=$((mw - pw - 6)); [ "$x" -gt "$max" ] && x=$max
      eww open usage --arg xpos="$x"
      eww update usage_open=true
    fi
    ;;
esac
