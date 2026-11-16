"""Regression tests for the P0 bugs found in the audit (docs/IMPROVEMENT_PLAN.md)."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.dry_run import ConsoleBuddy, FakeArm, _out
from medicine_buddy.scheduler import find_due_doses
from medicine_buddy.states import DoseState


@pytest.fixture
def svc(tmp_path: Path):
    s = MedicineBuddyService(tmp_path / "p0.db", AppConfig(dry_run=True, tray_labels=["ASPIRIN"]),
                             MedicineCatalog({"Aspirin": []}))
    s.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    yield s
    s.close()


# ---- #3 the simulated arm must not crash on narrow console encodings

class _Cp1252Out(io.TextIOWrapper):
    pass


def test_dry_run_output_survives_narrow_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    narrow = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", narrow)
    _out("slot 0 → bowl")           # an arrow cannot be encoded in cp1252
    ConsoleBuddy().speak("snowman ☃")
    FakeArm().replay_slot_to_bowl(0)     # used to raise UnicodeEncodeError
    narrow.flush()
    assert b"bowl" in raw.getvalue()


def test_dispense_succeeds_even_with_narrow_console(svc: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict"))
    r = svc.run_dose({"medicine_name": "Aspirin", "confirmed": True})
    assert r["final_state"] == "Placed", r


def test_simulated_arm_failure_does_not_blame_the_estop(svc: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self, slot_id):  # noqa: ANN001
        raise RuntimeError("software bug")
    monkeypatch.setattr(FakeArm, "replay_slot_to_bowl", boom)
    r = svc.run_dose({"medicine_name": "Aspirin", "confirmed": True})
    assert r["final_state"] == "Failed"
    assert "e-stop" not in r["speak_hint"] and "simulated" in r["speak_hint"]


# ---- #4 demo mode is announced, never "it is in the bowl"

def test_demo_mode_is_announced(svc: MedicineBuddyService) -> None:
    r = svc.run_dose({"medicine_name": "Aspirin", "confirmed": True})
    assert r["demo_mode"] is True and r["final_state"] == "Placed"
    assert "Demo mode" in r["speak_hint"] and "own box" in r["speak_hint"]
    assert "is in the bowl" not in r["speak_hint"] and all("in the bowl" not in m for m in r["messages"])
    assert "DEMO MODE" in svc.status_page()


def test_real_arm_still_says_bowl(tmp_path: Path) -> None:
    s = MedicineBuddyService(tmp_path / "hw.db", AppConfig(dry_run=False, tray_labels=["ASPIRIN"]),
                             MedicineCatalog({"Aspirin": []}))
    s.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    # the fake ports are still used by the service, so only the wording path is checked here
    r = s.run_dose({"medicine_name": "Aspirin", "confirmed": True})
    assert r["demo_mode"] is False and "bowl" in r["speak_hint"].lower()
    s.close()


# ---- #2 "I took it" works with no dispense

def test_taken_without_dispense(svc: MedicineBuddyService) -> None:
    r = svc.mark_adherence({"medicine_name": "Aspirin", "taken": True})
    assert r.get("final_state") == "Taken", r
    states = [e.state for e in svc.db.list_dose_events()]
    assert states == [DoseState.DUE, DoseState.TAKEN]


def test_missed_without_dispense(svc: MedicineBuddyService) -> None:
    assert svc.mark_adherence({"medicine_name": "Aspirin", "taken": False})["final_state"] == "Missed"


def test_taken_after_a_reminder(svc: MedicineBuddyService) -> None:
    med = svc.db.get_medicine_by_name("Aspirin")
    svc.db.record_transition(med.id, DoseState.DUE, from_state=DoseState.IDLE)
    svc.db.record_transition(med.id, DoseState.REMINDED)
    assert svc.mark_adherence({"medicine_name": "Aspirin", "taken": True})["final_state"] == "Taken"


def test_taken_still_works_after_dispense(svc: MedicineBuddyService) -> None:
    svc.run_dose({"medicine_name": "Aspirin", "confirmed": True})
    assert svc.mark_adherence({"medicine_name": "Aspirin", "taken": True})["final_state"] == "Taken"


def test_taken_twice_starts_a_new_record_each_time(svc: MedicineBuddyService) -> None:
    svc.mark_adherence({"medicine_name": "Aspirin", "taken": True})
    assert svc.mark_adherence({"medicine_name": "Aspirin", "taken": True})["final_state"] == "Taken"


def test_mid_dose_report_is_refused_not_corrupting(svc: MedicineBuddyService) -> None:
    med = svc.db.get_medicine_by_name("Aspirin")
    for st in (DoseState.DUE, DoseState.REMINDED, DoseState.CONFIRMED, DoseState.LOCATING):
        svc.db.record_transition(med.id, st, from_state=DoseState.IDLE if st == DoseState.DUE else None)
    r = svc.mark_adherence({"medicine_name": "Aspirin", "taken": True})
    assert "in progress" in r["error"]
    assert svc.db.latest_dose_state(med.id) == DoseState.LOCATING


def test_manual_taken_makes_the_dose_not_due(tmp_path: Path) -> None:
    from datetime import datetime
    now = datetime.now().replace(second=0, microsecond=0)
    s = MedicineBuddyService(tmp_path / "d.db", AppConfig(dry_run=True), MedicineCatalog({"Aspirin": []}))
    s.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": now.strftime("%H:%M")})
    assert len(find_due_doses(s.db, now)) == 1
    s.mark_adherence({"medicine_name": "Aspirin", "taken": True})
    assert find_due_doses(s.db, now) == []
    s.close()
