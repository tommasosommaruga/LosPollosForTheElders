"""Control the follower arm from our own Python code.

Station owns the USB boards and the servos. We don't change Station; we talk
to it over TCP (port 8888), the same way its web page does:
  - arm state comes in on the "st3215/inference" queue (~50 times a second)
  - servo commands go out on the "commands" queue

Joint positions are in PERCENT of each joint's calibrated range (0-100),
the same scale mirroring uses. Joints are the servo IDs 1-8.

Before using this, open the Station web page and set the follower's
mirroring dropdown to none. Otherwise the leader and this code fight.

Command line (run from the repo root):
  python -m arm.arm watch                 live joint readout (Ctrl+C to stop)
  python -m arm.arm save NAME             save where the follower is now
  python -m arm.arm go NAME [NAME ...]    move through saved poses in order
  python -m arm.arm set ID PERCENT        move one joint
  python -m arm.arm list                  list saved poses
  python -m arm.arm record NAME           record a movement done with the leader
  python -m arm.arm play NAME [NAME ...]  replay movements (goes to the start first)
  python -m arm.arm limp                  torque off, the arm drops: hold it!

In your own code:
  from arm.arm import Arm
  async with Arm() as arm:
      await arm.go("look_table")
      await arm.play("grab_glasses")
      print(arm.percent())

After a move, the arm keeps torque on and holds its pose. A joint that gets
blocked (table, object) stops pushing so it doesn't overload; the gripper
(joint 8) keeps a light squeeze on whatever it closed on.
"""

import argparse
import asyncio
import json
import logging
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from arm.station import (
    FOLLOWER_BUS,
    REPO_ROOT,
    STATION_HOST,
    commands,
    drivers,
    new_station_client,
    send_commands,
    st3215,
)

POSES_DIR = REPO_ROOT / "poses"
MOVES_DIR = REPO_ROOT / "moves"

# ST3215 servo registers (RAM area)
TORQUE_ENABLE = 0x28
ACCEL = 0x29
GOAL_POSITION = 0x2A
GOAL_SPEED = 0x2E
PRESENT_POSITION = 0x38
PRESENT_CURRENT = 0x45

# Safety caps written to every servo before we move it.
# Speed in encoder steps/s (4096 steps = one turn), 0 would mean "no limit".
# Accel in units of 100 steps/s^2.
SPEED_LIMIT = 1500
ACCEL_LIMIT = 40

SEND_RATE_HZ = 25  # how often we send goal updates during a move

GRIPPER_ID = 8
BLOCKED_STEPS = 40     # a joint ending further than this from its target counts as blocked
SAG_TOL = 10           # steps (~1 deg): closer than this to the target counts as arrived
MAX_SAG_PUSH = 150     # steps (~13 deg): most we aim past a target to make up for the arm sagging
GRIPPER_SQUEEZE = 40   # how far past the contact point the gripper keeps pushing (light grip)
GRIPPER_DEADBAND = 60  # gripper target this close to where it is: leave the gripper alone
GRIP_LAG_STEPS = 45    # closing: jaws this far behind their target (beyond their usual lag) ...
GRIP_STILL_STEPS = 5   # ... while not having moved more than this ...
GRIP_STILL_READINGS = 3   # ... over this many readings = they have met the object
GRIP_KEPT_STEPS = 12   # holding: jaws still at least this far short of their squeeze target = object still there

log = logging.getLogger("arm")


def _u16(state: bytes, addr: int) -> int:
    return struct.unpack_from("<H", state, addr)[0] if len(state) >= addr + 2 else 0


def _u8(state: bytes, addr: int) -> int:
    return state[addr] if len(state) > addr else 0


def _position(raw: int) -> int:
    """Servo positions use bit 15 as a sign flag; fold into 0-4095."""
    if raw & 0x8000:
        return (4096 - (raw & 4095)) & 4095
    return raw & 4095


@dataclass
class Joint:
    id: int
    raw: int  # encoder steps, 0-4095
    goal: int
    range_min: int
    range_max: int
    torque: bool
    current_ma: int

    @property
    def calibrated(self) -> bool:
        return self.range_max > self.range_min

    @property
    def percent(self) -> float:
        if not self.calibrated:
            return 0.0
        return 100.0 * (self.raw - self.range_min) / (self.range_max - self.range_min)

    def raw_for(self, percent: float) -> int:
        percent = max(0.0, min(100.0, percent))
        return round(self.range_min + (self.range_max - self.range_min) * percent / 100.0)


