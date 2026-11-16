"""Send the gripper tip to a point in space (inverse kinematics).

Coordinates are in cm in the arm's own frame (same numbers as
`python -m arm.kinematics tip`). Heights are measured ABOVE THE TABLE, which
you teach once with `table`. Nothing is allowed to go below the table.

The gripper points as close to straight down as the arm's reach allows: straight
down near the base, leaning forward further out (the tool prints the lean it uses).

Command line (run from the repo root):
  python -m arm.reach table              teach where the table is (mirroring ON)
  python -m arm.reach where              live x, y, height above table, tilt (any mode)
  python -m arm.reach goto X Y HEIGHT    move the tip there (follower Self-controlled)
  python -m arm.reach goto X Y HEIGHT --line    straight line, e.g. down onto an object or back up
  python -m arm.reach grab X Y           open, go above, straight down, close, lift
  python -m arm.reach grab X Y --side    gripper horizontal, slide in from the arm's side, close, lift

In your own code:
  from arm.arm import Arm
  from arm.reach import Reacher
  async with Arm() as arm:
      r = Reacher(arm)
      await r.goto(-15, 25, 8)      # x, y in cm, 8 cm above the table
"""

import argparse
import asyncio
import json
import math
import sys
import time

from arm.arm import BLOCKED_STEPS, GOAL_POSITION, GRIPPER_ID, SEND_RATE_HZ, Arm
from arm.kinematics import (MOTOR_JOINTS, TIP_OFFSET, TRIM_FILE, ArmModel, _apply, _ask,   # noqa: F401
                            joint_trim, roll_raw, tip_offset)
from arm.station import REPO_ROOT

from arm.vision_contract import GRIPPER_OPEN_CM, JAW_MARGIN_CM, width_fits
from arm.trajectory_timing import bounded_duration, ease

TABLE_FILE = REPO_ROOT / "calib" / "table.json"
IK_MOTORS = [1, 2, 3, 4, 5, 6]   # joint 7 (wrist roll) stays where it is, 8 is the gripper
DIR_WEIGHT = 0.10                # how much pointing-down error counts vs position error
POS_TOL = 0.003                  # 3 mm: a waypoint the solver can't reach this well is "unreachable"
DIR_TOL = 0.06                   # ~3.5 degrees
STEP_CM = 1.0                    # path waypoint spacing
MIN_TIP_HEIGHT = 0.5             # cm: lowest the fingertips may be asked to go
MIN_BODY_HEIGHT = 1.5            # cm: lowest any wrist/forearm joint may go
CLEARANCE_CM = 5.0               # approach this far above an object's top
MAX_AUTO_TILT = 80               # degrees: most the gripper may lean forward when chosen automatically
BASE_Y_CM = 4.2                  # the base's turning axis sits at x=0, y=4.2 cm in the model
SMALL_OBJECT_CM = 6.0            # below this: let the arm end a little low on the way down (no sag push-up)
GUARD_SPEED_CM_S = 2.5           # going down by feel: this slowly
GUARD_THRESH_CM = 0.6            # ... "met something" = the arm is this much higher than told, beyond its usual lag
GUARD_BLANK_S = 0.45             # ... not judged during the first moment (the arm is still getting going)
GUARD_BACKOFF_CM = 0.3           # ... then lift off what it met by this much before closing
GUARD_FROM_CM = 7.0              # ... starts feeling from this height (faster down to here)
GUARD_FLOOR_CM = -2.5            # ... gives up this far below where the table is thought to be
MAX_JOINT_SPEED = 700            # peak continuous reference steps/s (~60 deg/s), before integer rounding
STEPS_PER_RAD = 4096 / (2 * math.pi)


