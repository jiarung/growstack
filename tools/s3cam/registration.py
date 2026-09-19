#!/usr/bin/env python3
"""Calibrating the RGB-to-thermal mapping: solve it, score it, store it.

observe.Registration is the TRANSFORM — three numbers and the algebra that
applies them. This is the calibration around it: where those three numbers come
from, how well they fit, and what it means to keep one.

Until now they came from dragging sliders until the overlay looked right, and
the result was copied into a note by hand. Three things were missing and all
three are the difference between an alignment and a measurement:

  no solver    — "looks right" has no repeatable definition, and two people
                 (or the same person twice) produce different numbers.
  no residual  — the roadmap's Phase 5 deliverable is registration STABILITY,
                 which is a number. Eyeballing produces no number at all, so
                 there was nothing to be stable.
  no record    — a calibration only means something next to the distance it
                 was found at, and nothing persisted the pair. This repo has
                 already lost six days to a definition that lived in two
                 places; one that lives in a person's notes is worse.

WHY PARALLAX MAKES THIS PER-DISTANCE
The two sensors sit side by side, so a point at range Z appears displaced by
roughly f*B/Z between them: the offset is proportional to 1/Z, not to Z. One
calibration is therefore correct at exactly one distance, which is why the
roadmap refuses a single global homography and why entries here are keyed by
range. It is also why interpolation happens in 1/Z — linear in millimetres
would be wrong in a way that looks plausible in the middle and fails at both
ends.

WHAT THIS DELIBERATELY WILL NOT DO
Extrapolate beyond the measured range, or interpolate from a single entry. The
Photone pipeline paid for both lessons: a model asked for a value it has no
evidence for must refuse, not fall back to something reasonable-looking. A
refusal is visible; a plausible wrong number is not.
"""
import datetime
import json
import math
import os
import subprocess
from collections import namedtuple

ROWS, COLS = 24, 32

# One correspondence: a point identified in BOTH images. (x, y) in RGB pixels,
# (r, c) in thermal pixels — sub-pixel, because centroid() produces sub-pixel
# and rounding it here would put a floor under the residual that has nothing to
# do with the mapping.
Point = namedtuple("Point", "x y r c")

# Four parameters now (sx, sy, dx, dy), so TWO points determine the mapping
# exactly and their residual is zero by construction — a perfect score for a
# fit nothing has checked. Three is the first count with anything left over.
MIN_POINTS = 3
# And the spread gate is per axis, because the axes are now solved separately:
# points strung out horizontally observe sx perfectly and say nothing at all
# about sy. Under the old isotropic model a single pooled spread was enough;
# keeping it would have let a horizontal line of points produce a confident
# vertical scale built from noise.
MIN_SPREAD_PX = 2.0      # thermal pixels, RMS about the mean, in EACH axis

# Quality gates. A calibration worse than this is recorded but must not be
# adopted: at 1.72 deg/px on pan, two thermal pixels of registration error is
# larger than the repeatability Phase 3 is trying to measure.
RESIDUAL_P90_MAX_PX = 1.0
RESIDUAL_MAX_PX = 2.0
# Below this the leftovers carry no information about the model — see diagnose().
NEGLIGIBLE_RESIDUAL_PX = 0.001


def git_rev():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=os.path.dirname(os.path.abspath(__file__)),
                              capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except Exception:                                                # noqa: BLE001
        return None


def _uv(p, rgb_w, rgb_h):
    """RGB pixel -> thermal-centred coordinates, before scale and offset."""
    return (p.x / rgb_w * COLS - COLS / 2.0,
            p.y / rgb_h * ROWS - ROWS / 2.0)


def _fit1d(xs, ys):
    """Least squares y = m*x + b. -> (m, b, spread_of_x) or (None, None, spread)."""
    n = float(len(xs))
    xb, yb = sum(xs) / n, sum(ys) / n
    den = sum((x - xb) ** 2 for x in xs)
    spread = math.sqrt(den / n)
    if den <= 0:
        return None, None, spread
    m = sum((x - xb) * (y - yb) for x, y in zip(xs, ys)) / den
    return m, yb - m * xb, spread


def _slope_through_origin(xs, ys):
    """Least squares for y = m*x with no intercept. See diagnose()."""
    den = sum(x * x for x in xs)
    return None if den <= 0 else sum(x * y for x, y in zip(xs, ys)) / den


