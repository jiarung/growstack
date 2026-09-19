#!/usr/bin/env python3
"""See the thermal frame — as a terminal heatmap, and optionally as a PNG.

    ./thermal_view.py http://<ip>/thermal          # ASCII heatmap, no deps
    ./thermal_view.py http://<ip>/thermal --png t.png   # also write an image
    ./thermal_view.py frame.json                   # a saved /thermal response
    ./thermal_view.py http://<ip>/thermal --watch  # refresh until Ctrl-C
    ./thermal_view.py ... --flipv --fliph          # fix the image orientation
    ./thermal_view.py ... --roi 8,12,16,20         # measure ONE component
    ./thermal_view.py ... --watch --log soak.csv   # log that region over time
    ./thermal_view.py ... --roi 8,12,16,20 --centroid   # sub-pixel target position
    ./thermal_view.py ... --centroid --cold        # the target is COLDER than the room

--roi r0,c0,r1,c1 (inclusive) is how this becomes an instrument rather than a
picture. Aimed at the board, the frame contains the SoC, the regulator and the
camera module at once; a whole-frame max tells you the hottest of those, which
is not the question when you want to know what ONE of them is doing before and
after a change. Point it, read `hot @ r,c` to find the part, then box it.

--log appends a CSV row per refresh (time, Ta, frame min/max, ROI min/mean/max)
so a before/after soak is a diff of two files, not two remembered numbers.

--centroid answers a different question from `hot @ r,c`: not "which pixel is
hottest" but "where IS the warm thing", to a fraction of a pixel. That is the
Phase 3 measurement — the servos' repeatability lands at about one thermal
pixel, so an integer answer cannot resolve it. It prints only; the acceptance
runs record raw frames instead (scan_repeat.py), so the CSV format above is
unchanged and old soak logs stay comparable.

32x24 is small enough that a terminal IS a reasonable display: two rows of
pixels per line of text (upper/lower half-blocks) gives a square-ish 32x12
image in colour, and every value is on screen at once. The PNG path exists for
sharing and for looking at fine structure; it upscales with nearest-neighbour
because interpolation invents detail a 768-pixel sensor does not have.

Colour maps the frame's OWN min..max, not an absolute scale: a thermal frame
of a room spans a couple of degrees, and a fixed scale would render it flat.
The range is always printed, so a picture is never read without its numbers.

--flipv/--fliph exist because the module's scan order is not documented and the
firmware stores pixels in wire order deliberately. Point a warm hand at a known
corner, see where it lands, and the right flags are the answer — put THAT in
the firmware only once hardware has settled it, not as a guess today.
"""
import json
import statistics
import sys
import urllib.request

# 24-step ramp through the usual thermal look: black -> blue -> red -> yellow
# -> white. Built as 256-colour ANSI so it works over ssh without truecolor.
RAMP = [16, 17, 18, 19, 20, 21, 26, 32, 38, 44, 50, 51,
        86, 121, 156, 191, 226, 220, 214, 208, 202, 196, 203, 231]


def fetch(src):
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=15) as r:
            return json.loads(r.read())
    with open(src) as f:
        return json.load(f)


