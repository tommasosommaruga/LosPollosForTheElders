"""Due-dose detection from active schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from medicine_buddy.db import Database, Schedule
from medicine_buddy.states import DoseState

# A dose is "due" from DUE_BEFORE before its time until DUE_AFTER after it.
# (Previously only the exact minute matched, so asking at 09:01 found nothing.)
DUE_BEFORE = timedelta(minutes=15)
DUE_AFTER = timedelta(minutes=60)
_DONE_STATES = {DoseState.TAKEN, DoseState.SKIPPED}


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


def _already_handled(db: Database, medicine_id: int, since: datetime) -> bool:
    """True if this medicine was taken or skipped since `since` (aware datetime)."""
    for event in reversed(db.list_dose_events(medicine_id)):
        at = datetime.fromisoformat(event.created_at)
        if at < since:
            return False
        if event.state in _DONE_STATES:
            return True
    return False


def find_due_doses(db: Database, when: datetime | None = None) -> list[DueDose]:
    """Active schedules whose time is within [-DUE_BEFORE, +DUE_AFTER] of `when` today,
    skipping doses already taken/skipped in that window."""
    now = (when or datetime.now()).astimezone()  # naive -> local time
    due: list[DueDose] = []
    for schedule in db.list_active_schedules():
        hour, minute = (int(x) for x in schedule.time_hhmm.split(":"))
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not (scheduled - DUE_BEFORE <= now <= scheduled + DUE_AFTER):
            continue
        if not _weekday_matches(schedule, scheduled):
            continue
        if _already_handled(db, schedule.medicine_id, scheduled - DUE_BEFORE):
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
