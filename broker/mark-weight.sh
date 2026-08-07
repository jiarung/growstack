#!/usr/bin/env bash
# Re-label plant_weight records — a soft delete, so nothing is ever lost.
#
#   ./mark-weight.sh --at '2026-08-07 07:59:45' deleted        # one point
#   ./mark-weight.sh --from '2026-08-06 12:00' --to '2026-08-06 13:00' suspect
#   ./mark-weight.sh --from '2026-08-06 12:00' --to '2026-08-06 13:00' --plant cactus-01 suspect
#   ./mark-weight.sh --from ... --to ... ok            # change your mind back
#   ./mark-weight.sh --from ... --to ... --dry-run suspect
#
# Times are LOCAL (Asia/Taipei) and inclusive of --from, exclusive of --to.
# `--at` targets a single reading: it is --from <t> --to <t+1s>. Add --plant if two
# plants were weighed in the same second. `deleted` is the conventional label for
# "this reading is wrong" — panels show only quality == "ok", so any other label
# hides it, and re-running with `ok` brings it back.
#
# InfluxDB cannot update a tag in place, so this reads the affected points,
# rewrites them with the new `quality` at their ORIGINAL nanosecond timestamps,
# and only then deletes the old copies. Write-then-delete on purpose: a failure at
# any step loses nothing. Every run also dumps what it touched to
# /data/influx-backups/ first.
#
# Panels filter on quality == "ok", so marking something `suspect` hides it from
# the charts while keeping it in the database and reversible with one more run.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

TZ_NAME="${TZ_NAME:-Asia/Taipei}"
CONTAINER="${CONTAINER:-monitor-air-influxdb}"
ORG="${ORG:-monitor-air}"
BUCKET="${BUCKET:-sensors}"
BACKUP_DIR="${BACKUP_DIR:-/data/influx-backups}"

FROM=""; TO=""; AT=""; PLANT=""; DRY=0; QUALITY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --at)      AT="$2";   shift 2 ;;
    --from)    FROM="$2"; shift 2 ;;
    --to)      TO="$2";   shift 2 ;;
    --plant)   PLANT="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -*) echo "unknown flag: $1" >&2; exit 1 ;;
    *)  QUALITY="$1"; shift ;;
  esac
done
if [ -n "$AT" ]; then
  [ -z "$FROM$TO" ] || { echo "--at cannot be combined with --from/--to" >&2; exit 1; }
  FROM="$AT"; TO="$AT"       # widened to [t, t+1s) below, in epoch space
fi
[ -n "$FROM" ] && [ -n "$TO" ] && [ -n "$QUALITY" ] || {
  echo "usage: $(basename "$0") (--at '<local time>' | --from '<t>' --to '<t>') [--plant <id>] [--dry-run] <quality>" >&2
  echo "       quality is a free label; panels show only 'ok'. Use 'deleted' to retire a bad reading." >&2; exit 1; }
[[ "$QUALITY" =~ ^[a-z_]{1,20}$ ]] || { echo "quality must be [a-z_]{1,20}" >&2; exit 1; }

# GNU date traps, both hit here: `TZ=X date -d "..." -u` does NOT convert (the
# prefix affects parsing, then -u prints the same wall-clock digits back, so a
# Taipei time comes out labelled Z and the window misses by 8 hours), and a
# relative offset like "+1 second" re-emits in the SYSTEM zone, not $TZ_NAME.
# Going through epoch seconds sidesteps both — the arithmetic happens on an
# absolute instant, and the zone is applied exactly once, at parse time.
START_EPOCH="$(date -d "TZ=\"$TZ_NAME\" $FROM" +%s)"
STOP_EPOCH="$(date  -d "TZ=\"$TZ_NAME\" $TO"   +%s)"
[ -n "$AT" ] && STOP_EPOCH=$((START_EPOCH + 1))
[ "$STOP_EPOCH" -gt "$START_EPOCH" ] || { echo "--to must be after --from" >&2; exit 1; }
START_UTC="$(date -u -d "@$START_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
STOP_UTC="$(date  -u -d "@$STOP_EPOCH"  +%Y-%m-%dT%H:%M:%SZ)"
PRED='_measurement="plant_weight"'
[ -n "$PLANT" ] && PRED="$PRED AND plant_id=\"$PLANT\""

