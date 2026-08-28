#!/usr/bin/env bash
# k-model pipeline entry point (Phase B) — thin wrapper so cron and humans call
# one name. All logic lives in compute-k-models.py (+ the pure core kmodels.py).
#
#   ./compute-k-models.sh                      # live: Influx -> k_model + light_context + retained MQTT
#   ./compute-k-models.sh --fixture fixtures   # offline replay, diff vs fixtures/expected/
#   python3 kmodels.py --selftest              # pure-math contract tests
#
# Host crontab (concrete account-absolute paths + /tmp log — a placeholder path
# and a nonexistent /data/ once kept a cron job from ever running; README.md:429):
#   10 * * * * flock -n /tmp/k-models.lock /home/jiarung/monitor-air/broker/compute-k-models.sh >> /tmp/k-models.log 2>&1
# Acceptance: `crontab -l | grep k-models`, then after the hour `tail /tmp/k-models.log`.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/compute-k-models.py" "$@"
