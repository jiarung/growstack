#!/usr/bin/env python3
"""Phase 3's measurement: does the pan/tilt come back to the same place?

    ./scan_repeat.py http://<ip> --mode static --box 8,12,16,20 --n 100
    ./scan_repeat.py http://<ip> --mode gain   --box 8,12,16,20 --axis pan
    ./scan_repeat.py http://<ip> --mode repeat --box 8,12,16,20 \
        --poses A:1400,1500 B:1600,1500 C:1500,1600 --cycles 10
    ./scan_repeat.py --summarize runs/2026-09-18T22-04.jsonl

RUN THE MODES IN THAT ORDER. `static` measures the noise floor with the servos
untouched, and it is a hard precondition: MG996R's spec maps to about one
thermal pixel, so an instrument whose own spread is 0.3 px cannot resolve the
question and running `repeat` anyway produces a number nobody can interpret.
`gain` turns px into deg so the answer can be compared to the servo's spec at
all, and doubles as the cross-axis check — pan must move the image along one
axis only. `repeat` is the actual experiment.

EVERY FRAME IS RECORDED WHOLE, and the numbers come out of the recording. A
hardware run cannot be repeated identically — the target cools, the room
changes, somebody walks past — so changing a threshold must never mean
re-running hardware. --summarize re-derives the whole report offline, and is
the same code path the live run prints from.

THE TWO FAILURES ARE NOT SYMMETRIC. A capture that fails is recorded as failed
and the run continues; those frames are dropped at analysis time and the loss
is visible in the counts. A servo command that fails ABORTS — pose integrity
cannot be recovered after the fact, and pressing on produces a tidy set of
numbers for poses the head was never actually at. On abort the axis RETREATS
to its last good width and keeps holding: releasing an axis that is holding
the camera against gravity drops the camera (servo.h, invariant 2).

Uses argparse, like servo_probe.py and unlike the older tools here, whose
hand-rolled opt() silently ignores flags it does not know. A mistyped --settle
must be an error: this repo's most-repeated failure shape is a run that
quietly used a default and produced a beautiful wrong number.
"""
import argparse
import datetime
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scan_stats as S                                              # noqa: E402
from thermal_view import (centroid, fetch, orient,                   # noqa: E402
                          orientation_conflict, parse_roi)

CH = {"pan": 5, "tilt": 6}      # must match servo.h CH_PAN / CH_TILT
US_MIN, US_MAX = 600, 2400      # servo.h electrical span
US_CENTER = 1500

# The module runs at 4 Hz and the firmware's take() is consuming, so a null
# frame means "nothing new yet", not a fault. Poll a little faster than the
# period and let the seq check be the thing that guarantees freshness.
POLL_S = 0.08
FRAME_PERIOD_S = 0.25
# One flush plus two. Provable rather than cautious: a 1544-byte frame takes
# 134 ms to arrive at 115200 baud inside a 250 ms period, so the frame after
# the settle (S+1) cannot be shown to have FINISHED INTEGRATING after the head
# stopped. S+2 can.
DISCARD_FRAMES = 3


# --- board ------------------------------------------------------------------

class ServoAborted(Exception):
    """Raised after the retreat, to unwind the run. Never caught to continue."""


