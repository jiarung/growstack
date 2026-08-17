#!/usr/bin/env bash
# Manually switch the plant light via the MQTT command seam. This goes through
# the running `light` service (which owns the Tapo plug), so it also publishes
# state and suppresses automatic control for MANUAL_HOLD afterwards — 5 minutes
# (light.py:60), plus up to one 60s tick before the controller re-decides. It was
# 2h until commit 6a31076; lamp-hold.sh depends on the real value, so a stale
# figure here is not cosmetic.
#
#   ./light-ctl.sh on        # force the plant light on
#   ./light-ctl.sh off       # force it off
#   ./light-ctl.sh status    # show retained state + availability
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# take the bare value: tolerate surrounding quotes, whitespace and inline comments
LOC="$(sed -nE 's/^LIGHT_LOCATION=[[:space:]]*"?([^"#[:space:]]+).*/\1/p' "$DIR/.env" 2>/dev/null | head -1 || true)"
LOC="${LOC:-livingroom}"
BASE="monitor-air/$LOC/light"

pub() { docker exec monitor-air-mqtt mosquitto_pub -q 1 -t "$BASE/cmd" -m "{\"state\":\"$1\"}"; }

case "${1:-}" in
  on|ON)   pub ON;  echo "→ commanded ON   ($BASE/cmd) — auto control paused ~5 min";;
  off|OFF) pub OFF; echo "→ commanded OFF  ($BASE/cmd) — auto control paused ~5 min";;
  status)
    echo "retained state / availability ($BASE):"
    rc=0
    docker exec monitor-air-mqtt mosquitto_sub -W 2 -v \
      -t "$BASE/state" -t "$BASE/availability" 2>/dev/null || rc=$?
    # 27 = mosquitto_sub's normal -W timeout; anything else is a real failure
    [ "$rc" -eq 0 ] || [ "$rc" -eq 27 ] || { echo "mqtt read failed (exit $rc)" >&2; exit "$rc"; }
    ;;
  *) echo "usage: $(basename "$0") on|off|status" >&2; exit 1;;
esac
