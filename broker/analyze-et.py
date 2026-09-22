#!/usr/bin/env python3
"""Does a planted pot lose water differently from bare soil beside it?

    ./analyze-et.py                 # the report
    ./analyze-et.py --band 185,245  # a different size cohort
    ./analyze-et.py --csv out.csv   # the per-interval rows behind it

Three things this exists to stop happening again, each of which produced a
wrong answer on 2026-09-19 before it was written down:

1. IDENTITY. A repot does not move a pot's weight, it RETIRES a plant_id and
   starts a new one (cactus-05 -> cactus-05b). So a detector hunting weight
   steps inside one id finds nothing and reports "no repot in the record",
   and an analysis keyed on ids silently compares a pot to its own predecessor.
   Lineage is derived here, and a retired id never enters a cohort.

2. QUALITY. `quality == "ok"` is not optional. The station already labels a
   scanned tag whose pot never reached the plate (`empty`) and anything a human
   struck out (`deleted`); reading the raw series instead re-discovers those as
   "negative weights" and, worse, lets a median window straddle them and invent
   a repot that never happened.

3. THE ZERO. Between sessions the scale's zero moves by a few grams — additive,
   and independent of what is on it (measured: corr(gain, pot mass) = -0.03).
   A single day's evaporation is about the same size, so absolute rates have a
   signal-to-noise ratio near 1 and no amount of averaging fixes it. But 49 of
   59 sessions weighed 8+ pots at once, and the offset is common to all of them,
   so `rate_i - median_j(rate_j)` cancels it exactly. That is why every number
   below is RELATIVE to the cohort: the absolute ones are not trustworthy and
   the relative ones are (measured: 12x less scatter).

What it cannot do: bare-soil evaporation is not one number. It is highest a day
after watering and falls below the planted pots within three, so a mean over
mixed drying stages averages two opposite signs into nothing. The report
stratifies by days since the control was last watered for that reason, and with
four intervals that stratification is a shape, not yet a model.
"""
import argparse, collections, datetime, json, os, random, re, statistics, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_GAP_S = 3600      # weighings closer than this are one trip to the shelf
JUMP_G = 10.0             # a rise this big is a watering, not a failure to evaporate
MAX_STEP_G = 60.0         # beyond this a "drying interval" is some other event
MIN_COHORT = 5            # a session median needs a cohort to be a median of

FLUX = '''from(bucket: "sensors")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "plant_weight" and r.quality == "ok"
                       and (r._field == "weight_g" or r._field == "uid"))
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "weight_g", "uid", "plant_id"])
  |> sort(columns: ["_time"])'''


def _iso(t):
    """Influx gives 9 fractional digits; fromisoformat before 3.11 takes 3 or 6.
    Cron runs /usr/bin/python3 (3.8) while the shell has 3.11, so this crashed
    ONLY under cron after a manual run had proved it fine — the exact trap
    compute-k-models.py:_iso documents. Truncate to microseconds."""
    t = t.replace("Z", "+00:00")
    m = re.match(r"^(.*?)\.(\d+)(.*)$", t)
    if m:
        t = f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}{m.group(3)}"
    return datetime.datetime.fromisoformat(t)


def query():
    out = subprocess.run(
        ["docker", "exec", "-i", "monitor-air-influxdb", "influx", "query",
         "--org", "monitor-air", "--raw", "-f", "/dev/stdin"],
        input=FLUX, capture_output=True, text=True, check=True).stdout
    import csv, io
    hdr, rows = None, []
    for r in csv.reader(io.StringIO(out)):
        if not r:
            continue
        if len(r) > 1 and r[1] == "result":
            hdr = r
            continue
        if hdr and r[0] == "" and len(r) == len(hdr):
            d = dict(zip(hdr, r))
            if not d.get("weight_g"):
                continue
            rows.append((_iso(d["_time"]), d["plant_id"], float(d["weight_g"]),
                         d.get("uid", "")))
    if not rows:
        sys.exit("no plant_weight rows — is the stack up?")
    return rows


