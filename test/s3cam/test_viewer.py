#!/usr/bin/env python3
"""viewer.py's server against a fake board. No browser, no hardware.

    ./test_viewer.py

Written because every bug in one long evening landed here, in the one module
with no tests: a stale-capture 409 that blanked the canvas, a 4 Hz aim poll
that starved /observation of the thermal frames it also needs, and a held
overlay drawn in wire order while the box beside it was named in flipped
coordinates. The last one is the shape that matters most — nothing on screen
disagreed, so the operator would have boxed the wrong physical object and the
numbers would all have been computable.

What is covered here is the SERVER half, which is where the orientation and
pairing rules actually live. The two timing bugs are in the page's JavaScript
and were verified in a browser; the rules they depend on are pinned below, so
a future change to either cannot quietly drop the guarantee.
"""
import contextlib
import io
import json
import math
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "../../tools/s3cam"))
import viewer                                                       # noqa: E402
from thermal_view import orient                                     # noqa: E402

ROWS, COLS = 24, 32
RGB_W, RGB_H = 640, 480
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def cold_blob(r0=12.0, c0=16.0, tbg=30.0, depth=30.0, sigma=1.5):
    two = 2.0 * sigma * sigma
    return [round(tbg - depth * math.exp(-(((r - r0) ** 2 + (c - c0) ** 2) / two)), 2)
            for r in range(ROWS) for c in range(COLS)]


class FakeBoard:
    """Serves the four endpoints observe.py and the aim path need."""

    I2C_REAL = ("I2C scan on SDA 5 / SCL 6\n\n"
                "  0x29  VL53L0X rangefinder\n"
                "  0x40  PCA9685 servo driver\n\n"
                "2 device(s). Expected for the pan/tilt head: 0x29 + 0x40.\n")
    I2C_NO_SERVO = ("I2C scan on SDA 5 / SCL 6\n\n"
                    "  0x29  VL53L0X rangefinder\n\n"
                    "1 device(s). Expected for the pan/tilt head: 0x29 + 0x40.\n")

    def __init__(self, orientation="wire", thermal_null=False, i2c_text=None):
        self.cap = 0
        self.seq = 0
        self.orientation = orientation
        self.thermal_null = thermal_null
        self.i2c_text = i2c_text if i2c_text is not None else FakeBoard.I2C_REAL
        self.px = cold_blob()
        self.servo_log = []
        board = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body, ctype="application/json", hdrs=()):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                for k, v in hdrs:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                cid = lambda: "cap-fake-%04d" % max(1, board.cap)
                if u.path == "/observation":
                    board.cap += 1
                    board.seq += 1
                    return self._send({
                        "capture_id": cid(), "timestamp": "2026-09-19T16:00:00Z",
                        "time_source": "ntp", "range_mm": None,
                        "range_invalid_reason": "no_distance:s255",
                        "rgb": {"file": cid() + ".jpg", "width": RGB_W,
                                "height": RGB_H, "bytes": len(JPG)},
                        "thermal": {"file": cid() + ".thermal.json",
                                    "width": COLS, "height": ROWS, "ta_c": 28.0,
                                    "seq": board.seq, "checksum_ok": False,
                                    "orientation": board.orientation,
                                    "age_ms": 120},
                        "environment": {}})
                if u.path in ("/capture", "/last.jpg"):
                    return self._send(JPG, "image/jpeg",
                                      [("X-Capture-Id", cid())])
                if u.path == "/last.thermal":
                    return self._send({"capture_id": cid(), "rows": ROWS,
                                       "cols": COLS, "px": board.px,
                                       "orientation": board.orientation})
                if u.path == "/thermal":
                    if board.thermal_null:
                        return self._send({"frame": None, "stream": {}})
                    board.seq += 1
                    return self._send({"frame": {
                        "seq": board.seq, "rows": ROWS, "cols": COLS,
                        "ta_c": 28.0, "checksum_ok": False,
                        "orientation": board.orientation, "px": board.px}})
                if u.path == "/i2c/scan":
                    # PLAIN TEXT, byte for byte the shape the firmware emits.
                    # The previous fake answered JSON — which is to say it
                    # answered what the client happened to expect, so the
                    # client's wrong assumption passed every test while
                    # reporting "no PCA9685" against a real board that was
                    # listing 0x40 the whole time.
                    return self._send(board.i2c_text.encode(), "text/plain")
                if u.path == "/servo":
                    board.servo_log.append((int(q["ch"][0]), int(q["us"][0])))
                    return self._send({"present": True, "set": "ok"})
                self.send_response(404)
                self.end_headers()

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


