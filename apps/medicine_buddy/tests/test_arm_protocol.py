"""Structured arm.v1 JSON protocol + HTTP client + remote dose path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.arm_protocol import (
    CheckTakenCommand,
    CheckTakenResult,
    DispenseCommand,
    DispenseResult,
    MedicineRef,
    MoveObjectCommand,
    MoveObjectResult,
    ObjectRef,
    parse_json_body,
)
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.dry_run import FakeAdherence, FakeVision
from medicine_buddy.http_arm import HttpArmClient
from medicine_buddy.orchestrator import DoseStateMachine
from medicine_buddy.silent_buddy import SilentBuddy
from medicine_buddy.states import DoseState
from medicine_buddy.voice_guard import VoiceGuard


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
        self.last_move = None
        self._last_slot = None
        self._dispense = dispense
        self._check = check or {"ok": True, "taken": True, "via": "camera", "detail": "ok"}
        self._move: dict[str, Any] | None = None
        self.last_post_path: str | None = None
        self.last_post_body: dict[str, Any] | None = None

    def _post(self, path: str, body: str) -> dict[str, Any]:
        payload = json.loads(body)
        self.last_post_path = path
        self.last_post_body = payload
        if path.endswith("move_object"):
            out = dict(self._move or {"ok": True, "final_state": "placed", "detect_detail": "ok"})
            out.setdefault("request_id", payload["request_id"])
            return out
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
    assert any("sending a request to the arm" in m for m in buddy.messages)
    assert any("placed your Aspirin" in m for m in buddy.messages)
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


def test_move_object_command_roundtrip_json() -> None:
    cmd = MoveObjectCommand(
        request_id="m1",
        object=ObjectRef(label="water bottle", kind_hint="bottle"),
        destination="bowl",
    )
    data = parse_json_body(cmd.to_json())
    assert data["schema"] == "medicine_buddy.arm.v1"
    assert data["message_type"] == "move_object"
    assert data["object"] == {"label": "water bottle", "kind_hint": "bottle"}
    assert data["destination"] == "bowl"
    assert data["actions"] == ["locate", "pick", "place"]
    assert data["one_shot_detect"] is True


def test_move_object_result_parse() -> None:
    result = MoveObjectResult.from_dict({
        "schema": "medicine_buddy.arm.v1",
        "message_type": "move_object_result",
        "request_id": "m1",
        "ok": True,
        "final_state": "placed",
        "detect_detail": "bottle_in_bowl",
    })
    assert result.ok and result.final_state == "placed"


def test_http_client_posts_move_object_path() -> None:
    arm = _FakeArmHttp({"ok": True, "final_state": "placed"})
    arm._move = {"ok": True, "final_state": "placed", "detect_detail": "ok"}
    result = arm.move_object(label="water bottle", destination="bowl", kind_hint="bottle")
    assert result.ok and result.final_state == "placed"
    assert arm.last_post_path == "/v1/move_object"
    assert arm.last_post_body is not None
    assert arm.last_post_body["message_type"] == "move_object"
    assert arm.last_post_body["object"]["label"] == "water bottle"


def test_move_object_falls_back_to_dispense_on_404() -> None:
    """Arm PCs that only expose /v1/dispense still handle bottles by name."""
    arm = _FakeArmHttp({
        "ok": True,
        "final_state": "placed",
        "detect_detail": "bottle_via_dispense",
    })

    def _post(path: str, body: str) -> dict:
        if path.endswith("move_object"):
            raise RuntimeError(
                'arm HTTP 404 at http://arm.test/v1/move_object: {"error": "not found"}'
            )
        return _FakeArmHttp._post(arm, path, body)

    arm._post = _post  # type: ignore[method-assign]
    result = arm.move_object(label="small bottle", destination="bowl", kind_hint="bottle")
    assert result.ok and result.final_state == "placed"
    assert arm.last_post_path == "/v1/dispense"
    assert arm.last_post_body is not None
    assert arm.last_post_body["message_type"] == "dispense"
    assert arm.last_post_body["medicine"]["name"] == "small bottle"


def test_move_object_can_force_dispense_via_env(monkeypatch) -> None:
    monkeypatch.setenv("MEDICINE_BUDDY_ARM_MOVE_VIA", "dispense")
    arm = _FakeArmHttp({
        "ok": True,
        "final_state": "placed",
        "detect_detail": "forced",
    })
    result = arm.move_object(label="small bottle")
    assert result.ok
    assert arm.last_post_path == "/v1/dispense"
    assert arm.last_post_body is not None
    assert arm.last_post_body["medicine"]["name"] == "small bottle"


def test_api_move_object_needs_confirm_then_demo_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Strict move_object path (demo_smooth redirects to dispense by default).
    monkeypatch.setenv("MEDICINE_BUDDY_DEMO_SMOOTH", "0")
    monkeypatch.delenv("MEDICINE_BUDDY_ARM_MOVE_VIA", raising=False)
    cat = MedicineCatalog({"Aspirin": []})
    svc = MedicineBuddyService(
        tmp_path / "move.db",
        AppConfig(dry_run=True, arm_url=""),
        cat,
        VoiceGuard(cat, enabled=False),
    )
    ask = svc.move_object({"object_label": "water bottle", "confirmed": False})
    assert ask["status"] == "needs_confirm"
    assert "water bottle" in ask["speak_hint"]

    done = svc.move_object({
        "object_label": "water bottle",
        "destination": "bowl",
        "kind_hint": "bottle",
        "confirmed": True,
    })
    assert done["status"] == "ok"
    assert done["demo_mode"] is True
    assert done["ok"] is True
    assert "speak_hint" in done


def test_api_move_object_unreachable_arm_gives_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured but dead ARM_URL must not hang — speak feedback and continue."""
    monkeypatch.setenv("MEDICINE_BUDDY_DEMO_SMOOTH", "0")
    monkeypatch.delenv("MEDICINE_BUDDY_ARM_MOVE_VIA", raising=False)
    cat = MedicineCatalog({"Aspirin": []})
    svc = MedicineBuddyService(
        tmp_path / "dead.db",
        AppConfig(dry_run=False, arm_url="http://127.0.0.1:1", arm_timeout_seconds=90.0),
        cat,
        VoiceGuard(cat, enabled=False),
    )
    ask = svc.move_object({"object_label": "green bottle", "confirmed": False})
    assert ask["status"] == "needs_confirm"

    done = svc.move_object({
        "object_label": "green bottle",
        "destination": "bowl",
        "confirmed": True,
    })
    assert done["status"] == "arm_unreachable"
    assert done["ok"] is False
    assert "can't reach the arm" in done["speak_hint"].lower()
    assert "green bottle" in done["speak_hint"]


