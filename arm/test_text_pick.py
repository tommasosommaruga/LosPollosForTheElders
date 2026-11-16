"""Hardware-free text workflow tests. No model, network, camera or servo use."""
import argparse
import base64
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

import text_pick as workflow


def fixture():
    image = Image.new("RGB", (160, 120), "gray")
    mask = np.zeros((120, 160), bool)
    mask[30:100, 60:100] = True
    candidate = {"mask": mask, "box": [60, 30, 100, 100], "sam_score": .95,
                 "target_score": .95, "prompts": {"test bottle"}, "is_match": True,
                 "vlm": {"match": True, "confidence": .95}}
    result = {"status": "GO", "chosen": candidate, "candidates": [candidate]}
    calibration = {"size": 160, "pairs": [[u, v, u/10, v/10] for u, v in
        ((0, 0), (159, 0), (0, 119), (159, 119), (80, 0), (80, 119), (0, 60), (159, 60))]}
    profile = {"width_cm": 4., "height_cm": 12., "grasp": "side", "measurements_verified": True}
    return image, result, calibration, profile


def live_fixture():
    image, result, calibration, profile = fixture()
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    frames = {role: {"camera_id": role, "role": role, "sequence": i + 1,
                     "image_size": list(image.size), "receive_age_s": .01,
                     "jpeg_base64": base64.b64encode(buf.getvalue()).decode()}
              for i, role in enumerate(("stand", "hand"))}
    status = {**calibration, "camera_role": "stand", "calibrated": True, "cam": 0,
              "table_calibration": {"a": 0., "b": 0., "c": 0., "touches_m": [[0, 0, 0]]}}
    packet = {"frames": frames}
    bridge = Mock()
    bridge.get.side_effect = lambda path: copy.deepcopy(status if path == "/status" else packet)
    bridge.submit_grasp.return_value = {"ok": True, "holding": True, "method": "side"}
    return bridge, image, result, calibration, profile, status, packet


class PlanningTests(unittest.TestCase):
    def test_mask_area_and_pixel_grasp_without_calibration(self):
        image, result, _, profile = fixture()
        report = workflow.plan_selection(image, result, profile)
        self.assertTrue(report["object_present"])
        self.assertEqual(report["region"]["area_px"], 2800)
        self.assertEqual(report["region"]["bbox_xyxy_px"], [60, 30, 100, 100])
        self.assertIn("point_px", report["grasp"])
        self.assertNotIn("width_cm", report["grasp"])
        self.assertIsNone(report["controller_request"])
        self.assertTrue(report["execution_blocks"])

    def test_absent_ambiguous_or_failed_never_plan(self):
        image, result, _, profile = fixture()
        for status in ("NOT_FOUND", "AMBIGUOUS", "CHECK_FAILED"):
            report = workflow.plan_selection(image, {**result, "status": status, "chosen": None}, profile)
            self.assertIsNone(report["controller_request"])
            self.assertNotIn("grasp", report)
            self.assertEqual(report["status"], status)

    def test_side_plan_preserves_cm_and_approach(self):
        image, result, calibration, profile = fixture()
        report = workflow.plan_selection(image, result, profile, calibration=calibration)
        self.assertEqual(report["execution_blocks"], [])
        self.assertEqual(report["controller_request"]["method"], "side")
        self.assertAlmostEqual(report["controller_request"]["jaws"], 4.)
        self.assertEqual(report["approach"]["approach_height_cm"], 17.)
        self.assertFalse(report["approach"]["collision_checked"])

    def test_missing_physical_dimensions_block_motion(self):
        image, result, calibration, _ = fixture()
        report = workflow.plan_selection(image, result, {}, calibration=calibration, method="side")
        self.assertTrue(any("width" in block for block in report["execution_blocks"]))
        self.assertTrue(any("height" in block for block in report["execution_blocks"]))

    def test_unknown_posture_never_chooses_grasp_from_aspect_ratio(self):
        image, result, _, _ = fixture()
        report = workflow.plan_selection(image, result, {})
        self.assertEqual(report["status"], "NEEDS_GRASP_METHOD")
        self.assertEqual(report["posture"]["value"], "unknown")
        self.assertNotIn("grasp", report)

    def test_example_dimensions_require_physical_verification(self):
        image, result, calibration, profile = fixture()
        del profile["measurements_verified"]
        report = workflow.plan_selection(image, result, profile, calibration=calibration)
        self.assertTrue(any("not been verified" in block for block in report["execution_blocks"]))

    def test_override_cannot_reuse_verification_of_another_grasp_method(self):
        image, result, calibration, profile = fixture()
        profile["grasp"] = "top"
        report = workflow.plan_selection(image, result, profile, calibration=calibration, method="side")
        self.assertIn("Requested grasp method differs from the verified item profile", report["execution_blocks"])

    def test_invalid_dimensions_never_produce_a_motion_request(self):
        image, result, calibration, profile = fixture()
        for key, value in (("width_cm", -4), ("height_cm", float("nan")), ("grasp_height_frac", 2)):
            with self.assertRaises(ValueError):
                workflow.plan_selection(image, result, {**profile, key: value}, calibration=calibration)

    def test_top_angle_is_not_silently_discarded(self):
        image, result, calibration, profile = fixture()
        report = workflow.plan_selection(image, result, profile, calibration=calibration, method="top")
        self.assertIn("closing_angle_table_deg", report["controller_request"])
        self.assertTrue(any("ORIENTATION_UNSUPPORTED" in block for block in report["execution_blocks"]))

    def test_oversize_and_weak_calibration_are_blocked(self):
        image, result, calibration, profile = fixture()
        calibration["pairs"] = [[u, v, 2*x, 2*y] for u, v, x, y in calibration["pairs"][:5]]
        report = workflow.plan_selection(image, result, profile, calibration=calibration)
        self.assertEqual(report["status"], "NO_GRASP")
        self.assertTrue(any("six calibration" in block for block in report["execution_blocks"]))

    def test_empty_mask_is_rejected(self):
        with self.assertRaises(ValueError):
            workflow.object_region(np.zeros((20, 20), bool))


