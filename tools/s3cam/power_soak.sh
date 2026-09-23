#!/bin/bash
# Sample /power over time, for the A/B in docs/mlx90640/overheating-procedure.md.
#
#     tools/s3cam/power_soak.sh http://<ip>              # 5 min, every 30 s
#     tools/s3cam/power_soak.sh http://<ip> 120 15       # 2 min, every 15 s
#     tools/s3cam/power_soak.sh http://<ip> 300 30 'cam=idle&xclk=10'
#
# The third argument is applied BEFORE sampling starts, and the applied state is
# echoed back so a knob that did not take is visible immediately rather than as
# a puzzling flat curve later.
#
# WHY THIS IS A SCRIPT AND NOT A ONE-LINER IN THE DOC
# The doc's earlier version piped `curl -s` straight into json.load(). One
# dropped request — and this board has dropped off Wi-Fi repeatedly — fed an
# empty string to the parser, which killed the whole five-minute run with a
# JSONDecodeError pointing at column 1. The traceback named the parser; the
# problem was the network.
#
# So a failed sample is DATA here: it prints as `--` and the run continues, the
# same asymmetry scan_repeat.py uses. A soak that dies at minute four tells you
# nothing; one with two gaps in it tells you almost everything.
set -u
B="${1:?usage: power_soak.sh http://<ip> [seconds] [interval] [knobs]}"
DUR="${2:-300}"
EVERY="${3:-30}"
KNOBS="${4:-}"
B="${B%/}"

field() {   # field <json> <expr>
  printf '%s' "$1" | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('--'); sys.exit()
try:
    print($2)
except Exception:
    print('--')
" 2>/dev/null || printf -- '--'
}

# ${1:-} not $1: the sampling loop calls this with no argument at all, and
# `set -u` turns that into a fatal error on the FIRST sample — which is how a
# soak script whose entire purpose is surviving hiccups died before taking one.
get() { curl -s --max-time 10 "$B/power${1:-}" 2>/dev/null; }

if [ -n "$KNOBS" ]; then
  R="$(get "?$KNOBS")"
  echo "apply ?$KNOBS"
  echo "  applied=$(field "$R" "d['applied']")  set=$(field "$R" "d['set']")"
  echo "  now: cpu=$(field "$R" "d['cpu_mhz']") xclk=$(field "$R" "int(d['xclk_hz']/1e6)")MHz rest=$(field "$R" "d['rest_size']") cam_idle=$(field "$R" "d['cam_idle']")"
  echo
fi

printf '%8s  %7s  %7s  %9s  %6s  %5s\n' t die_c peak_c cam_idle xclk cpu
N=$(( DUR / EVERY ))
FAIL=0
for i in $(seq 0 "$N"); do
  R="$(get)"
  DIE="$(field "$R" "'%.1f' % d['die_c']")"
  [ "$DIE" = "--" ] && FAIL=$((FAIL+1))
  printf '%7ss  %7s  %7s  %9s  %6s  %5s\n' \
    "$(( i * EVERY ))" "$DIE" \
    "$(field "$R" "'%.1f' % d['die_max_c']")" \
    "$(field "$R" "d['cam_idle']")" \
    "$(field "$R" "int(d['xclk_hz']/1e6)")" \
    "$(field "$R" "d['cpu_mhz']")"
  [ "$i" -lt "$N" ] && sleep "$EVERY"
done

echo
echo "failed samples: $FAIL / $((N+1))"
echo "Read the LAST THREE rows, not the first: switching a knob often looks"
echo "backwards for a minute or two while earlier heat is still leaving."
echo "If cam_idle changed during the run, something captured — the run is void."
