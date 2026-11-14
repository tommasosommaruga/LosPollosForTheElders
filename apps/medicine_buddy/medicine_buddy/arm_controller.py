"""Maker Arm teach/replay controller stub."""

from __future__ import annotations

import json
from pathlib import Path

from medicine_buddy.ports import PlaceVerifyResult

DEFAULT_WAYPOINTS_DIR = (
    Path(__file__).resolve().parent.parent / "data" / "waypoints"
)


class HardwareArmController:
    """Loads taught joint waypoints; live maker-arm-sdk replay comes later."""

    def __init__(self, waypoints_dir: Path | None = None) -> None:
        self.waypoints_dir = waypoints_dir or DEFAULT_WAYPOINTS_DIR
        self.waypoints_dir.mkdir(parents=True, exist_ok=True)

    def waypoint_path(self, slot_id: int) -> Path:
        return self.waypoints_dir / f"slot_{slot_id}.json"

    def save_waypoints(self, slot_id: int, joints_sequence: list[list[float]]) -> None:
        path = self.waypoint_path(slot_id)
        path.write_text(
            json.dumps({"slot_id": slot_id, "joints": joints_sequence}, indent=2),
            encoding="utf-8",
        )

    def load_waypoints(self, slot_id: int) -> list[list[float]]:
        path = self.waypoint_path(slot_id)
        if not path.exists():
            raise FileNotFoundError(f"No taught waypoints for slot {slot_id}: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return data["joints"]

    def replay_slot_to_bowl(self, slot_id: int) -> None:
        joints = self.load_waypoints(slot_id)
        raise NotImplementedError(
            "maker-arm-sdk replay not wired yet. "
            f"Loaded {len(joints)} poses for slot {slot_id}; use FakeArm --dry-run."
        )

    def verify_place(self, slot_id: int) -> PlaceVerifyResult:
        raise NotImplementedError(
            f"Hardware place verify not wired (slot {slot_id}). Use FakeArm --dry-run."
        )
