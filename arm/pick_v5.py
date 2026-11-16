"""
pick_v5.py — SAM 3 finds the object you ask for, the arm grabs it.

It replaces the human click in your friend's "click to grab" page (arm/click.py):
    his page:   human clicks a pixel          -> pixel -> table x,y -> arm grabs
    this file:  SAM 3 + checker pick a pixel  -> pixel -> table x,y -> arm grabs
Nothing in his code changes. This script talks to his page over HTTP:
    GET  /status      calibration marks, busy flag
    GET  /frame.jpg   the camera image (the SAME image his calibration was made on)
    POST /api/grab    {u, v, height, above}  -> his Reacher.grab() does the motion

New in v4: the red target is the GRASP POINT, chosen from the mask's shape
  - upright items (side grasp): the row of the object where the jaws should close. It looks at the
    object's width at every height and picks a straight, fitting, well-balanced section (not the
    cap, not a tapering neck, not the very bottom).
  - flat / low items (top grasp): grasp_planner_v3's best center and closing angle.
  - the table x, y sent to the arm is the footprint under that point; the height is printed too.
  - the real object width is measured from the mask and checked against the gripper opening.

Steps
  1. Get the camera frame from his page (or --image for an offline test).
  2. target_selector_v5: SAM 3 (+ checker) -> GO / NOT_FOUND / AMBIGUOUS.
  3. Choose the pixel where the arm should go:
       contact point  = where the object touches the table (bottom of its mask).
                        Only table-level pixels map to correct table coordinates.
       target point   = contact point pushed away from the camera by half the
                        object's width (from items_v1.json), i.e. the middle of its base.
  4. Pixel -> table x, y in cm, with the same calibration marks his page uses.
  5. Dry run by default: prints x, y and saves pick.jpg.  With --go: the arm grabs.

Run (his page must be running:  python -m arm.click --camera ...  ->  http://localhost:8890)
  python pick_v5.py --target "energy drink can" --category can --vlm none          # dry run
  python pick_v5.py --target "energy drink can" --category can --vlm none --go     # grab

How accurate is the calibration? (no SAM 3, no motion)
  python pick_v5.py --check
  python pick_v5.py --check --calib /path/to/LosPollosForTheElders/calib/camera.json

Grab, then put it down at a fixed table point (needs the 'place' action, see place_snippet_v1.py):
  python pick_v5.py --target "blue pill box" --category box --vlm none --go --place -10 25

Offline test, no arm (uses his calibration file and a saved frame from the same camera):
  python pick_v5.py --image frame.jpg --calib /path/to/LosPollosForTheElders/calib/camera.json \
                    --target "energy drink can" --category can --vlm none

Needs in the same folder: target_selector_v5.py, grasp_planner_v3.py, items_v1.json
"""
import argparse, io, json, math, subprocess, sys
import numpy as np
import cv2
import requests
from PIL import Image

import target_selector_v5 as ts
import grasp_planner_v3 as gp
from arm_bridge import ArmBridge, grasp_request, request_errors, GRIPPER_OPEN_CM, JAW_MARGIN_CM

DEFAULT_PUSH_CM = 2.5      # contact point -> middle of the base, when the item's width is unknown
DEFAULT_CLOSE_CM = 4.0     # his defaults: close 4 cm above the table, approach from 12 cm
DEFAULT_ABOVE_CM = 12.0
CLEARANCE_CM = 5.0         # approach this far above the object's top


# ---------- calibration: pixel <-> table (same maths as arm/click.py) ----------

def _fit_h(pts, xy):
    """Least-squares homography: normalised pixel (u, v) -> table (x, y) cm."""
    rows, rhs = [], []
    for (u, v), (x, y) in zip(pts, xy):
        rows.append([u, v, 1, 0, 0, 0, -x * u, -x * v]); rhs.append(x)
        rows.append([0, 0, 0, u, v, 1, -y * u, -y * v]); rhs.append(y)
    h = np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=None)[0]
    return np.append(h, 1.0).reshape(3, 3)


def _apply_h(H, u, v):
    p = H @ np.array([u, v, 1.0])
    return np.array([p[0] / p[2], p[1] / p[2]])


