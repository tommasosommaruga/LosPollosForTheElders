"""Due-dose detection from active schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from medicine_buddy.db import Database, Schedule


@dataclass(frozen=True)
class DueDose:
    medicine_id: int
    schedule_id: int
    medicine_name: str
    time_hhmm: str


def _weekday_matches(schedule: Schedule, when: datetime) -> bool:
    if schedule.weekdays.strip() == "*":
        return True
    allowed = {int(p) for p in schedule.weekdays.split(",") if p.strip() != ""}
    # Python: Monday=0 .. Sunday=6
    return when.weekday() in allowed


def find_due_doses(db: Database, when: datetime | None = None) -> list[DueDose]:
    """Return active schedules whose HH:MM matches `when` (minute precision)."""
    now = when or datetime.now().astimezone()
    hhmm = now.strftime("%H:%M")
    due: list[DueDose] = []
    for schedule in db.list_active_schedules():
        if schedule.time_hhmm != hhmm:
            continue
        if not _weekday_matches(schedule, now):
            continue
        medicine = db.get_medicine(schedule.medicine_id)
        if medicine is None:
            continue
        due.append(
            DueDose(
                medicine_id=medicine.id,
                schedule_id=schedule.id,
                medicine_name=medicine.name,
                time_hhmm=schedule.time_hhmm,
            )
        )
    return due
