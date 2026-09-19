#!/usr/bin/env python3
"""registration.py on constructed data. No board, no camera, no thermal module.

    ./test_registration.py

Every expectation is derived from the definition, never from what the code
printed last time. The solver is checked by PLANTING a known (scale, dx, dy),
generating the correspondences it would produce, and requiring them back
exactly — a fit that cannot recover a mapping it was handed cannot be trusted
with one it has to discover.

The interpolation test is the one that matters most. Parallax disparity goes
as 1/Z, so entries are mixed in inverse range; doing it linearly in millimetres
gives an answer that is right at both ends and wrong everywhere between, which
is the shape of error nobody notices. The case below is built so that the two
methods disagree by 25%, and only the correct one passes.
"""
import json
import math
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "../../tools/s3cam"))
import registration as G                                            # noqa: E402

RGB_W, RGB_H = 640, 480
ROWS, COLS = G.ROWS, G.COLS


def plant(sx, sy, dx, dy, xy):
    """The correspondences a given mapping WOULD produce, exactly."""
    pts = []
    for x, y in xy:
        u = x / RGB_W * COLS - COLS / 2.0
        v = y / RGB_H * ROWS - ROWS / 2.0
        pts.append(G.Point(x, y, v * sy + ROWS / 2.0 + dy,
                           u * sx + COLS / 2.0 + dx))
    return pts


SPREAD = [(80, 60), (560, 60), (80, 420), (560, 420), (320, 240)]


def entry(range_mm, sx, dx, dy, adoptable=True, sy=None):
    return {"range_mm": float(range_mm), "sx": sx, "sy": sy if sy else sx,
            "dx": dx, "dy": dy, "anisotropy": 1.0,
            "rgb_w": RGB_W, "rgb_h": RGB_H, "flipv": False, "fliph": False,
            "quality": {"n": 5, "p90": 0.1, "max": 0.2, "rms": 0.1,
                        "bias_r": 0.0, "bias_c": 0.0},
            "points": [], "t": "2026-09-19T16:00:00+08:00", "git_rev": "deadbee",
            "note": "", "gate": {}, "adoptable": adoptable}


