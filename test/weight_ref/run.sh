#!/usr/bin/env bash
# Host regression test for the station's weight-ref cache: compiles the REAL
# src/weight_ref.cpp (Arduino stubbed, ArduinoJson from the project's own
# libdeps) and replays the three-tier ingest matrix — see test_main.cpp.
#
#   ./run.sh          # pass line or loud CHECK failures; nonzero exit on FAIL
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$DIR/../.."
AJ="$REPO/.pio/libdeps/esp32-s3-devkitc-1/ArduinoJson/src"
[ -d "$AJ" ] || { echo "ArduinoJson not found at $AJ — run 'pio run' once first"; exit 2; }
BIN="${TMPDIR:-/tmp}/weight_ref_test"

c++ -std=c++11 -O1 -g -Wall -Wextra -fsanitize=address,undefined \
    -I "$DIR/stub" -I "$AJ" \
    "$DIR/test_main.cpp" "$REPO/src/weight_ref.cpp" -o "$BIN"
"$BIN"
