#!/usr/bin/env bash
# lamp-hold.sh's duration grammar, offline. The rest of that script is an
# integration with the running controller and was exercised live on 2026-10-04
# (40 s OFF hold: publish, heartbeat x2, restore ON; then a detached ON hold
# cancelled by its pidfile). This pins the one piece that can break silently:
# a misparsed duration is a lamp in the wrong state for hours.
#
#   ./test/lamp-hold.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source <(sed -n '/^to_secs()/,/^}/p' "$DIR/../broker/lamp-hold.sh")
fail=0
ok()  { local got; got="$(to_secs "$1" 2>/dev/null)" || got="(err)"; [ "$got" = "$2" ] && echo "pass $1 -> $2" || { echo "FAIL $1 -> $got (want $2)"; fail=1; }; }
bad() { to_secs "$1" >/dev/null 2>&1 && { echo "FAIL $1 accepted"; fail=1; } || echo "pass $1 rejected"; }
ok 15m 900;  ok 2h 7200;  ok 1h30m 5400;  ok 90 90;  ok 900s 900;  ok 2h0m 7200
bad 2d;  bad 15min;  bad "";  bad abc;  bad 1.5h;  bad -5m;  bad "15 m"
# repeated or out-of-order units are typos, not sums (codex, 2026-10-04)
bad 1h1h;  bad 30m1h;  bad 1s1s;  bad 1m1h;  ok 1h30m15s 5415
# the cap is enforced by the caller, not the parser — but the parser must not
# wrap or truncate a big number into a small one
ok 99h 356400
[ "$fail" = 0 ] && echo "ALL PASS" || { echo "FAILED"; exit 1; }
