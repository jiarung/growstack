#!/usr/bin/env bash
# Append-only registry for the k-model pipeline (tasks/photone-cal-pipeline.md):
# epochs (a hardware/instrument state change per calibration target) and the
# station-map (device -> light_location, time-versioned). The estimator never
# mixes data across an epoch boundary, so a missing mark silently corrupts a
# model — mark BEFORE changing hardware when possible.
#
#   ./mark-epoch.sh epoch --target bh1750_lux_ref --start 2026-08-28T04:00:00Z \
#       --reason "diffuser added" [--device livingroom] [--config '{"optics":"..."}'] \
#       [--photone '{"phone":"...","app_ver":"..."}']
#   ./mark-epoch.sh map --device livingroom --light-location balcony \
#       --valid-from 2026-09-01T00:00:00Z
#   ./mark-epoch.sh list
#
# Validations (the whole point of going through this script): starts must be
# UTC RFC3339 on a 5-minute grid (the light_context grid cannot split a cell
# across epochs), strictly increasing per target/device, and existing entries
# are never modified — the script re-verifies the current file before touching it.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REG="$DIR/epochs.json"

CMD="${1:-}"; shift || true
case "$CMD" in epoch|map|list) ;; *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 1;; esac

TARGET=""; START=""; REASON=""; DEVICE="livingroom"; CONFIG="{}"; PHOTONE="{}"
LLOC=""; VFROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --target)         TARGET="${2:?}"; shift 2;;
    --start)          START="${2:?}"; shift 2;;
    --reason)         REASON="${2:?}"; shift 2;;
    --device)         DEVICE="${2:?}"; shift 2;;
    --config)         CONFIG="${2:?}"; shift 2;;
    --photone)        PHOTONE="${2:?}"; shift 2;;
    --light-location) LLOC="${2:?}"; shift 2;;
    --valid-from)     VFROM="${2:?}"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

CMD="$CMD" TARGET="$TARGET" START="$START" REASON="$REASON" DEVICE="$DEVICE" \
CONFIG="$CONFIG" PHOTONE="$PHOTONE" LLOC="$LLOC" VFROM="$VFROM" REG="$REG" python3 - <<'PY'
import json, os, sys, re, fcntl, tempfile
from datetime import datetime, timezone

def die(m): print(m, file=sys.stderr); sys.exit(1)

# Exclusive lock around read -> validate -> append -> atomic replace: two
# concurrent invocations must serialize, or one silently overwrites the other's
# append (the registry is the estimator's immutable lineage — losing an entry
# means cross-epoch mixing later).
_lock = open(os.environ["REG"] + ".lock", "w")
fcntl.flock(_lock, fcntl.LOCK_EX)

def parse_grid_utc(s, what):
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", s or ""):
        die(f"{what} must be UTC RFC3339 seconds, e.g. 2026-08-28T04:00:00Z (got {s!r})")
    t = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if t.minute % 5 != 0 or t.second != 0:
        die(f"{what} must sit on the UTC 5-minute grid (the light_context cell "
            f"cannot be split across epochs) — got {s}")
    return t

reg_path = os.environ["REG"]
reg = json.load(open(reg_path))
if set(reg) != {"epochs", "station_map"}:
    die(f"{reg_path}: top level must be exactly {{\"epochs\":[...],\"station_map\":[...]}}")

# Re-verify the existing file BEFORE appending: append-only means broken history
# must halt the tool, not be silently extended.
TARGETS = ("bh1750_lux_main", "bh1750_lux_ref", "as7341_ppfd")
CONFIG_KEYS = {"bh1750_lux_main": {"address", "position", "optics"},
               "bh1750_lux_ref":  {"address", "position", "optics"},
               "as7341_ppfd":     {"gain", "tint_ms"}}
TAG_RE = r"^[A-Za-z0-9_.-]+$"
seen, ids = {}, set()
for e in reg["epochs"]:
    t = parse_grid_utc(e.get("start",""), f"epoch {e.get('epoch_id')} start")
    key = e.get("target","")
    if key not in TARGETS:
        die(f"registry corrupt: unknown target {key!r} in {e.get('epoch_id')}")
    if e.get("epoch_id") in ids:
        die(f"registry corrupt: duplicate epoch_id {e.get('epoch_id')}")
    ids.add(e.get("epoch_id"))
    if not re.match(TAG_RE, e.get("device","") or ""):
        die(f"registry corrupt: bad device in {e.get('epoch_id')}")
    if CONFIG_KEYS[key] - set(e.get("config", {})):
        die(f"registry corrupt: {e.get('epoch_id')} config missing "
            f"{sorted(CONFIG_KEYS[key] - set(e.get('config', {})))} — was it hand-edited?")
    if not {"phone", "app_ver"} <= set(e.get("photone", {})):
        die(f"registry corrupt: {e.get('epoch_id')} photone identity incomplete")
    if key in seen and t <= seen[key]:
        die(f"registry corrupt: epochs for {key} not strictly increasing")
    seen[key] = t
