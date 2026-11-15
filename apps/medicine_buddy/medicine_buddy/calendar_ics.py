"""Build and parse ICS for Medicine Buddy's companion calendar (stdlib only).

Medicine events use SUMMARY ``Med: <name>``. Companion events use free-text titles.
Recurrence uses weekly ``BYDAY`` matching our schedule weekday codes (Mon=0 .. Sun=6).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

# ICS BYDAY <-> Python weekday (Mon=0)
_BYDAY = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_BYDAY_INDEX = {d: i for i, d in enumerate(_BYDAY)}

_MED_PREFIX = re.compile(r"^\s*med(?:icine)?\s*:\s*(.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedEvent:
    uid: str
    summary: str
    description: str
    time_hhmm: str | None
    weekdays: str | None  # '*' or '0,2,4' or None for one-shot / unknown
    starts_at: str | None  # ISO date or datetime when one-shot
    all_day: bool
    medicine_hint: str | None  # stripped name after Med:, if present


def medicine_summary(name: str) -> str:
    return f"Med: {name.strip()}"


def medicine_name_from_summary(summary: str) -> str | None:
    m = _MED_PREFIX.match(summary or "")
    return m.group(1).strip() if m else None


def weekdays_to_byday(weekdays: str) -> str:
    """Stored weekdays ('*' or '0,2') -> ICS BYDAY list."""
    raw = (weekdays or "").strip()
    if raw in {"", "*"}:
        return ",".join(_BYDAY)
    codes = sorted({int(p) for p in raw.split(",") if p.strip() != ""})
    return ",".join(_BYDAY[c] for c in codes if 0 <= c <= 6)


def byday_to_weekdays(byday: str) -> str:
    """ICS BYDAY -> stored weekdays ('*' if all seven)."""
    parts = [p.strip().upper() for p in (byday or "").split(",") if p.strip()]
    codes = sorted({_BYDAY_INDEX[p[:2]] for p in parts if p[:2] in _BYDAY_INDEX})
    if not codes or len(codes) == 7:
        return "*"
    return ",".join(str(c) for c in codes)


def _fold(line: str) -> str:
    """ICS line folding at 75 octets (ASCII-safe for our titles)."""
    if len(line) <= 75:
        return line
    chunks = [line[:75]]
    rest = line[75:]
    while rest:
        chunks.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(chunks)


def _escape_text(value: str) -> str:
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _unescape_text(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt == "n" else nxt)
            i += 2
            continue
        out.append(value[i])
        i += 1
    return "".join(out)


def _dtstamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _hhmm_to_ics(time_hhmm: str) -> str:
    hour, minute = (int(x) for x in time_hhmm.split(":"))
    return f"{hour:02d}{minute:02d}00"


def build_vevent(
    *,
    uid: str,
    summary: str,
    description: str = "",
    time_hhmm: str | None = None,
    weekdays: str | None = "*",
    starts_date: date | None = None,
    all_day: bool = False,
) -> list[str]:
    """One VEVENT block (lines without folding)."""
    day = starts_date or date.today()
    stamp = _dtstamp()
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"SUMMARY:{_escape_text(summary)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_escape_text(description)}")
    if all_day or not time_hhmm:
        lines.append(f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}")
    else:
        # Floating local time (family TZ is configured separately in Buddy).
        lines.append(f"DTSTART:{day.strftime('%Y%m%d')}T{_hhmm_to_ics(time_hhmm)}")
        if weekdays is not None:
            lines.append(f"RRULE:FREQ=WEEKLY;BYDAY={weekdays_to_byday(weekdays)}")
    lines.append("END:VEVENT")
    return lines


def build_calendar(events: Iterable[list[str]], *, name: str = "Medicine Buddy") -> str:
    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Medicine Buddy//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape_text(name)}",
    ]
    body: list[str] = []
    for block in events:
        body.extend(block)
    lines = [_fold(line) for line in (*header, *body, "END:VCALENDAR")]
    return "\r\n".join(lines) + "\r\n"


def new_uid(prefix: str = "mb") -> str:
    return f"{prefix}-{uuid.uuid4().hex}@medicine-buddy"


def _unfold(text: str) -> str:
    # Join folded lines (CRLF + space/tab continuation).
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n").replace("\r", "\n"))


def _parse_props(block: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for raw in block.split("\n"):
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key_part, value = line.split(":", 1)
        name = key_part.split(";", 1)[0].upper()
        props[name] = value
        if name == "DTSTART":
            props["_DTSTART_PARAMS"] = key_part
        if name == "RRULE":
            props["RRULE"] = value
    return props


def _parse_dtstart(props: dict[str, str]) -> tuple[str | None, str | None, bool]:
    """Return (time_hhmm, starts_at_iso, all_day)."""
    raw = props.get("DTSTART", "")
    params = props.get("_DTSTART_PARAMS", "")
    if "VALUE=DATE" in params.upper() or (len(raw) == 8 and raw.isdigit()):
        day = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
        return None, day, True
    # 20261115T090000 or with Z
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})", raw)
    if not m:
        return None, None, False
    starts = f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}"
    return f"{m.group(4)}:{m.group(5)}", starts, False


def parse_ics(text: str) -> list[ParsedEvent]:
    """Parse VEVENTs from an ICS document. Tolerant of missing fields."""
    unfolded = _unfold(text or "")
    events: list[ParsedEvent] = []
    for match in re.finditer(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, re.DOTALL | re.IGNORECASE):
        props = _parse_props(match.group(1))
        summary = _unescape_text(props.get("SUMMARY", "")).strip()
        description = _unescape_text(props.get("DESCRIPTION", "")).strip()
        uid = (props.get("UID") or new_uid("import")).strip()
        time_hhmm, starts_at, all_day = _parse_dtstart(props)
        weekdays: str | None = None
        rrule = props.get("RRULE", "")
        if "BYDAY=" in rrule.upper():
            byday = re.search(r"BYDAY=([^;]+)", rrule, re.IGNORECASE)
            if byday:
                weekdays = byday_to_weekdays(byday.group(1))
        elif time_hhmm and not all_day and starts_at:
            # One-shot timed event: use that calendar weekday once.
            try:
                dt = datetime.fromisoformat(starts_at)
                weekdays = str(dt.weekday())
            except ValueError:
                weekdays = None
        events.append(
            ParsedEvent(
                uid=uid,
                summary=summary,
                description=description,
                time_hhmm=time_hhmm,
                weekdays=weekdays,
                starts_at=starts_at,
                all_day=all_day,
                medicine_hint=medicine_name_from_summary(summary),
            )
        )
    return events
