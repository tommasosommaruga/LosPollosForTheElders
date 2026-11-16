"""Calibrated geometry primitives. No estimated/default calibration and no motion.

Transforms map camera coordinates into a common robot-base frame. Translation
units MUST be metres; image coordinates MUST match the calibration resolution.
Correspondences must identify the same physical point, not mask centroids.
"""
import cv2
import numpy as np


def rigid_transform(value):
    t = np.asarray(value, dtype=float)
    if (t.shape != (4, 4) or not np.isfinite(t).all()
            or not np.allclose(t[3], [0, 0, 0, 1])
            or not np.allclose(t[:3, :3].T @ t[:3, :3], np.eye(3), atol=1e-5)
            or not np.isclose(np.linalg.det(t[:3, :3]), 1, atol=1e-5)):
        raise ValueError("Expected a finite rigid 4x4 transform with a proper rotation")
    return t


def hand_camera_pose(base_from_gripper, gripper_from_camera):
    """Compose FK output and measured hand-eye transform; not forward kinematics."""
    return rigid_transform(base_from_gripper) @ rigid_transform(gripper_from_camera)


def triangulate(stand_px, hand_px, stand_K, hand_K, base_from_stand,
                base_from_hand, stand_dist=None, hand_dist=None,
                max_reprojection_px=2.0, min_angle_deg=2.0):
    """Return base-frame points and per-point rejection flags.

    Passing the gates measures internal geometric consistency only. It does not
    establish calibration accuracy, matching correctness, collision clearance,
    synchronization, or a safe grasp. Never feed uncalibrated SIFT matches into
    motion just because this function accepted them.
    """
    pixels = [np.asarray(p, dtype=float) for p in (stand_px, hand_px)]
    if (pixels[0].ndim != 2 or pixels[0].shape[1] != 2
            or pixels[0].shape != pixels[1].shape or len(pixels[0]) == 0
            or not all(np.isfinite(p).all() for p in pixels)):
        raise ValueError("Provide equal, nonempty Nx2 finite physical point correspondences")
    if not (np.isfinite(max_reprojection_px) and max_reprojection_px > 0
            and np.isfinite(min_angle_deg) and 0 < min_angle_deg < 90):
        raise ValueError("Invalid geometric gate thresholds")
    matrices = [np.asarray(k, dtype=float) for k in (stand_K, hand_K)]
    for k in matrices:
        if (k.shape != (3, 3) or not np.isfinite(k).all() or k[0, 0] <= 0 or k[1, 1] <= 0
                or not np.allclose(k[2], [0, 0, 1])):
            raise ValueError("Invalid camera intrinsic matrix")
    poses = [rigid_transform(t) for t in (base_from_stand, base_from_hand)]
    extrinsics = [np.linalg.inv(t)[:3] for t in poses]
    distortions = [None if d is None else np.asarray(d, dtype=float) for d in (stand_dist, hand_dist)]
    if any(d is not None and not np.isfinite(d).all() for d in distortions):
        raise ValueError("Nonfinite distortion coefficients")
    normalized = [cv2.undistortPoints(p.reshape(-1, 1, 2), k, d).reshape(-1, 2)
                  for p, k, d in zip(pixels, matrices, distortions)]
    homogeneous = cv2.triangulatePoints(*extrinsics, normalized[0].T, normalized[1].T).T
    with np.errstate(divide="ignore", invalid="ignore"):
        xyz = homogeneous[:, :3] / homogeneous[:, 3, None]
    finite = np.isfinite(xyz).all(axis=1) & (np.abs(homogeneous[:, 3]) > 1e-12)
    safe_xyz = np.where(finite[:, None], xyz, 0.0)
    errors, depths, rays = [], [], []
    for p, k, d, pose, ext, norm in zip(pixels, matrices, distortions, poses, extrinsics, normalized):
        depth = (safe_xyz @ ext[:, :3].T + ext[:, 3])[:, 2]
        depths.append(depth)
        rotation, _ = cv2.Rodrigues(ext[:, :3])
        projected, _ = cv2.projectPoints(safe_xyz, rotation, ext[:, 3], k, d)
        errors.append(np.linalg.norm(projected.reshape(-1, 2) - p, axis=1))
        ray = np.column_stack([norm, np.ones(len(norm))]) @ pose[:3, :3].T
        rays.append(ray / np.linalg.norm(ray, axis=1)[:, None])
    angle = np.degrees(np.arccos(np.clip(np.sum(rays[0] * rays[1], axis=1), -1, 1)))
    max_error = np.maximum(*errors)
    positive_depth = (depths[0] > 0) & (depths[1] > 0)
    separation = np.minimum(angle, 180 - angle)
    valid = finite & positive_depth & np.isfinite(max_error) & (max_error <= max_reprojection_px) & (separation >= min_angle_deg)
    return {"points_base_m": xyz, "valid": valid, "reprojection_error_px": max_error,
            "ray_angle_deg": angle, "positive_depth": positive_depth,
            "motion_allowed": False}
