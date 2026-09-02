#!/usr/bin/env python3
"""Fixture generator for the GY-MCU90640 parser (mlx90640 Phase 1).

Writes fixtures/*.bin (synthetic UART streams) and expected/*.json (the
runner's bytewise output, computed INDEPENDENTLY from the input formulas —
never by running the parser, which would make the test a tautology).

The wire layout has ONE source: this script parses SYNC/TYPE/SUBTYPE and the
geometry straight out of gymcu_parser.h, so a phase-2 hardware correction to
those constants regenerates correct fixtures instead of invalidating them.

Synthetic pixel values are chosen so no 0x5A byte appears where a case's
recovery reasoning assumes none (asserted): recovery scans hunt for sync
pairs, and expected stats are only hand-derivable when the scan targets are
exactly the intended ones. The dense_sync case does the opposite ON PURPOSE.

    ./gen_fixtures.py [outdir]     # default: this directory (the committed set)

run.sh regenerates into a scratch dir and diffs against the committed set, so
generator and fixtures cannot silently drift."""
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else HERE
FIX = os.path.join(OUT, "fixtures")
EXP = os.path.join(OUT, "expected")
os.makedirs(FIX, exist_ok=True)
os.makedirs(EXP, exist_ok=True)

# ---- the layout, parsed from the parser's own header (single source) --------
_hdr = open(os.path.join(HERE, "../../src/s3cam/thermal/gymcu_parser.h")).read()


def _const(name):
    m = re.search(rf"\b{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", _hdr)
    assert m, f"cannot parse {name} from gymcu_parser.h"
    return int(m.group(1), 0)


ROWS, COLS = _const("ROWS"), _const("COLS")
PIXELS = ROWS * COLS
HDR = _const("HDR")
SYNC0, SYNC1 = _const("SYNC0"), _const("SYNC1")
TYPE, SUBTYPE = _const("TYPE"), _const("SUBTYPE")
OFF_TA = HDR + 2 * PIXELS
FRAME_LEN = OFF_TA + 4


def px_std(k):
    """2001..2313, step 4 with offset k — no byte is ever 0x5A (see module doc)."""
    return lambda p: 2001 + ((p + k) % 79) * 4


def px_neg(p):
    """-1500..-1157: negative int16 decode; verified free of 0x5A bytes."""
    return -1500 + (p % 50) * 7


def frame(pxfn, ta_raw, checksum_override=None):
    body = bytearray([SYNC0, SYNC1, TYPE, SUBTYPE])
    body += struct.pack(f"<{PIXELS}h", *(pxfn(p) for p in range(PIXELS)))
    body += struct.pack("<h", ta_raw)
    real = sum(body) & 0xFFFF
    cs = real if checksum_override is None else checksum_override
    if checksum_override is not None:
        assert cs != real, "override must actually break the checksum"
    body += struct.pack("<H", cs)
    assert len(body) == FRAME_LEN
    return bytes(body)


def assert_sync_pairs_exactly(stream, allowed_offsets):
    got = {i for i in range(len(stream) - 1)
           if stream[i] == SYNC0 and stream[i + 1] == SYNC1}
    assert got == set(allowed_offsets), f"sync pairs {sorted(got)} != {sorted(allowed_offsets)}"


def frame_json(seq, pxfn, ta_raw):
    px = [pxfn(p) / 100 for p in range(PIXELS)]
    inner = ", ".join(f"{v:.2f}" for v in px)
    return (f'    {{"seq": {seq}, "ta_c": {ta_raw / 100:.2f}, "checksum_ok": true, '
            f'"min": {min(px):.2f}, "max": {max(px):.2f}, "px": [{inner}]}}')


def expected(frames, frames_ok, bad_checksum=0, bad_header=0, resyncs=0,
             bytes_dropped=0, overwritten=0, timeouts=0):
    return ("{\n  \"frames\": [\n" + ",\n".join(frames) + "\n  ],\n"
            f'  "stats": {{"frames_ok": {frames_ok}, "bad_checksum": {bad_checksum}, '
            f'"bad_header": {bad_header}, "resyncs": {resyncs}, '
            f'"bytes_dropped": {bytes_dropped}, "overwritten": {overwritten}, '
            f'"timeouts": {timeouts}}}\n}}\n')


def write_expected(name, exp):
    with open(os.path.join(EXP, name + ".json"), "w") as f:
        f.write(exp)


def write(name, stream, exp):
    with open(os.path.join(FIX, name + ".bin"), "wb") as f:
        f.write(stream)
    write_expected(name, exp)


F0, F1, F2 = (frame(px_std(k), 2560 + k * 10) for k in range(3))

# --- A. clean: three back-to-back frames -------------------------------------
sA = F0 + F1 + F2
assert_sync_pairs_exactly(sA, {0, FRAME_LEN, 2 * FRAME_LEN})
write("clean", sA, expected(
    [frame_json(1, px_std(0), 2560), frame_json(2, px_std(1), 2570),
     frame_json(3, px_std(2), 2580)], frames_ok=3))

