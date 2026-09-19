#!/usr/bin/env python3
"""The analysis half of Phase 3 — pure functions over recorded frames.

Separate from scan_repeat.py on purpose. Acquisition needs a board, a target
that is still warm and an operator in the room; analysis needs none of those,
and a hardware run costs an afternoon that cannot be repeated identically —
the target cools, the room changes. So every frame is recorded whole and the
numbers are DERIVED. Changing a threshold must never mean re-running hardware.

That split is also what makes the estimator testable: test_scan_stats.py feeds
synthetic runs with known answers through exactly the code the acceptance run
uses, with no fake board in the way.

THE CRITERION IS IN THIS FILE, AS CONSTANTS, BECAUSE IT WAS FIXED BEFORE THE
RUN. Phase 3 asks whether MG996R repeatability clears one thermal pixel; the
angular pitch is 1.72 deg/px on pan and 1.46 deg/px on tilt, so the servo's
+-1.5 deg spec maps to +-0.87 px and +-1.03 px. The answer lands ON the
threshold, which is why a criterion chosen after seeing the histogram would be
worthless — and why the noise floor is a precondition rather than a footnote.
"""
import json
import math
import statistics

ROWS, COLS = 24, 32

# --- the pre-registered criterion -------------------------------------------
# Stage 3 pass/fail.
P90_RADIAL_MAX_PX = 1.0     # per pose, 90th percentile of radial error
P2P_AXIS_MAX_PX = 2.0       # per pose, per axis, peak-to-peak
# Stage 2 gates. Above SIGMA_STATIC_MAX the instrument cannot resolve the
# question at all and the honest move is to stop and fix the instrument; the
# band between the two still measures, but only decides clear passes and clear
# failures, never a marginal one.
SIGMA_STATIC_GOOD_PX = 0.1
SIGMA_STATIC_MAX_PX = 0.3
CONTRAST_DRIFT_MAX = 0.20   # (max-min)/mean over one block
# Stage 3.2/3.3 — the run's own validity, checked before its result is read.
BRACKET_SHIFT_MAX_PX = 0.2
# Stage 2.3 — the scale factor that makes a pixel number mean an angle.
GAIN_R2_MIN = 0.99
GAIN_VARIATION_MAX = 0.10
CROSS_AXIS_MAX_RATIO = 0.20  # Stage 1.4: cross term vs main term

# A pixel whose temporal spread is this many times the frame's median spread is
# a CANDIDATE defect — not a confirmed one, and the difference turned out to
# matter. Measured on two consecutive 100-frame runs of the same bench: the
# spread distribution's own p99 sits at 3.7-5.4x the median, so this threshold
# lies inside the noise tail rather than outside it, and which pixels cross it
# is luck. The two runs produced [212] and [56, 120] — disjoint lists, with the
# other run's pixels at 4.7x and 1.1x respectively.
#
# A real defect is a property of the sensor and appears in EVERY run. So
# candidates are reported and INTERSECTED across runs; nothing is repaired on
# the strength of one. Raising the number until these two runs came out clean
# would have been choosing a threshold after seeing the histogram, which is
# the failure this repo keeps a lessons entry about.
BAD_PIXEL_SIGMA_RATIO = 5.0


