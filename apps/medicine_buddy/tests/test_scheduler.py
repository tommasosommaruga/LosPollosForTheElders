from __future__ import annotations

from datetime import datetime
from pathlib import Path

from medicine_buddy.db import Database
from medicine_buddy.scheduler import find_due_doses
from medicine_buddy.states import DoseState


def _db_with_9am(tmp_path: Path, weekdays: str = "*") -> Database:
    db = Database(tmp_path / "s.db")
    med = db.add_medicine("Aspirin")
    db.add_schedule(med.id, weekdays, "09:00")
    return db


def test_find_due_daily(tmp_path: Path) -> None:
    db = _db_with_9am(tmp_path)
    due = find_due_doses(db, datetime(2026, 11, 14, 9, 0, 0))
    assert len(due) == 1
    assert due[0].medicine_name == "Aspirin"
    db.close()


def test_due_window_not_just_exact_minute(tmp_path: Path) -> None:
    """Regression: asking at 09:01 used to say nothing was due."""
    db = _db_with_9am(tmp_path)
    assert len(find_due_doses(db, datetime(2026, 11, 14, 9, 1))) == 1
    assert len(find_due_doses(db, datetime(2026, 11, 14, 8, 50))) == 1   # 10 min early
    assert len(find_due_doses(db, datetime(2026, 11, 14, 9, 55))) == 1   # 55 min late
    db.close()


def test_not_due_outside_window(tmp_path: Path) -> None:
    db = _db_with_9am(tmp_path)
    assert find_due_doses(db, datetime(2026, 11, 14, 8, 40)) == []
    assert find_due_doses(db, datetime(2026, 11, 14, 10, 30)) == []
    db.close()


def test_weekday_filter(tmp_path: Path) -> None:
    db = _db_with_9am(tmp_path, weekdays="0")  # Mondays only
    assert find_due_doses(db, datetime(2026, 11, 16, 9, 0)) != []  # Monday
    assert find_due_doses(db, datetime(2026, 11, 14, 9, 0)) == []  # Saturday
    db.close()


def test_inactive_schedule_not_due(tmp_path: Path) -> None:
    db = _db_with_9am(tmp_path)
    [s] = db.list_active_schedules()
    db.deactivate_schedule(s.id)
    assert find_due_doses(db, datetime(2026, 11, 14, 9, 0)) == []
    db.close()


def test_taken_dose_no_longer_due(tmp_path: Path) -> None:
    now = datetime.now().replace(second=0, microsecond=0)
    db = Database(tmp_path / "s.db")
    med = db.add_medicine("Aspirin")
    db.add_schedule(med.id, "*", now.strftime("%H:%M"))
    assert len(find_due_doses(db, now)) == 1
    for state in [DoseState.DUE, DoseState.REMINDED, DoseState.CONFIRMED, DoseState.LOCATING,
                  DoseState.PICKING, DoseState.PLACED, DoseState.WATCHING, DoseState.TAKEN]:
        db.record_transition(med.id, state)
    assert find_due_doses(db, now) == []
    db.close()
