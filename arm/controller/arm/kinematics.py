"""Where is the gripper? Forward kinematics for the ElRobot follower.

Uses the arm's geometry file from norma-core (the same one Station's 3D view uses)
and the same joint mapping as Station: each joint's calibrated range (0-100 %)
is spread linearly over the model's joint limits.

Positions are in the arm's own frame: origin under the base, z pointing up,
in metres internally and printed in cm.

Command line (run from the repo root, mirroring ON, move the arm with the leader):
  python -m arm.kinematics tip                 live gripper tip position
  python -m arm.kinematics check                accuracy check on an A4 sheet taped to the table
"""

import argparse
import asyncio
import math
import sys
import time
import xml.etree.ElementTree as ET

from pathlib import Path

import json

URDF_RELATIVE = Path("hardware/elrobot/simulation/elrobot_follower.urdf")
TIP_LINK = "Gripper_Base_v1_1"
# Point between the closed fingertips, in the Gripper_Base frame (from the jaw meshes).
TIP_OFFSET = (0.001, 0.100, 0.0)
MOTOR_JOINTS = {i: f"rev_motor_0{i}" for i in range(1, 8)}  # joint 8 is the gripper itself

# ---------- this arm's own corrections (files in calib/, all optional) ----------
# trim.json  {"2": 1.5, ..., "roll_raw": 2202}: degrees to add to a joint's angle (its real zero is
#            not exactly where the calibrated range says), and where joint 7 sits when the gripper is straight.
# tip.json   {"tip_m": [x, y, z]}: where the point between the fingertips really is, in the gripper's frame.
# Both are read again whenever the file changes, so no restart is needed.
_CALIB = Path(__file__).resolve().parent.parent / "calib"
TRIM_FILE = _CALIB / "trim.json"
TIP_FILE = _CALIB / "tip.json"
_trim_cache = {"mtime": None, "deg": {}, "roll_raw": None}
_tip_cache = {"mtime": None, "tip": TIP_OFFSET}


def joint_trim(motor):
    """Angle fix (rad) for this joint: what its real angle is beyond what the calibration says."""
    try:
        mt = TRIM_FILE.stat().st_mtime
    except OSError:                    # no file (or deleted): no fixes at all
        _trim_cache.update(mtime=None, deg={}, roll_raw=None)
        return 0.0
    if mt != _trim_cache["mtime"]:
        try:
            data = json.loads(TRIM_FILE.read_text())
            _trim_cache["deg"] = {int(k): float(v) for k, v in data.items() if k.isdigit()}
            _trim_cache["roll_raw"] = data.get("roll_raw")
        except (ValueError, OSError, AttributeError):
            _trim_cache["deg"], _trim_cache["roll_raw"] = {}, None
        _trim_cache["mtime"] = mt
    return math.radians(_trim_cache["deg"].get(motor, 0.0))


def roll_raw():
    """Where joint 7 (wrist roll) has to be for the gripper to hang straight (set with "trim"),
    or None: then joint 7 is left wherever it is, as before."""
    joint_trim(7)                      # (re)loads the file
    r = _trim_cache.get("roll_raw")
    return int(r) if r is not None else None


def tip_offset():
    """The point between the fingertips in the gripper's frame (m): tip.json if there is one."""
    try:
        mt = TIP_FILE.stat().st_mtime
    except OSError:
        _tip_cache.update(mtime=None, tip=TIP_OFFSET)
        return TIP_OFFSET
    if mt != _tip_cache["mtime"]:
        try:
            t = json.loads(TIP_FILE.read_text())["tip_m"]
            t = tuple(float(v) for v in t)
            ok = len(t) == 3 and all(math.isfinite(v) for v in t) and math.dist(t, TIP_OFFSET) < 0.03
            _tip_cache["tip"] = t if ok else TIP_OFFSET     # more than 3 cm off the drawing: not believable
        except (ValueError, OSError, KeyError, TypeError):
            _tip_cache["tip"] = TIP_OFFSET
        _tip_cache["mtime"] = mt
    return _tip_cache["tip"]


# ---------- small 4x4 matrix helpers (no numpy needed) ----------

def _matmul(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _translate(x, y, z):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]


def _rpy(r, p, y):
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, 0],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, 0],
        [-sp, cp * sr, cp * cr, 0],
        [0, 0, 0, 1],
    ]


