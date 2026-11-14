"""Port interfaces for buddy, vision, arm, and adherence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OcrMatch:
    slot_id: int
    label: str
    score: float


@dataclass(frozen=True)
class PlaceVerifyResult:
    ok: bool
    detail: str


@dataclass(frozen=True)
class AdherenceResult:
    taken: bool
    via: str  # "camera" | "voice" | "timeout_missed"
    detail: str


class BuddyPort(Protocol):
    def speak(self, message: str) -> None: ...

    def ask_yes_no(self, prompt: str) -> bool: ...


class VisionPort(Protocol):
    def locate_medicine(self, medicine_name: str) -> OcrMatch | None: ...


class ArmPort(Protocol):
    def replay_slot_to_bowl(self, slot_id: int) -> None: ...

    def verify_place(self, slot_id: int) -> PlaceVerifyResult: ...


class AdherencePort(Protocol):
    def watch_or_ask(self, medicine_name: str, timeout_seconds: float) -> AdherenceResult: ...