class Head:
    """The commanded state of the mechanism, and the only path that moves it."""

    def __init__(self, base, settle_ms, approach_us, dry_run=False):
        # Checked in the constructor, not only in the CLI, because the CLI is
        # not the only way here — the tests build one directly, and so would
        # any future scan controller. A negative settle used to survive as far
        # as time.sleep(), which raises ValueError AFTER the first servo
        # command has gone out; ValueError is not ServoAborted, so the abort
        # path never ran and the head was left at the back-off width.
        for name, v in (("settle_ms", settle_ms), ("approach_us", approach_us)):
            if not (isinstance(v, (int, float)) and v == v and v >= 0):
                raise ValueError(f"{name} must be >= 0, got {v!r}")
        self.base = base.rstrip("/")
        self.settle_s = settle_ms / 1000.0
        self.approach_us = approach_us
        self.dry_run = dry_run
        self.last_good = {}          # axis -> width we retreat TO
        self.moves = 0               # servo commands actually sent
        # Arrivals at a pose, counted separately from commands. They are not
        # the same number: a uni-directional approach sends TWO commands per
        # arrival (back off, then come in), so parity taken from the command
        # count is always even at the next arrival and "alternate" silently
        # behaves exactly like "uni". Stage 3.4 exists to ask whether the
        # approach matters, and that control would have compared a run against
        # an identical copy of itself and reported no difference.
        self.arrivals = 0

    def _cmd(self, axis, us):
        if not US_MIN <= us <= US_MAX:
            raise ServoAborted(f"{axis} {us}us is outside the electrical span "
                               f"{US_MIN}..{US_MAX}")
        if self.dry_run:
            self.last_good[axis] = us
            return
        url = f"{self.base}/servo?ch={CH[axis]}&us={us}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                doc = json.loads(r.read())
        except Exception as e:                                       # noqa: BLE001
            raise ServoAborted(f"{axis} -> {us}us failed: {e}")
        if not doc.get("present"):
            raise ServoAborted("PCA9685 not present — check /i2c/scan for 0x40")
        if doc.get("set") != "ok":
            raise ServoAborted(f"firmware refused {axis} {us}us: {doc.get('set')!r}")
        self.last_good[axis] = us
        self.moves += 1

    def goto(self, targets, approach="uni"):
        """Move to {axis: us}, one axis at a time, arriving from one direction.

        Uni-directional approach backs off past the target and comes in from a
        fixed side, so gear backlash is taken up the same way every time; the
        alternative is measuring the gearbox's hysteresis and calling it the
        servo's repeatability. Whether it actually helps is Stage 3.4's
        question, which is why `alt` exists rather than being assumed away.

        Axes move sequentially because servo.h does not allow otherwise: two
        MG996R stalling together is 5 A, and this rail's margin is the kind of
        thing that has bitten this project before.
        """
        # One parity per POSE VISIT, not per axis: both axes of a pose should be
        # entered from the same side, and the side is what alternates.
        visit = self.arrivals
        self.arrivals += 1
        for axis, us in sorted(targets.items()):
            if approach != "none" and self.approach_us > 0:
                sign = 1 if approach == "uni" else (-1 if visit % 2 else 1)
                back = us - sign * self.approach_us
                if US_MIN <= back <= US_MAX:
                    self._cmd(axis, back)
                    time.sleep(self.settle_s)
            self._cmd(axis, us)
            time.sleep(self.settle_s)

    def retreat(self, why):
        """The one recovery path. Never releases — see the module docstring."""
        if self.dry_run or not self.last_good:
            print(f"\n[{why}] nothing commanded yet; leaving the axes alone.")
            return
        print(f"\n[{why}] retreating to last good widths "
              f"(NOT releasing — a released axis holding weight drops)")
        for axis, us in sorted(self.last_good.items()):
            try:
                with urllib.request.urlopen(
                        f"{self.base}/servo?ch={CH[axis]}&us={us}", timeout=10) as r:
                    r.read()
                print(f"  {axis} -> {us}us")
            except Exception as e:                                   # noqa: BLE001
                print(f"  !! RETREAT FAILED for {axis}: {e}\n"
                      f"  !! cut the servo rail by hand if the axis is straining.")


def fresh_frames(base, k, discard=DISCARD_FRAMES, verbose=False):
    """k frames that are provably after the settle, newest-first-discarded.

    Returns a list of row dicts, each already marked ok/not-ok. A capture
    failure is DATA, not an exception: the run must continue and the loss must
    be visible in the counts rather than as a gap nobody can size later.
    """
    url = f"{base.rstrip('/')}/thermal"
    rows, last_seq, dropped = [], None, 0
    # Enough polls for k frames plus the discards, at roughly one new frame
    # every FRAME_PERIOD_S, with headroom for the nulls in between.
    budget = int((discard + k) * (FRAME_PERIOD_S / POLL_S) * 2) + 20
    for _ in range(budget):
        if len(rows) >= k:
            break
        try:
            doc = fetch(url)
        except Exception as e:                                       # noqa: BLE001
            rows.append({"ok": False, "reason": f"http:{e}"})
            time.sleep(FRAME_PERIOD_S)
            continue
        f = doc.get("frame")
        if not f:
            time.sleep(POLL_S)          # consuming take(): nothing new yet
            continue
        seq = f.get("seq")
        if last_seq is not None and seq is not None and seq <= last_seq:
            time.sleep(POLL_S)          # same frame again; not a new exposure
            continue
        last_seq = seq
        if dropped < discard:
            dropped += 1
            continue
        rows.append({"ok": True, "seq": seq, "ta_c": f.get("ta_c"),
                     "checksum_ok": f.get("checksum_ok", True),
                     "orientation": f.get("orientation", "wire"),
                     "rows": f.get("rows", S.ROWS), "cols": f.get("cols", S.COLS),
                     "px": f["px"]})
        if verbose:
            print(f"    seq {seq}", end="\r", flush=True)
    while len(rows) < k:
        rows.append({"ok": False, "reason": "poll_budget_exhausted"})
    return rows


