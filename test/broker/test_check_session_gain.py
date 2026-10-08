#!/usr/bin/env python3
"""check-session-gain.py's regression on synthetic sessions. No InfluxDB.

    ./test_check_session_gain.py

The case it must get right is the 2026-10-07 shape: every pot reads 5 %
heavier, so the big ones cross the watering threshold and the small ones do
not. And the case it must NOT call a gain error on its own: a batch watering
of the big pots, which has the same slope — only the next session separates
them, and the script is required to leave that to the reader.
"""
import importlib.util, os, random, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "cg", os.path.join(HERE, "../../broker/check-session-gain.py"))
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

POTS = {f"p{i}": w for i, w in enumerate([190, 200, 210, 230, 250, 260, 280, 300,
                                           400, 500, 700, 800, 1400, 2300])}


class Gain(unittest.TestCase):
    def test_a_gain_error_reads_as_a_slope_with_high_r(self):
        rnd = random.Random(1)
        prev = dict(POTS)
        cur = {p: w * 1.053 - 3 + rnd.gauss(0, 2) for p, w in POTS.items()}   # +5.3 %, drying, noise
        slope, r, n = cg.gain(prev, cur)
        self.assertAlmostEqual(slope, 0.053, delta=0.01)
        self.assertGreater(r, cg.R_FLAG)
        self.assertGreater(abs(slope), cg.GAIN_FLAG)

    def test_uniform_drying_has_no_slope(self):
        rnd = random.Random(2)
        prev = dict(POTS)
        cur = {p: w - 8 + rnd.gauss(0, 3) for p, w in POTS.items()}
        slope, r, n = cg.gain(prev, cur)
        self.assertLess(abs(slope), cg.GAIN_FLAG)

    def test_zero_drift_has_no_slope(self):
        rnd = random.Random(3)
        prev = dict(POTS)
        cur = {p: w + 6 + rnd.gauss(0, 1) for p, w in POTS.items()}   # +6 g to every pot, the known zero drift
        slope, r, n = cg.gain(prev, cur)
        self.assertLess(abs(slope), 0.005)
        # and an EXACT uniform shift has nothing to regress — None, not a crash
        self.assertIsNone(cg.gain(prev, {p: w + 6 for p, w in POTS.items()}))

    def test_watering_the_big_pots_also_slopes_and_that_is_by_design(self):
        # the ambiguity is real: the script flags this too, and its text says
        # the next session is what separates the two
        prev = dict(POTS)
        cur = {p: (w + 0.05 * w if w >= 600 else w - 5) for p, w in POTS.items()}
        slope, r, n = cg.gain(prev, cur)
        self.assertGreater(abs(slope), cg.GAIN_FLAG)

    def test_too_few_shared_pots_is_none(self):
        self.assertIsNone(cg.gain({"a": 1.0, "b": 2.0}, {"a": 1.1, "b": 2.2}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
