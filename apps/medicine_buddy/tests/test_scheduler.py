from __future__ import annotations

from datetime import datetime
from pathlib import Path

from medicine_buddy.db import Database
from medicine_buddy.scheduler import find_due_doses


def test_find_due_daily(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    med = db.add_medicine("Aspirin")
    db.add_schedule(med.id, "*", "09:00")
    when = datetime(2026, 11, 14, 9, 0, 0)
    due = find_due_doses(db, when)
    assert len(due) == 1
    assert due[0].medicine_name == "Aspirin"
    db.close()


def test_not_due_wrong_minute(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    med = db.add_medicine("Aspirin")
    db.add_schedule(med.id, "*", "09:00")
    when = datetime(2026, 11, 14, 9, 1, 0)
    assert find_due_doses(db, when) == []
    db.close()
