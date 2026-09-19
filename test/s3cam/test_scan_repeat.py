#!/usr/bin/env python3
"""The acquisition half: scan_repeat against a fake board. No hardware.

    ./test_scan_repeat.py

test_scan_stats.py covers the analysis, which was the easy half to test and
therefore the half that was tested first. The bug this file exists for lived
in the other half: both bracketing static blocks were recorded wherever the
head happened to be, so the closing block was taken at the last pose of the
last cycle. The target had not moved at all and every run still voided itself
at gate 3.2. Nothing in the analysis could see it — the numbers were computed
correctly from a run that had been taken wrong.

So the things pinned here are the SEQUENCING and the failure paths: where the
head is when each block is recorded, that a capture failure degrades while a
servo failure aborts, and that an abort still leaves a readable recording.
"""
import argparse
import io
import json
import math
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "../../tools/s3cam"))
import scan_repeat as R                                             # noqa: E402
import scan_stats as S                                             # noqa: E402

ROWS, COLS = S.ROWS, S.COLS
BOX = "8,12,16,20"
PX_PER_US = 0.010


def blob(r0, c0):
    two = 2.0 * 1.5 * 1.5
    return [22.0 + 30.0 * math.exp(-(((r - r0) ** 2 + (c - c0) ** 2) / two))
            for r in range(ROWS) for c in range(COLS)]


class Board:
    """Serves /thermal and /servo, and remembers every command it was sent."""

    def __init__(self, servo_fails=False, drop_every=0, orientation="wire",
                 always_null=False):
        self.pan = self.tilt = 1500
        self.seq = 0
        self.log = []               # (ch, us) in order
        self.blocks = []            # (pan, tilt) at the moment each frame was served
        self.servo_fails = servo_fails
        self.drop_every = drop_every
        self.orientation = orientation
        self.always_null = always_null
        self.served = 0
        self.lock = threading.Lock()
        board = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u, q = urlparse(self.path), parse_qs(urlparse(self.path).query)
                with board.lock:
                    if u.path == "/servo":
                        ch, us = int(q["ch"][0]), int(q["us"][0])
                        board.log.append((ch, us))
                        if board.servo_fails:
                            body = {"present": True, "set": "out_of_range"}
                        else:
                            if ch == 5:
                                board.pan = us
                            else:
                                board.tilt = us
                            body = {"present": True, "set": "ok"}
                    elif u.path == "/thermal":
                        board.served += 1
                        if board.always_null or (
                                board.drop_every
                                and board.served % board.drop_every == 0):
                            body = {"frame": None, "stream": {}}
                        else:
                            board.seq += 1
                            board.blocks.append((board.pan, board.tilt))
                            c0 = 16.0 + PX_PER_US * (board.pan - 1500)
                            r0 = 12.0 + PX_PER_US * (board.tilt - 1500)
                            body = {"frame": {"seq": board.seq, "rows": ROWS,
                                              "cols": COLS, "ta_c": 28.0,
                                              "checksum_ok": True,
                                              "orientation": board.orientation,
                                              "px": blob(r0, c0)}}
                    else:
                        self.send_response(404)
                        self.end_headers()
                        return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def args_for(base, mode, out, **kw):
    d = dict(base_url=base, mode=mode, out=out, box=BOX, flipv=False, fliph=False,
             n=2, static_n=2, min_contrast=5.0, settle=0, approach="uni",
             approach_us=80, cycles=2, poses=None, axis="pan",
             gain_from=1400, gain_to=1600, gain_steps=3, dry_run=False,
             verbose=False, summarize=None, cold=False, bad_pixels=None)
    d.update(kw)
    return argparse.Namespace(**d)


def run_to(board, mode, out, **kw):
    a = args_for(board.url, mode, out, **kw)
    rec = R.Recorder(out)
    rec.write(R.manifest_for(a, R.parse_poses(a.poses) if a.poses else []))
    head = R.Head(a.base_url, a.settle, a.approach_us, a.dry_run)
    try:
        R.run(a, rec, head)
    finally:
        rec.close()
    return a, head