# --- B. noise prefix: 17 junk bytes (incl. a lone 0x5A) then a frame ---------
# drops: 10 at pos0 + (abandoned 0x5A + the 0x33) + 5 at pos0 = 17
junk = bytes([0x11] * 10 + [SYNC0, 0x33] + [0x22] * 5)
sB = junk + F0
assert_sync_pairs_exactly(sB, {len(junk)})
write("noise_prefix", sB, expected([frame_json(1, px_std(0), 2560)],
                                   frames_ok=1, bytes_dropped=17))

# --- C. bad checksum, then a valid frame -------------------------------------
# Corrupt frame fills the buffer, fails the sum -> resync scans buf[1..],
# finds no pair and no trailing 0x5A (asserted) -> the whole frame dropped.
fx = bytearray(F0)
fx[100] = (fx[100] + 1) & 0xFF
assert fx[100] != SYNC0 and fx[-1] != SYNC0
sC = bytes(fx) + F1
assert_sync_pairs_exactly(sC, {0, FRAME_LEN})
write("bad_checksum", sC, expected([frame_json(1, px_std(1), 2570)], frames_ok=1,
                                   bad_checksum=1, resyncs=1,
                                   bytes_dropped=FRAME_LEN))

# --- D. truncated frame swallowed, full frame recovered ----------------------
# 700 bytes of a dying frame + a complete frame. The mixed buffer fails the
# sum; recovery finds the REAL header at offset 700 and keeps the tail ->
# only the 700 truncated bytes are lost.
sD = F0[:700] + F2
assert_sync_pairs_exactly(sD, {0, 700})
write("truncated", sD, expected([frame_json(1, px_std(2), 2580)], frames_ok=1,
                                bad_checksum=1, resyncs=1, bytes_dropped=700))
# ...and the SAME bytes under the timeout contract (runner mode timeout700):
# the UART layer declares the gap dead BEFORE the next frame arrives ->
# discardPartial drops the 700 buffered bytes, no checksum failure, no resync.
write_expected("truncated_timeout",
               expected([frame_json(1, px_std(2), 2580)], frames_ok=1,
                        bytes_dropped=700, timeouts=1))

# --- E. dense sync: a corrupt frame PACKED with sync pairs -------------------
# Header ok, payload+Ta all 0x5A, stored checksum 0x2211 (wrong). Regression
# for the recursive-resync stack overflow: the scan must walk every rejected
# candidate ITERATIVELY. Candidates start at HDR..OFF_TA (every consecutive
# pair inside the sync block), each rejected on type/subtype -> bad_header =
# OFF_TA - HDR + 1; no candidate survives, the tail byte (0x22) isn't sync ->
# the whole frame dropped. Then the appended clean frame parses.
sE_bad = bytearray([SYNC0, SYNC1, TYPE, SUBTYPE]) + bytearray([SYNC0] * (2 * PIXELS + 2))
sE_bad += struct.pack("<H", 0x2211)
assert (sum(sE_bad[:FRAME_LEN - 2]) & 0xFFFF) != 0x2211
assert len(sE_bad) == FRAME_LEN
sE = bytes(sE_bad) + F0
write("dense_sync", sE, expected([frame_json(1, px_std(0), 2560)], frames_ok=1,
                                 bad_checksum=1, bad_header=OFF_TA - HDR + 1,
                                 resyncs=1, bytes_dropped=FRAME_LEN))

# --- F. bad header: right sync, wrong type -----------------------------------
# [5A 5A ~TYPE SUBTYPE] fails the pos==HDR check -> resync finds no pair in
# the 4 bytes, the tail isn't sync -> 4 dropped; the clean frame parses.
sF = bytes([SYNC0, SYNC1, TYPE ^ 0x01, SUBTYPE]) + F0
write("bad_header", sF, expected([frame_json(1, px_std(0), 2560)], frames_ok=1,
                                 bad_header=1, resyncs=1, bytes_dropped=HDR))

# --- G. negative temperatures ------------------------------------------------
# int16 decode below zero for pixels AND Ta.
sG = frame(px_neg, -500)
assert_sync_pairs_exactly(sG, {0})
write("negative_temp", sG, expected([frame_json(1, px_neg, -500)], frames_ok=1))

# --- H. tail-sync retention across a rejected frame --------------------------
# F0 with its stored checksum forced to 0x5A11 (LE bytes 0x11 0x5A): the
# rejected frame's LAST byte is a lone 0x5A. Reasoning: checksum fails, the
# scan finds no interior pair (asserted), tail_sync keeps that 0x5A ->
# FRAME_LEN-1 dropped, pos=1. The next frame's own sync pair then lands as
# buf[1..2], so pos==HDR sees [5A 5A 5A TYPE] -> bad_header, resync #2 finds
# the pair at offset 1 (too short to type-check) -> 1 more byte dropped, and
# the frame completes cleanly from its own start. Total dropped FRAME_LEN,
# resyncs=2.
sH_bad = frame(px_std(0), 2560, checksum_override=0x5A11)
assert sH_bad[-1] == SYNC0 and sH_bad[-2] == 0x11
assert_sync_pairs_exactly(sH_bad, {0})
sH = sH_bad + F1
write("tail_sync", sH, expected([frame_json(1, px_std(1), 2570)], frames_ok=1,
                                bad_checksum=1, bad_header=1, resyncs=2,
                                bytes_dropped=FRAME_LEN))

print("fixtures written to", OUT + ":", sorted(os.listdir(FIX)))
