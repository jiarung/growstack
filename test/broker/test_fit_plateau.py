#!/usr/bin/env python3
"""fit-plateau.py on synthetic pots. No InfluxDB.

    ./test_fit_plateau.py

The thing that must never happen silently: a pot that has NOT plateaued inside
the window gets an A that is really "loss at day 8", panel 10 divides by it,
and a big pot reads 100% while still soaking wet. That is the boundary test.
"""
import datetime, importlib.util, math, os, random, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "fp", os.path.join(HERE, "../../broker/fit-plateau.py"))
fp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fp)

T0 = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
def at(d):
    return T0 + datetime.timedelta(days=d)


def synth(A, tau, cycles=5, days=4, step=0.5, noise=0.0, seed=1):
    """A pot that loses A*(1-exp(-t/tau)) after each watering, rewatered
    every `days` days back to 300 g."""
    rnd = random.Random(seed)
    out, t = [(at(-0.5), 270.0)], 0.0           # a watering is a RISE: give the first one a floor
    for _ in range(cycles):
        t += 0.01                                  # the watering reading
        out.append((at(t), 300.0))
        for k in range(1, int(days / step) + 1):
            tt = k * step
            out.append((at(t + tt), 300.0 - A * (1 - math.exp(-tt / tau))
                        + rnd.gauss(0, noise)))
        t += days
    return out


class Cycles(unittest.TestCase):
    def test_each_watering_starts_a_new_run_at_zero_loss(self):
        cy = fp.cycles(synth(30, 1.0, cycles=3))
        self.assertEqual(len(cy), 3)
        for c in cy:
            self.assertAlmostEqual(c[0][0], 0.0)
            self.assertAlmostEqual(c[0][1], 0.0)
            self.assertGreater(c[-1][1], 25.0)


class Fit(unittest.TestCase):
    def test_recovers_a_clean_curve(self):
        pts = [x for c in fp.cycles(synth(30, 1.2)) for x in c]
        A, tau, rmse = fp.fit(pts)
        self.assertAlmostEqual(A, 30.0, delta=0.5)
        self.assertAlmostEqual(tau, 1.2, delta=0.1)
        self.assertLess(rmse, 0.5)

    def test_survives_scale_noise(self):
        # 3.5 g per reading is the scale's measured sigma (WATERING-INDEX.md);
        # much more and the noise itself starts to look like waterings (JUMP_G)
        pts = [x for c in fp.cycles(synth(30, 1.2, cycles=8, noise=3.5)) for x in c]
        A, tau, rmse = fp.fit(pts)
        self.assertAlmostEqual(A, 30.0, delta=5.0)
        self.assertAlmostEqual(tau, 1.2, delta=0.5)

    def test_a_pot_still_drying_at_the_window_edge_hits_the_grid_edge(self):
        # tau = 12 d: at day 8 it has lost only half of A. The fit MUST land
        # on the largest tau in the grid, which is the signal fit-plateau.py
        # uses to refuse it — a fitted A here would be "loss at day 8".
        pts = [x for c in fp.cycles(synth(300, 12.0, cycles=4, days=8))
               for x in c if x[0] <= fp.MAX_DAYS]
        A, tau, rmse = fp.fit(pts)
        self.assertGreaterEqual(tau, fp.TAU_GRID[-1])
        # and the A it lands on is not the pot's real plateau — it is somewhere
        # between "loss at day 8" (146 g) and the truth (300 g), trustworthy as
        # neither. The grid-edge tau is what fit-plateau.py keys the refusal on.
        self.assertLess(A, 300.0 * 0.85)
        self.assertGreater(A, 146.0)

    def test_a_denominator_of_A_puts_the_plateau_at_100_percent(self):
        # the whole point: at 3 tau a pot has lost 95% of A
        A, tau = 30.0, 1.0
        pts = [x for c in fp.cycles(synth(A, tau)) for x in c]
        Af, tf, _ = fp.fit(pts)
        loss_at_3tau = A * (1 - math.exp(-3.0))
        self.assertAlmostEqual(100 * loss_at_3tau / Af, 95.0, delta=2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
