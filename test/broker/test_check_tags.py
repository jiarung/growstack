#!/usr/bin/env python3
"""check-tags.py's one piece of logic: were two uids ever on the bench together?

    ./test_check_tags.py

A retag puts the old tag away before the new one is scanned, so the two uids
never share a session. Two pots wrongly bound to one id are scanned minutes
apart. That difference is the whole test, and it is the difference between
"remove a line from tag-map" and "a plant has been mislabeled for days".
"""
import datetime, importlib.util, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "ct", os.path.join(HERE, "../../broker/check-tags.py"))
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

T0 = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)
def at(d, m=0):
    return T0 + datetime.timedelta(days=d, minutes=m)


class Together(unittest.TestCase):
    def test_a_retag_never_shares_a_session(self):
        old = [(at(0), "p"), (at(1), "p")]          # last scanned day 1
        new = [(at(2), "p"), (at(3), "p")]          # first scanned day 2
        self.assertFalse(ct.together(old, new, at(-10)))

    def test_two_pots_on_one_id_are_minutes_apart(self):
        # the 2026-10-03 shape: 5337 at 09:43, 5330 at 09:45, both "cactus-30"
        a = [(at(2, 0), "cactus-30")]
        b = [(at(2, 2), "cactus-30")]
        self.assertTrue(ct.together(a, b, at(-10)))

    def test_the_window_excludes_old_coincidences(self):
        # they WERE together once, long ago — a retag done badly and then
        # fixed; what matters is whether it is happening now
        a = [(at(-30, 0), "p"), (at(5), "p")]
        b = [(at(-30, 1), "p")]
        self.assertFalse(ct.together(a, b, at(-7)))
        self.assertTrue(ct.together(a, b, at(-60)))

    def test_an_hour_is_the_session_boundary(self):
        a = [(at(0, 0), "p")]
        self.assertTrue(ct.together(a, [(at(0, 59), "p")], at(-1)))
        self.assertFalse(ct.together(a, [(at(0, 61), "p")], at(-1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