class TestSolve(unittest.TestCase):
    def test_recovers_a_planted_mapping_exactly(self):
        for sx, sy, dx, dy in ((1.0, 1.0, 0.0, 0.0), (0.62, 0.9, 3.5, -2.25),
                               (1.4, 0.55, -6.0, 4.0)):
            s = G.solve(plant(sx, sy, dx, dy, SPREAD), RGB_W, RGB_H)
            with self.subTest(sx=sx, sy=sy):
                self.assertTrue(s["ok"], s["reason"])
                self.assertAlmostEqual(s["sx"], sx, places=9)
                self.assertAlmostEqual(s["sy"], sy, places=9)
                self.assertAlmostEqual(s["dx"], dx, places=9)
                self.assertAlmostEqual(s["dy"], dy, places=9)

    def test_an_anisotropic_mapping_is_recoverable_at_all(self):
        """The whole reason the model changed.

        An isotropic solver handed these points has no parameter that can
        express sx != sy, so it splits the difference and leaves a residual
        that is structural — present at every point, removable by nothing.
        """
        pts = plant(1.30, 0.70, 0.0, 0.0, SPREAD)
        s = G.solve(pts, RGB_W, RGB_H)
        self.assertAlmostEqual(s["sx"] / s["sy"], 1.30 / 0.70, places=9)
        res = G.residuals(pts, s["sx"], s["sy"], s["dx"], s["dy"], RGB_W, RGB_H)
        self.assertLess(max(res), 1e-9)
        # what one shared scale would have been forced to do instead
        iso = (s["sx"] + s["sy"]) / 2
        bad = G.residuals(pts, iso, iso, s["dx"], s["dy"], RGB_W, RGB_H)
        self.assertGreater(max(bad), 2.0)

    def test_a_perfect_fit_has_zero_residual(self):
        pts = plant(0.8, 1.1, 2.0, -1.0, SPREAD)
        s = G.solve(pts, RGB_W, RGB_H)
        res = G.residuals(pts, s["sx"], s["sy"], s["dx"], s["dy"], RGB_W, RGB_H)
        self.assertLess(max(res), 1e-9)

    def test_too_few_points_is_a_refusal_not_an_identity_mapping(self):
        # Returning (1, 0, 0) here would put a calibration nobody solved into
        # the record, looking exactly like one somebody did.
        s = G.solve(plant(1.0, 1.0, 0.0, 0.0, SPREAD[:2]), RGB_W, RGB_H)
        self.assertFalse(s["ok"])
        self.assertIn("need 3", s["reason"])
        self.assertIsNone(s["sx"])

    def test_clustered_points_cannot_observe_the_scales(self):
        near = [(320, 240), (322, 241), (318, 239), (321, 238)]
        s = G.solve(plant(1.0, 1.0, 0.0, 0.0, near), RGB_W, RGB_H)
        self.assertFalse(s["ok"])
        self.assertIn("clustered", s["reason"])

    def test_a_horizontal_line_of_points_refuses_on_the_VERTICAL_axis(self):
        """The failure the old pooled spread check could not see.

        registration-plan.md collects points by jogging the head so a fixed
        heat source lands in different parts of the frame. Jogging only pan
        gives a row of points that pins sx perfectly and says nothing about sy.
        Under one shared scale the horizontal evidence stood in for both; with
        the axes separate the vertical fit would be a line through noise, and
        it must be refused BY NAME so the operator knows to jog tilt too.
        """
        flat = [(80, 240), (240, 240), (400, 240), (560, 240)]
        s = G.solve(plant(1.0, 1.0, 0.0, 0.0, flat), RGB_W, RGB_H)
        self.assertFalse(s["ok"])
        self.assertIn("vertically", s["reason"])
        self.assertNotIn("horizontally", s["reason"])
        self.assertGreater(s["spread_u"], G.MIN_SPREAD_PX)

    def test_noise_perturbs_but_does_not_break_the_fit(self):
        pts = plant(0.9, 0.9, 1.0, 1.0, SPREAD)
        noisy = [G.Point(p.x, p.y, p.r + d, p.c - d)
                 for p, d in zip(pts, (0.1, -0.1, 0.1, -0.1, 0.0))]
        s = G.solve(noisy, RGB_W, RGB_H)
        self.assertTrue(s["ok"])
        self.assertAlmostEqual(s["sx"], 0.9, places=1)
        self.assertAlmostEqual(s["sy"], 0.9, places=1)


class TestQuality(unittest.TestCase):
    def test_a_pure_offset_shows_up_as_bias_not_scatter(self):
        # The distinction is actionable: all-bias means the mapping is simply
        # shifted and can be corrected; the same magnitude scattered means the
        # correspondences themselves are bad and must be taken again.
        pts = plant(1.0, 1.0, 0.0, 0.0, SPREAD)
        shifted = [G.Point(p.x, p.y, p.r + 0.5, p.c + 0.5) for p in pts]
        q = G.quality(shifted, 1.0, 1.0, 0.0, 0.0, RGB_W, RGB_H)
        self.assertAlmostEqual(q["bias_r"], 0.5, places=9)
        self.assertAlmostEqual(q["bias_c"], 0.5, places=9)
        self.assertAlmostEqual(q["rms"], math.hypot(0.5, 0.5), places=9)

    def test_alternating_error_has_no_bias(self):
        pts = plant(1.0, 1.0, 0.0, 0.0, SPREAD)
        jit = [G.Point(p.x, p.y, p.r + (0.5 if i % 2 else -0.5), p.c)
               for i, p in enumerate(pts[:4])]
        q = G.quality(jit, 1.0, 1.0, 0.0, 0.0, RGB_W, RGB_H)
        self.assertAlmostEqual(q["bias_r"], 0.0, places=9)
        self.assertAlmostEqual(q["max"], 0.5, places=9)

    def test_max_is_reported_beside_p90(self):
        # One bad corner is the failure a percentile hides, and corners are
        # where a plant sits when the frame is full.
        pts = plant(1.0, 1.0, 0.0, 0.0, SPREAD)
        one_bad = [G.Point(p.x, p.y, p.r + (3.0 if i == 0 else 0.0), p.c)
                   for i, p in enumerate(pts)]
        q = G.quality(one_bad, 1.0, 1.0, 0.0, 0.0, RGB_W, RGB_H)
        self.assertAlmostEqual(q["max"], 3.0, places=9)


