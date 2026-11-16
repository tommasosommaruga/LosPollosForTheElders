"""
calibrate_board.py — calibrate the standing camera to the arm's table x,y with a checkerboard.

IN THE LOSPOLLOS-ARM REPO (arm page running: python3 -m arm.click --camera /dev/video4):
    python3 vision/calibrate_board.py --page http://localhost:8890 --square-cm 2.5          # first board position
    python3 vision/calibrate_board.py --page http://localhost:8890 --square-cm 2.5 --add    # each further position
  With --page you don't type coordinates: touch a corner (mirroring ON, with the leader), press Enter,
  and the tip position is read from the arm page. The result goes straight to calib/camera.json (the
  old one is kept as calib/camera.before-*.json) and the arm page reloads it: no restart needed.

Why: clicking 5 marks by hand gives a loose pixel -> table mapping. A checkerboard lying flat on the
table gives dozens of precise points from ONE photo. The only manual part is telling the script where
three of the board's corners are in the ARM's coordinates (touch them with the gripper tip).

Needs: pip install opencv-python numpy        (nothing else; no torch)

STEP 0 - make and print a board (once)
    python calibrate_board.py --make-board            # writes board_9x6.png (10x7 squares)
  Print it at 100% / "actual size" (NO "fit to page"), glue it on something FLAT and stiff (cardboard,
  a book cover). A curled board ruins the result. Then MEASURE one square with a ruler, in cm. Use that
  number as --square-cm below (printers scale; do not trust the nominal size).

STEP 1 - lay the board flat on the table, inside the area where the arm will pick things, fully visible
  to the camera. Do not move it until step 2 is finished.

STEP 2 - run (pick ONE way to get the camera image):
    python calibrate_board.py --camera 0 --square-cm 2.5          # direct from the camera (press SPACE)
    python calibrate_board.py --page http://localhost:8890 --square-cm 2.5   # same image the arm page uses
    python calibrate_board.py --image photo.jpg --square-cm 2.5   # a saved photo from that camera
  The script finds the corners and saves board_view.jpg with corners labelled A (red), B (green),
  C (blue) and an optional far corner D (orange). Then, for each of A, B, C: move the gripper tip until it touches that exact corner and
  read its position (python -m arm.reach where), and type "x y" in cm when asked.
  (Non-interactive: add  --measure A=x,y B=x,y C=x,y [D=x,y] .)
  If the three touches do not fit a board of this size, the script REFUSES to write camera.json and says
  why (wrong --square-cm, wrong corner touched, corners swapped). --force overrides it; do not.

STEP 3 - move the board to another place in the pick area and run again with --append camera.json.
  3 to 5 positions covering the whole area (corners and middle) is much better than one.

RESULT: camera.json in the same format the arm page and pick_v4.py use ({"cam", "size", "pairs":
[[u, v, x_cm, y_cm], ...]}), plus calib_check.jpg and an error report. Copy camera.json to the arm
project's calib/camera.json. Then check with:   python pick_v4.py --check

Honest accuracy: the script fits the homography on half of the corners and measures the error on the
other half. That "held-out" number is what to trust, not the fit error. Under 1 cm is good enough for
grasping. If it stays above 1 cm with a flat board and careful touches, the lens (wide angle) is
bending the picture; tell whoever builds on this, a lens correction step is then needed.
"""
import argparse, io, json, math, os, shutil, subprocess, sys, time, urllib.request
import numpy as np
import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_CALIB = os.path.normpath(os.path.join(HERE, "..", "calib"))
DEFAULT_OUT = os.path.join(REPO_CALIB, "camera.json")


# ---------- the printable board ----------