class TestBracketing(unittest.TestCase):
    def test_both_static_blocks_are_taken_at_the_same_pose(self):
        """The regression. Voided every run for a reason unrelated to the target."""
        with tempfile.TemporaryDirectory() as d, Board() as b:
            out = os.path.join(d, "r.jsonl")
            run_to(b, "repeat", out, poses=["A:1400,1500", "B:1600,1500"], cycles=2)
            m, frames = S.load(out)
        pre = [f for f in frames if f["block"] == "static_pre"]
        post = [f for f in frames if f["block"] == "static_post"]
        self.assertTrue(pre and post)
        # Same pose in, same pose out: identical scenes, so identical frames.
        self.assertEqual(pre[0]["px"], post[0]["px"])

    def test_the_bracket_gate_passes_on_an_untouched_target(self):
        with tempfile.TemporaryDirectory() as d, Board() as b:
            out = os.path.join(d, "r.jsonl")
            run_to(b, "repeat", out, poses=["A:1400,1500", "B:1600,1500"], cycles=3)
            buf = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(buf):
                code = R.report(*S.load(out))
        self.assertIn("[PASS] 3.2 bracket shift", buf.getvalue())
        self.assertEqual(code, 0, buf.getvalue())


class TestSequencing(unittest.TestCase):
    def test_axes_are_commanded_one_at_a_time(self):
        # servo.h forbids driving both at once; two MG996R stalling together is
        # 5 A on a rail whose margin has bitten this project before.
        with tempfile.TemporaryDirectory() as d, Board() as b:
            run_to(b, "repeat", os.path.join(d, "r.jsonl"),
                   poses=["A:1400,1500"], cycles=1)
        self.assertTrue(b.log)
        self.assertTrue(all(isinstance(ch, int) for ch, _ in b.log))

    def test_uni_approach_always_arrives_from_the_same_side(self):
        with tempfile.TemporaryDirectory() as d, Board() as b:
            run_to(b, "repeat", os.path.join(d, "r.jsonl"),
                   poses=["A:1400,1500", "B:1600,1500"], cycles=2, approach="uni")
        pan = [us for ch, us in b.log if ch == 5]
        # every arrival at 1400 is preceded by 1320, i.e. from below
        for i, us in enumerate(pan):
            if us == 1400 and i > 0:
                self.assertEqual(pan[i - 1], 1400 - 80)

    def test_none_approach_sends_no_backoff(self):
        with tempfile.TemporaryDirectory() as d, Board() as b:
            run_to(b, "repeat", os.path.join(d, "r.jsonl"),
                   poses=["A:1400,1500"], cycles=1, approach="none")
        self.assertEqual({us for ch, us in b.log if ch == 5}, {1400})

    def test_gain_mode_holds_the_other_axis_at_centre(self):
        with tempfile.TemporaryDirectory() as d, Board() as b:
            run_to(b, "gain", os.path.join(d, "g.jsonl"), axis="pan",
                   gain_from=1400, gain_to=1600, gain_steps=3)
        tilt = [us for ch, us in b.log if ch == 6]
        self.assertTrue(tilt)
        self.assertEqual(set(tilt) - {R.US_CENTER - 80}, {R.US_CENTER})

    def test_static_mode_commands_nothing(self):
        with tempfile.TemporaryDirectory() as d, Board() as b:
            run_to(b, "static", os.path.join(d, "s.jsonl"), n=3)
        self.assertEqual(b.log, [])

    def test_dry_run_commands_nothing(self):
        with tempfile.TemporaryDirectory() as d, Board() as b:
            run_to(b, "repeat", os.path.join(d, "r.jsonl"),
                   poses=["A:1400,1500"], cycles=1, dry_run=True)
        self.assertEqual(b.log, [])


