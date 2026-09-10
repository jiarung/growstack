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
# Three tiers, retained at QoS 1 per tag UID:
#
#   full        monitor-air/ref/weight/<uid>  {"plant_id":...,"sat_g":...,"dry_g":...,"anchor_day":...}
#   provisional monitor-air/ref/weight/<uid>  {"plant_id":...,"sat_g":...,"provisional":true,"anchor_day":...}
#   name-only   monitor-air/ref/weight/<uid>  {"plant_id":...,"name_only":true}
#
# name-only = the tag is mapped but the plant has NO watering anchor yet (a new
# pot that has never been watered on the scale): there is nothing honest to say
# about water, but the OLED can still greet the plant by name instead of a raw
# UID. EVERY uid in tag-map.json therefore always has a retained ref; the tiers
# upgrade in place (name-only -> provisional -> full) as the plant earns them.
#
# Provisional = the plant HAS a watering anchor but the panel itself refuses to
# score it (span <= 5 g: new pot, repot, tiny history): the OLED then shows the
# honest absolute drawdown ("-87g since wtr") and NO percentage — a % against a
# too-small span reads "drier than reality" and nudges overwatering. The tier
# is decided here by the SAME panel rows: the panel's span>5g display filter is
# asserted and stripped below, so span-poor plants surface instead of vanishing.
# Plants with no watering anchor at all get the name-only floor (see above).
#
# PRECISE GUARANTEE: the OLED shows a % ONLY for spans earned by a completed
# dry-down cycle (the panel's basis == "循環"). 暫用-p10 rows go provisional
# too: that denominator errs LARGE only over a LONG history — over a new pot's
# short history it errs SMALL, which inflates the %, the overwatering
# direction. (This TIGHTENS prior behavior: an established pot with no
# completed cycle in 60d drops from % to the absolute line until it earns one;
# regulars with normal watering cadence all carry 循環 basis and keep their %.)
#
# DEPLOY ORDER, 2026-09-10 — the legacy keys sat_g/dry_g/anchor_day are GONE, which
# was only safe once the station ran firmware that reads anchor_g/span_g/anchor_ts.
# That flash was confirmed before this change; the ordering mattered because old
# firmware drops payloads missing sat_g/dry_g yet keeps any previously cached full
# ref in RAM until reboot, so a demotion would have left it showing a stale % —
# and clearing a retained topic cannot reach a station that is offline.
#   Consequence for the future: a station rolled BACK to pre-2026-09-10 firmware
# would see every payload as name-only and simply draw no ref line. That is the
# honest failure, not a wrong number, which is why no compatibility shim is kept.
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
# Same idea for the first-anchor filter: the panel hides pots whose anchor is only
# their FIRST weighing (not a watering), because a depletion% against it would be
# fabricated. Here they must come through, as their own tier, so the OLED can show
# the honest grams and mark them "1st".
first_filter = "|> filter(fn: (r) => r.afirst == 0.0)"
assert first_filter in q, "panel 10's first-anchor filter moved/changed — update publish-weight-ref.sh"
print(q.replace(span_filter, "").replace(first_filter, ""))
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

# ZERO anchored plants is a real state, not only a data problem: every anchor
# can age out of the panel's 90d window. Since the name-only floor exists, the
# honest move is to publish the downgrade (stale full/provisional refs would
# otherwise keep showing water lines nobody earned) — but LOUDLY, because a
# panel schema break can also masquerade as an empty result. The per-row
# sat/basis hard-bails still catch broken columns before this point when rows
# DO come back.
if not rows:
    print("WARNING: panel returned ZERO anchored plants — every mapped uid is "
          "being downgraded to a name-only ref. Expected only if all watering "
          "anchors aged out of the 90d window; otherwise check panel 10.",
          file=sys.stderr)

untagged = []
published = set()          # plants that got a full/provisional ref this round
unwatered = []          # sat_g absent — since the first-weighing fallback landed
                        # this is NO LONGER the new-pot state (every pot with any
                        # reading gets an anchor). It now means schema drift. Kept
                        # as a non-fatal guard only because crashing here stopped
                        # EVERY ref from publishing for three runs on 2026-08-31.
