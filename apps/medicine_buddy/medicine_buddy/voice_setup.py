"""Interactive setup for medicines and recurrent schedules."""

from __future__ import annotations

from medicine_buddy.db import Database, Medicine, Schedule
from medicine_buddy.ports import BuddyPort

WEEKDAY_NAMES = {
    "mon": "0",
    "monday": "0",
    "tue": "1",
    "tuesday": "1",
    "wed": "2",
    "wednesday": "2",
    "thu": "3",
    "thursday": "3",
    "fri": "4",
    "friday": "4",
    "sat": "5",
    "saturday": "5",
    "sun": "6",
    "sunday": "6",
}


def parse_weekdays(raw: str) -> str:
    text = raw.strip().lower()
    if text in {"daily", "every day", "everyday", "*"}:
        return "*"
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if not parts:
        raise ValueError("No weekdays provided")
    codes: list[str] = []
    for part in parts:
        if part.isdigit() and part in {"0", "1", "2", "3", "4", "5", "6"}:
            codes.append(part)
            continue
        if part not in WEEKDAY_NAMES:
            raise ValueError(f"Unknown weekday: {part}")
        codes.append(WEEKDAY_NAMES[part])
    return ",".join(sorted(set(codes), key=int))


def parse_time_hhmm(raw: str) -> str:
    text = raw.strip()
    if ":" not in text:
        raise ValueError("Time must look like HH:MM")
    hour_s, minute_s = text.split(":", 1)
    hour = int(hour_s)
    minute = int(minute_s)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Time out of range")
    return f"{hour:02d}:{minute:02d}"


def setup_medicine_schedule(
    db: Database,
    buddy: BuddyPort,
    *,
    name: str,
    weekdays_raw: str,
    time_raw: str,
) -> tuple[Medicine, Schedule]:
    weekdays = parse_weekdays(weekdays_raw)
    time_hhmm = parse_time_hhmm(time_raw)
    summary = (
        f"I will remind you for {name} on {weekdays} at {time_hhmm}. Is that correct?"
    )
    if not buddy.ask_yes_no(summary):
        raise ValueError("Setup cancelled by user")
    medicine = db.add_medicine(name)
    schedule = db.add_schedule(medicine.id, weekdays, time_hhmm)
    buddy.speak(f"Saved schedule for {medicine.name}.")
    return medicine, schedule
