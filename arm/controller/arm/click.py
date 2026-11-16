"""Click on the camera image, the arm grabs what's there.

A small web page (http://localhost:8890) showing the camera image that Station
records. Two jobs:

  1. Calibrate (once, mirroring ON, camera fixed): touch a mark on the table with
     the gripper tip, press "Record arm position", move the arm out of the way,
     click the same mark in the image. After 4+ marks it knows how to turn any
     pixel on the table into table x, y.
  2. Grab (follower Self-controlled): click where an object STANDS on the table
     (its base, not its top), press "Grab".
  3. Place: click a spot on the table, press "Place here".
  4. Park: back to the saved park pose ("Save current as park" sets it), keeping the grip.

HTTP API (used by the page, and by other programs such as pick_v2.py):
  GET  /status       calibration marks, image size, busy flag, last message
  GET  /frame.jpg    the current camera image
  POST /api/grab     {"u", "v", "height", "above"}       pixel of the object's base, or from object detection:
                     {"x", "y", "z", "jaws", "object_height"}   cm: table position, closing height,
                                                         object width at the jaws, object height
                     add "method": "side" to come in from the side (default "top")
  POST /api/place    {"x", "y", "height", "above"}       table point in cm (or "u", "v")
  POST /api/park     {}                                  back to poses/park.json, gripper unchanged
  POST /api/goto     {"x", "y", "height", "tilt"}        move the fingertips there (tilt optional) -> {"tilt"}
  POST /api/touch_down {"x", "y"}                        lower the tip until it touches -> where the table is
  GET  /pose         where the gripper really is (from the encoders): 4x4 matrix, tip, table
  GET  /model        the arm's geometry and joint ranges
  /api/grab also takes "guard": true (last part of the way down by feel) and "feel_all_the_way": true
  (no closing height: down until the gripper meets the table or the object's top).

Run from the repo root:
  python -m arm.click --camera /dev/video4     read the camera directly, sharp (recommended)
  python -m arm.click                          use Station's small 224x224 recording image
  add --host 0.0.0.0 to let another laptop use it (e.g. a vision script running there)

Reading the camera directly only works if Station is NOT using that camera
(see `v4l2-ctl --list-devices` for the device name).
"""

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import parse_qs, urlparse

from arm.arm import GRIPPER_ID, Arm, load_pose, save_pose
from arm.kinematics import MOTOR_JOINTS, ArmModel
from arm.vision_contract import capabilities, normalize_grab, MAX_JOINT_AGE_S
from arm.observations import ObservationStore, measured_pose
from arm.reach import CLEARANCE_CM, Reacher, Table, Unreachable, _solve
from arm.station import REPO_ROOT
from target.gen_python.protobuf.drivers.inferences import normvla

PORT = 8890
CAMERA_FILE = REPO_ROOT / "calib" / "camera.json"
QUEUE = "inference/normvla"


def save_grasp_report(report):
    """Local diagnostics only; a disk error must not hide a motion error."""
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '_' + uuid4().hex[:8]
    folder = REPO_ROOT.parent / 'config' / 'local' / 'grasp_runs'
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (run_id + '.json')
        path.write_text(json.dumps({'run_id': run_id, **report}, indent=2) + '\n')
        print('Grasp report:', path)
        return str(path)
    except OSError as exc:
        print(f'Could not save grasp report: {exc}', file=sys.stderr)
        return None


# ---------- pixel -> table position ----------

def fit_homography(pairs):
    """pairs: [(u, v, x, y)]. Returns the 3x3 matrix (as 9 numbers) mapping pixel -> table x, y."""
    rows, rhs = [], []
    for u, v, x, y in pairs:
        rows.append([u, v, 1, 0, 0, 0, -x * u, -x * v]); rhs.append(x)
        rows.append([0, 0, 0, u, v, 1, -y * u, -y * v]); rhs.append(y)
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(8)] for i in range(8)]
    atb = [sum(r[i] * b for r, b in zip(rows, rhs)) for i in range(8)]
    return _solve(ata, atb) + [1.0]


def apply_homography(h, u, v):
    w = h[6] * u + h[7] * v + h[8]
    return (h[0] * u + h[1] * v + h[2]) / w, (h[3] * u + h[4] * v + h[5]) / w