def ended_ids():
    """The dashboard owns this list; a second copy here would be a second truth."""
    p = os.path.join(HERE, "grafana/provisioning/dashboards/daily.json")
    with open(p) as f:
        q = next(x for x in json.load(f)["panels"] if x["id"] == 10)["targets"][0]["query"]
    m = re.search(r'ended\s*=\s*\[([^\]]*)\]', q)
    return set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()


def materials():
    with open(os.path.join(HERE, "pot-materials.json")) as f:
        d = json.load(f)
    return d["materials"], d.get("default", "plastic")


def registry(rows):
    by = collections.defaultdict(list)
    for t, p, v, _ in rows:
        by[p].append((t, v))
    for s in by.values():
        s.sort()
    mats, dflt = materials()
    ended, now = ended_ids(), max(t for t, _, _, _ in rows)
    # Tag ownership, the same derivation panel 10 calls `live`: one tag is on
    # one pot, so whoever holds a uid's most recent reading is that tag's
    # current owner. A pot that owns no tag at all has been ended — either by a
    # repot (its tag moved to the successor) or, since 2026-09-21, by the tag
    # being reused on a new plant (cactus-13-2's went to cactus-29). The latter
    # has no successor id and is not on `ended`, so without this rule it reads
    # as live and gets a plateau curve fitted to a plant that is gone.
    latest = {}
    for t, p, _, uid in sorted(rows):
        if uid:
            latest[uid] = p
    owns_a_tag = set(latest.values())
    # cactus-05b succeeds cactus-05: same plant, different pot, never pooled
    succ = {p: m.group(1) for p in by
            if (m := re.fullmatch(r"(.+?)[a-z]", p)) and m.group(1) in by}
    superseded = set(succ.values())      # ids a repot has handed on
    reg = {}
    for p, s in by.items():
        if p == "unknown":
            continue
        reg[p] = dict(
            n=len(s), median_g=statistics.median(v for _, v in s),
            recent_g=statistics.median([v for t, v in s if (now - t).days <= 30]
                                       or [v for _, v in s]),
            first=s[0][0], last=s[-1][0], succeeds=succ.get(p),
            material=mats.get(p, dflt),
            # Retirement is DERIVED: a successor owns the plant now, or a human
            # put it on the ended list. Staleness alone is a pot nobody weighed
            # this week, which is not the same thing at all.
            retired=(p in superseded) or (p in ended) or (p not in owns_a_tag),
            # WHY it is retired, recorded rather than re-derived by the caller:
            # a successor inherited the plant, or a human declared it ended.
            # The two look identical from the outside and read very differently.
            retired_why=("repotted — a successor id took over" if p in superseded
                         else "ended — rotted or removed, no successor"
                         if p in ended
                         else "ended — its tag now belongs to another plant"
                         if p not in owns_a_tag else None))
    return by, reg


