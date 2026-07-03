#!/usr/bin/env bash
# Add plant id(s) to the reflectance Plant dropdown and redeploy Node-RED.
#
#   ./add-plant.sh cactus-14              # one id
#   ./add-plant.sh cactus-14 cactus-15-1  # several at once
#
# Does the whole dance: edits node-red/flows.json, rebuilds the image, resets
# the stale volume (required for flow changes — see node-red/Dockerfile), brings
# the container up and verifies the options are live. Ids are permanent InfluxDB
# tags: zero-pad (cactus-07), sub-number multi-plant pots (cactus-13-2), and
# never reuse a retired id. Commit flows.json afterwards.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

[ $# -ge 1 ] || { echo "usage: $(basename "$0") <plant-id> [more-ids...]" >&2; exit 1; }

for id in "$@"; do
  # same validation the firmware applies to the cmd payload
  [[ "$id" =~ ^[A-Za-z0-9_-]{1,40}$ ]] || { echo "invalid id '$id' (allowed: [A-Za-z0-9_-]{1,40})" >&2; exit 1; }
done

python3 - "$@" <<'PY'
import json, sys
path = 'node-red/flows.json'
d = json.load(open(path))
dd = next(n for n in d if n.get('type') == 'ui_dropdown' and n.get('id') == 'n-plant')
have = [o['value'] for o in dd['options']]
args = list(dict.fromkeys(sys.argv[1:]))   # de-dupe repeated args, keep order
new = [p for p in args if p not in have]
dup = [p for p in args if p in have]
if dup:
    print(f"already present, skipping: {', '.join(dup)}")
if not new:
    print("nothing to add"); sys.exit(1)
# keep white-ref (and any other *-ref) at the end of the list
refs = [o for o in dd['options'] if o['value'].endswith('-ref')]
plants = [o for o in dd['options'] if not o['value'].endswith('-ref')]
plants += [{"label": p, "value": p, "type": "str"} for p in new]
dd['options'] = plants + refs
json.dump(d, open(path, 'w'), indent=1, ensure_ascii=False)
print(f"added: {', '.join(new)} ({len(dd['options'])} options total)")
PY

echo "Redeploying node-red (rebuild + volume reset)..."
docker compose rm -sf node-red >/dev/null
# resolve the volume name via compose (respects COMPOSE_PROJECT_NAME); tolerate
# a missing volume (fresh host) but still fail on e.g. volume-in-use
vol="$(docker compose config --format json | python3 -c 'import sys,json; print(json.load(sys.stdin)["volumes"]["nodered-data"]["name"])' 2>/dev/null || echo "broker_nodered-data")"
if docker volume inspect "$vol" >/dev/null 2>&1; then
  docker volume rm "$vol" >/dev/null
fi
docker compose build node-red >/dev/null
docker compose up -d node-red >/dev/null
sleep 8

echo "Verifying live options:"
docker exec monitor-air-nodered node -e "
const f=require('/data/flows.json');
const dd=f.find(n=>n.id==='n-plant');
console.log(' ', dd.options.map(o=>o.value).join(', '))"

echo "Done. Remember to commit: git add broker/node-red/flows.json && git commit"