class Camera:
    """Calibration of one fixed camera: pairs of (pixel, table x y in cm) and the fitted mapping."""

    def __init__(self):
        self.cam = 0
        self.size = 224
        self.pairs = []   # [u, v, x_cm, y_cm]
        self.h = None
        if CAMERA_FILE.exists():
            d = json.loads(CAMERA_FILE.read_text())
            self.cam, self.size, self.pairs = d["cam"], d.get("size", 224), d["pairs"]
            self._fit()

    def _norm(self, u, v):
        return u / self.size, v / self.size

    BOARD_MARKS = 54   # one checkerboard position from vision/calibrate_board.py (9 x 6 corners)

    def _fit(self):
        self.h = None
        self.boards = []
        if len(self.pairs) >= 4:
            try:
                self.h = fit_homography([(*self._norm(u, v), x, y) for u, v, x, y in self.pairs])
            except ValueError:
                pass  # marks in a line: no solution yet
        # Several checkerboard positions: give each its own mapping for the area it covers. The arm's
        # own error differs a little from one table area to another (it bends differently there), so
        # one mapping for the whole picture can't fit them all; locally the errors cancel out.
        n = self.BOARD_MARKS
        if len(self.pairs) >= 2 * n and len(self.pairs) % n == 0:
            for k in range(0, len(self.pairs), n):
                block = self.pairs[k:k + n]
                try:
                    h = fit_homography([(*self._norm(u, v), x, y) for u, v, x, y in block])
                except ValueError:
                    self.boards = []
                    break
                us, vs = [p[0] for p in block], [p[1] for p in block]
                self.boards.append((h, min(us), max(us), min(vs), max(vs)))

    BLEND_PX = 18.0    # within a board its own mapping dominates; between boards it blends over this scale

    def _board_weights(self, u, v):
        """How much each board's mapping counts at pixel (u, v): ~1 on its own area, smoothly shared
        between neighbours in the gaps (inverse distance to each board's area)."""
        w = []
        for _, u0, u1, v0, v1 in self.boards:
            du = max(u0 - u, 0, u - u1)
            dv = max(v0 - v, 0, v - v1)
            w.append(1.0 / (du * du + dv * dv + self.BLEND_PX ** 2) ** 2)
        total = sum(w)
        return [x / total for x in w]

    def save(self):
        CAMERA_FILE.parent.mkdir(exist_ok=True)
        CAMERA_FILE.write_text(json.dumps({"cam": self.cam, "size": self.size, "pairs": self.pairs}, indent=2) + "\n")

    def add(self, u, v, x, y):
        self.pairs.append([u, v, round(x, 2), round(y, 2)])
        self._fit()
        self.save()

    def locate(self, u, v):
        if self.h is None:
            raise RuntimeError("Camera not calibrated yet: needs at least 4 marks (not all in one line).")
        if not self.boards:
            return apply_homography(self.h, *self._norm(u, v))
        x = y = 0.0
        for wt, (h, *_) in zip(self._board_weights(u, v), self.boards):
            bx, by = apply_homography(h, *self._norm(u, v))
            x, y = x + wt * bx, y + wt * by
        return x, y

    def errors(self):
        """How far each mark lands from where the arm touched it, cm."""
        if self.h is None:
            return []
        out = []
        for u, v, x, y in self.pairs:
            lx, ly = self.locate(u, v)
            out.append(round(math.hypot(lx - x, ly - y), 2))
        return out


# ---------- the app ----------

