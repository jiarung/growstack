#!/usr/bin/env python3
"""observe.py against a fake board — both firmware generations, no hardware.

    ./test_observe.py        # exits non-zero on failure

The case that matters most is the DEGRADED one. Against firmware that still
emits "thermal": null the client must still return a usable bundle, and it
must mark it co_timed=False — because the failure this guards against is not a
crash, it is a detector quietly treating two requests as one moment and
publishing a per-plant temperature that belongs to a different scene.
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "../../tools/s3cam"))
import observe  # noqa: E402

ROWS, COLS = observe.ROWS, observe.COLS
PX = [20.0 + (r * COLS + c) * 0.01 for r in range(ROWS) for c in range(COLS)]
JPG = b"\xff\xd8\xff\xe0fake\xff\xd9"

OBS_ID = "cap-20260917T040000Z"
OBS = {
    "capture_id": OBS_ID, "timestamp": "2026-09-17T04:00:00Z",
    "time_source": "ntp", "uptime_ms": 12345, "plant_id": None, "pose": None,
    "range_mm": 412, "range_invalid_reason": None,
    "rgb": {"file": "cap.jpg", "width": 640, "height": 480, "bytes": len(JPG)},
    "thermal": None, "environment": None,
}


class Board(BaseHTTPRequestHandler):
    mode = "legacy"          # legacy | inline | fileref
    thermal_frame = True     # False -> the UART has produced nothing yet
    thermal_capture = OBS_ID  # the capture the thermal frame was taken FOR
    since_ms = 40             # /thermal's own report of its frame's age
    jpg_capture = OBS_ID      # the capture /last.jpg's X-Capture-Id claims
    hits = []

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Board.hits.append(self.path)
        if self.path == "/observation":
            o = dict(OBS)
            if Board.mode == "inline" and Board.thermal_frame:
                o["thermal"] = {"width": COLS, "height": ROWS, "ta_c": 26.4,
                                "emissivity": 0.95, "seq": 7, "age_ms": 118,
                                "checksum_ok": True, "matrix": PX}
            elif Board.mode == "fileref" and Board.thermal_frame:
                o["thermal"] = {"file": Board.thermal_capture + ".thermal.json",
                                "width": COLS,
                                "height": ROWS, "ta_c": 26.4, "seq": 7,
                                "age_ms": 118, "checksum_ok": True}
            return self._send(json.dumps(o))
        if self.path == "/last.thermal":
            return self._send(json.dumps({"seq": 7, "ta_c": 26.4, "px": PX}))
        if self.path == "/thermal":
            f = ({"seq": 7, "ta_c": 26.4, "checksum_ok": True, "rows": ROWS,
                  "cols": COLS, "px": PX} if Board.thermal_frame else None)
            return self._send(json.dumps(
                {"stream": {"frames_ok": 9, "ms_since_frame": Board.since_ms},
                 "frame": f}))
        if self.path == "/last.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("X-Capture-Id", Board.jpg_capture)
            self.send_header("Content-Length", str(len(JPG)))
            self.end_headers()
            return self.wfile.write(JPG)
        self.send_error(404)


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Board)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Board.mode, Board.thermal_frame, Board.hits = "legacy", True, []
        Board.thermal_capture = Board.jpg_capture = OBS_ID
        Board.since_ms = 40

    def test_legacy_is_usable_but_never_claims_co_timing(self):
        b = observe.fetch(self.url)
        self.assertFalse(b.co_timed)
        self.assertEqual(len(b.thermal), ROWS * COLS)
        self.assertEqual(b.range_mm, 412)
        self.assertEqual(b.rgb_jpeg, JPG)
        # the bound must include a frame period, not just the request time
        self.assertGreaterEqual(b.skew_bound_ms, observe.FRAME_PERIOD_MS)
        self.assertIn("/thermal", Board.hits)

    def test_inline_bundle_is_co_timed_and_asks_for_no_second_frame(self):
        Board.mode = "inline"
        b = observe.fetch(self.url)
        self.assertTrue(b.co_timed)
        self.assertEqual(b.skew_bound_ms, 118)
        self.assertNotIn("/thermal", Board.hits)
        self.assertNotIn("/last.thermal", Board.hits)

    def test_fileref_bundle_fetches_the_matrix(self):
        Board.mode = "fileref"
        b = observe.fetch(self.url)
        self.assertTrue(b.co_timed)
        self.assertEqual(len(b.thermal), ROWS * COLS)
        self.assertIn("/last.thermal", Board.hits)

    def test_no_thermal_frame_is_a_state_not_a_crash(self):
        Board.thermal_frame = False
        for mode in ("legacy", "inline"):
            Board.mode = mode
            b = observe.fetch(self.url)
            self.assertIsNone(b.thermal, mode)
            self.assertIsNone(b.thermal_stats(), mode)
            self.assertIn("no frame", b.summary())

    def test_a_long_skew_is_called_stale_rather_than_left_to_the_caller(self):
        fresh = observe.Bundle(OBS, {"px": PX}, JPG, True, 100.0)
        old = observe.Bundle(OBS, {"px": PX}, JPG, True, 5000.0)
        self.assertFalse(fresh.stale)
        self.assertTrue(old.stale)
        self.assertIn("STALE", old.summary())
        self.assertNotIn("STALE", fresh.summary())
        # no frame at all is "no frame", never "stale"
        self.assertFalse(observe.Bundle(OBS, None, JPG, True, 9e9).stale)

    def test_a_carried_frame_is_reported_not_silently_adopted(self):
        # the board carries the previous matrix when a capture lands between
        # frames; it stamps the frame with ITS OWN capture, and the client must
        # notice rather than assume the pair is one moment
        Board.mode, Board.thermal_capture = "fileref", "cap-20260917T035959Z"
        b = observe.fetch(self.url)
        self.assertTrue(b.carried)
        self.assertIn("carried", b.summary())
        Board.thermal_capture = OBS_ID
        self.assertFalse(observe.fetch(self.url).carried)

    def test_short_frame_is_rejected_loudly(self):
        with self.assertRaises(ValueError):
            observe.Bundle(OBS, {"px": [1.0, 2.0]}, JPG, True, 0.0)

    def test_thermal_stats_reads_the_box_it_was_given(self):
        b = observe.fetch(self.url)
        s = b.thermal_stats((2, 3, 2, 5))       # one row, three columns
        self.assertEqual(s["n"], 3)
        self.assertAlmostEqual(s["min"], PX[2 * COLS + 3])
        self.assertAlmostEqual(s["max"], PX[2 * COLS + 5])

    def test_registration_identity_maps_full_frame_to_full_frame(self):
        r = observe.Registration()
        tb = r.rgb_box_to_thermal((0, 0, 640, 480), 640, 480)
        self.assertEqual(tb[:4], (0, 0, ROWS - 1, COLS - 1))
        self.assertAlmostEqual(tb.coverage, 1.0)

    def test_a_box_outside_the_thermal_view_refuses_rather_than_clamps(self):
        # thermal sees less than the OV5640; a clamped box would report the
        # temperature of the frame edge as if it were the plant's
        r = observe.Registration(scale=0.25)
        b = observe.fetch(self.url)
        far = (10000, 10000, 10400, 10400)
        self.assertIsNone(r.rgb_box_to_thermal(far, 640, 480))
        self.assertIsNone(b.thermal_stats(far, reg=r))

    def test_a_partial_miss_is_measured_but_says_how_much_it_measured(self):
        # the dangerous middle case: half the plant is off the thermal sensor.
        # Refusing would make every edge plant unreadable; clamping silently
        # would hand back an edge temperature as the plant's. It does neither.
        r = observe.Registration()
        tb = r.rgb_box_to_thermal((-640, 0, 640, 480), 640, 480)
        self.assertIsNotNone(tb)
        self.assertAlmostEqual(tb.coverage, 0.5, places=2)
        b = observe.fetch(self.url)
        self.assertAlmostEqual(
            b.thermal_stats((-640, 0, 640, 480), reg=r)["coverage"], 0.5, places=2)
        self.assertEqual(b.thermal_stats((0, 0, 640, 480), reg=r)["coverage"], 1.0)

    def test_overlay_rect_is_the_exact_inverse_of_the_box_mapping(self):
        # the viewer draws with overlay_rect and measures with _map; if they
        # are not inverses you align by eye and read a different place
        W, H = 640, 480
        for sc, dx, dy in ((1.0, 0, 0), (1.7, 3.1, -2.4), (0.4, -9.0, 5.5)):
            r = observe.Registration(sc, dx, dy)
            q = r.overlay_rect(W, H)
            for (want_r, want_c), (x, y) in (
                    ((0, 0), (q["x"], q["y"])),
                    ((ROWS, COLS), (q["x"] + q["w"], q["y"] + q["h"]))):
                gr, gc = r._map(x, y, W, H)
                self.assertAlmostEqual(gr, want_r, places=9, msg=(sc, dx, dy))
                self.assertAlmostEqual(gc, want_c, places=9, msg=(sc, dx, dy))

    def test_offsets_move_the_box_in_the_direction_they_say(self):
        r0 = observe.Registration()
        r1 = observe.Registration(dx=4.0, dy=2.0)
        a = r0.rgb_box_to_thermal((320, 240, 321, 241), 640, 480)
        b = r1.rgb_box_to_thermal((320, 240, 321, 241), 640, 480)
        self.assertEqual(b.c0 - a.c0, 4)
        self.assertEqual(b.r0 - a.r0, 2)

    def test_a_scale_with_no_inverse_is_refused_at_construction(self):
        # the overlay rectangle IS the inverse; a slider cannot send 0 but a
        # URL can, and a 500 is a worse answer than a 400
        for bad in (0, -1.0, float("inf"), float("nan")):
            with self.assertRaises(ValueError, msg=bad):
                observe.Registration(scale=bad)
        with self.assertRaises(ValueError):
            observe.Registration(dx=float("nan"))

    def test_a_raw_box_with_negative_indices_is_refused_not_wrapped(self):
        # python would happily read px[-1] from the far side of the frame
        b = observe.fetch(self.url)
        for bad in ((-1, 0, 2, 2), (0, 0, ROWS, 2), (0, 0, 2, COLS), (5, 0, 2, 2)):
            with self.assertRaises(ValueError, msg=bad):
                b.thermal_stats(bad)

    def test_degraded_skew_uses_the_boards_own_frame_age(self):
        # a quiet UART makes /thermal's frame far older than one frame period;
        # a bound that ignores that is not a bound
        Board.since_ms = 4000
        b = observe.fetch(self.url)
        self.assertFalse(b.co_timed)
        self.assertGreaterEqual(b.skew_bound_ms, 4000)
        self.assertTrue(b.stale)

    def test_an_artefact_from_another_capture_is_refused(self):
        # a second client capturing between our requests would otherwise hand
        # back a JPEG from a scene the metadata does not describe
        Board.jpg_capture = "cap-somebody-elses"
        with self.assertRaises(ValueError):
            observe.fetch(self.url)


class V(unittest.TestCase):
    """The viewer end to end: page, capture, box -> temperature, failure."""

    @classmethod
    def setUpClass(cls):
        import viewer
        cls.viewer = viewer
        cls.board = HTTPServer(("127.0.0.1", 0), Board)
        threading.Thread(target=cls.board.serve_forever, daemon=True).start()
        viewer.Viewer.board = "http://127.0.0.1:%d" % cls.board.server_address[1]
        cls.srv = HTTPServer(("127.0.0.1", 0), viewer.Viewer)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.board.shutdown()

    def setUp(self):
        Board.mode, Board.thermal_frame, Board.hits = "inline", True, []
        Board.thermal_capture = OBS_ID
        self.viewer.Viewer.last = None

    def get(self, path):
        import urllib.request
        with urllib.request.urlopen(self.url + path, timeout=5) as r:
            return r.status, r.read()

    def test_page_serves(self):
        st, body = self.get("/")
        self.assertEqual(st, 200)
        self.assertIn(b"s3cam observation", body)

    def test_stats_before_any_capture_says_so_instead_of_500(self):
        st, body = self.get("/api/view?box=0,0,10,10&scale=1&dx=0&dy=0")
        d = json.loads(body)
        self.assertIsNone(d["stats"])
        self.assertIsNone(d["rect"])
        self.assertIn("no capture", d["reason"])

    def test_capture_then_box_returns_a_temperature(self):
        st, body = self.get("/api/observe")
        b = json.loads(body)
        self.assertTrue(b["co_timed"])
        self.assertEqual(len(b["thermal"]), ROWS * COLS)
        st, jpg = self.get("/api/last.jpg?id=" + b["capture_id"])
        self.assertEqual(jpg, JPG)
        st, body = self.get("/api/view?box=0,0,640,480&scale=1&dx=0&dy=0")
        d = json.loads(body)
        self.assertIsNotNone(d["rect"])
        s = d["stats"]
        self.assertEqual(s["n"], ROWS * COLS)
        self.assertAlmostEqual(s["min"], min(PX))

    def test_a_page_holding_an_older_capture_is_refused_not_quietly_updated(self):
        # the tautology this replaces compared the served JPEG against whatever
        # the server currently held, so it could not fail. This asks for a
        # capture the server no longer has — with auto-capture or a second tab
        # that is a real request — and requires an error rather than a swap.
        import urllib.error
        self.get("/api/observe")
        stale_id = self.viewer.Viewer.last.capture_id
        Board.thermal_capture = Board.jpg_capture = OBS_ID
        self.viewer.Viewer.last.capture_id = "cap-newer"   # the board moved on
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/last.jpg?id=" + stale_id)
        self.assertEqual(e.exception.code, 409)
        st, jpg = self.get("/api/last.jpg?id=cap-newer")
        self.assertEqual(jpg, JPG)

    def test_a_scale_with_no_inverse_is_a_400_not_a_500(self):
        import urllib.error
        self.get("/api/observe")
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/view?scale=0&dx=0&dy=0")
        self.assertEqual(e.exception.code, 400)

    def test_box_outside_thermal_view_reports_the_reason(self):
        self.get("/api/observe")
        st, body = self.get("/api/view?box=9000,9000,9100,9100&scale=0.25&dx=0&dy=0")
        d = json.loads(body)
        self.assertIsNone(d["stats"])
        self.assertIn("outside", d["reason"])

    def test_unreachable_board_is_an_error_not_a_traceback(self):
        import urllib.error
        self.viewer.Viewer.board = "http://127.0.0.1:1"
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.get("/api/observe")
            self.assertEqual(e.exception.code, 502)
            self.assertIn("error", json.loads(e.exception.read()))
        finally:
            self.viewer.Viewer.board = "http://127.0.0.1:%d" % self.board.server_address[1]


if __name__ == "__main__":
    unittest.main(verbosity=2)