def _solve(a, b):
    """Solve a*x = b (small dense system, Gaussian elimination with pivoting)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        if abs(m[c][c]) < 1e-12:
            raise ValueError("singular")
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def _norm(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v)


class Table:
    """The table surface as a plane z = a*x + b*y + c (metres), fitted from touches."""

    def __init__(self, a, b, c, touches=None):
        self.a, self.b, self.c = a, b, c
        self.touches = list(touches or [])
        self.down = _norm((a, b, -1.0))  # direction pointing into the table

    @classmethod
    def fit(cls, pts):
        sx = [[sum(f(p) * g(p) for p in pts) for g in (lambda p: p[0], lambda p: p[1], lambda p: 1.0)]
              for f in (lambda p: p[0], lambda p: p[1], lambda p: 1.0)]
        rhs = [sum(f(p) * p[2] for p in pts) for f in (lambda p: p[0], lambda p: p[1], lambda p: 1.0)]
        return cls(*_solve(sx, rhs), touches=pts)

    @classmethod
    def load(cls):
        if not TABLE_FILE.exists():
            raise RuntimeError("Table not taught yet. Run: python -m arm.reach table  (mirroring ON)")
        d = json.loads(TABLE_FILE.read_text())
        return cls(d["a"], d["b"], d["c"], d.get("touches_m"))

    def save(self, pts):
        self.touches = [list(p) for p in pts]
        TABLE_FILE.parent.mkdir(exist_ok=True)
        TABLE_FILE.write_text(json.dumps(
            {"a": self.a, "b": self.b, "c": self.c, "touches_m": [list(p) for p in pts]}, indent=2) + "\n")

    def z(self, x, y):
        return self.a * x + self.b * y + self.c

    def height(self, p):
        """Height of a point above the table, metres."""
        return (p[2] - self.z(p[0], p[1])) / math.sqrt(self.a ** 2 + self.b ** 2 + 1)


class Unreachable(RuntimeError):
    pass


class Reacher:
    def __init__(self, arm: Arm, table: Table | None = None):
        self.arm = arm
        self.model = ArmModel()
        self.table = table or Table.load()
        self._lim = {j["name"]: j for j in self.model.chain}

    # ---------- joints <-> angles (true servo scale, see kinematics.angles_true) ----------

    def _mid(self, motor):
        j = self.arm.joints[motor]
        lim = self._lim[MOTOR_JOINTS[motor]]
        return (j.range_min + j.range_max) / 2, (lim["lower"] + lim["upper"]) / 2 + joint_trim(motor)

    def _angle(self, motor, raw):
        mid_raw, mid_ang = self._mid(motor)
        return mid_ang + (raw - mid_raw) / STEPS_PER_RAD

    def _raw(self, motor, angle):
        mid_raw, mid_ang = self._mid(motor)
        j = self.arm.joints[motor]
        return max(j.range_min, min(j.range_max, round(mid_raw + (angle - mid_ang) * STEPS_PER_RAD)))

    def _limits(self, motor):
        j = self.arm.joints[motor]
        margin = 0.01 * (j.range_max - j.range_min)
        return self._angle(motor, j.range_min + margin), self._angle(motor, j.range_max - margin)

    def current_angles(self):
        """Angles the arm is at now (its commanded target when holding, so paths start smoothly)."""
        out = {}
        for motor, name in MOTOR_JOINTS.items():
            j = self.arm.joints[motor]
            # holding: its target (minus any push that makes up for sag), else where it is
            sag = getattr(self.arm, "sag", {}).get(motor, 0) if j.torque else 0
            out[name] = self._angle(motor, (j.goal - sag) if j.torque else j.raw)
        return out

    # ---------- forward ----------

    def tip(self, angles):
        """(tip position, direction the gripper points, joint positions), metres."""
        m, pts = self.model.frames(angles)
        return _apply(m, tip_offset()), (m[0][1], m[1][1], m[2][1]), pts

    def state(self, angles=None):
        """x, y of the table point right below the tip (cm), height above the table (cm),
        and how far the gripper leans from straight down (degrees)."""
        p, d, _ = self.tip(angles or {n: self._angle(m, self.arm.joints[m].raw) for m, n in MOTOR_JOINTS.items()})
        cosang = max(-1.0, min(1.0, sum(a * b for a, b in zip(d, self.table.down))))
        h = self.table.height(p)
        foot = tuple(p[k] + self.table.down[k] * h for k in range(3))  # the table point right below the tip
        return foot[0] * 100, foot[1] * 100, h * 100, math.degrees(math.acos(cosang))

    # ---------- inverse ----------

    def _residual(self, angles, pos, direction):
        p, d, _ = self.tip(angles)
        return [pos[k] - p[k] for k in range(3)] + [DIR_WEIGHT * (direction[k] - d[k]) for k in range(3)]

    def solve(self, seed, pos, direction, iters=80):
        """Find joint angles putting the tip at `pos` pointing along `direction`, starting from `seed`."""
        q = dict(seed)
        names = [MOTOR_JOINTS[m] for m in IK_MOTORS]
        lims = [self._limits(m) for m in IK_MOTORS]
        for _ in range(iters):
            e = self._residual(q, pos, direction)
            perr = math.sqrt(sum(v * v for v in e[:3]))
            derr = math.sqrt(sum(v * v for v in e[3:])) / DIR_WEIGHT
            if perr < 0.0005 and derr < 0.01:
                break
            cols = []
            for n in names:  # numeric Jacobian, one column per joint
                q2 = dict(q)
                q2[n] += 1e-4
                e2 = self._residual(q2, pos, direction)
                cols.append([(e[k] - e2[k]) / 1e-4 for k in range(6)])
            jjt = [[sum(c[r] * c[s] for c in cols) + (1e-4 if r == s else 0.0) for s in range(6)] for r in range(6)]
            y = _solve(jjt, e)
            dq = [sum(c[k] * y[k] for k in range(6)) for c in cols]
            big = max(abs(v) for v in dq)
            scale = min(1.0, 0.15 / big) if big > 0 else 1.0
            for n, v, (lo, hi) in zip(names, dq, lims):
                q[n] = max(lo, min(hi, q[n] + v * scale))
        e = self._residual(q, pos, direction)
        perr = math.sqrt(sum(v * v for v in e[:3]))
        derr = math.sqrt(sum(v * v for v in e[3:])) / DIR_WEIGHT
        return q, perr, derr

    def _target(self, x_cm, y_cm, h_cm):
        """Point h cm above the table at table position x, y (metres)."""
        t = self.table
        x, y = x_cm / 100, y_cm / 100
        base = (x, y, t.z(x, y))
        return tuple(base[k] - t.down[k] * h_cm / 100 for k in range(3))

    def _direction(self, x_cm, y_cm, tilt_deg):
        """Straight down at tilt 0; tilted outwards (away from the base) by tilt_deg otherwise."""
        out = _norm((x_cm, y_cm - BASE_Y_CM, 0.0))
        th = math.radians(tilt_deg)
        return _norm(tuple(math.cos(th) * self.table.down[k] + math.sin(th) * out[k] for k in range(3)))

    def _seed(self):
        """Fixed starting guess (every joint mid-range), so the same target always gives the same arm shape."""
        seed = {MOTOR_JOINTS[m]: sum(self._limits(m)) / 2 for m in MOTOR_JOINTS}
        r = roll_raw()
        # wrist roll: to its straight position if "trim" saved one, else it stays as it is
        seed[MOTOR_JOINTS[7]] = self._angle(7, r) if r is not None else self.current_angles()[MOTOR_JOINTS[7]]
        return seed

    def pose_for(self, x_cm, y_cm, heights, tilt=None):
        """Joint angles for the tip at x, y at each of `heights`, all with the same gripper tilt.

        The arm can only point the gripper straight down close to its base; further out it
        has to lean forward. With tilt=None this picks the smallest lean that reaches every height.
        Returns (list of angle dicts, tilt in degrees).
        """
        if min(heights) < MIN_TIP_HEIGHT:
            raise Unreachable(f"Height {min(heights)} cm is below the minimum of {MIN_TIP_HEIGHT} cm above the table.")
        seed = self._seed()
        for th in ([tilt] if tilt is not None else range(0, MAX_AUTO_TILT + 1, 5)):
            d = self._direction(x_cm, y_cm, th)
            sols = []
            for h in heights:
                q, perr, derr = self.solve(seed, self._target(x_cm, y_cm, h), d, iters=150)
                if perr > 0.001 or derr > 0.02:
                    break
                sols.append(q)
            else:
                return sols, th
        what = f"with tilt {tilt}" if tilt is not None else f"even leaning {MAX_AUTO_TILT} degrees"
        raise Unreachable(f"Can't reach x={x_cm:.1f}, y={y_cm:.1f}, height={list(heights)} {what}. Too far, too close, or a joint limit.")

    def _check_floor(self, path, target_h_cm):
        """Floor rule: nothing from the forearm to the fingertips may dip towards the table."""
        t = self.table
        p0, _, pts0 = self.tip(self.current_angles())
        tip_floor = min(target_h_cm, t.height(p0) * 100) - 0.7
        start_h = [t.height(p) * 100 for p in pts0]
        for q in path:
            p, _, pts = self.tip(q)
            if t.height(p) * 100 < tip_floor:
                raise Unreachable(f"Path would take the fingertips down to {t.height(p) * 100:.1f} cm above the table. "
                                  "Go to a higher point first.")
            for k in range(6, len(pts)):
                h = t.height(pts[k]) * 100
                if h < min(MIN_BODY_HEIGHT, start_h[k] - 0.5):
                    raise Unreachable(f"Path would take the wrist/forearm down to {h:.1f} cm above the table. "
                                      "Go to a higher point first.")

    def plan_swing(self, q_target, target_h_cm, steps=24):
        """All joints glide from where they are to q_target together (the tip follows a curve)."""
        q0 = self.current_angles()
        path = [{n: q0[n] + (q_target[n] - q0[n]) * i / steps for n in q0} for i in range(1, steps + 1)]
        self._check_floor(path, target_h_cm)
        return path

    def plan_line(self, x_cm, y_cm, h_cm, partial=False, allow_below=False, step_cm=STEP_CM):
        """The tip travels in a straight line and the gripper keeps the direction it has now.
        For going down onto an object and lifting it. With partial=True, a line the arm can't
        finish returns the part it can do (for lifting: as high as possible) instead of failing.
        allow_below=True: the target may be below where the table is thought to be (only for
        descend_guarded, which stops by feel when the fingertips really touch)."""
        if h_cm < MIN_TIP_HEIGHT and not allow_below:
            raise Unreachable(f"Height {h_cm} cm is below the minimum of {MIN_TIP_HEIGHT} cm above the table.")
        target = self._target(x_cm, y_cm, h_cm)
        q = self.current_angles()
        p0, d0, _ = self.tip(q)
        steps = max(4, int(math.dist(p0, target) * 100 / step_cm))
        path = []
        for i in range(1, steps + 1):
            pos = tuple(p0[k] + (target[k] - p0[k]) * i / steps for k in range(3))
            q, perr, derr = self.solve(q, pos, d0)
            if perr > POS_TOL or derr > DIR_TOL:
                if partial and path:
                    break
                raise Unreachable(f"Can't follow a straight line to x={x_cm:.1f}, y={y_cm:.1f}, height={h_cm:g} "
                                  f"(stuck {i / steps:.0%} along the way). Too far, or a joint limit.")
            path.append(q)
        self._check_floor(path, h_cm)
        return path

    async def follow(self, path, seconds, correct_sag=True, feedforward=False):
        """Send a planned path to the servos, with a gentle start and stop.
        feedforward=True: add the sag push measured at the last pose to the whole path from the start,
        so the arm doesn't droop short at the end, without a correcting jerk once it gets there."""
        arm = self.arm
        ff = {m: arm.sag.get(m, 0) for m in IK_MOTORS} if feedforward else {}
        await arm.hold()  # also resets speed/accel, which mirroring may have changed
        motors = list(MOTOR_JOINTS)
        start = {m: (arm.joints[m].goal if arm.joints[m].torque else arm.joints[m].raw) for m in motors}

        def raw_ff(m, q):
            j = arm.joints[m]
            return max(j.range_min, min(j.range_max, self._raw(m, q[MOTOR_JOINTS[m]]) + ff.get(m, 0)))
        way = [start] + [{m: raw_ff(m, q) for m in motors} for q in path]
        cmd = IK_MOTORS + [7] if roll_raw() is not None else IK_MOTORS   # joint 7 only once "trim" set it
        way = [{m: w[m] for m in cmd} for w in way]
        end = {m: self._raw(m, path[-1][MOTOR_JOINTS[m]]) for m in cmd}   # where the joints should really end
        n = len(way) - 1
        # Bound peak speed even with uneven waypoint spacing; average travel was
        # insufficient. The quintic easing also starts/ends with zero acceleration.
        seconds = bounded_duration(way, seconds, MAX_JOINT_SPEED)
        t0 = time.monotonic()
        while True:
            s = min(1.0, (time.monotonic() - t0) / max(seconds, 0.2))
            s = ease(s)
            f = s * n
            i = min(n - 1, int(f))
            u = f - i
            goals = {m: round(way[i][m] + (way[i + 1][m] - way[i][m]) * u) for m in cmd}
            await arm._write([(m, GOAL_POSITION, g.to_bytes(2, "little")) for m, g in goals.items()])
            if s >= 1.0:
                break
            await asyncio.sleep(1 / SEND_RATE_HZ)
        if ff:
            await arm._settle_and_ease(end, correct_sag, aim=way[-1])
        else:
            await arm._settle_and_ease(way[-1], correct_sag)
        off = [m for m in IK_MOTORS if abs(arm.joints[m].raw - end[m]) > 150]
        if len(off) >= 2:
            raise RuntimeError(
                f"The arm stopped before its target (joints {off} are far off). Either something blocks it "
                "(table, object, cable), or the follower is on mirroring instead of (Self-controlled).")

    async def lift(self, x_cm, y_cm, h_cm, seconds=None, cm_per_s=5.0):
        """Go up to h_cm above x, y, e.g. after closing on an object.

        Straight up keeps the gripper's current angle. After closing on something, that angle is
        often a few degrees off the plan (the load pulls the arm down), and the top of the line
        can then be out of reach. So if the straight line can't be done whole: straight up as far
        as it goes, then the rest of the way with whatever lean reaches it."""
        try:
            up = self.plan_line(x_cm, y_cm, h_cm)
        except Unreachable:
            up = None
        if up is not None:   # the normal case: straight up all the way
            await self.follow(up, seconds or self.duration(up))
            return
        start_h = self.state()[2]
        up = self.plan_line(x_cm, y_cm, h_cm, partial=True)
        await self.follow(up, max(1.5, (h_cm - start_h) / cm_per_s))
        now_h = self.state()[2]
        if now_h >= h_cm - 0.7:
            return
        try:
            (q,), _ = self.pose_for(x_cm, y_cm, [h_cm])
            rest = self.plan_swing(q, h_cm)
        except Unreachable:
            if now_h - start_h >= 3.0:   # well off the table: good enough, stay here
                return
            raise
        await self.follow(rest, max(1.0, (h_cm - now_h) / cm_per_s))

    # ---------- what the encoders say ----------

    def measured_angles(self):
        """Joint angles from the encoders (where the arm really is, not where it was told to go)."""
        return {n: self._angle(m, self.arm.joints[m].raw) for m, n in MOTOR_JOINTS.items()}

    def pose(self):
        """Where the gripper really is, for other programs (the gripper camera's maths):
        the gripper's 4x4 matrix in the arm's frame (metres), the tip, the table, the raw joint readings."""
        q = self.measured_angles()
        m, _ = self.model.frames(q)
        x, y, h, tilt = self.state(q)
        t = self.table
        return {"T_base_gripper": [[float(v) for v in row] for row in m], "tip_cm": [x, y, h], "tilt_deg": tilt,
                "tip_offset_m": list(tip_offset()), "table": {"a": t.a, "b": t.b, "c": t.c},
                "joints_raw": {str(i): j.raw for i, j in self.arm.joints.items()},
                "joints_goal": {str(i): j.goal for i, j in self.arm.joints.items()},
                "torque": all(j.torque for j in self.arm.joints.values())}

    def model_info(self):
        """Everything another program needs to redo this arm's forward kinematics itself."""
        chain = [{"name": j["name"], "type": j["type"], "origin": [[float(v) for v in row] for row in j["origin"]],
                  "axis": j["axis"],
                  "lower": j["lower"], "upper": j["upper"],
                  "motor": self.model.motor_of.get(j["name"])} for j in self.model.chain]
        joints = {str(m): {"range_min": self.arm.joints[m].range_min, "range_max": self.arm.joints[m].range_max,
                           "trim_deg": math.degrees(joint_trim(m))} for m in MOTOR_JOINTS}
        return {"chain": chain, "joints": joints, "tip_offset_m": list(tip_offset()),
                "tip_offset_nominal_m": list(TIP_OFFSET), "steps_per_rad": STEPS_PER_RAD, "roll_raw": roll_raw()}

    # ---------- going down by feel ----------

    async def descend_guarded(self, x_cm, y_cm, floor_cm, speed=None, backoff=None, report=None):
        """Straight down from where the tip is, slowly, towards floor_cm above the table, and stop at
        the first thing the gripper meets: the table under the fingertips, or the top of an object
        under the palm. Then lift off that by `backoff` cm and hold there.

        How it feels: it compares, 25 times a second, the height the arm was told to be at with the
        height the encoders say it is at. While moving freely the two stay a steady small distance
        apart. When that distance suddenly grows, something is holding the gripper up.

        floor_cm may be below 0 (to find a table that lies lower than expected). If nothing is met,
        the arm ends at floor_cm (or as low as it can reach).
        Returns {"contact": bool, "height_cm": where it stopped by the arm's own reading, before lifting off,
                 "final_cm": where it holds now}."""
        arm = self.arm
        report = report if report is not None else {}
        report.update(requested_height_cm=floor_cm, samples=[], compensation='taper_to_zero')
        speed = speed or GUARD_SPEED_CM_S
        backoff = GUARD_BACKOFF_CM if backoff is None else backoff
        t = self.table
        # A normal pickup needs the complete closing-height path. Partial paths
        # remain available only for the explicit below-table touch/search tool.
        path = self.plan_line(x_cm, y_cm, floor_cm, partial=floor_cm < MIN_TIP_HEIGHT,
                              allow_below=True, step_cm=0.5)
        ff = {m: arm.sag.get(m, 0) for m in IK_MOTORS}       # what the arm needs to hold itself up, from the last stop
        await arm.hold()
        cmd = IK_MOTORS + [7] if roll_raw() is not None else IK_MOTORS
        h_from = t.height(self.tip(self.current_angles())[0]) * 100
        heights = [h_from] + [t.height(self.tip(q)[0]) * 100 for q in path]            # told-to-be heights along the way
        start = {m: (arm.joints[m].goal if arm.joints[m].torque else arm.joints[m].raw) for m in cmd}
        true = [{m: start[m] - ff.get(m, 0) for m in cmd}] + [{m: self._raw(m, q[MOTOR_JOINTS[m]]) for m in cmd} for q in path]
        n = len(true) - 1
        # Hover compensation is pose-dependent: carrying the bottle run's -57
        # shoulder steps all the way down raised its final motor target ~2.9 cm.
        # Preserve the starting reference, then smoothly remove that old offset.
        way = [{m: max(arm.joints[m].range_min, min(arm.joints[m].range_max,
                    round(p[m] + ff.get(m, 0) * (1 - ease(i / n))))) for m in cmd}
               for i, p in enumerate(true)]
        base_angles = self.current_angles()

        def command_height(goals):
            angles = dict(base_angles)
            angles.update({MOTOR_JOINTS[m]: self._angle(m, g) for m, g in goals.items()})
            return t.height(self.tip(angles)[0]) * 100

        command_heights = [command_height(p) for p in way]
        report.update(start_sag_steps=ff, planned_endpoint_cm=heights[-1],
                      final_motor_target_cm=command_heights[-1])

        def goals_at(f):
            i = min(len(true) - 2, int(f))
            u = f - i
            out = {}
            for m in cmd:
                j = arm.joints[m]
                v = way[i][m] + (way[i + 1][m] - way[i][m]) * u
                out[m] = int(max(j.range_min, min(j.range_max, round(v))))
            return out, heights[i] + (heights[i + 1] - heights[i]) * u

        total = max(0.2, (heights[0] - heights[-1]) / speed)
        largest = max(abs(b[m] - a[m]) for a, b in zip(way, way[1:]) for m in cmd)
        total = max(total, n * largest / MAX_JOINT_SPEED)
        t0, seen_t = time.monotonic(), None
        hist, over, contact, h_stop = [], 0, False, None
        while True:
            el = time.monotonic() - t0
            s = min(1.0, el / total)
            goals, h_told = goals_at(s * n)
            await arm._write([(m, GOAL_POSITION, g.to_bytes(2, "little")) for m, g in goals.items()])
            await asyncio.sleep(1 / SEND_RATE_HZ)
            if arm.last_update != seen_t:
                seen_t = arm.last_update
                h_is = self.state()[2]
                report['samples'].append({'elapsed_s': round(el, 4), 'target_cm': h_told,
                    'motor_target_cm': command_height(goals), 'tip_cm': list(self.state()[:3]),
                    'joints_raw': {str(m): arm.joints[m].raw for m in MOTOR_JOINTS},
                    'joints_command': {str(m): g for m, g in goals.items()}})
                gap = h_is - h_told                      # how far above its told height the arm is
                hist.append((el, gap))
                if el > GUARD_BLANK_S:
                    # its usual lag: the last half second, leaving out the start-up and the newest readings
                    usual = [g for tt, g in hist if tt >= 0.25 and el - 0.7 <= tt <= el - 0.12]
                    if usual and gap - min(usual) > GUARD_THRESH_CM:
                        over += 1
                    else:
                        over = 0
                    if over >= 2:
                        contact, h_stop = True, h_is
                        break
            if s >= 1.0:
                break
        if contact:
            # hold a little above where it met something: the point of the way down with that height
            want = h_stop + backoff
            f = float(min(range(n + 1), key=lambda i: abs(command_heights[i] - want)))
            for i in range(n):
                if command_heights[i] >= want >= command_heights[i + 1]:
                    f = i + (command_heights[i] - want) / max(1e-6, command_heights[i] - command_heights[i + 1])
                    break
            goals, _ = goals_at(f)
            await arm._write([(m, GOAL_POSITION, g.to_bytes(2, "little")) for m, g in goals.items()])
            await arm._settle_and_ease(goals, False, aim=goals)
        else:
            end = true[-1]
            goals, _ = goals_at(float(n))
            # Do not add a new blind shoulder push just before closing. Retain
            # the friend's settling/blocked-joint relief, without extra sag pulses.
            await arm._settle_and_ease(end, False, aim=goals)
            h_stop = self.state()[2]
        report.update(contact=contact, final_cm=round(self.state()[2], 2),
                      commanded_stop_height_cm=command_height(goals),
                      stop_reason=('contact_inferred' if contact else
                                   'partial_path_complete' if abs(heights[-1] - floor_cm) > .3 else 'planned_path_complete'),
                      height_error_cm=round(self.state()[2] - floor_cm, 2))
        return {"contact": contact, "height_cm": round(h_stop, 2), "final_cm": round(self.state()[2], 2)}

    def plan(self, x_cm, y_cm, h_cm, tilt=None, line=False):
        """Returns (path, tilt used or None for a straight line)."""
        if line:
            return self.plan_line(x_cm, y_cm, h_cm), None
        (q,), tilt = self.pose_for(x_cm, y_cm, [h_cm], tilt)
        return self.plan_swing(q, h_cm), tilt

    def duration(self, path):
        """About 8 cm/s at the fingertips, at least 1.5 s."""
        return max(1.5, math.dist(self.tip(self.current_angles())[0], self.tip(path[-1])[0]) * 100 / 8)

    async def goto(self, x_cm, y_cm, h_cm, tilt=None, line=False, seconds=None):
        path, tilt = self.plan(x_cm, y_cm, h_cm, tilt, line)
        await self.follow(path, seconds or self.duration(path))
        return tilt


    @staticmethod
    def check_width(jaws_cm):
        """Refuse objects the gripper can't open around."""
        if jaws_cm is not None and not width_fits(jaws_cm):
            raise Unreachable(f"The object is {jaws_cm:g} cm wide but the gripper opens only {GRIPPER_OPEN_CM:g} cm "
                              f"(needs {JAW_MARGIN_CM:g} cm to spare). Pick a narrower object or a narrower part of it.")

    async def grab(self, x_cm, y_cm, grab_h=4.0, above_h=12.0, open_pct=100.0, close_pct=0.0, confirm=None,
                   object_h=None, guard=False, report=None):
        """Pick up whatever stands at x, y: open, go above, straight down, close, straight up.

        grab_h is how high above the table the fingertips are when they close.
        Returns True if the gripper closed on something.

        guard=True: the last part of the way down is done by feel (descend_guarded). With grab_h=None
        the gripper goes down until it meets something: the table (small objects: the fingertips end
        right at the table, the lowest and safest grip) or the object's top under the palm (tall
        objects). So neither the table's exact height nor the object's height has to be known.
        `report` (a dict) is filled with what happened, for printing.
        """
        arm = self.arm
        report = report if report is not None else {}
        plan_h = grab_h if grab_h is not None else MIN_TIP_HEIGHT
        (q_above, _), tilt = self.pose_for(x_cm, y_cm, [above_h, plan_h])
        swing = self.plan_swing(q_above, above_h)
        if confirm:
            await confirm(tilt)
        await arm.move({GRIPPER_ID: open_pct}, 0.8)
        await self.follow(swing, self.duration(swing))
        report["tilt_deg"] = tilt
        if guard:
            # fast and exact down to a height that clears most things, then slowly by feel
            mid = min(above_h, max(GUARD_FROM_CM, plan_h + 2.0))
            if above_h - mid > 0.5:
                await self.follow(self.plan_line(x_cm, y_cm, mid), max(1.0, (above_h - mid) / 6))
            report['descent'] = {}
            felt = await self.descend_guarded(x_cm, y_cm, grab_h if grab_h is not None else GUARD_FLOOR_CM,
                                               report=report['descent'])
            report.update(contact=felt["contact"], met_at_cm=felt["height_cm"], closed_at_cm=felt["final_cm"])
        else:
            down = self.plan_line(x_cm, y_cm, grab_h)
            # Going down, depending on the object (object_h, cm; None = unknown, treated as small):
            #  small: no sag correction. Pushing the arm back up right before closing lifts the jaws off a
            #         small object; ending a little low is what it needs.
            #  big:   sag correction on, so the jaws close at the planned height, mid-object.
            small = object_h is None or object_h < SMALL_OBJECT_CM
            # small: the push the arm needed hovering above (sag) is added to the way down from the start
            await self.follow(down, max(1.5, (above_h - grab_h) / 5), correct_sag=not small,
                              feedforward=small)   # about 5 cm/s
        mx, my, mh, _ = self.state()                 # where the encoders say the tip ended up
        report.update(tip_cm=[round(mx, 2), round(my, 2), round(mh, 2)],
                      tip_off_cm=round(math.hypot(mx - x_cm, my - y_cm), 2))
        holding, open_now = await arm.close_gripper(close_pct)    # stops when the jaws meet the object
        report.update(jaws_open_pct=open_now, jaws_gap_cm=round(GRIPPER_OPEN_CM * open_now / 100, 1))
        lift_from = self.state()[2]
        await self.lift(x_cm, y_cm, above_h, seconds=max(1.5, (above_h - lift_from) / 5))   # about 5 cm/s
        if holding and not arm.gripper_still_holding():
            report["slipped"] = True                 # had it, lost it on the way up
            holding = False
        return holding


    async def grab_side(self, x_cm, y_cm, grab_h=4.0, lift_cm=6.0, open_pct=100.0, close_pct=0.0, confirm=None, approach_h=None):
        """Pick up from the side: gripper (nearly) horizontal at grab_h, slide in from the
        arm's side of the object, close, lift straight up by lift_cm.
        Returns True if the gripper closed on something.
        """
        arm = self.arm
        approach_h = grab_h + 5 if approach_h is None else approach_h
        if not math.isfinite(approach_h) or approach_h <= grab_h or not math.isfinite(lift_cm) or lift_cm <= 0:
            raise Unreachable("Side approach must be above the grasp and lift must be positive")
        ox, oy = x_cm, y_cm - BASE_Y_CM                     # direction from the base to the object
        n = math.hypot(ox, oy)
        if n < 1:
            raise Unreachable("The object is right at the base.")
        ox, oy = ox / n, oy / n
        plan = None
        for tilt in (90, 80, 70, 60):                        # as horizontal as the arm can manage
            for back in (7.0, 6.0, 5.0):                     # start this far back from the object
                sx, sy = x_cm - ox * back, y_cm - oy * back
                try:
                    (q_hi,), _ = self.pose_for(sx, sy, [approach_h], tilt)
                    self.pose_for(sx, sy, [grab_h], tilt)
                    self.pose_for(x_cm, y_cm, [grab_h, grab_h + lift_cm], tilt)
                    plan = (tilt, back, sx, sy, q_hi)
                    break
                except Unreachable:
                    continue
            if plan:
                break
        if not plan:
            raise Unreachable(f"Can't reach x={x_cm:.1f}, y={y_cm:.1f} from the side at {grab_h:g} cm. Try from the top.")
        tilt, back, sx, sy, q_hi = plan
        swing = self.plan_swing(q_hi, approach_h)
        if confirm:
            await confirm(tilt)
        await arm.move({GRIPPER_ID: open_pct}, 0.8)
        await self.follow(swing, self.duration(swing))                       # beside the object, a bit high
        await self.follow(self.plan_line(sx, sy, grab_h), 1.5, correct_sag=False)   # down to the closing height
        await self.follow(self.plan_line(x_cm, y_cm, grab_h), max(1.5, back / 4))   # slide in, ~4 cm/s
        await arm.move({GRIPPER_ID: close_pct}, 1.0)
        await asyncio.sleep(0.3)
        g = arm.joints[GRIPPER_ID]
        holding = abs(g.raw - g.raw_for(close_pct)) > BLOCKED_STEPS
        await self.lift(x_cm, y_cm, grab_h + lift_cm, seconds=1.5)
        return holding


