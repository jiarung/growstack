#!/usr/bin/env bash
# Daily PPFD calibration monitor. Runs calibrate-ppfd.sh over TODAY's pre-lamp
# daylight window and writes ONE point to InfluxDB measurement `ppfd_cal`, so the
# candidate CAL and its quality metrics become a series you can watch drift
# instead of a number someone re-derives by hand every few weeks.
#
#   ./ppfd-cal-daily.sh              # today's window, write the point
#   ./ppfd-cal-daily.sh --dry-run    # print what would be written, write nothing
#   ./ppfd-cal-daily.sh --window pre-lamp   # the default, named explicitly
#   LUX_MIN=1500 ./ppfd-cal-daily.sh # tighten the brightness floor for one run
#   SKIP=1 ./ppfd-cal-daily.sh       # record the day as skipped without fitting
#
# THE WINDOW is derived, not hardcoded: it ends when the plant light comes on and
# starts SPAN_MIN minutes before that. With LIGHT_WINDOW_START_MIN=480 and the
# default 120-minute span that is 06:00-08:00 local.
#
# This window is FREE: the lamp is off on its own schedule, so nothing here ever
# touches it, and the measurement costs the plant no supplemental light. That is
# why it is the daily default rather than the brighter solar-noon window, which
# would have to switch the lamp off and spend DLI the budget does not have. Deriving it from the same .env vars the light controller uses
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
# The pre-lamp window is the only mode so far; Stage 5 adds the solar-noon one.
# In this mode the lamp is off because the schedule says so, not because we asked
# it to be, so there is nothing to verify — lamp_off_ok is 1 by construction.
WINDOW_MODE="pre-lamp"
LAMP_OFF_OK=1

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift;;
    --window)  WINDOW_MODE="${2:?--window needs a value}"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done
case "$WINDOW_MODE" in
  pre-lamp|noon) ;;
  *) echo "unknown --window mode: $WINDOW_MODE (pre-lamp | noon)" >&2; exit 1;;
esac
# noon holds the lamp off, so nothing is true by construction there — it has to be
# verified after the fact, and until then the honest value is 0.
[ "$WINDOW_MODE" = "noon" ] && LAMP_OFF_OK=0

DEVICE="${DEVICE:-livingroom}"
# Window length. 120 comes from measuring both on 2026-08-17: 120 minutes keeps 57
# samples at drift 20%, 60 keeps 45 at drift 17%, and the two fits land 1.4% apart
# — well inside every gate either way. 120 is the choice because the >=20-minute
# gate is the one with the least margin, and samples are what buy margin there.
# Shorten it if drift creeps toward the 30% limit as the season moves.
SPAN_MIN="${SPAN_MIN:-120}"
# --- noon mode only ---
HALF_WIN="${HALF_WIN:-15}"        # window is solar noon +/- this, so 30 minutes
LEAD="${LEAD:-120}"               # switch the lamp off this early, so the whole
                                  # window is clean rather than trimming the front
# Daylight floor checked BEFORE touching the lamp. The point is not data quality —
# the three acceptance gates already handle that — it is not spending lamp time on
# a day that cannot produce a fit. Read from lux_ref, which is shielded from the
# lamp and so measures daylight directly while the lamp is still on. Open-Meteo's
# rain would be the obvious alternative and is worse: hourly resolution on a
# grid-cell forecast, against a 30-minute window.
# 500 is a guess — there is no history to derive it from. Revisit once a few noon
# runs exist, using their lux_lo / n_kept / valid.
LUX_REF_MIN="${LUX_REF_MIN:-500}"
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

hhmm() { printf '%02d:%02d' $(( $1 / 60 )) $(( $1 % 60 )); }
DAY="$(TZ="$TZ_NAME" date +%Y-%m-%d)"

if [ "$WINDOW_MODE" = "noon" ]; then
  # Centred on solar noon, where the air mass is ~1.02 against 2.27 in the morning
  # window — the spectrum is closest to the sunlight that lux/54 assumes. That is
  # the entire reason this mode exists, and the reason it is not the daily default
  # is that it costs lamp time the DLI budget does not have.
  NOON_EPOCH="$(./solar-noon.py --noon-epoch)"
  W0_EPOCH=$(( NOON_EPOCH - HALF_WIN * 60 ))
  W1_EPOCH=$(( NOON_EPOCH + HALF_WIN * 60 ))
else
  [ "$START_MIN" -gt "$SPAN_MIN" ] || { echo "lamp starts at $START_MIN min — no room for a ${SPAN_MIN}min window before it" >&2; exit 1; }
  W0_MIN=$(( START_MIN - SPAN_MIN ))
  # Flux wants UTC. Two steps on purpose: `date -u -d "07:00"` parses the ARGUMENT
  # as UTC too, so `TZ=Asia/Taipei date -u -d ...` silently returns the same clock
  # time with a Z stapled on — an 8-hour error that lands the window in the middle
  # of lamp hours and still looks like a plausible result. Resolve to epoch first
  # (where TZ does apply), then format that instant as UTC.
  W0_EPOCH="$(TZ="$TZ_NAME" date -d "$DAY $(hhmm "$W0_MIN")" +%s)"
  W1_EPOCH="$(TZ="$TZ_NAME" date -d "$DAY $(hhmm "$START_MIN")" +%s)"
