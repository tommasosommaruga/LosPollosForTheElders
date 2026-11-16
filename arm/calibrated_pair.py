"""Triangulate supplied matches from a calibrated, timestamped capture; offline only."""
import argparse
import json
import math
from pathlib import Path

import numpy as np

from multiview_geometry import hand_camera_pose, rigid_transform, triangulate


def prepare_pair(packet, calibration):
    if calibration.get("schema_version") != 1 or packet.get("schema_version") != 1:
        raise ValueError("Unsupported calibration/observation schema")
    if calibration.get("validated") is not True or not calibration.get("calibration_id"):
        raise ValueError("Measured and independently validated calibration is required")
    if calibration.get("translation_units") != "m":
        raise ValueError("Calibration translations must be metres")
    if packet.get("synchronized") is not True:
        raise ValueError("Capture synchronization has not been established")
    frames = packet["frames"]
    cameras = calibration["cameras"]
    stamps = []
    for role in ("stand", "hand"):
        frame, cam = frames[role], cameras[role]
        if not cam.get("camera_id") or frame["camera_id"] != cam["camera_id"]:
            raise ValueError(f"{role}: camera identity mismatch")
        if frame["image_size"] != cam["image_size"] or cam["image_size"] is None:
            raise ValueError(f"{role}: calibration resolution mismatch")
        stamp = frame.get("capture_timestamp_s")
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or frame.get("timestamp_basis") != "exposure_common_clock":
            raise ValueError("Need exposure timestamps on a common clock, not receive times")
        stamps.append(stamp)
    skew = calibration.get("max_pair_skew_s")
    if type(skew) not in (int, float) or not math.isfinite(skew) or skew <= 0 or abs(stamps[0] - stamps[1]) > skew:
        raise ValueError("Camera pair timing exceeds the configured skew limit")
    pose = frames["hand"]["pose"]
    if pose.get("pose_valid") is not True or pose.get("translation_units") != "m":
        raise ValueError("Valid measured gripper pose is required")
    # An acquisition adapter must explicitly attach/interpolate the pose at exposure.
    if pose.get("timestamp_basis") != "exposure_common_clock" or pose.get("capture_timestamp_s") != stamps[1]:
        raise ValueError("Gripper pose is not aligned to hand-camera exposure")
    stand = rigid_transform(cameras["stand"]["T_base_camera"])
    hand = hand_camera_pose(pose["T_base_gripper"], cameras["hand"]["T_gripper_camera"])
    return stand, hand


def reconstruct(packet, calibration, matches):
    stand, hand = prepare_pair(packet, calibration)
    if matches.get("calibration_id") != calibration["calibration_id"]:
        raise ValueError("Correspondences refer to another calibration")
    for role in ("stand", "hand"):
        if matches.get(f"{role}_sequence") != packet["frames"][role]["sequence"]:
            raise ValueError("Correspondences refer to another frame")
        points = np.asarray(matches[f"{role}_points_px"], dtype=float)
        width, height = packet["frames"][role]["image_size"]
        if points.ndim != 2 or points.shape[1] != 2 or not np.isfinite(points).all() or np.any(points < 0) or np.any(points >= [width, height]):
            raise ValueError("Correspondences must lie within the original images")
    a, b = calibration["cameras"]["stand"], calibration["cameras"]["hand"]
    result = triangulate(matches["stand_points_px"], matches["hand_points_px"], a["K"], b["K"], stand, hand, a["distortion"], b["distortion"])
    # Rejected points become null so downstream code cannot mistake them for usable coordinates.
    return {"calibration_id": calibration["calibration_id"], "motion_allowed": False,
            "points_base_m": [p.tolist() if ok else None for p, ok in zip(result["points_base_m"], result["valid"])],
            "valid": result["valid"].tolist()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("observation", "calibration", "matches", "out"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    load = lambda path: json.loads(Path(path).read_text())
    try:
        result = reconstruct(load(args.observation), load(args.calibration), load(args.matches))
    except (ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    Path(args.out).write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
