import unittest
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

import calibration_report as audit


class CalibrationAuditTests(unittest.TestCase):
    def camera(self, count=8):
        pixels = [(0, 0), (160, 0), (0, 120), (160, 120), (80, 0), (80, 120), (0, 60), (160, 60)]
        return {"size": 160, "pairs": [[u, v, u/10, v/10] for u, v in pixels[:count]]}

    def test_exact_planar_mapping_passes_error_gate_but_not_physical_validation(self):
        report = audit.summarize_camera(self.camera())
        self.assertLess(report["leave_one_out_max_cm"], 1e-8)
        self.assertTrue(report["camera_error_gate_passed"])
        self.assertFalse(report["physical_accuracy_validated"])

    def test_five_marks_do_not_claim_validation(self):
        report = audit.summarize_camera(self.camera(5))
        self.assertIsNone(report["leave_one_out_max_cm"])
        self.assertFalse(report["camera_error_gate_passed"])

    def test_inconsistent_marks_fail_error_gate(self):
        data = self.camera()
        data["pairs"][4][2] += 8
        self.assertFalse(audit.summarize_camera(data)["camera_error_gate_passed"])

    def test_invalid_calibration_rejected(self):
        data = self.camera()
        data["pairs"][0][2] = float("nan")
        with self.assertRaises(ValueError):
            audit.summarize_camera(data)

    def test_plane_residuals_are_perpendicular_distance_in_mm(self):
        report = audit.summarize_table({"a": 0, "b": 0, "c": .02,
                                        "touches_m": [[0, 0, .02], [.1, 0, .023], [0, .1, .016]]})
        self.assertAlmostEqual(report["fit_max_mm"], 4)
        self.assertAlmostEqual(report["fit_rms_mm"], (25/3)**.5)
        self.assertFalse(report["physical_accuracy_validated"])

    def test_missing_touch_samples_do_not_invent_accuracy(self):
        report = audit.summarize_table({"a": 0, "b": 0, "c": .02})
        self.assertIsNone(report["fit_max_mm"])

    def test_live_template_binds_active_geometry_and_never_overwrites_measurements(self):
        status = {**self.camera(), "cam": 0, "camera_role": "stand",
                  "table_calibration": {"a": 0, "b": 0, "c": 0},
                  "observations": {"frames": {"stand": {"camera_id": "stand", "image_size": [160, 120]}}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "validation.json"
            args = ["--arm-url", "http://test.invalid", "--write-validation-template", str(path)]
            with patch.object(audit.requests, "get", return_value=Mock(json=lambda: status)) as get, \
                 patch.object(audit.requests, "post", side_effect=AssertionError("No writes")), redirect_stdout(io.StringIO()):
                report = audit.main(args)
                self.assertFalse(report["motion_allowed"])
                record = json.loads(path.read_text())
                self.assertEqual(record["camera_id"], "stand")
                self.assertEqual(record["camera_checks"], [])
                path.write_text("measurements to preserve")
                with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                    audit.main(args)
                self.assertEqual(path.read_text(), "measurements to preserve")
            self.assertEqual(get.call_args.args[0], "http://test.invalid/status")

    def test_moving_camera_cannot_create_fixed_camera_validation(self):
        with patch.object(audit.requests, "get", return_value=Mock(json=lambda: {**self.camera(), "camera_role": "hand"})), \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            audit.main(["--arm-url", "http://test.invalid", "--write-validation-template", "must-not-exist.json"])


if __name__ == "__main__":
    unittest.main()