def make_board(cols, rows, path, px=120):
    """(cols+1) x (rows+1) squares -> cols x rows inner corners. Odd x even inner corners (9 x 6) means the
    board looks different when turned 180 degrees, so the corner labels are unambiguous."""
    sq_x, sq_y = cols + 1, rows + 1
    m = px
    img = np.full((sq_y * px + 2 * m, sq_x * px + 2 * m), 255, np.uint8)
    for r in range(sq_y):
        for c in range(sq_x):
            if (r + c) % 2 == 0:
                img[m + r * px:m + (r + 1) * px, m + c * px:m + (c + 1) * px] = 0
    cv2.putText(img, f"{cols}x{rows} inner corners - print at 100% and measure one square",
                (m, m // 2 + 10), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2)
    cv2.imwrite(path, img)


# ---------- finding the corners ----------

def detect(gray, cols, rows):
    """Inner corners as an array (rows*cols, 2), row by row (index = r*cols + c), or None."""
    flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    ok, pts = cv2.findChessboardCornersSB(gray, (cols, rows), flags)
    if not ok:
        ok, pts = cv2.findChessboardCorners(gray, (cols, rows), cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)
        if ok:
            pts = cv2.cornerSubPix(gray, pts, (11, 11), (-1, -1),
                                   (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 0.001))
    return pts.reshape(-1, 2).astype(float) if ok else None


def label_view(img, corners, cols, rows, path):
    vis = img.copy()
    cv2.drawChessboardCorners(vis, (cols, rows), corners.reshape(-1, 1, 2).astype(np.float32), True)
    marks = {"A": (0, 0, (0, 0, 255)), "B": (cols - 1, 0, (0, 200, 0)), "C": (0, rows - 1, (255, 0, 0)),
             "D": (cols - 1, rows - 1, (0, 180, 255))}
    for name, (c, r, col) in marks.items():
        p = tuple(int(round(v)) for v in corners[r * cols + c])
        cv2.circle(vis, p, 14, col, 3)
        for colr, th in (((255, 255, 255), 5), (col, 2)):
            cv2.putText(vis, name, (p[0] + 14, p[1] - 14), cv2.FONT_HERSHEY_SIMPLEX, 1.1, colr, th)
    cv2.imwrite(path, vis)
    return marks


# ---------- board coordinates -> arm coordinates ----------

def fit_board_to_arm(meas, sq):
    """meas = [(col, row, x_cm, y_cm)], at least 3 corners that are not in one line.
    Board point (col*sq, row*sq) -> arm (x, y): a rotation + shift (+ a mirror if the board's axes run the
    other way round in the arm's frame). The size is fixed by --square-cm, so the measurements are only
    used to find where and how the board lies. Returns (function, residuals_cm, mirrored)."""
    P = np.array([[c * sq, r * sq] for c, r, _, _ in meas], float)
    Q = np.array([[x, y] for _, _, x, y in meas], float)
    best = None
    for mirror in (False, True):
        Pm = P * np.array([-1.0, 1.0]) if mirror else P
        pc, qc = Pm.mean(0), Q.mean(0)
        U, _, Vt = np.linalg.svd((Pm - pc).T @ (Q - qc))
        d = np.sign(np.linalg.det(Vt.T @ U.T)) or 1.0
        R = Vt.T @ np.diag([1.0, d]) @ U.T
        res = np.linalg.norm((R @ (Pm - pc).T).T + qc - Q, axis=1)
        if best is None or res.max() < best[0].max():
            best = (res, R, pc, qc, mirror)
    res, R, pc, qc, mirror = best

    def f(c, r):
        p = np.array([c * sq, r * sq], float)
        if mirror:
            p = p * np.array([-1.0, 1.0])
        return (R @ (p - pc)) + qc
    return f, res, mirror


# ---------- pixel -> table (the same plain homography the arm page uses) ----------

def fit_h(px, xy):
    H, _ = cv2.findHomography(np.asarray(px, np.float64), np.asarray(xy, np.float64), 0)
    return H


def apply_h(H, px):
    p = np.c_[np.asarray(px, float), np.ones(len(px))] @ H.T
    return p[:, :2] / p[:, 2:3]


def evaluate(px_norm, xy):
    """Fit error on all points, and honest error: fit on every other point, test on the rest (both ways)."""
    px_norm, xy = np.asarray(px_norm), np.asarray(xy)
    H = fit_h(px_norm, xy)
    fit = np.linalg.norm(apply_h(H, px_norm) - xy, axis=1)
    held = np.zeros(len(xy))
    for k in (0, 1):
        tr = np.arange(len(xy)) % 2 == k
        Hk = fit_h(px_norm[tr], xy[tr])
        held[~tr] = np.linalg.norm(apply_h(Hk, px_norm[~tr]) - xy[~tr], axis=1)
    return fit, held


# ---------- getting an image ----------

def grab_camera(index, cols, rows):
    cap = cv2.VideoCapture(int(index) if str(index).isdigit() else index)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))   # same picture as the arm page's camera
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    if not cap.isOpened():
        sys.exit(f"Can't open camera {index}. If the arm page is running it may be using the camera: "
                 "use --page http://localhost:8890 instead.")
    print("Live view: lay the board flat in the pick area. Press SPACE when 'FOUND' is shown, q to quit.")
    while True:
        ok, frame = cap.read()
        if not ok:
            sys.exit("Camera gave no image.")
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        k = min(1.0, 1280.0 / g.shape[1])        # a small-looking board needs the resolution; this is still fast
        found = detect(cv2.resize(g, None, fx=k, fy=k) if k < 1 else g, cols, rows) is not None
        vis = frame.copy()
        cv2.putText(vis, "FOUND - press SPACE" if found else "board not found", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 200, 0) if found else (0, 0, 255), 2)
        cv2.imshow("calibrate_board", vis)
        key = cv2.waitKey(30) & 0xFF
        if key == ord("q"):
            sys.exit("Cancelled.")
        if key == ord(" ") and found:
            cap.release(); cv2.destroyAllWindows()
            return frame