class Arm:
    def __init__(self, host: str = STATION_HOST, bus: str = FOLLOWER_BUS):
        self.host = host
        self.bus = bus
        self.joints: dict[int, Joint] = {}
        self.last_update = 0.0
        self.joint_updated_at: dict[int, float] = {}
        self._holding = False
        self.sag: dict[int, int] = {}   # per joint: how far past its target it is held (sag correction)
        self._client = None
        self._tasks: list[asyncio.Task] = []
        self._error: Exception | None = None

    # ---------- connection ----------

    async def connect(self, timeout: float = 5.0):
        try:
            self._client = await new_station_client(self.host, logging.getLogger("station"))
        except TimeoutError:
            raise RuntimeError(
                f"Can't reach Station at {self.host}:8888. Is it running with --tcp 127.0.0.1:8888?"
            ) from None

        entries: asyncio.Queue = asyncio.Queue()
        errors = self._client.follow("st3215/inference", entries)
        self._tasks.append(asyncio.create_task(self._read_loop(entries, errors)))

        deadline = time.monotonic() + timeout
        while not self.joints:
            if self._error:
                raise self._error
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"No data for follower bus {self.bus!r}. Is the arm powered and visible in Station?"
                )
            await asyncio.sleep(0.05)
        missing = [j.id for j in self.joints.values() if not j.calibrated]
        if missing:
            raise RuntimeError(f"Joints {missing} have no calibrated range. Calibrate in Station first.")

    async def close(self):
        for t in self._tasks:
            t.cancel()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _read_loop(self, entries: asyncio.Queue, errors: asyncio.Queue):
        while True:
            if not errors.empty():
                log.error("Station stream error: %s", await errors.get())
                return
            try:
                entry = await asyncio.wait_for(entries.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if entry is None:
                return
            state = st3215.InferenceStateReader(memoryview(bytes(entry.Data)))
            try:
                bus = self._pick_bus(state.get_buses() or [])
            except RuntimeError as e:
                self._error = e
                return
            if bus is None:
                continue
            joints = {}
            for m in bus.get_motors() or []:
                s = bytes(m.get_state() or b"")
                if not s:
                    continue
                joints[m.get_id()] = Joint(
                    id=m.get_id(),
                    raw=_position(_u16(s, PRESENT_POSITION)),
                    goal=_position(_u16(s, GOAL_POSITION)),
                    range_min=m.get_range_min(),
                    range_max=m.get_range_max(),
                    torque=_u8(s, TORQUE_ENABLE) != 0,
                    current_ma=_u16(s, PRESENT_CURRENT),
                )
            if joints:
                # Merge, don't replace: a frame can miss a motor for a moment
                # (bus glitch); keep its last known state instead of losing it.
                self.joints = {**self.joints, **joints}
                self.last_update = time.monotonic()
                self.joint_updated_at.update({mid: self.last_update for mid in joints})

    def _pick_bus(self, buses):
        serials = [b.get_bus().get_serial_number() for b in buses if b.get_bus()]
        if self.bus == "auto":
            if len(buses) == 1:
                self.bus = serials[0]
            elif buses:
                raise RuntimeError(f"Several arms connected {serials}: set FOLLOWER_BUS in .env")
        for b in buses:
            if b.get_bus() and b.get_bus().get_serial_number() == self.bus:
                return b
        return None

    # ---------- low level ----------

    async def _write(self, writes: list[tuple[int, int, bytes]]):
        """Send (motor_id, register, value) writes in one pack."""
        pack = [
            commands.DriverCommand(
                type=drivers.StationCommandType.STC_ST3215_COMMAND,
                body=st3215.Command(
                    target_bus_serial=self.bus,
                    write=st3215.ST3215WriteCommand(motor_id=mid, address=addr, value=val),
                ).encode(),
            )
            for mid, addr, val in writes
        ]
        await send_commands(self._client, pack)

    async def hold(self):
        """Take control without a jump.

        Joints with torque off get their target set to where they are, then
        torque on. Joints already holding keep their target, so a gripper
        squeezing something keeps squeezing.
        """
        ids = list(self.joints)
        off = [i for i in ids if not self.joints[i].torque]
        for i in off:
            self.sag.pop(i, None)
        if off:
            await self._write([(i, GOAL_POSITION, self.joints[i].raw.to_bytes(2, "little")) for i in off])
        await self._write(
            [(i, GOAL_SPEED, SPEED_LIMIT.to_bytes(2, "little")) for i in ids]
            + [(i, ACCEL, bytes([ACCEL_LIMIT])) for i in ids]
        )
        if off:
            await self._write([(i, TORQUE_ENABLE, b"\x01") for i in off])
        self._holding = True

    async def limp(self):
        """Torque off on every joint. The arm falls under its own weight."""
        await self._write([(i, TORQUE_ENABLE, b"\x00") for i in self.joints])
        self._holding = False

    # ---------- moving ----------

    def percent(self) -> dict[int, float]:
        """Where each joint is now, in % of its range."""
        return {i: round(j.percent, 1) for i, j in sorted(self.joints.items())}

    async def _wait_settled(self, ids, timeout: float = 4.0, still_steps: int = 6):
        """Wait until the joints have stopped moving (at most `timeout` s).

        A slow servo is still on its way after a fixed short wait, and would wrongly
        look blocked. So keep waiting while anything still moves.
        """
        t0 = time.monotonic()
        await asyncio.sleep(0.3)
        prev, prev_t = {i: self.joints[i].raw for i in ids}, self.last_update
        while time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.25)
            if self.last_update == prev_t:  # no new reading yet: can't tell
                continue
            cur = {i: self.joints[i].raw for i in ids}
            if max(abs(cur[i] - prev[i]) for i in ids) <= still_steps:
                return
            prev, prev_t = cur, self.last_update

    async def _settle_and_ease(self, end: dict[int, int], correct_sag: bool = True, aim: dict[int, int] | None = None):
        """After a move: let servos settle, then stop blocked joints from pushing.

        A joint that didn't get there is blocked (table, its own body, an object).
        If it keeps pushing it overloads and trips every few seconds. The gripper
        keeps a light squeeze so it doesn't drop what it holds.

        `end` is where each joint should really be (what its encoder should read). `aim` is what the
        servos were last told (default: the same); it differs when the move already aimed past the
        targets to make up for sag.
        """
        await self._wait_settled(list(end))
        # Make up for sag first: under the arm's own weight (more when the servos are warm, the arm
        # reaches far, or it holds something) shoulder and elbow stop a few degrees short and the
        # servo doesn't push the last bit. Aim past the target by what's missing, up to twice.
        # A joint that still doesn't get there after that is really blocked: eased off below.
        aim = {i: (aim or {}).get(i, t) for i, t in end.items()}
        for _ in range(2 if correct_sag else 0):
            pushes = []
            for i, target in end.items():
                if i == GRIPPER_ID:
                    continue
                j = self.joints[i]
                off = target - j.raw
                if abs(off) <= SAG_TOL:
                    continue
                new = max(target - MAX_SAG_PUSH, min(target + MAX_SAG_PUSH, aim[i] + off))
                new = max(j.range_min, min(j.range_max, new))
                if new != aim[i]:
                    aim[i] = new
                    pushes.append((i, GOAL_POSITION, int(new).to_bytes(2, "little")))
            if not pushes:
                break
            await self._write(pushes)
            await self._wait_settled([p[0] for p in pushes])
        for i, target in end.items():
            if i != GRIPPER_ID:
                self.sag[i] = aim[i] - target   # how far past the target this joint is held
        eases = []
        for i, target in end.items():
            j = self.joints[i]
            off = target - j.raw
            if abs(off) <= BLOCKED_STEPS:
                continue
            if i == GRIPPER_ID:
                log.warning("gripper stopped %d steps short: holding something, keeping a light grip", abs(off))
                push = max(-GRIPPER_SQUEEZE, min(GRIPPER_SQUEEZE, off))
            else:
                log.warning("joint %d blocked %d steps short of target, easing off", i, abs(off))
                push = 0
                self.sag[i] = 0
            eases.append((i, GOAL_POSITION, (j.raw + push).to_bytes(2, "little")))
        if eases:
            await self._write(eases)

    async def move(self, targets: dict[int, float], seconds: float = 2.0):
        """Move joints to target percentages, all arriving together, smoothly."""
        unknown = [i for i in targets if i not in self.joints]
        if unknown:
            raise ValueError(f"No joint(s) {unknown} on the follower. Joints: {sorted(self.joints)}")
        await self.hold()  # also resets speed/accel, which mirroring may have changed

        targets = dict(targets)
        g = self.joints.get(GRIPPER_ID)
        if GRIPPER_ID in targets and g and abs(g.raw_for(targets[GRIPPER_ID]) - g.raw) < GRIPPER_DEADBAND:
            # Gripper is already about there (maybe holding something): leave it,
            # so moving the arm doesn't loosen the grip.
            del targets[GRIPPER_ID]
        if not targets:
            return

        # Start from each joint's current target if it's holding, else from where it is.
        start = {i: self.joints[i].goal if self.joints[i].torque else self.joints[i].raw for i in targets}
        end = {i: self.joints[i].raw_for(p) for i, p in targets.items()}
        t0 = time.monotonic()
        while True:
            s = min(1.0, (time.monotonic() - t0) / max(seconds, 0.05))
            s = s * s * (3 - 2 * s)  # ease in and out
            await self._write([
                (i, GOAL_POSITION, round(start[i] + (end[i] - start[i]) * s).to_bytes(2, "little"))
                for i in targets
            ])
            if s >= 1.0:
                break
            await asyncio.sleep(1 / SEND_RATE_HZ)
        await self._settle_and_ease(end)

    async def close_gripper(self, target_pct: float = 0.0, seconds: float = 2.4, squeeze: int = GRIPPER_SQUEEZE):
        """Close the jaws until they meet something, then hold it with a light squeeze.

        The jaws move at a steady, moderate speed. The moment they stop moving while their target keeps
        going, they have met the object: stop there and keep only a small squeeze. So the grip is the
        same gentle one whatever the object's width, and nothing gets shot out or crushed.

        Returns (holding, open_pct): whether the jaws stopped on something before closing completely,
        and how far open they ended up (0-100 %).
        """
        g = self.joints.get(GRIPPER_ID)
        if g is None:
            raise ValueError("No gripper joint on the follower.")
        await self.hold()
        start = g.goal if g.torque else g.raw
        end = g.raw_for(target_pct)
        span = max(1, g.range_max - g.range_min)
        if abs(end - start) < GRIPPER_DEADBAND:
            g = self.joints[GRIPPER_ID]
            return abs(g.raw - end) > BLOCKED_STEPS, round(100.0 * (g.raw - g.range_min) / span, 1)
        direction = 1 if end > start else -1
        rate = span / max(0.5, seconds)              # steps per second: the whole range takes `seconds`
        goal, t_prev = float(start), time.monotonic()
        seen, lags, seen_t = [], [], None
        contact = None
        while True:
            now = time.monotonic()
            goal += direction * rate * (now - t_prev)
            t_prev = now
            done = (goal - end) * direction >= 0
            if done:
                goal = float(end)
            await self._write([(GRIPPER_ID, GOAL_POSITION, int(round(goal)).to_bytes(2, "little"))])
            await asyncio.sleep(1 / SEND_RATE_HZ)
            if self.last_update != seen_t:           # a new reading from the servo
                seen_t = self.last_update
                raw = self.joints[GRIPPER_ID].raw
                seen.append(raw)
                lags.append(abs(goal - raw))
                if len(seen) >= GRIP_STILL_READINGS + 1:
                    moved = abs(seen[-1] - seen[-1 - GRIP_STILL_READINGS])
                    # how far behind the jaws normally run while moving freely (the first second or so)
                    free = sorted(lags[2:max(3, len(lags) - GRIP_STILL_READINGS)])
                    usual = free[len(free) // 2] if free else 0.0
                    if moved <= GRIP_STILL_STEPS and lags[-1] > usual + GRIP_LAG_STEPS:
                        contact = raw
                        break
            if done:
                break
        if contact is None:                          # reached the end: let it settle, then look again
            await self._wait_settled([GRIPPER_ID], timeout=1.5)
            raw = self.joints[GRIPPER_ID].raw
            if abs(end - raw) > BLOCKED_STEPS:
                contact = raw
        g = self.joints[GRIPPER_ID]
        if contact is not None:
            hold = max(g.range_min, min(g.range_max, contact + direction * squeeze))
            if (hold - end) * direction > 0:         # never past the requested target
                hold = end
            await self._write([(GRIPPER_ID, GOAL_POSITION, int(hold).to_bytes(2, "little"))])
            await asyncio.sleep(0.35)
            g = self.joints[GRIPPER_ID]
            log.info("gripper met something at %d steps, holding it lightly", contact)
        holding = contact is not None and abs(g.raw - end) > BLOCKED_STEPS
        return holding, round(100.0 * (g.raw - g.range_min) / span, 1)

    def gripper_still_holding(self) -> bool:
        """After close_gripper said it holds something: is it still there? (If the object slipped out,
        the jaws have closed the last bit to their target.)"""
        g = self.joints.get(GRIPPER_ID)
        return bool(g and g.torque and abs(g.goal - g.raw) >= GRIP_KEPT_STEPS)

    async def go(self, pose_name: str, seconds: float = 2.0):
        await self.move(load_pose(pose_name), seconds)

    # ---------- whole movements ----------

    def _commanded(self) -> dict[int, float]:
        """What the follower is being told to do right now, in %.

        While mirroring, that's the leader's position mapped onto the follower,
        including the gripper squeezing past the point where it touches an object.
        """
        out = {}
        for i, j in sorted(self.joints.items()):
            raw = j.goal if j.torque else j.raw
            out[i] = round(100.0 * (raw - j.range_min) / (j.range_max - j.range_min), 2)
        return out

    async def record(self, stop: asyncio.Event) -> list[dict[int, float]]:
        """Sample the follower at SEND_RATE_HZ until `stop` is set."""
        frames = []
        while not stop.is_set():
            frames.append(self._commanded())
            await asyncio.sleep(1 / SEND_RATE_HZ)
        return _trim_still(frames)

    async def play(self, name: str, speed: float = 1.0, approach_seconds: float = 2.0):
        """Go smoothly to the movement's first frame, then replay it at its recorded pace."""
        frames = load_move(name)
        if not frames:
            raise ValueError(f"Movement {name!r} is empty")
        await self.move(frames[0], approach_seconds)
        ids = [i for i in frames[0] if i in self.joints]
        dt = 1 / (SEND_RATE_HZ * speed)
        t0 = time.monotonic()
        joints = dict(self.joints)  # calibration ranges, fixed for the whole replay
        end = {}
        for k, f in enumerate(frames):
            end = {i: joints[i].raw_for(f[i]) for i in ids}
            await self._write([(i, GOAL_POSITION, end[i].to_bytes(2, "little")) for i in ids])
            delay = t0 + (k + 1) * dt - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        await self._settle_and_ease(end)


# ---------- poses on disk ----------

def save_pose(name: str, percents: dict[int, float]) -> Path:
    POSES_DIR.mkdir(exist_ok=True)
    path = POSES_DIR / f"{name}.json"
    path.write_text(json.dumps({"joints": {str(k): v for k, v in percents.items()}}, indent=2) + "\n")
    return path


def load_pose(name: str) -> dict[int, float]:
    path = POSES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"No pose {name!r}. Saved poses: {list_poses()}")
    return {int(k): float(v) for k, v in json.loads(path.read_text())["joints"].items()}


