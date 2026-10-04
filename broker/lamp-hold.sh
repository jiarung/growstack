#!/usr/bin/env bash
# Hold the plant light ON or OFF for a fixed period, then hand control back.
#
#   ./lamp-hold.sh off 30m              # hold off for 30 minutes
#   ./lamp-hold.sh on 2h                # hold on for two hours
#   ./lamp-hold.sh on 90m --bg          # same, detached; see status / cancel
#   ./lamp-hold.sh status               # is a hold running, and until when
#   ./lamp-hold.sh cancel               # end it now; the lamp is restored
#   ./lamp-hold.sh 1800                 # legacy: seconds, OFF
#   ./lamp-hold.sh check <start> <end>  # was it off across [start,end)? epoch seconds
#
# Durations: a bare number is seconds; 15m / 2h / 1h30m are what you would
# type. Capped at MAX_HOLD (4 h) — past that a typo becomes a dark day.
#
# WHY A HEARTBEAT. light.py sets manual_until BEFORE drive()'s idempotency
# early-return, so republishing the same command keeps extending the manual
# suppression WITHOUT re-actuating the plug. One publish buys MANUAL_HOLD
# (5 min) plus up to one 60 s tick; we republish every 4 min.
#
# THAT EXPIRY IS THE SAFETY FEATURE, not a limitation. If this script is killed,
# crashes, or the host reboots, the suppression lapses and the controller takes
# the lamp back within ~6 minutes on its own. A scheduled window inside light.py
# would have no such floor: a bug there leaves the plant in the dark — or, for
# an ON hold, under the lamp at 3 a.m. — indefinitely.
#
# RESTORING MEANS THE STATE GOING IN, not the opposite of the hold. An OFF hold
# that started with the lamp off restores nothing (forcing ON at night would be
# worse than doing nothing); one that started with it on publishes ON so the
# light is back immediately rather than after the 6-minute lapse. Symmetrically
# an ON hold that started OFF publishes OFF at the end — a forced lamp must not
# linger past HARD_OFF for even six minutes.
#
# Every command goes through light-ctl.sh rather than being published directly, so
# exactly one place in the repo knows the command topic and payload shape. Reads
# of retained topics are done here; only writes are delegated.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

HEARTBEAT="${HEARTBEAT:-240}"     # < MANUAL_HOLD (300 s) in light.py
MAX_HOLD="${MAX_HOLD:-14400}"     # 4 h: longer than any deliberate hold, shorter than a forgotten one
PIDFILE="${PIDFILE:-/tmp/lamp-hold.pid}"
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

# "15m", "2h", "1h30m", "90" (seconds) -> seconds. Anything else is an error,
# because a silently-misparsed duration is a lamp in the wrong state for hours.
to_secs() {
  local d="$1" total=0 n
  case "$d" in ''|*[!0-9hms]*) return 1;; esac
  [[ "$d" =~ ^[0-9]+$ ]] && { echo "$d"; return 0; }
  while [ -n "$d" ]; do
    [[ "$d" =~ ^([0-9]+)([hms])(.*)$ ]] || return 1
    n="${BASH_REMATCH[1]}"
    case "${BASH_REMATCH[2]}" in h) total=$((total + n*3600));; m) total=$((total + n*60));; s) total=$((total + n));; esac
    d="${BASH_REMATCH[3]}"
  done
  echo "$total"
}

hold_running() {  # -> pid, or empty
  [ -f "$PIDFILE" ] || return 0
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && echo "$pid"
}

# ---- main ----
case "${1:-}" in
  status)
    pid="$(hold_running)"
    if [ -n "$pid" ]; then
      echo "hold running: pid $pid  $(cat "$PIDFILE.info" 2>/dev/null)"
    else
      echo "no hold running"
    fi
    echo "lamp: $(lamp_state)  controller: $(availability)"
    exit 0;;
  cancel)
    pid="$(hold_running)"
    [ -n "$pid" ] || { echo "no hold running"; exit 0; }
    echo "ending hold (pid $pid) — the lamp is restored by its exit trap"
    kill -TERM "$pid"
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || { echo "done"; exit 0; }; sleep 1; done
    echo "still running after 10 s" >&2; exit 1;;
esac

