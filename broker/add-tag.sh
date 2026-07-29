#!/usr/bin/env bash
# Map an NFC tag UID -> plant id for the weigh station.
#
#   ./add-tag.sh cactus-14              # tap the tag when prompted (uid read off MQTT)
#   ./add-tag.sh 00A8635C cactus-14     # or give the uid yourself
#
# tag-map.json is bind-mounted read-only into Node-RED (see docker-compose.yml),
# and the station flow re-reads it on each measurement, so a saved edit is picked
# up on the NEXT weigh — no rebuild, no volume reset, no restart (unlike
# add-plant.sh's flows.json). The uid is stored UPPERCASE to match the firmware's
# payload. plant_id MUST match an existing reflect `plant` id (see add-plant.sh)
# so weight and spectrum join on one plant. Commit tag-map.json afterwards.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
MAP="node-red/tag-map.json"

case $# in
  1) uid=""   plant="$1" ;;
  2) uid="$1" plant="$2" ;;
  *) echo "usage: $(basename "$0") [<tag-uid-hex>] <plant-id>   (omit the uid to read it from a tap)" >&2; exit 1 ;;
esac
[[ "$plant" =~ ^[A-Za-z0-9_-]{1,40}$ ]] || { echo "invalid plant id '$plant' ([A-Za-z0-9_-]{1,40})" >&2; exit 1; }

# plant_id MUST already exist as a reflect `plant` id (the n-plant dropdown), else
# weight can't join spectrum. Catches typos like `cactsu-01` that pass the regex.
python3 - "$plant" <<'PY' || { echo "  -> register it first: ./add-plant.sh $plant" >&2; exit 1; }
import json, sys
plant = sys.argv[1]
d = json.load(open('node-red/flows.json'))
dd = next(n for n in d if n.get('type') == 'ui_dropdown' and n.get('id') == 'n-plant')
ids = [o['value'] for o in dd['options']]
if plant not in ids:
    print(f"unknown plant id '{plant}' — not in the reflect dropdown", file=sys.stderr)
    sys.exit(1)
PY

# No uid given: read it off the wire instead of hand-copying it from a debug log.
# mosquitto_sub lives in the broker container, so nothing extra to install; -C 1
# takes the first of the ESP's up-to-5 retries. This is a passive listen — Node-RED
# still acks and records the measurement as usual (it'll log plant_id=unknown once).
if [ -z "$uid" ]; then
  echo "Tap the NFC tag on the station now (waiting ${WAIT:=60}s)..."
  err="$(mktemp)"; trap 'rm -f "$err"' EXIT
  evt="$(docker compose exec -T mosquitto \
           mosquitto_sub -t 'monitor-air/+/measure/event_raw' -C 1 -W "$WAIT" 2>"$err")" || true
  if [ -z "$evt" ]; then
    # Don't blame the station for what is usually a broker/compose problem — show why.
    echo "no measurement captured (station offline, or the broker is unreachable)" >&2
    [ -s "$err" ] && sed 's/^/  /' "$err" >&2
    exit 1
  fi
  uid="$(printf '%s' "$evt" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("uid",""))')"
  echo "captured uid $uid"
fi
uid="$(printf '%s' "$uid" | tr '[:lower:]' '[:upper:]')"
[[ "$uid" =~ ^[0-9A-F]{8,20}$ ]] || { echo "invalid uid '$uid' (hex, 8-20 chars)" >&2; exit 1; }

python3 - "$uid" "$plant" "$MAP" <<'PY'
import json, os, sys
uid, plant, path = sys.argv[1:4]
m = json.load(open(path)) if os.path.exists(path) else {}
old = m.get(uid)
m[uid] = plant
with open(path, 'w') as f:
    json.dump(dict(sorted(m.items())), f, indent=2, ensure_ascii=False)
    f.write('\n')
if   old == plant: print(f"unchanged: {uid} -> {plant}")
elif old:          print(f"remapped {uid}: {old} -> {plant}")
else:              print(f"added {uid} -> {plant}")
PY

echo "Live on the next weigh. Commit: git add $MAP && git commit"
