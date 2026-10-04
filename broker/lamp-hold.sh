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
# ENDING A HOLD HANDS CONTROL BACK; it does not restore a state. An earlier
# version republished the state seen going in, which is itself a manual command
# and buys another 5 minutes of suppression — so an OFF hold that began at 17:00
# with the lamp on and ended at 21:00 switched the lamp ON past HARD_OFF (codex,
# 2026-10-04). Now the end publishes AUTO: the controller clears its manual
# window and decides on its next tick with its own rules — HARD_OFF, lux, DLI.
# Cost: the lamp may wait up to 60 s for that tick. Benefit: the script never
# has to know what the right state is, because that is the controller's job.
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
LOCK="${LOCK:-/tmp/lamp-hold.lock}"      # flock: two holds started together must not both win
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
  # units in descending order, each at most once: "1h30m" yes, "1h1h" and
  # "30m1h" no — those are typos, and a typo here is a lamp in the wrong state
  local rank=0 r
  while [ -n "$d" ]; do
    [[ "$d" =~ ^([0-9]+)([hms])(.*)$ ]] || return 1
    n="${BASH_REMATCH[1]}"
    case "${BASH_REMATCH[2]}" in h) r=1; total=$((total + n*3600));; m) r=2; total=$((total + n*60));; s) r=3; total=$((total + n));; esac
    [ "$r" -gt "$rank" ] || return 1
    rank=$r
    d="${BASH_REMATCH[3]}"
  done
  echo "$total"
}

hold_running() {  # -> pid, or empty
  [ -f "$PIDFILE" ] || return 0
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 0
  # a live pid is not enough: after a crash the number can belong to anything,
  # and `cancel` would TERM it. It has to be running THIS script.
  tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q "lamp-hold" && echo "$pid"
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

# Check-then-write under a lock, or two invocations in the same instant both
# see "nothing running", both write the pidfile, and two heartbeats — one ON,
# one OFF — fight over the plug while `cancel` only knows about one of them.
exec 9>"$LOCK"
flock -n 9 || { echo "another lamp-hold is starting right now — try again" >&2; exit 1; }
pid="$(hold_running)"; [ -z "$pid" ] || { echo "a hold is already running (pid $pid) — cancel it first" >&2; exit 1; }
echo $$ >"$PIDFILE"
echo "$HOLD until $(date -d "@$(( $(date +%s) + SECS ))" '+%H:%M') (${SECS}s)" >"$PIDFILE.info"
flock -u 9

PRIOR="$(lamp_state)"
AVAIL_BAD=0
echo "lamp-hold: holding $HOLD for ${SECS}s (state going in: $PRIOR, heartbeat ${HEARTBEAT}s)"

NAP_PID=""
restore() {
  # Kill the sleep we may be parked on, or it outlives us holding the terminal.
  [ -n "$NAP_PID" ] && kill "$NAP_PID" 2>/dev/null || true
  rm -f "$PIDFILE" "$PIDFILE.info"
  echo "lamp-hold: handing control back (was $PRIOR going in; the controller decides on its next tick)"
  ./light-ctl.sh auto >/dev/null 2>&1 \
    || echo "lamp-hold: AUTO publish FAILED — the manual window lapses on its own within ~6 min" >&2
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