def list_poses() -> list[str]:
    return sorted(p.stem for p in POSES_DIR.glob("*.json"))


# ---------- movements on disk ----------

def _trim_still(frames: list[dict[int, float]], tol: float = 0.3) -> list[dict[int, float]]:
    """Drop the frames before the arm starts moving and after it stops."""
    def same(a, b):
        return all(abs(a[i] - b[i]) < tol for i in a)
    while len(frames) > 1 and same(frames[0], frames[1]):
        frames.pop(0)
    while len(frames) > 1 and same(frames[-1], frames[-2]):
        frames.pop()
    return frames


def save_move(name: str, frames: list[dict[int, float]]) -> Path:
    MOVES_DIR.mkdir(exist_ok=True)
    path = MOVES_DIR / f"{name}.json"
    ids = sorted(frames[0])
    data = {"hz": SEND_RATE_HZ, "joints": ids, "frames": [[f[i] for i in ids] for f in frames]}
    path.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    return path


def load_move(name: str) -> list[dict[int, float]]:
    path = MOVES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"No movement {name!r}. Recorded movements: {list_moves()}")
    data = json.loads(path.read_text())
    return [dict(zip(data["joints"], row)) for row in data["frames"]]


def list_moves() -> list[str]:
    return sorted(p.stem for p in MOVES_DIR.glob("*.json"))


