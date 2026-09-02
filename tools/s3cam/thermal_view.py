#!/usr/bin/env python3
"""See the thermal frame — as a terminal heatmap, and optionally as a PNG.

    ./thermal_view.py http://<ip>/thermal          # ASCII heatmap, no deps
    ./thermal_view.py http://<ip>/thermal --png t.png   # also write an image
    ./thermal_view.py frame.json                   # a saved /thermal response
    ./thermal_view.py http://<ip>/thermal --watch  # refresh until Ctrl-C
    ./thermal_view.py ... --flipv --fliph          # fix the image orientation

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


def show(doc, png=None, flipv=False, fliph=False):
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
    px = orient(px, rows, cols, flipv, fliph)
    lo, hi = min(px), max(px)
    print(render(px, rows, cols, lo, hi))
    warn = "" if f.get("checksum_ok", True) else "   [checksum UNVERIFIED]"
    print(f"seq {f['seq']}  {lo:.2f}..{hi:.2f} C   Ta {f.get('ta_c')} C{warn}")
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
    src = argv[0]
    png = argv[argv.index("--png") + 1] if "--png" in argv else None
    flipv, fliph = "--flipv" in argv, "--fliph" in argv
    if "--watch" not in argv:
        return show(fetch(src), png, flipv, fliph)
    import time
    try:
        while True:
            print("\x1b[H\x1b[J", end="")   # home + clear, so it redraws in place
            try:
                show(fetch(src), png, flipv, fliph)
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