class Calib:
    """pairs = [u, v, x_cm, y_cm] marks; size = width of the image they were clicked on.

    Two models are fitted from the same marks:
      plain  : pixel -> table with a homography only. This is what arm/click.py uses.
      lens   : first straighten the wide-angle lens (one curvature number k, fitted from the
               marks), then the homography. OFF by default: in simulation it did not help when
               the pick area is in the middle of the image; evenly spread, careful marks did.
               Try --lens only if 8+ careful marks still leave errors above 1 cm.
    We compute x, y with the better model, then hand his page the pixel that HIS plain
    model maps to exactly that x, y (server_pixel), so his code needs no change.
    """

    MIN_MARKS_FOR_LENS = 8

    def __init__(self, pairs, size, aspect=9 / 16, lens=False):
        if len(pairs) < 4:
            raise RuntimeError("Camera not calibrated: needs at least 4 marks (do it on his page).")
        self.pairs, self.size = [list(p) for p in pairs], float(size)
        self.c = np.array([0.5, 0.5 * aspect])                       # image centre, normalised
        self.px = [np.array([u, v]) / self.size for u, v, _, _ in self.pairs]
        self.xy = [np.array([x, y]) for _, _, x, y in self.pairs]
        self.H_plain = _fit_h(self.px, self.xy)
        self.Hinv_plain = np.linalg.inv(self.H_plain)
        self.k = 0.0
        self.loo_plain = self._loo(0.0)
        self.lens = lens
        if lens and len(self.pairs) >= self.MIN_MARKS_FOR_LENS:
            ks = np.linspace(-1.5, 1.5, 151)
            errs = [self._rms(self._loo(k)) for k in ks]
            best = int(np.argmin(errs))
            if errs[best] < 0.8 * self._rms(self.loo_plain):          # only if clearly better
                self.k = float(ks[best])
        self.H = _fit_h([self._undist(p, self.k) for p in self.px], self.xy)
        self.Hinv = np.linalg.inv(self.H)
        self.loo = self._loo(self.k)

    @staticmethod
    def _rms(e):
        return float(np.sqrt(np.mean(np.square(e)))) if len(e) else float("nan")

    def _undist(self, p, k):
        d = p - self.c
        return self.c + d / (1.0 + k * float(d @ d))

    def _dist(self, q, k):
        """Inverse of _undist (a few fixed-point steps)."""
        p = q.copy()
        for _ in range(15):
            d = p - self.c
            p = self.c + (q - self.c) * (1.0 + k * float(d @ d))
        return p

    def _loo(self, k):
        """Predict each mark from the OTHER marks: honest accuracy. Needs 6+ marks."""
        n = len(self.pairs)
        if n < 6:
            return []
        und = [self._undist(p, k) for p in self.px]
        out = []
        for i in range(n):
            H = _fit_h(und[:i] + und[i + 1:], self.xy[:i] + self.xy[i + 1:])
            out.append(float(np.linalg.norm(_apply_h(H, *und[i]) - self.xy[i])))
        return out

    def to_table(self, u, v):
        q = self._undist(np.array([u, v]) / self.size, self.k)
        x, y = _apply_h(self.H, *q)
        return float(x), float(y)

    def to_pixel(self, x, y):
        """Table point -> pixel in the real (curved) image. For drawing."""
        q = _apply_h(self.Hinv, x, y)
        p = self._dist(q, self.k) * self.size
        return float(p[0]), float(p[1])

    def server_pixel(self, x, y):
        """The pixel his page's plain model maps to exactly (x, y)."""
        p = _apply_h(self.Hinv_plain, x, y) * self.size
        return float(p[0]), float(p[1])

    def fit_errors(self):
        return [round(float(np.linalg.norm(np.array(self.to_table(u, v)) - [x, y])), 1)
                for u, v, x, y in self.pairs]

    def expected_error(self):
        """(cm, how it was measured)"""
        if self.loo:
            return max(self.loo), "predicting each mark from the others"
        return max(self.fit_errors()), "on the marks themselves; too few marks to measure it properly"

    def inside_marks(self, u, v):
        hull = cv2.convexHull(np.array([[p[0], p[1]] for p in self.pairs], np.float32))
        return cv2.pointPolygonTest(hull, (float(u), float(v)), False) >= 0

    def report(self):
        acc, how = self.expected_error()
        lines = [f"Calibration: {len(self.pairs)} marks. Worst error {acc:.1f} cm ({how})."]
        if self.k:
            lines.append(f"  Lens correction ON (k={self.k:+.2f}): typical error "
                         f"{self._rms(self.loo_plain):.1f} cm -> {self._rms(self.loo):.1f} cm.")
        elif self.lens and len(self.pairs) < self.MIN_MARKS_FOR_LENS:
            lines.append(f"  Lens correction needs {self.MIN_MARKS_FOR_LENS}+ marks; using the plain model.")
        elif self.lens:
            lines.append("  Lens correction did not help; using the plain model.")
        fit = self.fit_errors()
        for i, (u, v, x, y) in enumerate(self.pairs):
            loo = f"   unseen: {self.loo[i]:.1f} cm" if self.loo else ""
            lines.append(f"  mark {i + 1}: x={x:6.1f} y={y:6.1f}   fit: {fit[i]:.1f} cm{loo}")
        if acc > 1.0:
            lines.append("  -> too loose for grasping (aim for under 1 cm). Redo the worst marks carefully and "
                         "spread 8+ marks evenly over the pick area (corners, edges, middle).")
        return "\n".join(lines)


