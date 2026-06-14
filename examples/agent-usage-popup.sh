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
      # Open on the monitor under the cursor, centred on the chip we just clicked
      # (cursor x relative to that monitor), clamped to it. Hyprland-specific
      # (uses hyprctl); falls back to a single 1920px screen otherwise.
      # pw ~= popup min-width + padding + border.
      pw=344
      pos=$(hyprctl cursorpos 2>/dev/null | tr -dc '0-9,-')
      cx=$(printf '%s' "$pos" | cut -d, -f1)
      cy=$(printf '%s' "$pos" | cut -d, -f2)
      geo=$(hyprctl monitors -j 2>/dev/null | jq -r \
        --argjson cx "${cx:-0}" --argjson cy "${cy:-0}" \
        '(map(select($cx >= .x and $cx < (.x + .width/.scale) and $cy >= .y and $cy < (.y + .height/.scale))) | first)
         // (map(select(.focused)) | first) // .[0]
         | "\(.name)\t\((($cx - .x))|floor)\t\((.width/.scale)|floor)"' 2>/dev/null)
      mon=$(printf '%s' "$geo" | cut -f1)
      relx=$(printf '%s' "$geo" | cut -f2)
      lw=$(printf '%s' "$geo" | cut -f3)
      [ -n "$lw" ] || lw=1920
      [ -n "$relx" ] || relx=$((lw - 2))
      x=$((relx - pw / 2))
      [ "$x" -lt 6 ] && x=6
      max=$((lw - pw - 6)); [ "$x" -gt "$max" ] && x=$max
      if [ -n "$mon" ]; then
        eww open usage --screen "$mon" --arg xpos="$x"
      else
        eww open usage --arg xpos="$x"
      fi
      eww update usage_open=true
    fi
    ;;
esac
