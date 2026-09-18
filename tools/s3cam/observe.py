#!/usr/bin/env python3
"""One observation: RGB + thermal + distance, paired, as a single object.

    ./observe.py http://<ip>                    # fetch one, print the summary
    ./observe.py http://<ip> --save obs/        # + write the .jpg and .json
    ./observe.py http://<ip> --box 8,12,16,20   # thermal stats for one ROI

Why this is a library before it is a CLI: the next thing on the roadmap is
object detection that reports PER-PLANT numbers, and a detector that reaches
for /capture and /thermal separately has already thrown away the only thing
that makes the pair mean anything — that both describe the same scene at the
same moment from a known distance. So the pairing lives here once, and the
viewer and the detector are both just callers.

Co-timing is a property of the FIRMWARE, not of this script. When the board
bundles the thermal frame into /observation the pair is genuinely co-timed and
the bundle says so. Against firmware that still emits "thermal": null this
falls back to two requests and reports the skew it actually measured, with
co_timed=False. A caller that needs a tight pair must check the flag; it is
never silently promoted to a promise.

And even a board-side bundle is not simultaneous. The sensor runs at 4 Hz, so
the newest COMPLETE frame can be a frame period old. That is reported as a
bound, never as zero — registration at distance only holds while nothing
moved, and a caller cannot judge that without the number.
"""
import collections
import json
import math
import sys
import time
import urllib.error
import urllib.request

ROWS, COLS = 24, 32
FRAME_PERIOD_MS = 250.0   # GY-MCU90640 at 4 Hz: the age of a "newest" frame
# Beyond this the RGB and the thermal frame are not describing one moment in any
# useful sense. It is not an error — the board reports the age truthfully and a
# still scene tolerates far more — but it is the point where a per-plant number
# stops being about the plant in the picture, so it gets said out loud. Two
# frame periods: enough that an ordinary miss does not cry wolf.
STALE_MS = 2 * FRAME_PERIOD_MS
TIMEOUT_S = 10.0

# A mapped box, already clipped to the sensor, plus how much of the box the
# sensor actually covered. Coverage is the whole point: a plant at the edge of
# the thermal field is still worth measuring, but a mean over the 40% that was
# in view must never be handed over looking like a mean over the plant.
ThermalBox = collections.namedtuple(
    "ThermalBox", "r0 c0 r1 c1 coverage")


def _get(url, timeout=TIMEOUT_S):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _get_for(url, capture_id, timeout=TIMEOUT_S):
    """Fetch a held artefact and refuse it if it belongs to another capture.

    The board stamps X-Capture-Id for exactly this, and without the check a
    second client capturing between our requests hands us a JPEG and a matrix
    from different scenes — which looks like a successful fetch and produces a
    per-plant temperature for a plant that is no longer in the picture.
    """
    with urllib.request.urlopen(url, timeout=timeout) as r:
        body, got = r.read(), r.headers.get("X-Capture-Id")
    if got and capture_id and got != capture_id:
        raise ValueError(f"{url} is from capture {got}, not {capture_id} — "
                         "something else is capturing from this board")
    return body


