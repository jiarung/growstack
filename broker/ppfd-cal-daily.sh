#!/usr/bin/env bash
# Daily PPFD calibration monitor. Runs calibrate-ppfd.sh over TODAY's pre-lamp
# daylight window and writes ONE point to InfluxDB measurement `ppfd_cal`, so the
# candidate CAL and its quality metrics become a series you can watch drift
# instead of a number someone re-derives by hand every few weeks.
#
#   ./ppfd-cal-daily.sh              # today's window, write the point
#   ./ppfd-cal-daily.sh --dry-run    # print what would be written, write nothing
#   LUX_MIN=1500 ./ppfd-cal-daily.sh # tighten the brightness floor for one run
#
# THE WINDOW is derived, not hardcoded: it ends when the plant light comes on and
# starts WIN_MIN minutes before that. With LIGHT_WINDOW_START_MIN=480 that is
# 07:00-08:00 local. Deriving it from the same .env vars the light controller uses
# (LIGHT_WINDOW_START_MIN and LIGHT_TZ — the minute is meaningless without the
# zone) means moving the lamp window moves this with it — the alert rules already do
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

DEVICE="${DEVICE:-livingroom}"
# The pre-lamp window is the only mode so far; Stage 5 adds the solar-noon one.
# In this mode the lamp is off because the schedule says so, not because we asked
# it to be, so there is nothing to verify — lamp_off_ok is 1 by construction.
WINDOW_MODE="pre-lamp"
LAMP_OFF_OK=1
WIN_MIN="${WIN_MIN:-60}"          # length of the pre-lamp window, minutes
# Brightness floor. calibrate-ppfd.sh defaults to 3000, which is right for the
# balcony daylight it was written for and unreachable here: measured 2026-08-17,
# this window spans 230-877 lux even after the sensor block moved to the window.
# At 3000 the series is valid=0 every single day, which is a pipeline that cannot
# ever answer the question it was built for.
#
# 300 is chosen from that measurement, not picked round: it keeps 45 of 60 minutes
# with a 2.9x brightness span, clearing both the >=20-minute and >=2x gates with
# margin, while still dropping the darkest samples. Raising it to 500 keeps only
# 15 minutes and fails.
#
# The obvious alternative — move the window to 09:00-10:00, where daylight peaks
# at ~1800 lux — was rejected on 2026-08-17: the lamp already runs to its 20:30
# extension cap EVERY day and still lands at DLI 3.0-3.9 against a target of 4.0,
# so there is no lamp time to give back. Re-open that option if the DLI budget
# ever goes into surplus.
#
# This is a monitoring floor, not a quality claim. A fit from a few hundred lux of
# indirect morning light carries more error than the +/-15-20% the method already
# admits to; treat the output as drift, never as an absolute anchor.
LUX_MIN="${LUX_MIN:-300}"
GAIN="${GAIN:-4}"                 # firmware ambient gain (AMBIENT_GAIN, sensors.cpp)
TINT_MS="${TINT_MS:-280.78}"      # (ATIME+1)*(ASTEP+1)*2.78us
CONTAINER="${CONTAINER:-monitor-air-influxdb}"
ORG="${ORG:-monitor-air}"
BUCKET="${BUCKET:-sensors}"

# take the bare value: tolerate surrounding quotes, whitespace and inline comments
envval() { sed -nE "s/^$1=[[:space:]]*\"?([^\"#[:space:]]+).*/\1/p" "$DIR/.env" 2>/dev/null | head -1 || true; }

# BOTH of these must be the vars the light controller reads (LIGHT_TZ and
# LIGHT_WINDOW_START_MIN), not just the minute. Deriving the right minute in the
# wrong timezone is not a partial win — it puts the window straight back inside
# lamp hours, which is the single failure this derivation exists to prevent.
# The first version hardcoded Asia/Taipei here and only agreed with the
# controller by coincidence; .env has set LIGHT_TZ all along.
TZ_NAME="${TZ_NAME:-$(envval LIGHT_TZ)}"
TZ_NAME="${TZ_NAME:-Asia/Taipei}"
START_MIN="$(envval LIGHT_WINDOW_START_MIN)"
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

# ---- point assembly ----
# This layer owns it, not calibrate-ppfd.sh. That script knows exactly one thing:
# what a window of data fits to. Which window we picked, whether we skipped the
# day, and whether the lamp really went off are facts only the orchestrator has —
# and on a skipped day calibrate-ppfd.sh is never even called, so it could not
# report them anyway.

# window_mode is a TAG, not a field. As a field the two modes would share one
# series and stay apart only by having different window-start timestamps, which
# is a coincidence rather than a design — and InfluxDB's field-union would let
# them overwrite each other the moment those coincided. As a tag each mode is its
# own series with its own idempotent timestamp.
add_tag() {   # <line> <key> <value>  — insert into the tag set (before the first space)
  printf '%s,%s=%s %s' "${1%% *}" "$2" "$3" "${1#* }"
}

# skipped / lamp_off_ok are written on EVERY run, never omitted. emit() drops
# None-valued fields, and field-union means an omitted field leaves the previous
# value sitting at the same timestamp — where it reads as this run's result.
assemble() {  # <base-line> <skipped> <lamp_off_ok>
  printf '%s,skipped=%s,lamp_off_ok=%s' "$(add_tag "$1" window_mode "$WINDOW_MODE")" "$2" "$3"
}

if [ "${SKIP:-0}" = "1" ]; then
  # Skipped: no fit was attempted, so there is nothing for calibrate-ppfd.sh to
  # say. The day still has to land in the series — a gap reads as "nothing wrong"
  # rather than "not measured", which is the whole reason --emit exists.
  echo "SKIP=1 — not fitting, recording the day as skipped" >&2
  POINT="$(assemble "ppfd_cal,device=$DEVICE n_kept=0,valid=0" 1 0) $TS_NS"
else
  # The human report goes to stderr so cron's log keeps it; stdout carries only the
  # line protocol, which is what we parse. Grep for the prefix rather than tailing:
  # the report above it is free to grow.
  LINE="$(./calibrate-ppfd.sh --device "$DEVICE" --start "$START_UTC" --stop "$STOP_UTC" \
            --lux-min "$LUX_MIN" --gain "$GAIN" --tint-ms "$TINT_MS" --emit \
          | tee /dev/stderr | grep '^ppfd_cal' || true)"

  [ -n "$LINE" ] || { echo "calibrate-ppfd.sh emitted no ppfd_cal line — not writing" >&2; exit 1; }

  POINT="$(assemble "$LINE" 0 "$LAMP_OFF_OK") $TS_NS"
fi
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
