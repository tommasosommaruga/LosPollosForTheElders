"""Mirror active medicine schedules into calendar_events for unified UI/list.

Export ICS still builds medicine VEVENTs from ``schedules`` only so mirrored
rows do not duplicate feed entries.
"""

from __future__ import annotations

from medicine_buddy.calendar_ics import medicine_summary
from medicine_buddy.db import CalendarEvent, Database, Medicine, Schedule


def medicine_mirror_uid(schedule_id: int) -> str:
    return f"mb-med-{schedule_id}@medicine-buddy"


def mirror_schedule(db: Database, schedule: Schedule, medicine: Medicine) -> CalendarEvent:
    """Upsert an active medicine calendar row for this schedule."""
    return db.upsert_calendar_event(
        uid=medicine_mirror_uid(schedule.id),
        kind="medicine",
        title=medicine_summary(medicine.name),
        description="Medicine reminder.",
        weekdays=schedule.weekdays,
        time_hhmm=schedule.time_hhmm,
        person_id=medicine.person_id,
        medicine_name=medicine.name,
        source="local",
        status="active",
    )


def cancel_mirror(db: Database, schedule_id: int) -> CalendarEvent | None:
    """Mark the mirrored row cancelled. Returns None if no row exists."""
    existing = db.get_calendar_event_by_uid(medicine_mirror_uid(schedule_id))
    if existing is None:
        return None
    return db.set_calendar_event_status(existing.id, "cancelled")


def backfill_medicine_mirrors(db: Database) -> int:
    """Ensure every active schedule has an active medicine mirror row. Returns count upserted.

    Also cancels legacy active ``kind=medicine`` rows whose uid is not ``mb-med-*``
    so import leftovers cannot sit beside the stable mirror.
    """
    count = 0
    for schedule in db.list_active_schedules():
        medicine = db.get_medicine(schedule.medicine_id)
        if medicine is None:
            continue
        mirror_schedule(db, schedule, medicine)
        count += 1
    for ev in db.list_calendar_events(status="active", kind="medicine", limit=500):
        if ev.uid.startswith("mb-med-"):
            continue
        db.set_calendar_event_status(ev.id, "cancelled")
    return count
