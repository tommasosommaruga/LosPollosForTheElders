"""Offline regression checks; no models, camera devices, or arm connections needed."""
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import cv2
from PIL import Image

import target_selector_v5 as ts
import grasp_planner_v3 as gp
import pick_v5 as pick
import two_view
import eval_v2
import multiview_geometry as geometry


class VisionTests(unittest.TestCase):
    def test_calibrated_triangulation(self):
        k = np.array([[800., 0, 320], [0, 800., 240], [0, 0, 1]])
        stand, hand = np.eye(4), np.eye(4)
        hand[0, 3] = .1
        result = geometry.triangulate([[320, 240]], [[240, 240]], k, k, stand, hand)
        self.assertTrue(result["valid"][0])
        np.testing.assert_allclose(result["points_base_m"], [[0, 0, 1]], atol=1e-8)
        self.assertFalse(result["motion_allowed"])

    def test_bad_correspondence_rejected(self):
        k = np.array([[800., 0, 320], [0, 800., 240], [0, 0, 1]])
        hand = np.eye(4)
        hand[0, 3] = .1
        result = geometry.triangulate([[320, 240]], [[240, 280]], k, k, np.eye(4), hand)
        self.assertFalse(result["valid"][0])

    def test_zero_baseline_rejected(self):
        k = np.array([[800., 0, 320], [0, 800., 240], [0, 0, 1]])
        result = geometry.triangulate([[320, 240]], [[320, 240]], k, k, np.eye(4), np.eye(4))
        self.assertFalse(result["valid"][0])

    def test_hand_camera_transform_composition(self):
        a, b = np.eye(4), np.eye(4)
        a[0, 3], b[2, 3] = .2, .05
        np.testing.assert_allclose(geometry.hand_camera_pose(a, b)[:3, 3], [.2, 0, .05])
        with self.assertRaises(ValueError):
            geometry.rigid_transform(np.zeros((4, 4)))

    def test_rotated_distorted_camera_pair(self):
        k = np.array([[800., 0, 320], [0, 780., 240], [0, 0, 1]])
        distortion = np.array([.05, -.01, .001, .002, 0.])
        truth = np.array([[.2, .1, 1.5], [.25, -.05, 1.2]])
        poses, pixels = [], []
        for angle, translation in ((.1, [.1, 0, 0]), (-.1, [.3, .05, .02])):
            pose = np.eye(4)
            pose[:3, :3] = cv2.Rodrigues(np.array([0., angle, 0.]))[0]
            pose[:3, 3] = translation
            ext = np.linalg.inv(pose)
            points = cv2.projectPoints(truth, cv2.Rodrigues(ext[:3, :3])[0], ext[:3, 3], k, distortion)[0]
            poses.append(pose)
            pixels.append(points.reshape(-1, 2))
        result = geometry.triangulate(*pixels, k, k, *poses, distortion, distortion)
        self.assertTrue(result["valid"].all())
        np.testing.assert_allclose(result["points_base_m"], truth, atol=1e-6)

    def test_opposed_collinear_rays_rejected(self):
        k = np.array([[800., 0, 320], [0, 800., 240], [0, 0, 1]])
        hand = np.eye(4)
        hand[:3, :3] = np.diag([-1., 1., -1.])
        hand[2, 3] = 2.
        result = geometry.triangulate([[320, 240]], [[320, 240]], k, k, np.eye(4), hand)
        self.assertFalse(result["valid"][0])
    def test_overlapping_boxes_do_not_merge_distinct_masks(self):
        a = np.zeros((20, 20), bool)
        a[:, :10] = True
        candidates = [{"mask": mask, "box": [0, 0, 20, 20], "sam_score": .9,
                       "prompt": "cup", "is_target_prompt": True} for mask in (a, ~a)]
        self.assertEqual(len(ts.dedupe(candidates)), 2)

    def test_duplicate_mask_preserves_target_score(self):
        mask = np.ones((20, 20), bool)
        candidates = [{"mask": mask, "box": [0, 0, 20, 20], "sam_score": score,
                       "prompt": prompt, "is_target_prompt": specific}
                      for score, prompt, specific in ((.95, "cup", False), (.85, "white cup", True))]
        merged = ts.dedupe(candidates)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["target_score"], .85)

    def test_evaluator_requires_mask_membership(self):
        chosen = {"mask": np.zeros((20, 20), bool), "box": [0, 0, 20, 20]}
        self.assertEqual(eval_v2.grade({"expected": "GO", "x": "10", "y": "10"}, {"status": "GO", "chosen": chosen})[0], "WRONG_GO")
        self.assertEqual(eval_v2.grade({"expected": "GO"}, {"status": "GO", "chosen": chosen})[0], "UNVERIFIED")
    def test_invalid_vlm_values_are_rejected(self):
        for value in (None, [], {"match": "false", "confidence": .99},
                      {"match": True, "confidence": "0.99"},
                      {"match": True, "confidence": True},
                      {"match": True, "confidence": float("nan")},
                      {"match": True, "confidence": float("inf")},
                      {"match": True, "confidence": 1.1}):
            with self.subTest(value=value):
                self.assertTrue(ts.validate_match(value)["error"])

    def test_valid_vlm_response_preserved(self):
        self.assertEqual(ts.validate_match({"match": True, "confidence": .9})["confidence"], .9)
        self.assertFalse(ts.validate_match({"match": False, "confidence": .99})["match"])

    def test_parse_failure_remains_error(self):
        self.assertTrue(ts.validate_match(ts.parse_json("not JSON"))["error"])

    def test_invalid_candidate_blocks_other_positive(self):
        candidates = [{"box": [0, 0, 10, 10]}, {"box": [10, 10, 20, 20]}]
        with patch.object(ts, "VLM_BACKEND", "ollama"), patch.object(ts, "sam3_candidates", return_value=candidates), patch.object(ts, "vlm_check", side_effect=[{"match": True, "confidence": .99}, {"match": "false", "confidence": .99}]):
            result = ts.decide(Image.new("RGB", (30, 30)), "cup", None)
        self.assertEqual(result["status"], "CHECK_FAILED")
        self.assertIsNone(result["chosen"])

    def test_empty_consistency(self):
        self.assertFalse(ts.consistent([]))

    def test_sam_decisions_preserved(self):
        for count, expected in [(0, "NOT_FOUND"), (1, "GO"), (2, "AMBIGUOUS")]:
            with patch.object(ts, "VLM_BACKEND", "none"), patch.object(ts, "sam3_candidates", return_value=[{"target_score": .9} for _ in range(count)]):
                self.assertEqual(ts.decide(Image.new("RGB", (20, 20)), "cup", None)["status"], expected)

    def test_metric_width_is_checked(self):
        mask = np.zeros((100, 100), bool)
        mask[40:60, 20:80] = True
        class Calib:
            def to_table(self, u, v):
                return .01 * u, v
        result = pick.top_grasp(mask, Calib(), 1, .01, 6)
        self.assertIsNotNone(result)
        self.assertTrue(not result["fits"] or result["width_cm"] <= 5.4)

    def test_disconnected_side_mask_does_not_crash(self):
        mask = np.zeros((120, 80), bool)
        mask[10:110, 30:50] = True
        mask[59:62] = False
        result = pick.best_side_row(mask, .1, 6)
        self.assertIsNotNone(result)
        self.assertTrue(result["fits"])

    def test_oversized_side_grasp_rejected(self):
        mask = np.zeros((180, 100), bool)
        mask[10:170, 10:90] = True
        self.assertFalse(pick.best_side_row(mask, .1, 6)["fits"])

    def test_plan_filter_rejects_all(self):
        mask = np.zeros((50, 50), bool)
        mask[10:40, 10:40] = True
        self.assertEqual(gp.plan_grasp(mask, candidate_filter=lambda a, b: False), (None, []))

    def test_calibration_blocks(self):
        class Calib:
            pairs = [None] * 5
            def expected_error(self): return 2.5, "test"
            def inside_marks(self, u, v): return False
        self.assertEqual(len(pick.calibration_blocks(Calib(), (10, 10), 1)), 3)

    def test_good_calibration_allowed(self):
        class Calib:
            pairs = [None] * 8
            def expected_error(self): return .2, "test"
            def inside_marks(self, u, v): return True
        self.assertEqual(pick.calibration_blocks(Calib(), (10, 10), 1), [])

    def test_two_view_no_forced_agreement(self):
        self.assertEqual(two_view.pair_status({"status": "GO"}, {"status": "AMBIGUOUS"}), "NEEDS_REVIEW")
        self.assertEqual(two_view.pair_status({"status": "GO"}, {"status": "GO"}), "TARGET_IN_BOTH_VIEWS")

    def test_multiple_joint_positives_are_ambiguous(self):
        reviews = [{"assessment": {"match": True, "confidence": 1.0}} for _ in range(3)]
        summary = two_view.summarize_joint_reviews(reviews)
        self.assertEqual(summary["status"], "AMBIGUOUS_CORROBORATION")
        self.assertFalse(summary["physical_instance_verified"])

    def test_textureless_images_do_not_invent_depth(self):
        img = Image.new("RGB", (100, 100), "white")
        mask = np.ones((100, 100), bool)
        result = two_view.feature_matches(img, img, mask, mask)
        self.assertEqual(result["status"], "INSUFFICIENT_FEATURES")
        self.assertFalse(result["metric_3d_available"])

    def test_joint_review_never_promotes_disagreement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            img = root / "input.png"
            Image.new("RGB", (30, 30), "white").save(img)
            mask = np.zeros((30, 30), bool)
            mask[5:25, 5:25] = True
            candidate = {"mask": mask, "box": [5, 5, 25, 25], "is_match": True,
                         "prompts": {"cup"}, "target_score": .9, "sam_score": .9,
                         "vlm": {"match": True, "confidence": .99}}
            results = [{"status": "NOT_FOUND", "candidates": [], "chosen": None},
                       {"status": "GO", "candidates": [candidate], "chosen": candidate}]
            with patch.object(ts, "decide", side_effect=results), patch.object(two_view, "joint_review", return_value={"match": True}), patch.object(ts, "VLM_BACKEND", "ollama"):
                report = two_view.analyze(img, img, "cup", None, root / "result", vlm="ollama")
            self.assertEqual(report["status"], "NEEDS_REVIEW")
            self.assertFalse(report["motion_allowed"])
            self.assertFalse(report["metric_3d_available"])
            self.assertTrue((root / "result" / "comparison.jpg").exists())
            self.assertEqual(json.loads((root / "result" / "report.json").read_text())["status"], "NEEDS_REVIEW")


if __name__ == "__main__":
    unittest.main()