# ---------- command line ----------

async def _main(args):
    async with Arm() as arm:
        print(f"Connected to follower {arm.bus}")
        if args.cmd == "table":
            model = ArmModel()
            print("\nTeach the table: with the LEADER (mirroring ON), touch the bare table with the closed")
            print("fingertips, gripper pointing straight down, at 4 spots spread over the pickup area")
            print("(near-left, near-right, far-right, far-left). Light touch, hold still, Enter.\n")
            pts = []
            for k in range(4):
                await _ask(f"Touch spot {k + 1} of 4, then Enter... ")
                samples = []
                for _ in range(15):
                    samples.append(model.forward(angles=model.angles_true(dict(arm.joints)))[0])
                    await asyncio.sleep(0.03)
                p = tuple(sum(s[i] for s in samples) / len(samples) for i in range(3))
                pts.append(p)
                print(f"   x={p[0] * 100:6.1f}  y={p[1] * 100:6.1f}  z={p[2] * 100:5.1f} cm")
            table = Table.fit(pts)
            off = [abs(table.height(p)) * 100 for p in pts]
            table.save(pts)
            tilt = math.degrees(math.acos(-table.down[2]))
            print(f"\nSaved {TABLE_FILE.relative_to(REPO_ROOT)}. Table seen as tilted {tilt:.1f} degrees;"
                  f" touches are within {max(off):.1f} cm of the fitted surface.")
            if max(off) > 1.0:
                print("That's a lot: one touch was probably pressed down or not on the table. Run it again.")
            return

        r = Reacher(arm)
        if args.cmd == "where":
            while True:
                x, y, h, tilt = r.state()
                print(f"\rx={x:6.1f}  y={y:6.1f}  height={h:5.1f} cm  tilt={tilt:4.0f} deg   ", end="", flush=True)
                await asyncio.sleep(0.1)
        elif args.cmd == "goto":
            x, y, h, tilt = r.state()
            print(f"Now:    x={x:6.1f}  y={y:6.1f}  height={h:5.1f} cm  tilt={tilt:.0f} deg")
            print(f"Target: x={args.x:6.1f}  y={args.y:6.1f}  height={args.height:5.1f} cm")
            path, used = r.plan(args.x, args.y, args.height, args.tilt, args.line)
            how = "straight line, gripper keeps its direction" if args.line else f"gripper leaning {used} deg from straight down"
            print(f"Path OK ({how}; nothing goes below the table).")
            if not args.yes:
                await _ask("Follower on (Self-controlled)? Press Enter to move, Ctrl+C to cancel... ")
            await r.follow(path, args.seconds or r.duration(path))
            await asyncio.sleep(0.3)
            x, y, h, tilt = r.state()
            print(f"Arrived: x={x:6.1f}  y={y:6.1f}  height={h:5.1f} cm  tilt={tilt:.0f} deg"
                  f"   (off by {math.hypot(x - args.x, y - args.y, h - args.height):.1f} cm by the arm's own reckoning)")


        elif args.cmd == "grab":
            async def confirm(tilt):
                print(f"Plan OK: open, go {args.above:g} cm above x={args.x:g} y={args.y:g}, straight down to "
                      f"{args.height:g} cm, close, lift. Gripper leaning {tilt} deg.")
                if not args.yes:
                    await _ask("Follower on (Self-controlled)? Press Enter to grab, Ctrl+C to cancel... ")
            Reacher.check_width(args.jaws)
            above = args.above if args.above is not None else (
                args.object_height + CLEARANCE_CM if args.object_height else 12.0)
            args.above = above
            if args.side:
                async def confirm_side(tilt):
                    print(f"Plan OK: open, come in from the side at {args.height:g} cm (gripper {tilt} deg from "
                          f"vertical), close, lift.")
                    if not args.yes:
                        await _ask("Follower on (Self-controlled)? Press Enter to grab, Ctrl+C to cancel... ")
                holding = await r.grab_side(args.x, args.y, args.height, 6.0, args.open, args.close, confirm_side)
            else:
                holding = await r.grab(args.x, args.y, args.height, above, args.open, args.close, confirm)
            print("Got it: the gripper is holding something." if holding
                  else "The gripper closed completely: nothing in it.")


