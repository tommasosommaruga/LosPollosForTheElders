"""Explainable candidate selection after SAM. No model or hardware dependencies."""
import cv2
import numpy as np


def workspace_polygon(config, image_size, camera_id=None):
    if config.get("schema_version") != 1 or config.get("role") != "stand":
        raise ValueError("Workspace must describe the fixed stand camera, schema version 1")
    if config.get("image_size") != list(image_size):
        raise ValueError("Workspace resolution differs from the current camera image")
    if camera_id is not None and config.get("camera_id") != camera_id:
        raise ValueError("Workspace is not bound to this standing camera ID")
    polygon = np.asarray(config.get("polygon_px"), dtype=np.float32)
    if polygon.ndim != 2 or polygon.shape[1] != 2 or len(polygon) < 3 or not np.isfinite(polygon).all():
        raise ValueError("Workspace needs at least three finite pixel points")
    w, h = image_size
    if (polygon < 0).any() or (polygon[:, 0] >= w).any() or (polygon[:, 1] >= h).any():
        raise ValueError("Workspace polygon lies outside the image")
    if not cv2.isContourConvex(polygon) or cv2.contourArea(polygon) < 4:
        raise ValueError("Use an ordered, convex workspace polygon with nonzero area")
    return polygon


def select_candidates(result, image_size, workspace=None, camera_id=None, pick_at=None):
    """Preserve full silhouettes; test their bottom band against the stand ROI.

    The bottom band is only an image-space support estimate, not a measured 3D
    contact point. The ROI defines where to look; it does not certify reachability.
    """
    w, h = image_size
    polygon = workspace_polygon(workspace, image_size, camera_id) if workspace else None
    roi = np.zeros((h, w), np.uint8) if polygon is not None else None
    if roi is not None:
        cv2.fillPoly(roi, [np.rint(polygon).astype(np.int32)], 1)
    candidates, eligible, uncertain = [], [], []
    for i, original in enumerate(result["candidates"]):
        c = dict(original)
        c["source_match"] = bool(c.get("is_match"))
        c["is_match"] = False
        reasons = []
        raw = np.asarray(c.get("mask"))
        if raw.shape != (h, w) or not np.isfinite(raw).all():
            raise ValueError(f"Candidate {i}: mask dimensions or values are invalid")
        mask = raw.astype(bool)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        area = int(mask.sum())
        quality = {"original_area_px": area, "removed_speckles_px": 0}
        if area:
            largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            fraction = float(stats[largest, cv2.CC_STAT_AREA] / area)
            quality["largest_component_fraction"] = fraction
            if fraction >= .95:
                cleaned = labels == largest
                quality["removed_speckles_px"] = int(area - cleaned.sum())
                mask = cleaned
            else:
                reasons.append("fragmented_mask")
        if area < 20:
            reasons.append("mask_too_small")
        ys, xs = np.nonzero(mask)
        outside = False
        if len(xs):
            c["box"] = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
            if xs.min() == 0 or ys.min() == 0 or xs.max() == w - 1 or ys.max() == h - 1:
                reasons.append("object_cut_off")
            if roi is not None:
                band = ys >= ys.max() - max(2, int(.08 * (ys.max() - ys.min() + 1)))
                inside = float(roi[ys[band], xs[band]].mean())
                quality["support_band_inside_fraction"] = inside
                outside = inside == 0
                if inside < .8:
                    reasons.append("outside_workspace" if outside else "workspace_boundary")
        c["mask"] = mask
        scores = [c.get("sam_score"), c.get("target_score")]
        if any(type(s) not in (float, int) or not np.isfinite(s) or not 0 <= s <= 1 for s in scores):
            raise ValueError(f"Candidate {i}: invalid score; expected finite values in [0, 1]")
        near_threshold = (not c["source_match"] and
                          c.get("vlm", {}).get("reason", "").startswith("SAM only") and
                          .4 <= c["target_score"] < .5)
        if not c["source_match"]:
            reasons.append("low_target_score" if near_threshold else "target_not_matched")
        if c["source_match"] and not reasons:
            c["is_match"] = True
            eligible.append(c)
        elif not outside and (c["source_match"] or near_threshold):
            uncertain.append(c)
        c["decision"] = {"candidate_id": i, "eligible": c["is_match"], "reasons": reasons,
                         "mask_quality": quality}
        candidates.append(c)
    if result["status"] == "CHECK_FAILED":
        status, reason, chosen = "CHECK_FAILED", "Upstream identity check failed", None
    elif pick_at is not None:
        x, y = pick_at
        if not np.isfinite([x, y]).all() or not 0 <= x < w or not 0 <= y < h:
            raise ValueError("Selection point is outside the current stand image")
        hits = [c for c in eligible if c["mask"][int(y), int(x)]]
        uncertain_hits = [c for c in uncertain if c["mask"][int(y), int(x)]]
        chosen = hits[0] if len(hits) == 1 and not uncertain_hits else None
        status = "GO" if chosen is not None else "NEEDS_REVIEW"
        reason = "Explicit point selects one eligible mask" if chosen is not None else "Point is ambiguous or does not select an eligible mask"
    elif uncertain:
        status, reason, chosen = "NEEDS_REVIEW", "Possible target has uncertain score, mask or workspace boundary", None
    elif len(eligible) == 1:
        status, reason, chosen = "GO", "One eligible target remains after workspace and mask checks", eligible[0]
    elif len(eligible) > 1:
        status, reason, chosen = "AMBIGUOUS", "Multiple eligible targets; clarify the request or select a point", None
    else:
        status, reason, chosen = "NOT_FOUND", "No eligible target in the searched region", None
    return {"status": status, "candidates": candidates, "chosen": chosen,
            "decision_reason": reason, "search_scope": "configured_workspace" if workspace else "full_image",
            "workspace": workspace, "pick_at_px": list(pick_at) if pick_at is not None else None}
