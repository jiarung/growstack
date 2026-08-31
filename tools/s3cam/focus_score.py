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

Sweep procedure (phase-1b step 4 / AF verification):
    for each distance 0.3 0.5 0.75 1.0 1.5 2.0 m:
        put a TEXTURED target (printed text) at that distance
        ./focus_score.py http://<ip>/capture          # note distance + score
    -> the distance with the peak score is where the lens actually focuses.
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


def score(source):
    """source: a file path or an http(s) URL -> Laplacian-variance sharpness."""
    if source.startswith(("http://", "https://")):
        source = io.BytesIO(urllib.request.urlopen(source, timeout=30).read())
    img = Image.open(source)
    w, h = img.size
    # center 50% crop FIRST, grayscale after — judge the subject, not the
    # corners (lens field curvature and vignetting would pollute a full-frame
    # score), and don't convert pixels the crop is about to throw away
    img = img.crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4)).convert("L")
    return ImageStat.Stat(img.filter(LAPLACIAN)).var[0]


def main(argv):
    if argv and argv[0] == "--grab":   # legacy spelling; a URL argument suffices
        argv = argv[1:]
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    for s, src in sorted(((score(p), p) for p in argv), reverse=True):
        print(f"{s:10.1f}  {src}")
    print("(higher = sharper; compare across distances, not in the absolute)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