# ---------- which pixel should the arm go to ----------

def contact_pixel(mask):
    """Where the object touches the table: lowest mask pixel under the mask's middle column."""
    ys, xs = np.nonzero(mask)
    u_mid = int(round((xs.min() + xs.max()) / 2))
    col = np.abs(xs - u_mid) <= 2
    if not col.any():
        col = np.ones_like(xs, bool)
    return float(u_mid), float(ys[col].max())


def choose_target(mask, calib, scale, push_cm, mode):
    """Returns dict with contact/target in frame pixels and table cm.
    scale = calibration image width / frame width (1.0 when they are the same image)."""
    if mode == "center":
        ys, xs = np.nonzero(mask)
        u, v = float(xs.mean()), float(ys.mean())
        x, y = calib.to_table(u * scale, v * scale)
        return {"mode": "mask center", "contact_px": None, "target_px": [u, v], "x_cm": x, "y_cm": y}
    cu, cv_ = contact_pixel(mask)
    c = np.array(calib.to_table(cu * scale, cv_ * scale))
    # a table point a few pixels further "up" the image = further from the camera
    f = np.array(calib.to_table(cu * scale, (cv_ - 5) * scale))
    away = (f - c) / (np.linalg.norm(f - c) + 1e-9)
    t = c + away * push_cm
    tu, tv = calib.to_pixel(*t)
    return {"mode": f"table contact + {push_cm:.1f} cm away from the camera",
            "contact_px": [cu, cv_], "contact_cm": [float(c[0]), float(c[1])],
            "target_px": [tu / scale, tv / scale], "x_cm": float(t[0]), "y_cm": float(t[1])}


def grab_heights(profile, close_cm, above_cm):
    h = profile.get("height_cm")
    if close_cm is None:
        close_cm = max(1.0, h / 2) if h else DEFAULT_CLOSE_CM
    if above_cm is None:
        above_cm = max(DEFAULT_ABOVE_CM, h + CLEARANCE_CM) if h else DEFAULT_ABOVE_CM
    return round(close_cm, 1), round(above_cm, 1)


def calibration_blocks(calib, target_pixel, scale, max_error_cm=1.0):
    """Reasons to refuse motion; dry-run geometry remains available for inspection."""
    errors = []
    if len(calib.pairs) < 6:
        errors.append("at least six calibration marks are required for validation")
    accuracy, _ = calib.expected_error()
    if not math.isfinite(accuracy) or accuracy > max_error_cm:
        errors.append(f"calibration error exceeds {max_error_cm:g} cm")
    if not calib.inside_marks(target_pixel[0] * scale, target_pixel[1] * scale):
        errors.append("target lies outside the calibrated area")
    return errors


# ---------- where exactly to grasp (from the mask's shape) ----------

def local_cm_per_px(calib, u, v, scale, d=10.0):
    """Table cm per image pixel (sideways) at the table point under pixel (u, v)."""
    a = np.array(calib.to_table((u - d) * scale, v * scale))
    b = np.array(calib.to_table((u + d) * scale, v * scale))
    return float(np.linalg.norm(b - a) / (2 * d))


