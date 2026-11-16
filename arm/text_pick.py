"""Text -> two-view segmentation -> grasp proposal -> optional controller pickup.

No hardware is opened on import. Saved images never command motion. The fixed
stand view supplies approximate table geometry; the hand view checks identity,
not depth. All physical distances in the controller contract are centimetres.
"""
import argparse
import base64
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import uuid

import cv2
import numpy as np
from PIL import Image

from arm_bridge import ArmBridge, GRIPPER_OPEN_CM, grasp_request, request_errors
import grasp_planner_v3 as gp
import pick_v5 as pick
import target_selector_v5 as ts
from voice_pick import describe_item
from selection_policy import select_candidates
import calibration_validation

HERE = Path(__file__).resolve().parent
MAX_RECEIVE_AGE_S = 1.0


def object_region(mask):
    ys, xs = np.nonzero(mask)
    if not len(xs):
        raise ValueError("Selected mask is empty")
    return {"area_px": int(len(xs)), "image_fraction": float(mask.mean()),
            "bbox_xyxy_px": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
            "centroid_px": [float(xs.mean()), float(ys.mean())],
            "touches_image_edge": bool(xs.min() == 0 or ys.min() == 0 or
                                       xs.max() == mask.shape[1] - 1 or ys.max() == mask.shape[0] - 1)}


def pixel_grasp(mask, method, profile):
    if method == "side":
        # Unit pixel scale and an unrestricted opening rank the silhouette only.
        g = pick.best_side_row(mask, 1.0, sum(mask.shape) + 1, profile.get("grasp_height_frac", .5))
        if g:
            g.pop("width_cm")
            g.pop("fits")
        return g
    g, _ = gp.plan_grasp(mask, top_k=1)
    return {"type": "top", "point_px": g["center_px"], "jaw1_px": g["jaw1_px"],
            "jaw2_px": g["jaw2_px"], "width_px": g["width_px"],
            "closing_angle_image_deg": g["closing_angle_deg"], "score": g["score"]} if g else None


