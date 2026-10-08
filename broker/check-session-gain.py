#!/usr/bin/env python3
"""Did the scale's GAIN move between sessions? Zero drift adds the same grams
to every pot; gain drift multiplies, so it shows up as a change proportional
to pot weight — small pots barely move, big pots cross the 10 g watering
threshold, and the dashboard reports waterings that never happened.

    ./check-session-gain.py            # last 14 days, flag |gain| > 2 %
    ./check-session-gain.py --days 30

2026-10-07: one session read +5.3 % (r = 0.97 across 30 pots), five big pots
were "watered", the next session read −5.6 % and they were "dry" again. The
dead-weight reference tag (cactus-water-ref) that would have shown +5 % on
its own had never been weighed. This is the after-the-fact check; weighing
the reference every session is the one that catches it before it is a chart.

Per session: for every pot weighed in it AND in the previous session, Δ =
this − previous, regressed on this session's W. Drying subtracts a few grams
from every pot and produces no slope. A gain error produces one. So does
watering only the big pots — which this gardener does — and on one session's
numbers the two are indistinguishable. What tells them apart is the NEXT
session: water stays, a gain error reverses. A flagged session followed by
one with the opposite slope is a gain error; a flagged session whose small
pots then dry normally was a watering. The script prints both sessions'
slopes so that comparison is in front of you; it does not decide for you.
"""
import argparse, collections, datetime, importlib.util, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("et", os.path.join(HERE, "analyze-et.py"))
et = importlib.util.module_from_spec(spec)
spec.loader.exec_module(et)

GAIN_FLAG = 0.02      # 2 % — the 10-07 event was 5 %; scale noise is well under 1 %
R_FLAG = 0.80         # the slope must EXPLAIN the spread, not just exist
MIN_POTS = 8


def sessions(rows, gap_s=3600):
    rows = sorted(rows)
    out, cur = [], [rows[0]]
    for r in rows[1:]:
        if (r[0] - cur[-1][0]).total_seconds() > gap_s:
            out.append(cur); cur = [r]
        else:
            cur.append(r)
    out.append(cur)
    return [(s[0][0], {p: v for _, p, v, *_ in s}) for s in out]   # last reading per pot wins


def gain(prev, cur):
    """(slope, r, n) of Δ on W across pots in both sessions, or None."""
    pts = [(cur[p], cur[p] - prev[p]) for p in cur if p in prev]
    if len(pts) < MIN_POTS:
        return None
    xs, ys = zip(*pts)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / sxx, sxy / (sxx * syy) ** 0.5, len(pts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    a = ap.parse_args()
    rows = [r for r in et.query() if r[1] != "unknown" and not r[1].startswith("cactus-water")]
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=a.days)
    rows = [r for r in rows if r[0] >= since]
    if not rows:
        print("no readings"); return 0
    S = sessions(rows)
    tz = datetime.timezone(datetime.timedelta(hours=8))
    print(f"{'session (local)':17s} {'pots':>4s} {'gain':>7s} {'r':>6s}")
    bad = 0
    for (t0, prev), (t1, cur) in zip(S, S[1:]):
        g = gain(prev, cur)
        if not g:
            print(f"{t1.astimezone(tz):%m-%d %H:%M}       {len(cur):4d}   (too few shared pots)"); continue
        slope, r, n = g
        flag = abs(slope) > GAIN_FLAG and abs(r) > R_FLAG
        bad += flag
        print(f"{t1.astimezone(tz):%m-%d %H:%M}       {n:4d} {100*slope:+6.1f}% {r:+6.2f}"
              + ("   <- slope: gain error OR big pots watered" if flag else ""))
    if bad:
        print(f"\n{bad} flagged. A slope that REVERSES in the next session (+5 % then −5 %)\n"
              "is a gain error — water does not un-happen — and that session should be\n"
              "marked suspect (mark-weight.sh --from/--to … suspect). A slope the next\n"
              "session does not reverse was most likely a watering of the big pots.\n"
              "The dead-weight reference (cactus-water-ref) answers this on the spot:\n"
              "it has no plant, so any change in it is the scale.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
