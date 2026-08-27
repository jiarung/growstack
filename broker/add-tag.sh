#!/usr/bin/env bash
# Map an NFC tag UID -> plant id for the weigh station.
#
#   ./add-tag.sh cactus-14              # tap the tag when prompted (uid read off MQTT)
#   ./add-tag.sh cactus-16 cactus-17    # several: it prompts for each in turn
#   ./add-tag.sh 00A8635C cactus-14     # or give the uid yourself (one plant only)
#
# Binding several tags is one command, not one console trip per pot: it asks for
# each id in order and waits for that tag's tap. Every pairing is confirmed at the
# moment you tap it, which is the point — the original 16 were reconstructed after
# the fact from the ORDER 19 pots were weighed in, and one of them (tare-ref) was
# wrong for two weeks before anyone noticed.
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

# An explicit uid pairs exactly one plant — you cannot hand-type a uid for a tag
# you are about to tap. Anything else is a list of plants, each read from a tap.
GIVEN_UID=""
if [ $# -eq 2 ] && [[ "$1" =~ ^[0-9A-Fa-f]{8,20}$ ]]; then
  GIVEN_UID="$1"; shift
fi
[ $# -ge 1 ] || { echo "usage: $(basename "$0") <plant-id>...   |   $(basename "$0") <tag-uid-hex> <plant-id>" >&2; exit 1; }

PLANTS=()
for p in "$@"; do
  [[ "$p" =~ ^[A-Za-z0-9_-]{1,40}$ ]] || { echo "invalid plant id '$p' ([A-Za-z0-9_-]{1,40})" >&2; exit 1; }
  PLANTS+=("$p")
done

# plant_id MUST already exist as a reflect `plant` id (the n-plant dropdown), else
# weight can't join spectrum. Catches typos like `cactsu-01` that pass the regex.
# Validate EVERY id up front. Failing on the third id after you have already
# tapped two tags would leave the map half-written and you back at the console —
# exactly what batching is meant to avoid.
python3 - "${PLANTS[@]}" <<'PY' || { echo "  -> register it first: ./add-plant.sh <plant-id>" >&2; exit 1; }
import json, sys
d = json.load(open('node-red/flows.json'))
dd = next(n for n in d if n.get('type') == 'ui_dropdown' and n.get('id') == 'n-plant')
ids = {o['value'] for o in dd['options']}
missing = [p for p in sys.argv[1:] if p not in ids]
if missing:
    print("unknown plant id(s), not in the reflect dropdown: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
PY

# One pass per plant. Each tap is captured and written before the next prompt, so
# an interrupted run leaves every pairing made so far already saved and correct.
SEEN_UIDS=""
for plant in "${PLANTS[@]}"; do
  uid="$GIVEN_UID"

  # No uid given: read it off the wire instead of hand-copying it from a debug log.
  # mosquitto_sub lives in the broker container, so nothing extra to install; -C 1
  # takes the first of the ESP's up-to-5 retries. This is a passive listen — Node-RED
  # still acks and records the measurement as usual (it'll log plant_id=unknown once).
  if [ -z "$uid" ]; then
    echo
    echo "==> $plant: tap its NFC tag on the station now (waiting ${WAIT:=60}s)..."
    err="$(mktemp)"
    evt="$(docker compose exec -T mosquitto \
             mosquitto_sub -t 'monitor-air/+/measure/event_raw' -C 1 -W "$WAIT" 2>"$err")" || true
    if [ -z "$evt" ]; then
      # Don't blame the station for what is usually a broker/compose problem — show why.
      echo "no measurement captured for $plant (station offline, or the broker is unreachable)" >&2
      [ -s "$err" ] && sed 's/^/  /' "$err" >&2
      rm -f "$err"
      echo "stopping; pairings made before this one are already saved." >&2
      exit 1
    fi
    rm -f "$err"
    uid="$(printf '%s' "$evt" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("uid",""))')"
    echo "captured uid $uid"
  fi
  uid="$(printf '%s' "$uid" | tr '[:lower:]' '[:upper:]')"
  [[ "$uid" =~ ^[0-9A-F]{8,20}$ ]] || { echo "invalid uid '$uid' (hex, 8-20 chars)" >&2; exit 1; }

  # Tapping the same tag twice in one run is a mis-tap, not a remap: it would
  # silently move a pot's identity onto the wrong plant. Stop and say so.
  case " $SEEN_UIDS " in
    *" $uid "*) echo "uid $uid was already tapped earlier in this run — wrong tag? nothing written for $plant" >&2; exit 1 ;;
  esac
  SEEN_UIDS="$SEEN_UIDS $uid"

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
done

echo "Live on the next weigh. Commit: git add $MAP && git commit"
