#!/usr/bin/env python3
"""Sweep the OV5640's focus motor by hand and plot sharpness against position.

    ./vcm_sweep.py http://<ip>                    # 8 positions across full travel
    ./vcm_sweep.py http://<ip> --steps 16 --halt-mcu
    ./vcm_sweep.py http://<ip> --roi 900,700,1700,1300

This is the instrument that settles "does the lens actually move", which
autofocus cannot answer: AF reports success against a dead actuator just as
happily as against a live one, because the OV5640's AF firmware has no lens
position feedback. Commanding the VCM DAC directly and watching sharpness is
the measurement AF is not.

READ THIS BEFORE TRUSTING A FLAT RESULT
A sweep that produces no sharpness change means one of:
  * the coil is not being driven (no AF-VCC on pin 24 of the ribbon, an open
    coil, or no VCM in the lens at all), or
  * the subject is outside the lens's reachable focus range at every position.
The second is ruled out by putting a textured target 10-30 cm away, where a
lens parked at infinity is visibly soft. Both were true on the first board this
ran on, and the sweep stayed flat to 1.02x across the full 10-bit range.

WHAT THE NUMBERS MEAN
Laplacian variance is comparable WITHIN one sweep and meaningless across two.
Exposure, gain and scene all move it — gain especially, because noise is
high-frequency and inflates the score. Do not change /cam/tune mid-sweep, and
do not compare today's peak against yesterday's.

REGISTERS (OV5640 datasheet; the mapping is documented, the AF command bytes
are not — see af.cpp)
    0x3602[7:4] = DAC target bits [3:0],  [3:0] = slew rate
    0x3603[5:0] = DAC target bits [9:4],  bit7  = VCM power down
So 0x3603 alone is a coarse position knob: 0x00..0x3F walks the whole travel in
steps of 16, which is why --steps defaults to a divisor of that.
"""
import argparse
import io
import re
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from focus_score import score_bytes  # noqa: E402


class Board:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def get(self, path, timeout=70):
        return urllib.request.urlopen(self.base + path, timeout=timeout).read()

    def reg(self, addr):
        t = self.get(f"/cam/reg?from=0x{addr:04X}&to=0x{addr:04X}", 25).decode()
        m = re.search(r"\s0x%04X\s+0x([0-9A-F]{2})" % addr, t)
        if not m:
            raise SystemExit(f"could not read 0x{addr:04X} — is /cam/reg present?")
        return int(m.group(1), 16)

    def wreg(self, addr, val):
        self.get(f"/cam/reg?a=0x{addr:04X}&v=0x{val:02X}", 25)

    def target(self):
        r2, r3 = self.reg(0x3602), self.reg(0x3603)
        return ((r3 & 0x3F) << 4) | (r2 >> 4), (r3 >> 7) & 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("base_url", help="e.g. http://192.168.50.126")
    ap.add_argument("--steps", type=int, default=8, help="sweep points (default 8)")
    ap.add_argument("--settle", type=float, default=0.7,
                    help="seconds to let the coil settle before capturing")
    ap.add_argument("--halt-mcu", action="store_true",
                    help="hold the AF MCU in reset so it cannot fight the writes. "
                         "Safe (that is its power-on state) and reversible; the "
                         "MCU is released again on exit.")
    ap.add_argument("--keep-rest", action="store_true",
                    help="do NOT pin the resting framesize to qsxga. By default "
                         "it is pinned, because /capture's VGA<->QSXGA switch "
                         "rewrites sensor tables and can undo the DAC write.")
    a = ap.parse_args()

    b = Board(a.base_url)
    restored = []

    # Pin the resolution first: a framesize change reprograms the sensor, and a
    # sweep whose position silently resets between points measures nothing.
    if not a.keep_rest:
        b.get("/power?rest=qsxga", 30)
        restored.append(lambda: b.get("/power?rest=vga", 30))

    sys0 = b.reg(0x3000)
    if a.halt_mcu:
        b.wreg(0x3000, sys0 | 0x20)
        restored.append(lambda: b.wreg(0x3000, sys0))
        print(f"AF MCU held in reset (0x3000 {sys0:#04x} -> {b.reg(0x3000):#04x})")

    slew = b.reg(0x3602) & 0x0F     # preserve whatever slew rate is configured
    print(f"slew bits {slew:#03x}\n")
    print(f"{'DAC':>5} {'readback':>9} {'PD':>3} {'sharpness':>10}")
    rows = []
    try:
        for i in range(a.steps):
            p = round(i * 1023 / max(1, a.steps - 1))
            b.wreg(0x3603, (p >> 4) & 0x3F)
            b.wreg(0x3602, ((p & 0x0F) << 4) | slew)
            time.sleep(a.settle)
            got, pd = b.target()
            s = score_bytes(b.get("/capture"))
            rows.append((p, got, s))
            flag = "" if got == p else "   <- DAC did NOT take the value"
            print(f"{p:5d} {got:9d} {pd:3d} {s:10.1f}{flag}")
    finally:
        for undo in reversed(restored):
            try:
                undo()
            except Exception as e:                                # noqa: BLE001
                print(f"cleanup failed: {e}", file=sys.stderr)

    if not rows:
        return 1
    lo = min(r[2] for r in rows)
    hi = max(r[2] for r in rows)
    peak = max(rows, key=lambda r: r[2])
    ratio = hi / lo if lo else 0.0
    print(f"\nsharpness {lo:.1f}..{hi:.1f}   ratio {ratio:.2f}x   peak at DAC {peak[0]}")
    # 1.5x is not a physical constant; it is comfortably above the few percent
    # that scene drift and JPEG noise produce, and far below the several-fold
    # swing a real focus curve shows.
    if ratio > 1.5:
        print("=> THE LENS MOVES: a focus curve exists. Refine around the peak.")
    else:
        print("=> FLAT: the DAC accepts the value but the image does not change.\n"
              "   The break is between the DAC and the lens — AF-VCC (ribbon pin 24),\n"
              "   the coil, or a lens with no VCM in it. Not a software problem.")
    if any(r[1] != r[0] for r in rows):
        print("   NOTE: some positions did not read back — something is rewriting\n"
              "   0x3602/0x3603 (continuous AF still running?). Retry with --halt-mcu.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
