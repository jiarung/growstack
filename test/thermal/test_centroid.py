#!/usr/bin/env python3
"""The centroid must find a planted target to a fraction of a pixel — and
refuse when there is nothing to find.

Phase 3 asks whether the pan/tilt returns to the same place. MG996R's spec
(+-1-2 deg) lands at roughly ONE thermal pixel (1.72 deg/px pan, 1.46 deg/px
tilt), so the estimator has to resolve well inside a pixel or it cannot answer
the question at all — it would report "about one pixel" no matter what the
mechanism did. That is what the 0.5 px case here is for: it is not a nicety,
it is the gate that says the instrument is worth pointing at hardware.

The other half is refusal. An estimator that returns the frame centre when the
target is absent puts a fabricated position into a dataset that looks exactly
like a measurement.

    ./test_centroid.py        # exits non-zero on failure
"""
import math
import os
import random
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "../../tools/s3cam"))
from thermal_view import centroid  # noqa: E402

ROWS, COLS = 24, 32
BG_C = 24.0          # a plausible room
AMPL_C = 20.0        # a hot mug against it
SIGMA_PX = 1.6       # roughly what a small target subtends at working distance

# The gate. 0.15 px is comfortably below the ~1 px effect being measured, and
# comfortably above the arithmetic.
TOL_PX = 0.15


def blob(r0, c0, ampl=AMPL_C, sigma=SIGMA_PX, bg=BG_C, rows=ROWS, cols=COLS):
    """A Gaussian target centred at sub-pixel (r0, c0), sampled on the grid."""
    px = []
    for r in range(rows):
        for c in range(cols):
            d2 = (r - r0) ** 2 + (c - c0) ** 2
            px.append(bg + ampl * math.exp(-d2 / (2.0 * sigma * sigma)))
    return px


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    return cond


def main():
    ok = True
    print("recovery of a planted sub-pixel position:")
    # A centre at a whole or half pixel is sampled symmetrically and would come
    # back exact by symmetry alone, which tests nothing. 0.25/0.3/0.75 break
    # that symmetry, so the grid sampling has to actually be handled.
    recovered = {}
    for dc in (0.0, 0.25, 0.3, 0.5, 0.75):
        r_true, c_true = 12.0, 16.0 + dc
        cd = centroid(blob(r_true, c_true), ROWS, COLS)
        if not cd["ok"]:
            ok = check(f"offset {dc}px accepted", False, cd["reason"]) and ok
            continue
        recovered[dc] = cd["c"]
        er, ec = abs(cd["r"] - r_true), abs(cd["c"] - c_true)
        ok = check(f"offset {dc}px within {TOL_PX}px",
                   er < TOL_PX and ec < TOL_PX,
                   f"got r{cd['r']:.3f} c{cd['c']:.3f}, err r{er:.3f} c{ec:.3f}") and ok

    print("half-pixel discrimination (the Phase 3 gate):")
    if 0.0 in recovered and 0.5 in recovered:
        d = recovered[0.5] - recovered[0.0]
        # Not just "both are accurate" — the two must be TOLD APART. A biased
        # estimator can be accurate on average and still collapse the gap.
        ok = check("0.0px vs 0.5px separated by >0.3px", d > 0.3, f"gap {d:.3f}px") and ok
        ok = check("...and the gap is ~0.5px", abs(d - 0.5) < TOL_PX, f"gap {d:.3f}px") and ok
    else:
        ok = check("0.0px vs 0.5px separated", False, "one of them was rejected")

    print("monotonicity across the sweep:")
    xs = sorted(recovered)
    ok = check("recovered position increases with true position",
               all(recovered[a] < recovered[b] for a, b in zip(xs, xs[1:]))) and ok

    print("refusal:")
    flat = [BG_C] * (ROWS * COLS)
    cd = centroid(flat, ROWS, COLS)
    ok = check("uniform frame reports NO centroid", not cd["ok"] and cd["r"] is None,
               cd["reason"]) and ok

    warm = centroid(blob(12.0, 16.0, ampl=3.0), ROWS, COLS)   # 3C < MIN_CONTRAST_C
    ok = check("3C target refused as low contrast",
               not warm["ok"] and warm["reason"].startswith("low_contrast"),
               warm["reason"]) and ok

    tiny = centroid(blob(12.0, 16.0, sigma=0.35), ROWS, COLS)
    ok = check("single-pixel speck refused as too small support",
               not tiny["ok"] and tiny["reason"].startswith("support_too_small"),
               tiny["reason"]) and ok

    print("a second warm object must not drag the centroid:")
    # Two blobs, well separated so their half-max supports cannot touch. The
    # connectivity rule should keep only the peak's own component.
    a, b = blob(12.0, 10.0, ampl=20.0), blob(12.0, 24.0, ampl=16.0)
    both = [max(x, y) for x, y in zip(a, b)]
    cd = centroid(both, ROWS, COLS)
    ok = check("centroid stays on the hotter object", cd["ok"] and abs(cd["c"] - 10.0) < TOL_PX,
               f"got c{cd['c']:.3f}" if cd["ok"] else cd["reason"]) and ok

    print("a search window excludes an interfering source:")
    # Same two objects, but the window is drawn around the COOLER one — the
    # operator's way of saying "measure that one".
    cd = centroid(both, ROWS, COLS, box=(6, 18, 18, 30))
    ok = check("windowed centroid lands on the windowed object",
               cd["ok"] and abs(cd["c"] - 24.0) < TOL_PX,
               f"got c{cd['c']:.3f}" if cd["ok"] else cd["reason"]) and ok

    # This one is a PREDICTION, not just a correctness check: Stage 2 gates the
    # hardware run at sigma_static <= 0.1 px. Sensor noise alone sets a floor
    # under that number, and it is far cheaper to discover here that the gate is
    # unreachable than after mounting the head and running for twenty minutes.
    print("noise floor implied by sensor NETD (predicts Stage 2's sigma_static):")
    for netd in (0.1, 0.25):
        rng = random.Random(20260906)      # seeded: a flaky gate is not a gate
        rs, cs = [], []
        for _ in range(50):
            px = [v + rng.gauss(0.0, netd) for v in blob(12.0, 16.3)]
            cd = centroid(px, ROWS, COLS)
            if cd["ok"]:
                rs.append(cd["r"])
                cs.append(cd["c"])
        sr, sc = statistics.pstdev(rs), statistics.pstdev(cs)
        detail = f"NETD {netd}C -> sigma r{sr:.3f} c{sc:.3f} px  (n={len(rs)})"
        if netd == 0.1:
            ok = check("at spec NETD, estimator noise leaves room under 0.1px",
                       sr < 0.1 and sc < 0.1, detail) and ok
        else:
            # Not a gate — a datum. If the real target only manages this, the
            # plan's fallback (constant-power source, tighter window) is aimed
            # at the right thing.
            print(f"  ----  {detail}")

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
