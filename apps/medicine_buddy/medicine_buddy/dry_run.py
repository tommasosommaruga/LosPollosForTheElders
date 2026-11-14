"""Fake ports for hardware-free demos and tests."""

from __future__ import annotations

import sys
from typing import TextIO

from medicine_buddy.config import AppConfig
from medicine_buddy.ports import AdherenceResult, OcrMatch, PlaceVerifyResult


def _out(message: str) -> None:
    """print() that cannot raise UnicodeEncodeError (a crash here used to be reported as an arm fault)."""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(encoding, "replace").decode(encoding))


class ConsoleBuddy:
    def __init__(self, input_stream: TextIO | None = None) -> None:
        self._in = input_stream or sys.stdin

    def speak(self, message: str) -> None:
        _out(f"[Reachy] {message}")

    def ask_yes_no(self, prompt: str) -> bool:
        self.speak(prompt)
        while True:
            raw = self._in.readline()
            if raw == "":
                return False
            answer = raw.strip().lower()
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            _out("Please answer yes or no.")


class FakeVision:
    def __init__(self, config: AppConfig) -> None:
        self._labels = [label.upper() for label in config.tray_labels]
        self._scores = {k.upper(): v for k, v in config.dry_run_scores.items()}
        self.min_confidence = config.ocr_min_confidence

    def locate_medicine(self, medicine_name: str) -> OcrMatch | None:
        target = medicine_name.strip().upper().replace(" ", "_")
        # Also allow plain "Aspirin" matching ASPIRIN
        target_alt = medicine_name.strip().upper()
        matches: list[OcrMatch] = []
        for slot_id, label in enumerate(self._labels):
            if label in {"EMPTY", ""}:
                continue
            if label == target or label == target_alt or label.replace("_", " ") == target_alt:
                score = self._scores.get(label, 0.5)
                matches.append(OcrMatch(slot_id=slot_id, label=label, score=score))
        if len(matches) != 1:
            return None
        match = matches[0]
        if match.score < self.min_confidence:
            return None
        return match


class FakeArm:
    def __init__(self) -> None:
        self.replayed: list[int] = []
        self.bowl_occupied = False
        self.fail_place_verify = False

    def replay_slot_to_bowl(self, slot_id: int) -> None:
        _out(f"[Arm] Replaying taught waypoints: slot {slot_id} -> bowl")
        self.replayed.append(slot_id)
        self.bowl_occupied = True

    def verify_place(self, slot_id: int) -> PlaceVerifyResult:
        if self.fail_place_verify:
            return PlaceVerifyResult(ok=False, detail="bowl appears empty after place")
        if not self.bowl_occupied:
            return PlaceVerifyResult(ok=False, detail="no place motion recorded")
        return PlaceVerifyResult(
            ok=True,
            detail=f"slot {slot_id} cleared and bowl occupied (dry-run)",
        )


class FakeAdherence:
    def __init__(self, buddy: ConsoleBuddy, auto_taken: bool | None = None) -> None:
        self._buddy = buddy
        # None → ask user; True/False → scripted for tests
        self.auto_taken = auto_taken

    def watch_or_ask(self, medicine_name: str, timeout_seconds: float) -> AdherenceResult:
        _out(
            f"[Adherence] Watching Reachy camera for {timeout_seconds:.0f}s "
            f"({medicine_name})..."
        )
        if self.auto_taken is True:
            return AdherenceResult(
                taken=True, via="camera", detail="dry-run camera heuristic"
            )
        if self.auto_taken is False:
            return AdherenceResult(
                taken=False, via="timeout_missed", detail="dry-run timeout"
            )

        # Simulate timeout then voice fallback
        _out("[Adherence] Camera inconclusive - voice fallback")
        answered = self._buddy.ask_yes_no("Did you take it?")
        if answered:
            return AdherenceResult(taken=True, via="voice", detail="user confirmed")
        return AdherenceResult(
            taken=False, via="timeout_missed", detail="user said no / missed"
        )