def percentile(xs, q):
    """Linear-interpolated percentile, numpy's default convention.

    Spelled out because P90 of ten samples interpolates between the 9th and
    10th ranked value — it is very nearly the worst sample, so the Stage 3
    criterion is in practice close to a max. Worth knowing before reading a
    pass as comfortable.
    """
    s = sorted(xs)
    if not s:
        return None
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (q / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def load(path):
    """-> (manifest, frames). Rows are never mutated, only read."""
    manifest, frames = None, []
    with open(path) as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError as e:
                raise SystemExit(f"{path}:{n}: not JSON ({e})")
            kind = row.get("kind")
            if kind == "manifest":
                manifest = row
            elif kind == "frame":
                frames.append(row)
            else:
                raise SystemExit(f"{path}:{n}: unknown kind {kind!r}")
    if manifest is None:
        raise SystemExit(f"{path}: no manifest row — cannot tell what this run was")
    return manifest, frames


# --- bad pixels --------------------------------------------------------------

def bad_pixel_candidates(frames, rows=ROWS, cols=COLS,
                         ratio=BAD_PIXEL_SIGMA_RATIO):
    """Isolated pixels whose temporal spread is an outlier across a block.

    CANDIDATES, not defects — see BAD_PIXEL_SIGMA_RATIO. Confirm by
    intersecting several runs before excluding anything.

    Temporal spread alone is not enough, and getting this wrong is worse than
    not doing it. A target that wanders even a twentieth of a pixel drags the
    steep flanks of its own thermal profile across the sensor, and at ~12 C per
    pixel of gradient that is several times the NETD — so the flank pixels are
    spread outliers too. Flagging them removes the very signal the centroid is
    computed from, and the noise floor then comes out beautifully small for a
    measurement that no longer contains the target.

    So the second condition is ISOLATION: a dead or noisy pixel is a defect of
    one sensor element and its neighbours are fine, while a moving edge always
    produces a contiguous band. Candidates with a candidate neighbour are left
    alone.

    The cost is a pair of adjacent dead pixels, which this will miss. That is
    the right way round: a missed bad pixel widens the measured spread and can
    only make the result look worse than it is, while an eaten target makes it
    look better.
    """
    ok = [f["px"] for f in frames if f.get("ok") and f.get("px")]
    if len(ok) < 3:
        return []
    spread = [statistics.pstdev([f[i] for f in ok]) for i in range(rows * cols)]
    # A median of zero means every pixel but a handful was perfectly still —
    # synthetic data, or a block short enough that quantisation hid the noise.
    # Returning nothing there would be a silent no-op on exactly the input
    # where an outlier is most obvious, so the test becomes "moved at all".
    med = statistics.median(spread)
    limit = ratio * med if med > 0 else 0.0
    cand = {i for i, sd in enumerate(spread) if sd > limit}
    out = []
    for i in sorted(cand):  # noqa: E501
        r, c = divmod(i, cols)
        neigh = [(nr, nc) for nr, nc in
                 ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1))
                 if 0 <= nr < rows and 0 <= nc < cols]
        if not any(nr * cols + nc in cand for nr, nc in neigh):
            out.append(i)
    return out


def confirmed_bad_pixels(candidate_lists):
    """Pixels present in EVERY run's candidate list. The persistence test.

    A sensor defect does not move. Two lists that do not overlap are evidence
    that neither is measuring a defect, and intersecting them says so by
    coming back empty rather than by picking a side.
    """
    lists = [set(c) for c in candidate_lists if c is not None]
    if not lists:
        return []
    out = lists[0]
    for s in lists[1:]:
        out = out & s
    return sorted(out)


def repair(px, bad, rows=ROWS, cols=COLS):
    """A NEW frame with each bad pixel replaced by its good 4-neighbours' median.

    Returns a copy: the recorded frame stays the record. Substitution beats
    dropping because the centroid's support must stay a connected region — a
    hole punched in it changes the support's shape, which is precisely what
    the centroid measures.
    """
    if not bad:
        return list(px)
    badset = set(bad)
    out = list(px)
    for i in badset:
        r, c = divmod(i, cols)
        vals = [px[nr * cols + nc]
                for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1))
                if 0 <= nr < rows and 0 <= nc < cols and (nr * cols + nc) not in badset]
        if vals:
            out[i] = statistics.median(vals)
    return out


# --- per-block and per-pose statistics ---------------------------------------