def cell(v, lo, hi):
    if hi <= lo:
        return RAMP[len(RAMP) // 2]
    i = int((v - lo) / (hi - lo) * (len(RAMP) - 1))
    return RAMP[max(0, min(len(RAMP) - 1, i))]


def render(px, rows, cols, lo, hi):
    """Two pixel rows per text line via the upper-half-block glyph."""
    out = []
    for r in range(0, rows - 1, 2):
        line = []
        for c in range(cols):
            top = cell(px[r * cols + c], lo, hi)
            bot = cell(px[(r + 1) * cols + c], lo, hi)
            line.append(f"\x1b[38;5;{top}m\x1b[48;5;{bot}m▀")
        out.append("".join(line) + "\x1b[0m")
    return "\n".join(out)


def orient(px, rows, cols, flipv, fliph):
    """Applied once, so the terminal view and the PNG can never disagree."""
    grid = [px[r * cols:(r + 1) * cols] for r in range(rows)]
    if flipv:
        grid.reverse()
    if fliph:
        grid = [row[::-1] for row in grid]
    return [v for row in grid for v in row]


def orientation_conflict(frame_orientation, flipv, fliph):
    """-> a warning string when host flips would undo the board's own, else None.

    Firmware from 2026-09-19 rotates the thermal frame on the device, because
    orientation is a property of how the head is MOUNTED and that is a fact
    about the device. A host that then applies --flipv --fliph rotates a second
    time, and two 180-degree rotations are the identity: the frame comes back
    looking completely ordinary, every number is computable, and every one of
    them is about the wrong pixels. Nothing downstream can notice.

    A frame with no `orientation` field predates the change and is wire order,
    so the flags are still how it gets corrected — that case stays silent.
    """
    if frame_orientation != "rot180":
        return None
    if not (flipv or fliph):
        return None
    both = flipv and fliph
    return ("the board already reports orientation=rot180 and "
            + ("--flipv --fliph would rotate it back to wire order"
               if both else "a host flip would mirror it")
            + " — drop the host flips")


def orientation_mismatch(rgb_orientation, thermal_orientation):
    """-> a warning when the two sensors are not in the same frame, else None.

    /cam/tune keeps the camera's hmirror/vflip adjustable at runtime while the
    thermal rotation is compiled in. That is useful while a mounting is being
    decided and dangerous afterwards: turn one of them off and the two images
    are in different coordinate systems, so every box mapped from RGB lands on
    the wrong thermal pixels — silently, because each image on its own still
    looks perfectly sensible.

    An older board reports no RGB tag; that predates the correction and is not
    something to shout about, so it stays quiet.
    """
    if not rgb_orientation or not thermal_orientation:
        return None
    if rgb_orientation == thermal_orientation:
        return None
    return (f"RGB is {rgb_orientation} but the thermal frame is "
            f"{thermal_orientation} — the two are in different coordinate "
            f"systems, so any box mapped between them lands on the wrong "
            f"pixels. Fix /cam/tune, or reflash so both agree.")


def flip_box(box, rows, cols, flipv, fliph):
    """Re-index an inclusive box the way orient() re-indexes the array.

    orient() moves every pixel; a box named in one orientation therefore names
    different pixels in the other. Two places in the viewer learned this the
    hard way — a box dragged on the RGB went through Registration in WIRE
    order while the centroid was computed on the ORIENTED frame, so the two
    agreed only while both flips were off.

    Both transforms are involutions, so this converts in either direction. The
    property that matters is that it agrees with orient(): the pixels inside
    flip_box(b) of the oriented frame are exactly the pixels inside b of the
    raw one. test_scan_stats.py asserts that rather than trusting it.
    """
    r0, c0, r1, c1 = box
    if flipv:
        r0, r1 = rows - 1 - r1, rows - 1 - r0
    if fliph:
        c0, c1 = cols - 1 - c1, cols - 1 - c0
    return r0, c0, r1, c1


def parse_roi(s, rows, cols):
    """'r0,c0,r1,c1' -> inclusive box, validated against the frame it will index."""
    try:
        r0, c0, r1, c1 = (int(x) for x in s.split(","))
    except ValueError:
        raise SystemExit(f"--roi wants r0,c0,r1,c1 (got {s!r})")
    r0, r1 = min(r0, r1), max(r0, r1)
    c0, c1 = min(c0, c1), max(c0, c1)
    if not (0 <= r0 <= r1 < rows and 0 <= c0 <= c1 < cols):
        raise SystemExit(f"--roi {s} is outside the {rows}x{cols} frame")
    return r0, c0, r1, c1


def roi_stats(px, cols, box):
    r0, c0, r1, c1 = box
    vals = [px[r * cols + c] for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]
    return min(vals), sum(vals) / len(vals), max(vals), len(vals)


# --- sub-pixel target position ----------------------------------------------
# `hot @ r,c` above is an integer argmax: it answers "which pixel is hottest",
# which is the right question when hunting a hot component and the WRONG one
# when measuring whether the mechanism returns to the same place. MG996R's
# +-1-2 deg maps to roughly one thermal pixel (1.72 deg/px pan, 1.46 deg/px
# tilt), so an estimator quantised to whole pixels cannot resolve the very
# thing Phase 3 exists to measure.
MIN_CONTRAST_C = 5.0     # below this the "target" is not distinguishable from room
MIN_SUPPORT_PX = 4       # a support this small is noise, not an object


def centroid(px, rows, cols, box=None,
             min_contrast=MIN_CONTRAST_C, min_support=MIN_SUPPORT_PX,
             polarity="hot"):
    """Intensity-weighted centroid of the target -> sub-pixel (r, c).

    `polarity` picks which way the target stands out. "cold" is implemented by
    NEGATING the frame and running the identical estimator: every step below is
    an inequality about distance from the background, and negation turns each
    one into its mirror exactly — the median negates, the max becomes the min,
    "at least the half-max above background" becomes "at most the half-min
    below". Writing a second copy with the comparisons flipped would be four
    opportunities to flip three of them, and the two copies would then disagree
    only on the frames that matter.

    A cold target is a real case: a chilled cup is as good a registration mark
    as a hot one and easier to keep still, but the hot estimator does not
    merely miss it — it locks onto whatever IS hottest, which on this bench is
    usually the board, and reports a confident centroid for the wrong object.

    Always returns a dict carrying its own validity, never a bare pair: a
    rejected frame that returned (0, 0) or the frame centre would enter a
    dataset looking exactly like a measurement.
    """
    if polarity not in ("hot", "cold"):
        raise ValueError(f"polarity must be 'hot' or 'cold', got {polarity!r}")
    if polarity == "cold":
        out = centroid([-v for v in px], rows, cols, box,
                       min_contrast, min_support, "hot")
        # Back to real temperatures for anything a human reads. contrast is a
        # magnitude and stays positive; tbg and tth are temperatures and negate.
        out["tbg"], out["tth"] = -out["tbg"], -out["tth"]
        return out

    out = {"ok": False, "reason": "", "r": None, "c": None,
           "n": 0, "contrast": 0.0, "tbg": 0.0, "tth": 0.0}
    r0, c0, r1, c1 = box if box else (0, 0, rows - 1, cols - 1)
    win = [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]

    # Background from the WHOLE frame, not the window: the window is chosen to
    # contain the target, so its own median is already contaminated by it.
    tbg = statistics.median(px)
    kmax = max(win, key=lambda rc: px[rc[0] * cols + rc[1]])
    tmax = px[kmax[0] * cols + kmax[1]]
    out["tbg"], out["contrast"] = tbg, tmax - tbg
    if out["contrast"] < min_contrast:
        out["reason"] = f"low_contrast:{out['contrast']:.1f}C"
        return out

    # Half-max ABOVE BACKGROUND. Background-relative because the MLX90640's
    # Ta-dependent offset moves every pixel together — an absolute threshold
    # would let the support size drift with room temperature and manufacture
    # displacement out of nothing. Half-max (~FWHM) because it stays stable
    # while a cooling target loses contrast.
    tth = tbg + 0.5 * out["contrast"]
    out["tth"] = tth

    # 4-connected component containing the peak, NOT every pixel over the
    # threshold: a second warm object inside the window would otherwise drag
    # the centroid silently, and nothing in the summary would show it.
    inwin = lambda r, c: r0 <= r <= r1 and c0 <= c <= c1
    seen, stack, sup = {kmax}, [kmax], []
    while stack:
        r, c = stack.pop()
        sup.append((r, c))
        for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if (nr, nc) in seen or not inwin(nr, nc):
                continue
            if px[nr * cols + nc] >= tth:
                seen.add((nr, nc))
                stack.append((nr, nc))
    out["n"] = len(sup)
    if out["n"] < min_support:
        out["reason"] = f"support_too_small:{out['n']}px"
        return out

    # Weight is height ABOVE THE THRESHOLD, not above background. This is what
    # makes the estimate sub-pixel stable: a pixel flickering across the
    # threshold enters and leaves with weight ~0, contributing a continuous
    # change instead of a step. Weighting by (T - tbg) would make that same
    # pixel arrive already carrying half the contrast.
    wsum = sr = sc = 0.0
    for r, c in sup:
        w = px[r * cols + c] - tth
        wsum += w
        sr += w * r
        sc += w * c
    if wsum <= 0:
        out["reason"] = "zero_weight"
        return out
    out["ok"], out["reason"] = True, "ok"
    out["r"], out["c"] = sr / wsum, sc / wsum
    return out


def show(doc, png=None, flipv=False, fliph=False, roi=None, log=None, cen=False,
         cold=False):
    f = doc.get("frame")
    if not f:
        s = doc.get("stream", {})
        print("no frame yet — stream says:", json.dumps(s))
        return 1
    rows, cols = f.get("rows", 24), f.get("cols", 32)
    px = f["px"]
    if len(px) != rows * cols:
        print(f"frame says {rows}x{cols} but carries {len(px)} values", file=sys.stderr)
        return 1
    # Same guard scan_repeat.py enforces, and for the same reason: two
    # 180-degree rotations are the identity, so a host flip over a board that
    # already corrects itself produces a completely ordinary-looking frame of
    # the wrong pixels. This is an AIMING tool — the box chosen here is the box
    # the acceptance run is given — so it cannot be the one path that stays
    # quiet about it.
    clash = orientation_conflict(f.get("orientation", "wire"), flipv, fliph)
    if clash:
        print(f"\x1b[1;31m!! {clash}\x1b[0m")

    px = orient(px, rows, cols, flipv, fliph)
    lo, hi = min(px), max(px)
    print(render(px, rows, cols, lo, hi))
    warn = "" if f.get("checksum_ok", True) else "   [checksum UNVERIFIED]"
    # WHERE the extreme is, not just how extreme. Aimed at a board that is the
    # whole diagnosis — which component is cooking is a coordinate, not a
    # number — and aimed at a CHILLED registration target it is the difference
    # between being pointed at the target and being pointed at the warmest
    # unrelated object in the room, which is exactly what `hot @` did in cold
    # mode while the centroid below was quietly correct.
    k = px.index(lo if cold else hi)
    label = "cold @" if cold else "hot @"
    print(f"seq {f['seq']}  {lo:.2f}..{hi:.2f} C   "
          f"{label} r{k // cols} c{k % cols}   Ta {f.get('ta_c')} C{warn}")

    box = parse_roi(roi, rows, cols) if roi else None
    if box:
        rmin, rmean, rmax, n = roi_stats(px, cols, box)
        r0, c0, r1, c1 = box
        print(f"roi r{r0}-{r1} c{c0}-{c1} ({n}px)   "
              f"min {rmin:.2f}  mean {rmean:.2f}  max {rmax:.2f} C")
    if cen:
        # A live sub-pixel aiming instrument: watch this number sit still with
        # the servos untouched and you are reading the noise floor with your
        # own eyes, before committing to a long run that assumes it is small.
        cd = centroid(px, rows, cols, box, polarity="cold" if cold else "hot")
        if cd["ok"]:
            print(f"centroid r{cd['r']:.2f} c{cd['c']:.2f}   "
                  f"{cd['n']}px {'under' if cold else 'over'} {cd['tth']:.2f} C   "
                  f"contrast {cd['contrast']:.2f} C {'(cold)' if cold else ''}")
        else:
            print(f"centroid --  ({cd['reason']})")
    if log:
        # header only when the file is new, so --log can append across runs and
        # a before/after soak stays one continuous, self-describing series
        import csv, datetime, os
        new = not os.path.exists(log) or os.path.getsize(log) == 0
        with open(log, "a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["time", "seq", "ta_c", "frame_min", "frame_max",
                            "roi_min", "roi_mean", "roi_max"])
            row = [datetime.datetime.now().isoformat(timespec="seconds"),
                   f["seq"], f.get("ta_c"), f"{lo:.2f}", f"{hi:.2f}"]
            row += [f"{rmin:.2f}", f"{rmean:.2f}", f"{rmax:.2f}"] if box else ["", "", ""]
            w.writerow(row)
    if png:
        try:
            from PIL import Image
        except ImportError:
            print("--png needs Pillow: python3 -m pip install pillow", file=sys.stderr)
            return 1
        img = Image.new("RGB", (cols, rows))
        for i, v in enumerate(px):
            n = 0 if hi <= lo else (v - lo) / (hi - lo)
            # same black->blue->red->yellow->white feel, computed continuously
            r = int(255 * min(1.0, max(0.0, 2.2 * n - 0.5)))
            g = int(255 * min(1.0, max(0.0, 2.0 * n - 1.0)))
            b = int(255 * min(1.0, max(0.0, 1.6 * n if n < 0.4 else 1.4 - 2.0 * n)))
            img.putpixel((i % cols, i // cols), (r, g, b))
        # nearest-neighbour: a 32x24 sensor has no finer detail to reveal, and
        # smoothing would draw structure the measurement never contained
        img.resize((cols * 16, rows * 16), Image.NEAREST).save(png)
        print(f"wrote {png} ({cols * 16}x{rows * 16}, nearest-neighbour upscale)")
    return 0


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    def opt(name):
        return argv[argv.index(name) + 1] if name in argv else None

    src = argv[0]
    png, roi, log = opt("--png"), opt("--roi"), opt("--log")
    flipv, fliph = "--flipv" in argv, "--fliph" in argv
    cen = "--centroid" in argv
    cold = "--cold" in argv
    if "--watch" not in argv:
        return show(fetch(src), png, flipv, fliph, roi, log, cen, cold)
    import time
    try:
        while True:
            print("\x1b[H\x1b[J", end="")   # home + clear, so it redraws in place
            try:
                show(fetch(src), png, flipv, fliph, roi, log, cen, cold)
            except OSError as e:
                # a dropped frame or a Wi-Fi hiccup must not end a watch that
                # is meant to run while somebody moves things in front of the
                # sensor — report it in place and try again next tick
                print("fetch failed:", e)
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