def sessions(by, keep):
    ev = sorted((t, p, v) for p, s in by.items() if p in keep for t, v in s)
    out, cur = [], [ev[0]]
    for e in ev[1:]:
        if (e[0] - cur[-1][0]).total_seconds() <= SESSION_GAP_S:
            cur.append(e)
        else:
            out.append(cur)
            cur = [e]
    out.append(cur)
    res = []
    for s in out:
        d = collections.defaultdict(list)
        for t, p, v in s:
            d[p].append(v)
        res.append((sorted(t for t, _, _ in s)[len(s) // 2],
                    {p: statistics.median(v) for p, v in d.items()}))
    return sorted(res)


def intervals(S, cohort):
    """Per session pair: each pot's loss rate, and the cohort median that cancels
    the session zero. Waterings and step changes are not evaporation."""
    for (t0, a), (t1, b) in zip(S, S[1:]):
        dt = (t1 - t0).total_seconds() / 86400
        if not 0.25 <= dt <= 8.0:
            continue
        r = {p: (a[p] - b[p]) / dt for p in a.keys() & b.keys() & cohort
             if b[p] - a[p] <= JUMP_G and abs(b[p] - a[p]) <= MAX_STEP_G}
        if len(r) >= MIN_COHORT:
            yield t0, t1, dt, r, statistics.median(r.values())


# ---------------------------------------------------------------- watering index
#
# A second question, on the same data: of the things we could put on the OLED,
# which one best says "this pot wants water"? Scored against the only ground
# truth there is — whether the gardener watered it within WATER_WINDOW_H of the
# reading. That is a proxy for their judgement, not for the plant's need, so it
# ranks indicators against each other and must not be read as "the indicator is
# correct". An indicator that merely reproduced the calendar would score well
# here too, which is why every table below is also cut by days since watering:
# the calendar's own score collapses past three days (it is worse than chance
# there), and that is exactly where a real indicator has to earn its place.

WATER_WINDOW_H = 36       # "watered soon after this reading"
MIN_RATE_D = 0.4          # shorter than this and a rate is mostly scale noise
MAX_RATE_D = 4.0          # longer and the drying stage has moved on mid-interval


def triples(by, reg, span, sat, cohort):
    """Three consecutive weighings with no watering between, per pot.

    Three, not two, because a rate needs two and a change in rate needs three.
    The anchor is the first reading after the most recent watering — the same
    definition the OLED and panel 10 use, so the numbers here are the ones the
    gardener actually sees.
    """
    out = []
    for p in cohort:
        if span.get(p, 0) <= 5:
            continue
        s = sorted(by[p])
        wet = [t for (t0, v0), (t, v) in zip(s, s[1:]) if v - v0 > JUMP_G]
        anchors = []
        for t in wet:
            after = [(tt, vv) for tt, vv in s if tt >= t]
            if after:
                anchors.append((t, after[0][1]))
        for (t0, v0), (t1, v1), (t2, v2) in zip(s, s[1:], s[2:]):
            d1 = (t1 - t0).total_seconds() / 86400
            d2 = (t2 - t1).total_seconds() / 86400
            if not (MIN_RATE_D <= d1 <= MAX_RATE_D and MIN_RATE_D <= d2 <= MAX_RATE_D):
                continue
            if v1 - v0 > JUMP_G or v2 - v1 > JUMP_G:
                continue
            if abs(v1 - v0) > MAX_STEP_G or abs(v2 - v1) > MAX_STEP_G:
                continue
            prev = [(t, w) for t, w in anchors if t <= t2]
            if not prev:
                continue
            at, aw = prev[-1]
            nxt = [t for t in wet if t > t2]
            r1, r2 = (v0 - v1) / d1, (v1 - v2) / d2
            out.append(dict(
                p=p, t0=t0, t2=t2, dt=d1 + d2,
                age=(t2 - at).total_seconds() / 86400,
                depl=(sat[p] - v2) / span[p],       # panel 10's number
                frac=(aw - v2) / aw,                # loss over weight when watered
                rate=(v0 - v2) / (d1 + d2), dec=r1 - r2,
                label=bool(nxt and (nxt[0] - t2).total_seconds() <= WATER_WINDOW_H * 3600)))
    return out


def within_pot_z(rows, keys, min_n=3):
    """Standardise each indicator inside its own pot.

    This is the single highest-value step in the whole file, and it is free.
    Substrate mix, species, pot material, water-holding capacity — every one of
    them is a per-pot CONSTANT, and every one of them is unmeasured. Pooling
    pots compares a pot to other pots; z-scoring inside a pot compares a pot to
    ITSELF, and the constants divide out without ever being known.

    It is not cosmetic. Pooled, deceleration scores 0.374 — apparently inverted
    — while inside a pot it is 0.64 and points the same way in 16 of 17 pots.
    That reversal is Simpson's paradox, and the pooled number is the wrong one.
    """
    byp = collections.defaultdict(list)
    for x in rows:
        byp[x["p"]].append(x)
    out = []
    for g in byp.values():
        stat = {}
        for k in keys:
            v = [x[k] for x in g if x.get(k) is not None]
            if len(v) >= min_n:
                stat[k] = (statistics.mean(v), statistics.pstdev(v) or 1.0)
        for x in g:
            y = dict(x)
            for k, (m, sd) in stat.items():
                y["z_" + k] = (x[k] - m) / sd if x.get(k) is not None else None
            out.append(y)
    return out


def auc(rows, key, high_means_water=True):
    """P(a watered reading scores above an unwatered one). 0.5 is a coin."""
    pos = [x[key] for x in rows if x["label"] and x.get(key) is not None]
    neg = [x[key] for x in rows if not x["label"] and x.get(key) is not None]
    if len(pos) < 5 or len(neg) < 5:
        return None
    v = sum((1 if a > b else 0.5 if a == b else 0)
            for a in pos for b in neg) / (len(pos) * len(neg))
    return v if high_means_water else 1 - v


def pot_cv(rows, key, high=True, folds=200, seed=42):
    """AUC on held-out POTS. Samples from one pot are not independent, so a
    random split leaks: the same pot's other readings sit in the training half
    and the score flatters an indicator that has merely learned that pot."""
    rnd = random.Random(seed)
    pots = sorted({x["p"] for x in rows})
    if len(pots) < 4:
        return []
    sc = []
    for _ in range(folds):
        te = set(rnd.sample(pots, max(2, len(pots) // 3)))
        v = auc([x for x in rows if x["p"] in te], key, high)
        if v is not None:
            sc.append(v)
    return sc


def control_rates(by, ctrl):
    """The bare-soil pot's own drying rate, per interval it was weighed over."""
    s = sorted(by[ctrl])
    out = []
    for (t0, v0), (t1, v1) in zip(s, s[1:]):
        dt = (t1 - t0).total_seconds() / 86400
        if dt < 0.25 or v1 - v0 > JUMP_G or abs(v1 - v0) > MAX_STEP_G:
            continue
        out.append((t0, t1, (v0 - v1) / dt))
    return out


def overlap_rate(cr, t0, t1):
    """The control's rate over a window, weighted by how much of it overlaps."""
    num = den = 0.0
    for c0, c1, r in cr:
        ov = (min(t1, c1) - max(t0, c0)).total_seconds()
        if ov > 0:
            num += r * ov
            den += ov
    return num / den if den > 0 else None


def control_clock(by, ctrl, cr):
    """corr(control rate, days since the control itself was watered).

    Near zero means the control's rate is driven by something other than its
    own drying stage — weather, which is what a control is for. Strongly
    negative means it is a clock: fast right after watering, slow three days
    later, exactly like the plants. Subtracting a clock from the plants'
    depletion subtracts one drying stage from another; measured -0.69 on
    2026-09-22, and every form of the ET correction made the index worse.
    None when there are too few intervals to say.
    """
    cs = sorted(by[ctrl])
    cwet = [t for (t0, v0), (t, v) in zip(cs, cs[1:]) if v - v0 > JUMP_G]
    pairs = []
    for t0, t1, r in cr:
        prev = [t for t in cwet if t <= t1]
        if prev:
            pairs.append((r, (t1 - prev[-1]).total_seconds() / 86400))
    if len(pairs) < 4:
        return None
    mr = statistics.mean(r for r, _ in pairs)
    md = statistics.mean(d for _, d in pairs)
    sr = sum((r - mr) ** 2 for r, _ in pairs) ** 0.5
    sd = sum((d - md) ** 2 for _, d in pairs) ** 0.5
    return sum((r - mr) * (d - md) for r, d in pairs) / (sr * sd) if sr and sd else None


def et_corrected(rows, span, key="ctrl", min_fit=5):
    """Subtract the bare-soil evaporation this pot's own history says it gets.

    The coefficient is FITTED per pot, never assumed. A first attempt scaled the
    control by (mass ratio)^(2/3) — the area-to-volume argument — and made the
    index dramatically worse. The fitted coefficients have a median of 0.16
    against that formula's 1.13: the form is right (they correlate at +0.64) and
    the scale was wrong by about sevenfold, so the correction subtracted seven
    times too much. Fitting removes the chance to make that mistake again.

    Leave-one-out: a sample never contributes to the coefficient used on it.
    That is not the same as validating forward in time, which the control pot
    does not yet have the history for — see the caveat the report prints.
    """
    byp = collections.defaultdict(list)
    for x in rows:
        byp[x["p"]].append(x)
    for g in byp.values():
        for i, x in enumerate(g):
            oth = [y for j, y in enumerate(g) if j != i and y.get(key) is not None]
            x["et_b"] = x["depl_et"] = None
            if len(oth) < min_fit or x.get(key) is None:
                continue
            xs = [y[key] for y in oth]
            ys = [y["rate"] for y in oth]
            mx = statistics.mean(xs)
            var = sum((v - mx) ** 2 for v in xs)
            if var < 1e-9:
                continue
            my = statistics.mean(ys)
            b = sum((v - mx) * (w - my) for v, w in zip(xs, ys)) / var
            # a negative coefficient would mean the pot dries SLOWER when the
            # weather is drier; that is not a physical pot, it is a fit to noise
            soil = max(0.0, x[key]) * max(0.0, b) * x["dt"]
            x["et_b"] = b
            x["depl_et"] = x["depl"] - soil / span[x["p"]]
    return rows


def permutation_gain(rows, span, n=200, seed=42):
    """Is the ET correction's gain bigger than shuffling the control gives?

    The control takes only a handful of distinct values, so a coefficient fitted
    against it can improve an index by chance alone. Shuffling which value
    belongs to which moment destroys the timing while keeping the distribution:
    whatever gain survives that is not about evaporation."""
    rnd = random.Random(seed)
    vals = sorted({round(x["ctrl"], 3) for x in rows if x.get("ctrl") is not None})

    def score(mapper):
        w = [dict(x, ctrl=mapper(x)) for x in rows]
        Z = within_pot_z(et_corrected(w, span), ["depl", "depl_et"])
        Z = [x for x in Z if x.get("z_depl") is not None and x.get("z_depl_et") is not None]
        a, b = auc(Z, "z_depl"), auc(Z, "z_depl_et")
        return (b - a, a, b, len(Z)) if (a and b) else (None, a, b, len(Z))

    obs, base, corr_, n_used = score(lambda x: x["ctrl"])
    if obs is None:
        return None
    null = []
    for _ in range(n):
        perm = vals[:]
        rnd.shuffle(perm)
        m = dict(zip(vals, perm))
        g, _, _, _ = score(lambda x: m[round(x["ctrl"], 3)])
        if g is not None:
            null.append(g)
    p = (sum(1 for g in null if g >= obs) + 1) / (len(null) + 1)
    return dict(base=base, corrected=corr_, gain=obs, p=p, n=n_used, null=null)


def boot(x, B=4000, seed=42):
    rnd = random.Random(seed)
    s = sorted(statistics.mean(rnd.choices(x, k=len(x))) for _ in range(B))
    return s[int(.025 * B)], s[int(.975 * B)]


def index_report(by, reg, cohort, ctrl, args):
    # the same definitions panel 10 uses: sat is the pot's wettest observed
    # weight, span the largest drop it has ever recovered from
    sat = {p: max(v for _, v in by[p]) for p in cohort}
    span = {p: sat[p] - min(v for _, v in by[p]) for p in cohort}
    T = triples(by, reg, span, sat, cohort - {ctrl})
    if len(T) < 40:
        sys.exit(f"only {len(T)} usable triples — widen --band")
    pots = {x["p"] for x in T}
    print(f"{len(T)} triples from {len(pots)} pots, "
          f"{sum(x['label'] for x in T)} followed by watering within "
          f"{WATER_WINDOW_H} h\n")

    Z = within_pot_z(T, ["depl", "frac", "rate", "dec", "age"])
    IND = [("depl", True, "loss / span"),
           ("frac", True, "loss / weight when watered"),
           ("age", True, "days since watering"),
           ("rate", False, "drying rate g/day"),
           ("dec", False, "deceleration g/day2")]

    print("A. pooled across pots vs standardised inside each pot")
    print(f"   {'indicator':28s} {'pooled':>7s} {'within':>7s}")
    for k, hi, lab in IND:
        pooled, within = auc(T, k, hi), auc(Z, "z_" + k, hi)
        f = lambda v: f"{v:7.3f}" if v is not None else f"{'-':>7s}"
        print(f"   {lab:28s} {f(pooled)} {f(within)}")
    print("   within-pot divides out substrate, species, pot and capacity —\n"
          "   all unmeasured, all constant per pot. Pooled numbers can invert.")

    print("\nB. held-out pots (200 folds, median and IQR)")
    print(f"   {'indicator':28s} {'median':>7s} {'IQR':>15s}")
    for k, hi, lab in IND:
        sc = pot_cv(Z, "z_" + k, hi)
        if not sc:
            continue
        q = statistics.quantiles(sc, n=4)
        print(f"   {lab:28s} {statistics.median(sc):7.3f} {q[0]:6.3f}-{q[2]:<6.3f}")

    print("\nC. by days since watering — where the calendar stops working")
    cuts = [(1, 2), (2, 3), (3, 5)]
    print(f"   {'indicator':28s} " + " ".join(f"{f'{l}-{h}d':>7s}" for l, h in cuts))
    for k, hi, lab in IND:
        cells = []
        for lo, high in cuts:
            v = auc([x for x in Z if lo <= x["age"] < high], "z_" + k, hi)
            cells.append(f"{v:7.3f}" if v is not None else f"{'-':>7s}")
        print(f"   {lab:28s} " + " ".join(cells))

    # ---- circularity ----------------------------------------------------------
    # The label is the gardener's decision, and since 2026-08-18 the OLED has
    # shown the gardener loss/span. An indicator the gardener can see partly
    # predicts the gardener. The only non-circular window is before that date;
    # it is small, so this is a bound on the effect, not a measurement of it.
    OLED_SHOWS_DEPL = datetime.datetime(2026, 8, 18, tzinfo=datetime.timezone.utc)
    pre = [x for x in Z if x["t2"] < OLED_SHOWS_DEPL]
    post = [x for x in Z if x["t2"] >= OLED_SHOWS_DEPL]
    print(f"\nC2. circularity — the gardener has seen loss/span on the OLED since "
          f"{OLED_SHOWS_DEPL:%Y-%m-%d}")
    print(f"   {'indicator':28s} {'before':>7s} {'after':>7s}   (before: n={len(pre)}, "
          f"{sum(x['label'] for x in pre)} watered)")
    for k, hi, lab in IND:
        a_, b_ = auc(pre, "z_" + k, hi), auc(post, "z_" + k, hi)
        f = lambda v: f"{v:7.3f}" if v is not None else f"{'-':>7s}"
        print(f"   {lab:28s} {f(a_)} {f(b_)}")
    print("   'days since watering' is the control here: visible in both periods.\n"
          "   A gap on loss/span that the calendar does not share is circularity.")

    # ---- the bare-soil correction -------------------------------------------
    cr = control_rates(by, ctrl)
    for x in T:
        x["ctrl"] = overlap_rate(cr, x["t0"], x["t2"])
    W = [x for x in T if x["ctrl"] is not None]
    print(f"\nD. subtracting bare-soil evaporation")
    print(f"   control {ctrl}: {len(cr)} intervals, "
          f"{len(W)}/{len(T)} triples covered ({100*len(W)//max(len(T),1)}%)")
    if len(W) < 40:
        print("   too little overlap to score — the control needs more history.")
        return 0

    # Is the control's rate weather, or its OWN drying stage? A control pot that
    # is watered and left to dry evaporates fast the day after and barely at all
    # three days on, exactly like the plants — so its rate is a clock, and the
    # correction below subtracts the control's clock from each plant's. They are
    # different clocks. Measured 2026-09-22: r = -0.69 against days since the
    # control was last watered, which is why every form of the correction made
    # the index worse. A control whose rate IS weather has to be held at constant
    # moisture — re-wetted to a fixed weight every session — and this line is
    # how you would know that had been done.
    clock = control_clock(by, ctrl, cr)
    # And is that clock the PLANTS' clock? If the control were watered with the
    # batch, its stage would be every plant's stage and the correction would be
    # subtracting the label. Measured +0.04 on 2026-09-22: it is not — the
    # control runs on its own schedule, so what gets subtracted is an
    # independent clock, which for any plant is noise. Both facts are needed:
    # the first says the correction is wrong, the second says how.
    cs = sorted(by[ctrl])
    cwet = [t for (t0, v0), (t, v) in zip(cs, cs[1:]) if v - v0 > JUMP_G]
    ages = []
    for x in W:
        prev = [t for t in cwet if t <= x["t2"]]
        if prev:
            ages.append(((x["t2"] - prev[-1]).total_seconds() / 86400, x["age"]))
    shared = None
    if len(ages) >= 10:
        ma = statistics.mean(a for a, _ in ages)
        mb = statistics.mean(b for _, b in ages)
        sa = sum((a - ma) ** 2 for a, _ in ages) ** 0.5
        sb = sum((b - mb) ** 2 for _, b in ages) ** 0.5
        if sa and sb:
            shared = sum((a - ma) * (b - mb) for a, b in ages) / (sa * sb)
    if clock is not None:
        print(f"   control rate vs its OWN days-since-watering: r = {clock:+.2f} (n={len(cr)})"
              + ("   <- a clock, not a weather gauge" if abs(clock) > 0.5 else ""))
    if shared is not None:
        print(f"   control's clock vs the plants' clock:         r = {shared:+.2f} (n={len(ages)})"
              + ("   <- shared: the correction subtracts the LABEL"
                 if shared > 0.5 else
                 "   <- independent: the correction subtracts NOISE"
                 if clock is not None and abs(clock) > 0.5 else ""))
    if clock is not None and abs(clock) > 0.5:
        print("   Hold the control at constant moisture (weigh, refill to a fixed\n"
              "   target, weigh again) before trusting anything below.")

    E = within_pot_z(et_corrected(W, span), ["depl", "depl_et"])
    E = [x for x in E if x.get("z_depl") is not None and x.get("z_depl_et") is not None]
    base, corr_ = auc(E, "z_depl"), auc(E, "z_depl_et")
    print(f"   {'loss / span':28s} {base:7.3f}")
    print(f"   {'  minus bare-soil ET':28s} {corr_:7.3f}   ({corr_-base:+.3f})")
    print(f"   {'':28s} " + " ".join(f"{f'{l}-{h}d':>7s}" for l, h in cuts))
    for k, lab in (("z_depl", "loss / span"), ("z_depl_et", "  minus bare-soil ET")):
        cells = []
        for lo, high in cuts:
            v = auc([x for x in E if lo <= x["age"] < high], k)
            cells.append(f"{v:7.3f}" if v is not None else f"{'-':>7s}")
        print(f"   {lab:28s} " + " ".join(cells))

    bs = collections.defaultdict(list)
    for x in E:
        if x.get("et_b") is not None:
            bs[x["p"]].append(x["et_b"])
    if bs:
        print(f"\n   fitted ET coefficient per pot (median of the leave-one-out fits).")
        print(f"   'area' is the (mass ratio)^2/3 guess that was wrong by ~7x —\n"
              f"   shown only so the gap stays visible.")
        print(f"     {'plant_id':32s} {'b':>6s} {'area':>6s} {'mass':>7s}")
        for p_, v in sorted(bs.items(), key=lambda kv: -statistics.median(kv[1])):
            mass = reg[p_]["recent_g"]
            print(f"     {p_:32s} {statistics.median(v):+6.2f} "
                  f"{(mass/reg[ctrl]['recent_g'])**(2/3):6.2f} {mass:6.0f} g")

    if args.perm:
        r = permutation_gain(W, span, n=args.perm)
        if r:
            print(f"\n   permutation test on the gain ({args.perm} shuffles of the\n"
                  f"   control's timing, distribution kept):")
            print(f"     observed {r['gain']:+.3f}   null median "
                  f"{statistics.median(r['null']):+.3f}   p = {r['p']:.3f}")
            if r["p"] > 0.01:
                print("     NOT established. Treat the gain as a lead, not a result.")

    span_d = (max(t for _, t, _ in cr) - min(t for t, _, _ in cr)).days if cr else 0
    print(f"\n   CAVEAT: the control has {span_d} days of history, so a coefficient\n"
          f"   cannot yet be fitted on the past and tested on the future — which\n"
          f"   is how it would be used. Until it can, D is not deployable.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--band", default="185,245", help="cohort weight band in grams")
    ap.add_argument("--control", default="cactus-nature-evapotranspiration")
    ap.add_argument("--csv")
    ap.add_argument("--index", action="store_true",
                    help="score watering indicators instead of comparing pots")
    ap.add_argument("--perm", type=int, default=200,
                    help="permutation replicates for the ET gain (0 to skip)")
    a = ap.parse_args()
    lo, hi = (float(x) for x in a.band.split(","))

    rows = query()
    by, reg = registry(rows)
    ctrl = a.control
    if ctrl not in reg:
        sys.exit(f"{ctrl} has no readings")

    cohort = {p for p, d in reg.items()
              if not d["retired"] and d["material"] == "plastic"
              and lo <= d["recent_g"] <= hi} | {ctrl}
    # The control is always in the cohort — it is the comparison. But evaporation
    # follows exposed soil area, so comparing it against a band it does not
    # belong to measures pot size and calls the answer transpiration. Say so
    # rather than printing a number that looks like all the others.
    cw = reg[ctrl]["recent_g"]
    off_band = not (lo <= cw <= hi)
    excluded = [(p, d) for p, d in reg.items()
                if lo <= d["recent_g"] <= hi and p not in cohort]

    print(f"{len(rows)} readings (quality=ok), {len(reg)} ids, "
          f"{sum(1 for d in reg.values() if d['retired'])} retired\n")
    print(f"cohort — plastic, live, {lo:.0f}-{hi:.0f} g: {len(cohort)} pots")
    if off_band:
        print(f"  ** the control is {cw:.0f} g, OUTSIDE this band. Its row below is a\n"
              f"     size comparison, not a bare-soil one — use --band around {cw:.0f} g. **")
    for p in sorted(cohort):
        d = reg[p]
        print(f"  {p:34s} {d['recent_g']:6.0f} g  n={d['n']}"
              + ("   <- bare-soil control" if p == ctrl else ""))
    if excluded:
        print("  excluded from this band:")
        for p, d in sorted(excluded):
            why = d["retired_why"] or d["material"]
            print(f"    {p:32s} {d['recent_g']:6.0f} g  ({why})")

    if a.index:
        return index_report(by, reg, cohort, ctrl, a)

    S = sessions(by, cohort)
    dev, rowsout = collections.defaultdict(list), []
    for t0, t1, dt, r, m in intervals(S, cohort):
        for p, v in r.items():
            dev[p].append(v - m)
            rowsout.append((t1, p, dt, v, v - m))

    print(f"\nevaporation relative to the cohort median in the SAME session")
    print(f"(the session zero cancels; absolute rates do not survive it)\n")
    print(f"  {'plant_id':34s} {'n':>3s} {'g/day':>7s} {'95% CI':>16s}")
    for p in sorted(dev, key=lambda p: -statistics.mean(dev[p])):
        x = dev[p]
        if len(x) < 4:
            print(f"  {p:34s} {len(x):3d}   (too few intervals)")
            continue
        l, h = boot(x)
        print(f"  {p:34s} {len(x):3d} {statistics.mean(x):7.2f} [{l:6.2f},{h:6.2f}]"
              + ("  *" if (l > 0 or h < 0) else "")
              + (("  <- bare soil, OFF-BAND" if off_band else "  <- bare soil")
                 if p == ctrl else ""))

    # Bare soil is not one number: it dries fast, then stops. Averaging over
    # stages cancels two opposite signs, which is what "indistinguishable from
    # the planted pots" meant the first three times it was concluded.
    s = sorted(by[ctrl])
    wet = [t for (t0, v0), (t, v) in zip(s, s[1:]) if v - v0 > JUMP_G]
    print(f"\nbare soil, by days since it was last watered:")
    got = False
    for t0, t1, dt, r, m in intervals(S, cohort):
        if ctrl not in r:
            continue
        prev = [t for t in wet if t <= t1]
        age = (t1 - prev[-1]).total_seconds() / 86400 if prev else None
        print(f"  {t1:%m-%d}  {age:4.1f} d since watering   {r[ctrl] - m:+6.2f} g/day"
              if age is not None else
              f"  {t1:%m-%d}  (never seen watered)      {r[ctrl] - m:+6.2f} g/day")
        got = True
    if not got:
        print("  no intervals — the control was not weighed alongside the cohort")

    if a.csv:
        import csv as _csv
        with open(a.csv, "w", newline="") as f:
            wtr = _csv.writer(f)
            wtr.writerow(["time", "plant_id", "days", "rate_g_per_day", "relative_g_per_day"])
            for t, p, dt, v, d in sorted(rowsout):
                wtr.writerow([t.isoformat(), p, round(dt, 3), round(v, 3), round(d, 3)])
        print(f"\nwrote {len(rowsout)} rows to {a.csv}")


if __name__ == "__main__":
    main()