fi
START_UTC="$(date -u -d "@$W0_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
STOP_UTC="$(date -u -d "@$W1_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
# Stamp the point at the WINDOW START, not at write time. InfluxDB overwrites a
# point with the same measurement+tags+timestamp, so a re-run (or a backfill of a
# day the cron missed) corrects that day instead of adding a second point to it.
TS_NS="${W0_EPOCH}000000000"

# The conversion above is the one thing here that fails silently and plausibly, so
# it gets a self-check rather than a comment: the window must be the length we
# asked for, and must land where the local clock says it does.
if [ "$WINDOW_MODE" = "noon" ]; then
  EXPECT_MIN=$(( HALF_WIN * 2 ))
  EXPECT_START="$(TZ="$TZ_NAME" date -d "@$(( NOON_EPOCH - HALF_WIN * 60 ))" +%H:%M)"
else
  EXPECT_MIN="$SPAN_MIN"
  EXPECT_START="$(hhmm "$W0_MIN")"
fi
[ $(( W1_EPOCH - W0_EPOCH )) -eq $(( EXPECT_MIN * 60 )) ] \
  || { echo "window is $(( (W1_EPOCH-W0_EPOCH)/60 ))min, expected ${EXPECT_MIN}min" >&2; exit 1; }
[ "$(TZ="$TZ_NAME" date -d "@$W0_EPOCH" +%H:%M)" = "$EXPECT_START" ] \
  || { echo "window start is $(TZ="$TZ_NAME" date -d "@$W0_EPOCH" +%H:%M) $TZ_NAME, expected $EXPECT_START" >&2; exit 1; }

echo "=== ppfd-cal-daily $DAY [$WINDOW_MODE]  window $(TZ="$TZ_NAME" date -d "@$W0_EPOCH" +%H:%M)-$(TZ="$TZ_NAME" date -d "@$W1_EPOCH" +%H:%M) $TZ_NAME  ($START_UTC → $STOP_UTC) ==="

# ---- noon mode: gate on daylight, then hold the lamp off across the window ----
if [ "$WINDOW_MODE" = "noon" ] && [ "$DRY_RUN" = "0" ]; then
  NOW=$(date +%s)
  if [ "$NOW" -ge "$W1_EPOCH" ]; then
    # The window has already passed. Nothing to hold; just fit what was recorded
    # and let the check below report what the lamp was actually doing. This is how
    # you re-analyse a day, and it is honest: it cannot claim the lamp was off.
    echo "window already over — fitting recorded data, no lamp action" >&2
  else
    REF="$(docker exec -i "$CONTAINER" influx query --org "$ORG" --raw -f /dev/stdin <<FLUX 2>/dev/null | awk -F, '!/^#/ && NF>3 && $0 !~ /_value/ { v=$NF } END { gsub(/\r/,"",v); print v }'
from(bucket: "$BUCKET")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "air" and r.device == "$DEVICE" and r._field == "lux_ref")
  |> median()
  |> keep(columns: ["_value"])
FLUX
)"
    REF="${REF:-0}"
    echo "daylight check: lux_ref median ${REF} over the last 5 min (floor $LUX_REF_MIN)" >&2
    if [ "${REF%%.*}" -lt "$LUX_REF_MIN" ]; then
      echo "too dark — skipping, the lamp is not touched" >&2
      SKIP=1
    else
      WAIT_UNTIL=$(( W0_EPOCH - LEAD ))
      if [ "$NOW" -lt "$WAIT_UNTIL" ]; then
        echo "waiting $(( WAIT_UNTIL - NOW ))s until $(TZ="$TZ_NAME" date -d "@$WAIT_UNTIL" +%H:%M:%S) to switch the lamp off" >&2
        sleep $(( WAIT_UNTIL - NOW ))
      fi
      # Blocks through the whole window and hands the lamp back on the way out —
      # including if this script is killed, and if it is not, within ~6 minutes
      # anyway once the suppression lapses.
      ./lamp-hold.sh $(( W1_EPOCH - $(date +%s) )) >&2
      sleep 20   # let Telegraf flush the last samples of the window
    fi
  fi
  if [ "${SKIP:-0}" != "1" ]; then
    # Verified, never assumed. A failed plug switch leaves the controller's state
    # untouched and only flips availability, so "we sent OFF" says nothing about
    # whether the lamp went off — and fitting lamp light against lux/54 produces a
    # wrong CAL that passes every gate.
    if ./lamp-hold.sh check "$W0_EPOCH" "$W1_EPOCH" >&2; then LAMP_OFF_OK=1; else LAMP_OFF_OK=0; fi
  fi
fi

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
  # lamp_off_ok keeps the mode's own answer rather than a hardcoded 0. In pre-lamp
  # the lamp was off on its own schedule whether or not we fitted, so 0 there would
  # show a dark skipped day as a FAILED lamp check — a different, alarming fact.
  POINT="$(assemble "ppfd_cal,device=$DEVICE n_kept=0,valid=0" 1 "$LAMP_OFF_OK") $TS_NS"
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