# --- recording ---------------------------------------------------------------

def git_rev():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=HERE, capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except Exception:                                                # noqa: BLE001
        return None


def manifest_for(args, poses):
    """Everything needed to read this run in a year, including the criterion.

    The criterion is copied in rather than referenced so that a recording
    carries the thresholds it was judged against. Tightening a constant later
    must not silently re-judge an old run as a failure — or, worse, a pass.
    """
    return {
        "kind": "manifest", "version": 1,
        "t": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": args.mode, "base_url": args.base_url, "git_rev": git_rev(),
        "box": args.box, "flipv": args.flipv, "fliph": args.fliph,
        "n": args.n, "settle_ms": args.settle, "approach": args.approach,
        "approach_us": args.approach_us, "cycles": args.cycles,
        "axis": args.axis, "gain_from": args.gain_from, "gain_to": args.gain_to,
        "gain_steps": args.gain_steps, "poses": poses,
        "min_contrast_c": args.min_contrast,
        "polarity": "cold" if args.cold else "hot",
        "bad_pixels": parse_bad_pixels(args.bad_pixels),
        "criterion": dict(S.CRITERION_DEFAULTS),
    }


class Recorder:
    def __init__(self, path):
        self.path = path
        self.fh = open(path, "w") if path else None
        self.n_ok = self.n_bad = 0

    def write(self, row):
        if row.get("kind") == "frame":
            self.n_ok += 1 if row.get("ok") else 0
            self.n_bad += 0 if row.get("ok") else 1
        if self.fh:
            self.fh.write(json.dumps(row) + "\n")
            self.fh.flush()   # a run that dies at cycle 8 keeps cycles 1..7

    def close(self):
        if self.fh:
            self.fh.close()


def collect(rec, base, label, k, extra=None, verbose=False):
    """Record k fresh frames under `label`. Does NOT move — the caller does.

    Kept apart from Head.goto() deliberately: a function that both moves and
    samples is one whose label argument can drift into meaning a target, and
    the resulting run records poses it was never at.
    """
    print(f"  {label}: {k} frames", flush=True)
    for row in fresh_frames(base, k, verbose=verbose):
        rec.write({"kind": "frame", "block": label, **(extra or {}), **row})


# --- analysis ----------------------------------------------------------------

def samples_from(frames, box, flipv, fliph, min_contrast, bad=(),
                 polarity="hot"):
    """Centroid every accepted frame of a block, in recorded orientation.

    orient() is applied here for the same reason thermal_view applies it once:
    the search box was chosen while looking at an oriented image, so analysing
    an unoriented frame would index different pixels entirely.
    """
    out, rejected = [], []
    for f in frames:
        if not f.get("ok"):
            rejected.append(f.get("reason", "capture_failed"))
            continue
        rows, cols = f.get("rows", S.ROWS), f.get("cols", S.COLS)
        px = orient(S.repair(f["px"], bad, rows, cols), rows, cols, flipv, fliph)
        cd = centroid(px, rows, cols, box, min_contrast=min_contrast,
                      polarity=polarity)
        if cd["ok"]:
            out.append(cd)
        else:
            rejected.append(cd["reason"])
    return out, rejected


def group(frames, key="block"):
    """-> ordered {value: [frame,...]}, preserving the order blocks were run."""
    out = {}
    for f in frames:
        out.setdefault(f.get(key), []).append(f)
    return out


def fmt(v, nd=3):
    return "--" if v is None else f"{v:.{nd}f}"


def show_gates(gates):
    for g in gates:
        mark = "PASS" if g["ok"] else "FAIL"
        note = f"  ({g['note']})" if g["note"] else ""
        print(f"  [{mark}] {g['name']:<34} {fmt(g['value'])}  limit {g['limit']}{note}")
    return all(g["ok"] for g in gates)


