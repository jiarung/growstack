#!/usr/bin/env python3
"""head_datum.py — where this head sits level, kept apart from what 1500 means.

    ./test_head_datum.py

The distinction is the whole point. US_CENTER is 1500 because that is what a
neutral RC pulse means; the datum is which spline tooth somebody pressed the
horn onto. A test suite that let the two be the same number would be testing
the bug.
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "../../tools/s3cam"))
from head_datum import Datum, DEFAULT_US, AXES                     # noqa: E402


class TestProvenance(unittest.TestCase):
    """An unmeasured axis must never look like a measured one."""

    def test_an_unmeasured_axis_is_the_default_and_says_so(self):
        us, measured = Datum().us("pan")
        self.assertEqual(us, DEFAULT_US)
        self.assertFalse(measured)
        self.assertIn("DEFAULT", Datum().describe("pan"))
        self.assertIn("not measured", Datum().describe("pan"))

    def test_a_measured_axis_carries_when_and_why(self):
        d = Datum().set("pan", 1513, note="levelled against the bench edge")
        us, measured = d.us("pan")
        self.assertEqual(us, 1513)
        self.assertTrue(measured)
        self.assertIn("measured", d.describe("pan"))
        self.assertIn("bench edge", d.describe("pan"))

    def test_the_manifest_records_measured_ness_per_axis(self):
        rec = Datum().set("pan", 1513).as_recorded()
        self.assertEqual(rec["pan"], {"us": 1513, "measured": True})
        self.assertEqual(rec["tilt"], {"us": DEFAULT_US, "measured": False})

    def test_a_measured_datum_equal_to_1500_is_still_measured(self):
        # The two concepts coincide numerically sometimes; they are still
        # different claims, and only one of them is evidence.
        d = Datum().set("tilt", 1500, note="genuinely level here")
        self.assertEqual(d.us("tilt"), (1500, True))
        self.assertNotIn("DEFAULT", d.describe("tilt"))


class TestSetting(unittest.TestCase):
    def test_setting_returns_a_new_record(self):
        a = Datum()
        b = a.set("pan", 1513)
        self.assertEqual(a.axes, {})
        self.assertIn("pan", b.axes)

    def test_outside_the_electrical_span_is_refused(self):
        for bad in (599, 2401, 0):
            with self.assertRaises(SystemExit):
                Datum().set("pan", bad)

    def test_an_unknown_axis_is_refused(self):
        with self.assertRaises(SystemExit):
            Datum().set("yaw", 1500)


class TestCliOverride(unittest.TestCase):
    def test_one_axis_named_leaves_the_other_alone(self):
        # Measuring one axis is the normal case. Resetting the other to 1500
        # by omission would undo a measurement without anybody asking.
        base = Datum().set("tilt", 1498, note="measured last week")
        out = Datum.from_cli("pan=1513", base)
        self.assertEqual(out.us("pan"), (1513, True))
        self.assertEqual(out.us("tilt"), (1498, True))

    def test_both_axes(self):
        out = Datum.from_cli("pan=1513,tilt=1498")
        self.assertEqual(out.us("pan")[0], 1513)
        self.assertEqual(out.us("tilt")[0], 1498)

    def test_empty_is_the_base_unchanged(self):
        base = Datum().set("pan", 1513)
        self.assertEqual(Datum.from_cli("", base).us("pan"), (1513, True))
        self.assertEqual(Datum.from_cli(None, base).us("pan"), (1513, True))

    def test_garbage_is_refused(self):
        for bad in ("pan", "pan=abc", "=1513", "pan:1513"):
            with self.assertRaises(SystemExit):
                Datum.from_cli(bad)

    def test_an_out_of_span_override_is_refused(self):
        with self.assertRaises(SystemExit):
            Datum.from_cli("pan=9000")


class TestPersistence(unittest.TestCase):
    def test_round_trip(self):
        d = Datum().set("pan", 1513, note="bench edge")
        with tempfile.TemporaryDirectory() as t:
            p = d.save(os.path.join(t, "head.json"))
            back = Datum.load(p)
        self.assertEqual(back.us("pan"), (1513, True))
        self.assertEqual(back.axes["pan"]["note"], "bench edge")

    def test_a_missing_file_is_all_defaults_not_an_error(self):
        with tempfile.TemporaryDirectory() as t:
            d = Datum.load(os.path.join(t, "nope.json"))
        for a in AXES:
            self.assertEqual(d.us(a), (DEFAULT_US, False))

    def test_a_future_version_is_refused_rather_than_guessed_at(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "head.json")
            with open(p, "w") as fh:
                json.dump({"version": 99, "axes": {}}, fh)
            with self.assertRaises(SystemExit):
                Datum.load(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
