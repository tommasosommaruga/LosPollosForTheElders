"""Interactive setup for medicines and recurrent schedules."""

from __future__ import annotations

import re
from datetime import date, timedelta

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


DAY_SPOKEN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def parse_weekdays(raw: str, as_of: date | None = None) -> str:
    """'daily' / 'weekdays' / 'today' / 'tomorrow' / 'Mon, Wed' / names -> '*' or '0,2'.

    ``today`` / ``tomorrow`` become the weekday digit for that calendar day (Mon=0…Sun=6),
    so “tomorrow at 9” stores a recurring schedule for that weekday. ``as_of`` defaults to
    ``date.today()``; callers with a Family TZ clock should pass ``service.now().date()``.
    """
    ref = as_of if as_of is not None else date.today()
    text = raw.strip().lower()
    text = re.sub(r"\bevery\s+", "", text).strip()
    if text in {"daily", "day", "every day", "everyday", "each day", "*", ""}:
        if text == "":
            raise ValueError("No weekdays provided")
        return "*"
    if text in {"weekdays", "weekday", "workdays"}:
        return "0,1,2,3,4"
    if text in {"weekends", "weekend"}:
        return "5,6"
    if text == "today":
        return str(ref.weekday())
    if text == "tomorrow":
        return str((ref + timedelta(days=1)).weekday())
    parts = [p for p in re.split(r"[,;/&]|\band\b|\s+", text) if p.strip()]
    if not parts:
        raise ValueError("No weekdays provided")
    codes: list[str] = []
    for part in parts:
        part = part.strip().rstrip(".")
        if part in {"0", "1", "2", "3", "4", "5", "6"}:
            codes.append(part)
            continue
        if part not in WEEKDAY_NAMES and part.endswith("s") and part[:-1] in WEEKDAY_NAMES:
            part = part[:-1]  # "mondays" -> "monday"
        if part not in WEEKDAY_NAMES:
            raise ValueError(f"Unknown weekday: {part}")
        codes.append(WEEKDAY_NAMES[part])
    codes = sorted(set(codes), key=int)
    return "*" if len(codes) == 7 else ",".join(codes)


def parse_time_hhmm(raw: str) -> str:
    """'09:00', '9:5', '9', '9 am', '8:30 PM', '21.30', '2130', 'noon', 'midnight' -> 'HH:MM'."""
    text = raw.strip().lower().replace(".", ":").replace(" o'clock", "").replace("o'clock", "")
    text = text.replace("a.m.", "am").replace("p.m.", "pm")
    if text in {"noon", "midday"}:
        return "12:00"
    if text == "midnight":
        return "00:00"
    m = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(am|pm)?", text.replace(" ", ""))
    if not m:
        raise ValueError(f"Time must look like HH:MM or '9 AM', got {raw!r}")
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = m.group(3)
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError("Time out of range")
        hour = (hour % 12) + (12 if meridiem == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Time out of range")
    return f"{hour:02d}:{minute:02d}"


def describe_weekdays(weekdays: str) -> str:
    """Stored weekdays -> words Reachy can say: 'every day', 'every Monday', 'on weekdays'."""
    if weekdays.strip() == "*":
        return "every day"
    codes = [int(c) for c in weekdays.split(",") if c.strip()]
    if codes == [0, 1, 2, 3, 4]:
        return "on weekdays"
    if codes == [5, 6]:
        return "on weekends"
    names = [DAY_SPOKEN[c] for c in codes]
    if len(names) == 1:
        return f"every {names[0]}"
    return "on " + ", ".join(names[:-1]) + " and " + names[-1]


def describe_time(time_hhmm: str) -> str:
    """'09:00' -> '9 AM', '20:30' -> '8:30 PM'."""
    hour, minute = (int(x) for x in time_hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12} {suffix}" if minute == 0 else f"{h12}:{minute:02d} {suffix}"


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
        f"I will remind you for {name} {describe_weekdays(weekdays)} at "
        f"{describe_time(time_hhmm)}. Is that correct?"
    )
    if not buddy.ask_yes_no(summary):
        raise ValueError("Setup cancelled by user")
    medicine = db.add_medicine(name)
    schedule = db.add_schedule(medicine.id, weekdays, time_hhmm)
    buddy.speak(f"Saved schedule for {medicine.name}.")
    return medicine, schedule
