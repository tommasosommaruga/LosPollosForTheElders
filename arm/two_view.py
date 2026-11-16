"""Offline two-camera inspection. No controller imports, requests, or motion commands.

Semantic agreement and feature matches are evidence, not calibrated 3D coordinates.
Use original, unannotated images from the same stationary scene.
"""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import requests

import target_selector_v5 as ts


def feature_matches(stand, hand, stand_mask, hand_mask):
    """Mutual SIFT matches inside masks, then a robust epipolar consistency check.

    Uncalibrated matching cannot establish metric scale or certify object identity.
    """
    sift = cv2.SIFT_create(nfeatures=2500)
    points, descriptors = [], []
    for img, mask in ((stand, stand_mask), (hand, hand_mask)):
        gray = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
        # Avoid features on silhouette boundaries, which change across viewpoints.
        interior = cv2.erode(mask.astype(np.uint8), np.ones((5, 5), np.uint8)) * 255
        kp, desc = sift.detectAndCompute(gray, interior)
        points.append(kp)
        descriptors.append(desc)
    result = {"status": "INSUFFICIENT_FEATURES", "mutual_matches": 0,
              "inliers": 0, "stand_points_px": [], "hand_points_px": [],
              "metric_3d_available": False}
    if any(d is None or len(d) < 2 for d in descriptors):
        return result
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    def ratio_matches(a, b):
        return {pair[0].queryIdx: pair[0].trainIdx
                for pair in matcher.knnMatch(a, b, k=2)
                if len(pair) == 2 and pair[0].distance < 0.7 * pair[1].distance}
    forward = ratio_matches(*descriptors)
    backward = ratio_matches(*descriptors[::-1])
    pairs = [(a, b) for a, b in forward.items() if backward.get(b) == a]
    result["mutual_matches"] = len(pairs)
    if len(pairs) < 12:
        return result
    a = np.float32([points[0][i].pt for i, _ in pairs])
    b = np.float32([points[1][j].pt for _, j in pairs])
    F, keep = cv2.findFundamentalMat(a, b, cv2.FM_RANSAC, 1.5, 0.995)
    if F is None or F.shape != (3, 3) or not np.isfinite(F).all() or keep is None:
        result["status"] = "GEOMETRY_UNCERTAIN"
        return result
    keep = keep.ravel().astype(bool)
    count = int(keep.sum())
    result.update(inliers=count, stand_points_px=a[keep].tolist(), hand_points_px=b[keep].tolist())
    result["status"] = "TENTATIVE_CORRESPONDENCES" if count >= 12 and count / len(pairs) >= .6 else "GEOMETRY_UNCERTAIN"
    return result


def pair_status(stand, hand):
    if stand["status"] == "GO" and hand["status"] == "GO":
        return "TARGET_IN_BOTH_VIEWS"
    return "NEEDS_REVIEW"


def summarize_joint_reviews(reviews):
    positives = sum(r["assessment"].get("match") is True
                    and r["assessment"].get("confidence", 0) >= ts.VLM_CONF_MIN
                    and not r["assessment"].get("error") for r in reviews)
    status = "NO_CORROBORATION"
    if any(r["assessment"].get("error") for r in reviews):
        status = "CHECK_FAILED"
    elif positives > 1:
        status = "AMBIGUOUS_CORROBORATION"
    elif positives == 1:
        status = "TENTATIVE_CORROBORATION"
    return {"purpose": "semantic evidence only; independent decisions remain unchanged",
            "status": status, "positive_pairs": positives,
            "physical_instance_verified": False, "pairs": reviews}