first_anchored = []     # anchor is the pot's FIRST weighing, not a watering
for r in sorted(rows, key=lambda r: r["plant_id"]):
    plant = r["plant_id"]
    if not (r.get("sat_g") or "").strip():
        unwatered.append(plant)
        continue
    # anchor_g and first_anchor DECIDE THE PAYLOAD SHAPE, so schema drift in either
    # must abort the round rather than silently mis-tier every pot.
    try:
        anchor_g = float(r["anchor_g"])
        first = float(r["first_anchor"]) > 0.5
    except (KeyError, ValueError):
        print(f"bad row for {plant}: missing anchor_g/first_anchor — panel schema changed?",
              file=sys.stderr)
        sys.exit(1)
    try:                                   # absent span -> provisional tier below
        span = float(r["span_g"])
    except (KeyError, ValueError):
        span = float("nan")
    if not math.isfinite(anchor_g):
        print(f"bad values for {plant}: anchor_g={anchor_g}", file=sys.stderr)
        sys.exit(1)
    # tier decision: FULL only for a span EARNED by a completed dry-down cycle
    # (basis == "循環") — a p10-basis span over a short history errs small and
    # would inflate the %. Anything else is PROVISIONAL: an anchor with no span.
    # Span is judged on the ROUNDED values that actually ship: a raw 5.03 g
    # rounds to 5.0 on the wire and the firmware's own >5 g guard would then
    # reject the "full" payload outright — worse than provisional.
    basis = r.get("basis")
    if basis not in ("循環", "暫用 p10"):
        print(f"bad basis for {plant}: {basis!r} — panel schema changed?", file=sys.stderr)
        sys.exit(1)
    # `not first` is unreachable by construction (a completed cycle REQUIRES a >10 g
    # jump, which is exactly a watering anchor) — one token, and it makes the
    # invariant explicit instead of implied.
    full = (not first) and basis == "循環" and math.isfinite(span) and round(span, 1) > 5.0
    # anchor_ts is what the OLED uses: it recomputes the age live from
    # time(nullptr), so the displayed "3.6D" never goes stale between hourly runs.
    # It derives from the panel's `days` rather than a time column because that
    # pivot's value column is float and unioning a time into it is a type error.
    # days = (now()-anchor)/86400e9, so this inverts to sub-millisecond accuracy.
    # Taipei is a fixed UTC+8 with no DST, so no tz database is needed (cron's
    # python3 may lack one).
    try:
        anchor_dt = (dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
                     - dt.timedelta(days=float(r["days"])))
        anchor_ts = int(anchor_dt.timestamp())
    except (KeyError, ValueError):
        anchor_ts = None
    uids = plant_to_uids.get(plant)
    if not uids:
        untagged.append(plant)
        continue
    # One tier is one shape. The station reads anchor_g, span_g, anchor_ts and
    # first_anchor and nothing else, so nothing else is sent.
    if first:
        d = {"plant_id": plant, "first_anchor": True, "anchor_g": round(anchor_g, 1)}
        first_anchored.append(plant)
    elif full:
        d = {"plant_id": plant, "anchor_g": round(anchor_g, 1),
             "span_g": round(span, 1)}
    else:
        # No span_g: the firmware can then never compute a % from an unearned one.
        d = {"plant_id": plant, "provisional": True, "anchor_g": round(anchor_g, 1)}
        print(f"provisional (span not yet earned): {plant}", file=sys.stderr)
    if anchor_ts is not None:
        d["anchor_ts"] = anchor_ts
    payload = json.dumps(d)
    # The size limit lives on the far side of the wire (src/weight_ref.cpp
    # PAYLOAD_MAX = 192): over it the station drops the message SILENTLY and keeps
    # whatever it had cached. Check here, where it can still be a log line.
    if len(payload.encode()) > 192:
        print(f"WARNING: {plant} payload {len(payload.encode())} B > 192 B — the "
              f"station will drop it silently and keep its stale cached ref",
              file=sys.stderr)
    for uid in uids:
        print(f"{prefix}/{uid}\t{payload}")
    published.add(plant)

# NAME-ONLY tier: every mapped uid whose plant produced no panel row (no
# watering anchor yet — brand-new pot) still gets a ref carrying just the
# name, so the OLED greets the plant instead of showing a raw UID. Upgrades
# happen in place: the plant's first scale-witnessed watering (>10g jump)
# creates its anchor and the next round overwrites this topic.
name_only = sorted(p for p in plant_to_uids if p not in published)
for plant in name_only:
    payload = json.dumps({"plant_id": plant, "name_only": True})
    for uid in plant_to_uids[plant]:
        print(f"{prefix}/{uid}\t{payload}")
if name_only:
    print("name-only (no watering anchor yet): " + ", ".join(name_only), file=sys.stderr)

if first_anchored:
    print("first-weighing anchor (no watering on record): " + ", ".join(first_anchored),
          file=sys.stderr)
if unwatered:
    print(f"WARNING: {len(unwatered)} pot(s) reached the plan with an EMPTY sat_g. "
          f"Since the first-weighing fallback this should be impossible — suspect "
          f"panel 10 schema drift, not new pots: " + ", ".join(unwatered), file=sys.stderr)
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