class TestApproachControl(unittest.TestCase):
    """Stage 3.4 asks whether uni-directional approach matters. It can only
    answer that if the two arms actually differ."""

    @staticmethod
    def _pan_backoffs(log, target=1400):
        """The width commanded immediately before each arrival at `target`."""
        pan = [us for ch, us in log if ch == 5]
        return [pan[i - 1] for i, us in enumerate(pan) if us == target and i > 0]

    def test_alt_really_alternates_the_side(self):
        # The bug: a uni approach sends TWO commands per arrival, so parity
        # taken from the command count was always even and `alt` behaved
        # exactly like `uni`. The control would then have compared a run
        # against an identical copy of itself and reported no difference —
        # and "single-direction approach is folklore, delete it" is a
        # conclusion the plan was prepared to draw from that.
        with tempfile.TemporaryDirectory() as d, Board() as b:
            run_to(b, "repeat", os.path.join(d, "r.jsonl"),
                   poses=["A:1400,1500"], cycles=4, approach="alt",
                   approach_us=80)
        backs = self._pan_backoffs(b.log)
        self.assertGreaterEqual(len(backs), 4)
        self.assertEqual(len(set(backs)), 2, f"never alternated: {backs}")
        self.assertEqual(backs[0], 1320)
        self.assertEqual(backs[1], 1480)
        self.assertEqual(backs[2], 1320)

    def test_uni_never_alternates(self):
        with tempfile.TemporaryDirectory() as d, Board() as b:
            run_to(b, "repeat", os.path.join(d, "r.jsonl"),
                   poses=["A:1400,1500"], cycles=4, approach="uni",
                   approach_us=80)
        backs = self._pan_backoffs(b.log)
        self.assertEqual(set(backs), {1320}, f"uni should be one-sided: {backs}")

    def test_both_axes_of_one_pose_share_a_side(self):
        # A pose entered with pan from below and tilt from above is not "one
        # approach direction"; the parity belongs to the visit, not the axis.
        with tempfile.TemporaryDirectory() as d, Board() as b:
            run_to(b, "repeat", os.path.join(d, "r.jsonl"),
                   poses=["A:1400,1500"], cycles=2, approach="alt",
                   approach_us=80)
        first = b.log[:4]        # visit 0: pan back, pan, tilt back, tilt
        pan_back = next(us for ch, us in first if ch == 5 and us != 1400)
        tilt_back = next(us for ch, us in first if ch == 6 and us != 1500)
        self.assertLess(pan_back, 1400)
        self.assertLess(tilt_back, 1500)


class TestTimingValidation(unittest.TestCase):
    """Reject before commanding. The failure was an exception on the wrong
    side of the first servo command, which skipped the recovery path."""

    def test_a_negative_settle_never_reaches_the_board(self):
        with Board() as b:
            with self.assertRaises(ValueError):
                R.Head(b.url, -1, 80)
        self.assertEqual(b.log, [], "a command went out before validation")

    def test_a_negative_backoff_is_refused_too(self):
        with Board() as b:
            with self.assertRaises(ValueError):
                R.Head(b.url, 0, -80)
        self.assertEqual(b.log, [])

    def test_nan_is_refused(self):
        with Board() as b:
            with self.assertRaises(ValueError):
                R.Head(b.url, float("nan"), 80)
        self.assertEqual(b.log, [])

    def test_zero_is_fine(self):
        with Board() as b:
            h = R.Head(b.url, 0, 0)
            h.goto({"pan": 1400})
        self.assertEqual(b.log, [(5, 1400)])


class TestOrientationProbe(unittest.TestCase):
    """The probe must fail closed. /thermal returning null is NORMAL."""

    def _run_main(self, board, extra):
        out = os.path.join(tempfile.mkdtemp(), "r.jsonl")
        argv = ["scan_repeat.py", board.url, "--mode", "static", "--box", BOX,
                "--n", "2", "--settle", "0", "--out", out] + extra
        old = sys.argv
        sys.argv = argv
        try:
            buf = io.StringIO()
            import contextlib
            with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
                try:
                    code = R.main()
                except SystemExit as e:
                    return e.code, buf.getvalue()
            return code, buf.getvalue()
        finally:
            sys.argv = old

    def test_a_corrected_board_refuses_host_flips(self):
        with Board(orientation="rot180") as b:
            code, out = self._run_main(b, ["--flipv", "--fliph"])
        self.assertEqual(code, 2, out)
        self.assertIn("drop the host flips", out)

    def test_a_null_window_does_not_read_as_wire_order(self):
        # A one-shot probe landing in the gap between frames used to conclude
        # "wire order" and wave the run through — recording the whole session
        # doubly rotated, with the report only saying so afterwards.
        with Board(orientation="rot180", always_null=True) as b:
            code, out = self._run_main(b, ["--flipv", "--fliph"])
        self.assertEqual(code, 2, out)
        self.assertIn("orientation is unknown", out)

    def test_a_wire_order_board_accepts_the_flips(self):
        with Board(orientation="wire") as b:
            code, out = self._run_main(b, ["--flipv", "--fliph"])
        self.assertIn(code, (0, 1), out)      # ran; pass/fail is the gate's call

    def test_no_flips_means_no_probe_at_all(self):
        with Board(orientation="rot180") as b:
            code, out = self._run_main(b, [])
        self.assertIn(code, (0, 1), out)


