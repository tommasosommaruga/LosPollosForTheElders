"""Synthetic regression checks; no calibration here is suitable for a robot."""
import copy
import importlib.util
from pathlib import Path
import unittest

import numpy as np

import calibration_validation as validation
from selection_policy import select_candidates


def workspace():
    return {"schema_version": 1, "role": "stand", "camera_id": "stand", "image_size": [160, 120],
            "polygon_px": [[5, 50], [154, 50], [154, 115], [5, 115]]}


def validation_record(camera, table):
    record = validation.template(camera, table, (160, 120), "stand")
    record["camera_checks"] = [{"pixel": [u, v], "measured_xy_cm": [u/10, v/10]} for u, v in
                               ((10, 10), (150, 10), (150, 110), (10, 110), (80, 10), (80, 110))]
    record["table_checks_base_m"] = [[.01, .01, 0], [.15, .01, 0], [.15, .11, 0], [.01, .11, 0]]
    return record


def candidate(x1=60, y1=30, x2=100, y2=100, score=.95):
    mask = np.zeros((120, 160), bool)
    mask[y1:y2, x1:x2] = True
    return {"mask": mask, "box": [x1, y1, x2, y2], "sam_score": score, "target_score": score,
            "is_match": score >= .5, "vlm": {"reason": "SAM only: exact target"}}


def choose(candidates, **kwargs):
    raw = {"status": "GO", "candidates": candidates, "chosen": None}
    return select_candidates(raw, (160, 120), **kwargs)


class SelectionTests(unittest.TestCase):
    def test_background_excluded_without_clipping_raised_silhouette(self):
        target = candidate()
        out = choose([target, candidate(20, 5, 35, 20)], workspace=workspace(), camera_id="stand")
        self.assertEqual(out["status"], "GO")
        np.testing.assert_array_equal(out["chosen"]["mask"], target["mask"])
        self.assertIn("outside_workspace", out["candidates"][1]["decision"]["reasons"])

    def test_highest_score_does_not_break_tie_but_explicit_point_can(self):
        objects = [candidate(20, 60, 40, 100), candidate(70, 60, 90, 100, .6)]
        self.assertEqual(choose(objects)["status"], "AMBIGUOUS")
        out = choose(objects, pick_at=(80, 80))
        self.assertEqual(out["status"], "GO")
        self.assertEqual(out["chosen"]["decision"]["candidate_id"], 1)

    def test_point_cannot_override_overlapping_uncertainty(self):
        self.assertEqual(choose([candidate(), candidate(score=.45)], pick_at=(80, 80))["status"], "NEEDS_REVIEW")
        self.assertEqual(choose([candidate(), candidate()], pick_at=(80, 80))["status"], "NEEDS_REVIEW")

    def test_near_threshold_competitor_requires_review(self):
        self.assertEqual(choose([candidate(), candidate(10, 60, 40, 100, .45)])["status"], "NEEDS_REVIEW")

    def test_mask_holes_preserved_and_small_speck_removed(self):
        c = candidate()
        c["mask"][40:50, 70:80] = False
        c["mask"][2, 2] = True
        out = choose([c])
        self.assertEqual(out["status"], "GO")
        self.assertFalse(out["chosen"]["mask"][45, 75])
        self.assertFalse(out["chosen"]["mask"][2, 2])
        self.assertEqual(out["chosen"]["decision"]["mask_quality"]["removed_speckles_px"], 1)

    def test_fragmented_truncated_tiny_or_boundary_mask_abstains(self):
        fragmented = candidate()
        fragmented["mask"][5:25, 5:25] = True
        cases = [(fragmented, None), (candidate(0, 30, 40, 100), None),
                 (candidate(60, 60, 63, 63), None), (candidate(1, 40, 10, 100), workspace())]
        for c, roi in cases:
            with self.subTest(c=c["box"]):
                out = choose([c], workspace=roi)
                self.assertEqual(out["status"], "NEEDS_REVIEW")
                self.assertIsNone(out["chosen"])

    def test_camera_or_resolution_mismatch_rejected(self):
        for config, camera_id in ((workspace(), "another"), ({**workspace(), "image_size": [320, 240]}, "stand")):
            with self.assertRaises(ValueError):
                choose([candidate()], workspace=config, camera_id=camera_id)

    def test_upstream_failure_or_invalid_score_never_promoted(self):
        raw = {"status": "CHECK_FAILED", "candidates": [candidate()], "chosen": None}
        self.assertEqual(select_candidates(raw, (160, 120), pick_at=(80, 80))["status"], "CHECK_FAILED")
        c = candidate()
        c["sam_score"] = float("nan")
        with self.assertRaises(ValueError):
            choose([c])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.camera = {"size": 160, "pairs": [[u, v, u/10, v/10] for u, v in
             ((0, 0), (159, 0), (0, 119), (159, 119), (80, 0), (80, 119), (0, 60), (159, 60))]}
        self.table = {"a": 0., "b": 0., "c": 0., "touches_m": [[0, 0, 0]]}
        self.record = validation_record(self.camera, self.table)

    def check_record(self, record=None, **kw):
        return validation.validate(record or self.record, self.camera, self.table, (160, 120), "stand", **kw)

    def test_independent_checks_pass_without_claiming_physical_certification(self):
        out = self.check_record(target_px=(80, 90))
        self.assertTrue(out["passed"], out)
        self.assertFalse(out["physical_accuracy_certified"])
        self.assertLess(out["camera_max_error_cm"], 1e-5)

    def test_missing_and_empty_records_block(self):
        self.assertFalse(validation.validate(None, self.camera, self.table, (160, 120))["passed"])
        self.assertFalse(self.check_record(validation.template(self.camera, self.table, (160, 120), "stand"))["passed"])

    def test_changed_geometry_camera_or_resolution_blocks(self):
        for field, value in (("geometry_sha256", "wrong"), ("camera_id", "other"), ("image_size", [320, 240])):
            record = {**self.record, field: value}
            self.assertFalse(self.check_record(record)["passed"])
        self.table["c"] = .001
        self.assertFalse(self.check_record()["passed"])

    def test_reusing_fitted_points_blocks(self):
        record = copy.deepcopy(self.record)
        record["camera_checks"][0] = {"pixel": [0, 0], "measured_xy_cm": [0, 0]}
        self.assertIn("reuses", " ".join(self.check_record(record)["errors"]))
        record = copy.deepcopy(self.record)
        record["table_checks_base_m"][0] = [0, 0, 0]
        self.assertIn("reuses", " ".join(self.check_record(record)["errors"]))

    def test_error_or_outside_validated_area_blocks(self):
        record = copy.deepcopy(self.record)
        record["camera_checks"][0]["measured_xy_cm"][0] += 2
        self.assertFalse(self.check_record(record)["passed"])
        record = copy.deepcopy(self.record)
        record["table_checks_base_m"][0][2] = .02
        self.assertFalse(self.check_record(record)["passed"])
        self.assertFalse(self.check_record(target_px=(155, 90))["passed"])

    def test_collinear_and_nonfinite_measurements_block(self):
        for points in ([[x/100, 0, 0] for x in (1, 2, 3, 4)], [[.01, .01, float("nan")]] * 4):
            self.assertFalse(self.check_record({**self.record, "table_checks_base_m": points})["passed"])