def _axis_angle(axis, a):
    x, y, z = axis
    n = math.sqrt(x * x + y * y + z * z)
    x, y, z = x / n, y / n, z / n
    c, s, t = math.cos(a), math.sin(a), 1 - math.cos(a)
    return [
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0],
        [0, 0, 0, 1],
    ]


def _apply(m, p):
    return tuple(m[r][0] * p[0] + m[r][1] * p[1] + m[r][2] * p[2] + m[r][3] for r in range(3))


# ---------- the arm model ----------

class ArmModel:
    def __init__(self, urdf_path=None):
        if urdf_path is None:
            from arm.station import NORMA_CORE
            urdf_path = NORMA_CORE / URDF_RELATIVE
        self.urdf_path = Path(urdf_path)
        root = ET.parse(self.urdf_path).getroot()
        if TIP_LINK not in {link.get("name") for link in root.findall("link")}:
            raise ValueError(f"URDF has no {TIP_LINK} link")
        by_child = {j.find("child").get("link"): j for j in root.findall("joint")}
        chain = []
        link = TIP_LINK
        while link in by_child:  # walk from the gripper back to the base
            j = by_child[link]
            chain.append(j)
            link = j.find("parent").get("link")
        self.chain = []
        for j in reversed(chain):
            o = j.find("origin")
            xyz = [float(v) for v in (o.get("xyz", "0 0 0") if o is not None else "0 0 0").split()]
            rpy = [float(v) for v in (o.get("rpy", "0 0 0") if o is not None else "0 0 0").split()]
            ax = j.find("axis")
            lim = j.find("limit")
            self.chain.append({
                "name": j.get("name"),
                "type": j.get("type"),
                "origin": _matmul(_translate(*xyz), _rpy(*rpy)),
                "axis": [float(v) for v in ax.get("xyz").split()] if ax is not None else None,
                "lower": float(lim.get("lower")) if lim is not None else 0.0,
                "upper": float(lim.get("upper")) if lim is not None else 0.0,
            })
        self.motor_of = {name: i for i, name in MOTOR_JOINTS.items()}

    def angles(self, percents: dict[int, float]) -> dict[str, float]:
        """Joint angles (rad) from joint percentages, exactly like Station's 3D view."""
        out = {}
        for j in self.chain:
            if j["type"] == "revolute" and j["name"] in self.motor_of:
                p = max(0.0, min(100.0, percents[self.motor_of[j["name"]]])) / 100.0
                out[j["name"]] = j["lower"] + p * (j["upper"] - j["lower"])
        return out

    def angles_true(self, joints) -> dict[str, float]:
        """Joint angles using the servo's real scale (4096 steps = 360 deg).

        The middle of the calibrated range is taken as the middle of the model's
        range. Better than `angles` when calibration didn't reach the full limits.
        """
        out = {}
        for j in self.chain:
            if j["type"] == "revolute" and j["name"] in self.motor_of:
                motor = self.motor_of[j["name"]]
                m = joints[motor]
                mid_raw = (m.range_min + m.range_max) / 2
                out[j["name"]] = ((j["lower"] + j["upper"]) / 2 + joint_trim(motor)
                                  + (m.raw - mid_raw) * 2 * math.pi / 4096)
        return out

    def frames(self, angles: dict[str, float]):
        """Return (4x4 matrix of the gripper base, list of every joint position), metres."""
        m = _translate(0, 0, 0)
        points = []
        for j in self.chain:
            m = _matmul(m, j["origin"])
            points.append(_apply(m, (0, 0, 0)))
            if j["name"] in angles:
                m = _matmul(m, _axis_angle(j["axis"], angles[j["name"]]))
        return m, points

    def forward(self, percents: dict[int, float] | None = None, angles: dict[str, float] | None = None):
        """Return (tip position, list of every joint position along the arm), in metres."""
        ang = angles if angles is not None else self.angles(percents)
        m, points = self.frames(ang)
        return _apply(m, tip_offset()), points


def _cm(p):
    return f"x={p[0] * 100:6.1f}  y={p[1] * 100:6.1f}  z={p[2] * 100:6.1f} cm"


def _dist(a, b):
    return math.dist(a, b) * 100


# ---------- command line ----------

