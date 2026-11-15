"""Structured arm.v1 JSON protocol + HTTP client + remote dose path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from medicine_buddy.arm_protocol import (
    CheckTakenCommand,
    CheckTakenResult,
    DispenseCommand,
    DispenseResult,
    MedicineRef,
    parse_json_body,
)
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.dry_run import FakeAdherence, FakeVision
from medicine_buddy.http_arm import HttpArmClient
from medicine_buddy.orchestrator import DoseStateMachine
from medicine_buddy.silent_buddy import SilentBuddy
from medicine_buddy.states import DoseState


def test_dispense_command_roundtrip_json() -> None:
    cmd = DispenseCommand(
        request_id="req-1",
        medicine=MedicineRef(id=3, name="Aspirin", person="Mario"),
        ocr_min_confidence=0.65,
    )
    data = parse_json_body(cmd.to_json())
    assert data["schema"] == "medicine_buddy.arm.v1"
    assert data["message_type"] == "dispense"
    assert data["medicine"]["name"] == "Aspirin"
    assert data["one_shot_detect"] is True
    assert data["actions"] == ["locate", "pick_to_bowl", "verify_place"]


def test_dispense_result_parse() -> None:
    raw = {
        "schema": "medicine_buddy.arm.v1",
        "message_type": "dispense_result",
        "request_id": "req-1",
        "ok": True,
        "final_state": "placed",
        "slot_id": 1,
        "label": "ASPIRIN",
        "score": 0.9,
        "place_verified": True,
        "detect_detail": "ok",
    }
    result = DispenseResult.from_dict(raw)
    assert result.ok and result.final_state == "placed" and result.slot_id == 1


def test_check_taken_null_inconclusive() -> None:
    result = CheckTakenResult.from_dict({
        "request_id": "r2",
        "ok": True,
        "taken": None,
        "detail": "unclear",
    })
    assert result.taken is None


class _FakeArmHttp(HttpArmClient):
    """HttpArmClient without network — canned responses."""

    def __init__(self, dispense: dict[str, Any], check: dict[str, Any] | None = None) -> None:
        # Skip parent URL validation by setting attrs directly
        self.base_url = "http://arm.test"
        self.timeout_s = 5.0
        self.dry_run = True
        self.ocr_min_confidence = 0.65
        self.last_dispense = None
        self.last_check = None
        self._last_slot = None
        self._dispense = dispense
        self._check = check or {"ok": True, "taken": True, "via": "camera", "detail": "ok"}

    def _post(self, path: str, body: str) -> dict[str, Any]:
        payload = json.loads(body)
        if path.endswith("dispense"):
            out = dict(self._dispense)
            out.setdefault("request_id", payload["request_id"])
            return out
        out = dict(self._check)
        out.setdefault("request_id", payload["request_id"])
        return out


def test_remote_dose_after_confirm(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    med = db.add_medicine("Aspirin")
    buddy = SilentBuddy(confirm=True)
    arm = _FakeArmHttp({
        "ok": True,
        "final_state": "placed",
        "slot_id": 0,
        "label": "ASPIRIN",
        "score": 0.95,
        "place_verified": True,
        "detect_detail": "box_in_bowl",
    })
    machine = DoseStateMachine(
        db, buddy, FakeVision(AppConfig()), arm, FakeAdherence(buddy, auto_taken=None), AppConfig(),
    )
    final = machine.run_dose(med.id, stop_after_placed=False, person_name="Mario")
    assert final == DoseState.TAKEN
    assert any("arm.v1" in e.detail or "dispense" in e.detail.lower()
               for e in db.list_dose_events(med.id))


def test_remote_dose_failed_on_arm_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "b.db")
    med = db.add_medicine("Aspirin")
    buddy = SilentBuddy(confirm=True)
    arm = _FakeArmHttp({
        "ok": False,
        "final_state": "failed",
        "error": "low confidence",
        "place_verified": False,
    })
    machine = DoseStateMachine(
        db, buddy, FakeVision(AppConfig()), arm, FakeAdherence(buddy, auto_taken=None), AppConfig(),
    )
    assert machine.run_dose(med.id) == DoseState.FAILED


def test_no_motion_when_person_declines(tmp_path: Path) -> None:
    db = Database(tmp_path / "c.db")
    med = db.add_medicine("Aspirin")
    buddy = SilentBuddy(confirm=False)
    arm = _FakeArmHttp({"ok": True, "final_state": "placed", "place_verified": True})
    machine = DoseStateMachine(
        db, buddy, FakeVision(AppConfig()), arm, FakeAdherence(buddy), AppConfig(),
    )
    assert machine.run_dose(med.id) == DoseState.SKIPPED
    assert arm.last_dispense is None


def test_check_taken_command_json() -> None:
    cmd = CheckTakenCommand(
        request_id="c1",
        medicine=MedicineRef(1, "Aspirin"),
        slot_id=2,
        timeout_seconds=8.0,
    )
    data = json.loads(cmd.to_json())
    assert data["message_type"] == "check_taken"
    assert data["one_shot_detect"] is True
    assert data["slot_id"] == 2
