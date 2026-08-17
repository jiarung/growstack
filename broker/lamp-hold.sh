#!/usr/bin/env bash
# Hold the plant light off for a fixed period, then hand control back.
#
#   ./lamp-hold.sh 1800                 # hold off for 30 minutes
#   ./lamp-hold.sh check <start> <end>  # was it off across [start,end)? epoch seconds
#
# WHY A HEARTBEAT. light.py sets manual_until BEFORE drive()'s idempotency
# early-return (light.py:251-254 vs :199-201), so republishing the same OFF keeps
# extending the manual suppression WITHOUT re-actuating the plug. One publish buys
# MANUAL_HOLD (5 min) plus up to one 60 s tick; we republish every 4 min.
#
# THAT EXPIRY IS THE SAFETY FEATURE, not a limitation. If this script is killed,
# crashes, or the host reboots, the suppression lapses and the controller restores
# the lamp within ~6 minutes on its own. A scheduled off-window inside light.py
# would have no such floor: a bug there leaves the plant in the dark indefinitely.
#
# RESTORING IS NOT "PUBLISH ON". The lamp may well have been off when we started
# (at night, or outside the lamp window), and forcing it on then would be worse
# than doing nothing. We record the state going in and restore that: ON gets an
# explicit ON so it comes back immediately; OFF gets nothing, and auto control
# resumes by itself when the suppression lapses.
#
# Every command goes through light-ctl.sh rather than being published directly, so
# exactly one place in the repo knows the command topic and payload shape. Reads
# of retained topics are done here; only writes are delegated.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

HEARTBEAT="${HEARTBEAT:-240}"     # < MANUAL_HOLD (300 s) in light.py
MQTT="${MQTT:-monitor-air-mqtt}"
CONTAINER="${CONTAINER:-monitor-air-influxdb}"
ORG="${ORG:-monitor-air}"

envval() { sed -nE "s/^$1=[[:space:]]*\"?([^\"#[:space:]]+).*/\1/p" "$DIR/.env" 2>/dev/null | head -1 || true; }
LOC="$(envval LIGHT_LOCATION)"; LOC="${LOC:-livingroom}"
BASE="monitor-air/$LOC/light"

# Read a retained topic. Reads only — every write goes through light-ctl.sh.
retained() { docker exec "$MQTT" mosquitto_sub -t "$1" -C 1 -W 3 2>/dev/null || true; }

lamp_state() {  # -> ON | OFF | UNKNOWN
  local s; s="$(retained "$BASE/state")"
  case "$s" in
    *'"state": "ON"'*|*'"state":"ON"'*)   echo ON;;
    *'"state": "OFF"'*|*'"state":"OFF"'*) echo OFF;;
    *) echo UNKNOWN;;
  esac
}

availability() { local a; a="$(retained "$BASE/availability")"; echo "${a:-unknown}"; }