class LiveTests(unittest.TestCase):
    def test_both_roles_read_from_controller_without_post(self):
        bridge, *_ = live_fixture()
        images, _, metadata = workflow.read_live(bridge)
        self.assertEqual(set(images), {"stand", "hand"})
        self.assertEqual(metadata["hand"]["camera_id"], "hand")
        self.assertNotIn("jpeg_base64", metadata["stand"])
        bridge.submit_grasp.assert_not_called()

    def test_missing_or_stale_hand_feed_is_rejected(self):
        for age in (None, 5., float("nan"), -1., True):
            bridge, *_, packet = live_fixture()
            packet["frames"]["hand"]["receive_age_s"] = age
            with self.assertRaises(ValueError):
                workflow.read_live(bridge)
        bridge, *_, packet = live_fixture()
        del packet["frames"]["hand"]
        with self.assertRaises(ValueError):
            workflow.read_live(bridge)

    def test_hand_homography_is_rejected(self):
        bridge, _, _, _, _, status, _ = live_fixture()
        status["camera_role"] = "hand"
        with self.assertRaises(ValueError):
            workflow.read_live(bridge)

    def test_fresh_scene_submits_exactly_once(self):
        bridge, image, result, calibration, profile, status, packet = live_fixture()
        images, _, metadata = workflow.read_live(bridge)
        report = workflow.plan_selection(image, result, profile, calibration=calibration)
        for frame in packet["frames"].values():
            frame["sequence"] += 2
        workflow.execute_live(report, images, {r: result for r in images}, status, metadata, bridge)
        bridge.submit_grasp.assert_called_once_with(report["controller_request"])
        self.assertEqual(report["status"], "HOLDING_REPORTED")

    def test_calibration_change_or_frozen_feed_blocks_post(self):
        for change in ("calibration", "table", "frozen"):
            bridge, image, result, calibration, profile, status, packet = live_fixture()
            images, initial, metadata = workflow.read_live(bridge)
            report = workflow.plan_selection(image, result, profile, calibration=calibration)
            if change == "calibration":
                status["pairs"][0][2] += 1
            elif change == "table":
                status["table_calibration"]["c"] += .01
                for frame in packet["frames"].values():
                    frame["sequence"] += 2
            with self.assertRaises(ValueError):
                workflow.execute_live(report, images, {r: result for r in images}, initial, metadata, bridge)
            bridge.submit_grasp.assert_not_called()

    def test_scene_change_blocks_post(self):
        bridge, image, result, calibration, profile, status, packet = live_fixture()
        images, _, metadata = workflow.read_live(bridge)
        report = workflow.plan_selection(image, result, profile, calibration=calibration)
        buf = io.BytesIO()
        Image.new("RGB", image.size, "white").save(buf, format="JPEG")
        for frame in packet["frames"].values():
            frame["sequence"] += 2
        packet["frames"]["hand"]["jpeg_base64"] = base64.b64encode(buf.getvalue()).decode()
        with self.assertRaises(ValueError):
            workflow.execute_live(report, images, {r: result for r in images}, status, metadata, bridge)
        bridge.submit_grasp.assert_not_called()