def main():
    p = argparse.ArgumentParser(prog="python -m arm.reach", description="Move the gripper tip to a point")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("table", help="teach where the table is (mirroring ON)")
    sub.add_parser("where", help="live tip position and height above the table")
    g = sub.add_parser("goto", help="move the tip to X Y HEIGHT (cm)")
    g.add_argument("x", type=float)
    g.add_argument("y", type=float)
    g.add_argument("height", type=float, help="cm above the table")
    g.add_argument("--tilt", type=float, default=None, help="gripper lean from straight down, degrees (default: smallest that reaches)")
    g.add_argument("--line", action="store_true", help="tip moves in a straight line, gripper keeps its direction (for going down / lifting)")
    g.add_argument("--seconds", type=float, default=None, help="duration (default: about 8 cm/s)")
    g.add_argument("-y", "--yes", action="store_true", help="don't ask before moving")
    gr = sub.add_parser("grab", help="pick up what stands at X Y (cm)")
    gr.add_argument("x", type=float)
    gr.add_argument("y", type=float)
    gr.add_argument("--height", type=float, default=4.0, help="fingertip height above the table when closing, cm (default 4)")
    gr.add_argument("--above", type=float, default=None, help="height to approach from and lift to, cm (default 12, or object height + 5)")
    gr.add_argument("--object-height", type=float, default=None, help="object's height, cm (sets the approach height)")
    gr.add_argument("--jaws", type=float, default=None, help="object's width at the grip, cm (refused if too wide)")
    gr.add_argument("--side", action="store_true", help="come in from the side instead of from the top")
    gr.add_argument("--open", type=float, default=100.0, help="gripper opening before going down, %% (default 100)")
    gr.add_argument("--close", type=float, default=0.0, help="gripper target when closing, %% (default 0)")
    gr.add_argument("-y", "--yes", action="store_true", help="don't ask before moving")
    args = p.parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        print()
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
