"""Object height from one fixed camera, using the table calibration (pixel <-> table x,y)."""
import numpy as np
import cv2


def camera_from_pairs(pairs, image_size):
    """Camera intrinsics and pose from table calibration marks [u, v, x_cm, y_cm].
    Assumes square pixels and the optical centre in the middle of the picture; the focal length
    and pose come from the table->picture homography. Returns (K, R, t, C, up)."""
    w, h = image_size
    P = np.asarray(pairs, float)
    H, _ = cv2.findHomography(P[:, 2:], P[:, :2], 0)          # table (cm) -> pixel
    cx, cy = w / 2.0, h / 2.0
    h1, h2, h3 = H[:, 0], H[:, 1], H[:, 2]
    a1 = np.array([h1[0] - cx * h1[2], h1[1] - cy * h1[2], h1[2]])
    a2 = np.array([h2[0] - cx * h2[2], h2[1] - cy * h2[2], h2[2]])
    ests = []
    den = a1[2] * a2[2]
    if abs(den) > 1e-12:
        f2 = -(a1[0] * a2[0] + a1[1] * a2[1]) / den
        if f2 > 0:
            ests.append(f2)
    den = a1[2] ** 2 - a2[2] ** 2
    if abs(den) > 1e-12:
        f2 = -((a1[0] ** 2 + a1[1] ** 2) - (a2[0] ** 2 + a2[1] ** 2)) / den
        if f2 > 0:
            ests.append(f2)
    if not ests:
        raise ValueError("can't work out the camera from this calibration")
    f = float(np.sqrt(np.median(ests)))
    K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]])
    Ki = np.linalg.inv(K)
    r1, r2, tt = Ki @ h1, Ki @ h2, Ki @ h3
    lam = 2.0 / (np.linalg.norm(r1) + np.linalg.norm(r2))
    r1, r2, tt = r1 * lam, r2 * lam, tt * lam
    if tt[2] < 0:                       # the table must be in front of the camera
        r1, r2, tt = -r1, -r2, -tt
    r3 = np.cross(r1, r2)
    U, _, Vt = np.linalg.svd(np.column_stack([r1, r2, r3]))
    R = U @ Vt
    C = -R.T @ tt                       # camera centre in table coordinates (cm)
    up = np.array([0, 0, 1.0]) if C[2] > 0 else np.array([0, 0, -1.0])
    return K, R, tt, C, up


def ray(K, R, C, u, v):
    d = R.T @ (np.linalg.inv(K) @ np.array([u, v, 1.0]))
    return C, d / np.linalg.norm(d)


def height_at(K, R, C, up, base_xy, top_px):
    """Height (cm) of the vertical line above table point base_xy that the pixel top_px looks at."""
    B = np.array([base_xy[0], base_xy[1], 0.0])
    o, d = ray(K, R, C, *top_px)
    # closest points between ray o + s d and line B + h up
    A = np.array([[d @ d, -(d @ up)], [d @ up, -(up @ up)]])
    rhs = np.array([(B - o) @ d, (B - o) @ up])
    s, hgt = np.linalg.solve(A, rhs)
    return float(hgt)


def object_height(K, R, C, up, mask, table_of_pixel):
    """Estimate an upright object's height from its outline in the picture.
    table_of_pixel(u, v) -> (x, y) cm on the table (the page's calibration)."""
    ys, xs = np.nonzero(mask)
    v_bot = ys.max()
    near = xs[ys >= v_bot - 2]
    u_bot = float(near.mean())
    front = np.array(table_of_pixel(u_bot, float(v_bot)))     # where it touches the table, nearest the camera
    # its width on the table, from the widest row in its lower part
    low = ys >= v_bot - 0.25 * (v_bot - ys.min() + 1)
    row = ys[low][np.argmax([np.ptp(xs[ys == r]) if (ys == r).any() else 0 for r in ys[low]])]
    rx = xs[ys == row]
    pl, pr = np.array(table_of_pixel(float(rx.min()), float(row))), np.array(table_of_pixel(float(rx.max()), float(row)))
    width = float(np.linalg.norm(pr - pl))
    away = front - C[:2]
    away = away / np.linalg.norm(away)
    far_base = front + away * width                         # the top's far edge sits above this point
    v_top = ys.min()
    u_top = float(xs[ys == v_top].mean())
    center = front + away * width / 2                       # middle of where it stands
    return height_at(K, R, C, up, far_base, (u_top, float(v_top))), width, (float(center[0]), float(center[1]))
