#!/usr/bin/env python3
"""Diff a live OV5640 register dump against a vendor register table.

    curl -s 'http://<ip>/cam/reg?from=0x3000&to=0x30FF' > live.txt
    ./reg_diff.py live.txt ~/rt-thread/bsp/k210/driver/camera/ov5640cfg.h

The overheating investigation is looking for registers this board leaves in a
state a working configuration does not — an internal regulator still enabled, a
clock domain powered for a block we never use. Hunting those by guessing
addresses is how you brown out a sensor whose PWDN and RESET pins are unwired.
Diffing a full dump against a table someone shipped is how you find them.

Read the output as leads, NOT as a list of things to fix. The vendor table
targets different hardware and a different output format, so many differences
are correct: it drives MIPI where we drive DVP, and it disables the JPEG clocks
precisely because it does not use JPEG — while we do. A difference is a question
("why is ours 0xFF here?"), and the answer usually lives in the datasheet.

Vendor tables apply in order and rewrite registers as they switch modes, so the
LAST value written wins; that final state is what we compare against.
"""
import re
import sys
from collections import OrderedDict

DUMP_RE = re.compile(r"0x([0-9A-Fa-f]{4})\s+0x([0-9A-Fa-f]{2})")
TABLE_RE = re.compile(r"\{\s*0x([0-9A-Fa-f]+)\s*,\s*0x([0-9A-Fa-f]+)\s*\}"
                      r"\s*,?\s*(?://\s*(.*))?")


def parse_dump(path):
    """Our /cam/reg output. '--' (a failed read) is skipped, not read as 0."""
    out = OrderedDict()
    for line in open(path):
        if "read failed" in line:
            continue
        m = DUMP_RE.search(line)
        if m:
            out[int(m.group(1), 16)] = int(m.group(2), 16)
    return out


def parse_table(path):
    """Vendor table: last write wins, and we keep the comment that came with it."""
    out, notes = OrderedDict(), {}
    for line in open(path):
        m = TABLE_RE.search(line)
        if not m:
            continue
        reg, val = int(m.group(1), 16), int(m.group(2), 16)
        if reg > 0xFFFF or val > 0xFF:
            continue
        out[reg] = val
        if m.group(3):
            notes[reg] = m.group(3).strip()
    return out, notes


def bits(a, b):
    """Which bit numbers differ — the actionable part of a byte difference."""
    return [i for i in range(8) if (a ^ b) >> i & 1]


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 1
    live = parse_dump(argv[0])
    table, notes = parse_table(argv[1])
    if not live:
        print("no registers parsed from the dump — is it /cam/reg output?", file=sys.stderr)
        return 1
    print(f"live: {len(live)} registers   table: {len(table)} registers "
          f"(final state)\n")

    both = [r for r in live if r in table]
    diff = [r for r in both if live[r] != table[r]]
    print(f"== {len(diff)} of {len(both)} common registers DIFFER ==")
    for r in diff:
        note = f"   {notes[r]}" if r in notes else ""
        print(f"  0x{r:04X}  live 0x{live[r]:02X}  table 0x{table[r]:02X}   "
              f"bits {bits(live[r], table[r])}{note}")

    missing = [r for r in table if r not in live]
    print(f"\n== {len(missing)} registers the table sets that the dump did not cover ==")
    if missing:
        lo, hi = min(missing), max(missing)
        print(f"   range 0x{lo:04X}..0x{hi:04X}; widen the dump to see them:")
        print(f"   curl -s 'http://<ip>/cam/reg?from=0x{lo:04X}&to=0x{min(hi, lo + 255):04X}'")

    same = len(both) - len(diff)
    print(f"\n{same} common registers already agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
