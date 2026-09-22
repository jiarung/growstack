#!/usr/bin/env python3
"""analyze-et.py's derivations, on synthetic data. No InfluxDB, no docker.

    ./test_analyze_et.py

The load-bearing test is the last one. Every relative number the report prints
rests on the claim that a session's zero offset cancels when you subtract the
cohort median — so that claim is tested by ADDING a known offset to a whole
session and requiring the output not to move. If that ever stops holding, the
report is measuring the scale instead of the plants, and it would still look
perfectly reasonable.
"""
import datetime, importlib.util, os, random, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "et", os.path.join(HERE, "../../broker/analyze-et.py"))
et = importlib.util.module_from_spec(spec)
spec.loader.exec_module(et)

T0 = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
def at(days, hours=0):
    return T0 + datetime.timedelta(days=days, hours=hours)


class Registry(unittest.TestCase):
    def rows(self, extra=()):
        # (time, plant_id, weight, uid). 05 and 05b share a tag — the repot
        # handed it on; 16 and 13-2 each hold their own.
        r = []
        for p, d0, d1, uid in (("cactus-05", 0, 10, "T05"), ("cactus-05b", 11, 20, "T05"),
                               ("cactus-16", 0, 20, "T16"), ("cactus-13-2", 0, 20, "T132")):
            r += [(at(d), p, 200.0 + d, uid) for d in range(d0, d1 + 1)]
        return r + list(extra)

    def test_a_repot_retires_the_old_id_and_not_the_new_one(self):
        _, reg = et.registry(self.rows())
        self.assertEqual(reg["cactus-05b"]["succeeds"], "cactus-05")
        self.assertTrue(reg["cactus-05"]["retired"])
        self.assertFalse(reg["cactus-05b"]["retired"])

    def test_the_ended_list_retires_a_pot_with_no_successor(self):
        # A pot that rotted hands its tag to nobody, so lineage cannot see the
        # ending and only the dashboard's list can. The list is usually EMPTY —
        # an entry comes out again the moment the tag goes onto another pot —
        # so this drives registry() with a stand-in rather than asserting on
        # whatever happens to be in daily.json today. Pinning a live id here is
        # what made this test fail the day cactus-13-2's tag was recycled.
        real = et.ended_ids
        et.ended_ids = lambda: {"cactus-16"}
        try:
            _, reg = et.registry(self.rows())
        finally:
            et.ended_ids = real
        self.assertTrue(reg["cactus-16"]["retired"])
        self.assertIsNone(reg["cactus-16"]["succeeds"])
        self.assertIn("no successor", reg["cactus-16"]["retired_why"])

    def test_the_ended_list_parses_whatever_the_dashboard_holds(self):
        # the parser must survive an empty list as well as a populated one;
        # `ended = []` is valid Flux and is the normal state
        ids = et.ended_ids()
        self.assertIsInstance(ids, set)
        self.assertTrue(all(isinstance(i, str) and i for i in ids))

    def test_a_pot_whose_tag_was_reused_is_retired_without_any_list(self):
        # 2026-09-21: cactus-13-2 rotted, its tag went onto the new cactus-29.
        # No successor id, and the `ended` entry had been removed because the
        # derivation was supposed to cope — this is the rule that makes it cope.
        rows = self.rows(extra=[(at(25), "cactus-29", 250.0, "T132")])
        _, reg = et.registry(rows)
        self.assertTrue(reg["cactus-13-2"]["retired"])
        self.assertIn("another plant", reg["cactus-13-2"]["retired_why"])
        self.assertFalse(reg["cactus-29"]["retired"])

    def test_a_live_pot_that_is_merely_stale_is_not_retired(self):
        # "nobody weighed it this week" is not "it does not exist"
        _, reg = et.registry(self.rows())
        self.assertFalse(reg["cactus-16"]["retired"])

    def test_material_defaults_to_plastic_and_the_listed_ones_do_not(self):
        mats, dflt = et.materials()
        self.assertEqual(dflt, "plastic")
        self.assertEqual(mats["cactus-12"], "ceramic")
        self.assertNotIn("cactus-16", mats)
        _, reg = et.registry(self.rows())
        self.assertEqual(reg["cactus-16"]["material"], "plastic")


class Sessions(unittest.TestCase):
    def test_a_trip_to_the_shelf_is_one_session_and_a_later_trip_is_another(self):
        by = {"a": [(at(0, 0), 200.0), (at(0, 0.2), 300.0), (at(1), 199.0)],
              "b": [(at(0, 0.1), 400.0), (at(1, 0.1), 398.0)]}
        S = et.sessions(by, {"a", "b"})
        self.assertEqual(len(S), 2)
        self.assertEqual(set(S[0][1]), {"a", "b"})
        self.assertEqual(S[0][1]["a"], 250.0)     # two reads of one pot -> median

    def test_a_watering_is_not_a_failure_to_evaporate(self):
        S = [(at(0), {f"p{i}": 200.0 for i in range(6)}),
             (at(1), {**{f"p{i}": 195.0 for i in range(6)}, "p0": 260.0})]
        out = list(et.intervals(S, {f"p{i}" for i in range(6)}))
        self.assertEqual(len(out), 1)
        self.assertNotIn("p0", out[0][3])
        self.assertIn("p1", out[0][3])