def solve(points, rgb_w, rgb_h):
    """Least squares for (sx, sy, dx, dy). -> dict carrying its own validity.

    With the scales independent the problem SEPARATES: the horizontal equations
    c - COLS/2 = sx*u + dx involve only sx and dx, the vertical ones only sy
    and dy. Two ordinary line fits, no shared term, and each axis can be
    refused on its own evidence rather than one pooled verdict standing in for
    both.

    Returns a dict rather than a tuple so a refusal carries its reason. A
    solver that returned unit scales on bad input would put a calibration into
    the record that was never solved.
    """
    out = {"ok": False, "reason": "", "sx": None, "sy": None,
           "dx": None, "dy": None, "n": len(points),
           "spread_u": None, "spread_v": None}
    if len(points) < MIN_POINTS:
        out["reason"] = f"need {MIN_POINTS} points, got {len(points)}"
        return out

    uv = [_uv(p, rgb_w, rgb_h) for p in points]
    us, vs = [t[0] for t in uv], [t[1] for t in uv]
    a = [p.c - COLS / 2.0 for p in points]      # thermal, centred
    b = [p.r - ROWS / 2.0 for p in points]

    sx, dx, su = _fit1d(us, a)
    sy, dy, sv = _fit1d(vs, b)
    out["spread_u"], out["spread_v"] = su, sv

    # Named per axis: "the points are too clustered" is not actionable while
    # "spread them out vertically" is. The failure is a real one for the
    # procedure in registration-plan.md, where the points come from jogging the
    # head so a fixed heat source lands in different parts of the frame —
    # jogging only pan produces a row of points that pins sx perfectly and says
    # nothing whatever about sy.
    thin = [n for n, sp in (("horizontally", su), ("vertically", sv))
            if sp < MIN_SPREAD_PX]
    if thin:
        out["reason"] = (f"points too clustered {' and '.join(thin)} "
                         f"(spread u={su:.2f} v={sv:.2f} px, "
                         f"need {MIN_SPREAD_PX} in each)")
        return out
    for name, v in (("sx", sx), ("sy", sy)):
        if v is None or not (math.isfinite(v) and v > 0):
            # Negative would be a mirror, and a mirror is not this model's to
            # represent — see Registration's boundary note. It means the
            # thermal and the camera disagree about handedness, which is fixed
            # at the source, not absorbed here.
            out["reason"] = (f"degenerate {name}={v!r} — if it wanted to be "
                             f"negative the two images are mirrored, and that "
                             f"is fixed at the sensor, not in the fit")
            return out
    out.update({"ok": True, "reason": "ok", "sx": sx, "sy": sy,
                "dx": dx, "dy": dy})
    return out


def _predict(p, sx, sy, dx, dy, rgb_w, rgb_h):
    u, v = _uv(p, rgb_w, rgb_h)
    return v * sy + ROWS / 2.0 + dy, u * sx + COLS / 2.0 + dx


def residuals(points, sx, sy, dx, dy, rgb_w, rgb_h):
    """Per-point error in THERMAL pixels, between predicted and observed."""
    out = []
    for p in points:
        rh, ch = _predict(p, sx, sy, dx, dy, rgb_w, rgb_h)
        out.append(math.hypot(p.r - rh, p.c - ch))
    return out


def quality(points, sx, sy, dx, dy, rgb_w, rgb_h):
    """How well this mapping actually fits. The number Phase 5 asks for.

    Reports the maximum next to the P90 on purpose. With a handful of points a
    P90 interpolates near the top of the sample anyway, and a single corner
    that is badly wrong is exactly the failure a percentile hides — it is also
    the one that matters, because the corners are where a plant sits when the
    frame is full.
    """
    res = residuals(points, sx, sy, dx, dy, rgb_w, rgb_h)
    n = len(res)
    out = {"n": n, "p90": None, "max": None, "rms": None,
           "bias_r": None, "bias_c": None}
    if not n:
        return out
    srt = sorted(res)
    k = (n - 1) * 0.9
    lo, hi = math.floor(k), math.ceil(k)
    out["p90"] = srt[int(k)] if lo == hi else srt[lo] + (srt[hi] - srt[lo]) * (k - lo)
    out["max"] = srt[-1]
    out["rms"] = math.sqrt(sum(x * x for x in res) / n)
    # Signed mean error per axis. A residual that is all bias is a mapping that
    # is simply offset — fixable — while the same magnitude spread randomly is
    # noise in the correspondences and means the points must be taken again.
    dr = dc = 0.0
    for p in points:
        rh, ch = _predict(p, sx, sy, dx, dy, rgb_w, rgb_h)
        dr += p.r - rh
        dc += p.c - ch
    out["bias_r"], out["bias_c"] = dr / n, dc / n
    return out


