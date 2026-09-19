#!/usr/bin/env python3
"""scan_stats + the scan_repeat report, on synthetic runs. No board.

    ./test_scan_stats.py        # exits non-zero on failure

Every expected value here is derived independently of the code under test —
by hand from the definition, or by construction (frames that are byte-for-byte
identical must produce exactly zero spread). A test whose expectation is "what
it printed last time" only detects change, and the thing this measurement can
fail at is being subtly wrong from the start.

The end-to-end cases matter most. Phase 3's whole output is a pass/fail against
a criterion fixed in advance, so the case worth pinning is not "the numbers are
plausible" but "a run that should fail DOES fail" — a gate that silently passes
on missing data would let a broken afternoon look like a good servo.
"""
import contextlib
import io
import json
import math
import os
import statistics
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "../../tools/s3cam"))
import scan_stats as S                                              # noqa: E402
import scan_repeat as R                                             # noqa: E402

ROWS, COLS = S.ROWS, S.COLS
TBG, PEAK, SIGMA = 22.0, 30.0, 1.5
BOX = "8,12,16,20"


def blob(r0, c0, tbg=TBG, peak=PEAK, sigma=SIGMA):
    """A Gaussian target at sub-pixel (r0, c0) on a flat background."""
    two_s2 = 2.0 * sigma * sigma
    return [tbg + peak * math.exp(-(((r - r0) ** 2 + (c - c0) ** 2) / two_s2))
            for r in range(ROWS) for c in range(COLS)]


def frame_row(px, block, seq, **extra):
    return {"kind": "frame", "block": block, "ok": True, "seq": seq,
            "ta_c": 28.0, "checksum_ok": True, "rows": ROWS, "cols": COLS,
            "px": px, **extra}