class Registration:
    """Map an RGB box onto thermal pixels.

    Deliberately a plain affine with no distance term. The roadmap is explicit
    that parallax makes a single homography wrong across distances, which is
    why the rangefinder exists — but nothing has MEASURED a second distance
    yet, and a table interpolating between one point is a table pretending.
    The viewer produces these numbers; record them against `ref_mm` so the
    interpolation has something real to be built from.

    ponytail: per-distance interpolation lands when Phase 5 has >= 2 measured
    distances. Until then `ref_mm` is provenance, not a parameter.
    """

    def __init__(self, scale=1.0, dx=0.0, dy=0.0, ref_mm=None):
        # a zero or negative scale has no inverse, and the overlay rectangle is
        # that inverse; rejecting it here keeps every caller from having to
        # know that (the viewer's slider cannot produce one, a URL can)
        if not (math.isfinite(scale) and scale > 0):
            raise ValueError(f"scale must be finite and positive, got {scale!r}")
        if not (math.isfinite(dx) and math.isfinite(dy)):
            raise ValueError(f"dx/dy must be finite, got {dx!r}, {dy!r}")
        self.scale, self.dx, self.dy, self.ref_mm = scale, dx, dy, ref_mm

    def rgb_box_to_thermal(self, box, rgb_w, rgb_h):
        """(x0,y0,x1,y1) in RGB pixels -> ThermalBox, or None.

        None means the box missed the sensor entirely — a real outcome, since
        the thermal camera sees less than the OV5640 does, and a box clamped
        onto the frame edge would report the edge's temperature as the plant's.
        A box that only PARTLY misses is returned clipped, with the fraction it
        covers, because refusing that case would make every edge plant
        unmeasurable while lying about none of them.
        """
        x0, y0, x1, y1 = box
        r0, c0 = self._map(min(x0, x1), min(y0, y1), rgb_w, rgb_h)
        r1, c1 = self._map(max(x0, x1), max(y0, y1), rgb_w, rgb_h)
        # _map works in EDGE coordinates: the sensor spans [0,COLS] x [0,ROWS]
        # and pixel i covers [i, i+1). Mixing that up with the index range
        # 0..COLS-1 is what made a full-frame box report 93% coverage.
        if r1 <= 0 or c1 <= 0 or r0 >= ROWS or c0 >= COLS:
            return None
        cov = self._overlap(r0, r1, ROWS) * self._overlap(c0, c1, COLS)
        r0i = min(ROWS - 1, max(0, int(math.floor(r0))))
        c0i = min(COLS - 1, max(0, int(math.floor(c0))))
        # ceil-1 turns an edge back into the last pixel the box touches; a box
        # thinner than a pixel still touches one, hence the max()
        r1i = max(r0i, min(ROWS - 1, int(math.ceil(r1)) - 1))
        c1i = max(c0i, min(COLS - 1, int(math.ceil(c1)) - 1))
        return ThermalBox(r0i, c0i, r1i, c1i, cov)

    def _map(self, x, y, rgb_w, rgb_h):
        c = (x / rgb_w * COLS - COLS / 2.0) * self.scale + COLS / 2.0 + self.dx
        r = (y / rgb_h * ROWS - ROWS / 2.0) * self.scale + ROWS / 2.0 + self.dy
        return r, c

    @staticmethod
    def _overlap(lo, hi, n):
        """Fraction of [lo,hi] inside the sensor's [0,n]. A point is in or out."""
        if hi <= lo:
            return 1.0 if 0 <= lo <= n else 0.0
        return max(0.0, min(hi, float(n)) - max(lo, 0.0)) / (hi - lo)

    def overlay_rect(self, rgb_w, rgb_h):
        """Where to DRAW the thermal frame over the RGB image, in RGB pixels.

        The exact algebraic inverse of _map, and it exists because the viewer
        had two transforms: one to draw the overlay and one to read a box out
        of it. Two transforms that disagree turn the alignment tool into a
        device for aiming confidently at the wrong pixels — you line the image
        up by eye and the number comes from somewhere else. There is one
        transform now, it lives here, and the browser asks for the rectangle
        rather than deriving it.
        """
        x0 = rgb_w / COLS * ((-COLS / 2.0 - self.dx) / self.scale + COLS / 2.0)
        y0 = rgb_h / ROWS * ((-ROWS / 2.0 - self.dy) / self.scale + ROWS / 2.0)
        return {"x": x0, "y": y0,
                "w": rgb_w / self.scale, "h": rgb_h / self.scale}


class Bundle:
    """One observation. Construct via fetch(); the fields are the contract."""

    def __init__(self, obs, thermal, rgb_jpeg, co_timed, skew_bound_ms):
        # A thermal frame the board CARRIED over from an earlier capture: it
        # names its own capture in `file`, and the board deliberately does not
        # restamp it. Worth knowing separately from age — a 200 ms carry is
        # harmless on a still scene, but "which capture is this" and "how old is
        # it" are different questions and only one of them is about time.
        self.raw = obs
        self.capture_id = obs.get("capture_id")
        self.timestamp = obs.get("timestamp")
        self.time_source = obs.get("time_source")
        self.uptime_ms = obs.get("uptime_ms")
        self.range_mm = obs.get("range_mm")
        self.range_invalid_reason = obs.get("range_invalid_reason")
        tf = (thermal or {}).get("file") or ""
        self.carried = bool(tf) and tf.split(".")[0] != self.capture_id
        rgb = obs.get("rgb") or {}
        self.rgb_w, self.rgb_h = rgb.get("width"), rgb.get("height")
        self.rgb_bytes = rgb.get("bytes")
        self.rgb_jpeg = rgb_jpeg
        self.co_timed = co_timed
        self.skew_bound_ms = skew_bound_ms
        # thermal: None when no complete frame has arrived. Absence is a state,
        # not an error — the UART can be unplugged and everything else is fine.
        self.thermal = self.ta_c = self.thermal_seq = None
        self.checksum_ok = None
        if thermal:
            self.thermal = thermal.get("px") or thermal.get("matrix")
            self.ta_c = thermal.get("ta_c")
            self.thermal_seq = thermal.get("seq")
            self.checksum_ok = thermal.get("checksum_ok")
            n = len(self.thermal or [])
            if n != ROWS * COLS:
                raise ValueError(f"thermal frame has {n} pixels, expected {ROWS*COLS}")

    @property
    def stale(self):
        """The pair is too loose to treat as one moment. See STALE_MS."""
        return self.thermal is not None and self.skew_bound_ms > STALE_MS

    def thermal_stats(self, box=None, reg=None):
        """min/mean/max over a thermal box — the per-plant primitive.

        `box` is in RGB pixels when `reg` is given, thermal (r0,c0,r1,c1)
        otherwise. Returns None when there is no frame or the box misses the
        sensor, because a detector must be able to tell "this plant is 31 C"
        from "this plant was not in thermal view". When the box only partly
        misses, `coverage` says how much of it was really measured.
        """
        if not self.thermal:
            return None
        cov = 1.0
        if box is None:
            r0, c0, r1, c1 = 0, 0, ROWS - 1, COLS - 1
        elif reg is not None:
            tb = reg.rgb_box_to_thermal(box, self.rgb_w, self.rgb_h)
            if tb is None:
                return None
            r0, c0, r1, c1, cov = tb
        else:
            # a raw box is caller-supplied indices; negative ones would wrap
            # round to the far side of the frame and read a different plant
            r0, c0, r1, c1 = (int(v) for v in box)
            if not (0 <= r0 <= r1 <= ROWS - 1 and 0 <= c0 <= c1 <= COLS - 1):
                raise ValueError(f"thermal box {box} outside 0..{ROWS-1} / "
                                 f"0..{COLS-1}, or inverted")
        v = [self.thermal[r * COLS + c]
             for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]
        if not v:
            return None
        return {"box": [r0, c0, r1, c1], "n": len(v), "coverage": cov,
                "min": min(v), "max": max(v), "mean": sum(v) / len(v)}

    def summary(self):
        rng = (f"{self.range_mm} mm" if self.range_mm is not None
               else f"none ({self.range_invalid_reason})")
        pair = ("co-timed" if self.co_timed else "SEPARATE REQUESTS")
        if self.stale:
            pair += " — STALE"
        if self.carried:
            pair += " — thermal carried from an earlier capture"
        th = "no frame"
        if self.thermal:
            # every field but the pixels is optional here on purpose: a summary
            # that raises is a summary you cannot use to diagnose the frame
            ta = f"{self.ta_c:.1f}C" if self.ta_c is not None else "?"
            th = (f"seq {self.thermal_seq} Ta {ta} "
                  f"{min(self.thermal):.1f}..{max(self.thermal):.1f}C"
                  + ("" if self.checksum_ok else " CHECKSUM BAD"))
        return (f"{self.capture_id}  {self.timestamp or '(unsynced)'}\n"
                f"  range   {rng}\n"
                f"  rgb     {self.rgb_w}x{self.rgb_h}, {self.rgb_bytes} B\n"
                f"  thermal {th}\n"
                f"  pairing {pair}, skew <= {self.skew_bound_ms:.0f} ms")