# ---- check: was the lamp off across a past window? ----
# Boundary-based ON PURPOSE. The heartbeat republishes do NOT produce new
# light/state messages — drive() returns early when the target already matches
# (light.py:199-201) — and Telegraf only ingests light/state (telegraf.conf:25-38).
# So the `light` measurement carries STATE CHANGES, not samples, and a correctly
# held window usually contains no points at all. Asking "were all points in the
# window 0" would read that silence as a failure.
lamp_was_off() {   # <start_epoch> <end_epoch>  -> 0 if it was off throughout
  local s="$1" e="$2" before during
  before="$(influx_q "from(bucket:\"sensors\")
      |> range(start: $((s - 7*86400)), stop: $s)
      |> filter(fn:(r)=> r._measurement==\"light\" and r._field==\"on\" and r.location==\"$LOC\")
      |> last() |> keep(columns:[\"_value\"])")"
  during="$(influx_q "from(bucket:\"sensors\")
      |> range(start: $s, stop: $e)
      |> filter(fn:(r)=> r._measurement==\"light\" and r._field==\"on\" and r.location==\"$LOC\" and r._value==1.0)
      |> count() |> keep(columns:[\"_value\"])")"
  before="${before:-}" ; during="${during:-0}"
  # No state ever recorded before the window means we cannot say it was off.
  # Fail closed: an unverifiable window must not be reported as verified.
  [ -n "$before" ] || { echo "no light/state before the window — cannot verify" >&2; return 1; }
  [ "${before%%.*}" = "0" ] || { echo "lamp was ON entering the window" >&2; return 1; }
  [ "${during%%.*}" = "0" ] || { echo "lamp turned ON $during time(s) during the window" >&2; return 1; }
  return 0
}

influx_q() {  # last numeric value of a one-column query, or empty
  docker exec -i "$CONTAINER" influx query --org "$ORG" --raw -f /dev/stdin <<<"$1" 2>/dev/null \
    | awk -F, '!/^#/ && NF>3 && $0 !~ /_value/ { v=$NF } END { gsub(/\r/,"",v); print v }'
}

# ---- main ----
if [ "${1:-}" = "check" ]; then
  lamp_was_off "${2:?need start epoch}" "${3:?need end epoch}" && { echo "lamp was off throughout"; exit 0; } || exit 1
fi

SECS="${1:?usage: lamp-hold.sh <seconds> | lamp-hold.sh check <start> <end>}"
case "$SECS" in ''|*[!0-9]*) echo "seconds must be a positive integer" >&2; exit 1;; esac

PRIOR="$(lamp_state)"
AVAIL_BAD=0
echo "lamp-hold: holding OFF for ${SECS}s (state going in: $PRIOR, heartbeat ${HEARTBEAT}s)"

NAP_PID=""
restore() {
  # Kill the sleep we may be parked on, or it outlives us holding the terminal.
  [ -n "$NAP_PID" ] && kill "$NAP_PID" 2>/dev/null || true
  if [ "$PRIOR" = "ON" ]; then
    echo "lamp-hold: restoring ON (it was on when we started)"
    ./light-ctl.sh on >/dev/null 2>&1 || echo "lamp-hold: restore FAILED — auto control resumes within ~6 min" >&2
  else
    echo "lamp-hold: it was $PRIOR when we started, leaving it — auto resumes within ~6 min"
  fi
}
# Only EXIT restores, and INT/TERM merely exit so it runs exactly once. Trapping
# restore on TERM directly does NOT end the script: bash runs the handler and then
# carries on where it left off, so the lamp would be "restored" while the hold loop
# kept going — the process outlives the signal and keeps republishing OFF.
trap restore EXIT
trap 'exit 143' INT TERM

END=$(( $(date +%s) + SECS ))
./light-ctl.sh off >/dev/null

while :; do
  now=$(date +%s); [ "$now" -lt "$END" ] || break
  left=$(( END - now )); nap=$(( left < HEARTBEAT ? left : HEARTBEAT ))
  # Background the sleep and wait on it. bash defers trap handling until the
  # current FOREGROUND command returns, so a plain `sleep 240` would swallow a
  # TERM for up to four minutes before restoring. `wait` is interruptible.
  sleep "$nap" & NAP_PID=$!
  wait "$NAP_PID" 2>/dev/null || true
  NAP_PID=""
  [ "$(date +%s)" -lt "$END" ] || break
  ./light-ctl.sh off >/dev/null          # refreshes manual_until; no plug traffic
  st="$(lamp_state)"; av="$(availability)"
  [ "$st" = "OFF" ] || echo "lamp-hold: WARNING state is $st mid-hold" >&2
  if [ "$av" != "online" ]; then
    AVAIL_BAD=1
    echo "lamp-hold: WARNING controller availability=$av — commands are not queued" >&2
  fi
done

echo "lamp-hold: window over (availability problems seen: $AVAIL_BAD)"
[ "$AVAIL_BAD" = "0" ]