# ---------- command line ----------

def _fmt(arm: Arm) -> str:
    return "  ".join(
        f"M{i}:{j.percent:5.1f}%{'' if j.torque else '(off)'}" for i, j in sorted(arm.joints.items())
    )


async def _main(args):
    if args.cmd == "list":
        print("Poses:", ", ".join(list_poses()) or "(none yet)")
        print("Movements:", ", ".join(list_moves()) or "(none yet)")
        return

    async with Arm() as arm:
        print(f"Connected to follower {arm.bus}")
        if args.cmd == "watch":
            while True:
                age = time.monotonic() - arm.last_update
                stale = "  (NO NEW DATA)" if age > 1 else ""
                print("\r" + _fmt(arm) + stale + "   ", end="", flush=True)
                await asyncio.sleep(0.1)
        elif args.cmd == "save":
            path = save_pose(args.name, arm.percent())
            print(f"Saved {path.relative_to(REPO_ROOT)}: {arm.percent()}")
        elif args.cmd == "go":
            for k, name in enumerate(args.names):
                print(f"-> {name}")
                await arm.go(name, args.seconds)
                if k < len(args.names) - 1:
                    await asyncio.sleep(args.pause)
            print("Done, holding:", arm.percent())
        elif args.cmd == "set":
            await arm.move({args.id: args.percent}, args.seconds)
            print("Done, holding:", arm.percent())
        elif args.cmd == "record":
            if not all(j.torque for j in arm.joints.values()):
                print("Warning: follower torque is off. Is mirroring on? Recording actual positions.")
            await asyncio.get_running_loop().run_in_executor(None, input, "Press Enter to START recording... ")
            stop = asyncio.Event()
            task = asyncio.create_task(arm.record(stop))
            await asyncio.get_running_loop().run_in_executor(None, input, "Recording. Do the movement with the leader, then press Enter to STOP... ")
            stop.set()
            frames = await task
            path = save_move(args.name, frames)
            print(f"Saved {path.relative_to(REPO_ROOT)}: {len(frames) / SEND_RATE_HZ:.1f} s of movement")
        elif args.cmd == "play":
            for name in args.names:
                print(f"-> {name}")
                await arm.play(name, speed=args.speed)
            print("Done, holding:", arm.percent())
        elif args.cmd == "limp":
            input("The arm will DROP. Hold it, then press Enter... ")
            await arm.limp()
            print("Torque off.")


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m arm.arm", description="Control the follower arm")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("watch", help="live joint readout")
    sub.add_parser("list", help="list saved poses")
    sub.add_parser("limp", help="torque off (hold the arm!)")
    s = sub.add_parser("save", help="save the current follower pose")
    s.add_argument("name")
    g = sub.add_parser("go", help="move through saved poses")
    g.add_argument("names", nargs="+")
    g.add_argument("--seconds", type=float, default=2.0, help="time per move (default 2)")
    g.add_argument("--pause", type=float, default=0.5, help="pause between poses (default 0.5)")
    r = sub.add_parser("record", help="record a movement done with the leader (mirroring ON)")
    r.add_argument("name")
    pl = sub.add_parser("play", help="replay recorded movements (follower Self-controlled)")
    pl.add_argument("names", nargs="+")
    pl.add_argument("--speed", type=float, default=1.0, help="0.5 = half speed (default 1)")
    st = sub.add_parser("set", help="move one joint")
    st.add_argument("id", type=int)
    st.add_argument("percent", type=float)
    st.add_argument("--seconds", type=float, default=2.0)
    args = p.parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        print()
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
