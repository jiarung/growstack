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
# Plants whose span is <= 5 g are noise and get no reference. Payload per tag UID,
# retained at QoS 1:
#
#   monitor-air/ref/weight/<uid>  {"plant_id":...,"sat_g":...,"dry_g":...,"anchor_day":...}
#
# Retained lifecycle: after a SUCCESSFUL query round, this round's valid set is
# authoritative — any previously retained ref not in it (plant re-tagged, data
# marked suspect, span collapsed) is cleared with an empty retained publish. On
# ANY query failure the script exits non-zero and touches nothing: a failed
# query means "we don't know", not "the references are invalid".
#
# Exit codes: 0 ok · 1 query/parse failure (nothing touched) · 4 published, but
# some plant has no tag in tag-map.json (visible in cron mail; refs for the
# others were still published).
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
print(q)
PY
)"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
docker exec -i "$INFLUX" influx query --org "$ORG" --raw -f /dev/stdin <<<"$FLUX" > "$TMP/rows.csv"

# CSV + tag-map -> one publish plan: "topic<TAB>payload" lines, plus warnings.
# All validation lives here; the shell below only ships what this emits.
# Exit 4 (an untagged plant) still publishes the rest, so it must not trip set -e
# here — it is re-raised at the end where cron can see it.
PLAN_RC=0
python3 - "$TMP" "$TAG_MAP" "$PREFIX" > "$TMP/plan" <<'PY' || PLAN_RC=$?
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
    if not (math.isfinite(sat) and math.isfinite(dry) and sat - dry > 5.0):
        print(f"bad values for {plant}: sat={sat} dry={dry}", file=sys.stderr)
        sys.exit(1)
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
    payload = json.dumps({"plant_id": plant, "sat_g": round(sat, 1),
                          "dry_g": round(dry, 1), "anchor_day": anchor})
    for uid in uids:
        print(f"{prefix}/{uid}\t{payload}")

for plant in untagged:
    print(f"WARNING: {plant} has sat/dry but no tag in tag-map.json — no ref published", file=sys.stderr)
sys.exit(4 if untagged else 0)
PY
[ "$PLAN_RC" -eq 0 ] || [ "$PLAN_RC" -eq 4 ] || exit "$PLAN_RC"

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

exit "$PLAN_RC"