class TestDiagnose(unittest.TestCase):
    """The boundary, measured rather than asserted in prose.

    A four-parameter fit cannot represent rotation or lens distortion, and both
    announce themselves in the SHAPE of the leftovers, not their size. Without
    these numbers a residual over the gate is just "it does not fit" and the
    next move is guesswork.
    """

    WIDE = [(80, 60), (560, 60), (80, 420), (560, 420),
            (320, 240), (320, 60), (80, 240)]

    @staticmethod
    def _rotate(pts, th):
        out = []
        for p in pts:
            pr, pc = p.r - ROWS / 2.0, p.c - COLS / 2.0
            out.append(G.Point(p.x, p.y,
                               pc * math.sin(th) + pr * math.cos(th) + ROWS / 2.0,
                               pc * math.cos(th) - pr * math.sin(th) + COLS / 2.0))
        return out

    def _fit_and_diagnose(self, pts):
        s = G.solve(pts, RGB_W, RGB_H)
        self.assertTrue(s["ok"], s["reason"])
        return s, G.diagnose(pts, s["sx"], s["sy"], s["dx"], s["dy"], RGB_W, RGB_H)

    def test_a_planted_rotation_comes_back_within_ten_percent(self):
        # The magnitude matters, not just the flag: "about 3 degrees" sends
        # somebody to the bracket, "about 8" sends them to the wrong problem.
        for th in (0.05, -0.05, 0.10):
            pts = self._rotate(plant(1.0, 1.0, 0, 0, self.WIDE), th)
            _, d = self._fit_and_diagnose(pts)
            with self.subTest(theta=th):
                self.assertAlmostEqual(abs(d["rotation_rad"]), abs(th),
                                       delta=0.1 * abs(th))
                self.assertEqual(d["rotation_rad"] > 0, th < 0)   # consistent sign
                self.assertIn("rotation", d["hint"])

    def test_the_slope_is_fitted_through_the_origin(self):
        """Regression: a free intercept overstated a planted 0.050 by 26%.

        A rotation displaces nothing at the centre of rotation, so the
        intercept is zero by construction; leaving it free gave it signal to
        absorb and sent the estimate 26% high — enough to chase a mounting
        error that is not there.
        """
        pts = self._rotate(plant(1.0, 1.0, 0, 0, self.WIDE), 0.05)
        _, d = self._fit_and_diagnose(pts)
        self.assertLess(abs(abs(d["rotation_rad"]) - 0.05), 0.05 * 0.10)

    def test_a_clean_fit_reports_neither_term(self):
        _, d = self._fit_and_diagnose(plant(1.2, 0.8, 2.0, -1.0, self.WIDE))
        self.assertAlmostEqual(d["rotation_rad"], 0.0, places=9)
        self.assertIn("nothing left", d["hint"])

    def test_rotation_is_almost_purely_tangential(self):
        # The separation that makes the two hints trustworthy: a rotation puts
        # essentially none of its energy in the radial direction, so the
        # distortion branch cannot fire on a twisted bracket.
        pts = self._rotate(plant(1.0, 1.0, 0, 0, self.WIDE), 0.05)
        _, d = self._fit_and_diagnose(pts)
        self.assertLess(d["radial_frac"], 0.10)

    def test_distortion_shows_as_a_radial_trend(self):
        # Planted as error growing with the SQUARE of radius: a term linear in
        # radius is just a scale change and the fit absorbs it, which is
        # exactly why a linear one would be invisible here.
        base = plant(1.0, 1.0, 0, 0, self.WIDE)
        bent = []
        for p in base:
            pr, pc = p.r - ROWS / 2.0, p.c - COLS / 2.0
            rad = math.hypot(pr, pc)
            k = 0.02
            bent.append(G.Point(p.x, p.y, p.r + k * pr * rad, p.c + k * pc * rad))
        _, d = self._fit_and_diagnose(bent)
        self.assertGreater(d["radial_frac"], 0.7)
        self.assertLess(abs(math.degrees(d["rotation_rad"])), 1.0)
        self.assertIn("distortion", d["hint"])

    def test_a_straight_line_fit_would_have_missed_this(self):
        """Why the radial term is an energy fraction and not a slope.

        Distortion grows as a high power of radius, so a line through the
        origin fitted to it understates it enormously: the r-cubed case below
        produces residuals of tens of pixels and returned a slope of 0.047 —
        the same order as noise. The fraction of residual energy pointing
        radially does not care how the magnitude grows.
        """
        base = plant(1.0, 1.0, 0, 0, self.WIDE)
        cubed = []
        for p in base:
            pr, pc = p.r - ROWS / 2.0, p.c - COLS / 2.0
            f = 0.0005 * math.hypot(pr, pc) ** 3
            cubed.append(G.Point(p.x, p.y, p.r + f * pr, p.c + f * pc))
        s, d = self._fit_and_diagnose(cubed)
        q = G.quality(cubed, s["sx"], s["sy"], s["dx"], s["dy"], RGB_W, RGB_H)
        self.assertGreater(q["p90"], 5.0)            # unmistakably broken
        self.assertGreater(d["radial_frac"], 0.7)    # and correctly attributed

    def test_scatter_is_named_as_scatter(self):
        import random
        random.seed(3)
        pts = plant(1.0, 1.0, 0, 0, self.WIDE)
        jit = [G.Point(p.x, p.y, p.r + random.gauss(0, 0.3),
                       p.c + random.gauss(0, 0.3)) for p in pts]
        _, d = self._fit_and_diagnose(jit)
        self.assertIn("scatter", d["hint"])
        # neither pattern dominates: that IS the diagnosis
        self.assertGreater(d["radial_frac"], 0.2)
        self.assertLess(d["radial_frac"], 0.7)

    def test_too_few_off_centre_points_says_so(self):
        pts = plant(1.0, 1.0, 0, 0, [(320, 240), (330, 250), (310, 230)])
        d = G.diagnose(pts, 1.0, 1.0, 0.0, 0.0, RGB_W, RGB_H)
        self.assertIsNone(d["rotation_rad"])
        self.assertIsNone(d["radial_frac"])
        self.assertIn("too few", d["hint"])