async def _sample_tip(arm, model, seconds=0.5):
    """Average the tip position over a short moment, to remove servo jitter."""
    a, b = [], []
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        joints = dict(arm.joints)
        a.append(model.forward({i: j.percent for i, j in joints.items()})[0])
        b.append(model.forward(angles=model.angles_true(joints))[0])
        await asyncio.sleep(0.03)
    avg = lambda pts: tuple(sum(p[k] for p in pts) / len(pts) for k in range(3))
    return avg(a), avg(b)


async def _ask(prompt):
    return await asyncio.get_running_loop().run_in_executor(None, input, prompt)


async def _main(args):
    from arm.arm import Arm  # imported here so the model can be used without Station

    model = ArmModel()
    async with Arm() as arm:
        print(f"Connected to follower {arm.bus}. Model: {model.urdf_path.name}")
        if args.cmd == "tip":
            while True:
                tip, _ = model.forward(angles=model.angles_true(dict(arm.joints)))
                print("\rgripper tip: " + _cm(tip) + "   ", end="", flush=True)
                await asyncio.sleep(0.1)

        # ---- accuracy check on a taped rectangle (default: an A4 sheet, long side facing the arm) ----
        w, d = args.width, args.depth
        names = ["A (front left)", "B (front right)", "C (back right)", "D (back left)"]
        print(f"\nRectangle {w} cm wide x {d} cm deep. 'Front' = the edge closest to the arm.")
        print("For each corner: with the LEADER, touch the corner with the closed fingertips,")
        print("gripper pointing straight down, hold still, then press Enter.\n")
        touches = []
        for n in names:
            await _ask(f"Touch corner {n}, then Enter... ")
            touches.append(await _sample_tip(arm, model))
            print("   " + _cm(touches[-1][1]))
        print("\nNow touch corner A again, but with the arm bent differently")
        await _ask("(e.g. elbow higher or lower), then Enter... ")
        touches.append(await _sample_tip(arm, model))
        print("   " + _cm(touches[-1][1]))

        diag = math.hypot(w, d)
        results = {}
        for k, label in ((1, "TRUE SCALE (servo steps -> degrees)"), (0, "STRETCH (like Station's 3D view)")):
            pts = [t[k] for t in touches[:4]]
            a2 = touches[4][k]
            rows = [
                ("side A-B", _dist(pts[0], pts[1]), w),
                ("side B-C", _dist(pts[1], pts[2]), d),
                ("side C-D", _dist(pts[2], pts[3]), w),
                ("side D-A", _dist(pts[3], pts[0]), d),
                ("diagonal A-C", _dist(pts[0], pts[2]), diag),
                ("diagonal B-D", _dist(pts[1], pts[3]), diag),
                ("A touched twice", _dist(pts[0], a2), 0.0),
            ]
            print(f"\n--- {label} ---")
            print("                 measured   should be   error")
            worst = 0.0
            for name, got, want in rows:
                worst = max(worst, abs(got - want))
                print(f"{name:16s} {got:6.1f} cm   {want:6.1f} cm   {got - want:+5.1f} cm")
            zs = [p[2] * 100 for p in pts]
            flat = max(zs) - min(zs)
            worst = max(worst, flat)
            print(f"{'table flatness':16s} corner heights differ by {flat:.1f} cm (should be ~0)")
            print(f"worst error: {worst:.1f} cm")
            results[label] = worst

        best = min(results, key=results.get)
        worst = results[best]
        if worst <= 1.0:
            verdict = "GOOD: accurate enough. Go with kinematics."
        elif worst <= 2.0:
            verdict = "OK: usable, grabs may be off by 1-2 cm. Kinematics plus a small correction."
        else:
            verdict = "POOR: errors over 2 cm. Use the taught-grid approach instead."
        print(f"\nBest mapping: {best}, worst error {worst:.1f} cm -> {verdict}")


def main():
    p = argparse.ArgumentParser(prog="python -m arm.kinematics", description="Arm geometry tools")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tip", help="live gripper tip position")
    c = sub.add_parser("check", help="accuracy check on a taped rectangle (default A4)")
    c.add_argument("--width", type=float, default=29.7, help="front edge length in cm (default 29.7 = A4)")
    c.add_argument("--depth", type=float, default=21.0, help="side edge length in cm (default 21.0 = A4)")
    args = p.parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        print()
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
