from __future__ import annotations

from io import StringIO
from pathlib import Path

from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.dry_run import ConsoleBuddy, FakeAdherence, FakeArm, FakeVision
from medicine_buddy.orchestrator import DoseStateMachine
from medicine_buddy.states import DoseState
from medicine_buddy.voice_setup import setup_medicine_schedule


def _machine(
    tmp_path: Path,
    answers: str,
    *,
    auto_taken: bool | None = True,
    tray: list[str] | None = None,
    scores: dict[str, float] | None = None,
) -> tuple[DoseStateMachine, Database]:
    config = AppConfig(
        dry_run=True,
        adherence_timeout_seconds=0.1,
        tray_labels=tray or ["ASPIRIN", "EMPTY", "EMPTY", "EMPTY"],
        dry_run_scores=scores or {"ASPIRIN": 0.95},
        ocr_min_confidence=0.65,
    )
    db = Database(tmp_path / "demo.db")
    buddy = ConsoleBuddy(StringIO(answers))
    vision = FakeVision(config)
    arm = FakeArm()
    adherence = FakeAdherence(buddy, auto_taken=auto_taken)
    return DoseStateMachine(db, buddy, vision, arm, adherence, config), db


def test_happy_path_taken(tmp_path: Path) -> None:
    machine, db = _machine(tmp_path, answers="yes\nyes\n", auto_taken=True)
    med, _ = setup_medicine_schedule(
        db,
        machine.buddy,
        name="Aspirin",
        weekdays_raw="daily",
        time_raw="09:00",
    )
    # setup consumed first yes; inject confirm for dose
    machine.buddy = ConsoleBuddy(StringIO("yes\n"))
    machine.adherence = FakeAdherence(machine.buddy, auto_taken=True)
    final = machine.run_dose(med.id)
    assert final == DoseState.TAKEN
    states = [e.state for e in db.list_dose_events(med.id)]
    assert DoseState.CONFIRMED in states
    assert DoseState.PLACED in states
    assert states[-1] == DoseState.TAKEN
    db.close()


def test_skip_on_decline(tmp_path: Path) -> None:
    machine, db = _machine(tmp_path, answers="yes\n", auto_taken=True)
    med, _ = setup_medicine_schedule(
        db,
        machine.buddy,
        name="Aspirin",
        weekdays_raw="daily",
        time_raw="09:00",
    )
    machine.buddy = ConsoleBuddy(StringIO("no\n"))
    final = machine.run_dose(med.id)
    assert final == DoseState.SKIPPED
    db.close()


def test_ocr_failure(tmp_path: Path) -> None:
    machine, db = _machine(
        tmp_path,
        answers="yes\n",
        tray=["EMPTY", "EMPTY", "EMPTY", "EMPTY"],
    )
    med, _ = setup_medicine_schedule(
        db,
        machine.buddy,
        name="Aspirin",
        weekdays_raw="daily",
        time_raw="09:00",
    )
    machine.buddy = ConsoleBuddy(StringIO("yes\n"))
    final = machine.run_dose(med.id)
    assert final == DoseState.FAILED
    db.close()


def test_low_confidence_fails(tmp_path: Path) -> None:
    machine, db = _machine(
        tmp_path,
        answers="yes\n",
        scores={"ASPIRIN": 0.2},
    )
    med, _ = setup_medicine_schedule(
        db,
        machine.buddy,
        name="Aspirin",
        weekdays_raw="daily",
        time_raw="09:00",
    )
    machine.buddy = ConsoleBuddy(StringIO("yes\n"))
    final = machine.run_dose(med.id)
    assert final == DoseState.FAILED
    db.close()
