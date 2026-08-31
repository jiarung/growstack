#!/usr/bin/env bash
# Publish per-plant watering references as retained MQTT, so the weigh station's
# OLED can show "how far from the last full watering" while you hold the pot.
#
#   ./publish-weight-ref.sh              # compute + publish + clear stale
#   ./publish-weight-ref.sh --dry-run    # print what would happen, touch nothing
#
# The sat/dry definition is NOT duplicated here — this reads the Flux straight out
# of the 該澆水了嗎 panel (daily.json id 10) and ships what it computes. sat_g is
# each plant's max since ITS OWN last watering; dry_g is the panel's trig_g, i.e.
# sat minus the largest drawdown that plant has actually completed in 60 days.
# Two tiers, retained at QoS 1 per tag UID:
#
#   full        monitor-air/ref/weight/<uid>  {"plant_id":...,"sat_g":...,"dry_g":...,"anchor_day":...}
#   provisional monitor-air/ref/weight/<uid>  {"plant_id":...,"sat_g":...,"provisional":true,"anchor_day":...}
#
# Provisional = the plant HAS a watering anchor but the panel itself refuses to
# score it (span <= 5 g: new pot, repot, tiny history): the OLED then shows the
# honest absolute drawdown ("-87g since wtr") and NO percentage — a % against a
# too-small span reads "drier than reality" and nudges overwatering. The tier
# is decided here by the SAME panel rows: the panel's span>5g display filter is
# asserted and stripped below, so span-poor plants surface instead of vanishing.
# Plants with no watering anchor at all stay absent (nothing honest to show).
#
# PRECISE GUARANTEE: the OLED shows a % ONLY for spans earned by a completed
# dry-down cycle (the panel's basis == "循環"). 暫用-p10 rows go provisional
# too: that denominator errs LARGE only over a LONG history — over a new pot's
# short history it errs SMALL, which inflates the %, the overwatering
# direction. (This TIGHTENS prior behavior: an established pot with no
# completed cycle in 60d drops from % to the absolute line until it earns one;
# regulars with normal watering cadence all carry 循環 basis and keep their %.)
#
# DEPLOY ORDER: flash the station firmware that understands provisional refs
# BEFORE first running this version. Old firmware drops a dry-less payload but
# keeps any previously cached full ref in RAM until reboot — a full->provisional
# demotion would leave it showing a stale % (retained clearing can't fix an
# offline station either; ordering is the real fix, and we own the one station).
#
# Retained lifecycle: after a SUCCESSFUL query round, this round's valid set is
# authoritative — any previously retained ref not in it (plant re-tagged, data
# marked suspect, span collapsed) is cleared with an empty retained publish. On
# ANY query failure the script exits non-zero and touches nothing: a failed
# query means "we don't know", not "the references are invalid".
#
# A plant with history but no tag is NORMAL, not a fault: repotting retires an id
# and moves its tag to the successor, so the list only ever grows (5 as of
# 2026-08-31). It used to warn per plant and exit 4 — five lines an hour that no
# action could ever clear. Now it is one informational line and exit 0.
#
# Exit codes: 0 ok · 1 query/parse failure (nothing touched).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

INFLUX="${CONTAINER:-monitor-air-influxdb}"
MQTT="${MQTT_CONTAINER:-monitor-air-mqtt}"
ORG="${ORG:-monitor-air}"
BUCKET="${BUCKET:-sensors}"
PREFIX="${PREFIX:-monitor-air/ref/weight}"
TAG_MAP="${TAG_MAP:-node-red/tag-map.json}"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

# One definition, not two. This script and the panel used to carry separate copies
# of the same Flux; on 2026-08-22 the panel's anchor was fixed (per plant instead of
# per global session) and this copy was not, so every pot skipped in the last group
# watering has been reading 0% on the OLED ever since. Reading the panel's query
# makes that class of drift impossible rather than merely detectable.
FLUX="$(python3 - <<'PY'
import json
q = next(p for p in json.load(open("grafana/provisioning/dashboards/daily.json"))["panels"]
         if p["id"] == 10)["targets"][0]["query"]
# The panel hardcodes its own ranges today. If someone switches it to the dashboard
# time picker there is no time range to supply headless, and silently publishing
# refs computed over the wrong window would be worse than not publishing.
assert "v.timeRange" not in q, "panel 10 now uses dashboard time variables — cannot run headless"
# Strip the panel's DISPLAY filter so span-poor plants (new pots) reach the plan
# as provisional refs instead of vanishing. Asserted verbatim: if the panel's
# filter line changes shape, fail loudly here rather than silently republishing
# full refs for plants whose span no longer qualifies.
span_filter = "|> filter(fn: (r) => r.span > 5.0)"
assert span_filter in q, "panel 10's span filter moved/changed — update publish-weight-ref.sh"
print(q.replace(span_filter, ""))
PY
)"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
docker exec -i "$INFLUX" influx query --org "$ORG" --raw -f /dev/stdin <<<"$FLUX" > "$TMP/rows.csv"

# CSV + tag-map -> one publish plan: "topic<TAB>payload" lines, plus notices.
# All validation lives here; the shell below only ships what this emits.
python3 - "$TMP" "$TAG_MAP" "$PREFIX" > "$TMP/plan" <<'PY'
import csv, datetime as dt, json, math, sys, os
tmp, tag_map_path, prefix = sys.argv[1], sys.argv[2], sys.argv[3]

