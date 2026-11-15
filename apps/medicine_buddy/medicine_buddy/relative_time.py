"""Parse relative time phrases into half-open [start, end) windows in local TZ."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})$")
_ISO_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")
_IN_N_DAYS = re.compile(r"^in\s+(\d+)\s+days?$")
_N_DAYS_AGO = re.compile(r"^(\d+)\s+days?\s+ago$")

_UNKNOWN_HINT = (
    "I could not understand that time. "
    "Try today, tomorrow, next week, in 3 days, a weekday, or YYYY-MM-DD."
)

# Phrases that mean a single calendar day (not a weekly recurrence).
_ONE_SHOT_KEYS = frozenset({"today", "tonight", "tomorrow", "yesterday"})


def is_one_shot_when(text: str) -> bool:
    """True for today/tomorrow/ISO/in-N-days — voice should create one event, not RRULE."""
    key = re.sub(r"\s+", " ", (text or "").strip().casefold())
    if not key:
        return False
    if key in _ONE_SHOT_KEYS:
        return True
    if _ISO_DATE.match(key):
        return True
    if _IN_N_DAYS.match(key) or _N_DAYS_AGO.match(key):
        return True
    return False


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime
    label: str


def parse_window(text: str, now: datetime) -> TimeWindow:
    """Parse a relative phrase into a half-open [start, end) in ``now``'s timezone.

    Week starts Monday (local). Raises ``ValueError`` with a friendly message if unknown.
    Empty / ``soon`` are intentionally not handled here (caller default).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    raw = (text or "").strip()
    key = re.sub(r"\s+", " ", raw.casefold())
    if not key:
        raise ValueError(_UNKNOWN_HINT)

    local_today = now.date()

    if key in {"today", "tonight"}:
        return _day_window(local_today, now, label=key)

    if key == "yesterday":
        return _day_window(local_today - timedelta(days=1), now, label=key)

    if key == "tomorrow":
        return _day_window(local_today + timedelta(days=1), now, label=key)

    if key == "this week":
        start_d = _monday_on_or_before(local_today)
        return _range_window(start_d, start_d + timedelta(days=7), now, label=key)

    if key == "next week":
        start_d = _monday_on_or_before(local_today) + timedelta(days=7)
        return _range_window(start_d, start_d + timedelta(days=7), now, label=key)

    if key == "last week":
        start_d = _monday_on_or_before(local_today) - timedelta(days=7)
        return _range_window(start_d, start_d + timedelta(days=7), now, label=key)

    m = _IN_N_DAYS.match(key)
    if m:
        n = int(m.group(1))
        return _day_window(local_today + timedelta(days=n), now, label=raw.strip())

    m = _N_DAYS_AGO.match(key)
    if m:
        n = int(m.group(1))
        return _day_window(local_today - timedelta(days=n), now, label=raw.strip())

    m = _ISO_RANGE.match(key)
    if m:
        start_d = date.fromisoformat(m.group(1))
        end_inclusive = date.fromisoformat(m.group(2))
        if end_inclusive < start_d:
            raise ValueError(_UNKNOWN_HINT)
        return _range_window(
            start_d, end_inclusive + timedelta(days=1), now, label=raw.strip()
        )

    m = _ISO_DATE.match(key)
    if m:
        return _day_window(date.fromisoformat(m.group(1)), now, label=raw.strip())

    if key in _WEEKDAYS:
        target = _WEEKDAYS[key]
        this_week_day = _monday_on_or_before(local_today) + timedelta(days=target)
        if this_week_day >= local_today:
            day = this_week_day
        else:
            day = this_week_day + timedelta(days=7)
        return _day_window(day, now, label=key)

    raise ValueError(_UNKNOWN_HINT)


def _monday_on_or_before(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday=0


def _at_midnight(d: date, now: datetime) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=now.tzinfo)


def _day_window(d: date, now: datetime, *, label: str) -> TimeWindow:
    start = _at_midnight(d, now)
    end = _at_midnight(d + timedelta(days=1), now)
    return TimeWindow(start=start, end=end, label=label)


def _range_window(start_d: date, end_d: date, now: datetime, *, label: str) -> TimeWindow:
    return TimeWindow(
        start=_at_midnight(start_d, now),
        end=_at_midnight(end_d, now),
        label=label,
    )