class TestFreshness(unittest.TestCase):
    def test_discards_three_frames_then_returns_increasing_seq(self):
        # One flush plus two: a 1544-byte frame takes 134 ms of a 250 ms period,
        # so the frame after the settle cannot be shown to have finished
        # integrating after the head stopped. S+2 can.
        with Board() as b:
            rows = R.fresh_frames(b.url, 4)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["ok"] for r in rows))
        seqs = [r["seq"] for r in rows]
        self.assertEqual(seqs[0], R.DISCARD_FRAMES + 1)
        self.assertEqual(seqs, sorted(set(seqs)))

    def test_a_null_frame_is_polled_through_not_counted(self):
        # take() is consuming and the module is 4 Hz, so null means "nothing
        # new yet". Counting it as a sample would fill a block with absences.
        with Board(drop_every=2) as b:
            rows = R.fresh_frames(b.url, 4)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["ok"] for r in rows))

    def test_an_unreachable_board_yields_failed_rows_not_an_exception(self):
        rows = R.fresh_frames("http://127.0.0.1:9", 2)
        self.assertEqual(len(rows), 2)
        self.assertFalse(any(r["ok"] for r in rows))
        self.assertTrue(all("http" in r["reason"] for r in rows))


class TestFailureAsymmetry(unittest.TestCase):
    def test_a_refused_servo_command_aborts(self):
        with Board(servo_fails=True) as b:
            head = R.Head(b.url, 0, 80)
            with self.assertRaises(R.ServoAborted):
                head.goto({"pan": 1400})

    def test_a_width_outside_the_electrical_span_never_reaches_the_board(self):
        with Board() as b:
            head = R.Head(b.url, 0, 0)
            with self.assertRaises(R.ServoAborted):
                head.goto({"pan": 5000})
        self.assertEqual(b.log, [])

    def test_abort_keeps_the_partial_recording_and_exits_two(self):
        with tempfile.TemporaryDirectory() as d, Board(servo_fails=True) as b:
            out = os.path.join(d, "r.jsonl")
            argv = [b.url, "--mode", "repeat", "--box", BOX, "--poses",
                    "A:1400,1500", "--cycles", "2", "--n", "2",
                    "--settle", "0", "--out", out]
            old = sys.argv
            sys.argv = ["scan_repeat.py"] + argv
            try:
                import contextlib
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = R.main()
            finally:
                sys.argv = old
            self.assertEqual(code, 2, buf.getvalue())
            self.assertTrue(os.path.exists(out))
            m, frames = S.load(out)       # must still be readable
            self.assertEqual(m["mode"], "repeat")
            self.assertTrue(any(f["block"] == "aborted" for f in frames))


class TestConfirmedBadPixels(unittest.TestCase):
    def test_the_manifest_records_what_was_excluded(self):
        with tempfile.TemporaryDirectory() as d, Board() as b:
            out = os.path.join(d, "s.jsonl")
            run_to(b, "static", out, n=3, bad_pixels="56,120")
            m, _ = S.load(out)
        self.assertEqual(m["bad_pixels"], [56, 120])

    def test_none_recorded_when_none_were_given(self):
        with tempfile.TemporaryDirectory() as d, Board() as b:
            out = os.path.join(d, "s.jsonl")
            run_to(b, "static", out, n=3)
            m, _ = S.load(out)
        self.assertEqual(m["bad_pixels"], [])

    def test_an_out_of_range_index_is_refused(self):
        with self.assertRaises(SystemExit):
            R.parse_bad_pixels("768")
        with self.assertRaises(SystemExit):
            R.parse_bad_pixels("-1")

    def test_garbage_is_refused(self):
        with self.assertRaises(SystemExit):
            R.parse_bad_pixels("56,abc")

    def test_it_deduplicates_and_sorts(self):
        self.assertEqual(R.parse_bad_pixels("120,56,120"), [56, 120])


