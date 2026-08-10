#!/usr/bin/env bash
# Back up InfluxDB to the HDD (/data/influx-backups) and rotate old backups.
# Designed to be cron-safe: resolves its own location, loads .env, uses an
# absolute docker path, fails fast with clear messages.
set -euo pipefail

# --- resolve paths (cron has a minimal CWD/env) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BROKER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKER="$(command -v docker || echo /usr/bin/docker)"
KEEP="${KEEP:-14}"                       # how many backups to retain
HOST_BACKUP_DIR="/data/influx-backups"   # HDD

cd "$BROKER_DIR"

# --- load secrets ---
if [ ! -f .env ]; then
  echo "ERROR: $BROKER_DIR/.env not found" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a

: "${DOCKER_INFLUXDB_INIT_ADMIN_TOKEN:?ERROR: DOCKER_INFLUXDB_INIT_ADMIN_TOKEN not set in .env}"
: "${DOCKER_INFLUXDB_INIT_ORG:=monitor-air}"

STAMP="$(date +%F_%H%M%S)"   # seconds → no same-minute collisions

echo "[$(date)] influx backup -> ${HOST_BACKUP_DIR}/${STAMP}"
"$DOCKER" compose exec -T influxdb \
  influx backup "/backups/${STAMP}" \
  --org "$DOCKER_INFLUXDB_INIT_ORG" \
  --token "$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN" \
  --host http://localhost:8086

# --- rotate: keep newest $KEEP backup dirs ---
# The deletes run INSIDE the container, not on the host. `influx backup` writes as
# root, so the directories it creates are root-owned and a cron running as the
# normal user cannot remove them: rotation failed with "Permission denied", set -e
# aborted the script before "backup done", and because the only output goes to a
# log file nobody reads, the whole thing was silent. The container is already root
# and has the same volume at /backups, so it can delete what it created.
ROTATED="$("$DOCKER" compose exec -T influxdb sh -c '
  cd /backups 2>/dev/null || exit 0
  ls -1dt */ 2>/dev/null | tail -n "+'"$((KEEP + 1))"'" | while read -r old; do
    echo "rotate: removing old backup $old"
    rm -rf -- "$old"
  done')"
[ -n "$ROTATED" ] && echo "$ROTATED"

# Rotation must actually have worked — a silent failure here is how the disk fills.
LEFT="$("$DOCKER" compose exec -T influxdb sh -c 'ls -1d /backups/*/ 2>/dev/null | wc -l' | tr -d "\r")"
if [ "${LEFT:-0}" -gt "$KEEP" ]; then
  echo "ERROR: $LEFT backups remain, expected at most $KEEP — rotation did not apply" >&2
  exit 1
fi

echo "[$(date)] backup done"
