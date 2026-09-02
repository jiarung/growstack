#!/usr/bin/env python3
"""Focus scoring for the imaging head — numbers instead of squinting.

Human eyes are terrible at judging focus on low-res or downscaled images (and a
browser scales everything). This scores sharpness objectively: variance of the
Laplacian over the center crop — in-focus images score HIGH, defocused ones
collapse. The absolute number is meaningless; the CURVE across distances is the
instrument: shoot the same textured target at each distance, the peak is your
focal distance. Compare only same-resolution sources (the score scales with
resolution) — in practice, always /capture from the same board.

    ./focus_score.py shot*.jpg                        # score files, sorted
    ./focus_score.py http://<ip>/capture              # fetch a still and score it
    ./focus_score.py shot1.jpg http://<ip>/capture    # sources mix freely

    ./focus_score.py --sweep 40 http://<ip>/capture   # 40 shots, distance vs score

--sweep is the real instrument, and it needs no ruler: the board's VL53L0X
reports the distance with every capture (X-Range-Mm), so you just hold a
TEXTURED target (printed text works best) and walk it slowly toward or away
from the lens while this samples. The peak of the resulting curve is where
the lens actually focuses; a FLAT curve means focus never happens at any
distance — the lens film is still on, or the AF firmware was never uploaded
and the VCM is parked somewhere useless.
Needs Pillow:  python3 -m pip install pillow
"""
import io
import sys
import urllib.request

try:
    from PIL import Image, ImageFilter, ImageStat
except ImportError:
    print("needs Pillow:  python3 -m pip install pillow", file=sys.stderr)
    sys.exit(1)

# 3x3 Laplacian; offset centers the zero-response at 128 so negatives survive
# the uint8 clamp — variance is offset-invariant, so the score is unaffected
LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0],
                               scale=1, offset=128)


def fetch(url):
    """-> (bytes, range_mm|None, reason). The board reports distance per
    capture in X-Range-Mm; an unmeasurable one arrives as
    'invalid:<reason>' — kept and shown, because "invalid" alone cannot tell
    apart "the target left the sensor's narrow cone" (no_distance), "the read
    failed" (driver_err) and "the scene moved across the exposure" (moved:a->b,
    which the board detects by bracketing the frame with two readings)."""
    with urllib.request.urlopen(url, timeout=30) as r:
        data = r.read()
        raw = r.headers.get("X-Range-Mm", "(header missing)")
    try:
        return data, int(raw), ""
    except ValueError:
        return data, None, raw


def jpeg_fault(data):
    """None if this looks like a whole JPEG, else why not.

    Transfers from the board DO die mid-frame (seen from the first bring-up
    day onward), and a truncated file must be reported as a transfer problem
    rather than surfacing as a decoder traceback three frames deeper."""
    if len(data) < 4:
        return f"empty ({len(data)} B)"
    if data[:2] != b"\xff\xd8":
        return "not a JPEG (bad SOI)"
    if data[-2:] != b"\xff\xd9":
        return f"truncated ({len(data)} B, no EOI)"
    return None


def score_image(img):
    w, h = img.size
    # center 50% crop FIRST, grayscale after — judge the subject, not the
    # corners (lens field curvature and vignetting would pollute a full-frame
    # score), and don't convert pixels the crop is about to throw away
    img = img.crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4)).convert("L")
    return ImageStat.Stat(img.filter(LAPLACIAN)).var[0]


def score(source):
    """source: a file path or an http(s) URL -> Laplacian-variance sharpness."""
    if source.startswith(("http://", "https://")):
        source = io.BytesIO(fetch(source)[0])
    return score_image(Image.open(source))


def score_bytes(data):
    return score_image(Image.open(io.BytesIO(data)))


def sweep(n, url):
    """Sample n captures, print them ordered BY DISTANCE — the focus curve.

    Samples whose distance the sensor refused are kept out of the curve and
    counted instead: a stretch of them usually means the target left the
    VL53L0X's cone, which is a fact about the sweep, not about focus."""
    rows, invalid, broken, reasons = [], 0, 0, {}
    print(f"sampling {n} captures — move the target slowly through the range...")
    for i in range(n):
        data, mm, why = fetch(url)
        # a dropped transfer must cost ONE sample, never the whole sweep
        fault = jpeg_fault(data)
        if fault is None:
            try:
                s = score_bytes(data)
            except OSError as e:
                fault = f"decode failed: {e}"
        if fault is not None:
            broken += 1
            reasons[fault] = reasons.get(fault, 0) + 1
            print(f"  {i + 1:3d}/{n}  image {fault}")
            continue
        if mm is None:
            invalid += 1
            reasons[why] = reasons.get(why, 0) + 1
            print(f"  {i + 1:3d}/{n}  {why:<18} score {s:8.1f}")
        else:
            rows.append((mm, s))
            print(f"  {i + 1:3d}/{n}  {mm:5d} mm           score {s:8.1f}")
    if not rows:
        print(f"\nno usable samples ({invalid} without a distance, {broken} broken images)")
        for why, cnt in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {cnt:3d}x {why}")
        return 1
    rows.sort()
    peak = max(rows, key=lambda r: r[1])
    width = max(40, 0)
    hi = peak[1] or 1.0
    print("\ndistance  score      (bar = relative sharpness)")
    for mm, s in rows:
        bar = "#" * int(width * s / hi)
        print(f"{mm:6d}mm {s:9.1f}  {bar}")
    spread = (max(s for _, s in rows) / max(min(s for _, s in rows), 1e-9))
    print(f"\npeak: {peak[1]:.1f} at {peak[0]} mm   "
          f"({len(rows)} usable, {invalid} no-distance, {broken} broken images)")
    for why, cnt in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {cnt:3d}x {why}")
    if spread < 2.0:
        print("FLAT curve (peak/min < 2x) — focus does not happen at any sampled\n"
              "distance: lens film still on, or the AF firmware was never uploaded.")
    else:
        print("Peaked curve — that distance is where this lens focuses.")
    return 0


def main(argv):
    if argv and argv[0] == "--grab":   # legacy spelling; a URL argument suffices
        argv = argv[1:]
    if len(argv) >= 3 and argv[0] == "--sweep":
        return sweep(int(argv[1]), argv[2])
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    scored, bad = [], []
    for p in argv:
        try:
            scored.append((score(p), p))
        except OSError as e:          # truncated download, unreadable file
            bad.append((p, str(e)))
    for s, src in sorted(scored, reverse=True):
        print(f"{s:10.1f}  {src}")
    for src, why in bad:
        print(f"{'--':>10}  {src}  ({why})")
    print("(higher = sharper; compare across distances, not in the absolute)")
    return 1 if bad and not scored else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