def plan_selection(img, result, profile, *, calibration=None, vlm="none",
                   method="auto", height=None, above=None):
    """Return JSON data; retain the exact mask in result for rendering/export."""
    report = {"status": result["status"], "object_present": None,
              "selection": ts.summary(result), "execution_blocks": [],
              "controller_request": None, "motion_attempted": False}
    if result["status"] != "GO":
        report["object_present"] = False if result["status"] == "NOT_FOUND" else None
        report["execution_blocks"] = ["A unique target was not selected"]
        return report
    cand = result["chosen"]
    mask = np.asarray(cand["mask"], dtype=bool)
    if mask.shape != (img.height, img.width):
        raise ValueError("Selected mask does not match image dimensions")
    region = object_region(mask)
    report.update(status="PREVIEW", object_present=True, region=region)
    for key in ("width_cm", "height_cm", "grasp_height_frac"):
        value = profile.get(key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
            raise ValueError(f"Item profile {key} must be finite and positive")
    if profile.get("grasp_height_frac", .5) >= 1:
        raise ValueError("Item grasp_height_frac must be strictly between zero and one")
    # A cup can look squat from the wrist camera. Aspect ratio is not 3D posture.
    posture = profile.get("posture", "unknown")
    how = "configured item posture" if posture != "unknown" else "not measured; image aspect ratio is not used"
    if method != "auto":
        why = "explicit --grasp choice; requires measured setup for motion"
    else:
        method = profile.get("grasp", "auto")
        why = "configured item grasp method"
    if method not in ("top", "side"):
        report.update(status="NEEDS_GRASP_METHOD", posture={"value": posture, "basis": how})
        report["execution_blocks"].append("Choose a grasp method in a measured item profile or use --grasp for a proposal")
        return report
    report.update(posture={"value": posture, "basis": how}, method_reason=why,
                  geometry="2D silhouette proposal; table homography is approximate for raised objects",
                  approach={"method": method, "description": (
                      "Open, move above the target, descend, close, lift" if method == "top" else
                      "Open, move above a point nearer the base, lower, approach outward from the base, close, lift"),
                      "collision_checked": False, "orientation_verified": False})
    blocks = report["execution_blocks"]
    if profile.get("measurements_verified") is not True:
        blocks.append("Item dimensions and grasp method have not been verified on the real setup")
    elif method != profile.get("grasp"):
        blocks.append("Requested grasp method differs from the verified item profile")
    if region["touches_image_edge"]:
        blocks.append("Object is cut off by the image edge")
    g = pixel_grasp(mask, method, profile)
    if not g:
        report.update(status="NO_GRASP")
        blocks.append("No usable jaw contacts in the selected mask")
        return report
    report["grasp"] = g
    if calibration is None:
        blocks.append("Measured fixed-camera calibration is required for robot coordinates")
        return report
    calib = pick.Calib(calibration["pairs"], calibration["size"], img.height / img.width)
    scale = calib.size / img.width
    cu, cv = pick.contact_pixel(mask)
    cmpp = pick.local_cm_per_px(calib, cu, cv, scale)
    if not math.isfinite(cmpp) or cmpp <= 0:
        raise ValueError("Calibration produced an invalid image scale")
    metric = (pick.best_side_row(mask, cmpp, GRIPPER_OPEN_CM, profile.get("grasp_height_frac", .5))
              if method == "side" else pick.top_grasp(mask, calib, scale, cmpp, GRIPPER_OPEN_CM))
    if metric is None:
        report["status"] = "NO_GRASP"
        blocks.append("No calibrated grasp candidate")
        return report
    g = metric
    report["grasp"] = g
    if not g["fits"]:
        report["status"] = "NO_GRASP"
        blocks.append("Object width exceeds the usable gripper opening")
    profile_width = profile.get("width_cm")
    if profile_width and abs(g["width_cm"] - profile_width) > .35 * profile_width:
        blocks.append("Estimated width disagrees with the item profile by more than 35 percent")
    if g.get("tilt_deg", 0) > 20:
        blocks.append("Side height estimate is unreliable for this leaning object")
    if method == "side":
        if not profile_width:
            blocks.append("Side grasp needs measured item width to estimate the footprint")
        t = pick.choose_target(mask, calib, scale, (profile_width or g["width_cm"]) / 2, "contact")
    else:
        x, y = calib.to_table(g["point_px"][0] * scale, g["point_px"][1] * scale)
        t = {"target_px": g["point_px"], "x_cm": x, "y_cm": y,
             "mode": "grasp center projected onto the table; height not corrected"}
    object_height = profile.get("height_cm")
    if height is None and not object_height:
        blocks.append("Measured object height or explicit closing height is required")
    close, lift = pick.grab_heights(profile, height, above)
    if method == "side" and object_height and height is None:
        close = max(1.0, float(object_height * g["height_frac"]))
    if object_height and lift < object_height + pick.CLEARANCE_CM:
        blocks.append("Approach height must clear the object's top")
    accuracy, basis = calib.expected_error()
    report["calibration"] = {"marks": len(calib.pairs), "max_error_cm": accuracy if math.isfinite(accuracy) else None,
                             "error_basis": basis, "independent_physical_validation": False}
    blocks.extend(pick.calibration_blocks(calib, t["target_px"], scale))
    body = grasp_request(t["x_cm"], t["y_cm"], close, lift, g)
    blocks.extend(request_errors(body))
    report.update(footprint=t, controller_request=body)
    report["approach"].update(close_height_cm=close, approach_height_cm=lift,
                               lift_height_cm=lift)
    return report


def render_view(img, result, report, path):
    """Mask, bounding box, jaw contacts and point; image pixels stay unmodified in source files."""
    ts.draw_sam3_style(img, {**result, "status": report["status"]}, str(path), show_all=True)
    vis = cv2.imread(str(path))
    if result.get("workspace"):
        polygon = np.asarray(result["workspace"]["polygon_px"], np.int32)
        cv2.polylines(vis, [polygon], True, (0, 220, 255), 3)
    g = report.get("grasp")
    if g:
        a, b, c = [tuple(int(round(x)) for x in g[k]) for k in ("jaw1_px", "jaw2_px", "point_px")]
        color = (0, 220, 0) if g.get("fits", True) else (0, 150, 255)
        cv2.line(vis, a, b, color, 3)
        direction = np.asarray(b, float) - a
        normal = np.array([-direction[1], direction[0]]) / max(np.linalg.norm(direction), 1) * 15
        for p in (a, b):
            cv2.line(vis, tuple(np.rint(np.asarray(p) - normal).astype(int)),
                         tuple(np.rint(np.asarray(p) + normal).astype(int)), color, 4)
        cv2.drawMarker(vis, c, (0, 0, 255), cv2.MARKER_CROSS, 26, 3)
    # Put explanations below the image so they cannot hide the target.
    canvas = np.full((vis.shape[0] + 105, max(vis.shape[1], 720), 3), 245, np.uint8)
    canvas[:vis.shape[0], :vis.shape[1]] = vis
    lines = [f"{report['status']} | {report.get('target', '')}",
             (f"Object area: {report['region']['area_px']} px | {report['approach']['method']} approach | "
              "red: grasp point; green: jaw contacts") if g else "No grasp selected",
             result.get("decision_reason", "2D proposal. See result.json for execution checks.")]
    for i, line in enumerate(lines):
        cv2.putText(canvas, line[:120], (12, vis.shape[0] + 26 + 30 * i),
                    cv2.FONT_HERSHEY_SIMPLEX, .52, (25, 25, 25), 1, cv2.LINE_AA)
    if not cv2.imwrite(str(path), canvas):
        raise OSError(f"Cannot write {path}")


def read_live(bridge):
    status = bridge.get("/status")
    if status.get("camera_role") != "stand":
        raise ValueError("Select the calibrated fixed stand camera on the integrated controller")
    packet = bridge.get("/observation")
    images, metadata = {}, {}
    for role in ("stand", "hand"):
        frame = packet.get("frames", {}).get(role)
        if not isinstance(frame, dict):
            raise ValueError(f"Controller is missing the {role} camera observation")
        age = frame.get("receive_age_s")
        if type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age <= MAX_RECEIVE_AGE_S:
            raise ValueError(f"The {role} camera feed is stale")
        if frame.get("role") != role or not frame.get("camera_id") or type(frame.get("sequence")) is not int:
            raise ValueError(f"Invalid {role} camera metadata")
        img = Image.open(io.BytesIO(base64.b64decode(frame["jpeg_base64"], validate=True))).convert("RGB")
        if list(img.size) != frame.get("image_size"):
            raise ValueError(f"Incorrect {role} image resolution metadata")
        images[role] = img
        metadata[role] = {k: v for k, v in frame.items() if k not in ("jpeg_base64", "pose")}
    return images, status, metadata


def changed_scene(before, after, mask):
    """Conservative image-change gate, not tracking, depth, or collision checking."""
    if before.size != after.size:
        return True
    a, b = (cv2.GaussianBlur(np.asarray(im, np.float32), (5, 5), 0) for im in (before, after))
    changed = np.max(np.abs(a - b), axis=2) > 25
    near = cv2.dilate(np.asarray(mask, np.uint8), np.ones((31, 31), np.uint8)).astype(bool)
    return bool(changed.mean() > .01 or (near.any() and changed[near].mean() > .01))


def execute_live(report, images, results, status, metadata, bridge):
    """One motion attempt only. Caller must explicitly request --go using live views."""
    if report["execution_blocks"] or not report.get("controller_request"):
        report["status"] = "EXECUTION_BLOCKED"
        return
    current, fresh_status, fresh_meta = read_live(bridge)
    if any(fresh_status.get(k) != status.get(k) for k in ("cam", "pairs", "size", "calibrated", "table_calibration")):
        raise ValueError("Camera calibration changed during planning; rerun the command")
    for role in ("stand", "hand"):
        if fresh_meta[role]["camera_id"] != metadata[role]["camera_id"]:
            raise ValueError(f"The {role} camera changed during planning")
        if fresh_meta[role]["sequence"] <= metadata[role]["sequence"]:
            raise ValueError(f"No new {role} camera frame since planning")
        if changed_scene(images[role], current[role], results[role]["chosen"]["mask"]):
            raise ValueError(f"Scene changed in the {role} camera; rerun the command")
    report["motion_attempted"] = True
    result = bridge.submit_grasp(report["controller_request"])
    report.update(controller_result=result,
                  status="HOLDING_REPORTED" if result["holding"] else "EMPTY_REPORTED",
                  holding_verification="Controller encoder heuristic; possession/slip not visually verified")


def save_report(root, report):
    # Serialize before opening so invalid numbers cannot leave a partial JSON file.
    data = json.dumps(report, indent=2, allow_nan=False) + "\n"
    (root / "result.json").write_text(data)


def run(args):
    root = Path(args.out).resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    root.mkdir(parents=True)
    parsed = describe_item(args.text)
    report = {"schema_version": 1, "text": args.text, **parsed,
              "category": args.category,
              "mode": "saved_images" if args.image else "live_controller",
              "motion_attempted": False, "output_dir": str(root)}
    if parsed["status"] != "MATCHED":
        save_report(root, report)
        return report
    if args.go and args.image:
        raise ValueError("Saved images cannot be used with --go")
    images, results, status, metadata = {}, {}, {}, {}
    try:
        bridge = ArmBridge(args.arm_url) if args.arm_url else None
        if args.image:
            images["stand"] = Image.open(args.image).convert("RGB")
            if args.hand_image:
                images["hand"] = Image.open(args.hand_image).convert("RGB")
            calibration = json.loads(Path(args.calib).read_text()) if args.calib else None
        else:
            images, status, metadata = read_live(bridge)
            calibration = status if status.get("calibrated") else None
        ts.VLM_BACKEND = args.vlm
        ts.PX_PER_CM = None
        profile = gp.load_profile(args.items, parsed["target"])
        workspace = json.loads(Path(args.workspace).read_text()) if getattr(args, "workspace", None) else None
        validation = json.loads(Path(args.validation).read_text()) if getattr(args, "validation", None) else None
        for role, img in images.items():
            print(f"Checking {role} camera: {parsed['target']}", flush=True)
            img.save(root / f"{role}_input.jpg")
            result = ts.decide(img, parsed["target"], args.category)
            result = select_candidates(result, img.size, workspace if role == "stand" else None,
                                       metadata.get(role, {}).get("camera_id") if role == "stand" else None,
                                       getattr(args, "pick_at", None) if role == "stand" else None)
            results[role] = result
            view = plan_selection(img, result, profile, calibration=calibration if role == "stand" else None,
                                  vlm=args.vlm, method=args.grasp, height=args.height, above=args.above)
            view["target"] = parsed["target"]
            render_view(img, result, view, root / f"{role}_preview.jpg")
            if result["status"] == "GO":
                Image.fromarray(np.asarray(result["chosen"]["mask"], np.uint8) * 255).save(root / f"{role}_mask.png")
            if role == "stand":
                report.update(view)
            else:
                report["hand_view"] = view
        report["source"] = metadata or {role: {"image_size": list(im.size)} for role, im in images.items()}
        report["two_view_geometry"] = "Independent semantic checks only; no stereo/depth or instance correspondence established"
        blocks = report["execution_blocks"]
        if "hand" not in results or results["hand"]["status"] != "GO":
            blocks.append("Hand camera must also see a unique matching object before live pickup")
        if args.image:
            blocks.append("Saved-image preview cannot command motion")
        else:
            if status.get("read_only"):
                blocks.append("Controller is running in read-only mode")
            if workspace is None:
                blocks.append("Configure a standing-camera workspace before live pickup")
            evidence = calibration_validation.validate(validation, status, status.get("table_calibration"),
                        images["stand"].size, metadata["stand"]["camera_id"],
                        report.get("footprint", {}).get("target_px"))
            report["calibration_validation"] = evidence
            blocks.extend(evidence["errors"])
        if status.get("busy") or status.get("pending") is not None:
            blocks.append("Controller is busy or waiting for calibration")
        report["planning_checks_passed"] = not blocks
        save_report(root, report)  # Save the concrete plan before any motion attempt.
        if args.show:
            pick.show(str(root / "stand_preview.jpg"))
        if args.go:
            try:
                execute_live(report, images, results, status, metadata, bridge)
            except Exception as exc:
                report.update(status="MOTION_RESULT_UNKNOWN" if report["motion_attempted"] else "EXECUTION_BLOCKED",
                              error=f"{type(exc).__name__}: {exc}")
                # A timeout can happen after movement: never automatically retry.
    except Exception as exc:
        report.update(status="CHECK_FAILED", error=f"{type(exc).__name__}: {exc}",
                      execution_blocks=["Perception, input or planning failed"])
    save_report(root, report)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text", help='e.g. "pick up the blue pill box"; omit to type interactively')
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="saved standing-camera frame")
    source.add_argument("--arm-url", help="integrated controller URL on the arm laptop")
    ap.add_argument("--hand-image", help="saved hand-camera frame of the same stationary scene")
    ap.add_argument("--calib", help="measured stand-camera calibration for saved-image planning")
    ap.add_argument("--workspace", help="stand workspace JSON created with configure_workspace.py")
    ap.add_argument("--validation", help="independent measured camera/table checks bound to the active geometry")
    ap.add_argument("--pick-at", nargs=2, type=float, metavar=("U", "V"),
                    help="explicitly disambiguate at a pixel of the current stand image; never overrides quality checks")
    ap.add_argument("--category", help="optional broad SAM prompt, e.g. box")
    ap.add_argument("--vlm", choices=("none", "ollama", "claude"), default="none",
                    help="optional identity checker; default is SAM 3 only, with no model service")
    ap.add_argument("--items", default=str(HERE / "items_v1.json"))
    ap.add_argument("--grasp", choices=("auto", "top", "side"), default="auto")
    ap.add_argument("--height", type=float, help="measured closing height in cm")
    ap.add_argument("--above", type=float, help="approach and lift height in cm")
    ap.add_argument("--go", action="store_true", help="request one live pickup if all checks pass")
    ap.add_argument("--show", action="store_true", help="open the standing-camera result")
    ap.add_argument("--out", default=str(HERE / "review_outputs" / "text_pick"))
    args = ap.parse_args(argv)
    if args.go and args.image:
        ap.error("--go requires --arm-url; saved images never command motion")
    if args.arm_url and (args.hand_image or args.calib):
        ap.error("Live mode uses both camera frames and calibration from the controller")
    for name in ("height", "above"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            ap.error(f"--{name} must be finite and positive")
    if args.text is None:
        args.text = input("What should the robot pick up? ")
    report = run(args)
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["status"] in ("PREVIEW", "HOLDING_REPORTED", "CANCELLED") else 2


if __name__ == "__main__":
    raise SystemExit(main())