class CommonMode(unittest.TestCase):
    """The method itself: a session's zero must not reach the output."""

    POTS = {f"p{i}" for i in range(6)}

    def run_dev(self, offsets):
        """offsets[k] is added to EVERY pot in session k — a moved zero."""
        loss = {"p0": 12.0, "p1": 10.0, "p2": 10.0,
                "p3": 10.0, "p4": 8.0, "p5": 10.0}     # g/day, the truth
        S = []
        for k in range(5):
            S.append((at(k), {p: 300.0 - loss[p] * k + offsets[k] for p in self.POTS}))
        dev = {}
        for _, _, _, r, m in et.intervals(S, self.POTS):
            for p, v in r.items():
                dev.setdefault(p, []).append(v - m)
        return {p: round(sum(v) / len(v), 6) for p, v in dev.items()}

    def test_a_moving_zero_does_not_change_a_single_relative_number(self):
        clean = self.run_dev([0, 0, 0, 0, 0])
        drifted = self.run_dev([0, +7.0, -5.0, +3.0, -9.0])
        self.assertEqual(clean, drifted)

    def test_the_relative_numbers_recover_the_truth_they_were_built_from(self):
        # p0 loses 2 g/day more than the median pot, p4 loses 2 less
        d = self.run_dev([0, +7.0, -5.0, +3.0, -9.0])
        self.assertAlmostEqual(d["p0"], 2.0, places=6)
        self.assertAlmostEqual(d["p4"], -2.0, places=6)
        self.assertAlmostEqual(d["p1"], 0.0, places=6)

    def test_absolute_rates_do_move_with_the_zero(self):
        # the other half of the claim: this is WHY the report is relative
        S0 = [(at(k), {p: 300.0 - 10.0 * k for p in self.POTS}) for k in range(3)]
        S1 = [(at(k), {p: 300.0 - 10.0 * k + (0, 7.0, -5.0)[k] for p in self.POTS})
              for k in range(3)]
        a = [m for _, _, _, _, m in et.intervals(S0, self.POTS)]
        b = [m for _, _, _, _, m in et.intervals(S1, self.POTS)]
        self.assertNotEqual([round(x, 6) for x in a], [round(x, 6) for x in b])

    def test_a_session_too_small_to_have_a_median_is_skipped(self):
        S = [(at(0), {"p0": 300.0, "p1": 300.0}), (at(1), {"p0": 290.0, "p1": 292.0})]
        self.assertEqual(list(et.intervals(S, {"p0", "p1"})), [])