def block_stats(samples):
    """Spread of one group of centroid samples. The static-block reading.

    `samples` are dicts with r, c, contrast — the output of centroid() for the
    frames of one block, already filtered to the accepted ones.
    """
    n = len(samples)
    out = {"n": n, "sigma_r": None, "sigma_c": None,
           "mean_r": None, "mean_c": None, "contrast_drift": None}
    if n < 2:
        return out
    rs = [s["r"] for s in samples]
    cs = [s["c"] for s in samples]
    out["sigma_r"] = statistics.pstdev(rs)
    out["sigma_c"] = statistics.pstdev(cs)
    out["mean_r"] = statistics.fmean(rs)
    out["mean_c"] = statistics.fmean(cs)
    con = [s["contrast"] for s in samples]
    mean_con = statistics.fmean(con)
    if mean_con > 0:
        # Range over mean, not first-vs-last: a target that cools and is then
        # bumped warmer again would show ~0 end-to-end while having drifted
        # through the whole block.
        out["contrast_drift"] = (max(con) - min(con)) / mean_con
    return out


def pose_stats(samples):
    """Repeatability of ONE pose: radial error about that pose's own mean.

    About its own mean, not about the commanded position, because there is no
    px/us-free way to know where the commanded position IS in the image. The
    question Phase 3 asks is "does it come back to the same place", which is a
    question about spread, not about accuracy.
    """
    n = len(samples)
    out = {"n": n, "mean_r": None, "mean_c": None,
           "p90_radial": None, "max_radial": None, "p2p_r": None, "p2p_c": None}
    if n < 2:
        return out
    rs = [s["r"] for s in samples]
    cs = [s["c"] for s in samples]
    mr, mc = statistics.fmean(rs), statistics.fmean(cs)
    radial = [math.hypot(r - mr, c - mc) for r, c in zip(rs, cs)]
    out.update({"mean_r": mr, "mean_c": mc,
                "p90_radial": percentile(radial, 90),
                "max_radial": max(radial),
                "p2p_r": max(rs) - min(rs),
                "p2p_c": max(cs) - min(cs)})
    return out


def half_split_drift(samples):
    """First half vs second half of one pose's repeats, in each axis.

    Stage 3.3. A monotonic march across a run is drift — the target cooling
    and shrinking its support, or the bracket relaxing — and a spread computed
    over it reports that drift as repeatability.
    """
    n = len(samples)
    if n < 4:
        return {"n": n, "d_r": None, "d_c": None}
    half = n // 2
    a, b = samples[:half], samples[n - half:]
    return {"n": n,
            "d_r": statistics.fmean([s["r"] for s in b]) - statistics.fmean([s["r"] for s in a]),
            "d_c": statistics.fmean([s["c"] for s in b]) - statistics.fmean([s["c"] for s in a])}


def bracket_shift(pre, post):
    """Distance between the mean centroid of the two static blocks.

    Stage 3.2. If the target was knocked or has cooled enough to move its own
    apparent centre, the repeatability number measured between them is about
    the target, not the mechanism — and the run is void. NOT a correction: a
    run compensated for a bump is a run whose bump was guessed at.
    """
    a, b = block_stats(pre), block_stats(post)
    if a["mean_r"] is None or b["mean_r"] is None:
        return None
    return math.hypot(b["mean_r"] - a["mean_r"], b["mean_c"] - a["mean_c"])


# --- gain --------------------------------------------------------------------

def fit_line(xs, ys):
    """Least squares. r2 is None when y does not vary — see gain_stats()."""
    n = len(xs)
    out = {"n": n, "slope": None, "intercept": None, "r2": None}
    if n < 2:
        return out
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return out
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    out.update({"slope": slope, "intercept": intercept,
                "r2": None if ss_tot <= 0 else 1.0 - ss_res / ss_tot})
    return out


