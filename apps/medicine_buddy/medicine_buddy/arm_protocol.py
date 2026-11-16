"""Standardized structured text (JSON) protocol between Medicine Buddy and arm software.

schema: medicine_buddy.arm.v1

Buddy → Arm:
  POST {ARM_URL}/v1/dispense     message_type=dispense
  POST {ARM_URL}/v1/check_taken  message_type=check_taken
  POST {ARM_URL}/v1/move_object  message_type=move_object  (non-medicine graspables)

Arm → Buddy: same request's JSON response body
  (dispense_result / check_taken_result / move_object_result).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA = "medicine_buddy.arm.v1"
DISPENSE_PATH = "/v1/dispense"
CHECK_TAKEN_PATH = "/v1/check_taken"
MOVE_OBJECT_PATH = "/v1/move_object"
DEFAULT_DESTINATION = "bowl"

FINAL_STATES = frozenset({"placed", "failed", "aborted"})


def normalize_destination(raw: object | None) -> str:
    """Place target for the arm. Default bowl; use the spoken/specified place when set."""
    cleaned = " ".join(str(raw or DEFAULT_DESTINATION).split())
    return cleaned or DEFAULT_DESTINATION


def dispense_actions_for(destination: str) -> tuple[str, ...]:
    """Keep pick_to_bowl for the default place; generic pick/place otherwise."""
    if normalize_destination(destination).casefold() == DEFAULT_DESTINATION:
        return ("locate", "pick_to_bowl", "verify_place")
    return ("locate", "pick", "place", "verify_place")


@dataclass(frozen=True)
class MedicineRef:
    id: int
    name: str
    person: str | None = None


@dataclass(frozen=True)
class DispenseCommand:
    request_id: str
    medicine: MedicineRef
    slot_hint: int | None = None
    ocr_min_confidence: float = 0.65
    # Soft deadline for the arm PC: stop searching if the object is gone / never matched.
    timeout_seconds: float = 60.0
    destination: str = DEFAULT_DESTINATION
    actions: tuple[str, ...] = ("locate", "pick_to_bowl", "verify_place")
    one_shot_detect: bool = True
    dry_run: bool = False
    schema: str = SCHEMA
    message_type: str = "dispense"

    def to_json(self) -> str:
        dest = normalize_destination(self.destination)
        actions = self.actions if self.actions else dispense_actions_for(dest)
        # If caller left the default bowl actions but destination changed, adapt.
        if dest.casefold() != DEFAULT_DESTINATION and actions == ("locate", "pick_to_bowl", "verify_place"):
            actions = dispense_actions_for(dest)
        payload = {
            "schema": self.schema,
            "message_type": self.message_type,
            "request_id": self.request_id,
            "medicine": asdict(self.medicine),
            "slot_hint": self.slot_hint,
            "ocr_min_confidence": self.ocr_min_confidence,
            "timeout_seconds": float(self.timeout_seconds),
            "destination": dest,
            "actions": list(actions),
            "one_shot_detect": self.one_shot_detect,
            "dry_run": self.dry_run,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class DispenseResult:
    request_id: str
    ok: bool
    final_state: str
    slot_id: int | None = None
    label: str | None = None
    score: float | None = None
    place_verified: bool = False
    detect_detail: str = ""
    error: str | None = None
    schema: str = SCHEMA
    message_type: str = "dispense_result"

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, fallback_request_id: str = "") -> "DispenseResult":
        state = str(data.get("final_state") or ("placed" if data.get("ok") else "failed")).lower()
        if state not in FINAL_STATES:
            state = "failed"
        return cls(
            request_id=str(data.get("request_id") or fallback_request_id),
            ok=bool(data.get("ok")),
            final_state=state,
            slot_id=_optional_int(data.get("slot_id")),
            label=(str(data["label"]) if data.get("label") is not None else None),
            score=_optional_float(data.get("score")),
            place_verified=bool(data.get("place_verified")),
            detect_detail=str(data.get("detect_detail") or data.get("error") or ""),
            error=(str(data["error"]) if data.get("error") is not None else None),
            schema=str(data.get("schema") or SCHEMA),
            message_type=str(data.get("message_type") or "dispense_result"),
        )


@dataclass(frozen=True)
class CheckTakenCommand:
    request_id: str
    medicine: MedicineRef
    slot_id: int | None = None
    timeout_seconds: float = 8.0
    one_shot_detect: bool = True
    dry_run: bool = False
    schema: str = SCHEMA
    message_type: str = "check_taken"

    def to_json(self) -> str:
        payload = {
            "schema": self.schema,
            "message_type": self.message_type,
            "request_id": self.request_id,
            "medicine": asdict(self.medicine),
            "slot_id": self.slot_id,
            "timeout_seconds": self.timeout_seconds,
            "one_shot_detect": self.one_shot_detect,
            "dry_run": self.dry_run,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class CheckTakenResult:
    request_id: str
    ok: bool
    taken: bool | None  # None = inconclusive
    via: str = "camera"
    detail: str = ""
    error: str | None = None
    schema: str = SCHEMA
    message_type: str = "check_taken_result"

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, fallback_request_id: str = "") -> "CheckTakenResult":
        taken_raw = data.get("taken")
        taken: bool | None
        if taken_raw is None:
            taken = None
        else:
            taken = bool(taken_raw)
        return cls(
            request_id=str(data.get("request_id") or fallback_request_id),
            ok=bool(data.get("ok", True)),
            taken=taken,
            via=str(data.get("via") or "camera"),
            detail=str(data.get("detail") or data.get("detect_detail") or ""),
            error=(str(data["error"]) if data.get("error") is not None else None),
            schema=str(data.get("schema") or SCHEMA),
            message_type=str(data.get("message_type") or "check_taken_result"),
        )


@dataclass(frozen=True)
class ObjectRef:
    """Spoken label for a graspable non-medicine object (bottle, box, …)."""

    label: str
    kind_hint: str | None = None


@dataclass(frozen=True)
class MoveObjectCommand:
    request_id: str
    object: ObjectRef
    destination: str = "bowl"
    timeout_seconds: float = 60.0
    actions: tuple[str, ...] = ("locate", "pick", "place")
    one_shot_detect: bool = True
    dry_run: bool = False
    schema: str = SCHEMA
    message_type: str = "move_object"

    def to_json(self) -> str:
        obj: dict[str, Any] = {"label": self.object.label}
        if self.object.kind_hint:
            obj["kind_hint"] = self.object.kind_hint
        payload = {
            "schema": self.schema,
            "message_type": self.message_type,
            "request_id": self.request_id,
            "object": obj,
            "destination": self.destination,
            "timeout_seconds": float(self.timeout_seconds),
            "actions": list(self.actions),
            "one_shot_detect": self.one_shot_detect,
            "dry_run": self.dry_run,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class MoveObjectResult:
    request_id: str
    ok: bool
    final_state: str
    detect_detail: str = ""
    error: str | None = None
    schema: str = SCHEMA
    message_type: str = "move_object_result"

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, fallback_request_id: str = "") -> "MoveObjectResult":
        state = str(data.get("final_state") or ("placed" if data.get("ok") else "failed")).lower()
        if state not in FINAL_STATES:
            state = "failed"
        return cls(
            request_id=str(data.get("request_id") or fallback_request_id),
            ok=bool(data.get("ok")),
            final_state=state,
            detect_detail=str(data.get("detect_detail") or data.get("error") or ""),
            error=(str(data["error"]) if data.get("error") is not None else None),
            schema=str(data.get("schema") or SCHEMA),
            message_type=str(data.get("message_type") or "move_object_result"),
        )


def new_request_id() -> str:
    return str(uuid.uuid4())


def parse_json_body(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("arm response must be a JSON object")
    return data


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