def diagnose(points, sx, sy, dx, dy, rgb_w, rgb_h):
    """Name the unmodelled term, when there is one. The boundary, measured.

    A four-parameter fit leaves rotation and lens distortion unrepresented, and
    both of them announce themselves in the SHAPE of what is left over rather
    than its size. Decomposing each residual about the frame centre separates
    them:

      TANGENTIAL, growing linearly with radius -> a ROTATION. That relation is
        exactly linear, so the slope through the origin IS the angle in radians
        for small angles: 0.05 is about 3 degrees of mounting twist. The fix is
        mechanical; no parameter here will take it up.
      RADIAL -> lens DISTORTION, or a field of view the linear model does not
        describe. Reported as the fraction of residual ENERGY pointing radially
        rather than as a slope, because real distortion grows as the cube of
        radius and a straight line fitted to it understates it by an order of
        magnitude — measured: a distortion producing 30 px of error returned a
        "slope" of 0.047, indistinguishable from noise.

    Neither dominant, with the residual still over the gate, means what is left
    is scatter: take the correspondences again, more carefully.
    """
    tans, rads, radii = [], [], []
    for p in points:
        rh, ch = _predict(p, sx, sy, dx, dy, rgb_w, rgb_h)
        er, ec = p.r - rh, p.c - ch
        pr, pc = p.r - ROWS / 2.0, p.c - COLS / 2.0
        rad = math.hypot(pr, pc)
        if rad < 1e-9:
            continue            # the centre has no radial direction to speak of
        rads.append((er * pr + ec * pc) / rad)
        tans.append((-er * pc + ec * pr) / rad)
        radii.append(rad)
    out = {"n": len(radii), "rotation_rad": None, "radial_frac": None,
           "mean_tangential_px": None, "hint": ""}
    if len(radii) < 3:
        out["hint"] = "too few off-centre points to tell"
        return out
    out["mean_tangential_px"] = sum(tans) / len(tans)
    # Through the origin, both of them. A rotation displaces nothing at the
    # centre of rotation and distortion vanishes on axis, so the intercept is
    # zero by construction — leaving it free adds a parameter that can only
    # absorb signal. Measured: with a free intercept a planted 0.050 rad came
    # back as 0.063, a 26% overstatement of a mounting error somebody would
    # then go and chase.
    if sum(r * r for r in radii) <= 0:
        out["hint"] = "all points at the centre — cannot separate the terms"
        return out
    out["rotation_rad"] = _slope_through_origin(radii, tans)
    energy = sum(r * r + t * t for r, t in zip(rads, tans))
    # A floor, not a zero check. Below this there is no pattern to name: the
    # centroid's own scatter is 0.05 px on a good bench and Stage 2.1's gate is
    # 0.1, so a residual two orders under that is arithmetic noise — and
    # normalising it still yields a fraction between 0 and 1, which is how a
    # bare `energy <= 0` guard let a PERFECT fit be reported as lens
    # distortion with complete confidence.
    if math.sqrt(energy / len(radii)) < NEGLIGIBLE_RESIDUAL_PX:
        out["radial_frac"] = 0.0
        out["hint"] = "the fit is exact: nothing left to attribute"
        return out
    out["radial_frac"] = sum(r * r for r in rads) / energy
    deg = math.degrees(out["rotation_rad"])
    # Rotation first: it is the one with an exact signature and a mechanical
    # fix. A degree of twist is already half a thermal pixel at the corners.
    if abs(deg) > 1.0 and out["radial_frac"] < 0.5:
        out["hint"] = (f"about {deg:.1f} deg of rotation between the two images "
                       f"— mechanical, this model has no term for it")
    elif out["radial_frac"] > 0.7:
        out["hint"] = ("residual points radially — lens distortion or a field "
                       "of view the linear model does not describe")
    else:
        out["hint"] = "no rotation or radial pattern: what is left is scatter"
    return out