class WorkflowTests(unittest.TestCase):
    def test_default_cli_uses_sam_and_never_calls_checker_or_controller(self):
        image, result, _, profile = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image.save(root / "frame.jpg")
            (root / "items.json").write_text(json.dumps({"test bottle": profile}))
            with patch.object(workflow.ts, "sam3_candidates", return_value=result["candidates"]), \
                 patch.object(workflow.ts, "vlm_check", side_effect=AssertionError("No checker allowed")), \
                 patch.object(workflow.ts, "ollama_check", side_effect=AssertionError("No Ollama allowed")), \
                 patch.object(workflow, "ArmBridge", side_effect=AssertionError("No arm allowed")), \
                 patch("requests.sessions.Session.request", side_effect=AssertionError("No network allowed")), \
                 redirect_stdout(io.StringIO()):
                code = workflow.main(["--image", str(root / "frame.jpg"),
                                      "--hand-image", str(root / "frame.jpg"),
                                      "--text", "pick up the test bottle", "--items", str(root / "items.json"),
                                      "--out", str(root / "output")])
            self.assertEqual(code, 0)
            report = json.loads(next((root / "output").glob("*/result.json")).read_text())
            for view in (report, report["hand_view"]):
                self.assertTrue(view["object_present"])
                self.assertIn("SAM only", view["selection"]["candidates"][0]["vlm"]["reason"])
                self.assertEqual(view["posture"]["value"], "unknown")
                self.assertIn("aspect ratio is not used", view["posture"]["basis"])
            self.assertFalse(report["motion_attempted"])

    def args(self, root, **kw):
        return argparse.Namespace(text="pick up the test bottle", image=str(root / "frame.jpg"),
                                  arm_url=None, hand_image=str(root / "frame.jpg"),
                                  calib=None, category=None, vlm="none", items=str(root / "items.json"),
                                  grasp="auto", height=None, above=None, go=False, show=False,
                                  out=str(root / "output"), **kw)

    def test_saved_pair_exports_masks_previews_and_report_without_controller(self):
        image, result, _, profile = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image.save(root / "frame.jpg")
            (root / "items.json").write_text(json.dumps({"test bottle": profile}))
            with patch.object(workflow.ts, "decide", return_value=result), patch.object(workflow, "ArmBridge") as bridge:
                report = workflow.run(self.args(root))
            bridge.assert_not_called()
            out = Path(report["output_dir"])
            self.assertEqual(report["target"], "test bottle")
            self.assertEqual(report["hand_view"]["status"], "PREVIEW")
            self.assertEqual(json.loads((out / "result.json").read_text())["region"]["area_px"], 2800)
            for role in ("stand", "hand"):
                self.assertEqual(int((np.asarray(Image.open(out / f"{role}_mask.png")) > 0).sum()), 2800)
                self.assertTrue((out / f"{role}_preview.jpg").exists())

    def test_cancellation_does_not_read_images_or_load_models(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(Path(directory))
            args.text = "cancel"
            with patch.object(workflow.ts, "decide") as selector:
                report = workflow.run(args)
            selector.assert_not_called()
            self.assertEqual(report["status"], "CANCELLED")

    def test_invalid_score_produces_serializable_failure_without_motion(self):
        image, result, _, profile = fixture()
        result["chosen"]["target_score"] = float("nan")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image.save(root / "frame.jpg")
            with patch.object(workflow.ts, "decide", return_value=result), \
                 patch.object(workflow.gp, "load_profile", return_value=profile), \
                 patch.object(workflow, "ArmBridge") as bridge:
                report = workflow.run(self.args(root))
            self.assertEqual(report["status"], "CHECK_FAILED")
            self.assertIn("invalid score", report["error"])
            self.assertFalse(report["motion_attempted"])
            self.assertEqual(json.loads((Path(report["output_dir"])/"result.json").read_text())["status"], "CHECK_FAILED")
            bridge.assert_not_called()

    def test_saved_image_go_rejected_before_loading(self):
        with self.assertRaises(SystemExit), patch.object(workflow, "run") as run:
            workflow.main(["--image", "missing.jpg", "--text", "cup", "--go"])
        run.assert_not_called()

    def test_hand_negative_blocks_live_pick(self):
        bridge, _, result, _, profile, _, _ = live_fixture()
        negative = {"status": "NOT_FOUND", "candidates": [], "chosen": None}
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(Path(directory))
            args.image = args.hand_image = None
            args.arm_url, args.go = "http://test.invalid", True
            with patch.object(workflow, "ArmBridge", return_value=bridge), \
                 patch.object(workflow.ts, "decide", side_effect=[result, negative]), \
                 patch.object(workflow.gp, "load_profile", return_value=profile):
                report = workflow.run(args)
            self.assertEqual(report["status"], "EXECUTION_BLOCKED")
            bridge.submit_grasp.assert_not_called()

    def test_positive_detections_cannot_bypass_missing_workspace_or_validation(self):
        from test_reliability import workspace, validation_record
        for missing in ("workspace", "validation"):
            bridge, _, result, _, profile, status, _ = live_fixture()
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args = self.args(root)
                args.image = args.hand_image = None
                args.arm_url, args.go = "http://test.invalid", True
                for name, data in (("workspace", workspace()), ("validation", validation_record(status, status["table_calibration"]))):
                    path = root / f"{name}.json"
                    path.write_text(json.dumps(data))
                    setattr(args, name, None if name == missing else str(path))
                with patch.object(workflow, "ArmBridge", return_value=bridge), \
                     patch.object(workflow.ts, "decide", return_value=result), \
                     patch.object(workflow.gp, "load_profile", return_value=profile):
                    report = workflow.run(args)
                self.assertEqual(report["status"], "EXECUTION_BLOCKED")
                self.assertTrue(any(missing in b.lower() for b in report["execution_blocks"]))
                self.assertFalse(report["motion_attempted"])
                bridge.submit_grasp.assert_not_called()

    def test_motion_timeout_is_unknown_and_never_retried(self):
        bridge, image, result, calibration, profile, status, packet = live_fixture()
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(Path(directory))
            args.image = args.hand_image = None
            args.arm_url, args.go = "http://test.invalid", True
            from test_reliability import workspace, validation_record
            args.workspace, args.validation = str(Path(directory) / "workspace.json"), str(Path(directory) / "validation.json")
            Path(args.workspace).write_text(json.dumps(workspace()))
            Path(args.validation).write_text(json.dumps(validation_record(status, status["table_calibration"])))
            meta = {role: {"camera_id": role, "sequence": 1} for role in ("stand", "hand")}
            fresh = {role: {"camera_id": role, "sequence": 2} for role in meta}
            bridge.submit_grasp.side_effect = TimeoutError("connection lost")
            with patch.object(workflow, "ArmBridge", return_value=bridge), \
                 patch.object(workflow, "read_live", side_effect=[({r: image for r in meta}, status, meta),
                                                                ({r: image for r in meta}, status, fresh)]), \
                 patch.object(workflow.ts, "decide", return_value=result), \
                 patch.object(workflow.gp, "load_profile", return_value=profile):
                report = workflow.run(args)
            self.assertEqual(report["status"], "MOTION_RESULT_UNKNOWN")
            bridge.submit_grasp.assert_called_once()


if __name__ == "__main__":
    unittest.main()
