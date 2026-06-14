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
      eww open usage
      eww update usage_open=true
    fi
    ;;
esac