if [ "${1:-}" = "check" ]; then
  lamp_was_off "${2:?need start epoch}" "${3:?need end epoch}" && { echo "lamp was off throughout"; exit 0; } || exit 1
fi

USAGE="usage: lamp-hold.sh on|off <duration> [--bg] | status | cancel | check <start> <end>"
case "${1:-}" in
  on|ON)   HOLD=ON;  shift;;
  off|OFF) HOLD=OFF; shift;;
  ''|-*)   echo "$USAGE" >&2; exit 1;;
  *)       HOLD=OFF;;                       # legacy: bare seconds means OFF
esac
SECS="$(to_secs "${1:?$USAGE}")" || { echo "bad duration '$1' — use 15m, 2h, 1h30m, or seconds" >&2; exit 1; }
[ "$SECS" -gt 0 ] || { echo "duration must be positive" >&2; exit 1; }
[ "$SECS" -le "$MAX_HOLD" ] || { echo "duration ${SECS}s exceeds MAX_HOLD ${MAX_HOLD}s — a hold that long is a forgotten one" >&2; exit 1; }
shift

if [ "${1:-}" = "--bg" ]; then
  pid="$(hold_running)"; [ -z "$pid" ] || { echo "a hold is already running (pid $pid) — cancel it first" >&2; exit 1; }
  LOG="/tmp/lamp-hold.log"
  nohup "$0" "$HOLD" "$SECS" >>"$LOG" 2>&1 &
  echo "detached: pid $! holding $HOLD for ${SECS}s — log $LOG; ./lamp-hold.sh status | cancel"
  exit 0
fi

pid="$(hold_running)"; [ -z "$pid" ] || { echo "a hold is already running (pid $pid) — cancel it first" >&2; exit 1; }
echo $$ >"$PIDFILE"
echo "$HOLD until $(date -d "@$(( $(date +%s) + SECS ))" '+%H:%M') (${SECS}s)" >"$PIDFILE.info"

PRIOR="$(lamp_state)"
AVAIL_BAD=0
echo "lamp-hold: holding $HOLD for ${SECS}s (state going in: $PRIOR, heartbeat ${HEARTBEAT}s)"

NAP_PID=""
restore() {
  # Kill the sleep we may be parked on, or it outlives us holding the terminal.
  [ -n "$NAP_PID" ] && kill "$NAP_PID" 2>/dev/null || true
  rm -f "$PIDFILE" "$PIDFILE.info"
  if [ "$PRIOR" = "ON" ] || [ "$PRIOR" = "OFF" ]; then
    if [ "$PRIOR" != "$HOLD" ]; then
      echo "lamp-hold: restoring $PRIOR (it was $PRIOR when we started)"
      ./light-ctl.sh "$(tr A-Z a-z <<<"$PRIOR")" >/dev/null 2>&1 \
        || echo "lamp-hold: restore FAILED — auto control resumes within ~6 min" >&2
    else
      echo "lamp-hold: it was already $PRIOR going in, leaving it — auto resumes within ~6 min"
    fi
  else
    echo "lamp-hold: state going in was $PRIOR, leaving it — auto resumes within ~6 min"
  fi
}
# Only EXIT restores, and INT/TERM merely exit so it runs exactly once. Trapping
# restore on TERM directly does NOT end the script: bash runs the handler and then
# carries on where it left off, so the lamp would be "restored" while the hold loop
# kept going — the process outlives the signal and keeps republishing OFF.
trap restore EXIT
trap 'exit 143' INT TERM

END=$(( $(date +%s) + SECS ))
./light-ctl.sh "$(tr A-Z a-z <<<"$HOLD")" >/dev/null

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
  ./light-ctl.sh "$(tr A-Z a-z <<<"$HOLD")" >/dev/null   # refreshes manual_until; no plug traffic
  st="$(lamp_state)"; av="$(availability)"
  [ "$st" = "$HOLD" ] || echo "lamp-hold: WARNING state is $st mid-hold (wanted $HOLD)" >&2
  if [ "$av" != "online" ]; then
    AVAIL_BAD=1
    echo "lamp-hold: WARNING controller availability=$av — commands are not queued" >&2
  fi
done

echo "lamp-hold: window over (availability problems seen: $AVAIL_BAD)"
[ "$AVAIL_BAD" = "0" ]