class Calibration:
    """The versioned record: entries keyed by range, and the rules for reading it.

    Orientation is part of an entry's identity, not a note on it. A mapping
    found while the thermal frame was flipped describes different pixels once
    it is not, and an entry that silently applied under both would be wrong
    exactly half the time.
    """

    VERSION = 1

    def __init__(self, entries=None, flipv=False, fliph=False):
        self.entries = sorted(entries or [], key=lambda e: e["range_mm"])
        self.flipv, self.fliph = flipv, fliph

    # --- building -----------------------------------------------------------

    def add(self, range_mm, points, rgb_w, rgb_h, note=""):
        """Solve from correspondences and append. -> the new entry, or a refusal.

        `range_mm` is measured by whatever is to hand — the rangefinder when it
        is on the bus, a tape measure when it is not. It is an INPUT, not a
        reading: the calibration is worthless without it, and waiting for the
        sensor to come back would mean recording nothing in the meantime.
        """
        if not (isinstance(range_mm, (int, float)) and math.isfinite(range_mm)
                and range_mm > 0):
            return {"ok": False, "reason": f"range_mm must be positive, got {range_mm!r}"}
        sol = solve(points, rgb_w, rgb_h)
        if not sol["ok"]:
            return {"ok": False, "reason": sol["reason"]}
        q = quality(points, sol["sx"], sol["sy"], sol["dx"], sol["dy"],
                    rgb_w, rgb_h)
        dg = diagnose(points, sol["sx"], sol["sy"], sol["dx"], sol["dy"],
                      rgb_w, rgb_h)
        entry = {
            "range_mm": float(range_mm),
            "sx": sol["sx"], "sy": sol["sy"], "dx": sol["dx"], "dy": sol["dy"],
            "anisotropy": sol["sx"] / sol["sy"],
            "diagnose": dg,
            "rgb_w": rgb_w, "rgb_h": rgb_h,
            "flipv": self.flipv, "fliph": self.fliph,
            "quality": q,
            "points": [list(p) for p in points],   # kept: a fit without its
                                                   # evidence cannot be re-checked
            "t": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "git_rev": git_rev(), "note": note,
            "gate": {"p90_max_px": RESIDUAL_P90_MAX_PX, "max_px": RESIDUAL_MAX_PX},
            "adoptable": bool(q["p90"] is not None
                              and q["p90"] <= RESIDUAL_P90_MAX_PX
                              and q["max"] <= RESIDUAL_MAX_PX),
        }
        self.entries = sorted(self.entries + [entry], key=lambda e: e["range_mm"])
        return {"ok": True, "entry": entry}

    # --- reading ------------------------------------------------------------

    def for_distance(self, range_mm):
        """-> {ok, scale, dx, dy, how, reason}. Refuses rather than guesses.

        `how` is part of the answer: a caller that cannot tell an exact entry
        from an interpolated one cannot judge how much to trust the number, and
        this repo has a documented incident about a value whose provenance was
        only in prose.
        """
        usable = [e for e in self.entries if e.get("adoptable")]
        if not usable:
            return {"ok": False, "how": None,
                    "reason": f"no adoptable entry ({len(self.entries)} recorded)"}
        if range_mm is None:
            return {"ok": False, "how": None,
                    "reason": "no range: the mapping is only defined at a distance"}
        exact = [e for e in usable if abs(e["range_mm"] - range_mm) < 1e-6]
        if exact:
            e = exact[0]
            return {"ok": True, "how": "exact", "reason": "",
                    "sx": e["sx"], "sy": e["sy"],
                    "dx": e["dx"], "dy": e["dy"],
                    "entries": [e["range_mm"]]}
        lo = [e for e in usable if e["range_mm"] < range_mm]
        hi = [e for e in usable if e["range_mm"] > range_mm]
        if not lo or not hi:
            # Outside the measured span. Parallax keeps changing out there and
            # nothing here has seen it do so; an extrapolation would be a
            # confident number with no evidence under it.
            span = (usable[0]["range_mm"], usable[-1]["range_mm"])
            return {"ok": False, "how": None,
                    "reason": f"{range_mm:.0f} mm is outside the measured "
                              f"{span[0]:.0f}..{span[1]:.0f} mm — measure it, "
                              f"do not extrapolate"}
        a, b = lo[-1], hi[0]
        # Interpolate in 1/Z: disparity goes as f*B/Z, so the offsets are
        # linear in inverse range and NOT in range. Scale rides along the same
        # parameter for consistency; it barely moves, and using two different
        # interpolation variables in one mapping is how the middle looks fine
        # while both ends drift.
        ia, ib, iz = 1.0 / a["range_mm"], 1.0 / b["range_mm"], 1.0 / range_mm
        t = (iz - ia) / (ib - ia)
        mix = lambda k: a[k] + (b[k] - a[k]) * t
        return {"ok": True, "how": "interpolated", "reason": "",
                "sx": mix("sx"), "sy": mix("sy"),
                "dx": mix("dx"), "dy": mix("dy"),
                "entries": [a["range_mm"], b["range_mm"]]}

    # --- persistence --------------------------------------------------------

    def to_dict(self):
        return {"version": self.VERSION, "flipv": self.flipv, "fliph": self.fliph,
                "entries": self.entries}

    def save(self, path):
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)     # a crash mid-write must not eat the record
        return path

    @classmethod
    def load(cls, path):
        if not os.path.exists(path):
            return cls()
        with open(path) as fh:
            d = json.load(fh)
        if d.get("version") != cls.VERSION:
            raise SystemExit(f"{path}: version {d.get('version')}, this code "
                             f"writes {cls.VERSION} — migrate it deliberately")
        return cls(d.get("entries"), d.get("flipv", False), d.get("fliph", False))