def parse_bad_pixels(text, rows=S.ROWS, cols=S.COLS):
    """'56,120' -> [56, 120], validated against the frame they will index."""
    if not text:
        return []
    try:
        out = sorted({int(x) for x in text.split(",") if x.strip()})
    except ValueError:
        raise SystemExit(f"--bad-pixels wants comma-separated indices (got {text!r})")
    bad = [i for i in out if not 0 <= i < rows * cols]
    if bad:
        raise SystemExit(f"--bad-pixels out of range for a {rows}x{cols} frame: {bad}")
    return out


def report(manifest, frames, override=None):
    """Print the whole report and return an exit code. Pure: no board.

    `override` re-derives the run under different ANALYSIS choices — the
    polarity and the search box. Those are not acquisition parameters: every
    frame's 768 floats are in the recording, so a run taken with the estimator
    pointed the wrong way is a run whose numbers were never computed, not a
    run that has to happen again on a bench whose target has since warmed up.
    Overrides are printed, loudly, because a report that quietly disagreed
    with its own manifest would be worse than one that refused.
    """
    box = parse_roi(manifest["box"], S.ROWS, S.COLS) if manifest.get("box") else None
    flipv, fliph = manifest.get("flipv", False), manifest.get("fliph", False)
    mc = manifest.get("min_contrast_c", 5.0)
    pol = manifest.get("polarity", "hot")
    mode = manifest["mode"]
    over = override or {}
    confirmed = over.get("bad_pixels") or manifest.get("bad_pixels") or []
    changed = []
    if over.get("polarity") and over["polarity"] != pol:
        changed.append(f"polarity {pol} -> {over['polarity']}")
        pol = over["polarity"]
    if over.get("box") and over["box"] != manifest.get("box"):
        changed.append(f"box {manifest.get('box')} -> {over['box']}")
        manifest = dict(manifest, box=over["box"])
    if over.get("min_contrast") is not None and over["min_contrast"] != mc:
        changed.append(f"min_contrast {mc} -> {over['min_contrast']}")
        mc = over["min_contrast"]
    blocks = group(frames)
    # The thresholds THIS run was judged against, not today's. See
    # scan_stats.criterion() — honouring the copy is what makes the copy mean
    # anything, and without it the manifest is a comment that says "promise".
    crit, missing = S.criterion(manifest)

    print(f"\n=== {mode} run  {manifest.get('t')}  git {manifest.get('git_rev')} ===")
    if changed:
        print("RE-ANALYSED, not as recorded: " + "; ".join(changed))
    print(f"box {manifest.get('box')}  flipv={flipv} fliph={fliph}  "
          f"settle {manifest.get('settle_ms')}ms  approach {manifest.get('approach')}"
          f"  target={pol}")
    unverified = sum(1 for f in frames if f.get("ok") and not f.get("checksum_ok", True))
    if unverified:
        print(f"note: {unverified} frames carry checksum_ok=false — expected while "
              f"the frame checksum convention is unresolved (parser is in REPORT mode)")
    clash = orientation_conflict(
        next((f.get("orientation", "wire") for f in frames if f.get("ok")), "wire"),
        flipv, fliph)
    if clash:
        print(f"WARNING: {clash}")
    if missing:
        # Never silently: a verdict reached partly against today's bar and
        # partly against the recording's is a verdict whose bar is unstated.
        print(f"note: this recording predates {len(missing)} threshold(s) "
              f"({', '.join(missing)}); today's values were used for those")

    if mode == "static":
        fr = blocks.get("static", [])
        cand = S.bad_pixel_candidates(fr)
        # Nothing is repaired on one run's evidence: the candidate threshold
        # sits inside this sensor's noise tail, so a list from a single block
        # is partly luck. --bad-pixels takes the list you confirmed by
        # intersecting several runs.
        bad = confirmed or []
        clean, rej = samples_from(fr, box, flipv, fliph, mc, bad, pol)
        st = S.block_stats(clean)
        print(f"\nframes {len(fr)}  accepted {len(clean)}  rejected {len(rej)}")
        if rej:
            print(f"  rejections: {', '.join(sorted(set(rej)))}")
        print(f"bad-pixel candidates ({len(cand)}): {cand if cand else 'none'}")
        if cand and not bad:
            print("  NOT excluded. A defect is a property of the sensor and "
                  "appears in EVERY run;\n  this threshold sits inside the "
                  "noise tail, so one run's list is partly luck.\n  Run again, "
                  "intersect the lists, and pass the survivors as "
                  "--bad-pixels i,j,k.")
        if bad:
            print(f"  excluded by neighbour median (confirmed): {bad}")
        print(f"sigma_r {fmt(st['sigma_r'])} px   sigma_c {fmt(st['sigma_c'])} px   "
              f"contrast drift {fmt(st['contrast_drift'])}")
        worst = max([v for v in (st["sigma_r"], st["sigma_c"]) if v is not None],
                    default=None)
        gates = [S.gate("2.1 sigma_r", st["sigma_r"], crit["sigma_static_good_px"]),
                 S.gate("2.1 sigma_c", st["sigma_c"], crit["sigma_static_good_px"]),
                 S.gate("2.2 contrast drift", st["contrast_drift"], crit["contrast_drift_max"])]
        ok = show_gates(gates)
        if worst is not None and worst > crit["sigma_static_max_px"]:
            print(f"\n  STOP. sigma_static {worst:.3f} px > {crit['sigma_static_max_px']} px.\n"
                  f"  The answer lands on 1 px; this instrument cannot resolve it.\n"
                  f"  Fix in order: a constant-power heat source, a smaller search\n"
                  f"  box, excluded bad pixels, then a bench that is not vibrating.")
        elif worst is not None and worst > crit["sigma_static_good_px"]:
            print(f"\n  Usable but RESOLUTION-LIMITED ({worst:.3f} px). Stage 3 may\n"
                  f"  call a clear pass or a clear failure, never a marginal one,\n"
                  f"  and the report must say so.")
        return 0 if ok else 1

    if mode == "gain":
        pts, rows, dropped_widths = [], [], []
        for label, fr in blocks.items():
            if not label.startswith("us="):
                continue
            clean, _ = samples_from(fr, box, flipv, fliph, mc, (), pol)
            st = S.block_stats(clean)
            if st["mean_r"] is None:
                dropped_widths.append(label)
                continue
            us = int(label.split("=", 1)[1])
            pts.append((us, st["mean_r"], st["mean_c"]))
            rows.append((us, st["mean_r"], st["mean_c"], st["n"]))
        if dropped_widths:
            # Almost always the target leaving the search box at one end of the
            # sweep. A slope fitted to the widths that survived is fitted to a
            # sub-range AND to truncated centroids at its edge, so it is wrong
            # in a way that still looks linear.
            print(f"\nDROPPED {len(dropped_widths)} widths: "
                  f"{', '.join(dropped_widths)}\n"
                  f"  The box must contain the target across the WHOLE sweep.\n"
                  f"  Widen --box or shorten the sweep and run it again; do not\n"
                  f"  read the gain below.")
        print(f"\n{'us':>6}  {'mean_r':>8}  {'mean_c':>8}  n")
        for us, r, c, n in sorted(rows):
            print(f"{us:>6}  {r:>8.3f}  {c:>8.3f}  {n}")
        g = S.gain_stats(pts)
        if g["main"] is None:
            if g.get("reason") == "no_motion":
                print("\nNO MOTION: the target did not move in either image axis\n"
                      "  across the whole sweep. The servo acknowledged every\n"
                      "  command, so this is mechanical: a horn slipping on its\n"
                      "  spline, a linkage adrift, or a head that is not carrying\n"
                      "  the thermal module at all.")
            else:
                print("\nnot enough usable points to fit a gain")
            return 1
        main = g["main"]
        fit = g["fit_r"] if main == "r" else g["fit_c"]
        print(f"\nmain axis: image {main}   {fit['slope']:.5f} px/us   "
              f"R2 {fmt(fit['r2'], 4)}")
        print(f"cross term: {fmt(g['cross_ratio'])} of main")
        if g["half_slopes"]:
            print(f"half slopes: {g['half_slopes'][0]:.5f} / {g['half_slopes'][1]:.5f} "
                  f"px/us   variation {fmt(g['gain_variation'])}")
        gates = [S.gate("2.3 fit R2", fit["r2"], crit["gain_r2_min"], worse="below"),
                 S.gate("2.3 gain variation", g["gain_variation"], crit["gain_variation_max"]),
                 S.gate("1.4 cross/main ratio", g["cross_ratio"], crit["cross_axis_max_ratio"]),
                 S.gate("2.3 widths kept", float(len(dropped_widths)), 0.0)]
        return 0 if show_gates(gates) else 1

    # repeat
    pre, post = blocks.get("static_pre", []), blocks.get("static_post", [])
    cand = S.bad_pixel_candidates(pre)
    bad = confirmed or []
    if cand and not bad:
        print(f"bad-pixel candidates in static_pre ({len(cand)}): {cand} "
              f"— not excluded; confirm across runs first")
    pre_s, _ = samples_from(pre, box, flipv, fliph, mc, bad, pol)
    post_s, _ = samples_from(post, box, flipv, fliph, mc, bad, pol)
    noise = S.block_stats(pre_s)
    shift = S.bracket_shift(pre_s, post_s)
    print(f"\nnoise floor (static_pre): sigma_r {fmt(noise['sigma_r'])} px   "
          f"sigma_c {fmt(noise['sigma_c'])} px   n={noise['n']}")
    print(f"bracket shift pre->post: {fmt(shift)} px")

    gates, drops = [], []
    print(f"\n{'pose':>6}  {'n':>3}  {'P90 radial':>10}  {'max':>7}  "
          f"{'p2p_r':>7}  {'p2p_c':>7}  {'half dr':>8}  {'half dc':>8}")
    for pose, fr in sorted(group([f for f in frames if f.get("pose")], "pose").items()):
        # One sample per CYCLE, not per frame: repeated frames at one pose
        # measure the sensor, and averaging them is how the mechanism's spread
        # stops being swamped by it.
        per_cycle = []
        for cyc, cf in sorted(group(fr, "cycle").items()):
            clean, rej = samples_from(cf, box, flipv, fliph, mc, bad, pol)
            st = S.block_stats(clean)
            if st["mean_r"] is None:
                drops.append(f"{pose}#{cyc}: {', '.join(sorted(set(rej))) or 'no frames'}")
                continue
            per_cycle.append({"r": st["mean_r"], "c": st["mean_c"]})
        ps, hs = S.pose_stats(per_cycle), S.half_split_drift(per_cycle)
        print(f"{pose:>6}  {ps['n']:>3}  {fmt(ps['p90_radial']):>10}  "
              f"{fmt(ps['max_radial']):>7}  {fmt(ps['p2p_r']):>7}  "
              f"{fmt(ps['p2p_c']):>7}  {fmt(hs['d_r']):>8}  {fmt(hs['d_c']):>8}")
        gates += [S.gate(f"3.1 {pose} P90 radial", ps["p90_radial"], crit["p90_radial_max_px"]),
                  S.gate(f"3.1 {pose} p2p_r", ps["p2p_r"], crit["p2p_axis_max_px"]),
                  S.gate(f"3.1 {pose} p2p_c", ps["p2p_c"], crit["p2p_axis_max_px"])]
    if drops:
        print(f"\ndropped cycles ({len(drops)}): " + "; ".join(drops))
    gates.append(S.gate("3.2 bracket shift", shift, crit["bracket_shift_max_px"]))
    ok = show_gates(gates)
    print("\n  Read P90 next to the noise floor above: a spread at or below it is\n"
          "  the sensor's, not the mechanism's, and this run cannot tell them apart.")
    if not ok:
        print("\n  A failure here is a RESULT, not a broken run: per the roadmap the\n"
              "  answer is a digital servo, and Stage 1.3 onward is re-run unchanged.")
    return 0 if ok else 1


