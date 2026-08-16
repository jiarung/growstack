#!/usr/bin/env bash
# Daily PPFD calibration monitor. Runs calibrate-ppfd.sh over TODAY's pre-lamp
# daylight window and writes ONE point to InfluxDB measurement `ppfd_cal`, so the
# candidate CAL and its quality metrics become a series you can watch drift
# instead of a number someone re-derives by hand every few weeks.
#
#   ./ppfd-cal-daily.sh              # today's window, write the point
#   ./ppfd-cal-daily.sh --dry-run    # print what would be written, write nothing
#   LUX_MIN=1500 ./ppfd-cal-daily.sh # loosen the brightness floor for one run
#
# THE WINDOW is derived, not hardcoded: it ends when the plant light comes on and
# starts WIN_MIN minutes before that. With LIGHT_WINDOW_START_MIN=480 that is
# 07:00-08:00 local. Deriving it from the same .env var the light controller uses
# means moving the lamp window moves this with it — the alert rules already do
# this for the same reason, and a calibration window that silently overlaps lamp
# hours would fit daylight coefficients to lamp light.
#
# WHAT THIS IS NOT: an authority. The reference is BH1750 lux via the Apogee
# sunlight factor, not a PAR meter, so `cal_ols` is a CANDIDATE for drift
# monitoring. Nothing here touches the panel formula — see PPFD-CALIBRATION.md
# for the human review step before any CAL is actually adopted.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

TZ_NAME="${TZ_NAME:-Asia/Taipei}"
DEVICE="${DEVICE:-livingroom}"
WIN_MIN="${WIN_MIN:-60}"          # length of the pre-lamp window, minutes
LUX_MIN="${LUX_MIN:-3000}"        # brightness floor; see calibrate-ppfd.sh
GAIN="${GAIN:-4}"                 # firmware ambient gain (AMBIENT_GAIN, sensors.cpp)
TINT_MS="${TINT_MS:-280.78}"      # (ATIME+1)*(ASTEP+1)*2.78us
CONTAINER="${CONTAINER:-monitor-air-influxdb}"
ORG="${ORG:-monitor-air}"
BUCKET="${BUCKET:-sensors}"

# take the bare value: tolerate surrounding quotes, whitespace and inline comments
START_MIN="$(sed -nE 's/^LIGHT_WINDOW_START_MIN=[[:space:]]*"?([0-9]+).*/\1/p' "$DIR/.env" 2>/dev/null | head -1 || true)"
START_MIN="${START_MIN:-480}"
[ "$START_MIN" -gt "$WIN_MIN" ] || { echo "lamp starts at $START_MIN min — no room for a ${WIN_MIN}min window before it" >&2; exit 1; }
W0_MIN=$(( START_MIN - WIN_MIN ))

hhmm() { printf '%02d:%02d' $(( $1 / 60 )) $(( $1 % 60 )); }
DAY="$(TZ="$TZ_NAME" date +%Y-%m-%d)"
W0_LOCAL="$DAY $(hhmm "$W0_MIN")"
W1_LOCAL="$DAY $(hhmm "$START_MIN")"

# Flux wants UTC. Two steps on purpose: `date -u -d "07:00"` parses the ARGUMENT
# as UTC too, so `TZ=Asia/Taipei date -u -d ...` silently returns the same clock
# time with a Z stapled on — an 8-hour error that lands the window in the middle
# of lamp hours and still looks like a plausible result. Resolve to epoch first
# (where TZ does apply), then format that instant as UTC.
W0_EPOCH="$(TZ="$TZ_NAME" date -d "$W0_LOCAL" +%s)"
W1_EPOCH="$(TZ="$TZ_NAME" date -d "$W1_LOCAL" +%s)"
START_UTC="$(date -u -d "@$W0_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
STOP_UTC="$(date -u -d "@$W1_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
# Stamp the point at the WINDOW START, not at write time. InfluxDB overwrites a
# point with the same measurement+tags+timestamp, so a re-run (or a backfill of a
# day the cron missed) corrects that day instead of adding a second point to it.
TS_NS="${W0_EPOCH}000000000"

# The conversion above is the one thing here that fails silently and plausibly, so
# it gets a self-check rather than a comment: the window must be WIN_MIN long and
# must land where the local clock says it does.
[ $(( W1_EPOCH - W0_EPOCH )) -eq $(( WIN_MIN * 60 )) ] \
  || { echo "window is $(( (W1_EPOCH-W0_EPOCH)/60 ))min, expected ${WIN_MIN}min" >&2; exit 1; }
[ "$(TZ="$TZ_NAME" date -d "@$W0_EPOCH" +%H:%M)" = "$(hhmm "$W0_MIN")" ] \
  || { echo "window start is $(TZ="$TZ_NAME" date -d "@$W0_EPOCH" +%H:%M) $TZ_NAME, expected $(hhmm "$W0_MIN")" >&2; exit 1; }

echo "=== ppfd-cal-daily $DAY  window $(hhmm "$W0_MIN")-$(hhmm "$START_MIN") $TZ_NAME  ($START_UTC → $STOP_UTC) ==="

# The human report goes to stderr so cron's log keeps it; stdout carries only the
# line protocol, which is what we parse. Grep for the prefix rather than tailing:
# the report above it is free to grow.
LINE="$(./calibrate-ppfd.sh --device "$DEVICE" --start "$START_UTC" --stop "$STOP_UTC" \
          --lux-min "$LUX_MIN" --gain "$GAIN" --tint-ms "$TINT_MS" --emit \
        | tee /dev/stderr | grep '^ppfd_cal' || true)"

[ -n "$LINE" ] || { echo "calibrate-ppfd.sh emitted no ppfd_cal line — not writing" >&2; exit 1; }

POINT="$LINE $TS_NS"
if [ "$DRY_RUN" = "1" ]; then
  echo "--dry-run, would write:"
  echo "  $POINT"
  exit 0
fi

TOKEN="$(sed -nE 's/^DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=[[:space:]]*"?([^"#[:space:]]+).*/\1/p' "$DIR/.env" 2>/dev/null | head -1 || true)"
[ -n "$TOKEN" ] || { echo "no DOCKER_INFLUXDB_INIT_ADMIN_TOKEN in $DIR/.env" >&2; exit 1; }

INFLUX_TOKEN="$TOKEN" docker exec -i -e INFLUX_TOKEN "$CONTAINER" \
  influx write --org "$ORG" --bucket "$BUCKET" --precision ns - <<<"$POINT"

echo "wrote: $POINT"
