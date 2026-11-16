"""Read-only calibration diagnostics. Never opens cameras or commands the arm."""
import argparse
import json
from pathlib import Path

import numpy as np
import requests

from pick_v5 import Calib
import calibration_validation


def summarize_camera(data):
    pairs = np.asarray(data["pairs"], dtype=float)
    size = float(data["size"])
    if pairs.ndim != 2 or pairs.shape[1] != 4 or len(pairs) < 4 or not np.isfinite(pairs).all():
        raise ValueError("Need at least four finite [u,v,x_cm,y_cm] marks")
    if not np.isfinite(size) or size <= 0:
        raise ValueError("Calibration image width must be finite and positive")
    calib = Calib(pairs.tolist(), size)
    fit = [float(np.linalg.norm(np.array(calib.to_table(u, v)) - [x, y])) for u, v, x, y in pairs]
    loo = calib.loo
    if not np.isfinite(fit + loo).all():
        raise ValueError("Degenerate calibration produced nonfinite errors")
    return {"marks": len(pairs), "image_width_px": size,
            "fit_rms_cm": float(np.sqrt(np.mean(np.square(fit)))),
            "fit_max_cm": max(fit),
            "leave_one_out_max_cm": max(loo) if loo else None,
            "camera_error_gate_passed": bool(len(pairs) >= 6 and loo and max(loo) <= 1.0),
            "physical_accuracy_validated": False,
            "note": "Residuals from stored marks, not independent robot landing measurements. Target coverage must also be checked."}


def summarize_table(data):
    a, b, c = [float(data[key]) for key in ("a", "b", "c")]
    if not np.isfinite([a, b, c]).all():
        raise ValueError("Table coefficients must be finite")
    report = {"equation": "z = a*x + b*y + c", "units": "m", "a": a, "b": b, "c": c,
              "physical_accuracy_validated": False,
              "note": "Touch residuals cannot detect shared joint/FK/fingertip error"}
    points = np.asarray(data.get("touches_m", []), dtype=float)
    if points.size:
        if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
            raise ValueError("Table touches must be finite xyz points in metres")
        errors = (points[:, 2] - a * points[:, 0] - b * points[:, 1] - c) / np.sqrt(a*a + b*b + 1) * 1000
        report.update(touches=len(points), fit_rms_mm=float(np.sqrt(np.mean(errors**2))),
                      fit_max_mm=float(np.abs(errors).max()))
    else:
        report.update(touches=0, fit_rms_mm=None, fit_max_mm=None)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--camera", help="measured camera JSON file")
    source.add_argument("--arm-url", help="read camera pairs from controller GET /status")
    parser.add_argument("--table", help="measured table JSON file on this laptop")
    parser.add_argument("--validation", help="independent measurement record to evaluate")
    parser.add_argument("--write-validation-template", help="write an empty record bound to the loaded geometry")
    parser.add_argument("--camera-id", help="exact stand camera ID from /observation")
    parser.add_argument("--image-size", nargs=2, type=int, metavar=("WIDTH", "HEIGHT"))
    args = parser.parse_args(argv)
    if not any((args.camera, args.arm_url, args.table)):
        parser.error("Provide --camera, --arm-url or --table")
    report = {"motion_allowed": False}
    try:
        camera, table, status = None, None, {}
        if args.camera:
            camera = json.loads(Path(args.camera).read_text())
            report["camera"] = summarize_camera(camera)
        elif args.arm_url:
            response = requests.get(args.arm_url.rstrip("/") + "/status", timeout=5)
            response.raise_for_status()
            status = response.json()
            camera, table = status, status.get("table_calibration")
            report["camera"] = summarize_camera(status)
            report["camera"]["selected_camera_role"] = status.get("camera_role")
        if args.table:
            table = json.loads(Path(args.table).read_text())
        if table:
            report["table"] = summarize_table(table)
        if args.validation or args.write_validation_template:
            if args.arm_url and status.get("camera_role") != "stand":
                raise ValueError("Select the fixed standing camera before recording validation")
            frame = status.get("observations", {}).get("frames", {}).get("stand", {})
            size = args.image_size or frame.get("image_size")
            camera_id = args.camera_id or frame.get("camera_id")
            if not camera or not table or not size or not camera_id:
                raise ValueError("Validation needs camera and table geometry, camera ID and full image size")
            if args.write_validation_template:
                path = Path(args.write_validation_template)
                # Never overwrite existing real measurements with a blank template.
                with path.open("x") as file:
                    json.dump(calibration_validation.template(camera, table, size, camera_id), file, indent=2)
                report["template"] = str(path)
            if args.validation:
                record = json.loads(Path(args.validation).read_text())
                report["independent_validation"] = calibration_validation.validate(record, camera, table, size, camera_id)
    except (ValueError, KeyError, TypeError, OSError, requests.RequestException, np.linalg.LinAlgError) as exc:
        parser.exit(2, f"Calibration audit failed: {exc}\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    return report


if __name__ == "__main__":
    main()
