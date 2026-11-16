"""
grasp_planner_v3.py — find the best top-down grasp for an object from its SAM 3 mask.

For a parallel-jaw gripper the mask (the object's outline seen from above) tells us:
  - WHERE to put the gripper center
  - which ANGLE to rotate the gripper so the jaws close across the object
  - how WIDE the object is there (must be less than the gripper's max opening)

How it works
  1. Get the target's mask from target_selector_v5 (SAM 3 + optional checker).
  2. Candidate centers: the mask centroid (balance point) + the points deepest inside
     the mask (distance transform). Deep points matter for shapes like a ring, where
     the centroid lies in the hole.
  3. For each center and each angle (every 10 deg), shoot a line through the center and
     measure where it leaves the mask on both sides = where the two jaws would touch.
  4. Score each grasp:  narrow is good (easy to hold, fits the gripper),
                        near the centroid is good (balanced, won't tip),
                        centered between the jaws is good (both jaws touch together),
                        flat contact is good (jaws sit parallel to the object's sides),
                        too wide for the gripper = rejected.
  5. Draw the best grasps and print the winner.

Run
  python grasp_planner_v3.py --image top.jpeg --target "blue pill box" --category "box" --vlm none
  python grasp_planner_v3.py --image top.jpeg --target "energy drink can" --category "can" --px-per-cm 12 --max-open-cm 6

Best results with a camera looking DOWN at the table (like your stand camera). From a
side/oblique view the mask includes the object's sides, so the grasp is only approximate.
"""
import argparse, json, math
import numpy as np
import cv2
from PIL import Image

ANGLE_STEP = 10           # degrees
N_DEEP_POINTS = 6         # how many "deep inside" candidate centers to try
W_WIDTH, W_CENTER, W_SYM, W_FLAT = 1.0, 0.6, 0.4, 1.0   # scoring weights (lower = better)


def jaw_contacts(mask, c, theta):
    """Walk from center c along +/- direction theta until leaving the mask.
    Returns (p1, p2, width_px) or None if c is not inside the mask."""
    h, w = mask.shape
    cx, cy = c
    if not mask[int(cy), int(cx)]:
        return None
    dx, dy = math.cos(theta), math.sin(theta)
    ends = []
    for sgn in (1, -1):
        t = 0.0
        while True:
            x, y = cx + sgn * t * dx, cy + sgn * t * dy
            if not (0 <= x < w and 0 <= y < h) or not mask[int(y), int(x)]:
                break
            t += 0.25
        ends.append(t)
    p1 = (cx + ends[0] * dx, cy + ends[0] * dy)
    p2 = (cx - ends[1] * dx, cy - ends[1] * dy)
    return p1, p2, ends[0] + ends[1], ends


def candidate_centers(mask):
    m8 = mask.astype(np.uint8)
    ys, xs = np.nonzero(m8)
    centroid = (float(xs.mean()), float(ys.mean()))
    dist = cv2.distanceTransform(m8, cv2.DIST_L2, 5)
    centers = [centroid]
    d = dist.copy()
    sep = max(5, int(0.15 * math.sqrt(m8.sum())))    # keep candidate points spread out
    for _ in range(N_DEEP_POINTS):
        y, x = np.unravel_index(np.argmax(d), d.shape)
        if d[y, x] <= 0:
            break
        centers.append((float(x), float(y)))
        cv2.circle(d, (int(x), int(y)), sep, 0, -1)
    return centroid, centers


def plan_grasp(mask, max_open_px=None, top_k=3, candidate_filter=None):
    mask = mask.astype(bool)
    if mask.sum() < 20:
        return None, []
    centroid, centers = candidate_centers(mask)
    size = math.sqrt(mask.sum())                     # typical object size in px
    grasps = []
    for c in centers:
        for deg in range(0, 180, ANGLE_STEP):
            r = jaw_contacts(mask, c, math.radians(deg))
            if r is None:
                continue
            p1, p2, width, (a, b) = r
            if candidate_filter is not None and not candidate_filter(p1, p2):
                continue
            if width < 3:
                continue
            if max_open_px and width > 0.9 * max_open_px:
                continue                             # won't fit in the gripper
            mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)   # actual grasp center
            off_center = math.dist(mid, centroid) / size
            asym = abs(a - b) / max(width, 1)
            # flat contact: measure the width a bit to each side along the jaw faces;
            # if the jaws sit flat on the object, those widths are the same
            s = min(0.15 * size, 15)
            th = math.radians(deg)
            side = []
            for k in (-s, s):
                c2 = (mid[0] - k * math.sin(th), mid[1] + k * math.cos(th))
                if 0 <= c2[0] < mask.shape[1] and 0 <= c2[1] < mask.shape[0]:
                    r2 = jaw_contacts(mask, c2, th)
                    side.append(r2[2] if r2 else 0.0)
            flat = (max(abs(x - width) for x in side) / max(width, 1)) if side else 1.0
            score = (W_WIDTH * width / size + W_CENTER * off_center + W_SYM * asym
                     + W_FLAT * flat)
            grasps.append({"center_px": [round(mid[0]), round(mid[1])],
                           "jaw1_px": [round(p1[0]), round(p1[1])],
                           "jaw2_px": [round(p2[0]), round(p2[1])],
                           "closing_angle_deg": deg,   # direction the jaws close, image frame
                           "width_px": round(width, 1),
                           "score": round(score, 3)})
    grasps.sort(key=lambda g: g["score"])
    # keep top_k that are meaningfully different
    best = []
    for g in grasps:
        if all(abs(g["closing_angle_deg"] - b["closing_angle_deg"]) >= 20 or
               math.dist(g["center_px"], b["center_px"]) > 0.2 * size for b in best):
            best.append(g)
        if len(best) == top_k:
            break
    return (best[0] if best else None), best