spec = importlib.util.spec_from_file_location("timing_test", Path(__file__).parent / "controller/arm/trajectory_timing.py")
timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timing)


class TrajectoryTests(unittest.TestCase):
    def test_uneven_path_peak_speed_stays_bounded(self):
        way = [{1: v, 2: v/2} for v in (0, 1, 101, 102)]
        seconds = timing.bounded_duration(way, .2, 20)
        self.assertGreaterEqual(seconds, 3*100*1.875/20)
        times = np.linspace(0, seconds, 20001)
        progress = np.array([timing.ease(t/seconds) for t in times]) * 3
        positions = np.interp(progress, np.arange(4), [p[1] for p in way])
        self.assertLessEqual(float(np.max(np.abs(np.diff(positions)/np.diff(times)))), 20.0001)

    def test_easing_starts_and_stops_with_zero_speed_and_acceleration(self):
        dt = 1e-5
        for t in (0., 1.):
            self.assertLess(abs((timing.ease(t+dt)-timing.ease(t-dt))/(2*dt)), 1e-7)
            self.assertLess(abs((timing.ease(t+dt)-2*timing.ease(t)+timing.ease(t-dt))/dt**2), .002)

    def test_invalid_paths_rejected_before_writes(self):
        for way, seconds, speed in (([], 1, 20), ([{1: 0}, {2: 1}], 1, 20),
                                    ([{1: 0}, {1: float("nan")}], 1, 20),
                                    ([{1: 0}, {1: 1}], 1, 0), ([{1: 0}, {1: 1}], -1, 20)):
            with self.assertRaises(ValueError):
                timing.bounded_duration(way, seconds, speed)


if __name__ == "__main__":
    unittest.main()
