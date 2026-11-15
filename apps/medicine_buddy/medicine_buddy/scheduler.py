"""Due-dose detection from active schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

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


@dataclass(frozen=True)
class UpcomingDose:
    """A schedule whose time is still ahead, within the wake-greeting window."""

    medicine_id: int
    schedule_id: int
    medicine_name: str
    time_hhmm: str
    person_name: str | None = None


# How far ahead the wake greeting looks for "coming up soon".
UPCOMING_WITHIN = timedelta(minutes=30)


def _weekday_matches(schedule: Schedule, when: datetime) -> bool:
    if schedule.weekdays.strip() == "*":
        return True
    allowed = {int(p) for p in schedule.weekdays.split(",") if p.strip() != ""}
    # Python: Monday=0 .. Sunday=6
    return when.weekday() in allowed


def dose_handled(db: Database, medicine_id: int, since: datetime) -> bool:
    """True if this medicine was taken or skipped since `since` (aware datetime)."""
    return _already_handled(db, medicine_id, since)


def _already_handled(db: Database, medicine_id: int, since: datetime) -> bool:
    """True if this medicine was taken or skipped since `since` (aware datetime)."""
    for event in reversed(db.list_dose_events(medicine_id)):
        at = datetime.fromisoformat(event.created_at)
        if at < since:
            return False
        if event.state in _DONE_STATES:
            return True
    return False


def _as_now(when: datetime | None, tz: tzinfo | None) -> datetime:
    """An aware 'now' in the family's time zone (tz=None: the machine's own zone)."""
    if when is None:
        return datetime.now(tz) if tz else datetime.now().astimezone()
    if when.tzinfo is None:  # naive: wall-clock time in the family's zone
        return when.replace(tzinfo=tz) if tz else when.astimezone()
    return when.astimezone(tz) if tz else when


def find_due_doses(db: Database, when: datetime | None = None, tz: tzinfo | None = None) -> list[DueDose]:
    """Active schedules whose time is within [-DUE_BEFORE, +DUE_AFTER] of `when` today,
    skipping doses already taken/skipped in that window. `tz`: the family's time zone."""
    now = _as_now(when, tz)
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


def find_upcoming_doses(
    db: Database,
    when: datetime | None = None,
    *,
    within: timedelta = UPCOMING_WITHIN,
    tz: tzinfo | None = None,
) -> list[UpcomingDose]:
    """Active schedules whose time is strictly after `when` and within `within`.

    Used for the wake greeting ("what's coming in the next half hour"). Already
    taken/skipped doses for that slot are omitted. Results are sorted by time.
    """
    now = _as_now(when, tz)
    horizon = now + within
    upcoming: list[UpcomingDose] = []
    for schedule in db.list_active_schedules():
        hour, minute = (int(x) for x in schedule.time_hhmm.split(":"))
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not (now < scheduled <= horizon):
            continue
        if not _weekday_matches(schedule, scheduled):
            continue
        if _already_handled(db, schedule.medicine_id, scheduled - DUE_BEFORE):
            continue
        medicine = db.get_medicine(schedule.medicine_id)
        if medicine is None:
            continue
        person = db.get_person(medicine.person_id) if medicine.person_id else None
        upcoming.append(
            UpcomingDose(
                medicine_id=medicine.id,
                schedule_id=schedule.id,
                medicine_name=medicine.name,
                time_hhmm=schedule.time_hhmm,
                person_name=person.name if person else None,
            )
        )
    upcoming.sort(key=lambda d: d.time_hhmm)
    return upcoming