def grab_page(url):
    import urllib.request
    data = urllib.request.urlopen(url.rstrip("/") + "/frame.jpg", timeout=10).read()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        sys.exit("The arm page gave no camera image.")
    return img


# ---------- main ----------

def ask_xy(name, optional=False):
    while True:
        raw = input(f"  corner {name}: arm x y in cm (e.g. -12.5 30.2): ").replace(",", " ").split()
        if optional and not raw:
            return None
        try:
            x, y = [float(v) for v in raw]
            return x, y
        except ValueError:
            print("  type two numbers, like:  -12.5 30.2")


def page_tip(url, n=8):
    """The arm page's live tip position [x, y, height above table] in cm, averaged over ~0.5 s."""
    tips = []
    for _ in range(n):
        st = json.loads(urllib.request.urlopen(url.rstrip("/") + "/status", timeout=5).read())
        if st.get("tip"):
            tips.append(st["tip"])
        time.sleep(0.06)
    return [sum(t[k] for t in tips) / len(tips) for k in range(3)] if tips else None


def ask_page(name, url, optional=False):
    """Touch the corner, press Enter: the position comes from the arm page (same x, y the page uses)."""
    while True:
        raw = input(f"  corner {name}: touch it with the fingertips, press Enter"
                    + (" (s = skip)" if optional else "") + ": ").replace(",", " ").split()
        if optional and raw and raw[0].lower() == "s":
            return None
        if raw:   # typing x y still works
            try:
                x, y = [float(v) for v in raw]
                return x, y
            except ValueError:
                print("  just press Enter (or type two numbers like -12.5 30.2)")
                continue
        t = page_tip(url)
        if t is None:
            print("  The arm page shows no tip position. Is the arm connected? (or type x y)")
            continue
        x, y, h = t
        print(f"    tip at x={x:.1f}  y={y:.1f}  ({h:.1f} cm above the table)")
        if h > 3.0:
            print("    That's well above the table: put the fingertips ON the corner, then Enter again.")
            continue
        return x, y


