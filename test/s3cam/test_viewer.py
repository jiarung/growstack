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
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "../../tools/s3cam"))
import viewer                                                       # noqa: E402
from head_datum import Datum                                        # noqa: E402
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

    def __init__(self, orientation="wire", thermal_null=False, i2c_text=None,
                 servo_fails=False):
        self.cap = 0
        self.seq = 0
        self.orientation = orientation
        self.thermal_null = thermal_null
        self.i2c_text = i2c_text if i2c_text is not None else FakeBoard.I2C_REAL
        self.servo_fails = servo_fails
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
                    if board.servo_fails:
                        # The firmware's own refusal shape: reachable, and
                        # saying no. Distinct from an unreachable board, and
                        # the two clean up differently.
                        return self._send({"present": True,
                                           "set": "rejected"})
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
        viewer.Viewer.recent.clear()
        # EVERY field, not the ones a given test happens to read. Aim's state
        # is class-level, so anything left set leaks into the next test — and
        # `verified`, forgotten here at first, let one test's lost-reply make a
        # later test's perfectly good jog refuse itself.
        viewer.Aim.samples.clear()
        viewer.Aim.key = None
        viewer.Aim.pan = viewer.Aim.tilt = None
        viewer.Aim.assumed = False
        viewer.Aim.verified = True
        viewer.Aim.moving = False
        viewer.Aim.generation = 0
        viewer.Aim.moves = 0
        # A datum the test owns, so the repo's real head-datum.json cannot
        # decide whether this suite passes.
        viewer.Aim.datum = Datum().set("pan", 1500).set("tilt", 1500)
        # THREADING, like production. With HTTPServer here the request loop
        # serialises every handler, so the concurrency tests below could not
        # execute two handlers at once — they would pass against code with a
        # real race in it, which is a test that proves nothing.
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), viewer.Viewer)
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

    def test_the_overlay_follows_the_id_not_whichever_is_newest(self):
        """Both halves of a pair must come from the same capture.

        Keeping recent JPEGs fetchable fixed the image; this endpoint still
        answered from the newest capture, so a tab could draw its own
        photograph under another tab's matrix.
        """
        with FakeBoard() as b, ViewerServer(b.url) as v:
            _, first = v.json("/api/observe")
            b.px = [v + 40.0 for v in b.px]        # a visibly different scene
            _, second = v.json("/api/observe")
            q = "sx=1&sy=1&dx=0&dy=0"
            _, a = v.json(f"/api/view?{q}&id=" + first["capture_id"])
            _, z = v.json(f"/api/view?{q}&id=" + second["capture_id"])
        self.assertNotEqual(a["thermal"], z["thermal"])
        self.assertEqual(a["thermal"], first["thermal"])
        self.assertEqual(z["thermal"], second["thermal"])

    def test_an_unheld_id_is_a_reason_not_another_captures_matrix(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            v.json("/api/observe")
            _, d = v.json("/api/view?sx=1&sy=1&dx=0&dy=0&id=cap-never-existed")
        self.assertIsNone(d["rect"])
        self.assertIsNone(d["thermal"])
        self.assertIn("no longer held", d["reason"])

    def test_every_view_response_has_the_same_shape(self):
        # A response whose keys depend on which branch produced it makes every
        # caller learn the branches, and the one that forgets reads a missing
        # key as a value.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            probes = [
                "/api/view?sx=1&sy=1&dx=0&dy=0",                  # no capture
                "/api/view?sx=0&sy=1&dx=0&dy=0",                  # bad overlay
            ]
            v.json("/api/observe")
            probes += [
                "/api/view?sx=1&sy=1&dx=0&dy=0",                  # ok
                "/api/view?sx=1&sy=1&dx=0&dy=0&id=cap-nope",      # unheld
                "/api/view?sx=1&sy=1&dx=0&dy=0&box=0,0,10,10",    # ok with box
            ]
            for path in probes:
                _, d = v.json(path)
                with self.subTest(path=path):
                    self.assertEqual(set(d), {"rect", "thermal", "stats", "reason"},
                                     sorted(d))

    def test_the_page_sends_the_id_it_is_displaying(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            html = v.get("/")[1].decode()
        self.assertIn("q.set('id',bundle.capture_id)", html)

    def test_no_capture_yet_is_a_reason_not_a_crash(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            st, d = v.json("/api/view?sx=1&sy=1&dx=0&dy=0")
            self.assertEqual(st, 200)
            self.assertIsNone(d["rect"])
            self.assertIn("no capture", d["reason"])


class TestPairing(unittest.TestCase):
    """One capture's photograph must never sit beside another's matrix."""

    def test_you_get_the_image_belonging_to_the_id_you_named(self):
        """The guarantee, stated as itself.

        It was never "only the newest capture exists" — that was a consequence
        of holding exactly one, and it broke the moment the server went
        threaded: two tabs overlap, the second capture replaces the first, and
        the first client is refused a capture it was handed a moment earlier.
        A few recent captures are kept so both clients can be served. What
        must never happen is either of them receiving the OTHER's image.
        """
        with FakeBoard() as b, ViewerServer(b.url) as v:
            _, first = v.json("/api/observe")
            _, second = v.json("/api/observe")
            self.assertNotEqual(first["capture_id"], second["capture_id"])
            for obs in (first, second):
                st, body = v.get("/api/last.jpg?id=" + obs["capture_id"])
                self.assertEqual(st, 200, obs["capture_id"])
                self.assertTrue(body.startswith(b"\xff\xd8"))

    def test_a_capture_evicted_by_newer_ones_is_an_error_not_a_swap(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            _, first = v.json("/api/observe")
            for _ in range(viewer.Viewer.RECENT_MAX + 1):
                v.json("/api/observe")
            st, body = v.get("/api/last.jpg?id=" + first["capture_id"])
        self.assertEqual(st, 409)
        self.assertNotIn(b"\xff\xd8", body[:4])       # no JPEG smuggled in

    def test_concurrent_captures_from_two_clients_both_get_their_own(self):
        import threading as th
        with FakeBoard() as b, ViewerServer(b.url) as v:
            got, errs = [], []

            def client():
                try:
                    _, obs = v.json("/api/observe")
                    st, body = v.get("/api/last.jpg?id=" + obs["capture_id"])
                    got.append((obs["capture_id"], st, len(body)))
                except Exception as e:                               # noqa: BLE001
                    errs.append(e)

            ts = [th.Thread(target=client) for _ in range(3)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(errs, [])
        self.assertEqual(len(got), 3)
        self.assertEqual(len({g[0] for g in got}), 3, "ids must be distinct")
        for cid, st, n in got:
            self.assertEqual(st, 200, cid)
            self.assertGreater(n, 0)

    def test_before_any_capture_the_image_is_a_404(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            st, _ = v.get("/api/last.jpg")
            self.assertEqual(st, 404)


class TestTestServerIsThreaded(unittest.TestCase):
    """Without this the concurrency suite below proves nothing.

    Its first version built an HTTPServer, whose request loop serialises every
    handler — so every threaded test passed against code that had a real race
    in it. A concurrency test on a serialising server is not a weak test, it
    is a test of something else entirely.
    """

    def test_the_fixture_matches_production(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            self.assertIsInstance(v.httpd, ThreadingHTTPServer)
        # and production really is threaded, so the fixture is not stricter
        # than the thing it stands in for
        import inspect
        self.assertIn("ThreadingHTTPServer", inspect.getsource(viewer.main))


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
        self.assertIsNone(orientation_mismatch("vflip", "vflip"))

    def test_a_recording_from_before_the_shared_tag_still_reads(self):
        """"wire" and "none" are the same state, one firmware apart.

        Both sensors derive their tag from head_mount.h now, so a live board
        never mixes the two. The alias is for recordings made before that
        change — and it is an alias, not a translation table between two
        live vocabularies, which is what this used to be and was a fix at the
        wrong end.
        """
        from thermal_view import orientation_mismatch
        self.assertIsNone(orientation_mismatch("none", "wire"))
        self.assertIsNone(orientation_mismatch("wire", "none"))

    def test_uncorrected_against_corrected_still_warns(self):
        from thermal_view import orientation_mismatch
        self.assertIsNotNone(orientation_mismatch("none", "vflip"))
        self.assertIsNotNone(orientation_mismatch("vflip", "wire"))

    def test_a_disagreement_is_reported(self):
        # The real one, live from 2026-09-24 to 2026-09-28: the camera
        # corrected with a vflip while the thermal still rotated, leaving one
        # horizontal mirror between them — and a mirror is the one transform
        # the four-parameter registration cannot absorb.
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
        self.assertEqual(d["servo"]["pan"], 1500)      # the injected datum
        self.assertTrue(d["servo"]["assumed"])

    def test_the_assumed_width_is_the_datum_not_the_protocol_neutral(self):
        """Where the head sits level, not what a neutral pulse means.

        The first jog is measured from this number, so a head whose horn sits
        13 us off level is nudged from a guess wrong by that much.
        """
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1513).set("tilt", 1487)
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertEqual(d["servo"]["pan"], 1513)
        self.assertEqual(d["servo"]["tilt"], 1487)

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


class TestAimYieldsToCapture(unittest.TestCase):
    """The board's frames are consume-once and two tabs both want them."""

    def test_aim_answers_204_while_a_capture_holds_the_board(self):
        # The page's `capturing` guard covers ONE tab. A second tab aiming
        # while the first captures would eat the frame /observation needs.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            v.json("/api/observe")
            viewer.Viewer.capture_lock.acquire()
            try:
                st, _ = v.get("/api/aim?box=8,12,16,20&cold=1")
            finally:
                viewer.Viewer.capture_lock.release()
        self.assertEqual(st, 204)

    def test_it_resumes_once_the_capture_is_done(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Viewer.capture_lock.acquire()
            viewer.Viewer.capture_lock.release()
            st, _ = v.get("/api/aim?box=8,12,16,20&cold=1")
        self.assertEqual(st, 200)

    def test_the_lock_is_released_even_when_the_board_errors(self):
        # Released in a finally: a board that times out mid-aim must not leave
        # captures blocked for the rest of the session.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Viewer.board = "http://127.0.0.1:9"
            v.get("/api/aim?box=8,12,16,20&cold=1")
            self.assertTrue(viewer.Viewer.capture_lock.acquire(blocking=False))
            viewer.Viewer.capture_lock.release()

    def test_a_real_concurrent_capture_and_aim_do_not_both_take_a_frame(self):
        import threading as th
        with FakeBoard() as b, ViewerServer(b.url) as v:
            results = []

            def cap():
                results.append(("observe", v.json("/api/observe")[1]))

            def aim():
                for _ in range(6):
                    results.append(("aim", v.get("/api/aim?box=8,12,16,20&cold=1")[0]))

            ts = [th.Thread(target=cap), th.Thread(target=aim)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        obs = [r for k, r in results if k == "observe"]
        self.assertTrue(obs)
        for o in obs:
            # the capture kept its thermal half
            self.assertNotIn("error", o, o)
            self.assertEqual(len(o["thermal"]), ROWS * COLS)


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


class TestConcurrency(unittest.TestCase):
    """A slow capture must not stall the aim poll, and a client that walks
    away must not look like a crash."""

    def test_the_server_is_threaded(self):
        # /api/observe measured at 2.5 s against a real board. Single-threaded,
        # every 250 ms aim poll queues behind it — ten deep for one capture.
        self.assertIs(viewer.ThreadingHTTPServer.__mro__[0],
                      viewer.ThreadingHTTPServer)
        self.assertTrue(getattr(viewer.ThreadingHTTPServer, "daemon_threads", False)
                        or hasattr(viewer.ThreadingHTTPServer, "process_request_thread"))

    def test_a_client_that_hangs_up_is_not_an_error(self):
        # The handler swallows the two ways a browser closes a socket early.
        # Without this a reload printed a full traceback per in-flight poll.
        import inspect
        src = inspect.getsource(viewer.Viewer.handle_one_request)
        self.assertIn("BrokenPipeError", src)
        self.assertIn("ConnectionResetError", src)

    def test_concurrent_aim_polls_do_not_corrupt_the_window(self):
        import threading as th
        with FakeBoard() as b, ViewerServer(b.url) as v:
            errs = []

            def hammer():
                try:
                    for _ in range(8):
                        v.json("/api/aim?box=8,12,16,20&cold=1")
                except Exception as e:                               # noqa: BLE001
                    errs.append(e)

            ts = [th.Thread(target=hammer) for _ in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errs, [])
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        # The window is bounded and self-consistent, whatever the interleaving
        self.assertGreaterEqual(d["window"]["n"], 1)
        self.assertLessEqual(d["window"]["n"], d["window_max"])

    def test_a_jog_during_polling_still_resets_cleanly(self):
        import threading as th
        with FakeBoard() as b, ViewerServer(b.url) as v:
            stop = th.Event()

            def poll():
                while not stop.is_set():
                    v.json("/api/aim?box=8,12,16,20&cold=1")

            t = th.Thread(target=poll)
            t.start()
            try:
                for _ in range(5):
                    v.json("/api/jog?axis=pan&d=25")
            finally:
                stop.set()
                t.join()
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertTrue(d["servo"]["present"])
        self.assertLessEqual(d["window"]["n"], d["window_max"])


class TestJogSerialisation(unittest.TestCase):
    """Threading made these possible; neither was reachable single-threaded."""

    def test_concurrent_jogs_do_not_lose_increments(self):
        """Four +25s must be +100, not +25 with three presses dropped.

        Without the transaction lock each thread read the same starting width
        before any of them committed, so they all computed the same target and
        the board ended up one step along instead of four.
        """
        import threading as th
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1500).set("tilt", 1500)
            v.json("/api/aim?box=8,12,16,20&cold=1")     # establish the widths
            ts = [th.Thread(target=lambda: v.json("/api/jog?axis=pan&d=25"))
                  for _ in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertEqual(d["servo"]["pan"], 1500 + 4 * 25)
        # and the board really was commanded to each intermediate width
        pan = [us for ch, us in b.servo_log if ch == 5]
        self.assertEqual(sorted(pan), [1525, 1550, 1575, 1600])

    def test_the_reported_width_matches_the_last_command_sent(self):
        import threading as th
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1500).set("tilt", 1500)
            v.json("/api/aim?box=8,12,16,20&cold=1")
            ts = [th.Thread(target=lambda: v.json("/api/jog?axis=pan&d=10"))
                  for _ in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertEqual(d["servo"]["pan"], [us for ch, us in b.servo_log
                                             if ch == 5][-1])


class TestAimSnapshotIsOneInstant(unittest.TestCase):
    """The window and the servo widths in one reply must describe one moment."""

    def test_the_reply_carries_a_generation(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertIn("generation", d)

    def test_a_jog_bumps_the_generation_and_empties_the_window(self):
        # The pairing that used to be possible: a spread measured before the
        # head moved, shown beside the width it moved to — at exactly the
        # moment somebody had just jogged and would most trust the number.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1500).set("tilt", 1500)
            for _ in range(6):
                v.json("/api/aim?box=8,12,16,20&cold=1")
            _, before = v.json("/api/aim?box=8,12,16,20&cold=1")
            v.json("/api/jog?axis=pan&d=25")
            _, after = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertGreater(before["window"]["n"], 1)
        self.assertGreater(after["generation"], before["generation"])
        self.assertEqual(after["window"]["n"], 1)        # only this poll's sample
        self.assertEqual(after["servo"]["pan"], 1525)

    def test_polls_racing_jogs_never_report_a_window_from_another_pose(self):
        import threading as th
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1500).set("tilt", 1500)
            seen, stop = [], th.Event()

            def poll():
                while not stop.is_set():
                    _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
                    seen.append((d["generation"], d["window"]["n"],
                                 d["servo"]["pan"]))

            t = th.Thread(target=poll)
            t.start()
            try:
                for _ in range(6):
                    v.json("/api/jog?axis=pan&d=25")
            finally:
                stop.set()
                t.join()
        self.assertTrue(seen)
        # A window can never hold more samples than polls since its generation
        # began — that is what "one instant" buys, and what a torn read broke.
        counts = {}
        for gen, n, _ in seen:
            counts[gen] = counts.get(gen, 0) + 1
            self.assertLessEqual(n, counts[gen], f"gen {gen}: n={n}")


class TestStaleSampleDropped(unittest.TestCase):
    """A frame that outlived its pose must not seed the next pose's window."""

    def test_a_jog_during_a_poll_discards_that_poll_s_sample(self):
        # Fetching and centroiding take a frame period or more. A jog inside
        # that window leaves a sample describing the OLD pose; appending it
        # would seed the new window with a point from somewhere else and
        # inflate the very spread the panel exists to measure.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1500).set("tilt", 1500)
            v.json("/api/aim?box=8,12,16,20&cold=1")
            # Simulate the race deterministically: bump the generation the way
            # a jog does, between the "fetch" and the commit.
            moves0 = viewer.Aim.moves
            # What a jog actually does: clear, bump BOTH counters, together.
            with viewer.Aim.lock:
                viewer.Aim.samples.clear()
                viewer.Aim.generation += 1
                viewer.Aim.moves += 1
            cd = {"ok": True, "r": 12.0, "c": 16.0, "n": 9, "contrast": 30.0,
                  "tbg": 22.0, "tth": 7.0, "reason": "ok"}
            snap = viewer.Aim.snapshot(b.url, cd, viewer.Aim.key, False, moves0)
        self.assertTrue(snap["sample_dropped"])
        self.assertEqual(snap["window"]["n"], 0)

    def test_an_undisturbed_poll_keeps_its_sample(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1500).set("tilt", 1500)
            v.json("/api/aim?box=8,12,16,20&cold=1")
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertFalse(d["sample_dropped"])
        self.assertGreaterEqual(d["window"]["n"], 1)

    def test_changing_the_box_still_keeps_this_poll_s_sample(self):
        # The key change is made BY this request, so its own sample was
        # computed with the new box and belongs in the new window. Only a
        # MOVE invalidates it.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            v.json("/api/aim?box=8,12,16,20&cold=1")
            _, d = v.json("/api/aim?box=6,10,18,22&cold=1")
        self.assertFalse(d["sample_dropped"])
        self.assertEqual(d["window"]["n"], 1)


class TestMoveInFlight(unittest.TestCase):
    """The board applies the move before it answers, so the reply is too late."""

    def test_samples_taken_while_moving_are_dropped(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1500).set("tilt", 1500)
            v.json("/api/aim?box=8,12,16,20&cold=1")
            with viewer.Aim.lock:
                viewer.Aim.moving = True          # command is on the wire
            try:
                cd = {"ok": True, "r": 12.0, "c": 16.0, "n": 9,
                      "contrast": 30.0, "tbg": 22.0, "tth": 7.0, "reason": "ok"}
                # move count UNCHANGED — this is the window the reply missed
                snap = viewer.Aim.snapshot(b.url, cd, viewer.Aim.key, False,
                                           viewer.Aim.moves)
            finally:
                with viewer.Aim.lock:
                    viewer.Aim.moving = False
        self.assertTrue(snap["sample_dropped"])
        self.assertTrue(snap["moving"])

    def test_the_window_is_invalidated_before_the_command_goes_out(self):
        import inspect
        src = inspect.getsource(viewer.Aim.jog)
        self.assertLess(src.index("cls.moving = True"), src.index("urlopen"))
        self.assertLess(src.index("cls.generation += 1"), src.index("urlopen"))

    def test_a_failed_command_clears_moving_and_keeps_the_width_unknown(self):
        # A refused command may still have moved the head part of the way, so
        # the width must not be rolled back to a value nothing verified.
        with FakeBoard(servo_fails=True) as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1500).set("tilt", 1500)
            v.json("/api/aim?box=8,12,16,20&cold=1")
            _, r = v.json("/api/jog?axis=pan&d=25")
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertFalse(r["ok"])
        self.assertFalse(viewer.Aim.moving)
        self.assertFalse(d["moving"])

    def test_an_unreachable_board_leaves_the_width_assumed(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1500).set("tilt", 1500)
            v.json("/api/aim?box=8,12,16,20&cold=1")
            viewer.Viewer.board = "http://127.0.0.1:9"
            r = viewer.Aim.jog("http://127.0.0.1:9", "pan", 25)
        self.assertFalse(r["ok"])
        self.assertFalse(viewer.Aim.moving)
        self.assertTrue(viewer.Aim.assumed)


class TestWindowResetIsNotAMove(unittest.TestCase):
    """A second tab changing its box must not discard this tab's frame."""

    def test_a_key_reset_does_not_drop_an_in_flight_sample(self):
        # generation and moves were one counter; a key-only reset therefore
        # looked exactly like a jog, so two tabs aiming at different targets
        # made each other's windows intermittently empty.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            v.json("/api/aim?box=8,12,16,20&cold=1")
            moves0 = viewer.Aim.moves
            with viewer.Aim.lock:                 # another tab resets the window
                viewer.Aim.samples.clear()
                viewer.Aim.key = ("other", False, False, True)
                viewer.Aim.generation += 1        # NOT moves
            cd = {"ok": True, "r": 12.0, "c": 16.0, "n": 9, "contrast": 30.0,
                  "tbg": 22.0, "tth": 7.0, "reason": "ok"}
            snap = viewer.Aim.snapshot(b.url, cd, viewer.Aim.key, False, moves0)
        self.assertFalse(snap["sample_dropped"])
        self.assertEqual(snap["window"]["n"], 1)

    def test_two_tabs_on_different_boxes_keep_collecting(self):
        import threading as th
        with FakeBoard() as b, ViewerServer(b.url) as v:
            dropped = []

            def tab(box):
                for _ in range(8):
                    _, d = v.json(f"/api/aim?box={box}&cold=1")
                    dropped.append(d["sample_dropped"])

            ts = [th.Thread(target=tab, args=("8,12,16,20",)),
                  th.Thread(target=tab, args=("6,10,18,22",))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        # Nobody moved the head, so nothing should have been discarded.
        self.assertFalse(any(dropped), f"{sum(dropped)}/{len(dropped)} dropped")

    def test_a_real_jog_still_drops(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1500).set("tilt", 1500)
            v.json("/api/aim?box=8,12,16,20&cold=1")
            moves0 = viewer.Aim.moves
            v.json("/api/jog?axis=pan&d=25")
            cd = {"ok": True, "r": 12.0, "c": 16.0, "n": 9, "contrast": 30.0,
                  "tbg": 22.0, "tth": 7.0, "reason": "ok"}
            snap = viewer.Aim.snapshot(b.url, cd, viewer.Aim.key, False, moves0)
        self.assertTrue(snap["sample_dropped"])


class TestRefusalVersusLostReply(unittest.TestCase):
    """Two failures, two meanings — and servo.cpp settles which is which.

    Every `return false` in setUs() sits above its first pca call, so an
    explicit refusal means the axis was NOT driven: the width is exactly as
    good as it was. A lost reply says nothing at all, because the command may
    have landed.
    """

    def test_an_explicit_refusal_keeps_the_width_verified(self):
        with FakeBoard(servo_fails=True) as b, ViewerServer(b.url) as v:
            v.json("/api/aim?box=8,12,16,20&cold=1")
            _, r = v.json("/api/jog?axis=pan&d=25")
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertFalse(r["ok"])
        self.assertTrue(d["servo"]["verified"])
        self.assertEqual(d["servo"]["pan"], 1500)       # unchanged, as claimed

    def test_a_lost_reply_makes_the_width_unverified(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            v.json("/api/aim?box=8,12,16,20&cold=1")
            r = viewer.Aim.jog("http://127.0.0.1:9", "pan", delta=25)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("unverified"))
        self.assertFalse(viewer.Aim.verified)

    def test_a_relative_jog_refuses_on_an_unverified_width(self):
        # The head is at the old width or the new one, and arithmetic on
        # either is a guess wearing a number.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            v.json("/api/aim?box=8,12,16,20&cold=1")
            viewer.Aim.jog("http://127.0.0.1:9", "pan", delta=25)
            _, r = v.json("/api/jog?axis=pan&d=25")
        self.assertFalse(r["ok"])
        self.assertIn("unverified", r["reason"])
        self.assertEqual([us for ch, us in b.servo_log if ch == 5], [])

    def test_an_absolute_width_re_establishes_it(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            v.json("/api/aim?box=8,12,16,20&cold=1")
            viewer.Aim.jog("http://127.0.0.1:9", "pan", delta=25)
            _, r = v.json("/api/jog?axis=pan&us=1643")
            _, d = v.json("/api/aim?box=8,12,16,20&cold=1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["us"], 1643)
        self.assertTrue(d["servo"]["verified"])
        self.assertEqual(d["servo"]["pan"], 1643)

    def test_relative_jogging_resumes_after_an_absolute_width(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            v.json("/api/aim?box=8,12,16,20&cold=1")
            viewer.Aim.jog("http://127.0.0.1:9", "pan", delta=25)
            v.json("/api/jog?axis=pan&us=1643")
            _, r = v.json("/api/jog?axis=pan&d=25")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["us"], 1668)

    def test_an_absolute_width_is_clamped_to_the_electrical_span(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            v.json("/api/aim?box=8,12,16,20&cold=1")
            _, hi = v.json("/api/jog?axis=pan&us=9000")
            _, lo = v.json("/api/jog?axis=pan&us=1")
        self.assertEqual(hi["us"], viewer.US_MAX)
        self.assertEqual(lo["us"], viewer.US_MIN)

    def test_a_non_numeric_absolute_width_is_a_400(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            st, _ = v.get("/api/jog?axis=pan&us=typo")
        self.assertEqual(st, 400)


class TestLazyServoInit(unittest.TestCase):
    """"pan is not None" must mean BOTH widths are set."""

    def test_concurrent_first_probes_never_expose_a_half_set_state(self):
        import threading as th
        for _ in range(12):
            with FakeBoard() as b, ViewerServer(b.url) as v:
                viewer.Aim.pan = viewer.Aim.tilt = None
                viewer.Aim.datum = Datum().set("pan", 1643).set("tilt", 1498)
                out = []

                def probe():
                    out.append(viewer.Aim.servo(b.url))

                ts = [th.Thread(target=probe) for _ in range(6)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
            for st in out:
                if st["present"]:
                    # the failure this guards: pan set, tilt still None, and a
                    # tilt jog then evaluating None + delta
                    self.assertIsNotNone(st["tilt"], st)
                    self.assertEqual((st["pan"], st["tilt"]), (1643, 1498))

    def test_a_tilt_jog_racing_the_first_probe_does_not_crash(self):
        import threading as th
        with FakeBoard() as b, ViewerServer(b.url) as v:
            viewer.Aim.pan = viewer.Aim.tilt = None
            viewer.Aim.datum = Datum().set("pan", 1643).set("tilt", 1498)
            res = []
            ts = ([th.Thread(target=lambda: res.append(
                       v.json("/api/jog?axis=tilt&d=25")[1])) for _ in range(3)]
                  + [th.Thread(target=lambda: v.json("/api/aim?box=8,12,16,20&cold=1"))
                     for _ in range(3)])
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertTrue(all(r.get("ok") for r in res), res)


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

    def test_a_jog_is_one_transaction(self):
        # jog_lock covers read-command-commit; Aim.lock is taken only for the
        # short reads and writes inside it, so a ten-second board timeout
        # cannot freeze the aim panel.
        import inspect
        src = inspect.getsource(viewer.Aim.jog)
        self.assertIn("jog_lock", src)
        self.assertLess(src.index("jog_lock"), src.index("urlopen"))

    def test_aim_polls_are_serialised(self):
        # The server is threaded, so the 250 ms interval can start a poll
        # while the last is still waiting on /thermal — and a frame period IS
        # 250 ms. Two concurrent polls race for frames take() hands out once
        # each.
        html = self._html()
        self.assertIn("if(aimBusy) return;", html)
        # the flag must be claimed before the first await, and released only
        # by the poll that set it
        self.assertIn("aimBusy = mine;", html)
        self.assertIn("if(aimBusy===mine) aimBusy=null;", html)
        self.assertLess(html.index("aimBusy = mine;"), html.index("await mine"))

    def test_view_requests_are_stamped(self):
        html = self._html()
        self.assertIn("let statSeq=0", html)
        self.assertIn("mine!==statSeq", html)


class TestPage(unittest.TestCase):
    def _html(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            return v.get("/")[1].decode()

    def test_the_page_is_served_and_carries_its_controls(self):
        with FakeBoard() as b, ViewerServer(b.url) as v:
            st, body = v.get("/")
        self.assertEqual(st, 200)
        html = body.decode()
        for ident in ("id=live", "id=cold", "id=tflipv", "id=tfliph", "id=zoom",
                      "id=sx", "id=sy", "id=owarn", "id=est", "id=nf", "id=cmd"):
            self.assertIn(ident, html, ident)

    def test_an_ipv6_host_is_actually_bindable(self):
        # The banner treats ::1 as a local address; ThreadingHTTPServer is
        # AF_INET, so accepting the value while being unable to bind it is a
        # promise the code cannot keep.
        import inspect
        src = inspect.getsource(viewer.main)
        self.assertIn("AF_INET6", src)
        self.assertLess(src.index("AF_INET6"), src.index("server_cls((host"))

    def test_the_page_works_on_a_phone(self):
        """Three things, and missing any one of them means it does not.

        Without the viewport meta a phone lays the page out at 980 px and
        scales it down, so every number is unreadable. Without
        touch-action:none a drag scrolls the page instead of drawing a box —
        and drawing the box is the one thing the page is for. Without the
        narrow-screen rule the image keeps 64vw of a screen that has little to
        spare.
        """
        html = self._html()
        self.assertIn("name=viewport", html)
        self.assertIn("width=device-width", html)
        self.assertIn("touch-action:none", html)
        self.assertIn("@media (max-width:760px)", html)
        # pointer events, not mouse events: the same handlers must serve both
        self.assertIn("pointerdown", html)
        self.assertNotIn("addEventListener('mousedown'", html)

    def test_the_recovery_control_is_on_the_page(self):
        """The backend refuses relative steps after a lost reply; the page has
        to offer the way out, or a transient timeout ends the session.

        The first version of this added the API and left the UI sending `d`
        only — a recovery reachable solely by hand-building an undocumented
        request, which is no recovery.
        """
        html = self._html()
        for ident in ("id=panabs", "id=tiltabs", "data-set=pan", "data-set=tilt",
                      "id=jwarn"):
            self.assertIn(ident, html, ident)
        self.assertIn("axis=${ax}&us=", html)          # absolute form is sent
        self.assertIn("verified===false", html)        # and the state is shown

    def test_the_javascript_wires_every_control_it_renders(self):
        # The failure this catches: an edit whose anchor no longer matched
        # left the warning element on the page with nothing ever writing to
        # it — silently, because a missing write looks exactly like no warning.
        with FakeBoard() as b, ViewerServer(b.url) as v:
            html = v.get("/")[1].decode()
        for ref in ("$('#owarn')", "$('#est')", "$('#nf')", "$('#cmd')",
                    "$('#zoom')", "$('#live')", "$('#cold')", "$('#tflipv')",
                    "$('#jwarn')"):
            self.assertIn(ref, html, ref)


if __name__ == "__main__":
    unittest.main(verbosity=2)