def write_run(path, mode, rows, **manifest):
    m = {"kind": "manifest", "version": 1, "mode": mode, "box": BOX,
         "flipv": False, "fliph": False, "min_contrast_c": 5.0,
         "t": "2026-09-18T22:00:00+08:00", "git_rev": "deadbee", **manifest}
    with open(path, "w") as fh:
        fh.write(json.dumps(m) + "\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def quietly(fn, *a, **kw):
    """Run something that prints a report; return (exit_code, output)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*a, **kw)
    return code, buf.getvalue()


class TestPercentile(unittest.TestCase):
    def test_interpolates_the_numpy_way(self):
        # 0..9: k = 9 * 0.9 = 8.1, so 8 + 0.1*(9-8) = 8.1. Derived from the
        # definition, not from running it.
        self.assertAlmostEqual(S.percentile(list(range(10)), 90), 8.1)
        self.assertAlmostEqual(S.percentile(list(range(10)), 0), 0.0)
        self.assertAlmostEqual(S.percentile(list(range(10)), 100), 9.0)

    def test_p90_of_ten_is_nearly_the_worst(self):
        # Worth pinning because it changes how a "pass" should be read: with
        # ten cycles the criterion sits between the 9th and 10th ranked sample.
        xs = [0.0] * 9 + [5.0]
        self.assertAlmostEqual(S.percentile(xs, 90), 0.5)

    def test_degenerate_inputs(self):
        self.assertIsNone(S.percentile([], 90))
        self.assertEqual(S.percentile([3.0], 90), 3.0)


class TestPoseStats(unittest.TestCase):
    def test_radial_and_p2p_by_hand(self):
        # Four samples arranged on a cross about (10, 10), radius 1 each.
        s = [{"r": 9.0, "c": 10.0}, {"r": 11.0, "c": 10.0},
             {"r": 10.0, "c": 9.0}, {"r": 10.0, "c": 11.0}]
        st = S.pose_stats(s)
        self.assertAlmostEqual(st["mean_r"], 10.0)
        self.assertAlmostEqual(st["mean_c"], 10.0)
        self.assertAlmostEqual(st["max_radial"], 1.0)
        self.assertAlmostEqual(st["p90_radial"], 1.0)   # all four are exactly 1
        self.assertAlmostEqual(st["p2p_r"], 2.0)
        self.assertAlmostEqual(st["p2p_c"], 2.0)

    def test_one_sample_reports_nothing_rather_than_zero(self):
        # A single cycle has no spread to report. Returning 0.0 would be a
        # perfect score for a pose that was visited once.
        st = S.pose_stats([{"r": 1.0, "c": 2.0}])
        self.assertIsNone(st["p90_radial"])
        self.assertIsNone(st["p2p_r"])


class TestBlockStats(unittest.TestCase):
    def test_sigma_is_population_stdev(self):
        s = [{"r": r, "c": 0.0, "contrast": 10.0} for r in (1.0, 2.0, 3.0)]
        st = S.block_stats(s)
        self.assertAlmostEqual(st["sigma_r"], statistics.pstdev([1.0, 2.0, 3.0]))
        self.assertAlmostEqual(st["sigma_c"], 0.0)
        self.assertAlmostEqual(st["contrast_drift"], 0.0)

    def test_contrast_drift_is_range_over_mean(self):
        # 8,10,12 -> range 4, mean 10 -> 0.4. Range rather than end-to-end so a
        # target that cools and is warmed again is not scored as steady.
        s = [{"r": 0.0, "c": 0.0, "contrast": v} for v in (8.0, 12.0, 10.0)]
        self.assertAlmostEqual(S.block_stats(s)["contrast_drift"], 0.4)


class TestHalfSplitAndBracket(unittest.TestCase):
    def test_monotonic_drift_is_visible(self):
        s = [{"r": float(i), "c": 0.0} for i in range(10)]
        d = S.half_split_drift(s)
        # mean(5..9) - mean(0..4) = 7 - 2 = 5
        self.assertAlmostEqual(d["d_r"], 5.0)
        self.assertAlmostEqual(d["d_c"], 0.0)

    def test_too_few_repeats_reports_nothing(self):
        self.assertIsNone(S.half_split_drift([{"r": 1.0, "c": 1.0}] * 3)["d_r"])

    def test_bracket_shift_is_the_distance_between_means(self):
        pre = [{"r": 0.0, "c": 0.0, "contrast": 9.0}] * 2
        post = [{"r": 3.0, "c": 4.0, "contrast": 9.0}] * 2
        self.assertAlmostEqual(S.bracket_shift(pre, post), 5.0)

    def test_bracket_shift_without_a_block_is_none_not_zero(self):
        self.assertIsNone(S.bracket_shift([], [{"r": 1.0, "c": 1.0, "contrast": 9.0}] * 2))


class TestBadPixels(unittest.TestCase):
    def test_finds_the_flickering_pixel_and_repairs_it(self):
        idx = 5 * COLS + 7
        frames = []
        for i in range(6):
            px = blob(12.0, 16.0)
            px[idx] = TBG + (60.0 if i % 2 else -60.0)   # alternating, huge
            frames.append({"ok": True, "px": px})
        bad = S.bad_pixel_candidates(frames)
        self.assertEqual(bad, [idx])
        fixed = S.repair(frames[0]["px"], bad)
        neigh = [frames[0]["px"][(5 - 1) * COLS + 7], frames[0]["px"][(5 + 1) * COLS + 7],
                 frames[0]["px"][5 * COLS + 6], frames[0]["px"][5 * COLS + 8]]
        self.assertAlmostEqual(fixed[idx], statistics.median(neigh))

    def test_repair_returns_a_copy(self):
        px = blob(12.0, 16.0)
        before = list(px)
        S.repair(px, [0])
        self.assertEqual(px, before)

    def test_a_still_scene_flags_nothing(self):
        frames = [{"ok": True, "px": blob(12.0, 16.0)} for _ in range(5)]
        self.assertEqual(S.bad_pixel_candidates(frames), [])


class TestBadPixelPersistence(unittest.TestCase):
    """A defect does not move. Two runs that disagree are evidence of neither.

    Measured on the bench: the spread distribution's own p99 sits at 3.7-5.4x
    the median, so the candidate threshold lies INSIDE the noise tail and
    which pixels cross it is luck. Two consecutive 100-frame runs produced
    [212] and [56, 120] — disjoint, with each run's pixels sitting at 4.7x and
    1.1x in the other. Repairing on one run's list would inject a different
    correction into every run of a measurement built on comparing runs.
    """

    def test_the_intersection_of_disjoint_lists_is_empty(self):
        self.assertEqual(S.confirmed_bad_pixels([[212], [56, 120]]), [])

    def test_a_pixel_in_every_run_survives(self):
        self.assertEqual(S.confirmed_bad_pixels([[7, 212], [7, 56], [7, 99]]), [7])

    def test_one_list_alone_confirms_nothing_beyond_itself(self):
        # A single run is allowed to stand for itself, but the caller has to
        # ask for exactly that — it is not what several runs would have said.
        self.assertEqual(S.confirmed_bad_pixels([[3, 4]]), [3, 4])

    def test_no_lists_is_empty_not_an_error(self):
        self.assertEqual(S.confirmed_bad_pixels([]), [])
        self.assertEqual(S.confirmed_bad_pixels([None]), [])


class TestGain(unittest.TestCase):
    def test_perfect_line_and_a_silent_cross_axis(self):
        # c moves 0.05 px per us, r does not move at all.
        pts = [(us, 12.0, 16.0 + 0.05 * (us - 1500)) for us in range(1300, 1701, 50)]
        g = S.gain_stats(pts)
        self.assertEqual(g["main"], "c")
        self.assertAlmostEqual(g["fit_c"]["slope"], 0.05)
        self.assertAlmostEqual(g["fit_c"]["r2"], 1.0)
        self.assertAlmostEqual(g["cross_ratio"], 0.0)
        self.assertAlmostEqual(g["gain_variation"], 0.0, places=9)

    def test_r2_is_none_when_the_axis_does_not_move(self):
        # The cross axis scores terribly on R-squared precisely when it is
        # behaving; this is why gain_stats judges it by slope ratio instead.
        f = S.fit_line([1.0, 2.0, 3.0], [7.0, 7.0, 7.0])
        self.assertAlmostEqual(f["slope"], 0.0)
        self.assertIsNone(f["r2"])

    def test_sagging_gain_at_one_end_is_reported(self):
        # First half 0.05 px/us, second half 0.02: a line still fits well.
        pts = []
        pos = 16.0
        for i, us in enumerate(range(1300, 1701, 50)):
            pts.append((us, 12.0, pos))
            pos += (0.05 if i < 4 else 0.02) * 50
        g = S.gain_stats(pts)
        self.assertGreater(g["gain_variation"], S.GAIN_VARIATION_MAX)


class TestNoMotion(unittest.TestCase):
    def test_a_sweep_where_nothing_moved_is_a_result_not_a_crash(self):
        # A stalled servo, a horn slipping on its spline, a linkage adrift:
        # every one of them produces two zero slopes, and the cross term is
        # then 0/0. Raising here would replace the most informative possible
        # measurement with a traceback.
        pts = [(us, 12.0, 16.0) for us in range(1300, 1701, 50)]
        g = S.gain_stats(pts)
        self.assertIsNone(g["main"])
        self.assertEqual(g["reason"], "no_motion")

    def test_the_report_names_it_mechanical(self):
        with tempfile.TemporaryDirectory() as d:
            rows = []
            for i, us in enumerate(range(1300, 1701, 50)):
                for j in range(3):
                    rows.append(frame_row(blob(12.0, 16.0), f"us={us}",
                                          i * 3 + j, pan_us=us))
            p = write_run(os.path.join(d, "g.jsonl"), "gain", rows, axis="pan", n=3)
            code, out = quietly(R.report, *S.load(p))
        self.assertEqual(code, 1, out)
        self.assertIn("NO MOTION", out)


class TestCriterionComesFromTheRecording(unittest.TestCase):
    """Tightening a constant must not re-judge an afternoon that already ran."""

    def _tight_run(self, path, **manifest):
        rows = [frame_row(blob(12.0, 16.0), "static_pre", i) for i in range(4)]
        seq = 100
        for cyc in range(6):
            for j in range(2):
                seq += 1
                rows.append(frame_row(blob(12.0, 15.0), "pose:A", seq,
                                      cycle=cyc, pose="A"))
        rows += [frame_row(blob(12.0, 16.0), "static_post", seq + 9 + i)
                 for i in range(4)]
        return write_run(path, "repeat", rows, cycles=6, settle_ms=400,
                         approach="uni", n=2, **manifest)

    def test_a_recorded_threshold_beats_todays_constant(self):
        # This run has zero spread and passes under today's 1.0 px. Recorded
        # against an impossible bar, it must FAIL — proving the gate reads the
        # manifest rather than the module.
        strict = dict(S.CRITERION_DEFAULTS, p90_radial_max_px=-1.0)
        with tempfile.TemporaryDirectory() as d:
            p = self._tight_run(os.path.join(d, "r.jsonl"), criterion=strict)
            code, out = quietly(R.report, *S.load(p))
        self.assertEqual(code, 1, out)
        self.assertIn("[FAIL] 3.1 A P90 radial", out)
        self.assertIn("limit -1.0", out)

    def test_a_recording_without_a_criterion_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._tight_run(os.path.join(d, "r.jsonl"))
            code, out = quietly(R.report, *S.load(p))
        self.assertIn("predates", out)
        self.assertIn("p90_radial_max_px", out)
        self.assertEqual(code, 0, out)          # falls back, still judges

    def test_criterion_reports_exactly_which_keys_were_missing(self):
        crit, missing = S.criterion({"criterion": {"p90_radial_max_px": 0.5}})
        self.assertEqual(crit["p90_radial_max_px"], 0.5)
        self.assertEqual(crit["p2p_axis_max_px"], S.P2P_AXIS_MAX_PX)
        self.assertIn("p2p_axis_max_px", missing)
        self.assertNotIn("p90_radial_max_px", missing)

    def test_defaults_match_the_constants(self):
        # The dict and the constants are two spellings of one truth; this is
        # the only thing stopping them drifting.
        self.assertEqual(S.CRITERION_DEFAULTS["p90_radial_max_px"], S.P90_RADIAL_MAX_PX)
        self.assertEqual(S.CRITERION_DEFAULTS["gain_r2_min"], S.GAIN_R2_MIN)
        self.assertEqual(S.CRITERION_DEFAULTS["cross_axis_max_ratio"],
                         S.CROSS_AXIS_MAX_RATIO)


class TestFlipBoxAgreesWithOrient(unittest.TestCase):
    """The property, not the formula.

    A box named in one orientation names different pixels in the other, and
    the viewer got this wrong twice: a box dragged on the RGB went through
    Registration in wire order while the centroid ran on the oriented frame.
    Checking flip_box's arithmetic against itself would have caught neither.
    Checking that it SELECTS THE SAME PIXELS as orient() does catches both.
    """

    @staticmethod
    def _inside(px, box, cols):
        r0, c0, r1, c1 = box
        return sorted(px[r * cols + c]
                      for r in range(r0, r1 + 1) for c in range(c0, c1 + 1))

    def test_same_pixels_in_both_orientations(self):
        from thermal_view import flip_box, orient
        px = [float(i) for i in range(ROWS * COLS)]       # every value distinct
        boxes = [(0, 0, 0, 0), (8, 12, 16, 20), (0, 0, ROWS - 1, COLS - 1),
                 (23, 31, 23, 31), (5, 0, 9, 31), (0, 7, 23, 7)]
        for fv in (False, True):
            for fh in (False, True):
                ori = orient(px, ROWS, COLS, fv, fh)
                for b in boxes:
                    fb = flip_box(b, ROWS, COLS, fv, fh)
                    with self.subTest(flipv=fv, fliph=fh, box=b):
                        self.assertEqual(self._inside(px, b, COLS),
                                         self._inside(ori, fb, COLS))

    def test_it_is_its_own_inverse(self):
        from thermal_view import flip_box
        b = (3, 5, 9, 20)
        for fv in (False, True):
            for fh in (False, True):
                once = flip_box(b, ROWS, COLS, fv, fh)
                self.assertEqual(flip_box(once, ROWS, COLS, fv, fh), b)

    def test_corners_map_to_opposite_corners(self):
        from thermal_view import flip_box
        # top-left under a 180 degree turn is bottom-right, and a box stays a
        # box: r0<=r1 and c0<=c1 after the swap, not a negative-width one.
        self.assertEqual(flip_box((0, 0, 2, 3), ROWS, COLS, True, True),
                         (ROWS - 3, COLS - 4, ROWS - 1, COLS - 1))


class TestColdPolarity(unittest.TestCase):
    """A chilled target must be found as precisely as a hot one.

    The failure without this is not a miss: the hot estimator locks onto
    whatever IS hottest — on this bench the board — and reports a confident
    centroid for the wrong object, which is indistinguishable in a dataset
    from a correct one.
    """

    @staticmethod
    def _cold(r0, c0, tbg=30.0, depth=30.0, sigma=1.5):
        two = 2.0 * sigma * sigma
        return [tbg - depth * math.exp(-(((r - r0) ** 2 + (c - c0) ** 2) / two))
                for r in range(ROWS) for c in range(COLS)]

    def test_cold_matches_hot_on_the_mirrored_frame(self):
        """The implementation is negation, so this is the property to pin.

        Anything else would be a second copy of four inequalities, and two
        copies disagree exactly on the frames that matter.
        """
        from thermal_view import centroid
        hot = blob(12.3, 15.7)
        cold = [2 * 22.0 - v for v in hot]          # reflect about the background
        h = centroid(hot, ROWS, COLS, (8, 12, 16, 20))
        c = centroid(cold, ROWS, COLS, (8, 12, 16, 20), polarity="cold")
        self.assertTrue(h["ok"] and c["ok"], (h["reason"], c["reason"]))
        self.assertAlmostEqual(h["r"], c["r"], places=9)
        self.assertAlmostEqual(h["c"], c["c"], places=9)
        self.assertAlmostEqual(h["contrast"], c["contrast"], places=9)
        self.assertEqual(h["n"], c["n"])

    def test_a_cold_target_is_invisible_to_the_hot_estimator(self):
        from thermal_view import centroid
        px = self._cold(12.0, 16.0)
        self.assertFalse(centroid(px, ROWS, COLS, (8, 12, 16, 20))["ok"])
        self.assertTrue(centroid(px, ROWS, COLS, (8, 12, 16, 20),
                                 polarity="cold")["ok"])

    def test_the_hot_estimator_locks_onto_the_wrong_object(self):
        # A cold cup in the box AND something hot elsewhere in the frame: the
        # background median stays near room temperature, so a whole-frame hot
        # search finds the hot thing and reports it happily.
        from thermal_view import centroid
        px = self._cold(12.0, 16.0)
        for r in range(2, 6):
            for c in range(2, 6):
                px[r * COLS + c] = 70.0              # the board, cooking
        hot = centroid(px, ROWS, COLS)               # whole frame
        self.assertTrue(hot["ok"])
        self.assertLess(hot["r"], 8)                 # nowhere near the cup
        cold = centroid(px, ROWS, COLS, (8, 12, 16, 20), polarity="cold")
        self.assertAlmostEqual(cold["r"], 12.0, delta=0.2)
        self.assertAlmostEqual(cold["c"], 16.0, delta=0.2)

    def test_reported_temperatures_are_real_not_negated(self):
        from thermal_view import centroid
        c = centroid(self._cold(12.0, 16.0), ROWS, COLS, (8, 12, 16, 20),
                     polarity="cold")
        self.assertAlmostEqual(c["tbg"], 30.0, delta=0.5)   # room, not -30
        self.assertLess(c["tth"], c["tbg"])                 # threshold is BELOW it
        self.assertGreater(c["contrast"], 0)                # a magnitude

    def test_an_unknown_polarity_is_refused(self):
        from thermal_view import centroid
        with self.assertRaises(ValueError):
            centroid(blob(12, 16), ROWS, COLS, polarity="tepid")


class TestFirmwareRotationMatchesTheHost(unittest.TestCase):
    """Two spellings of one operation, pinned against each other.

    thermal_uart.cpp rotates a frame by reversing the 768-element pixel array
    in place; thermal_view.orient(flipv=True, fliph=True) does it by reversing
    the rows and then each row. They must agree, because the whole
    double-rotation guard rests on "the board already did what the flags would
    have done" — if they ever diverged, the guard would be refusing a
    correction that was never actually applied.

    This cannot execute the C++, so it pins the CLAIM: whoever changes either
    side has to come here and say why the two are no longer the same thing.
    """

    @staticmethod
    def _firmware_rotate180(px):
        """What thermal_uart.cpp does: swap the two ends inward, one pass."""
        out = list(px)
        i, j = 0, len(out) - 1
        while i < j:
            out[i], out[j] = out[j], out[i]
            i, j = i + 1, j - 1
        return out

    def test_reversing_the_array_equals_flipv_plus_fliph(self):
        from thermal_view import orient
        px = [float(i) for i in range(ROWS * COLS)]      # every value distinct
        self.assertEqual(self._firmware_rotate180(px),
                         orient(px, ROWS, COLS, True, True))

    def test_on_a_real_looking_frame_too(self):
        from thermal_view import orient
        px = blob(7.5, 23.5)                             # off-centre, asymmetric
        self.assertEqual(self._firmware_rotate180(px),
                         orient(px, ROWS, COLS, True, True))

    def test_it_is_NOT_the_same_as_either_flip_alone(self):
        # The thing that would go unnoticed: a 180 rotation and a single mirror
        # both "look flipped" on a roughly symmetric scene, and only one of
        # them preserves handedness.
        from thermal_view import orient
        px = [float(i) for i in range(ROWS * COLS)]
        self.assertNotEqual(self._firmware_rotate180(px),
                            orient(px, ROWS, COLS, True, False))
        self.assertNotEqual(self._firmware_rotate180(px),
                            orient(px, ROWS, COLS, False, True))

    def test_applying_it_twice_is_the_identity(self):
        # Which is exactly why a host flip on top of a corrected frame is
        # invisible rather than obviously wrong.
        px = blob(9.25, 11.75)
        self.assertEqual(self._firmware_rotate180(self._firmware_rotate180(px)), px)


class TestOrientationConflict(unittest.TestCase):
    """Two 180-degree rotations are the identity, and that is the danger.

    Firmware now corrects the head's mounting on the device. A host that also
    flips does not produce a broken-looking frame — it produces a perfectly
    ordinary one of the wrong pixels, every number computable, nothing
    downstream able to notice. The only defence is refusing up front.
    """

    def test_board_corrected_plus_host_flips_is_refused(self):
        from thermal_view import orientation_conflict
        w = orientation_conflict("rot180", True, True)
        self.assertIsNotNone(w)
        self.assertIn("drop the host flips", w)

    def test_one_host_flip_over_a_corrected_frame_is_also_wrong(self):
        # Not a rotation but a MIRROR, which the registration model has no term
        # for at all — it would fail to converge with no visible reason.
        from thermal_view import orientation_conflict
        self.assertIsNotNone(orientation_conflict("rot180", True, False))
        self.assertIsNotNone(orientation_conflict("rot180", False, True))

    def test_a_corrected_frame_with_no_host_flips_is_fine(self):
        from thermal_view import orientation_conflict
        self.assertIsNone(orientation_conflict("rot180", False, False))

    def test_an_old_recording_keeps_its_flags(self):
        # Frames predating the firmware change carry no orientation field and
        # are wire order: the flags are still how they get corrected, so this
        # must stay silent or every old recording would start shouting.
        from thermal_view import orientation_conflict
        self.assertIsNone(orientation_conflict("wire", True, True))
        self.assertIsNone(orientation_conflict("wire", False, False))


class TestThermalViewCliHonoursColdAndOrientation(unittest.TestCase):
    """show() is an AIMING tool: the box chosen from it is the box the
    acceptance run is handed, so it must not point somewhere else."""

    def _show(self, doc, **kw):
        import contextlib
        import thermal_view
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            thermal_view.show(doc, **kw)
        return buf.getvalue()

    @staticmethod
    def _doc(px, orientation="wire"):
        return {"frame": {"seq": 1, "rows": ROWS, "cols": COLS, "ta_c": 28.0,
                          "checksum_ok": True, "orientation": orientation,
                          "px": px}}

    def test_cold_mode_points_at_the_cold_target(self):
        # A chilled cup in the box and something hot elsewhere: `hot @` sent
        # the operator to the hot thing while the centroid was correct, so the
        # two halves of the same screen disagreed.
        px = [30.0] * (ROWS * COLS)
        px[12 * COLS + 16] = 2.0                 # the cup
        px[3 * COLS + 4] = 70.0                  # the board, cooking
        out = self._show(self._doc(px), cold=True)
        self.assertIn("cold @ r12 c16", out)
        self.assertNotIn("hot @", out)

    def test_hot_mode_is_unchanged(self):
        px = [30.0] * (ROWS * COLS)
        px[3 * COLS + 4] = 70.0
        out = self._show(self._doc(px))
        self.assertIn("hot @ r3 c4", out)

    def test_it_warns_when_host_flips_would_undo_the_board(self):
        out = self._show(self._doc(blob(12, 16), orientation="rot180"),
                         flipv=True, fliph=True)
        self.assertIn("drop the host flips", out)

    def test_no_warning_for_a_wire_order_board(self):
        out = self._show(self._doc(blob(12, 16), orientation="wire"),
                         flipv=True, fliph=True)
        self.assertNotIn("drop the host flips", out)

    def test_no_warning_when_no_flips_are_asked_for(self):
        out = self._show(self._doc(blob(12, 16), orientation="rot180"))
        self.assertNotIn("drop the host flips", out)


class TestGate(unittest.TestCase):
    def test_missing_value_fails(self):
        g = S.gate("x", None, 1.0)
        self.assertFalse(g["ok"])
        self.assertEqual(g["note"], "not measured")

    def test_direction(self):
        self.assertTrue(S.gate("x", 0.5, 1.0)["ok"])
        self.assertFalse(S.gate("x", 1.5, 1.0)["ok"])
        self.assertTrue(S.gate("r2", 0.995, 0.99, worse="below")["ok"])
        self.assertFalse(S.gate("r2", 0.98, 0.99, worse="below")["ok"])


class TestLoad(unittest.TestCase):
    def test_a_recording_without_a_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.jsonl")
            with open(p, "w") as fh:
                fh.write(json.dumps(frame_row(blob(12, 16), "static", 1)) + "\n")
            with self.assertRaises(SystemExit):
                S.load(p)

    def test_unknown_row_kind_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.jsonl")
            with open(p, "w") as fh:
                fh.write(json.dumps({"kind": "manifest", "mode": "static"}) + "\n")
                fh.write(json.dumps({"kind": "note", "text": "hi"}) + "\n")
            with self.assertRaises(SystemExit):
                S.load(p)


class TestReportEndToEnd(unittest.TestCase):
    """The whole chain: JSONL -> orient -> centroid -> gates -> exit code."""

    def _repeat_run(self, path, jitter):
        """A→B→C ×10. `jitter(cycle)` returns the (dr, dc) actually achieved."""
        rows = [frame_row(blob(12.0, 16.0), "static_pre", i) for i in range(4)]
        seq = 100
        poses = {"A": (12.0, 15.0), "B": (12.0, 17.0), "C": (13.0, 16.0)}
        for cyc in range(10):
            for name, (r0, c0) in poses.items():
                dr, dc = jitter(cyc)
                for j in range(2):
                    seq += 1
                    rows.append(frame_row(blob(r0 + dr, c0 + dc), f"pose:{name}", seq,
                                          cycle=cyc, pose=name,
                                          pan_us=1500, tilt_us=1500))
        rows += [frame_row(blob(12.0, 16.0), "static_post", seq + 10 + i)
                 for i in range(4)]
        return write_run(path, "repeat", rows, cycles=10, settle_ms=400,
                         approach="uni", n=2)

    def test_a_perfectly_repeatable_head_passes_with_zero_spread(self):
        # Identical frames must give identical centroids: the spread is exactly
        # zero by construction, so this pins the plumbing (grouping, per-cycle
        # averaging, orientation) without depending on the estimator's accuracy.
        with tempfile.TemporaryDirectory() as d:
            p = self._repeat_run(os.path.join(d, "r.jsonl"), lambda c: (0.0, 0.0))
            code, out = quietly(R.report, *S.load(p))
        self.assertEqual(code, 0, out)
        self.assertIn("[PASS] 3.1 A P90 radial", out)
        self.assertIn("[PASS] 3.2 bracket shift", out)
        self.assertNotIn("[FAIL]", out)

    def test_a_head_that_misses_by_two_pixels_fails(self):
        # Alternating +-1 px in each axis: p2p = 2.0 px on both, radial ~1.41.
        # Both exceed the criterion, and the report must say so rather than
        # describing a wide distribution in neutral terms.
        with tempfile.TemporaryDirectory() as d:
            p = self._repeat_run(os.path.join(d, "r.jsonl"),
                                 lambda c: (1.0, 1.0) if c % 2 else (-1.0, -1.0))
            code, out = quietly(R.report, *S.load(p))
        self.assertEqual(code, 1, out)
        self.assertIn("[FAIL] 3.1 A P90 radial", out)
        self.assertIn("a digital servo", out)

    def test_a_knocked_target_voids_the_run(self):
        # The bracketing static blocks disagree by 2 px, so the repeatability
        # measured between them is about the target, not the mechanism.
        with tempfile.TemporaryDirectory() as d:
            rows = [frame_row(blob(12.0, 16.0), "static_pre", i) for i in range(4)]
            for cyc in range(10):
                for j in range(2):
                    rows.append(frame_row(blob(12.0, 15.0), "pose:A", 100 + cyc * 2 + j,
                                          cycle=cyc, pose="A"))
            rows += [frame_row(blob(14.0, 16.0), "static_post", 900 + i)
                     for i in range(4)]
            p = write_run(os.path.join(d, "r.jsonl"), "repeat", rows, cycles=10,
                          settle_ms=400, approach="uni", n=2)
            code, out = quietly(R.report, *S.load(p))
        self.assertEqual(code, 1, out)
        self.assertIn("[FAIL] 3.2 bracket shift", out)

    def test_static_mode_reports_a_zero_noise_floor_and_passes(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [frame_row(blob(12.0, 16.0), "static", i) for i in range(20)]
            p = write_run(os.path.join(d, "s.jsonl"), "static", rows, n=20)
            code, out = quietly(R.report, *S.load(p))
        self.assertEqual(code, 0, out)
        self.assertIn("[PASS] 2.1 sigma_r", out)
        self.assertNotIn("STOP.", out)

    def test_static_mode_stops_the_operator_when_it_cannot_resolve(self):
        # 0.4 px of wander is above the hard stop: the answer lands on 1 px and
        # this instrument cannot see it. The report must say STOP, not just FAIL.
        with tempfile.TemporaryDirectory() as d:
            rows = [frame_row(blob(12.0 + (0.4 if i % 2 else -0.4), 16.0), "static", i)
                    for i in range(20)]
            p = write_run(os.path.join(d, "s.jsonl"), "static", rows, n=20)
            code, out = quietly(R.report, *S.load(p))
        self.assertEqual(code, 1, out)
        self.assertIn("STOP.", out)

    def test_capture_failures_are_dropped_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [frame_row(blob(12.0, 16.0), "static", i) for i in range(10)]
            rows.append({"kind": "frame", "block": "static", "ok": False,
                         "reason": "http:timeout"})
            p = write_run(os.path.join(d, "s.jsonl"), "static", rows, n=11)
            code, out = quietly(R.report, *S.load(p))
        self.assertEqual(code, 0, out)
        self.assertIn("rejected 1", out)
        self.assertIn("http:timeout", out)

    def test_gain_mode_recovers_the_planted_slope(self):
        with tempfile.TemporaryDirectory() as d:
            rows = []
            for i, us in enumerate(range(1300, 1701, 50)):
                for j in range(3):
                    # 0.01 px/us on image column, nothing on row. Chosen so the
                    # target stays inside BOX across the whole sweep — a sweep
                    # that leaves the box is its own test, below.
                    rows.append(frame_row(blob(12.0, 16.0 + 0.01 * (us - 1500)),
                                          f"us={us}", i * 3 + j, pan_us=us))
            p = write_run(os.path.join(d, "g.jsonl"), "gain", rows, axis="pan", n=3)
            code, out = quietly(R.report, *S.load(p))
        self.assertIn("main axis: image c", out)
        self.assertIn("[PASS] 2.3 fit R2", out)
        self.assertIn("[PASS] 1.4 cross/main ratio", out)
        self.assertEqual(code, 0, out)

    def test_gain_mode_refuses_a_sweep_that_left_the_box(self):
        # The failure this guards against is not a crash: the widths that kept
        # their target still fit a convincing line, so a truncated sweep
        # produces a clean-looking px/us that is simply wrong.
        with tempfile.TemporaryDirectory() as d:
            rows = []
            for i, us in enumerate(range(1300, 1701, 50)):
                for j in range(3):
                    rows.append(frame_row(blob(12.0, 16.0 + 0.04 * (us - 1500)),
                                          f"us={us}", i * 3 + j, pan_us=us))
            p = write_run(os.path.join(d, "g.jsonl"), "gain", rows, axis="pan", n=3)
            code, out = quietly(R.report, *S.load(p))
        self.assertEqual(code, 1, out)
        self.assertIn("DROPPED", out)
        self.assertIn("[FAIL] 2.3 widths kept", out)


class TestPoseParsing(unittest.TestCase):
    def test_widths_outside_the_electrical_span_are_refused(self):
        with self.assertRaises(SystemExit):
            R.parse_poses(["A:600,2500"])

    def test_duplicate_names_are_refused(self):
        # The names label the results; two poses called A silently merge into
        # one distribution spanning both positions.
        with self.assertRaises(SystemExit):
            R.parse_poses(["A:1400,1500", "A:1600,1500"])

    def test_a_good_pose_table_parses(self):
        self.assertEqual(R.parse_poses(["A:1400,1500"]),
                         [{"name": "A", "pan_us": 1400, "tilt_us": 1500}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
