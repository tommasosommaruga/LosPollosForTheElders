"""Database tests: medicines, schedules (add / update / soft delete), events, persistence, threads."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from medicine_buddy.db import Database
from medicine_buddy.states import DoseState


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


# ---------------------------------------------------------------- medicines

def test_add_medicine_is_idempotent_and_case_insensitive(db: Database) -> None:
    a = db.add_medicine("Aspirin")
    b = db.add_medicine("  aspirin ")
    assert a == b
    assert [m.name for m in db.list_medicines()] == ["Aspirin"]
    assert db.get_medicine_by_name("ASPIRIN") == a
    assert db.get_medicine(a.id) == a


def test_add_medicine_rejects_empty_name(db: Database) -> None:
    with pytest.raises(ValueError):
        db.add_medicine("   ")


def test_unknown_lookups_return_none(db: Database) -> None:
    assert db.get_medicine(999) is None
    assert db.get_medicine_by_name("Nope") is None
    assert db.get_schedule(999) is None
    assert db.latest_dose_state(999) is None


# ---------------------------------------------------------------- schedules

def test_add_and_list_schedules_sorted_by_time(db: Database) -> None:
    asp = db.add_medicine("Aspirin")
    vit = db.add_medicine("Vitamin D")
    db.add_schedule(asp.id, "*", "21:00")
    db.add_schedule(vit.id, "0,2,4", "08:30")
    db.add_schedule(asp.id, "*", "09:00")
    times = [s.time_hhmm for s in db.list_active_schedules()]
    assert times == ["08:30", "09:00", "21:00"]
    assert [s.time_hhmm for s in db.list_schedules_for_medicine(asp.id)] == ["09:00", "21:00"]


def test_find_active_schedule_exact_match_only(db: Database) -> None:
    asp = db.add_medicine("Aspirin")
    s = db.add_schedule(asp.id, "*", "09:00")
    assert db.find_active_schedule(asp.id, "*", "09:00") == s
    assert db.find_active_schedule(asp.id, "*", "09:01") is None
    assert db.find_active_schedule(asp.id, "0", "09:00") is None


def test_update_schedule_time_only_keeps_days(db: Database) -> None:
    asp = db.add_medicine("Aspirin")
    s = db.add_schedule(asp.id, "0,2,4", "09:00")
    updated = db.update_schedule(s.id, time_hhmm="20:00")
    assert (updated.weekdays, updated.time_hhmm, updated.active) == ("0,2,4", "20:00", True)
    assert db.get_schedule(s.id) == updated


def test_update_schedule_days_only_keeps_time(db: Database) -> None:
    asp = db.add_medicine("Aspirin")
    s = db.add_schedule(asp.id, "*", "09:00")
    updated = db.update_schedule(s.id, weekdays="5,6")
    assert (updated.weekdays, updated.time_hhmm) == ("5,6", "09:00")


def test_update_missing_or_inactive_schedule_raises(db: Database) -> None:
    asp = db.add_medicine("Aspirin")
    s = db.add_schedule(asp.id, "*", "09:00")
    db.deactivate_schedule(s.id)
    with pytest.raises(ValueError):
        db.update_schedule(s.id, time_hhmm="10:00")
    with pytest.raises(ValueError):
        db.update_schedule(12345, time_hhmm="10:00")


def test_deactivate_is_soft_delete(db: Database) -> None:
    asp = db.add_medicine("Aspirin")
    s = db.add_schedule(asp.id, "*", "09:00")
    assert db.deactivate_schedule(s.id) is True
    assert db.deactivate_schedule(s.id) is False  # already inactive
    assert db.list_active_schedules() == []
    assert db.list_schedules_for_medicine(asp.id) == []
    kept = db.list_schedules_for_medicine(asp.id, active_only=False)
    assert len(kept) == 1 and kept[0].active is False  # row (and history) kept
    assert db.find_active_schedule(asp.id, "*", "09:00") is None


def test_schedule_requires_existing_medicine(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.add_schedule(999, "*", "09:00")  # foreign key enforced


# ---------------------------------------------------------------- dose events

def test_dose_events_full_cycle_then_new_cycle(db: Database) -> None:
    asp = db.add_medicine("Aspirin")
    for state in [DoseState.DUE, DoseState.REMINDED, DoseState.CONFIRMED, DoseState.LOCATING,
                  DoseState.PICKING, DoseState.PLACED, DoseState.WATCHING, DoseState.TAKEN]:
        db.record_transition(asp.id, state, slot_id=0 if state == DoseState.PICKING else None)
    assert db.latest_dose_state(asp.id) == DoseState.TAKEN
    db.record_transition(asp.id, DoseState.DUE)  # terminal -> new cycle allowed
    assert db.latest_dose_state(asp.id) == DoseState.DUE
    events = db.list_dose_events(asp.id)
    assert len(events) == 9
    assert events[4].slot_id == 0
    assert all(e.created_at.endswith("+00:00") for e in events)  # stored in UTC


def test_events_are_kept_per_medicine(db: Database) -> None:
    asp = db.add_medicine("Aspirin")
    vit = db.add_medicine("Vitamin D")
    db.record_transition(asp.id, DoseState.DUE)
    db.record_transition(vit.id, DoseState.DUE)
    db.record_transition(vit.id, DoseState.REMINDED)
    assert len(db.list_dose_events(asp.id)) == 1
    assert len(db.list_dose_events(vit.id)) == 2
    assert len(db.list_dose_events()) == 3


def test_illegal_transition_is_not_stored(db: Database) -> None:
    asp = db.add_medicine("Aspirin")
    db.record_transition(asp.id, DoseState.DUE)
    with pytest.raises(ValueError):
        db.record_transition(asp.id, DoseState.PICKING)
    assert [e.state for e in db.list_dose_events(asp.id)] == [DoseState.DUE]


# ---------------------------------------------------------------- persistence & threads

def test_data_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    first = Database(path)
    asp = first.add_medicine("Aspirin")
    s = first.add_schedule(asp.id, "*", "09:00")
    first.record_transition(asp.id, DoseState.DUE)
    first.close()

    second = Database(path)
    assert second.get_medicine_by_name("Aspirin") == asp
    assert second.get_schedule(s.id) == s
    assert second.latest_dose_state(asp.id) == DoseState.DUE
    second.close()


def test_concurrent_writes_and_reads_from_threads(db: Database) -> None:
    """The HTTP server shares one Database across threads; nothing may be lost or crash."""
    med = db.add_medicine("Aspirin")
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                db.add_schedule(med.id, "*", f"{n:02d}:{i:02d}")
                db.list_active_schedules()
                db.list_medicines()
        except Exception as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(db.list_active_schedules()) == 8 * 20


def test_count_rows_and_event_limit(db: Database) -> None:
    from medicine_buddy.states import DoseState
    asp = db.add_medicine("Aspirin")
    db.add_schedule(asp.id, "*", "09:00")
    for st in (DoseState.DUE, DoseState.REMINDED, DoseState.CONFIRMED):
        db.record_transition(asp.id, st)
    assert db.count_rows("medicines") == 1 and db.count_rows("dose_events") == 3
    assert db.count_rows("schedules", "active = 1") == 1
    assert [e.state for e in db.list_dose_events(limit=2)] == [DoseState.REMINDED, DoseState.CONFIRMED]   # newest 2, oldest first
    assert len(db.list_dose_events(asp.id)) == 3
    with pytest.raises(ValueError):
        db.count_rows("people; DROP TABLE medicines")