uid_to_plant = json.load(open(tag_map_path))
plant_to_uids = {}
for uid, plant in uid_to_plant.items():
    plant_to_uids.setdefault(plant, []).append(uid)

rows, hdr = [], None
for r in csv.reader(open(os.path.join(tmp, "rows.csv"))):
    if not r:                  continue
    if r[0].startswith("#"):   hdr = None; continue
    if hdr is None:            hdr = r;    continue
    rows.append(dict(zip(hdr, r)))
rows = [r for r in rows if r.get("plant_id")]

# An empty result after a "successful" query is indistinguishable from a data
# problem — clearing every retained ref over it would be destructive. Bail.
if not rows:
    print("no plants passed the span filter — refusing to touch retained refs", file=sys.stderr)
    sys.exit(1)

untagged = []
for r in sorted(rows, key=lambda r: r["plant_id"]):
    plant = r["plant_id"]
    # dry_g is the panel's trig_g — the weight this pot reaches when it has given
    # back as much water as it ever has. The firmware's (sat-w)/(sat-dry) then IS
    # the panel's depletion%, so src/ needs no change for this to take effect.
    try:
        sat, dry = float(r["sat_g"]), float(r["trig_g"])
    except (KeyError, ValueError):
        print(f"bad row for {plant}: {r} — schema changed?", file=sys.stderr)
        sys.exit(1)
    if not math.isfinite(sat):
        print(f"bad values for {plant}: sat={sat}", file=sys.stderr)
        sys.exit(1)
    # tier decision: FULL only for a span EARNED by a completed dry-down cycle
    # (basis == "循環") — a p10-basis span over a short history errs small and
    # would inflate the %. Anything else is PROVISIONAL: sat only, no dry_g,
    # so the firmware can never compute a % from an unearned span.
    # Span is judged on the ROUNDED values that actually ship: a raw 5.03 g
    # rounds to 5.0 on the wire and the firmware's own >5 g guard would then
    # reject the "full" payload outright — worse than provisional.
    basis = r.get("basis")
    if basis not in ("循環", "暫用 p10"):
        print(f"bad basis for {plant}: {basis!r} — panel schema changed?", file=sys.stderr)
        sys.exit(1)
    full = basis == "循環" and math.isfinite(dry) and round(sat, 1) - round(dry, 1) > 5.0
    # anchor_day is decorative — the firmware ignores it (src/weight_ref.cpp reads
    # only sat_g/dry_g). It is derived from the panel's `days` rather than carried
    # as its own column because that pivot's value column is float, and unioning a
    # time into it is a type error. days = (now()-anchor)/86400e9, so this inverts
    # to sub-millisecond accuracy — the date truncation is exact. Taipei is a fixed
    # UTC+8 with no DST, so no tz database is needed (cron's python3 may lack one).
    try:
        anchor = (dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
                  - dt.timedelta(days=float(r["days"]))).date().isoformat()
    except (KeyError, ValueError):
        anchor = ""
    uids = plant_to_uids.get(plant)
    if not uids:
        untagged.append(plant)
        continue
    if full:
        payload = json.dumps({"plant_id": plant, "sat_g": round(sat, 1),
                              "dry_g": round(dry, 1), "anchor_day": anchor})
    else:
        payload = json.dumps({"plant_id": plant, "sat_g": round(sat, 1),
                              "provisional": True, "anchor_day": anchor})
        print(f"provisional (span not yet earned): {plant}", file=sys.stderr)
    for uid in uids:
        print(f"{prefix}/{uid}\t{payload}")

if untagged:
    print(f"note: {len(untagged)} retired id(s) with history but no tag, no ref published: "
          + ", ".join(untagged), file=sys.stderr)
PY

cut -f1 "$TMP/plan" | sort > "$TMP/expected"
N="$(wc -l < "$TMP/expected" | tr -d ' ')"
echo "computed $N reference(s):"
sed 's/\t/  /' "$TMP/plan"

# Enumerate what is currently retained under the prefix. -W always "times out"
# after collecting retained messages, and mosquitto_sub signals that with exit
# 27 — that is its normal end-of-run here, not an error (same as light-ctl.sh).
docker exec "$MQTT" mosquitto_sub -t "$PREFIX/+" --retained-only -W 2 -F '%t' \
  > "$TMP/current" 2>/dev/null || [ $? -eq 27 ]
sort -u -o "$TMP/current" "$TMP/current"
STALE="$(comm -23 "$TMP/current" "$TMP/expected")"

if [ "$DRY" = "1" ]; then
  [ -n "$STALE" ] && printf 'would clear stale retained:\n%s\n' "$STALE"
  echo "--dry-run: nothing published"; exit 0
fi

while IFS=$'\t' read -r topic payload; do
  docker exec "$MQTT" mosquitto_pub -q 1 -r -t "$topic" -m "$payload"
done < "$TMP/plan"
echo "published $N retained reference(s) under $PREFIX/"

if [ -n "$STALE" ]; then
  while IFS= read -r topic; do
    docker exec "$MQTT" mosquitto_pub -q 1 -r -n -t "$topic"   # empty retained = clear
    echo "cleared stale retained: $topic"
  done <<<"$STALE"
fi