def draw(img_rgb, mask, best_list, out):
    vis = cv2.cvtColor(np.array(img_rgb), cv2.COLOR_RGB2BGR)
    overlay = vis.copy()
    overlay[mask.astype(bool)] = (255, 180, 0)
    vis = cv2.addWeighted(overlay, 0.35, vis, 0.65, 0)
    cnts, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(vis, cnts, -1, (255, 180, 0), 2)
    for i, g in enumerate(reversed(best_list)):
        rank = len(best_list) - i
        col = (0, 220, 0) if rank == 1 else (0, 200, 255)
        p1, p2, c = map(tuple, (g["jaw1_px"], g["jaw2_px"], g["center_px"]))
        cv2.line(vis, p1, p2, col, 3 if rank == 1 else 1)
        for p in (p1, p2):                           # draw jaws perpendicular to closing line
            th = math.radians(g["closing_angle_deg"] + 90)
            q1 = (int(p[0] + 15 * math.cos(th)), int(p[1] + 15 * math.sin(th)))
            q2 = (int(p[0] - 15 * math.cos(th)), int(p[1] - 15 * math.sin(th)))
            cv2.line(vis, q1, q2, col, 4 if rank == 1 else 2)
        cv2.circle(vis, c, 6, col, -1)
        cv2.putText(vis, f"#{rank} w={g['width_px']:.0f}px {g['closing_angle_deg']}deg",
                    (c[0] + 8, c[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 2)
    cv2.imwrite(out, vis)


# ---------------------------------------------------------------------------
# v3: choose the grasp TYPE first (top-down or from the side), then the grasp points
# ---------------------------------------------------------------------------
POSTURE_PROMPT = (
    "Look at the main object in this image. Is it STANDING UPRIGHT and taller than it is "
    "wide (like a standing can, bottle or cup), or is it FLAT / LYING DOWN (like a box, "
    "a book, a remote, or a can on its side)?\n"
    'Reply with JSON only: {"posture": "upright" or "flat", "reason": "short"}'
)


def detect_posture(ts, img, cand, vlm):
    """upright or flat. Uses the local checker if available, else the mask's shape."""
    if vlm == "ollama":
        try:
            r = ts.ollama_check(ts.crop(img, cand["box"]), POSTURE_PROMPT)
            if r.get("posture") in ("upright", "flat"):
                return r["posture"], f'checker: {r.get("reason", "")}'
        except Exception as e:
            print(f"posture check failed ({e}); using mask shape instead")
    ys, xs = np.nonzero(cand["mask"])
    h, w = ys.max() - ys.min() + 1, xs.max() - xs.min() + 1
    posture = "upright" if h > 1.3 * w else "flat"
    return posture, f"mask is {h}px tall x {w}px wide in the image (rough guess)"


def choose_grasp_type(profile, posture, how, forced, finger_cm):
    if forced != "auto":
        return forced, "forced with --grasp"
    pref = profile.get("grasp", "auto")
    h, w = profile.get("height_cm"), profile.get("width_cm")
    if pref == "side" and posture == "flat":
        return "top", f"item profile says side, but the object is lying down ({how})"
    if pref in ("top", "side"):
        return pref, "item profile"
    if posture == "upright":
        if h is not None and h <= finger_cm:
            return "top", f"upright but short ({h} cm <= finger length {finger_cm} cm)"
        return "side", f"standing upright ({how})"
    return "top", f"flat / low ({how})"


def plan_side_grasp(mask, height_frac=0.5):
    """Side grasp on an upright object seen from the side / at an angle.
    contact_px = where the object touches the table (bottom of the mask). Use THIS pixel,
    not the mask center, to get the table position: only table-level pixels map correctly."""
    mask = mask.astype(bool)
    ys, xs = np.nonzero(mask)
    y_top, y_bot = int(ys.min()), int(ys.max())
    h = y_bot - y_top + 1
    base = ys >= y_bot - max(2, int(0.05 * h))
    contact = [int(round(xs[base].mean())), y_bot]
    row = int(round(y_bot - height_frac * h))
    band = (ys >= row - 2) & (ys <= row + 2)
    if not band.any():
        return None
    left, right = int(xs[band].min()), int(xs[band].max())
    return {"contact_px": contact, "grasp_row_px": row,
            "jaw_left_px": [left, row], "jaw_right_px": [right, row],
            "width_px": right - left + 1, "mask_height_px": h,
            "height_frac": height_frac}


def draw_side(img_rgb, mask, g, out):
    vis = cv2.cvtColor(np.array(img_rgb), cv2.COLOR_RGB2BGR)
    overlay = vis.copy()
    overlay[mask.astype(bool)] = (255, 180, 0)
    vis = cv2.addWeighted(overlay, 0.35, vis, 0.65, 0)
    col = (0, 220, 0)
    p1, p2 = tuple(g["jaw_left_px"]), tuple(g["jaw_right_px"])
    cv2.line(vis, p1, p2, col, 3)
    for p in (p1, p2):                                   # jaws: vertical bars at each side
        cv2.line(vis, (p[0], p[1] - 22), (p[0], p[1] + 22), col, 5)
    c = tuple(g["contact_px"])
    cv2.circle(vis, c, 7, (255, 255, 255), -1)
    cv2.circle(vis, c, 7, (0, 0, 0), 2)
    cv2.putText(vis, "table contact", (c[0] + 10, c[1] + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (255, 255, 255), 2)
    cv2.putText(vis, f"SIDE grasp  w={g['width_px']}px", (p1[0], p1[1] - 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 2)
    cv2.imwrite(out, vis)


def load_profile(path, target):
    try:
        with open(path) as file:
            items = json.load(file)
    except FileNotFoundError:
        return {}
    for name, prof in items.items():
        if name.lower() == target.lower():
            return prof
    return {}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--category")
    ap.add_argument("--vlm", choices=["none", "ollama", "claude"], default="none")
    ap.add_argument("--items", default="items_v1.json", help="per-item sizes and grasp type")
    ap.add_argument("--grasp", choices=["auto", "top", "side"], default="auto")
    ap.add_argument("--px-per-cm", type=float, help="image scale at the table (top-down view)")
    ap.add_argument("--max-open-cm", type=float, default=6.0, help="gripper's max opening")
    ap.add_argument("--finger-cm", type=float, default=5.0, help="usable finger length")
    ap.add_argument("--out", default="grasp.jpg")
    a = ap.parse_args()

    import target_selector_v5 as ts
    ts.VLM_BACKEND = a.vlm
    img = Image.open(a.image).convert("RGB")
    res = ts.decide(img, a.target, a.category)
    if res["status"] != "GO":
        print(json.dumps({"status": res["status"], "grasp": None}, indent=2))
        raise SystemExit
    cand = res["chosen"]
    mask = cand["mask"]
    profile = load_profile(a.items, a.target)
    posture, how = detect_posture(ts, img, cand, a.vlm)
    gtype, reason = choose_grasp_type(profile, posture, how, a.grasp, a.finger_cm)

    out = {"status": "GO", "grasp_type": gtype, "reason": reason, "posture": posture,
           "item_profile": profile or "none (add it to " + a.items + ")"}
    w_cm = profile.get("width_cm")
    if w_cm and w_cm > 0.9 * a.max_open_cm:
        out.update(status="NO_GRASP", reason=f"item is {w_cm} cm wide, gripper opens {a.max_open_cm} cm")
        print(json.dumps(out, indent=2)); raise SystemExit

    if gtype == "side":
        g = plan_side_grasp(mask, profile.get("grasp_height_frac", 0.5))
        if g is None:
            out.update(status="NO_GRASP", reason="could not measure the object at grasp height")
            print(json.dumps(out, indent=2)); raise SystemExit
        if profile.get("height_cm"):
            g["grasp_height_cm"] = round(profile["height_cm"] * g["height_frac"], 1)
        g["approach"] = "horizontal, along the line from the arm base to the object"
        out["side_grasp"] = g
        draw_side(img, mask, g, a.out)
    else:
        max_open_px = a.max_open_cm * a.px_per_cm if a.px_per_cm else None
        best, top = plan_grasp(mask, max_open_px)
        if best is None:
            out.update(status="NO_GRASP", reason="too wide for the gripper, or mask too small")
            print(json.dumps(out, indent=2)); raise SystemExit
        if a.px_per_cm:
            for g in top:
                g["width_cm"] = round(g["width_px"] / a.px_per_cm, 1)
        if profile.get("height_cm"):
            best["descend_to_cm"] = round(max(1.0, profile["height_cm"] - a.finger_cm * 0.8), 1)
        best["approach"] = "from above, gripper pointing down"
        out["top_grasp"] = best
        out["alternatives"] = top[1:]
        draw(img, mask, top, a.out)
    print(json.dumps(out, indent=2))
    print(f"saved {a.out}")
