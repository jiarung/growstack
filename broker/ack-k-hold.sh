#!/usr/bin/env bash
# Human brake-release for the k-model adoption engine (Phase C).
# A bucket in `held` froze its adopted value and carries the challenger in
# pending_*; this tool is the ONLY way a held value moves.
#
#   ./ack-k-hold.sh <target>/<source>/<regime> --evidence-rev <rev> --accept
#   ./ack-k-hold.sh <target>/<source>/<regime> --evidence-rev <rev> --reject
#   ./ack-k-hold.sh bh1750_lux_ref/daylight/diffuse --evidence-rev a1b2c3d4e5f6 --accept
#
# --epoch defaults to the bucket's CURRENT epoch (from epochs.json); an older
# generation needs an explicit --epoch <id>.
#
# CAS contract: the authority is the LATEST k_adopted row's pending_evidence_rev.
# A mismatch is refused and the actual pending is printed — the thing a human
# confirms must never drift under them. The same exclusive lock as the compute
# round (flock /tmp/k-models.lock) makes read->compare->write atomic against a
# concurrent cron run; the Influx read-then-write alone would not be a CAS.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BUCKET="${1:?usage: ack-k-hold.sh <target>/<source>/<regime> --evidence-rev <rev> --accept|--reject}"
shift
REV=""; DECISION=""; EPOCH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --evidence-rev) REV="${2:?}"; shift 2;;
    --accept)       DECISION="accept"; shift;;
    --reject)       DECISION="reject"; shift;;
    --epoch)        EPOCH="${2:?}"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done
[ -n "$REV" ] || { echo "--evidence-rev is required (from the hold alert)" >&2; exit 1; }
[ -n "$DECISION" ] || { echo "one of --accept / --reject is required" >&2; exit 1; }

TOKEN="$(grep -E '^DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
[ -n "$TOKEN" ] || { echo "need DOCKER_INFLUXDB_INIT_ADMIN_TOKEN in $DIR/.env" >&2; exit 1; }

exec flock -w 30 /tmp/k-models.lock env \
  BUCKET="$BUCKET" REV="$REV" DECISION="$DECISION" EPOCH="$EPOCH" \
  DIR="$DIR" INFLUX_TOKEN="$TOKEN" python3 - <<'PY'
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

DIR = os.environ["DIR"]
sys.path.insert(0, DIR)
import kadopt   # noqa: E402
import kmodels  # noqa: E402


def die(m):
    print(m, file=sys.stderr)
    sys.exit(1)


parts = os.environ["BUCKET"].split("/")
if len(parts) != 3 or parts[0] not in kmodels.TARGETS:
    die("bucket must be <target>/<source>/<regime>, target one of: "
        + " | ".join(kmodels.TARGETS))
target, source, regime = parts

epoch = os.environ["EPOCH"]
if not epoch:
    reg = json.load(open(os.path.join(DIR, "epochs.json"), encoding="utf-8"))
    now = datetime.now(timezone.utc)
    epoch = kmodels.epoch_of(now, kmodels.epoch_intervals(reg, target))[0]

INFLUX = ["docker", "exec", "-e", "INFLUX_TOKEN", "monitor-air-influxdb", "influx"]


def influx_query(flux):
    import csv
    import io
    raw = subprocess.run(INFLUX + ["query", "--org", "monitor-air", "--raw", flux],
                         capture_output=True, text=True, env={**os.environ})
    if raw.returncode != 0:
        die("influx query failed:\n" + raw.stderr[:800])
    lines = [ln for ln in raw.stdout.splitlines() if ln and not ln.startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(lines)))) if lines else []


def fnum(r, k):
    try:
        return float(r.get(k, ""))
    except (TypeError, ValueError):
        return None


# Under the lock: RE-READ the latest pending rev, then compare, then write.
rows = influx_query(f'''
from(bucket: "sensors")
  |> range(start: 1970-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "k_adopted"
       and r.target == "{target}" and r.source == "{source}"
       and r.regime == "{regime}" and r.epoch == "{epoch}")
  |> last()
  |> pivot(rowKey: ["target", "source", "regime", "epoch"], columnKey: ["_field"], valueColumn: "_value")
''')
latest = None
if rows:
    r = rows[0]
    latest = {"target": target, "source": source, "regime": regime, "epoch": epoch,
              "value": fnum(r, "value"), "unit": r.get("unit") or "",
              "adoption_state": r.get("adoption_state") or "seed",
              "model_id": r.get("model_id") or "",
              "prev_value": fnum(r, "prev_value"),
              "reason": r.get("reason") or "",
              "pending_value": fnum(r, "pending_value"),
              "pending_model_id": r.get("pending_model_id"),
              "pending_evidence_rev": r.get("pending_evidence_rev"),
              "hold_ack_state": r.get("hold_ack_state") or "none"}
    for k in ("prev_value", "pending_value"):
        if latest[k] is not None and latest[k] < 0:
            latest[k] = None
    for k in ("pending_model_id", "pending_evidence_rev"):
        latest[k] = latest[k] or None

now = datetime.now(timezone.utc).replace(microsecond=0)
row, err = kadopt.ack(latest, os.environ["REV"], os.environ["DECISION"], now)
if err:
    die(f"ack refused for {target}/{source}/{regime}@{epoch}: {err}")

line = kadopt.to_line(row, int(now.timestamp()))
w = subprocess.run(INFLUX + ["write", "--bucket", "sensors", "--org", "monitor-air",
                             "--precision", "s"],
                   input=line, capture_output=True, text=True, env={**os.environ})
if w.returncode != 0:
    die("influx write failed:\n" + w.stderr[:800])

verb = "accepted -> value" if os.environ["DECISION"] == "accept" else "rejected -> value stays"
print(f"✓ {target}/{source}/{regime}@{epoch}: {verb} {row['value']} "
      f"(state={row['adoption_state']}, hold_ack={row['hold_ack_state']})")
PY