def _smooth(x, k):
    k = max(3, int(k) | 1)
    return np.convolve(np.pad(x, k // 2, mode="edge"), np.ones(k) / k, mode="valid")


def _axis_frame(mask):
    """Rotate the mask so the object's long axis points straight down (the end nearer the table at the
    bottom). Slicing it row by row then measures its width ACROSS its own axis, which stays right when
    the object leans. Returns (rotated mask, matrix back to the photo, tilt from vertical in degrees)."""
    ys, xs = np.nonzero(mask)
    pts = np.stack([xs, ys], 1).astype(np.float64)
    c = pts.mean(0)
    w, v = np.linalg.eigh(np.cov((pts - c).T))
    d = v[:, 1]                                       # long axis
    if d[1] < 0:
        d = -d                                        # point it toward larger y (nearer the table)
    R = np.array([[d[1], -d[0]], [d[0], d[1]]])       # rotates d onto (0, 1)
    n = int(np.ceil(np.hypot(*mask.shape))) + 4
    M = np.hstack([R, (np.array([n / 2, n / 2]) - R @ c)[:, None]])
    rot = cv2.warpAffine(mask.astype(np.uint8), M, (n, n), flags=cv2.INTER_NEAREST) > 0
    return rot, cv2.invertAffineTransform(M), float(math.degrees(math.atan2(abs(d[0]), d[1])))


def best_side_row(mask, cm_per_px, max_open_cm, frac_hint=0.5):
    """Where on an upright (or leaning) object should the jaws close?
    Slices the object across its own axis and scores every slice between 20% and 80% of its length:
    straight sides (width not changing) + near the object's centre of mass + wide body rather than a
    thin neck + close to the usual grasp height. Slices wider than the gripper opens don't fit.
    Always returns the best slice it found; "fits" says whether it really fits the gripper."""
    mask = mask.astype(bool)
    if mask.sum() < 50:
        return None
    rot, Minv, tilt = _axis_frame(mask)
    ys, xs = np.nonzero(rot)
    y_top, y_bot = int(ys.min()), int(ys.max())
    h = y_bot - y_top + 1
    if h < 15:
        return None
    rows = np.arange(y_top, y_bot + 1)
    left = np.zeros(len(rows)); right = np.zeros(len(rows))
    for i, y in enumerate(rows):
        r = np.nonzero(rot[y])[0]
        left[i], right[i] = (r.min(), r.max()) if r.size else (np.nan, np.nan)
    wid = right - left + 1
    if np.isnan(wid).any():
        wid = np.where(np.isnan(wid), np.nanmean(wid), wid)
    ws = _smooth(wid, 0.06 * h)
    taper = _smooth(np.abs(np.gradient(ws)), 0.06 * h) * (0.1 * h) / np.maximum(ws, 1.0)
    wmax = float(ws.max())
    cen_row = float(ys.mean())
    body = [i for i, y in enumerate(rows) if 0.2 <= (y_bot - y) / max(h - 1, 1) <= 0.8]
    best = {True: None, False: None}                    # True = fits the gripper, False = best of the rest
    for i in body:
        y = rows[i]
        if not np.isfinite(left[i]) or not np.isfinite(right[i]):
            continue
        frac = (y_bot - y) / max(h - 1, 1)
        fits = (right[i] - left[i] + 1) * cm_per_px <= max_open_cm - JAW_MARGIN_CM
        score = (1.0 * taper[i] + 0.6 * abs(y - cen_row) / h * 2
                 + 0.4 * (1 - ws[i] / wmax) + 0.3 * abs(frac - frac_hint))
        if not fits:
            score = ws[i]                               # nothing fits: show the narrowest slice
        if best[fits] is None or score < best[fits][0]:
            best[fits] = (score, i, frac)
    pick = best[True] or best[False]
    if pick is None:
        return None
    fits = best[True] is not None
    _, i, frac = pick
    y = int(rows[i])
    r = np.nonzero(rot[y])[0]
    l, rt = int(r.min()), int(r.max())
    back = lambda px, py: [float(v) for v in Minv @ np.array([px, py, 1.0])]
    return {"type": "side", "fits": fits, "tilt_deg": round(tilt, 1),
            "point_px": back((l + rt) / 2, y), "jaw1_px": back(l, y), "jaw2_px": back(rt, y),
            "width_px": rt - l + 1, "width_cm": float((rt - l + 1) * cm_per_px),
            "body_width_px": float(np.median(ws[body])) if body else float(ws.mean()),
            "height_frac": round(frac, 2), "mask_height_px": h, "score": round(float(pick[0]), 3)}


def top_grasp(mask, calib, scale, cm_per_px, max_open_cm):
    """grasp_planner_v3's best top-down grasp, with its closing angle turned into the table frame."""
    def metric_fits(p1, p2):
        a = np.array(calib.to_table(p1[0] * scale, p1[1] * scale))
        b = np.array(calib.to_table(p2[0] * scale, p2[1] * scale))
        width = np.linalg.norm(b - a)
        return bool(np.isfinite(width) and 0 < width <= max_open_cm - JAW_MARGIN_CM)
    best, _ = gp.plan_grasp(mask, top_k=1, candidate_filter=metric_fits)
    fits = best is not None
    if best is None:
        best, _ = gp.plan_grasp(mask, None, top_k=1)      # show the attempt anyway
    if best is None:
        return None
    p1, p2 = best["jaw1_px"], best["jaw2_px"]
    t1 = np.array(calib.to_table(p1[0] * scale, p1[1] * scale))
    t2 = np.array(calib.to_table(p2[0] * scale, p2[1] * scale))
    d = t2 - t1
    fits = bool(fits and np.isfinite(d).all() and 0 < np.linalg.norm(d) <= max_open_cm - JAW_MARGIN_CM)
    ang = math.degrees(math.atan2(d[1], d[0])) % 180
    return {"type": "top", "fits": fits, "point_px": [float(v) for v in best["center_px"]],
            "jaw1_px": p1, "jaw2_px": p2, "width_px": best["width_px"],
            "width_cm": float(np.linalg.norm(d)),
            "closing_angle_table_deg": round(ang), "score": best["score"]}


# ---------- drawing ----------

def show(path):
    """Open the result picture in the default viewer (Preview on a Mac)."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            Image.open(path).show()
    except Exception as e:
        print(f"(couldn't open {path}: {e})")


def annotate(path, t, status_line):
    vis = cv2.imread(path)
    if t.get("contact_px"):
        c = tuple(int(round(v)) for v in t["contact_px"])
        cv2.circle(vis, c, 7, (255, 255, 255), -1); cv2.circle(vis, c, 7, (0, 0, 0), 2)
    p = tuple(int(round(v)) for v in t["target_px"])
    cv2.drawMarker(vis, p, (0, 0, 255), cv2.MARKER_CROSS, 34, 3)
    cv2.circle(vis, p, 14, (0, 0, 255), 2)
    text = f"arm target  x={t['x_cm']:.1f}  y={t['y_cm']:.1f} cm"
    for col, th in (((255, 255, 255), 4), ((0, 0, 255), 2)):
        cv2.putText(vis, text, (p[0] + 20, p[1] + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.7, col, th)
        cv2.putText(vis, status_line, (10, vis.shape[0] - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255) if th == 4 else (0, 0, 0), th)
    cv2.imwrite(path, vis)


def annotate_grasp(path, t, g, status_line):
    """Red cross = the grasp point on the object. Green = where the jaws touch. White dot = where the
    object touches the table. Small red circle = the table spot (x, y) under the grasp, sent to the arm."""
    vis = cv2.imread(path)
    gp_ = tuple(int(round(v)) for v in g["point_px"])
    j1 = tuple(int(round(v)) for v in g["jaw1_px"]); j2 = tuple(int(round(v)) for v in g["jaw2_px"])
    green = (0, 220, 0) if g.get("fits", True) else (0, 140, 255)
    cv2.line(vis, j1, j2, green, 3)
    for j in (j1, j2):
        d = np.array(j2, float) - np.array(j1, float)
        n = np.array([-d[1], d[0]]); n = n / (np.linalg.norm(n) + 1e-9) * 22
        cv2.line(vis, (int(j[0] - n[0]), int(j[1] - n[1])), (int(j[0] + n[0]), int(j[1] + n[1])), green, 5)
    if t.get("contact_px"):
        c = tuple(int(round(v)) for v in t["contact_px"])
        cv2.circle(vis, c, 7, (255, 255, 255), -1); cv2.circle(vis, c, 7, (0, 0, 0), 2)
    fp = tuple(int(round(v)) for v in t["target_px"])
    if math.dist(fp, gp_) > 12:
        cv2.line(vis, gp_, fp, (0, 0, 255), 1)
        cv2.circle(vis, fp, 6, (0, 0, 255), 2)
    cv2.drawMarker(vis, gp_, (0, 0, 255), cv2.MARKER_CROSS, 34, 3)
    cv2.circle(vis, gp_, 14, (0, 0, 255), 2)
    z = f"  z={g['height_cm']:.1f}" if g.get("height_cm") is not None else ""
    text = (f"GRASP x={t['x_cm']:.1f} y={t['y_cm']:.1f}{z} cm  (jaws {g['width_cm']:.1f} cm)" if g.get("fits", True)
            else f"DOES NOT FIT: object {g['width_cm']:.1f} cm wide here")
    for col, th in (((255, 255, 255), 4), ((0, 0, 255), 2)):
        cv2.putText(vis, text, (gp_[0] + 20, gp_[1] + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.7, col, th)
        cv2.putText(vis, status_line, (10, vis.shape[0] - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255) if th == 4 else (0, 0, 0), th)
    cv2.imwrite(path, vis)


# ---------- main ----------

def main(argv=None):
    ap = argparse.ArgumentParser(description="SAM 3 picks the object, the arm grabs it")
    ap.add_argument("--target")
    ap.add_argument("--category")
    ap.add_argument("--vlm", choices=["none", "ollama", "claude"], default="none")
    ap.add_argument("--arm-url", default="http://localhost:8890", help="your friend's arm.click page")
    ap.add_argument("--image", help="offline test: use this saved frame instead of the live camera")
    ap.add_argument("--calib", help="offline test: path to his calib/camera.json")
    ap.add_argument("--items", default="items_v1.json")
    ap.add_argument("--point", choices=["auto", "contact", "center"], default="auto",
                    help="auto: table contact + half the item's width; center: middle of the mask")
    ap.add_argument("--height", type=float, help="fingertip height when closing, cm (default: from item)")
    ap.add_argument("--above", type=float, help="approach / lift height, cm (default: from item)")
    ap.add_argument("--go", action="store_true", help="really move the arm (default: dry run)")
    ap.add_argument("--check", action="store_true", help="only print how accurate the calibration is")
    ap.add_argument("--lens", action="store_true", help="try lens-curve correction (needs 8+ marks)")
    ap.add_argument("--place", nargs=2, type=float, metavar=("X", "Y"),
                    help="after a successful grab, put the object down at this table point (cm)")
    ap.add_argument("--place-height", type=float, help="fingertip height when releasing, cm (default: close height)")
    ap.add_argument("-y", "--yes", action="store_true", help="with --go: don't ask before moving")
    ap.add_argument("--out", default="pick.jpg")
    ap.add_argument("--max-open-cm", type=float, default=GRIPPER_OPEN_CM, help="measured opening; controller nominal default 5.1 cm")
    ap.add_argument("--finger-cm", type=float, default=5.0, help="usable finger length (default 5)")
    ap.add_argument("--scale-from-profile", action="store_true",
                    help="measure widths with the item profile's width as the ruler (use when the calibration was made on another camera view)")
    ap.add_argument("--no-show", action="store_true", help="don't open the result picture afterwards")
    a = ap.parse_args(argv)
    if a.max_open_cm <= JAW_MARGIN_CM or a.max_open_cm > GRIPPER_OPEN_CM:
        ap.error(f"--max-open-cm must exceed {JAW_MARGIN_CM} and not exceed controller limit {GRIPPER_OPEN_CM}")
    if not a.target and not a.check:
        ap.error("--target is required (or use --check)")
    ts.VLM_BACKEND = a.vlm
    live = a.calib is None
    if not live and not a.image and not a.check:
        sys.exit("Offline test needs --image as well as --calib.")
    if a.image and live:
        ap.error("--image requires --calib; saved images must not silently use the live camera")
    if a.go and not live:
        sys.exit("--go needs the live camera (don't combine it with --image).")

    # 1. frame + calibration
    if live:
        try:
            st = requests.get(a.arm_url + "/status", timeout=5).json()
        except requests.RequestException as e:
            sys.exit(f"Can't reach the arm page at {a.arm_url} ({e}). Is `python -m arm.click` running?")
        if not st.get("calibrated"):
            sys.exit("The camera isn't calibrated yet. Do the 6+ marks on the arm page first.")
        if st.get("pending"):
            sys.exit("The arm page is waiting for a calibration click. Finish or undo it first.")
        if st.get("camera_role") != "stand":
            sys.exit("Select the fixed stand camera on the integrated controller; a moving-camera homography is invalid.")
        r = requests.get(a.arm_url + "/frame.jpg", timeout=5)
        if r.status_code != 200:
            sys.exit("The arm page has no camera image right now.")
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        calib = Calib(st["pairs"], st["size"], img.height / img.width, a.lens)
    else:
        with open(a.calib) as file:
            d = json.load(file)
        img = Image.open(a.image).convert("RGB") if a.image else None
        calib = Calib(d["pairs"], d.get("size", 224), img.height / img.width if img else 9 / 16, a.lens)
    print(calib.report())
    if a.check:
        return "CHECK"
    scale = calib.size / img.width
    if abs(scale - 1) > 0.01:
        print(f"Note: frame is {img.width}px wide, calibration was made at {calib.size:.0f}px; rescaling.")

    # 2. which object
    res = ts.decide(img, a.target, a.category)
    ts.draw_sam3_style(img, res, a.out, show_all=True)
    if res["status"] != "GO":
        print(json.dumps({"status": res["status"], "target": a.target}, indent=2))
        print(f"saved {a.out}")
        if not a.no_show:
            show(a.out)
        return res["status"]
    cand = res["chosen"]

    # 3. grasp point, 4. table x, y
    profile = gp.load_profile(a.items, a.target)
    posture, how = gp.detect_posture(ts, img, cand, a.vlm)
    gtype, why = gp.choose_grasp_type(profile, posture, how, "auto", a.finger_cm)
    mask = cand["mask"]
    width, height_cm = profile.get("width_cm"), profile.get("height_cm")
    cu, cv_ = contact_pixel(mask)
    cmpp = local_cm_per_px(calib, cu, cv_, scale)
    if gtype == "side":
        g = best_side_row(mask, cmpp, a.max_open_cm, profile.get("grasp_height_frac", 0.5))
        if g is not None and a.scale_from_profile and width:
            cmpp = width / g["body_width_px"]
            print(f"Scale from the item profile: {cmpp:.3f} cm/px (calibration said "
                  f"{local_cm_per_px(calib, cu, cv_, scale):.3f}).")
            g = best_side_row(mask, cmpp, a.max_open_cm, profile.get("grasp_height_frac", 0.5))
    else:
        g = top_grasp(mask, calib, scale, cmpp, a.max_open_cm)
    if g is None:
        print(json.dumps({"status": "NO_GRASP", "target": a.target, "reason": "mask too small to plan a grasp"},
                         indent=2))
        return "NO_GRASP"
    if not g["fits"]:
        diag = {"status": "NO_GRASP", "target": a.target, "grasp_type": gtype,
                "reason": f"object looks {g['width_cm']} cm wide at its narrowest good spot; "
                          f"the gripper opens {a.max_open_cm} cm (needs {JAW_MARGIN_CM} cm clearance)",
                "scale_cm_per_px_at_object": round(cmpp, 3), "profile_width_cm": width,
                "tilt_in_image_deg": g.get("tilt_deg")}
        if width and width <= a.max_open_cm - JAW_MARGIN_CM:
            diag["hint"] = ("the item profile says it fits, so the scale from this calibration is probably "
                            "off here (different camera view, or loose marks) or the object leans a lot. "
                            "Try --scale-from-profile.")
        print(json.dumps(diag, indent=2))
        t0 = choose_target(mask, calib, scale, width / 2 if width else DEFAULT_PUSH_CM, "contact")
        g["height_cm"] = None
        annotate_grasp(a.out, t0, g, f"{a.target}: does not fit the gripper")
        print(f"saved {a.out}")
        if not a.no_show:
            show(a.out)
        return "NO_GRASP"
    g["height_cm"] = round(height_cm * g["height_frac"], 1) if (g["type"] == "side" and height_cm) else None
    # footprint: the table spot under the grasp
    mode = a.point
    if mode == "auto":
        mode = "contact" if (width or g["type"] == "side") else "center"
    push = (width / 2 if width else (g["width_cm"] / 2 if g["type"] == "side" else DEFAULT_PUSH_CM))
    if mode == "center":
        x, y = calib.to_table(g["point_px"][0] * scale, g["point_px"][1] * scale)
        t = {"mode": "grasp center (height not corrected)", "contact_px": None,
             "target_px": g["point_px"], "x_cm": x, "y_cm": y}
    else:
        t = choose_target(mask, calib, scale, push, "contact")
    close_cm, above_cm = grab_heights(profile, a.height, a.above)
    if g["height_cm"] is not None and a.height is None:
        close_cm = max(1.0, g["height_cm"])
    grasp_out = {"type": g["type"], "why": why, "width_cm": g["width_cm"]}
    if g["type"] == "side":
        grasp_out["at_height_fraction"] = g["height_frac"]
        if g["height_cm"] is not None:
            grasp_out["fingertip_height_cm"] = g["height_cm"]
    else:
        grasp_out["closing_angle_table_deg"] = g["closing_angle_table_deg"]
    if g.get("tilt_deg", 0) > 20:
        grasp_out["warning_tilt"] = (f"object leans {g['tilt_deg']} degrees in the image: the jaws close across "
                                     "its axis, but the height estimate assumes it stands upright")
    if width and abs(g["width_cm"] - width) > 0.35 * width:
        grasp_out["warning"] = (f"measured width {g['width_cm']} cm differs from the profile's {width} cm: "
                                "wrong object, tilted object, or wrong calibration")
    out = {"status": "GO", "target": a.target, "x_cm": round(t["x_cm"], 1), "y_cm": round(t["y_cm"], 1),
           "footprint": t["mode"], "posture": posture, "grasp": grasp_out,
           "close_height_cm": close_cm, "approach_height_cm": above_cm,
           "item_profile": profile or f"none (add it to {a.items})"}
    body = grasp_request(t["x_cm"], t["y_cm"], close_cm, above_cm, g)
    execution_blocks = request_errors(body)
    out["controller_request"] = body
    out["execution_blocks"] = execution_blocks
    out["executable"] = not execution_blocks and not calibration_blocks(calib, t["target_px"], scale)
    if not calib.inside_marks(t["target_px"][0] * scale, t["target_px"][1] * scale):
        out["warning"] = "target is outside the area covered by the calibration marks: position is a guess"
    annotate_grasp(a.out, t, g, f"{a.target}: {g['type']} grasp, close at {close_cm} cm, approach from {above_cm} cm")
    print(json.dumps(out, indent=2))
    print(f"saved {a.out}")
    if not a.no_show:
        show(a.out)

    # 5. grab
    if not a.go:
        print(f"Dry run: the arm did not move. Check {a.out}, then add --go.")
        return "DRY_RUN"
    blocked = calibration_blocks(calib, t["target_px"], scale)
    if blocked:
        print(json.dumps({"status": "CALIBRATION_BLOCKED", "reasons": blocked}, indent=2))
        return "CALIBRATION_BLOCKED"
    if execution_blocks:
        print(json.dumps({"status": "EXECUTION_BLOCKED", "reasons": execution_blocks}, indent=2))
        return "EXECUTION_BLOCKED"
    if not a.yes:
        input("Follower on (Self-controlled)? Table clear? Press Enter to grab, Ctrl+C to cancel... ")
    try:
        result = ArmBridge(a.arm_url).submit_grasp(body)
    except (ValueError, requests.RequestException) as exc:
        print("Arm request failed (not retried):", exc)
        return "ARM_ERROR"
    print("Arm:", result)
    if not result["holding"]:
        return "EMPTY"
    if a.place:
        px, py = a.place
        r = requests.post(a.arm_url + "/api/place", timeout=120,
                          json={"x": px, "y": py, "above": above_cm,
                                "height": a.place_height if a.place_height is not None else close_cm}).json()
        if not r.get("ok"):
            print("Could not place:", r.get("error"))
            if "Unknown action" in str(r.get("error")):
                print("  -> his page has no 'place' action yet. Add the snippet from place_snippet_v1.py to arm/click.py.")
            return "HOLDING"
        print(f"Placed at x={px:.1f}, y={py:.1f} cm.")
        return "PLACED"
    return "HOLDING"


if __name__ == "__main__":
    main()
