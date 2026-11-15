"""Companion calendar: ICS export/import, voice CRUD, medicine import proposals.

Medicine schedules remain in ``schedules``; this mixin mirrors them into ICS and
stores companion events (+ pending medicine proposals) in ``calendar_events``.
"""

from __future__ import annotations

import logging
import secrets
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from medicine_buddy.calendar_ics import (
    build_calendar,
    build_vevent,
    medicine_name_from_summary,
    medicine_summary,
    new_uid,
    parse_ics,
)
from medicine_buddy.companion_settings import validate_http_url
from medicine_buddy.db import CalendarEvent
from medicine_buddy.scheduler import find_upcoming_doses
from medicine_buddy.voice_setup import describe_time, describe_weekdays, parse_time_hhmm, parse_weekdays

logger = logging.getLogger("medicine_buddy.calendar")

EXPORT_TOKEN_KEY = "calendar_export_token"
IMPORT_URL_KEY = "calendar_import_url"
LAST_SYNC_KEY = "calendar_last_sync"
LAST_SYNC_ERROR_KEY = "calendar_last_sync_error"
SYNC_INTERVAL_S = 15 * 60
FETCH_TIMEOUT_S = 12
FETCH_MAX_BYTES = 1_500_000


def _event_json(ev: CalendarEvent) -> dict[str, Any]:
    return {
        "id": ev.id,
        "uid": ev.uid,
        "kind": ev.kind,
        "title": ev.title,
        "description": ev.description,
        "weekdays": ev.weekdays,
        "time_hhmm": ev.time_hhmm,
        "starts_at": ev.starts_at,
        "all_day": ev.all_day,
        "person_id": ev.person_id,
        "medicine_name": ev.medicine_name,
        "source": ev.source,
        "status": ev.status,
    }


