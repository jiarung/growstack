#!/usr/bin/env python3
"""Every dashboard query that differences plant_weight must keep() FIRST.

    ./test_panel_keep_order.py

A pot whose rows carry two column sets — the `topic` tag on some, absent on
others, which mark-weight.sh left behind until 2026-10-04 — makes Flux's
difference() panic inside InfluxDB rather than return an error, and the whole
dashboard answers 500. keep(columns: ["_time","_value","plant_id"]) placed
BEFORE the difference() removes the hazard; placed after, it removes nothing.
Panels 9 and 11 had none and died; panels 2 and 7 had one twenty lines too
late and were one interleaved rewrite from the same fate.

The first audit searched for the keep() string anywhere in the query and was
fooled twice — by a late keep(), and by the word difference() in a comment.
This one strips comments and compares positions. It is the test the incident
write-up claims exists.
"""
import glob, json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(HERE, "../../broker/grafana/provisioning/dashboards")
KEEP = 'keep(columns: ["_time", "_value", "plant_id"])'


def code_only(q):
    return "\n".join(l for l in q.split("\n") if not l.strip().startswith("//"))


def plant_weight_targets():
    for f in sorted(glob.glob(os.path.join(DASH, "*.json"))):
        with open(f) as fh:
            d = json.load(fh)
        for p in d.get("panels", []):
            for i, t in enumerate(p.get("targets", [])):
                q = t.get("query", "")
                if "plant_weight" in q:
                    yield os.path.basename(f), p.get("id"), i, q


class KeepBeforeDifference(unittest.TestCase):
    def test_every_differencing_query_keeps_first(self):
        seen, bad = 0, []
        for f, pid, i, q in plant_weight_targets():
            c = code_only(q)
            if "difference(" not in c:
                continue
            seen += 1
            k, d = c.find(KEEP), c.find("difference(")
            if not (0 <= k < d):
                bad.append(f"{f} panel {pid} target {i}: "
                           + ("no keep()" if k < 0 else "keep() after difference()"))
        self.assertGreaterEqual(seen, 7, "the scan found fewer differencing queries than exist")
        self.assertEqual(bad, [], "\n".join(bad))

    def test_the_scan_is_not_fooled_by_comments(self):
        q = "// difference() mentioned here\n|> filter()\n" + KEEP + "\n|> difference()"
        c = code_only(q)
        self.assertLess(c.find(KEEP), c.find("difference("))
        # and a keep() AFTER the difference is caught, not credited
        q2 = "|> difference()\n" + KEEP
        c2 = code_only(q2)
        self.assertGreater(c2.find(KEEP), c2.find("difference("))


if __name__ == "__main__":
    unittest.main(verbosity=2)