class ViewerServer:
    """viewer.py's own HTTP server, pointed at a fake board."""

    def __init__(self, board_url):
        viewer.Viewer.board = board_url.rstrip("/")
        viewer.Viewer.last = None
        viewer.Aim.samples.clear()
        viewer.Aim.key = None
        viewer.Aim.pan = viewer.Aim.tilt = None
        viewer.Aim.assumed = False
        self.httpd = HTTPServer(("127.0.0.1", 0), viewer.Viewer)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url + path, timeout=10) as r:
                body = r.read()
                return r.status, body
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def json(self, path):
        st, body = self.get(path)
        return st, (json.loads(body) if body else None)


class TestHeldOverlayOrientation(unittest.TestCase):
    """The still overlay and the box it is read through must be in ONE frame.

    The bug: /api/view answered with the box converted into flipped
    coordinates while the page drew the held thermal in wire order. With
    either flip ticked the picture showed one orientation and the selection
    meant another — and nothing on screen disagreed.
    """

    def test_the_held_frame_comes_back_in_the_boxs_orientation(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            v.json("/api/observe")
            for fv, fh in ((0, 0), (1, 0), (0, 1), (1, 1)):
                st, d = v.json(f"/api/view?sx=1&sy=1&dx=0&dy=0&flipv={fv}&fliph={fh}")
                with self.subTest(flipv=fv, fliph=fh):
                    self.assertEqual(st, 200)
                    self.assertEqual(d["thermal"],
                                     orient(b.px, ROWS, COLS, bool(fv), bool(fh)))

    def test_the_box_and_the_overlay_move_together(self):
        # A 180 degree turn must send the box to the mirrored indices, and the
        # overlay with it, so the same RGB drag keeps naming the same object.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            v.json("/api/observe")
            q = "sx=1&sy=1&dx=0&dy=0&box=100,60,400,330"
            _, a = v.json(q.join(["/api/view?", "&flipv=0&fliph=0"]))
            _, z = v.json(q.join(["/api/view?", "&flipv=1&fliph=1"]))
        r0, c0, r1, c1 = a["stats"]["box"]
        self.assertEqual(z["stats"]["box"],
                         [ROWS - 1 - r1, COLS - 1 - c1, ROWS - 1 - r0, COLS - 1 - c0])

    def test_no_capture_yet_is_a_reason_not_a_crash(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            st, d = v.json("/api/view?sx=1&sy=1&dx=0&dy=0")
            self.assertEqual(st, 200)
            self.assertIsNone(d["rect"])
            self.assertIn("no capture", d["reason"])


class TestPairing(unittest.TestCase):
    """One capture's photograph must never sit beside another's matrix."""

    def test_a_stale_id_is_refused_and_the_current_one_served(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            _, first = v.json("/api/observe")
            _, second = v.json("/api/observe")
            self.assertNotEqual(first["capture_id"], second["capture_id"])
            st, _ = v.get("/api/last.jpg?id=" + first["capture_id"])
            self.assertEqual(st, 409)
            st, body = v.get("/api/last.jpg?id=" + second["capture_id"])
            self.assertEqual(st, 200)
            self.assertTrue(body.startswith(b"\xff\xd8"))

    def test_before_any_capture_the_image_is_a_404(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            st, _ = v.get("/api/last.jpg")
            self.assertEqual(st, 404)


class TestAim(unittest.TestCase):
    def test_a_cold_target_needs_the_cold_flag(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            _, hot = v.json("/api/aim?box=8,12,16,20")
            _, cold = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertFalse(hot["centroid"]["ok"])
        self.assertTrue(cold["centroid"]["ok"])
        self.assertAlmostEqual(cold["centroid"]["r"], 12.0, delta=0.3)
        self.assertAlmostEqual(cold["centroid"]["c"], 16.0, delta=0.3)

    def test_the_emitted_command_carries_every_option(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1&flipv=1&fliph=1")
        for frag in ("--mode static", "--box 8,12,16,20", "--flipv", "--fliph",
                     "--cold", "--n 100"):
            self.assertIn(frag, d["cmd"])

    def test_a_corrected_board_plus_host_flips_is_flagged(self):
        with FakeBoard(orientation="rot180") as b, ViewerServer(b.url) as v:
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1&flipv=1&fliph=1")
        self.assertIn("drop the host flips", d["orientation_warning"])

    def test_a_wire_board_with_flips_is_not_flagged(self):
        with FakeBoard(orientation="wire") as b, ViewerServer(b.url) as v:
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1&flipv=1&fliph=1")
        self.assertEqual(d["orientation_warning"], "")

    def test_no_new_frame_is_a_204_not_an_error(self):
        # take() is consume-once at 4 Hz: "nothing new yet" is the normal case
        # for part of every frame period and must not look like a failure.
        with FakeBoard(thermal_null=True) as b, ViewerServer(b.url) as v:
            st, _ = v.get("/api/aim?box=8,12,16,20&cold=1")
        self.assertEqual(st, 204)

    def test_changing_the_box_resets_the_rolling_window(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            for _ in range(5):
                v.json("/api/aim?box=8,12,16,20&cold=1")
            _, before = v.json("/api/aim?box=8,12,16,20&cold=1")
            _, after = v.json("/api/aim?box=6,10,18,22&cold=1")
        self.assertGreater(before["window"]["n"], 1)
        self.assertEqual(after["window"]["n"], 1)

    def test_changing_a_flip_resets_the_window_too(self):
        # Every pixel is re-indexed, so the samples are about another image.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            for _ in range(4):
                v.json("/api/aim?box=8,12,16,20&cold=1")
            _, after = v.json("/api/aim?box=8,12,16,20&cold=1&flipv=1")
        self.assertEqual(after["window"]["n"], 1)


class TestOrientationAgreement(unittest.TestCase):
    """Both sensors must be in one coordinate system, and say so when not.

    /cam/tune keeps the camera's flips adjustable at runtime while the thermal
    rotation is compiled in. Turn one off and every box mapped from RGB lands
    on the wrong thermal pixels — silently, because each image on its own
    still looks perfectly sensible.
    """

    def test_matching_tags_are_quiet(self):
        from thermal_view import orientation_mismatch
        self.assertIsNone(orientation_mismatch("rot180", "rot180"))
        self.assertIsNone(orientation_mismatch("none", "none"))

    def test_a_disagreement_is_reported(self):
        from thermal_view import orientation_mismatch
        w = orientation_mismatch("vflip", "rot180")
        self.assertIsNotNone(w)
        self.assertIn("different coordinate systems", w)

    def test_an_older_board_with_no_tag_stays_quiet(self):
        from thermal_view import orientation_mismatch
        self.assertIsNone(orientation_mismatch(None, "rot180"))
        self.assertIsNone(orientation_mismatch("rot180", None))

    def test_the_observe_api_surfaces_it(self):
        with FakeBoard(orientation="rot180") as b, ViewerServer(b.url) as v:
            _, d = v.json("/api/observe")
        # the fake board reports no rgb orientation, so nothing to shout about
        self.assertEqual(d["orientation_mismatch"], "")


class TestServoPresence(unittest.TestCase):
    """Parsed from the real /i2c/scan, which is plain text meant for a human."""

    def test_the_servo_driver_is_found_in_the_real_text_format(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertTrue(d["servo"]["present"], d["servo"])
        self.assertEqual(d["servo"]["pan"], 1500)
        self.assertTrue(d["servo"]["assumed"])

    def test_a_bus_without_the_driver_reports_absent(self):
        with FakeBoard(i2c_text=FakeBoard.I2C_NO_SERVO) as b, ViewerServer(b.url) as v:
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertFalse(d["servo"]["present"])

    def test_an_unreachable_scan_reports_absent_rather_than_raising(self):
        viewer.Aim.pan = viewer.Aim.tilt = None
        self.assertFalse(viewer.Aim.servo("http://127.0.0.1:9")["present"])

    def test_the_footer_naming_expected_addresses_is_not_a_sighting(self):
        """The scan signs off with "Expected ... 0x29 + 0x40."

        A substring search over the response therefore finds the servo driver
        on a bus that does not have one — reporting present exactly when the
        thing is missing, which is the one answer worse than an error.
        """
        self.assertIn("0x40", FakeBoard.I2C_NO_SERVO)      # the trap is real
        viewer.Aim.pan = viewer.Aim.tilt = None
        with FakeBoard(i2c_text=FakeBoard.I2C_NO_SERVO) as b:
            self.assertFalse(viewer.Aim.servo(b.url)["present"])


class TestJog(unittest.TestCase):
    def test_a_jog_clamps_to_the_electrical_span(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            for _ in range(20):
                v.json("/api/jog?axis=pan&d=100")
        self.assertTrue(all(viewer.US_MIN <= us <= viewer.US_MAX
                            for _, us in b.servo_log))
        self.assertEqual(max(us for _, us in b.servo_log), viewer.US_MAX)

    def test_there_is_no_way_to_release_an_axis(self):
        # servo.h invariant 2: a released axis holding weight drops, and what
        # drops here is the camera. No jog may ever command zero.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            for d in (-100, -100, -100, -100, -100, -100, -100, -100,
                      -100, -100, -100, -100):
                v.json(f"/api/jog?axis=tilt&d={d}")
        self.assertTrue(all(us > 0 for _, us in b.servo_log))
        self.assertEqual(min(us for _, us in b.servo_log), viewer.US_MIN)

    def test_a_bad_axis_or_step_is_a_400(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            self.assertEqual(v.get("/api/jog?axis=zzz&d=10")[0], 400)
            self.assertEqual(v.get("/api/jog?axis=pan&d=abc")[0], 400)

    def test_a_jog_clears_the_rolling_window(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            for _ in range(4):
                v.json("/api/aim?box=8,12,16,20&cold=1")
            v.json("/api/jog?axis=pan&d=25")
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertEqual(d["window"]["n"], 1)


class TestPageGuards(unittest.TestCase):
    """Both async paths must be stamped, or a slow answer overwrites a fast one.

    The page cannot be executed here, so these pin the guards' PRESENCE: the
    two bugs they prevent — a stale capture blanking the canvas, and a stale
    /api/view redrawing the overlay from a registration the sliders no longer
    show — are both invisible on screen, which is why a reviewer removing
    either would have nothing to notice.
    """

    def _html(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            return v.get("/")[1].decode()

    def test_capture_is_serialised_and_stamped(self):
        html = self._html()
        self.assertIn("let capturing=false", html)
        self.assertIn("if(capturing) return", html)
        self.assertIn("mine!==capSeq", html)

    def test_the_aim_poll_stands_aside_during_a_capture(self):
        # /observation needs a thermal frame too, and take() is consume-once.
        html = self._html()
        self.assertIn("async function poll", html)
        self.assertIn("if(capturing) return", html)

    def test_capture_waits_for_an_aim_request_already_in_flight(self):
        # Refusing to START a poll is not enough: one already on the wire
        # consumes /thermal before /api/observe gets there, and take() is
        # consume-once, so the capture comes back with no thermal or a
        # carried one despite the guard.
        html = self._html()
        self.assertIn("let aimBusy=null", html)
        self.assertIn("if(aimBusy) await aimBusy", html)

    def test_view_requests_are_stamped(self):
        html = self._html()
        self.assertIn("let statSeq=0", html)
        self.assertIn("mine!==statSeq", html)


class TestPage(unittest.TestCase):
    def test_the_page_is_served_and_carries_its_controls(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            st, body = v.get("/")
        self.assertEqual(st, 200)
        html = body.decode()
        for ident in ("id=live", "id=cold", "id=tflipv", "id=tfliph", "id=zoom",
                      "id=sx", "id=sy", "id=owarn", "id=est", "id=nf", "id=cmd"):
            self.assertIn(ident, html, ident)

    def test_the_javascript_wires_every_control_it_renders(self):
        # The failure this catches: an edit whose anchor no longer matched
        # left the warning element on the page with nothing ever writing to
        # it — silently, because a missing write looks exactly like no warning.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            html = v.get("/")[1].decode()
        for ref in ("$('#owarn')", "$('#est')", "$('#nf')", "$('#cmd')",
                    "$('#zoom')", "$('#live')", "$('#cold')", "$('#tflipv')"):
            self.assertIn(ref, html, ref)


if __name__ == "__main__":
    unittest.main(verbosity=2)