# --- run ---------------------------------------------------------------------

def parse_poses(items):
    """['A:1400,1500', ...] -> [{'name','pan_us','tilt_us'}] with widths checked."""
    out = []
    for it in items:
        try:
            name, widths = it.split(":", 1)
            pan, tilt = (int(x) for x in widths.split(","))
        except ValueError:
            raise SystemExit(f"--poses wants NAME:pan_us,tilt_us (got {it!r})")
        for axis, us in (("pan", pan), ("tilt", tilt)):
            if not US_MIN <= us <= US_MAX:
                raise SystemExit(f"pose {name}: {axis} {us}us outside {US_MIN}..{US_MAX}")
        out.append({"name": name, "pan_us": pan, "tilt_us": tilt})
    if len({p["name"] for p in out}) != len(out):
        raise SystemExit("--poses names must be unique; they label the results")
    return out


def run(args, rec, head):
    base, n = args.base_url, args.n
    if args.mode == "static":
        print("servos are NOT commanded in this mode — do not touch the bench")
        collect(rec, base, "static", n, verbose=args.verbose)
        return

    if args.mode == "gain":
        step = (args.gain_to - args.gain_from) / max(1, args.gain_steps - 1)
        widths = [int(round(args.gain_from + i * step)) for i in range(args.gain_steps)]
        other = "tilt" if args.axis == "pan" else "pan"
        print(f"sweeping {args.axis} across {widths}, holding {other} at {US_CENTER}")
        # Approach every width from the same side so the gain being measured is
        # the servo's and not the backlash's.
        head.goto({other: US_CENTER}, approach=args.approach)
        for us in widths:
            head.goto({args.axis: us}, approach=args.approach)
            collect(rec, base, f"us={us}", n,
                    extra={f"{args.axis}_us": us, f"{other}_us": US_CENTER},
                    verbose=args.verbose)
        return

    poses = parse_poses(args.poses)
    # BOTH static blocks are taken at the same pose — the first one — and the
    # head is driven back to it before the closing block. They exist to detect
    # the TARGET moving, and the head moves too: bracketing a run with a block
    # at pose A and one at wherever the last cycle left off compares two
    # different views of a target that never moved, and every run would then
    # void itself at 3.2 for a reason that has nothing to do with the target.
    home = {"pan": poses[0]["pan_us"], "tilt": poses[0]["tilt_us"]}
    head.goto(home, approach=args.approach)
    collect(rec, base, "static_pre", args.static_n, verbose=args.verbose)
    for cyc in range(args.cycles):
        print(f"cycle {cyc + 1}/{args.cycles}")
        for p in poses:
            head.goto({"pan": p["pan_us"], "tilt": p["tilt_us"]}, approach=args.approach)
            collect(rec, base, f"pose:{p['name']}", n,
                    extra={"cycle": cyc, "pose": p["name"],
                           "pan_us": p["pan_us"], "tilt_us": p["tilt_us"]},
                    verbose=args.verbose)
    head.goto(home, approach=args.approach)
    collect(rec, base, "static_post", args.static_n, verbose=args.verbose)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base_url", nargs="?", help="e.g. http://192.168.50.204")
    ap.add_argument("--summarize", metavar="FILE",
                    help="re-derive the report from a recording; no board needed")
    ap.add_argument("--mode", choices=["static", "gain", "repeat"])
    ap.add_argument("--box", help="search window r0,c0,r1,c1 (inclusive)")
    ap.add_argument("--flipv", action="store_true")
    ap.add_argument("--fliph", action="store_true")
    ap.add_argument("--n", type=int, default=10,
                    help="frames per block (static: total; repeat: per pose per cycle)")
    ap.add_argument("--static-n", type=int, default=20, dest="static_n",
                    help="frames in each bracketing static block (repeat mode)")
    ap.add_argument("--min-contrast", type=float, default=5.0, dest="min_contrast",
                    help="deg C from background below which a frame is rejected")
    ap.add_argument("--bad-pixels", dest="bad_pixels", metavar="i,j,k",
                    help="pixel indices CONFIRMED across several runs, to "
                         "exclude by neighbour median. One run's candidates "
                         "are not evidence: this sensor's noise tail reaches "
                         "the candidate threshold, so the list moves between "
                         "runs. Intersect first.")
    ap.add_argument("--cold", action="store_true",
                    help="the target is COLDER than the room (a chilled cup). "
                         "Without it the estimator locks onto whatever is "
                         "hottest, which on this bench is usually the board.")
    ap.add_argument("--settle", type=int, default=400,
                    help="ms to wait after each servo command (Stage 3.4 tests this)")
    ap.add_argument("--approach", choices=["uni", "alt", "none"], default="uni",
                    help="uni = always arrive from one side; alt = alternate (the control)")
    ap.add_argument("--approach-us", type=int, default=80, dest="approach_us",
                    help="how far past the target to back off before arriving")
    ap.add_argument("--cycles", type=int, default=10)
    ap.add_argument("--poses", nargs="+", metavar="NAME:PAN,TILT")
    ap.add_argument("--axis", choices=sorted(CH), default="pan", help="gain mode")
    ap.add_argument("--gain-from", type=int, default=1300, dest="gain_from")
    ap.add_argument("--gain-to", type=int, default=1700, dest="gain_to")
    ap.add_argument("--gain-steps", type=int, default=9, dest="gain_steps")
    ap.add_argument("--out", help="JSONL path (default: runs/<mode>-<timestamp>.jsonl)")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="exercise the sequencing without commanding the servos")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.summarize:
        # Only the ANALYSIS choices may be overridden here. Nothing about how
        # the frames were acquired can be, because nothing about that can be
        # changed after the fact.
        if args.box:
            parse_roi(args.box, S.ROWS, S.COLS)
        return report(*S.load(args.summarize),
                      override={"polarity": "cold" if args.cold else None,
                                "box": args.box,
                                "bad_pixels": parse_bad_pixels(args.bad_pixels),
                                "min_contrast": (args.min_contrast
                                                 if args.min_contrast != 5.0
                                                 else None)})
    if not args.base_url or not args.mode:
        ap.error("need base_url and --mode (or --summarize FILE)")
    if not args.box:
        ap.error("--box is required: a whole-frame centroid follows whatever is "
                 "hottest, which on this bench is usually the board")
    parse_roi(args.box, S.ROWS, S.COLS)      # fail before touching the servos
    if args.flipv or args.fliph:
        # Ask the board what it is sending BEFORE recording anything: a double
        # rotation is invisible in the data and would only surface as a
        # registration that never converges.
        # take() is consume-once and the module is 4 Hz, so /thermal returning
        # `frame: null` is NORMAL for part of every frame period. A one-shot
        # probe that read a null and concluded "wire order" would wave the run
        # through, and the whole afternoon would be recorded doubly rotated —
        # with the report only saying so once the hardware was packed away.
        # So: poll for a real frame, and FAIL CLOSED if none arrives.
        url = f"{args.base_url.rstrip('/')}/thermal"
        seen = None
        for _ in range(int(4 * FRAME_PERIOD_S / POLL_S) + 10):
            try:
                f = fetch(url).get("frame")
            except OSError as e:
                ap.error(f"cannot read {url} to check orientation ({e}); "
                         f"the flips cannot be verified, so the run is refused")
            if f:
                seen = f.get("orientation", "wire")
                break
            time.sleep(POLL_S)
        if seen is None:
            ap.error(f"no thermal frame from {url} within four frame periods, "
                     f"so the board's orientation is unknown and --flipv/--fliph "
                     f"cannot be checked — refusing rather than risking a "
                     f"doubly-rotated recording")
        clash = orientation_conflict(seen, args.flipv, args.fliph)
        if clash:
            ap.error(clash)
    if args.mode == "repeat" and not args.poses:
        ap.error("--mode repeat needs --poses NAME:pan_us,tilt_us ...")
    if args.n < 2:
        ap.error("--n must be at least 2; a block of one has no spread to report")
    # Every timing input, checked BEFORE anything is commanded. A negative
    # --settle used to pass argparse, let Head.goto() send its first command,
    # and then raise ValueError out of time.sleep() — which is not ServoAborted,
    # so the abort path never ran, retreat() was never called, and the head was
    # left holding whatever intermediate width the back-off had just commanded.
    for name, v in (("--settle", args.settle), ("--approach-us", args.approach_us),
                    ("--static-n", args.static_n), ("--cycles", args.cycles),
                    ("--gain-steps", args.gain_steps)):
        if v < 0:
            ap.error(f"{name} must not be negative (got {v})")
    if args.mode == "repeat" and args.cycles < 1:
        ap.error("--cycles must be at least 1")
    if args.mode == "gain" and args.gain_steps < 2:
        ap.error("--gain-steps must be at least 2 to fit a gain")

    poses = parse_poses(args.poses) if args.poses else []
    out = args.out or os.path.join(
        "runs", f"{args.mode}-{datetime.datetime.now():%Y%m%dT%H%M%S}.jsonl")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    rec = Recorder(out)
    rec.write(manifest_for(args, poses))
    head = Head(args.base_url, args.settle, args.approach_us, args.dry_run)

    print(f"recording to {out}")
    try:
        run(args, rec, head)
    except ServoAborted as e:
        print(f"\nSERVO ABORT: {e}")
        head.retreat("servo abort")
        rec.write({"kind": "frame", "block": "aborted", "ok": False, "reason": str(e)})
        rec.close()
        print(f"partial recording kept at {out} — summarize it to see how far it got")
        return 2
    except KeyboardInterrupt:
        head.retreat("Ctrl-C")
        rec.close()
        print(f"\ninterrupted; partial recording kept at {out}")
        return 130
    finally:
        rec.close()

    print(f"\n{rec.n_ok} frames recorded, {rec.n_bad} failed captures")
    return report(*S.load(out))


if __name__ == "__main__":
    sys.exit(main())
