#!/usr/bin/env bash
# Publish per-plant watering references as retained MQTT, so the weigh station's
# OLED can show "how far from the last full watering" while you hold the pot.
#
#   ./publish-weight-ref.sh              # compute + publish + clear stale
#   ./publish-weight-ref.sh --dry-run    # print what would happen, touch nothing
#
# For each plant: sat_g = max weight since the last qualifying watering session
# (same session rule as the Grafana panels: a day where >=8 plants gained >10 g),
# dry_g = 10th percentile of the last 60 days (NOT the min — one bad low reading
# must not inflate the denominator). Plants whose span (sat-dry) is <= 5 g are
# noise and get no reference. Payload per tag UID, retained at QoS 1:
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

# Same anchor + estimators as the "該澆水了嗎" panel (daily.json id 10). If no
# qualifying session exists in 90 d, findRecord has no record and .day errors out
# — influx exits non-zero, which IS our fail path (nothing gets touched).
read -r -d '' FLUX <<'EOF' || true
import "date"
import "timezone"
option location = timezone.location(name: "Asia/Taipei")

src = (start) => from(bucket: "sensors")
  |> range(start: start)
  |> filter(fn: (r) => r._measurement == "plant_weight" and r._field == "weight_g"
       and r.quality == "ok" and r.plant_id != "unknown")

S = (src(start: -90d)
  |> group(columns: ["plant_id"]) |> sort(columns: ["_time"])
  |> difference(columns: ["_value"], nonNegative: false)
  |> filter(fn: (r) => r._value > 10.0)
  |> map(fn: (r) => ({ r with day: date.truncate(t: r._time, unit: 1d) }))
  |> group(columns: ["day", "plant_id"]) |> distinct(column: "plant_id")
  |> group(columns: ["day"]) |> count(column: "_value")
  |> filter(fn: (r) => r._value >= 8)
  |> group() |> sort(columns: ["day"]) |> last(column: "day")
  |> findRecord(fn: (key) => true, idx: 0)).day

satW = src(start: -90d) |> range(start: S) |> group(columns: ["plant_id"]) |> max()
  |> map(fn: (r) => ({ plant_id: r.plant_id, k: "sat", v: r._value }))
dryW = src(start: -60d) |> group(columns: ["plant_id"])
  |> quantile(q: 0.1, method: "exact_mean")
  |> map(fn: (r) => ({ plant_id: r.plant_id, k: "dry", v: r._value }))

union(tables: [satW, dryW]) |> group()
  |> pivot(rowKey: ["plant_id"], columnKey: ["k"], valueColumn: "v")
  |> filter(fn: (r) => exists r.sat and exists r.dry and r.sat - r.dry > 5.0)
  |> map(fn: (r) => ({ plant_id: r.plant_id, sat: r.sat, dry: r.dry, anchor: string(v: S) }))
EOF

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
docker exec -i "$INFLUX" influx query --org "$ORG" --raw -f /dev/stdin <<<"$FLUX" > "$TMP/rows.csv"

# CSV + tag-map -> one publish plan: "topic<TAB>payload" lines, plus warnings.
# All validation lives here; the shell below only ships what this emits.
# Exit 4 (an untagged plant) still publishes the rest, so it must not trip set -e
# here — it is re-raised at the end where cron can see it.
PLAN_RC=0
python3 - "$TMP" "$TAG_MAP" "$PREFIX" > "$TMP/plan" <<'PY' || PLAN_RC=$?
import csv, json, math, sys, os
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
    try:
        sat, dry = float(r["sat"]), float(r["dry"])
    except (KeyError, ValueError):
        print(f"bad row for {plant}: {r} — schema changed?", file=sys.stderr)
        sys.exit(1)
    if not (math.isfinite(sat) and math.isfinite(dry) and sat - dry > 5.0):
        print(f"bad values for {plant}: sat={sat} dry={dry}", file=sys.stderr)
        sys.exit(1)
    anchor = r.get("anchor", "")[:10]
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