class App:
    def __init__(self, arm, device=None, size="1280x720", hand_device=None, stand_index=0, hand_index=1, read_only=False):
        self.arm = arm
        self.device = device
        self.hand_device = hand_device
        self.read_only = read_only
        self.camera_roles = {stand_index: "stand", hand_index: "hand"}
        self.observations = ObservationStore()
        self.observation_model = None
        self.video_size = size
        self.camera = Camera()
        self.reacher = None
        self.jpegs = []
        self.pending = None      # arm position waiting for its click
        self.busy = False
        self.log = "Ready."

    def reach(self):
        if self.reacher is None:
            self.reacher = Reacher(self.arm)  # needs the table: python -m arm.reach table
        return self.reacher

    def record_frames(self, frames, source):
        now = time.monotonic()
        try:
            if self.observation_model is None:
                self.observation_model = ArmModel()
            pose = measured_pose(dict(self.arm.joints), dict(self.arm.joint_updated_at), self.observation_model, now)
        except (RuntimeError, FileNotFoundError, ValueError) as exc:
            pose = {"pose_valid": False, "T_base_gripper": None, "errors": [str(exc)]}
        for index, jpeg in frames.items():
            while len(self.jpegs) <= index:
                self.jpegs.append(b"")
            self.jpegs[index] = jpeg
            role = self.camera_roles.get(index)
            if role:
                self.observations.record(f"{source}:{index}", role, jpeg, pose, now)

    async def direct_frames(self, cmd=None, device=None, index=0):
        """Read the camera ourselves through ffmpeg, at full resolution."""
        device = device or self.device
        for copy in (True, False):  # first take the camera's own JPEGs untouched; else let ffmpeg encode
            args = cmd or (["ffmpeg", "-loglevel", "error", "-fflags", "nobuffer", "-f", "v4l2"]
                           + (["-input_format", "mjpeg"] if copy else [])
                           + ["-video_size", self.video_size, "-i", device]
                           + (["-c:v", "copy"] if copy else ["-c:v", "mjpeg", "-q:v", "3"])
                           + ["-f", "image2pipe", "-"])
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            buf, got = b"", 0
            try:
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:  # cut complete JPEGs (FFD8 ... FFD9) out of the stream
                        a = buf.find(b"\xff\xd8")
                        b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                        if b < 0:
                            break
                        self.record_frames({index: buf[a:b + 2]}, device)
                        buf = buf[b + 2:]
                        got += 1
            finally:
                if proc.returncode is None:
                    proc.kill()
            err = (await proc.stderr.read()).decode(errors="replace").strip().splitlines()
            if got or cmd:
                self.log = "Camera stream stopped: " + (err[-1] if err else "ffmpeg ended")
                return
            self.camera_error = err[-1] if err else "ffmpeg gave no image"
            print(f"Camera {self.device} ({'direct' if copy else 're-encoded'}): {self.camera_error}", file=sys.stderr)
        self.log = f"Can't read {self.device}: {self.camera_error}. Is Station or a camera app using it?"
        print(self.log, file=sys.stderr)

    async def frames(self):
        """Keep the latest camera images from Station's recording feed."""
        if self.device:
            if self.hand_device:
                return await asyncio.gather(self.direct_frames(), self.direct_frames(device=self.hand_device, index=1))
            return await self.direct_frames()
        while True:
            try:
                qr = self.arm._client.read_from_tail(QUEUE, offset=b"\x00", limit=1, step=1, buf_size=1)
                entry = await asyncio.wait_for(qr.data.get(), timeout=2.0)
                if entry is not None:
                    frame = normvla.FrameReader(memoryview(bytes(entry.Data)))
                    self.record_frames({i: bytes(img.get_jpeg()) for i, img in enumerate(frame.get_images() or [])}, "station")
            except Exception:  # no frame right now: keep the last one
                pass
            await asyncio.sleep(0.15)

    def status(self):
        d = {"log": self.log, "busy": self.busy, "cam": self.camera.cam, "cams": len(self.jpegs),
             "pairs": self.camera.pairs, "errors": self.camera.errors(),
             "calibrated": self.camera.h is not None, "pending": self.pending,
             "size": self.camera.size, "direct": bool(self.device)}
        try:
            x, y, h, tilt = self.reach().state()
            d["tip"] = [round(x, 1), round(y, 1), round(h, 1)]
        except RuntimeError as e:
            d["tip"] = None
            d["log"] = str(e)
        d["capabilities"] = capabilities()
        table = getattr(self.reacher, "table", None)
        d["table_calibration"] = ({"a": table.a, "b": table.b, "c": table.c,
                                   "touches_m": table.touches} if table is not None else None)
        d["read_only"] = self.read_only
        d["camera_role"] = self.camera_roles.get(self.camera.cam)
        d["observations"] = self.observations.snapshot(images=False)
        d["torque"] = all(j.torque for j in self.arm.joints.values())
        d["joints_raw"] = {str(i): (j.goal if j.torque else j.raw) for i, j in self.arm.joints.items()}
        return d

    async def api(self, name, body):
        if self.read_only:
            raise RuntimeError("READ_ONLY: all control and calibration-write actions are disabled")
        cam = self.camera
        if name == "cam":
            if cam.pairs and body["cam"] != cam.cam:
                raise RuntimeError("Reset the calibration before switching camera.")
            cam.cam = int(body["cam"]); cam.save()
            self.log = f"Using camera {cam.cam}."
        elif name == "touch":
            x, y, h, _ = self.reach().state()
            if h > 2.0:
                raise RuntimeError(f"The tip is {h:.1f} cm above the table. Touch the mark first.")
            self.pending = [round(x, 2), round(y, 2)]
            self.log = f"Arm position recorded (x={x:.1f}, y={y:.1f}). Move the arm away, then click that mark in the image."
        elif name == "click":
            u, v = float(body["u"]), float(body["v"])
            if self.pending:
                cam.size = int(body.get("size", cam.size))
                cam.add(u, v, *self.pending)
                self.pending = None
                errs = cam.errors()
                self.log = (f"Mark {len(cam.pairs)} saved. " + (
                    f"Calibrated: marks land within {max(errs):.1f} cm." if errs else
                    f"{max(0, 4 - len(cam.pairs))} more needed."))
                return {}
            x, y = cam.locate(u, v)
            self.log = f"Clicked point is at x={x:.1f}, y={y:.1f} cm."
            return {"x": round(x, 1), "y": round(y, 1)}
        elif name == "undo":
            if cam.pairs:
                cam.pairs.pop(); cam._fit(); cam.save()
            self.pending = None
            self.log = f"Removed the last mark. {len(cam.pairs)} left."
        elif name == "delete":
            n = int(body["mark"])
            if not 1 <= n <= len(cam.pairs):
                raise RuntimeError(f"There is no mark {n}.")
            cam.pairs.pop(n - 1); cam._fit(); cam.save()
            self.log = f"Deleted mark {n}. The others are renumbered; {len(cam.pairs)} left."
        elif name == "reload":
            self.camera = Camera()
            self.pending = None
            if isinstance(self.reacher, Reacher):   # the table may have been taught again too
                try:
                    self.reacher.table = Table.load()
                except (RuntimeError, ValueError, KeyError, OSError):
                    pass
            errs = self.camera.errors()
            self.log = f"Calibration reloaded: {len(self.camera.pairs)} marks" + (
                f", landing within {max(errs):.1f} cm." if errs else ".")
        elif name == "reset":
            cam.pairs = []; cam._fit(); cam.save()
            self.pending = None
            self.log = "Calibration cleared."
        elif name == "hold":
            await self.arm.hold()
            self.log = "Arm is holding its position."
        elif name == "goto":   # move the fingertips to x, y (cm), height cm above the table (e.g. to look down)
            if self.busy:
                raise RuntimeError("Already moving.")
            x, y, h = float(body["x"]), float(body["y"]), float(body.get("height", 12))
            tilt = body.get("tilt")              # gripper lean from straight down, degrees (default: the least that reaches)
            self.busy = True
            self.log = f"Moving to x={x:.1f}, y={y:.1f}, {h:g} cm up..."
            try:
                used = await self.reach().goto(x, y, h, tilt=float(tilt) if tilt is not None else None)
                self.log = f"At x={x:.1f}, y={y:.1f}, {h:g} cm up."
            finally:
                self.busy = False
            return {"tilt": used}
        elif name == "plan":   # where WOULD the gripper be for this target? (nothing moves)
            x, y, h = float(body["x"]), float(body["y"]), float(body.get("height", 12))
            tilt = body.get("tilt")
            r = self.reach()
            (q,), used = r.pose_for(x, y, [h], float(tilt) if tilt is not None else None)
            m, _ = r.model.frames(q)
            return {"tilt": used, "T_base_gripper": [[float(v) for v in row] for row in m]}
        elif name == "gripper":   # open/close the jaws to a percentage (100 = open)
            if self.busy:
                raise RuntimeError("Already moving.")
            await self.arm.move({GRIPPER_ID: max(0.0, min(100.0, float(body["pct"])))}, 0.8)
            self.log = "Gripper moved."
        elif name == "touch_down":   # lower the tip at x, y until it touches: tells where the table really is
            if self.busy:
                raise RuntimeError("Already moving.")
            x, y = float(body["x"]), float(body["y"])
            start = float(body.get("from", 5.0))
            r = self.reach()
            self.busy = True
            self.log = f"Feeling for the table at x={x:.1f}, y={y:.1f}..."
            try:
                tx, ty, th, _ = r.state()
                if math.hypot(tx - x, ty - y) > 0.5 or abs(th - start) > 0.5:
                    if th < start - 0.5:         # first up, so nothing sweeps along the table
                        try:
                            await r.lift(tx, ty, start)
                        except Unreachable:
                            pass
                    await r.goto(x, y, start)
                felt = await r.descend_guarded(x, y, float(body.get("floor", -2.5)),
                                               backoff=float(body.get("backoff", 0.0)))
                self.log = (f"Touched at {felt['height_cm']:+.1f} cm." if felt["contact"]
                            else "Went all the way down without feeling anything.")
            finally:
                self.busy = False
            return felt
        elif name == "nudge":   # turn one joint by a few degrees (for "trim": straightening the gripper)
            if self.busy:
                raise RuntimeError("Already moving.")
            m, deg = int(body["joint"]), float(body["deg"])
            if m not in self.arm.joints or m == GRIPPER_ID:
                raise RuntimeError(f"No joint {m} to turn.")
            j = self.arm.joints[m]
            raw = (j.goal if j.torque else j.raw) + deg * 4096 / 360
            pct = 100.0 * (raw - j.range_min) / (j.range_max - j.range_min)
            if not 0 <= pct <= 100:
                raise RuntimeError(f"Joint {m} can't turn further that way.")
            await self.arm.move({m: pct}, 0.6)
            self.log = f"Joint {m} turned {deg:+g} degrees."
        elif name == "release":
            await self.arm.hold()
            await self.arm.move({GRIPPER_ID: 100}, 0.8)
            self.log = "Gripper open."
        elif name == "grab":
            if self.busy:
                raise RuntimeError("Already moving.")
            if self.pending is not None:
                raise RuntimeError("Finish or cancel the pending calibration point first")
            request = normalize_grab(body, cam.locate)
            x, y, height, above = (request[k] for k in ("x", "y", "z", "above"))
            # Validate each measured joint, not a global timestamp refreshed by other motors.
            reacher = self.reach()
            measured = measured_pose(dict(self.arm.joints), dict(self.arm.joint_updated_at), reacher.model, time.monotonic())
            if not measured["pose_valid"]:
                raise RuntimeError("JOINT_STATE_INVALID: " + "; ".join(measured["errors"]))
            gripper_age = time.monotonic() - self.arm.joint_updated_at.get(GRIPPER_ID, -math.inf)
            if GRIPPER_ID not in self.arm.joints or not 0 <= gripper_age <= MAX_JOINT_AGE_S:
                raise RuntimeError("JOINT_STATE_INVALID: gripper feedback missing or stale")
            self.busy = True
            self.log = f"Grabbing at x={x:.1f}, y={y:.1f}, closing {height:g} cm above the table..."
            side = request["method"] == "side"
            report = {'controller_build': capabilities()['controller_build'],
                      'target_cm': [x, y, height], 'request': body, 'error': None}
            report_path = None
            try:
                if side:
                    got = await self.reach().grab_side(x, y, height, lift_cm=above - height, approach_h=above)
                else:
                    oh = body.get("object_height")   # estimated object height (cm): small/big handling
                    cp = body.get("close_pct")       # how far the jaws close (% open): just under its width
                    kw = dict(object_h=float(oh) if oh is not None else None,
                              close_pct=max(0.0, min(100.0, float(cp))) if cp is not None else 0.0)
                    if body.get("guard"):            # the last part of the way down by feel
                        kw.update(guard=True, report=report)
                        if body.get("feel_all_the_way"):
                            height = None            # no closing height given: down until it meets something
                    got = await self.reach().grab(x, y, height, above, **kw)
                self.log = (f"Grabbed at x={x:.1f}, y={y:.1f}: holding it." if got
                            else f"Closed at x={x:.1f}, y={y:.1f} but nothing is in the gripper.")
                report['holding'] = bool(got)
            except Exception as exc:
                report['error'] = str(exc)
                raise
            finally:
                self.busy = False
                report_path = save_grasp_report(report)
            return {"holding": bool(got), "method": request["method"],
                    "closing_angle_applied": False, "above_cm": above, "report": report,
                    'report_path': report_path}
        elif name == "save_park":
            pose = self.arm.percent()
            save_pose("park", pose)
            self.log = "Saved this position as the park pose."
        elif name == "park":
            if self.busy:
                raise RuntimeError("Already moving.")
            try:
                pose = load_pose("park")
            except FileNotFoundError:
                raise RuntimeError("No park pose yet. With mirroring ON, put the arm where it should rest "
                                   "(a few cm above the table, touching nothing), then press Save as park.")
            r = self.reach()
            q = r.current_angles()
            for m, pct in pose.items():
                if m in MOTOR_JOINTS and m != 7:  # wrist roll and gripper stay as they are
                    q[MOTOR_JOINTS[m]] = r._angle(m, self.arm.joints[m].raw_for(pct))
            park_h = r.table.height(r.tip(q)[0]) * 100
            self.busy = True
            self.log = "Parking..."
            try:
                x, y, h, _ = r.state()
                if h < 10:  # first up, so nothing sweeps along the table
                    try:
                        await r.lift(x, y, 12)
                    except Unreachable:
                        pass
                path = r.plan_swing(q, park_h)
                await r.follow(path, r.duration(path))
                self.log = "Parked. The gripper kept whatever it was holding (Open gripper to let go)."
            finally:
                self.busy = False
        elif name == "place":
            if self.busy:
                raise RuntimeError("Already moving.")
            if "x" in body:
                x, y = float(body["x"]), float(body["y"])
            else:
                x, y = cam.locate(float(body["u"]), float(body["v"]))
            above, height = float(body.get("above", 12)), float(body.get("height", 4))
            r = self.reach()
            r.pose_for(x, y, [above, height])  # refuse before moving if the spot can't be reached
            self.busy = True
            self.log = f"Placing at x={x:.1f}, y={y:.1f}..."
            try:
                await r.goto(x, y, above)                     # carry it over, staying high
                await r.goto(x, y, height, line=True)         # straight down
                await self.arm.move({GRIPPER_ID: 100}, 0.8)   # let go
                await r.lift(x, y, above)                     # back up
                self.log = f"Placed at x={x:.1f}, y={y:.1f}."
            finally:
                self.busy = False
        else:
            raise RuntimeError(f"Unknown action {name}")
        return {}

    # ---------- tiny web server ----------

    async def handle(self, reader, writer):
        try:
            line = (await reader.readline()).decode()
            method, target, _ = line.split()
            length = 0
            while True:
                h = (await reader.readline()).decode().strip()
                if not h:
                    break
                if h.lower().startswith("content-length:"):
                    length = int(h.split(":")[1])
            body = json.loads(await reader.readexactly(length)) if length else {}
            url = urlparse(target)
            code, ctype = 200, "application/json"
            if url.path == "/":
                data, ctype = PAGE.encode(), "text/html; charset=utf-8"
            elif url.path == "/frame.jpg":
                cam = int(parse_qs(url.query).get("cam", [self.camera.cam])[0])
                if 0 <= cam < len(self.jpegs) and self.jpegs[cam]:
                    data, ctype = self.jpegs[cam], "image/jpeg"
                else:
                    code, data = 503, b"{}"
            elif url.path == "/observation" and method == "GET":
                data = json.dumps(self.observations.snapshot()).encode()
            elif url.path == "/status":
                data = json.dumps(self.status()).encode()
            elif url.path == "/pose" and method == "GET":      # where the gripper really is (encoders)
                try:
                    d = self.reach().pose()
                    now = time.monotonic()
                    d["oldest_reading_s"] = max(now - self.arm.joint_updated_at.get(i, -math.inf) for i in range(1, 8))
                    d["busy"] = self.busy
                    data = json.dumps({"ok": True, **d}).encode()
                except (RuntimeError, ValueError, KeyError) as e:
                    data = json.dumps({"ok": False, "error": str(e)}).encode()
            elif url.path == "/model" and method == "GET":     # the arm's geometry, to redo its maths elsewhere
                try:
                    data = json.dumps({"ok": True, **self.reach().model_info()}).encode()
                except (RuntimeError, ValueError, KeyError) as e:
                    data = json.dumps({"ok": False, "error": str(e)}).encode()
            elif url.path.startswith("/api/") and method == "POST":
                try:
                    out = await self.api(url.path[5:], body)
                    data = json.dumps({"ok": True, **out}).encode()
                except (RuntimeError, Unreachable, ValueError, KeyError) as e:
                    self.log = f"Error: {e}"
                    data = json.dumps({"ok": False, "error": str(e)}).encode()
            else:
                code, data = 404, b"{}"
            writer.write(f"HTTP/1.1 {code} OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(data)}\r\n"
                         "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Arm: click to grab</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font: 15px/1.45 system-ui, sans-serif; margin: 0; background: #14171c; color: #e8eaed; }
  main { max-width: 1500px; margin: 0 auto; padding: 16px; display: grid; grid-template-columns: minmax(0, 3fr) minmax(260px, 1fr); gap: 20px; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  #view { position: relative; width: 100%; max-height: calc(100vh - 32px); margin: 0 auto; aspect-ratio: 16 / 9; background: #000; cursor: crosshair; touch-action: manipulation; }
  #view img, #view canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
  h1 { font-size: 18px; margin: 0 0 4px; } h2 { font-size: 15px; margin: 18px 0 6px; color: #9ecbff; }
  button, select, input { font: inherit; padding: 8px 12px; border-radius: 6px; border: 1px solid #454b55; background: #252a32; color: inherit; }
  button { cursor: pointer; } button:hover { background: #323944; } button:disabled { opacity: .45; cursor: default; }
  button.go { background: #1f6f3f; border-color: #2c8f53; } button.go:hover { background: #26864c; }
  input[type=number] { width: 64px; }
  #log { margin: 10px 0; padding: 10px 12px; background: #1e232b; border-left: 3px solid #9ecbff; min-height: 2.9em; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 6px 0; }
  ol { margin: 4px 0; padding-left: 20px; } small, .dim { color: #9aa3ad; }
  .num { font-variant-numeric: tabular-nums; }
</style></head><body><main>
<div>
  <div id="view"><img id="img" alt=""><canvas id="cv"></canvas></div>
</div>
<div>
  <h1>Click to grab</h1>
  <div id="log" role="status">Loading…</div>
  <div class="dim num" id="tip"></div>
  <div class="row"><label for="cam">Camera</label><select id="cam"></select>
    <button id="hold" title="Switch the motors on where the arm is now">Hold arm</button></div>

  <h2>1. Calibrate <small>(mirroring ON)</small></h2>
  <ol><li>Touch a mark on the table with the gripper tip.</li>
      <li>Press <b>Record arm position</b>.</li>
      <li>Move the arm out of the view, click the mark in the image.</li>
      <li>Repeat for 6 or more marks, spread out.</li></ol>
  <div class="row"><button id="touch">Record arm position</button>
    <button id="undo">Undo last</button><button id="reset">Reset</button></div>
  <div class="row"><label>Mark <input id="delmark" type="number" min="1" step="1" style="width:56px"></label>
    <button id="del">Delete mark</button></div>
  <div class="dim num" id="cal"></div>

  <h2>2. Grab <small>(follower Self-controlled)</small></h2>
  <div>Click where the object <b>stands on the table</b>, then Grab.</div>
  <div class="row"><label>Close at <input id="height" type="number" value="5" step="0.5" min="0.5"> cm</label>
    <label>approach from <input id="above" type="number" value="14" step="1" min="3"> cm</label></div>
  <div class="row"><label>From <select id="method"><option value="top">the top</option>
    <option value="side">the side</option></select></label></div>
  <div class="row"><button id="grab" class="go" disabled>Grab</button><button id="release">Open gripper</button></div>
  <div>To put it down: click the spot on the table, then <b>Place here</b>. It lets go at the "Close at" height.</div>
  <div class="row"><button id="place" disabled>Place here</button></div>

  <h2>3. Park</h2>
  <div class="row"><button id="park">Park</button>
    <button id="savepark" title="Mirroring ON: put the arm in its resting pose first">Save current as park</button></div>
  <div class="dim num" id="target"></div>
</div></main>
<script>
const $ = id => document.getElementById(id);
const img = $('img'), cv = $('cv'), ctx = cv.getContext('2d');
let st = {}, target = null;
async function post(name, body) {
  const r = await fetch('/api/' + name, {method: 'POST', body: JSON.stringify(body || {})});
  const d = await r.json(); await refresh(); return d;
}
function draw() {
  const n = img.naturalWidth || 224, nh = img.naturalHeight || 224;
  $('view').style.aspectRatio = n + ' / ' + nh;
  $('view').style.maxWidth = `calc((100vh - 32px) * ${n / nh})`;
  cv.width = 1280; cv.height = Math.round(1280 * nh / n); const k = cv.width / n;
  ctx.clearRect(0, 0, cv.width, cv.height); ctx.lineWidth = 3; ctx.font = '22px system-ui';
  const many = (st.pairs || []).length > 30;
  (st.pairs || []).forEach((p, i) => {
    const err = st.errors && st.errors.length ? st.errors[i] : null;
    ctx.strokeStyle = ctx.fillStyle = err !== null && err > 1 ? '#ff5d5d' : '#ffd24a';
    if (many) { ctx.beginPath(); ctx.arc(p[0] * k, p[1] * k, 3, 0, 7); ctx.fill(); return; }
    ctx.beginPath(); ctx.arc(p[0] * k, p[1] * k, 10, 0, 7); ctx.stroke();
    ctx.fillText(err !== null ? `${i + 1} (${err.toFixed(1)} cm)` : i + 1, p[0] * k + 14, p[1] * k - 10); });
  if (target) { ctx.strokeStyle = '#5ee08a'; const x = target.u * k, y = target.v * k;
    ctx.beginPath(); ctx.moveTo(x - 20, y); ctx.lineTo(x + 20, y); ctx.moveTo(x, y - 20); ctx.lineTo(x, y + 20); ctx.stroke(); }
}
async function refresh() {
  try { st = await (await fetch('/status')).json(); } catch (e) { $('log').textContent = 'Lost connection to arm.click.'; return; }
  let note = '';
  if (!st.cams) note = st.direct ? '  No image from the camera yet.' : '  No camera images from Station: is the follower powered and driven?';
  else if (st.pairs.length && img.naturalWidth && st.size !== img.naturalWidth)
    note = '  These calibration marks were made with a different image size: press Reset and calibrate again.';
  $('log').textContent = st.log + note;
  $('cam').parentElement.firstElementChild.hidden = $('cam').hidden = st.direct;
  $('tip').textContent = st.tip ? `Tip: x=${st.tip[0]}  y=${st.tip[1]}  height=${st.tip[2]} cm · motors ${st.torque ? 'on' : 'OFF'}` : '';
  const sel = $('cam'); if (sel.options.length !== st.cams) { sel.innerHTML = '';
    for (let i = 0; i < st.cams; i++) sel.add(new Option('camera ' + i, i)); } sel.value = st.cam;
  $('cal').textContent = st.calibrated ? `${st.pairs.length} marks, landing within ${Math.max(...st.errors).toFixed(1)} cm. Red marks are off by more than 1 cm.`
    : `${st.pairs.length} marks so far, need at least 4` + (st.pending ? ' · waiting for your click on the mark' : '');
  $('grab').disabled = $('place').disabled = !(target && st.calibrated) || st.busy;
  $('touch').disabled = $('park').disabled = $('savepark').disabled = $('del').disabled = st.busy;
  draw();
}
$('view').addEventListener('click', async e => {
  const r = $('view').getBoundingClientRect(), n = img.naturalWidth || 224, nh = img.naturalHeight || 224;
  const u = (e.clientX - r.left) / r.width * n, v = (e.clientY - r.top) / r.height * nh;
  const wasPending = !!st.pending;
  const d = await post('click', {u, v, size: n});
  if (!wasPending && d.ok) { target = {u, v}; $('target').textContent = `Target: x=${d.x}, y=${d.y} cm`; }
  refresh();
});
$('touch').onclick = () => post('touch');
$('del').onclick = () => { const n = +$('delmark').value; if (n && confirm('Delete mark ' + n + '?')) post('delete', {mark: n}); }; $('undo').onclick = () => post('undo');
$('reset').onclick = () => { if (confirm('Clear all calibration marks?')) post('reset'); };
$('hold').onclick = () => post('hold'); $('release').onclick = () => post('release');
$('cam').onchange = e => post('cam', {cam: +e.target.value});
$('grab').onclick = () => post('grab', {u: target.u, v: target.v, height: +$('height').value, above: +$('above').value, method: $('method').value});
$('park').onclick = () => post('park');
$('savepark').onclick = () => { if (confirm('Save the arm\'s current position as the park pose?')) post('save_park'); };
$('place').onclick = () => post('place', {u: target.u, v: target.v, height: +$('height').value, above: +$('above').value});
setInterval(() => { img.src = '/frame.jpg?t=' + Date.now(); }, 250);
img.onload = draw; setInterval(refresh, 700); refresh();
</script></body></html>
"""


async def _main(args):
    async with Arm() as arm:
        app = App(arm, args.camera, args.size, args.hand_camera, args.stand_index, args.hand_index, args.read_only)
        app.reach()  # fails early with a clear message if the table isn't taught
        asyncio.create_task(app.frames())
        server = await asyncio.start_server(app.handle, args.host, PORT)
        print("Camera:", args.camera or "Station's 224x224 recording feed")
        print(f"Connected to follower {arm.bus}.  Open  http://localhost:{PORT}  (Ctrl+C to stop)")
        if args.host != "127.0.0.1":
            print(f"Also reachable from other machines on port {PORT}. Open the firewall once:"
                  f"  sudo firewall-cmd --add-port={PORT}/tcp")
        async with server:
            await server.serve_forever()


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m arm.click", description="Click on the camera image to grab")
    p.add_argument("--camera", help="read this camera directly, e.g. /dev/video4 (Station must not be using it)")
    p.add_argument("--hand-camera", help="optional second direct camera; --camera must be the fixed stand camera")
    p.add_argument("--read-only", action="store_true", help="disable every POST action; expose observations only")
    p.add_argument("--stand-index", type=int, default=0, help="stand image index in Station feed")
    p.add_argument("--hand-index", type=int, default=1, help="hand image index in Station feed")
    p.add_argument("--size", default="1280x720", help="resolution for --camera (default 1280x720)")
    p.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 = reachable from other laptops on the network (anyone there can move the arm)")
    args = p.parse_args()
    if args.stand_index < 0 or args.hand_index < 0 or args.stand_index == args.hand_index:
        p.error("Camera indices must be distinct and nonnegative")
    if args.hand_camera and not args.camera:
        p.error("--hand-camera requires --camera")
    if args.camera and (args.stand_index != 0 or args.hand_index != 1):
        p.error("Direct cameras use stand index 0 and hand index 1")
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        print()
    except (RuntimeError, FileNotFoundError, ValueError, OSError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