def gain_stats(points):
    """px per us for one driven axis, plus how much of it leaks into the other.

    `points` are (us, mean_r, mean_c) — one entry per commanded width.

    R-squared is reported for the MAIN axis only. On the cross axis the image
    coordinate barely varies, so its total sum of squares is ~0 and R-squared
    measures how well a line fits noise: a cross axis that is doing exactly
    what it should scores terribly. The cross axis is judged by the ratio of
    slopes instead, which is the question actually being asked — how much of
    this motion shows up over there.
    """
    us = [p[0] for p in points]
    fr, fc = fit_line(us, [p[1] for p in points]), fit_line(us, [p[2] for p in points])
    sr, sc = fr["slope"], fc["slope"]
    out = {"n": len(points), "fit_r": fr, "fit_c": fc,
           "main": None, "cross_ratio": None, "reason": "",
           "half_slopes": None, "gain_variation": None}
    if sr is None or sc is None:
        return out
    # Nothing moved in either axis. A stalled servo, a linkage that came apart,
    # a horn spinning on its spline — all produce exactly this, and it is a
    # measurement result ("the mechanism did not respond"), not an exception.
    # Dividing to find the cross term here would be 0/0.
    if sr == 0.0 and sc == 0.0:
        out["reason"] = "no_motion"
        return out
    main, cross = ("r", sc / sr) if abs(sr) >= abs(sc) else ("c", sr / sc)
    out["main"], out["cross_ratio"] = main, abs(cross)

    # Gain across travel: fit each half of the commanded range separately. A
    # servo whose gain sags at one end still fits a straight line well enough
    # to pass R-squared, and a scan that assumes a constant px/us would then
    # place poses wrong only at the ends — where cable tension already differs.
    ordered = sorted(points)
    if len(ordered) >= 4:
        half = len(ordered) // 2
        pick = (lambda p: p[1]) if main == "r" else (lambda p: p[2])
        lo = fit_line([p[0] for p in ordered[:half]], [pick(p) for p in ordered[:half]])
        hi = fit_line([p[0] for p in ordered[len(ordered) - half:]],
                      [pick(p) for p in ordered[len(ordered) - half:]])
        overall = sr if main == "r" else sc
        if lo["slope"] is not None and hi["slope"] is not None and overall:
            out["half_slopes"] = (lo["slope"], hi["slope"])
            out["gain_variation"] = max(abs(lo["slope"] / overall - 1.0),
                                        abs(hi["slope"] / overall - 1.0))
    return out


# --- gates -------------------------------------------------------------------

# The same numbers as one dict, so manifest_for() and criterion() cannot drift
# apart from the constants above.
CRITERION_DEFAULTS = {
    "p90_radial_max_px": P90_RADIAL_MAX_PX,
    "p2p_axis_max_px": P2P_AXIS_MAX_PX,
    "sigma_static_good_px": SIGMA_STATIC_GOOD_PX,
    "sigma_static_max_px": SIGMA_STATIC_MAX_PX,
    "contrast_drift_max": CONTRAST_DRIFT_MAX,
    "bracket_shift_max_px": BRACKET_SHIFT_MAX_PX,
    "gain_r2_min": GAIN_R2_MIN,
    "gain_variation_max": GAIN_VARIATION_MAX,
    "cross_axis_max_ratio": CROSS_AXIS_MAX_RATIO,
}


def criterion(manifest):
    """-> (thresholds, missing_keys). The ones THIS run was judged against.

    Reading the module constants instead would mean that tightening one of
    them silently re-judges every recording ever made — turning an afternoon
    that passed into one that failed, with nothing in the output saying the
    bar had moved. The whole reason the criterion is copied into the manifest
    is to make a verdict reproducible; honouring the copy is the other half of
    that, and without it the promise is just a comment.

    Keys absent from an older recording fall back to today's values and are
    reported, so the report can say which numbers it had to supply itself.
    """
    rec = (manifest or {}).get("criterion") or {}
    out = dict(CRITERION_DEFAULTS)
    out.update({k: v for k, v in rec.items() if k in out})
    return out, sorted(k for k in CRITERION_DEFAULTS if k not in rec)


def gate(name, value, limit, worse="above"):
    """One pass/fail line. A missing value is a FAIL, never a pass by absence."""
    if value is None:
        return {"name": name, "value": None, "limit": limit,
                "ok": False, "note": "not measured"}
    ok = value <= limit if worse == "above" else value >= limit
    return {"name": name, "value": value, "limit": limit, "ok": ok, "note": ""}