class TestCalibrationAdd(unittest.TestCase):
    def test_a_clean_fit_is_adoptable(self):
        cal = G.Calibration()
        r = cal.add(800, plant(0.7, 0.9, 1.0, -1.0, SPREAD), RGB_W, RGB_H)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertTrue(r["entry"]["adoptable"])
        self.assertEqual(len(cal.entries), 1)

    def test_a_bad_fit_is_recorded_but_not_adoptable(self):
        # Recorded, because the evidence of a failed attempt is worth keeping;
        # not adoptable, because two thermal pixels of registration error is
        # bigger than the repeatability Phase 3 exists to measure.
        pts = plant(1.0, 1.0, 0.0, 0.0, SPREAD)
        wrecked = [G.Point(p.x, p.y, p.r + (4.0 if i % 2 else -4.0), p.c)
                   for i, p in enumerate(pts)]
        cal = G.Calibration()
        r = cal.add(800, wrecked, RGB_W, RGB_H)
        self.assertTrue(r["ok"])
        self.assertFalse(r["entry"]["adoptable"])

    def test_range_is_required_and_must_be_positive(self):
        cal = G.Calibration()
        for bad in (None, 0, -100, float("nan")):
            self.assertFalse(cal.add(bad, plant(1, 1, 0, 0, SPREAD), RGB_W, RGB_H)["ok"])
        self.assertEqual(cal.entries, [])

    def test_the_points_are_kept_with_the_fit(self):
        cal = G.Calibration()
        cal.add(800, plant(0.7, 0.9, 1.0, -1.0, SPREAD), RGB_W, RGB_H)
        self.assertEqual(len(cal.entries[0]["points"]), len(SPREAD))

    def test_orientation_is_part_of_the_entry(self):
        cal = G.Calibration(flipv=True, fliph=False)
        cal.add(800, plant(0.7, 0.7, 0, 0, SPREAD), RGB_W, RGB_H)
        self.assertTrue(cal.entries[0]["flipv"])
        self.assertFalse(cal.entries[0]["fliph"])