def joint_review(images, results, target):
    """Ask about both views with context; preserve independent selection decisions.

    This is a semantic review of at most three candidates per view, not geometric
    data association. It must never upgrade motion permission or certify 3D.
    """
    views = []
    for result in results:
        if result["chosen"] is not None:
            views.append([result["chosen"]])
        else:
            views.append(sorted(result["candidates"], key=lambda c: -c["sam_score"])[:3])
    reviews = []
    from PIL import ImageDraw
    for stand_cand in views[0]:
        for hand_cand in views[1]:
            encoded = []
            for img, cand in zip(images, (stand_cand, hand_cand)):
                context = img.copy()
                ImageDraw.Draw(context).rectangle(cand["box"], outline="red", width=5)
                context.thumbnail((1024, 768))
                detail = ts.crop(img, cand["box"])
                detail.thumbnail((640, 640))
                encoded.extend([ts.to_b64(context), ts.to_b64(detail)])
            prompt = (f'Target: {target!r}. Images 1 and 2 are a standing-camera scene with a red '
                      'candidate box and its crop. Images 3 and 4 are a hand-camera scene and crop. '
                      'Use surrounding objects for scale. Do BOTH boxed candidates match the requested '
                      'object description? Do not assume they are the same physical instance. '
                      'If either is unclear or inconsistent, match=false. Explain any view disagreement. '
                      'Return JSON: {"match": boolean, "confidence": number from 0 to 1, "reason": string}.')
            try:
                response = requests.post(ts.OLLAMA_URL, timeout=120, json={
                    "model": ts.OLLAMA_MODEL, "stream": False, "format": ts.MATCH_SCHEMA,
                    "options": {"temperature": 0},
                    "messages": [{"role": "user", "content": prompt, "images": encoded}]})
                response.raise_for_status()
                review = ts.validate_match(ts.parse_json(response.json()["message"]["content"]))
            except (requests.RequestException, KeyError, ValueError) as exc:
                review = {"match": False, "confidence": 0.0, "error": True, "reason": str(exc)}
            reviews.append({"stand_box": stand_cand["box"], "hand_box": hand_cand["box"],
                            "assessment": review})
    return summarize_joint_reviews(reviews)


def analyze(stand_path, hand_path, target, category, output_dir, vlm="none"):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    ts.VLM_BACKEND = vlm
    images = [Image.open(p).convert("RGB") for p in (stand_path, hand_path)]
    results = []
    for name, img in zip(("stand", "hand"), images):
        res = ts.decide(img, target, category)
        ts.draw_sam3_style(img, res, str(output / f"{name}.jpg"), show_all=True)
        results.append(res)
        print(f"{name}: {res['status']}", flush=True)
        if res["chosen"] is not None:
            Image.fromarray(res["chosen"]["mask"].astype(np.uint8) * 255).save(output / f"{name}_mask.png")
    report = {"target": target, "backend": vlm, "status": pair_status(*results),
              "motion_allowed": False, "metric_3d_available": False,
              "inputs": {"stand": str(Path(stand_path).resolve()), "hand": str(Path(hand_path).resolve())},
              "stand": ts.summary(results[0]), "hand": ts.summary(results[1]),
              "limitations": ["Unique semantic matches do not prove physical instance correspondence.",
                              "Capture synchronization and camera poses are unknown.",
                              "No metric position, object height, or executable grasp is estimated."]}
    if report["status"] == "TARGET_IN_BOTH_VIEWS":
        report["correspondences"] = feature_matches(*images, results[0]["chosen"]["mask"], results[1]["chosen"]["mask"])
    if vlm == "ollama" and any(r["chosen"] is not None for r in results):
        report["joint_review"] = joint_review(images, results, target)
    # Side-by-side inspection image; all reported pixel coordinates refer to originals.
    panels = []
    for name in ("stand", "hand"):
        panel = Image.open(output / f"{name}.jpg").convert("RGB")
        panel.thumbnail((960, 540))
        panels.append(panel)
    canvas = Image.new("RGB", (sum(p.width for p in panels), max(p.height for p in panels) + 48), "black")
    x = 0
    for panel in panels:
        canvas.paste(panel, (x, 48))
        x += panel.width
    from PIL import ImageDraw
    ImageDraw.Draw(canvas).text((12, 12), "STAND (left) | HAND (right) - recognition preview; 3D uncalibrated; no motion", fill="white")
    canvas.save(output / "comparison.jpg")
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stand", required=True)
    ap.add_argument("--hand", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--category")
    ap.add_argument("--vlm", choices=["ollama", "none"], default="none")
    ap.add_argument("--vlm-model", default=ts.OLLAMA_MODEL)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    ts.OLLAMA_MODEL = args.vlm_model
    report = analyze(args.stand, args.hand, args.target, args.category, args.out, args.vlm)
    print(json.dumps({k: report[k] for k in ("status", "motion_allowed", "metric_3d_available")}, indent=2))


if __name__ == "__main__":
    main()