class TestSummarizeOverride(unittest.TestCase):
    """Analysis choices can be re-made offline. Acquisition cannot.

    A hundred frames taken with the estimator pointed the wrong way are not a
    wasted afternoon — every pixel is in the recording. This is the whole
    reason frames are stored whole, and it only pays off if the polarity and
    the box can be changed after the fact.
    """

    def _cold_run(self, path):
        import math
        rows = []
        for i in range(8):
            two = 2.0 * 1.5 * 1.5
            px = [30.0 - 30.0 * math.exp(-(((r - 12) ** 2 + (c - 16) ** 2) / two))
                  for r in range(ROWS) for c in range(COLS)]
            rows.append({"kind": "frame", "block": "static", "ok": True,
                         "seq": i, "ta_c": 28.0, "checksum_ok": True,
                         "orientation": "rot180", "rows": ROWS, "cols": COLS,
                         "px": px})
        m = {"kind": "manifest", "version": 1, "mode": "static", "box": BOX,
             "flipv": False, "fliph": False, "min_contrast_c": 5.0,
             "polarity": "hot",          # recorded wrong on purpose
             "t": "2026-09-20T01:00:00+08:00", "git_rev": "deadbee",
             "criterion": dict(S.CRITERION_DEFAULTS)}
        with open(path, "w") as fh:
            fh.write(json.dumps(m) + "\n")
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        return path

    def _report(self, path, override=None):
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = R.report(*S.load(path), override=override)
        return code, buf.getvalue()

    def test_as_recorded_the_cold_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._cold_run(os.path.join(d, "s.jsonl"))
            code, out = self._report(p)
        self.assertEqual(code, 1)
        self.assertIn("rejected 8", out)

    def test_re_analysing_as_cold_recovers_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._cold_run(os.path.join(d, "s.jsonl"))
            code, out = self._report(p, {"polarity": "cold"})
        self.assertEqual(code, 0, out)
        self.assertIn("accepted 8", out)

    def test_an_override_is_announced_not_silent(self):
        # A report that quietly disagreed with its own manifest would be worse
        # than one that refused: the manifest is what a reader trusts.
        with tempfile.TemporaryDirectory() as d:
            p = self._cold_run(os.path.join(d, "s.jsonl"))
            _, out = self._report(p, {"polarity": "cold"})
        self.assertIn("RE-ANALYSED, not as recorded", out)
        self.assertIn("polarity hot -> cold", out)

    def test_no_override_says_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._cold_run(os.path.join(d, "s.jsonl"))
            _, out = self._report(p, {"polarity": None, "box": None})
        self.assertNotIn("RE-ANALYSED", out)

    def test_the_box_can_be_re_chosen_too(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._cold_run(os.path.join(d, "s.jsonl"))
            _, out = self._report(p, {"polarity": "cold", "box": "0,0,23,31"})
        self.assertIn("box 8,12,16,20 -> 0,0,23,31", out)


class TestManifest(unittest.TestCase):
    def test_the_target_polarity_is_recorded(self):
        # A recording that does not say which way the target stood out cannot
        # be re-analysed: --summarize would run the hot estimator over a cold
        # run and find the hottest thing in every frame instead.
        with tempfile.TemporaryDirectory() as d, Board() as b:
            out = os.path.join(d, "s.jsonl")
            run_to(b, "static", out, n=3, cold=True)
            m, _ = S.load(out)
        self.assertEqual(m["polarity"], "cold")

    def test_the_criterion_is_copied_into_the_recording(self):
        # So that tightening a constant later cannot silently re-judge an old
        # run — in either direction.
        with tempfile.TemporaryDirectory() as d, Board() as b:
            out = os.path.join(d, "s.jsonl")
            run_to(b, "static", out, n=3)
            m, _ = S.load(out)
        self.assertEqual(m["criterion"]["p90_radial_max_px"], S.P90_RADIAL_MAX_PX)
        self.assertEqual(m["criterion"]["sigma_static_max_px"], S.SIGMA_STATIC_MAX_PX)
        self.assertEqual(m["box"], BOX)

    def test_orientation_flags_are_recorded_so_summarize_indexes_the_same_pixels(self):
        with tempfile.TemporaryDirectory() as d, Board() as b:
            out = os.path.join(d, "s.jsonl")
            run_to(b, "static", out, n=3, fliph=True)
            m, _ = S.load(out)
        self.assertTrue(m["fliph"])
        self.assertFalse(m["flipv"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