class TestForDistance(unittest.TestCase):
    def test_interpolation_is_linear_in_inverse_range(self):
        """The physics, and the test that separates it from the plausible error.

        dx = K/Z with K = 1000: 2.000 at 500 mm, 0.6667 at 1500 mm. The true
        value at 750 mm is 1.3333. Mixing linearly in millimetres would give
        1.6667 — 25% high, and perfectly smooth.
        """
        cal = G.Calibration([entry(500, 1.0, 1000 / 500, 0.0),
                             entry(1500, 1.0, 1000 / 1500, 0.0)])
        got = cal.for_distance(750)
        self.assertTrue(got["ok"], got.get("reason"))
        self.assertEqual(got["how"], "interpolated")
        self.assertAlmostEqual(got["dx"], 1000 / 750, places=9)
        self.assertNotAlmostEqual(got["dx"], 1.66666667, places=3)

    def test_an_exact_entry_is_used_as_is_and_says_so(self):
        cal = G.Calibration([entry(500, 0.8, 2.0, 1.0), entry(1500, 0.8, 0.6, 1.0)])
        got = cal.for_distance(500)
        self.assertEqual(got["how"], "exact")
        self.assertAlmostEqual(got["dx"], 2.0)

    def test_outside_the_measured_span_it_refuses(self):
        cal = G.Calibration([entry(500, 1.0, 2.0, 0.0), entry(1500, 1.0, 0.67, 0.0)])
        for z in (400, 1600):
            got = cal.for_distance(z)
            self.assertFalse(got["ok"])
            self.assertIn("do not extrapolate", got["reason"])

    def test_one_entry_cannot_be_interpolated_from(self):
        cal = G.Calibration([entry(800, 1.0, 1.0, 0.0)])
        got = cal.for_distance(900)
        self.assertFalse(got["ok"])
        self.assertIn("outside", got["reason"])
        self.assertTrue(cal.for_distance(800)["ok"])     # its own distance is fine

    def test_no_range_is_a_refusal(self):
        cal = G.Calibration([entry(500, 1.0, 2.0, 0.0), entry(1500, 1.0, 0.67, 0.0)])
        got = cal.for_distance(None)
        self.assertFalse(got["ok"])
        self.assertIn("only defined at a distance", got["reason"])

    def test_unadoptable_entries_do_not_participate(self):
        cal = G.Calibration([entry(500, 1.0, 2.0, 0.0, adoptable=False),
                             entry(1500, 1.0, 0.67, 0.0)])
        got = cal.for_distance(750)
        self.assertFalse(got["ok"])
        self.assertIn("outside", got["reason"])   # only one usable entry remains

    def test_it_names_which_entries_it_used(self):
        cal = G.Calibration([entry(500, 1.0, 2.0, 0.0), entry(1500, 1.0, 0.67, 0.0)])
        self.assertEqual(cal.for_distance(750)["entries"], [500.0, 1500.0])


class TestPersistence(unittest.TestCase):
    def test_round_trip(self):
        cal = G.Calibration(flipv=True)
        cal.add(800, plant(0.7, 0.9, 1.0, -1.0, SPREAD), RGB_W, RGB_H, note="bench")
        with tempfile.TemporaryDirectory() as d:
            p = cal.save(os.path.join(d, "reg.json"))
            back = G.Calibration.load(p)
        self.assertTrue(back.flipv)
        self.assertEqual(len(back.entries), 1)
        self.assertAlmostEqual(back.entries[0]["sx"], cal.entries[0]["sx"])
        self.assertEqual(back.entries[0]["note"], "bench")

    def test_a_missing_file_is_an_empty_record_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(G.Calibration.load(os.path.join(d, "nope.json")).entries, [])

    def test_a_future_version_is_refused_rather_than_guessed_at(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "reg.json")
            with open(p, "w") as fh:
                json.dump({"version": 99, "entries": []}, fh)
            with self.assertRaises(SystemExit):
                G.Calibration.load(p)

    def test_entries_stay_sorted_by_range(self):
        cal = G.Calibration()
        for z in (1200, 400, 800):
            cal.add(z, plant(0.7, 0.9, 1.0, -1.0, SPREAD), RGB_W, RGB_H)
        self.assertEqual([e["range_mm"] for e in cal.entries], [400.0, 800.0, 1200.0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
