"""Read-only observation snapshots. Uses measured encoders, never commanded goals."""
import base64
import copy
import math
import struct
import time

from .vision_contract import MAX_JOINT_AGE_S


def measured_pose(joints, updated_at, model, now):
    errors, readings = [], {}
    for mid in range(1, 8):
        joint = joints.get(mid)
        stamp = updated_at.get(mid)
        if joint is None or stamp is None:
            errors.append(f"joint {mid} missing")
            continue
        age = now - stamp
        if not math.isfinite(age) or age < 0 or age > MAX_JOINT_AGE_S:
            errors.append(f"joint {mid} stale")
        if not joint.calibrated:
            errors.append(f"joint {mid} uncalibrated")
        readings[str(mid)] = {"raw": joint.raw, "range_min": joint.range_min,
                              "range_max": joint.range_max, "age_s": age}
    matrix, angles = None, None
    if not errors:
        try:
            angles = model.angles_true(joints)
            matrix, _ = model.frames(angles)
            if not all(math.isfinite(v) for row in matrix for v in row):
                raise ValueError("nonfinite FK")
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
            matrix = angles = None
    return {"joint_readings": readings, "joint_angles_rad": angles,
            "T_base_gripper": matrix, "translation_units": "m",
            "pose_valid": not errors, "errors": errors,
            "angle_mapping": "raw encoder scale, calibrated midpoint offset; physical validation required"}


def jpeg_dimensions(data):
    """Read JPEG SOF dimensions without adding a camera/image runtime dependency."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("Not a JPEG")
    i = 2
    while i + 3 < len(data):
        if data[i] != 255:
            raise ValueError("Invalid JPEG marker")
        while i < len(data) and data[i] == 255:
            i += 1
        if i >= len(data):
            break
        marker = data[i]
        i += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > len(data):
            break
        length = struct.unpack_from(">H", data, i)[0]
        if length < 2 or i + length > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2):
            if length < 7:
                break
            h, w = struct.unpack_from(">HH", data, i + 3)
            if min(w, h) <= 0:
                break
            return [w, h]
        i += length
    raise ValueError("JPEG dimensions unavailable")


class ObservationStore:
    def __init__(self):
        self.frames = {}
        self.sequence = 0

    def record(self, camera_id, role, jpeg, pose, received_at=None):
        now = time.monotonic() if received_at is None else received_at
        self.sequence += 1
        self.frames[role] = {"camera_id": camera_id, "role": role,
                             "sequence": self.sequence, "received_monotonic_s": now,
                             "image_size": jpeg_dimensions(jpeg),
                             "timestamp_basis": "host_receive", "capture_timestamp_s": None,
                             "source_age_known": False, "pose": copy.deepcopy(pose),
                             "jpeg_base64": base64.b64encode(jpeg).decode("ascii")}

    def snapshot(self, now=None, images=True):
        now = time.monotonic() if now is None else now
        frames = copy.deepcopy(self.frames)
        for frame in frames.values():
            frame["receive_age_s"] = now - frame["received_monotonic_s"]
            if not images:
                frame.pop("jpeg_base64")
        return {"schema_version": 1, "server_monotonic_s": now,
                "synchronized": False, "frames": frames,
                "note": "Receive times are not exposure times. Not suitable for moving-camera triangulation without timestamp calibration."}
