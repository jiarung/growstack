#!/usr/bin/env bash
# Run a Flux query against the local InfluxDB and print it readably.
#
#   ./influx-q.sh 'from(bucket:"sensors") |> range(start:-2d) |> filter(...)'
#   ./influx-q.sh -f query.flux
#   echo '<flux>' | ./influx-q.sh
#
# Why this exists: `influx query` prints UTC and one table block per group, which
# is unreadable for a quick look. This converts timestamps to $TZ (default
# Asia/Taipei) and flattens the annotated CSV — including the multi-header case,
# where groups with different columns each emit their own header and a naive
# parser silently drops every block after the first.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

TZ_NAME="${TZ_NAME:-Asia/Taipei}"
CONTAINER="${CONTAINER:-monitor-air-influxdb}"
ORG="${ORG:-monitor-air}"

case "${1:-}" in
  -f) [ -n "${2:-}" ] || { echo "usage: $(basename "$0") -f <file.flux>" >&2; exit 1; }
      flux="$(cat "$2")" ;;
  "") flux="$(cat)" ;;                 # stdin
  *)  flux="$1" ;;
esac
[ -n "${flux// /}" ] || { echo "empty query" >&2; exit 1; }

docker exec -i "$CONTAINER" influx query --org "$ORG" --raw -f /dev/stdin <<<"$flux" \
  | python3 -c '
import csv, sys, datetime as dt
from zoneinfo import ZoneInfo
tz = ZoneInfo(sys.argv[1])

rows, hdr = [], None
for r in csv.reader(sys.stdin):
    if not r:                      continue
    if r[0].startswith("#"):       hdr = None; continue   # new block -> new header
    if hdr is None:                hdr = r;    continue
    rows.append(dict(zip(hdr, r)))

if not rows:
    print("(no rows)"); raise SystemExit

# Union across ALL rows, not just the first: annotated CSV emits a fresh header
# whenever a table has different columns, so a later block can carry a column the
# first one lacks. Taking rows[0] parsed those values and then never printed them.
drop = {"result", "table", "_start", "_stop", ""}
cols = []
for r in rows:
    for c in r:
        if c not in drop and c not in cols:
            cols.append(c)
cols = [c for c in cols if any(r.get(c) for r in rows)]

def fmt(c, v):
    if c.endswith("_time") or c == "_time":
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(tz).strftime("%m-%d %H:%M:%S")
        except ValueError:
            return v
    # Only compact things that are actually decimals. A uid like 53486417230001
    # parses as a float and prints as 5.34864e+13, and 536E6417230001 is read as
    # scientific notation (536e6417230001) and comes out `inf` — the data is fine,
    # the display was lying. Integers already print identically without this.
    if "." in v:
        try:
            return f"{float(v):g}"
        except (ValueError, TypeError):
            pass
    return v

w = {c: max(len(c), max(len(fmt(c, r.get(c, ""))) for r in rows)) for c in cols}
print("  ".join(c.ljust(w[c]) for c in cols))
print("  ".join("-" * w[c] for c in cols))
for r in rows:
    print("  ".join(fmt(c, r.get(c, "")).ljust(w[c]) for c in cols))
print(f"\n{len(rows)} rows   (times in {sys.argv[1]})")
' "$TZ_NAME"