mseen = {}
for m in reg["station_map"]:
    t = parse_grid_utc(m.get("valid_from",""), f"station_map {m.get('device')} valid_from") \
        if m.get("valid_from") != "1970-01-01T00:00:00Z" else datetime(1970,1,1,tzinfo=timezone.utc)
    d = m.get("device","")
    if not (re.match(TAG_RE, d or "") and re.match(TAG_RE, m.get("light_location","") or "")):
        die(f"registry corrupt: bad device/light_location in station_map entry {m}")
    if d in mseen and t <= mseen[d]:
        die(f"registry corrupt: station_map for {d} not strictly increasing")
    mseen[d] = t

cmd = os.environ["CMD"]
if cmd == "list":
    print(json.dumps(reg, indent=2, ensure_ascii=False)); sys.exit(0)

if cmd == "epoch":
    target = os.environ["TARGET"]; start = os.environ["START"]; reason = os.environ["REASON"]
    if target not in TARGETS:
        die("--target must be one of: bh1750_lux_main | bh1750_lux_ref | as7341_ppfd")
    if not reason: die("--reason is required — an unexplained epoch is unusable later")
    t = parse_grid_utc(start, "--start")
    if target in seen and t <= seen[target]:
        die(f"--start must be after the last {target} epoch ({seen[target]:%Y-%m-%dT%H:%M:%SZ})")
    try:
        config = json.loads(os.environ["CONFIG"]); photone = json.loads(os.environ["PHOTONE"])
    except json.JSONDecodeError as e:
        die(f"--config/--photone must be valid JSON: {e}")
    # Per-target config schema (blueprint): an epoch without its config identity
    # cannot be compared against rows later — refuse to create one.
    missing = CONFIG_KEYS[target] - set(config)
    if missing:
        die(f"--config for {target} must include {sorted(CONFIG_KEYS[target])} "
            f"(missing: {sorted(missing)}) — measure/inventory first, don't guess")
    if not {"phone", "app_ver"} <= set(photone):
        die("--photone must include {phone, app_ver} — the reference instrument "
            "is an instrument; its identity is part of the epoch")
    if not re.match(r"^[A-Za-z0-9_.-]+$", os.environ["DEVICE"]):
        die("--device must be a simple tag id [A-Za-z0-9_.-]")
    prev = [e["epoch_id"] for e in reg["epochs"] if e["target"] == target]
    n = len(prev) + 1
    entry = {"epoch_id": f"{target}-e{n}", "target": target, "start": start,
             "reason": reason, "device": os.environ["DEVICE"],
             "config": config, "photone": photone,
             "prev": prev[-1] if prev else "e0-legacy"}
    reg["epochs"].append(entry)
else:  # map
    device = os.environ["DEVICE"]; lloc = os.environ["LLOC"]; vfrom = os.environ["VFROM"]
    if not lloc: die("--light-location is required")
    if not (re.match(r"^[A-Za-z0-9_.-]+$", device) and re.match(r"^[A-Za-z0-9_.-]+$", lloc)):
        die("--device / --light-location must be simple tag ids [A-Za-z0-9_.-]")
    t = parse_grid_utc(vfrom, "--valid-from")
    if device in mseen and t <= mseen[device]:
        die(f"--valid-from must be after the last {device} mapping ({mseen[device]:%Y-%m-%dT%H:%M:%SZ})")
    entry = {"device": device, "light_location": lloc, "valid_from": vfrom}
    reg["station_map"].append(entry)

fd, tmp = tempfile.mkstemp(dir=os.path.dirname(reg_path), prefix=".epochs-", suffix=".tmp")
with os.fdopen(fd, "w") as f:
    json.dump(reg, f, indent=2, ensure_ascii=False); f.write("\n")
os.replace(tmp, reg_path)
print("appended:", json.dumps(entry, ensure_ascii=False))
PY