def reload_page(url):
    req = urllib.request.Request(url.rstrip("/") + "/api/reload", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
        print("The arm page now uses it." if d.get("ok") else f"Arm page: {d.get('error')}")
    except Exception as e:  # older page without reload: restart it
        print(f"Couldn't tell the arm page to reload ({e}). Restart it to use the new calibration.")


def parse_measure(items):
    out = {}
    for it in items:
        k, v = it.split("=")
        x, y = [float(t) for t in v.split(",")]
        out[k.strip().upper()] = (x, y)
    return out


TOUCH_REFUSE_CM = 1.5  # touches disagreeing with the board by more than this = a mistake: refuse to write
TOUCH_WARN_CM = 0.6    # between this and the above: write it, but say the touches were loose


def calibrate(img, cols, rows, sq, measure, out_path, view_path, check_path, append=None, force=False, page=None):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    corners = detect(gray, cols, rows)
    if corners is None:
        sys.exit(f"Could not find the {cols}x{rows} inner corners. Is the whole board visible, flat, evenly lit, "
                 "and are --cols/--rows the number of INNER corners (squares minus one)?")
    h, w = gray.shape
    marks = label_view(img, corners, cols, rows, view_path)
    print(f"Found {len(corners)} corners. Look at {view_path}: A = red, B = green, C = blue, D = orange (optional).")
    if not measure:
        try:   # show the labelled picture
            subprocess.Popen(["xdg-open", view_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        if page:
            print("Mirroring ON: with the leader, put the follower's closed fingertips exactly on each labelled")
            print("corner, gripper pointing straight down (same way for every corner), then press Enter:")
            measure = {n: ask_page(n, page) for n in "ABC"}
            d_xy = ask_page("D (far corner, recommended)", page, optional=True)
        else:
            print("Touch the gripper tip to each labelled corner and read its position (python -m arm.reach where):")
            measure = {n: ask_xy(n) for n in "ABC"}
            d_xy = ask_xy("D (far corner, optional - just press Enter to skip)", optional=True)
        if d_xy:
            measure["D"] = d_xy
    meas = [(marks[n][0], marks[n][1], measure[n][0], measure[n][1]) for n in "ABCD" if n in measure]
    if not all(n in measure for n in "ABC"):
        sys.exit("Need at least the three corners A, B and C.")

    # sanity checks on the touches (are they consistent with a board of this size?)
    (a, b, c) = [np.array(m[2:]) for m in meas[:3]]
    problems = []
    if abs(np.linalg.norm(b - a) - (cols - 1) * sq) > TOUCH_REFUSE_CM:
        problems.append(f"A to B is {np.linalg.norm(b - a):.1f} cm but should be {(cols - 1) * sq:.1f} cm")
    if abs(np.linalg.norm(c - a) - (rows - 1) * sq) > TOUCH_REFUSE_CM:
        problems.append(f"A to C is {np.linalg.norm(c - a):.1f} cm but should be {(rows - 1) * sq:.1f} cm")
    f, res, mirrored = fit_board_to_arm(meas, sq)
    if res.max() > TOUCH_REFUSE_CM:
        problems.append(f"the three touched corners do not fit a board of this size (off by up to {res.max():.1f} cm)")
    if problems:
        print("\nPROBLEM with the touched corners (the picture itself is fine):")
        for p_ in problems:
            print("  -", p_)
        print("  Likely causes: wrong --square-cm (measure one square again), the tip was not exactly on the corner "
              "labelled A/B/C in the picture, or two corners were swapped.")
        if not force:
            sys.exit("Nothing was written. Fix the numbers and run again (or add --force to write it anyway).")
        print("  --force given: writing it anyway. Do not trust this calibration.")
    else:
        print(f"Touch check OK: your {len(meas)} measurements fit the board to within {res.max():.2f} cm"
              + (" (board axes mirrored in the arm frame: normal)." if mirrored else "."))
        if res.max() > TOUCH_WARN_CM:
            print(f"  Note: the touches are a bit loose ({res.max():.1f} cm). This limits the accuracy of this board "
                  "position, because a small error in where the board lies tilts the whole map. For a better result: "
                  "touch the corners more carefully, add the optional far corner D, and use several board positions.")

    xy = np.array([f(i % cols, i // cols) for i in range(len(corners))])
    pairs = [[float(u), float(v), float(x), float(y)] for (u, v), (x, y) in zip(corners, xy)]
    if append and os.path.exists(append):
        with open(append) as fh:
            old = json.load(fh)
        if abs(old.get("size", w) - w) > 1:
            sys.exit(f"{append} was made at width {old['size']} but this image is {w} px wide: use the same camera setup.")
        pairs = old["pairs"] + pairs
        print(f"Added to {len(old['pairs'])} existing marks.")
    px = np.array([[p[0], p[1]] for p in pairs]) / w
    table = np.array([[p[2], p[3]] for p in pairs])
    fit, held = evaluate(px, table)
    if os.path.exists(out_path) and os.path.abspath(append or "") != os.path.abspath(out_path):
        keep = out_path.replace(".json", "") + time.strftime(".before-%H%M%S.json")
        shutil.copy(out_path, keep)
        print(f"(the previous {os.path.basename(out_path)} is kept as {keep})")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"cam": 0, "size": w, "pairs": pairs}, fh, indent=1)

    vis = img.copy()
    H = fit_h(px, table)
    for (u, v), e in zip(corners, held[-len(corners):]):
        cv2.circle(vis, (int(u), int(v)), 6, (0, 200, 0) if e < 1 else (0, 0, 255), -1)
    msg = f"{len(pairs)} marks  held-out error: mean {held.mean():.2f} max {held.max():.2f} cm"
    cv2.putText(vis, msg, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 4)
    cv2.putText(vis, msg, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
    cv2.imwrite(check_path, vis)

    print(f"\nWrote {out_path}: {len(pairs)} marks at width {w}px.")
    print(f"  fit error      mean {fit.mean():.2f} cm, max {fit.max():.2f} cm")
    print(f"  held-out error mean {held.mean():.2f} cm, max {held.max():.2f} cm   <- trust this one")
    hull = cv2.contourArea(cv2.convexHull((px * w).astype(np.float32)))
    print(f"  the marks cover {100 * hull / (w * h):.0f}% of the picture.")
    if held.max() > 1.0:
        print("  -> above 1 cm. Check: board flat? tip exactly on the corners? square size measured? "
              "Move the board to more places (--append). If it stays high, the lens is bending the picture.")
    if hull / (w * h) < 0.15:
        print("  -> the marks cover a small part of the picture. Move the board around the pick area and "
              f"run again with --append {out_path}.")
    if page and os.path.abspath(out_path) == os.path.abspath(DEFAULT_OUT):
        reload_page(page)
    return held


def main(argv=None):
    ap = argparse.ArgumentParser(description="checkerboard calibration: camera pixels -> arm table x,y")
    ap.add_argument("--make-board", action="store_true", help="write a printable board and exit")
    ap.add_argument("--cols", type=int, default=9, help="INNER corners along the long side (default 9)")
    ap.add_argument("--rows", type=int, default=6, help="INNER corners along the short side (default 6)")
    ap.add_argument("--square-cm", type=float, help="measured side of one printed square, cm")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--camera", help="camera index (0, 1, ...) or device (/dev/video4); not while the arm page uses it")
    src.add_argument("--page", help="the arm page, e.g. http://localhost:8890 (uses its /frame.jpg)")
    src.add_argument("--image", help="a saved photo from the same camera")
    ap.add_argument("--measure", nargs="*", metavar="A=x,y", help="arm positions of corners A B C (and optionally D); skips typing")
    ap.add_argument("--force", action="store_true", help="write camera.json even if the touches do not fit the board")
    ap.add_argument("--append", metavar="camera.json", help="add this board position to an existing file")
    ap.add_argument("--add", action="store_true", help="add this board position to --out (same as --append <out>)")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"default {DEFAULT_OUT}")
    ap.add_argument("--view", default="board_view.jpg")
    ap.add_argument("--check-img", default="calib_check.jpg")
    a = ap.parse_args(argv)
    if a.make_board:
        path = f"board_{a.cols}x{a.rows}.png"
        make_board(a.cols, a.rows, path)
        print(f"Wrote {path}: print at 100%, glue on a flat board, measure one square, pass it as --square-cm.")
        return
    if a.add:
        a.append = a.out
    if not a.square_cm:
        ap.error("--square-cm is required (measure one printed square with a ruler)")
    if a.image:
        img = cv2.imread(a.image)
        if img is None:
            sys.exit(f"Can't read {a.image}")
    elif a.page:
        img = grab_page(a.page)
    elif a.camera is not None:
        img = grab_camera(a.camera, a.cols, a.rows)
    else:
        ap.error("choose one of --camera, --page, --image")
    calibrate(img, a.cols, a.rows, a.square_cm, parse_measure(a.measure or []), a.out, a.view, a.check_img,
              a.append, a.force, page=a.page)


if __name__ == "__main__":
    main()