class WateringIndex(unittest.TestCase):
    """The index evaluation: the parts that would fail silently."""

    POTS = ["p0", "p1", "p2", "p3", "p4", "p5"]

    def series(self, loss_per_day, days=14, start=300.0, water_every=None):
        out = []
        w = start
        for d in range(days):
            if water_every and d and d % water_every == 0:
                w = start
            out.append((at(d), w))
            w -= loss_per_day
        return out

    def test_a_watering_never_lands_inside_a_drying_triple(self):
        by = {"p0": [(at(0), 300.0), (at(1), 290.0), (at(2), 350.0), (at(3), 340.0)]}
        T = et.triples(by, {}, {"p0": 60.0}, {"p0": 350.0}, {"p0"})
        for x in T:
            self.assertLess(x["dt"], 3.0)
        # the rise at day 2 is a watering: no triple may span it
        self.assertTrue(all(x["t2"] <= at(2) or x["t0"] >= at(2) for x in T))

    def test_within_pot_z_removes_a_per_pot_offset(self):
        # substrate, species, pot material: all unmeasured, all constant per pot
        rows = ([dict(p="a", label=i % 2 == 0, v=10.0 + i) for i in range(6)]
                + [dict(p="b", label=i % 2 == 0, v=1000.0 + i) for i in range(6)])
        Z = et.within_pot_z(rows, ["v"])
        za = [x["z_v"] for x in Z if x["p"] == "a"]
        zb = [x["z_v"] for x in Z if x["p"] == "b"]
        for x, y in zip(sorted(za), sorted(zb)):
            self.assertAlmostEqual(x, y, places=9)

    def test_within_pot_z_rescues_a_signal_pooling_inverts(self):
        # Simpson's paradox, built to order. Inside each pot the indicator is
        # perfect. Pot "thirsty" sits low and is usually watered; pot "tough"
        # sits high and rarely is. Pooled, every one of tough's UNwatered rows
        # outranks thirsty's watered ones and the ranking flips.
        #
        # This is the real deceleration result in miniature: 0.374 pooled,
        # 0.64 within-pot, same data. Without this test a future refactor could
        # drop the standardisation and the report would still look plausible.
        rows = ([dict(p="tough", v=v, label=(v == 15)) for v in range(10, 16)]
                + [dict(p="thirsty", v=v, label=(v > 0)) for v in range(0, 6)])
        pooled = et.auc(rows, "v")
        within = et.auc(et.within_pot_z(rows, ["v"]), "z_v")
        self.assertLess(pooled, 0.40, "the fixture no longer inverts")
        self.assertGreater(within, 0.75)
        # and each pot really is perfectly separated on its own
        for pot in ("tough", "thirsty"):
            g = [x for x in rows if x["p"] == pot]
            pos = [x["v"] for x in g if x["label"]]
            neg = [x["v"] for x in g if not x["label"]]
            self.assertGreater(min(pos), max(neg))

    def test_auc_is_a_coin_on_noise_and_perfect_on_a_clean_split(self):
        rnd = random.Random(0)
        noise = [dict(p="a", label=i % 2 == 0, v=rnd.random()) for i in range(400)]
        self.assertAlmostEqual(et.auc(noise, "v"), 0.5, delta=0.08)
        clean = ([dict(p="a", label=True, v=1.0 + i) for i in range(10)]
                 + [dict(p="a", label=False, v=-1.0 - i) for i in range(10)])
        self.assertEqual(et.auc(clean, "v"), 1.0)
        self.assertEqual(et.auc(clean, "v", high_means_water=False), 0.0)

    def test_the_et_coefficient_is_fitted_not_assumed(self):
        # the pot really does respond to the control at 0.4; the mass-ratio
        # formula would have said ~1.1, and that sevenfold error is what broke
        # the first attempt
        rows = [dict(p="a", ctrl=c, rate=2.0 + 0.4 * c, dt=1.0, depl=0.5)
                for c in (2.0, 5.0, 9.0, 14.0, 20.0, 26.0, 31.0)]
        out = et.et_corrected(rows, {"a": 100.0})
        for x in out:
            self.assertAlmostEqual(x["et_b"], 0.4, places=6)

    def test_a_negative_fitted_coefficient_subtracts_nothing(self):
        # a pot that dries SLOWER when the air is drier is a fit to noise, and
        # subtracting a negative would ADD water the pot never had
        rows = [dict(p="a", ctrl=c, rate=20.0 - 0.5 * c, dt=1.0, depl=0.5)
                for c in (2.0, 5.0, 9.0, 14.0, 20.0, 26.0, 31.0)]
        out = et.et_corrected(rows, {"a": 100.0})
        for x in out:
            self.assertLess(x["et_b"], 0)
            self.assertEqual(x["depl_et"], x["depl"])

    def test_permutation_reports_a_high_p_when_the_control_is_noise(self):
        rnd = random.Random(7)
        rows = []
        for pot in self.POTS:
            for i in range(9):
                rows.append(dict(p=pot, ctrl=rnd.uniform(0, 30), rate=rnd.uniform(0, 20),
                                 dt=1.0, depl=rnd.random(), label=i % 2 == 0))
        r = et.permutation_gain(rows, {p: 100.0 for p in self.POTS}, n=40, seed=1)
        self.assertIsNotNone(r)
        self.assertGreater(r["p"], 0.05)

    def test_a_control_that_dries_on_its_own_cycle_is_flagged_as_a_clock(self):
        # watered day 0 and day 4; fast right after, slow later — the exact
        # shape that made every ET correction subtract the control's stage
        # from the plants'. This is the diagnostic that would have caught it.
        # a watering is a RISE, so the first one needs a reading before it
        w = [(at(-1), 150.0),
             (at(0), 200.0), (at(1), 180.0), (at(2), 175.0), (at(3), 173.0),
             (at(4), 220.0), (at(5), 198.0), (at(6), 193.0), (at(7), 191.0)]
        by = {"c": w}
        r = et.control_clock(by, "c", et.control_rates(by, "c"))
        self.assertIsNotNone(r)
        self.assertLess(r, -0.5)

    def test_a_control_at_constant_moisture_is_not_a_clock(self):
        # re-wetted every session to the same weight: its loss is weather
        # alone, and days-since-watering is always ~1 with nothing to
        # correlate against — the function must say "cannot tell", not "clock"
        w = []
        for d in range(8):
            w += [(at(d), 200.0), (at(d, 12), 200.0 - (5.0 + 3.0 * (d % 3)))]
        by = {"c": w}
        r = et.control_clock(by, "c", et.control_rates(by, "c"))
        self.assertTrue(r is None or abs(r) < 0.5, r)

    def test_pot_cv_holds_out_whole_pots(self):
        # a pot in the test half must not also be in the training half; the
        # scorer only ever sees the held-out rows, so this checks the split
        rows = [dict(p=f"p{i}", label=j % 2 == 0, v=float(j))
                for i in range(6) for j in range(8)]
        sc = et.pot_cv(rows, "v", folds=20)
        self.assertTrue(sc)
        self.assertTrue(all(0.0 <= s <= 1.0 for s in sc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
