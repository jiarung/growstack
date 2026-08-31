#!/usr/bin/env bash
# GY-MCU90640 parser fixture run (mlx90640 Phase 1). Fully offline:
# compiles the SAME gymcu_parser.cpp the firmware builds, replays synthetic
# UART streams, and semantically compares canonical JSON against expected/.
#
#   ./run.sh          # PASS/FAIL per case; nonzero exit on any FAIL
#
# What one pass checks:
#   * generator drift: fixtures are regenerated into a scratch dir and diffed
#     against the committed set — edit gen_fixtures.py without committing its
#     output and this fails loudly
#   * bytewise: full semantic JSON equality with expected/ (parsed objects,
#     not text — printf formatting is not part of the contract)
#   * chunk7/whole: parser STATE must not depend on chunking (stats equal
#     minus `overwritten`), and latest-wins must balance exactly:
#     frames_seen + overwritten == frames_ok
#   * timeout700 (truncated.bin): the discardPartial() contract
#   * an AddressSanitizer+UBSan build replays every stream — the resync index
#     arithmetic is the parser's whole risk surface; watch it, don't trust it
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${TMPDIR:-/tmp}/gymcu_host_runner"
BIN_SAN="${TMPDIR:-/tmp}/gymcu_host_runner_san"
SRCS=("$DIR/host_runner.cpp" "$DIR/../../src/s3cam/thermal/gymcu_parser.cpp")
INC=(-I "$DIR/../../src/s3cam/thermal")

c++ -std=c++11 -O2 -Wall -Wextra "${INC[@]}" "${SRCS[@]}" -o "$BIN"
c++ -std=c++11 -O1 -g -Wall -Wextra -fsanitize=address,undefined \
    "${INC[@]}" "${SRCS[@]}" -o "$BIN_SAN"

# generator <-> committed fixtures cannot drift silently
SCRATCH="$(mktemp -d)"; trap 'rm -rf "$SCRATCH"' EXIT
python3 "$DIR/gen_fixtures.py" "$SCRATCH" >/dev/null
if ! diff -r "$SCRATCH/fixtures" "$DIR/fixtures" >/dev/null 2>&1 \
   || ! diff -r "$SCRATCH/expected" "$DIR/expected" >/dev/null 2>&1; then
  echo "FAIL staleness: committed fixtures/expected differ from what gen_fixtures.py produces"
  echo "  rerun: python3 $DIR/gen_fixtures.py   (then review + commit)"
  exit 1
fi

fail=0

# full semantic comparison of one runner invocation against one expected file
check_full() {   # <label> <expected.json> <runner args...>
  local label=$1 exp=$2; shift 2
  if ! python3 -c '
import json, sys
a, b = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
sys.exit(0 if a == b else 1)' "$exp" <("$BIN" "$@") ; then
    echo "FAIL $label: output differs — reproduce:"
    echo "  $BIN $* | diff - $exp"
    fail=1
    return 1
  fi
}

for f in "$DIR"/fixtures/*.bin; do
  name="$(basename "$f" .bin)"
  exp="$DIR/expected/$name.json"
  ok=1
  check_full "$name (bytewise)" "$exp" "$f" bytewise || ok=0
  # chunking must not change parser STATE; latest-wins accounting must balance
  if ! python3 -c '
import json, sys
exp = json.load(open(sys.argv[1]))
for path in sys.argv[2:]:
    got = json.load(open(path))
    es, gs = dict(exp["stats"]), dict(got["stats"])
    es.pop("overwritten"); go = gs.pop("overwritten")
    if es != gs: sys.exit(1)
    if len(got["frames"]) + go != gs["frames_ok"]: sys.exit(1)
sys.exit(0)' "$exp" <("$BIN" "$f" chunk7) <("$BIN" "$f" whole); then
    echo "FAIL $name (chunk7/whole): parser state depends on chunking (or latest-wins broke)"
    fail=1; ok=0
  fi
  # sanitizer replay: any resync/index bug aborts loudly here
  for mode in bytewise whole; do
    if ! "$BIN_SAN" "$f" "$mode" >/dev/null; then
      echo "FAIL $name ($mode, sanitized): ASan/UBSan tripped"
      fail=1; ok=0
    fi
  done
  [ "$ok" = 1 ] && echo "pass $name (semantic + chunk state + latest-wins + sanitized)"
done

# the timeout contract: same truncated bytes, idle gap declared dead at 700
if check_full "truncated (timeout700)" "$DIR/expected/truncated_timeout.json" \
              "$DIR/fixtures/truncated.bin" timeout700; then
  "$BIN_SAN" "$DIR/fixtures/truncated.bin" timeout700 >/dev/null \
    && echo "pass truncated (timeout700 discardPartial contract)" \
    || { echo "FAIL truncated (timeout700, sanitized)"; fail=1; }
fi
exit $fail