def test_api_move_object_announces_progress_before_arm_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While waiting on a reachable arm, speak a short wait line before the long POST."""
    from medicine_buddy import http_arm as http_arm_mod
    from medicine_buddy.arm_protocol import MoveObjectResult

    monkeypatch.setenv("MEDICINE_BUDDY_DEMO_SMOOTH", "0")
    monkeypatch.delenv("MEDICINE_BUDDY_ARM_MOVE_VIA", raising=False)
    heard: list[str] = []

    class _OkArm(HttpArmClient):
        def probe_reachable(self, timeout_s: float = 2.0) -> bool:
            return True

        def move_object(self, **kwargs):
            return MoveObjectResult(
                request_id="r1",
                ok=True,
                final_state="placed",
                detect_detail="ok",
            )

    monkeypatch.setattr(http_arm_mod, "HttpArmClient", _OkArm)
    # from_config still constructs via the class in api module — patch there too
    import medicine_buddy.api as api_mod

    monkeypatch.setattr(api_mod, "HttpArmClient", _OkArm)

    cat = MedicineCatalog({"Aspirin": []})
    svc = MedicineBuddyService(
        tmp_path / "prog.db",
        AppConfig(dry_run=False, arm_url="http://arm.test:9090", arm_timeout_seconds=90.0),
        cat,
        VoiceGuard(cat, enabled=False),
    )
    svc.progress_speaker = lambda text: heard.append(text) or True
    svc.move_object({"object_label": "water bottle", "confirmed": False})
    done = svc.move_object({
        "object_label": "water bottle",
        "destination": "bowl",
        "confirmed": True,
    })
    assert done["status"] == "ok"
    assert heard, "expected a progress announcement while waiting on the arm"
    assert "arm" in heard[0].lower() and "water bottle" in heard[0].lower()


def test_probe_reachable_false_for_closed_port() -> None:
    arm = HttpArmClient("http://127.0.0.1:1", timeout_s=5.0)
    assert arm.probe_reachable(timeout_s=0.3) is False


def test_tool_http_uses_long_timeout_for_arm_paths() -> None:
    import importlib.util
    from pathlib import Path

    helper = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy"
        / "conversation"
        / "lib"
        / "tool_http.py"
    )
    spec = importlib.util.spec_from_file_location("tool_http_under_test", helper)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._timeout_for_path("/arm/move_object", None) >= 90
    assert mod._timeout_for_path("/doses/run", None) >= 90
    assert mod._timeout_for_path("/memories/save", None) == 25