class CalendarMixin:
    """Mixed into MedicineBuddyService (needs self.db, self.catalog, self.tz, access helpers)."""

    def _init_calendar(self) -> None:
        if not self.calendar_export_token():
            self.db.set_setting(EXPORT_TOKEN_KEY, secrets.token_urlsafe(18))

    def calendar_export_token(self) -> str:
        return (self.db.get_setting(EXPORT_TOKEN_KEY) or "").strip()

    def calendar_import_url(self) -> str:
        return (self.db.get_setting(IMPORT_URL_KEY) or "").strip()

    def rotate_calendar_token(self) -> str:
        token = secrets.token_urlsafe(18)
        self.db.set_setting(EXPORT_TOKEN_KEY, token)
        return token

    def build_export_ics(self) -> str:
        """Active medicine schedules + active companion events as one ICS document."""
        blocks: list[list[str]] = []
        names = {p.id: p.name for p in self.db.list_people()}
        for schedule in self.db.list_active_schedules():
            med = self.db.get_medicine(schedule.medicine_id)
            if med is None:
                continue
            who = names.get(med.person_id) if med.person_id else None
            desc = f"Medicine reminder for {who}." if who else "Medicine reminder."
            blocks.append(
                build_vevent(
                    uid=f"mb-med-{schedule.id}@medicine-buddy",
                    summary=medicine_summary(med.name),
                    description=desc,
                    time_hhmm=schedule.time_hhmm,
                    weekdays=schedule.weekdays,
                )
            )
        for ev in self.db.list_calendar_events(status="active", kind="companion"):
            blocks.append(
                build_vevent(
                    uid=ev.uid,
                    summary=ev.title,
                    description=ev.description,
                    time_hhmm=ev.time_hhmm or None,
                    weekdays=ev.weekdays or "*",
                    starts_date=_starts_date(ev.starts_at),
                    all_day=ev.all_day or not ev.time_hhmm,
                )
            )
        return build_calendar(blocks)

    def export_ics_for_token(self, token: str) -> str | None:
        expected = self.calendar_export_token()
        if not expected or not token or len(token) != len(expected):
            return None
        if not secrets.compare_digest(token, expected):
            return None
        return self.build_export_ics()

    def settings_calendar(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        if body.get("rotate_token"):
            self.rotate_calendar_token()
        if "import_url" in body:
            try:
                cleaned = validate_http_url(str(body.get("import_url") or ""), what="calendar URL")
            except ValueError as exc:
                return {"error": str(exc)}
            self.db.set_setting(IMPORT_URL_KEY, cleaned)
        if body.get("sync"):
            sync = self.sync_calendar_import()
            if sync.get("error") and not sync.get("status"):
                return sync
        proposals = [_event_json(e) for e in self.db.list_calendar_events(status="proposed", kind="medicine")]
        upcoming = self._upcoming_companion_summaries(limit=8)
        token = self.calendar_export_token()
        return {
            "status": "ok",
            "export_token": token,
            "export_path": f"/calendar/{token}.ics" if token else "",
            "import_url": self.calendar_import_url(),
            "last_sync": self.db.get_setting(LAST_SYNC_KEY) or "",
            "last_sync_error": self.db.get_setting(LAST_SYNC_ERROR_KEY) or "",
            "proposals": proposals,
            "upcoming": upcoming,
            "message": (
                "Calendar ready — subscribe with the export link; paste an ICS URL to import."
                if token else
                "Calendar token missing; rotate to create one."
            ),
        }

    def accept_calendar_proposal(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        try:
            event_id = int(body.get("id"))
        except (TypeError, ValueError):
            return {"error": "id is required"}
        person_name = body.get("person")
        ev = self.db.get_calendar_event(event_id)
        if ev is None or ev.status != "proposed" or ev.kind != "medicine":
            return {"error": "No pending medicine proposal with that id"}
        name = ev.medicine_name or ev.title
        matched = self.catalog.match(name)
        if matched.name is None:
            return {
                "error": "medicine not recognized",
                "suggestions": matched.suggestions,
                "speak_hint": "That medicine name is not on the allowed list.",
            }
        if not ev.time_hhmm:
            return {"error": "proposal has no time"}
        weekdays = ev.weekdays or "*"
        # Reuse the normal schedule path (auth via Family page token already checked).
        person = None
        if person_name:
            person = self.db.get_person_by_name(str(person_name))
            if person is None:
                return {"error": f"Unknown person {person_name!r}"}
        elif ev.person_id:
            person = self.db.get_person(ev.person_id)
        with self._lock:
            medicine = self.db.add_medicine(matched.name, person.id if person else None)
            existing = self.db.find_active_schedule(medicine.id, weekdays, ev.time_hhmm)
            if existing is None:
                self.db.add_schedule(medicine.id, weekdays, ev.time_hhmm)
            self.db.set_calendar_event_status(event_id, "active")
            # Keep proposal row as medicine/active for history, or clear — mark active but unused.
            self.db.upsert_calendar_event(
                uid=ev.uid,
                kind="medicine",
                title=medicine_summary(matched.name),
                description=ev.description,
                weekdays=weekdays,
                time_hhmm=ev.time_hhmm,
                starts_at=ev.starts_at,
                all_day=False,
                person_id=person.id if person else None,
                medicine_name=matched.name,
                source=ev.source,
                status="active",
            )
        when = f"{describe_weekdays(weekdays)} at {describe_time(ev.time_hhmm)}"
        return {
            "status": "ok",
            "medicine": matched.name,
            "message": f"Accepted: {matched.name} {when} is now on the medicine schedule.",
        }

    def reject_calendar_proposal(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        try:
            event_id = int(body.get("id"))
        except (TypeError, ValueError):
            return {"error": "id is required"}
        ev = self.db.get_calendar_event(event_id)
        if ev is None or ev.status != "proposed":
            return {"error": "No pending proposal with that id"}
        self.db.set_calendar_event_status(event_id, "rejected")
        return {"status": "ok", "message": f"Rejected “{ev.title}”."}

    def sync_calendar_import(self, *, ics_text: str | None = None) -> dict[str, Any]:
        """Fetch (or parse) ICS; companion events upsert; medicine → proposals only."""
        text = ics_text
        url = self.calendar_import_url()
        if text is None:
            if not url:
                return {"error": "No import calendar URL saved yet."}
            try:
                text = _fetch_ics(url)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)[:200]
                self.db.set_setting(LAST_SYNC_ERROR_KEY, msg)
                logger.info("Calendar import failed: %s", msg)
                return {"error": f"Could not fetch calendar: {msg}"}
        parsed = parse_ics(text)
        companion_n = proposal_n = skipped = 0
        for item in parsed:
            if item.medicine_hint or self._looks_like_medicine(item.summary):
                hint = item.medicine_hint or item.summary
                match = self.catalog.match(hint)
                if match.name is None or not item.time_hhmm:
                    skipped += 1
                    continue
                existing = self.db.get_calendar_event_by_uid(item.uid)
                if existing and existing.status in {"active", "rejected"}:
                    # Already accepted into schedules or dismissed.
                    skipped += 1
                    continue
                self.db.upsert_calendar_event(
                    uid=item.uid,
                    kind="medicine",
                    title=medicine_summary(match.name),
                    description=item.description,
                    weekdays=item.weekdays or "*",
                    time_hhmm=item.time_hhmm,
                    starts_at=item.starts_at or "",
                    all_day=False,
                    medicine_name=match.name,
                    source="import",
                    status="proposed",
                )
                proposal_n += 1
                continue
            # Companion event
            if not item.summary:
                skipped += 1
                continue
            self.db.upsert_calendar_event(
                uid=item.uid,
                kind="companion",
                title=item.summary,
                description=item.description,
                weekdays=item.weekdays or "",
                time_hhmm=item.time_hhmm or "",
                starts_at=item.starts_at or "",
                all_day=item.all_day,
                source="import",
                status="active",
            )
            companion_n += 1
        now = datetime.now().astimezone().replace(microsecond=0).isoformat()
        self.db.set_setting(LAST_SYNC_KEY, now)
        self.db.set_setting(LAST_SYNC_ERROR_KEY, "")
        return {
            "status": "ok",
            "companion_upserted": companion_n,
            "proposals": proposal_n,
            "skipped": skipped,
            "message": (
                f"Synced: {companion_n} companion event(s), {proposal_n} medicine proposal(s)"
                + (f", {skipped} skipped." if skipped else ".")
            ),
        }

    def _looks_like_medicine(self, summary: str) -> bool:
        match = self.catalog.match(summary or "")
        return match.name is not None and match.how in {"exact", "alias", "fuzzy"}

    def list_upcoming_events(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Voice/tool: medicines due soon + companion events."""
        del body  # reserved for filters later
        upcoming_meds = find_upcoming_doses(self.db, tz=self.tz())
        companion = self._upcoming_companion_summaries(limit=10)
        lines: list[str] = []
        for d in upcoming_meds:
            lines.append(f"Medicine: {d.medicine_name} at {describe_time(d.time_hhmm)}")
        lines.extend(companion)
        if not lines:
            hint = "Nothing special coming up soon. You can add a medicine reminder or a calendar event."
        else:
            hint = "Coming up: " + "; ".join(lines) + "."
        return {
            "status": "ok",
            "medicines": [
                {"name": d.medicine_name, "time_hhmm": d.time_hhmm, "schedule_id": d.schedule_id}
                for d in upcoming_meds
            ],
            "companion": companion,
            "speak_hint": hint,
        }

    def create_calendar_event(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Voice/tool: create a companion (non-medicine) event. Medicines use schedule_medicine."""
        body = body or {}
        title = " ".join(str(body.get("title") or body.get("name") or "").split())
        if not title:
            return {"error": "title is required", "speak_hint": "What should I put on the calendar?"}
        # Never create medicine schedules through this path.
        if medicine_name_from_summary(title) or self._looks_like_medicine(title):
            return {
                "error": "use schedule_medicine for medicines",
                "speak_hint": (
                    "That sounds like a medicine. Use the medicine reminder tools instead of "
                    "the general calendar."
                ),
            }
        time_raw = str(body.get("time") or body.get("time_hhmm") or "").strip()
        weekdays_raw = str(body.get("weekdays") or body.get("days") or "daily").strip()
        try:
            time_hhmm = parse_time_hhmm(time_raw) if time_raw else ""
            weekdays = parse_weekdays(weekdays_raw) if time_hhmm else ""
        except ValueError as exc:
            return {"error": str(exc), "speak_hint": f"I didn't understand the time or day. {exc}."}
        person = None
        if body.get("person"):
            person, err = self._resolve_person(body.get("person"), for_change=False)
            if err and err.get("error"):
                return err
        uid = new_uid("local")
        ev = self.db.upsert_calendar_event(
            uid=uid,
            kind="companion",
            title=title,
            description=str(body.get("description") or "").strip()[:500],
            weekdays=weekdays,
            time_hhmm=time_hhmm,
            starts_at=str(body.get("date") or date.today().isoformat()),
            all_day=not bool(time_hhmm),
            person_id=person.id if person else None,
            source="local",
            status="active",
        )
        when = (
            f"{describe_weekdays(weekdays)} at {describe_time(time_hhmm)}"
            if time_hhmm else
            "as an all-day note"
        )
        return {
            "status": "ok",
            "event": _event_json(ev),
            "speak_hint": f"I've put “{title}” on the calendar {when}.",
        }

    def cancel_calendar_event(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        title = " ".join(str(body.get("title") or body.get("name") or "").split()).lower()
        event_id = body.get("id")
        target: CalendarEvent | None = None
        if event_id is not None:
            try:
                target = self.db.get_calendar_event(int(event_id))
            except (TypeError, ValueError):
                target = None
        if target is None and title:
            for ev in self.db.list_calendar_events(status="active", kind="companion"):
                if ev.title.lower() == title or title in ev.title.lower():
                    target = ev
                    break
        if target is None or target.kind != "companion":
            return {
                "error": "event not found",
                "speak_hint": "I couldn't find that calendar event. Medicine reminders are removed with delete_medicine_schedule.",
            }
        if not body.get("confirmed"):
            return {
                "status": "needs_confirmation",
                "event": _event_json(target),
                "speak_hint": f"Should I remove “{target.title}” from the calendar?",
            }
        self.db.set_calendar_event_status(target.id, "cancelled")
        return {
            "status": "ok",
            "speak_hint": f"Okay, I removed “{target.title}” from the calendar.",
        }

    def _upcoming_companion_summaries(self, *, limit: int = 8) -> list[str]:
        out: list[str] = []
        now = datetime.now(self.tz()) if self.tz() else datetime.now().astimezone()
        for ev in self.db.list_calendar_events(status="active", kind="companion", limit=100):
            if not ev.time_hhmm:
                out.append(ev.title)
            else:
                try:
                    hour, minute = (int(x) for x in ev.time_hhmm.split(":"))
                except ValueError:
                    out.append(ev.title)
                    continue
                scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if ev.weekdays and ev.weekdays != "*":
                    allowed = {int(p) for p in ev.weekdays.split(",") if p.strip() != ""}
                    if now.weekday() not in allowed and scheduled.weekday() not in allowed:
                        # Still list recurring items for awareness.
                        pass
                label = f"{ev.title} at {describe_time(ev.time_hhmm)}"
                if scheduled + timedelta(hours=12) >= now >= scheduled - timedelta(hours=1):
                    out.insert(0, label)
                else:
                    out.append(label)
            if len(out) >= limit:
                break
        return out[:limit]


def _starts_date(starts_at: str) -> date | None:
    if not starts_at:
        return None
    try:
        if "T" in starts_at:
            return datetime.fromisoformat(starts_at).date()
        return date.fromisoformat(starts_at[:10])
    except ValueError:
        return None


class CalendarSyncLoop:
    """Background refresh of the pasted import ICS URL (no-op when unset)."""

    def __init__(self, service: Any, *, interval_s: float = SYNC_INTERVAL_S) -> None:
        self.service = service
        self.interval_s = max(60.0, float(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="calendar-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "interval_s": self.interval_s,
            "import_configured": bool(self.service.calendar_import_url()),
            "last_sync": self.service.db.get_setting(LAST_SYNC_KEY) or "",
            "last_sync_error": self.service.db.get_setting(LAST_SYNC_ERROR_KEY) or "",
        }

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            if not self.service.calendar_import_url():
                continue
            try:
                result = self.service.sync_calendar_import()
                logger.info("Calendar sync: %s", result.get("message") or result.get("error"))
            except Exception as exc:  # noqa: BLE001
                logger.info("Calendar sync crashed: %s", exc)


def _fetch_ics(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http(s) calendar URLs are allowed")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "MedicineBuddy/0.8 (+companion-calendar)", "Accept": "text/calendar, text/plain, */*"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:  # noqa: S310 — URL validated
        raw = resp.read(FETCH_MAX_BYTES + 1)
    if len(raw) > FETCH_MAX_BYTES:
        raise ValueError("Calendar file is too large")
    return raw.decode("utf-8", errors="replace")