def fetch(base, timeout=TIMEOUT_S):
    """Fetch one bundle. Works against both firmware generations."""
    base = base.rstrip("/")
    t0 = time.monotonic()
    obs = json.loads(_get(f"{base}/observation", timeout))
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    cid = obs.get("capture_id")

    th = obs.get("thermal")
    co_timed = th is not None
    if co_timed:
        # the board measured the frame's real age against its own clock
        skew = float(th.get("age_ms", FRAME_PERIOD_MS))
        if "matrix" not in th and "px" not in th:
            # the matrix is referenced by file, as rgb references the JPEG; the
            # id to check is the THERMAL frame's own, which differs from the
            # observation's whenever the board carried a frame forward
            tid = (th.get("file") or "").split(".")[0] or cid
            th = dict(th)
            th["px"] = json.loads(_get_for(f"{base}/last.thermal", tid,
                                           timeout)).get("px")
    else:
        # Degraded: a second request, and the frame it answers with may be
        # much older than one frame period if the UART has gone quiet. The
        # stream block reports that age — using it is the difference between a
        # bound and a guess that happens to be smaller.
        doc = json.loads(_get(f"{base}/thermal", timeout)) or {}
        th = doc.get("frame")
        since = (doc.get("stream") or {}).get("ms_since_frame", -1)
        age = FRAME_PERIOD_MS if since is None or since < 0 else float(since)
        skew = (time.monotonic() - t0) * 1000.0 + max(age, FRAME_PERIOD_MS)

    jpg = _get_for(f"{base}/last.jpg", cid, timeout)
    if not co_timed:
        skew = max(skew, elapsed_ms)
    return Bundle(obs, th, jpg, co_timed, skew)


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    base = argv[1]
    save = box = None
    for i, a in enumerate(argv):
        if a == "--save" and i + 1 < len(argv):
            save = argv[i + 1]
        if a == "--box" and i + 1 < len(argv):
            box = tuple(int(x) for x in argv[i + 1].split(","))
    try:
        b = fetch(base)
    except (urllib.error.URLError, OSError) as e:
        print(f"{base}: {e}", file=sys.stderr)
        return 1
    print(b.summary())
    if box:
        s = b.thermal_stats(box)
        print(f"  box     {s}" if s else "  box     no frame / outside view")
    if save:
        import os
        os.makedirs(save, exist_ok=True)
        stem = os.path.join(save, b.capture_id)
        with open(stem + ".jpg", "wb") as f:
            f.write(b.rgb_jpeg)
        rec = dict(b.raw)
        rec["thermal"] = ({"width": COLS, "height": ROWS, "ta_c": b.ta_c,
                           "seq": b.thermal_seq, "checksum_ok": b.checksum_ok,
                           "matrix": b.thermal} if b.thermal else None)
        rec["pairing"] = {"co_timed": b.co_timed,
                          "skew_bound_ms": round(b.skew_bound_ms, 1)}
        with open(stem + ".json", "w") as f:
            json.dump(rec, f, indent=2)
        print(f"  saved   {stem}.jpg + .json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
