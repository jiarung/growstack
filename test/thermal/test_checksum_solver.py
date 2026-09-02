#!/usr/bin/env python3
"""The checksum solver must find a planted algorithm — and reject a random one.

A brute-force search that reports a hit is only useful if it (a) finds the
truth when the truth is in its catalogue and (b) does not manufacture one when
it isn't. Both are tested here against synthetic frames whose answer we chose.

    ./test_checksum_solver.py        # exits non-zero on failure
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "../../tools/s3cam"))
from thermal_checksum import CRC16, crc, matches  # noqa: E402

HDR = bytes([0x5A, 0x5A, 0x02, 0x06])
PIXELS, TA_RAW = 768, 2812
FRAME_LEN = 4 + 2 * PIXELS + 4


def body(seed):
    b = bytearray(HDR)
    b += struct.pack(f"<{PIXELS}h", *(2000 + ((p + seed) % 300) for p in range(PIXELS)))
    b += struct.pack("<h", TA_RAW)
    return bytes(b)


def frame(seed, cs):
    f = body(seed) + struct.pack("<H", cs)
    assert len(f) == FRAME_LEN, len(f)
    return f


def survivors(frames):
    """What the tool reports for a whole capture: the cross-frame intersection."""
    common = None
    for f in frames:
        hits = matches(f, len(f) - 4, len(f) - 2)
        common = hits if common is None else (common & hits)
    return common


def check(label, ok):
    print(("pass " if ok else "FAIL ") + label)
    return ok


def main():
    seeds = (0, 37, 91, 150, 211)
    good = True

    # (a) a planted algorithm is found, and is the ONLY thing left standing
    for name, poly, init, refin, refout, xorout in CRC16:
        fs = [frame(s, crc(body(s)[4:4 + 2 * PIXELS], poly, init, refin,
                           refout, xorout, 16)) for s in seeds]
        left = survivors(fs)
        good &= check(f"planted crc16-{name} recovered uniquely",
                      left == {f"crc16-{name}/from4..toTa == LE16"})

    # (b) a checksum from OUTSIDE the catalogue must leave nothing standing.
    # A pseudo-random value per frame stands in for "an algorithm we do not
    # model": any single frame may collide by luck, five must not.
    fs = [frame(s, (s * 40503 + 12345) & 0xFFFF) for s in seeds]
    good &= check("unmodelled checksum yields no survivor", survivors(fs) == set())

    # (c) the intersection is what does the work. Build a capture where ONE
    # frame's checksum happens to equal a modelled hypothesis (a coincidence,
    # planted deterministically) while the rest are unmodelled: that frame on
    # its own reports a hit, and the intersection correctly reports none.
    lucky = frame(seeds[0], sum(body(seeds[0])[:FRAME_LEN - 2]) & 0xFFFF)
    alone = matches(lucky, FRAME_LEN - 4, FRAME_LEN - 2)
    mixed = [lucky] + [frame(s, (s * 40503 + 12345) & 0xFFFF) for s in seeds[1:]]
    good &= check("one frame can fit by coincidence", "sum16/from0..toCS == LE16" in alone)
    good &= check("the intersection rejects that coincidence", survivors(mixed) == set())

    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
