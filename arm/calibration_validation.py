"""Check independently recorded camera/table measurements against active geometry.

This validates the supplied measurements, not their physical truth or overall
robot accuracy. No file can certify unknown joint, fingertip or collision errors.
"""
import hashlib
import json

import cv2
import numpy as np
from pick_v5 import Calib


def fingerprint(camera, table):
    geometry = {"camera": {"cam": int(camera.get("cam", 0)), "size": float(camera["size"]),
                            "pairs": np.asarray(camera["pairs"], float).tolist()},
                "table": {key: float(table[key]) for key in ("a", "b", "c")}}
    return hashlib.sha256(json.dumps(geometry, sort_keys=True, allow_nan=False).encode()).hexdigest()


def template(camera, table, image_size, camera_id):
    return {"schema_version": 1, "camera_id": camera_id, "image_size": list(image_size),
            "geometry_sha256": fingerprint(camera, table),
            "camera_checks": [], "table_checks_base_m": [],
            "instructions": "Use unused independently measured points. camera_checks: {pixel:[u,v], measured_xy_cm:[x,y]}; table_checks_base_m: [x,y,z] touches not used to fit the plane. Do not copy calibration marks."}


def validate(record, camera, table, image_size, camera_id=None, target_px=None):
    out = {"passed": False, "errors": [], "physical_accuracy_certified": False}
    errors = out["errors"]
    if record is None:
        errors.append("Independent calibration validation measurements are required")
        return out
    try:
        if not table:
            raise ValueError("Active controller table plane is unavailable; upgrade the controller")
        if record.get("schema_version") != 1 or record.get("geometry_sha256") != fingerprint(camera, table):
            raise ValueError("Validation record does not match the active camera/table geometry")
        if record.get("image_size") != list(image_size) or not record.get("camera_id"):
            raise ValueError("Validation camera ID/resolution is missing or mismatched")
        if camera_id is not None and record["camera_id"] != camera_id:
            raise ValueError("Validation belongs to another camera")
        checks = record.get("camera_checks", [])
        if len(checks) < 6:
            raise ValueError("At least six independent camera check points are required")
        px = np.asarray([p["pixel"] for p in checks], float)
        xy = np.asarray([p["measured_xy_cm"] for p in checks], float)
        if px.shape != (len(checks), 2) or xy.shape != px.shape or not np.isfinite([px, xy]).all():
            raise ValueError("Camera checks must be finite pixel/xy pairs")
        w, h = image_size
        if (px < 0).any() or (px[:, 0] >= w).any() or (px[:, 1] >= h).any():
            raise ValueError("Camera check pixels lie outside the declared image")
        scale = float(camera["size"]) / w
        original = np.asarray(camera["pairs"], float)[:, :2] / scale
        if np.min(np.linalg.norm(px[:, None, :] - original[None, :, :], axis=2)) < 2:
            raise ValueError("Validation reuses a fitted camera mark")
        if len(np.unique(px, axis=0)) < 6:
            raise ValueError("Camera check points must be distinct")
        hull = cv2.convexHull(px.astype(np.float32))
        if cv2.contourArea(hull) < 4:
            raise ValueError("Camera checks must span a two-dimensional region")
        calib = Calib(camera["pairs"], camera["size"], h / w)
        predicted = np.asarray([calib.to_table(u * scale, v * scale) for u, v in px])
        distances = np.linalg.norm(predicted - xy, axis=1)
        if not np.isfinite(distances).all():
            raise ValueError("Camera validation produced nonfinite errors")
        out.update(camera_max_error_cm=float(distances.max()),
                   camera_rms_error_cm=float(np.sqrt(np.mean(distances**2))),
                   camera_checks=len(checks))
        if distances.max() > 1:
            errors.append("Independent camera error exceeds the provisional 1 cm limit")
        if target_px is not None and cv2.pointPolygonTest(hull, tuple(map(float, target_px)), False) < 0:
            errors.append("Grasp footprint lies outside the independent validation area")
        points = np.asarray(record.get("table_checks_base_m", []), float)
        if points.ndim != 2 or points.shape[1] != 3 or len(points) < 4 or not np.isfinite(points).all():
            raise ValueError("At least four finite independent table checks in base metres are required")
        if len(np.unique(points[:, :2], axis=0)) < 4 or np.linalg.matrix_rank(np.c_[points[:, :2], np.ones(len(points))]) < 3:
            raise ValueError("Table checks must be distinct and span the table")
        fitted = np.asarray(table.get("touches_m", []), float)
        if fitted.size and np.min(np.linalg.norm(points[:, None, :] - fitted[None, :, :], axis=2)) < .001:
            raise ValueError("Validation reuses a fitted table touch")
        if target_px is not None:
            target_xy_m = np.asarray(calib.to_table(*(np.asarray(target_px) * scale))) / 100
            table_hull = cv2.convexHull(points[:, :2].astype(np.float32))
            if cv2.pointPolygonTest(table_hull, tuple(map(float, target_xy_m)), False) < 0:
                errors.append("Grasp footprint lies outside the independent table validation area")
        a, b, c = [float(table[key]) for key in ("a", "b", "c")]
        residual = np.abs(points[:, 2] - a*points[:, 0] - b*points[:, 1] - c) / np.sqrt(a*a + b*b + 1) * 100
        out.update(table_max_error_cm=float(residual.max()), table_checks=len(points))
        if residual.max() > 1:
            errors.append("Independent table error exceeds the provisional 1 cm limit")
        out["passed"] = not errors
    except (ValueError, KeyError, TypeError, IndexError, np.linalg.LinAlgError) as exc:
        errors.append(str(exc))
    return out