read -r -d '' FLUX <<EOF || true
from(bucket: "$BUCKET")
  |> range(start: $START_UTC, stop: $STOP_UTC)
  |> filter(fn: (r) => r._measurement == "plant_weight")
  $( [ -n "$PLANT" ] && echo "|> filter(fn: (r) => r.plant_id == \"$PLANT\")" )
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "device", "plant_id", "quality", "weight_g", "uid"])
EOF

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
docker exec -i "$CONTAINER" influx query --org "$ORG" --raw -f /dev/stdin <<<"$FLUX" > "$TMP/rows.csv"

python3 - "$TMP" "$QUALITY" "$DRY" "$TZ_NAME" <<'PY'
import csv, sys, os, datetime as dt
from zoneinfo import ZoneInfo
tmp, quality, dry, tzname = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
tz = ZoneInfo(tzname)

rows, hdr = [], None
for r in csv.reader(open(os.path.join(tmp, "rows.csv"))):
    if not r:                  continue
    if r[0].startswith("#"):   hdr = None; continue
    if hdr is None:            hdr = r;    continue
    rows.append(dict(zip(hdr, r)))
rows = [r for r in rows if r.get("weight_g")]
if not rows:
    print("no records in that window — nothing to do"); raise SystemExit

def ns(ts):
    d, _, frac = ts.rstrip("Z").partition(".")
    sec = int(dt.datetime.strptime(d, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp())
    return sec * 10**9 + int(frac.ljust(9, "0")[:9])

print(f"{len(rows)} record(s) -> quality={quality}")
for r in sorted(rows, key=lambda r: r["_time"]):
    local = dt.datetime.fromisoformat(r["_time"].replace("Z", "+00:00")).astimezone(tz)
    print(f"  {local:%m-%d %H:%M:%S}  {r.get('plant_id',''):<12} {float(r['weight_g']):>8.1f} g"
          f"  {r.get('quality','(none)')} -> {quality}")
if dry:
    print("\n--dry-run: nothing written"); raise SystemExit

with open(os.path.join(tmp, "rewrite.lp"), "w") as f:
    for r in rows:
        uid = r.get("uid", "")
        fields = f'weight_g={float(r["weight_g"])}' + (f',uid="{uid}"' if uid else "")
        f.write(f'plant_weight,device={r["device"]},plant_id={r["plant_id"]},quality={quality} '
                f'{fields} {ns(r["_time"])}\n')
PY

[ "$DRY" = "1" ] && exit 0
[ -f "$TMP/rewrite.lp" ] || exit 0

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%F_%H%M%S)"
cp "$TMP/rows.csv" "$BACKUP_DIR/plant_weight-premark-$STAMP.csv"
echo "backup -> $BACKUP_DIR/plant_weight-premark-$STAMP.csv"

TOKEN="$(sed -nE 's/^DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=(.*)/\1/p' .env | head -1)"
docker cp "$TMP/rewrite.lp" "$CONTAINER:/tmp/mark.lp" >/dev/null
docker exec "$CONTAINER" influx write --org "$ORG" --bucket "$BUCKET" --precision ns \
  --token "$TOKEN" --file /tmp/mark.lp
echo "rewritten with quality=$QUALITY"

# Only now remove the superseded copies: anything in the window that is NOT the
# new label. Write-then-delete means a failure above would have changed nothing.
docker exec "$CONTAINER" influx delete --org "$ORG" --bucket "$BUCKET" \
  --start "$START_UTC" --stop "$STOP_UTC" \
  --predicate "$PRED AND quality!=\"$QUALITY\"" 2>/dev/null || true
echo "old copies removed"
# NOTE: a predicate cannot match points that LACK the tag — `quality=""` silently
# deletes nothing, which is how a backfill once left two copies of every record.
# Everything now carries a quality tag, so the predicate above is sufficient; if
# untagged rows ever reappear, delete the whole window and rewrite instead.
