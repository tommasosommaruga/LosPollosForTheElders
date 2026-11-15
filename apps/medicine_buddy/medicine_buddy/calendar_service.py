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
from typing import Any, Literal
from urllib.parse import urlparse

from medicine_buddy.calendar_ics import (
    ParsedEvent,
    build_calendar,
    build_vevent,
    medicine_name_from_summary,
    medicine_summary,
    new_uid,
    parse_ics,
)
from medicine_buddy.calendar_mirror import backfill_medicine_mirrors, medicine_mirror_uid, mirror_schedule
from medicine_buddy.companion_settings import validate_http_url
from medicine_buddy.db import CalendarEvent, Medicine, Schedule
from medicine_buddy.google_calendar import (
    SETTING_CALENDAR_ID,
    build_google_event_body,
    delete_google_event,
    ensure_timezone_name,
    google_write_status,
    insert_google_event,
    list_google_events_as_parsed,
    load_google_cal_config,
    next_occurrence_date,
    probe_google_calendar,
)
from medicine_buddy.relative_time import TimeWindow, is_one_shot_when, parse_window
from medicine_buddy.voice_setup import describe_time, describe_weekdays, parse_time_hhmm, parse_weekdays

logger = logging.getLogger("medicine_buddy.calendar")

EXPORT_TOKEN_KEY = "calendar_export_token"
IMPORT_URL_KEY = "calendar_import_url"
LAST_SYNC_KEY = "calendar_last_sync"
LAST_SYNC_ERROR_KEY = "calendar_last_sync_error"
SYNC_INTERVAL_S = 15 * 60
FETCH_TIMEOUT_S = 12
FETCH_MAX_BYTES = 1_500_000
# Voice must not wait forever on Google; finish in background if slower than this.
GOOGLE_VOICE_WAIT_S = 3.5
GOOGLE_HINT = (
    "Google OAuth (recommended): voice create/cancel + Sync. "
    "ICS subscribe/import stays available as an easier backup for apps that only support a link."
)


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
        "external_id": ev.external_id,
    }


def _unified_entry(ev: CalendarEvent) -> dict[str, Any]:
    """Slim row for the Family panel unified timeline."""
    return {
        "kind": ev.kind,
        "title": ev.title,
        "weekdays": ev.weekdays,
        "time_hhmm": ev.time_hhmm,
        "source": ev.source,
        "status": ev.status,
    }


class CalendarMixin:
    """Mixed into MedicineBuddyService (needs self.db, self.catalog, self.tz, access helpers)."""

    def _init_calendar(self) -> None:
        if not self.calendar_export_token():
            self.db.set_setting(EXPORT_TOKEN_KEY, secrets.token_urlsafe(18))
        backfill_medicine_mirrors(self.db)

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

    def google_calendar_id(self) -> str:
        return (self.db.get_setting(SETTING_CALENDAR_ID) or "").strip()

    def _google_cfg(self):
        return load_google_cal_config(self.google_calendar_id())

    def settings_calendar(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        sync_result: dict[str, Any] | None = None
        if body.get("rotate_token"):
            self.rotate_calendar_token()
        if "import_url" in body:
            try:
                cleaned = validate_http_url(str(body.get("import_url") or ""), what="calendar URL")
            except ValueError as exc:
                return {"error": str(exc)}
            self.db.set_setting(IMPORT_URL_KEY, cleaned)
        if body.get("clear_import"):
            self.db.set_setting(IMPORT_URL_KEY, "")
            retired = 0
            if body.get("retire_imports"):
                retired = self._retire_ics_import_companions()
            sync_result = {
                "status": "ok",
                "message": (
                    "Cleared ICS import URL."
                    + (f" Retired {retired} imported companion event(s)." if retired else "")
                ),
            }
        if "google_calendar_id" in body:
            cal_id = " ".join(str(body.get("google_calendar_id") or "").split())[:200]
            self.db.set_setting(SETTING_CALENDAR_ID, cal_id)
        if body.get("sync"):
            # Both can stay saved. Default Sync = Google when configured; ICS-only button forces ICS.
            if body.get("sync_ics_only"):
                sync_result = self.sync_calendar_import()
            elif self._google_cfg().configured:
                sync_result = self.sync_google_calendar()
            else:
                sync_result = self.sync_calendar_import()
            if sync_result.get("error") and not sync_result.get("status"):
                return sync_result
        probe: dict[str, Any] | None = None
        if body.get("probe_google"):
            probe = probe_google_calendar(self._google_cfg())
        # Mirrors power the Family unified list; keep them in sync with schedules.
        backfill_medicine_mirrors(self.db)
        proposals = [_event_json(e) for e in self.db.list_calendar_events(status="proposed", kind="medicine")]
        upcoming = self._upcoming_companion_summaries(limit=8)
        token = self.calendar_export_token()
        gcfg = self._google_cfg()
        gstatus = google_write_status(gcfg)
        linked = sum(
            1
            for e in self.db.list_calendar_events(status="active", limit=500)
            if e.external_id
        )
        msg = (sync_result or {}).get("message") or (
            (
                f"Google ready ({linked} linked). ICS subscribe/import still available as backup."
                if gstatus["configured"]
                else "Calendar ready — use ICS subscribe/import (easy), or connect Google for voice write."
            )
            if token else
            "Calendar token missing; rotate to create one."
        )
        return {
            "status": "ok",
            "export_token": token,
            "export_path": f"/calendar/{token}.ics" if token else "",
            "import_url": self.calendar_import_url(),
            "last_sync": self.db.get_setting(LAST_SYNC_KEY) or "",
            "last_sync_error": self.db.get_setting(LAST_SYNC_ERROR_KEY) or "",
            "google_hint": GOOGLE_HINT,
            "google_calendar_id": gcfg.calendar_id,
            "google_write": gstatus,
            "google_probe": probe,
            "google_linked": linked,
            "last_sync_detail": sync_result,
            "unified": self._unified_calendar_entries(),
            "proposals": proposals,
            "upcoming": upcoming,
            "message": msg,
        }

    def sync_calendar_voice(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Voice tool: push/pull the connected Google calendar. No calendar choice."""
        del body
        cfg = self._google_cfg()
        if not cfg.configured:
            return {
                "error": "google not configured",
                "speak_hint": (
                    "Google Calendar is not connected yet. Ask family to finish Google setup "
                    "on the Family page."
                ),
            }
        result = self.sync_google_calendar()
        if result.get("error") and not result.get("status"):
            # Fall back to ICS import if Google fails but a backup feed is saved.
            if self.calendar_import_url():
                ics = self.sync_calendar_import()
                if ics.get("status") == "ok":
                    return {
                        "status": "ok",
                        "result": ics,
                        "speak_hint": (
                            "Google sync failed, so I refreshed the backup calendar link instead. "
                            f"{ics.get('message', '')}"
                        ).strip(),
                    }
            return {
                "error": result["error"],
                "speak_hint": f"I couldn't sync with Google Calendar. {result['error']}",
            }
        pushed = int(result.get("google_pushed_local") or 0)
        pulled = int(result.get("companion_upserted") or 0)
        updated = int(result.get("schedules_updated") or 0)
        bits: list[str] = []
        if pushed:
            bits.append(f"sent {pushed} to Google")
        if pulled:
            bits.append(f"brought {pulled} from Google")
        if updated:
            bits.append(f"updated {updated} medicine time(s)")
        detail = (", ".join(bits) + ".") if bits else "Everything looks in sync."
        return {
            "status": "ok",
            "result": result,
            "speak_hint": f"Okay — I synced your calendar. {detail}",
        }

    def _unified_calendar_entries(self) -> list[dict[str, Any]]:
        """Active mb-med-* mirrors (+ schedule fallback) and companion events for Family UI.

        Legacy active ``kind=medicine`` rows (non ``mb-med-*`` uids) are skipped so
        import leftovers cannot duplicate a schedule that already has a mirror.
        """
        entries: list[dict[str, Any]] = []
        for ev in self.db.list_calendar_events(status="active", kind="medicine", limit=500):
            if not ev.uid.startswith("mb-med-"):
                continue
            entries.append(_unified_entry(ev))
        for schedule in self.db.list_active_schedules():
            mirror = self.db.get_calendar_event_by_uid(medicine_mirror_uid(schedule.id))
            if mirror is not None and mirror.status == "active":
                continue
            med = self.db.get_medicine(schedule.medicine_id)
            if med is None:
                continue
            entries.append(
                {
                    "kind": "medicine",
                    "title": medicine_summary(med.name),
                    "weekdays": schedule.weekdays,
                    "time_hhmm": schedule.time_hhmm,
                    "source": "local",
                    "status": "active",
                }
            )
        for ev in self.db.list_calendar_events(status="active", kind="companion", limit=500):
            entries.append(_unified_entry(ev))
        entries.sort(
            key=lambda row: (
                row.get("time_hhmm") or "99:99",
                0 if row.get("kind") == "medicine" else 1,
                row.get("title") or "",
            )
        )
        return entries

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
                schedule = self.db.add_schedule(medicine.id, weekdays, ev.time_hhmm)
            else:
                schedule = existing
            # Stable mirror uid; supersede the ICS-import proposal row so we do not keep two.
            mirror_schedule(self.db, schedule, medicine)
            if ev.uid != medicine_mirror_uid(schedule.id):
                self.db.set_calendar_event_status(event_id, "cancelled")
            else:
                self.db.set_calendar_event_status(event_id, "active")
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
        """Fetch (or parse) ICS; companion upsert; medicine proposals or schedule updates."""
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
        parsed = parse_ics(text, tz=self.tz())
        return self._apply_parsed_calendar_items(parsed, source="import")

    def sync_google_calendar(self) -> dict[str, Any]:
        """Bidirectional sync with the connected Google calendar (API).

        Pull Google → local companion / medicine proposals; push local mirrors
        missing a Google id → Google. Uses the single configured calendar id.
        """
        cfg = self._google_cfg()
        if not cfg.configured:
            return {
                "status": "skipped",
                "message": "Google Calendar write is not configured.",
            }
        try:
            parsed = list_google_events_as_parsed(
                cfg, tz_name=ensure_timezone_name(self.tz()), days_ahead=90
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)[:200]
            self.db.set_setting(LAST_SYNC_ERROR_KEY, msg)
            logger.info("Google Calendar pull failed: %s", msg)
            return {"error": f"Could not pull Google Calendar: {msg}"}
        pulled = self._apply_parsed_calendar_items(parsed, source="google")
        pushed_n = self._push_unsynced_local_to_google()
        pulled["google_pushed_local"] = pushed_n
        if pushed_n:
            pulled["message"] = (
                f"{pulled.get('message', 'Synced.')} Also pushed {pushed_n} local "
                "event(s) to Google."
            )
        return pulled

    def _apply_parsed_calendar_items(
        self, parsed: list[ParsedEvent], *, source: str
    ) -> dict[str, Any]:
        companion_n = proposal_n = updated_n = skipped = 0
        seen_companion_uids: set[str] = set()
        for item in parsed:
            if item.medicine_hint or self._looks_like_medicine(item.summary):
                hint = item.medicine_hint or item.summary
                match = self.catalog.match(hint)
                if match.name is None or not item.time_hhmm:
                    skipped += 1
                    continue
                weekdays = item.weekdays or "*"
                existing = self.db.get_calendar_event_by_uid(item.uid)
                if existing and existing.status == "rejected":
                    skipped += 1
                    continue
                person_id = existing.person_id if existing else None
                sync_result = self._sync_medicine_against_schedule(
                    match_name=match.name,
                    weekdays=weekdays,
                    time_hhmm=item.time_hhmm,
                    person_id=person_id,
                    uid=item.uid,
                )
                if sync_result == "updated":
                    updated_n += 1
                    continue
                if sync_result == "matched":
                    continue
                self.db.upsert_calendar_event(
                    uid=item.uid,
                    kind="medicine",
                    title=medicine_summary(match.name),
                    description=item.description,
                    weekdays=weekdays,
                    time_hhmm=item.time_hhmm,
                    starts_at=item.starts_at or "",
                    all_day=False,
                    person_id=person_id,
                    medicine_name=match.name,
                    source=source,
                    status="proposed",
                )
                proposal_n += 1
                continue
            if not item.summary:
                skipped += 1
                continue
            seen_companion_uids.add(item.uid)
            self.db.upsert_calendar_event(
                uid=item.uid,
                kind="companion",
                title=item.summary,
                description=item.description,
                weekdays=item.weekdays or "",
                time_hhmm=item.time_hhmm or "",
                starts_at=item.starts_at or "",
                all_day=item.all_day,
                source=source,
                status="active",
            )
            companion_n += 1
        cancelled_n = self._cancel_missing_import_companions(
            seen_companion_uids, sources={source}
        )
        now = datetime.now().astimezone().replace(microsecond=0).isoformat()
        self.db.set_setting(LAST_SYNC_KEY, now)
        self.db.set_setting(LAST_SYNC_ERROR_KEY, "")
        return {
            "status": "ok",
            "companion_upserted": companion_n,
            "proposals": proposal_n,
            "schedules_updated": updated_n,
            "companions_cancelled": cancelled_n,
            "skipped": skipped,
            "message": (
                f"Synced: {companion_n} companion event(s), {proposal_n} medicine proposal(s)"
                + (f", {updated_n} schedule update(s)" if updated_n else "")
                + (f", {cancelled_n} companion(s) cancelled" if cancelled_n else "")
                + (f", {skipped} skipped." if skipped else ".")
            ),
        }

    def _cancel_missing_import_companions(
        self, seen_uids: set[str], *, sources: set[str] | None = None
    ) -> int:
        """Cancel active import/google companions missing from this sync. Never touch local."""
        allowed = sources or {"import", "google"}
        cancelled = 0
        for ev in self.db.list_calendar_events(status="active", kind="companion", limit=500):
            if ev.source not in allowed:
                continue
            if ev.uid in seen_uids:
                continue
            self.db.set_calendar_event_status(ev.id, "cancelled")
            cancelled += 1
        return cancelled

    def _retire_ics_import_companions(self) -> int:
        """Cancel leftover ICS-import companions so Google is the only external feed."""
        cancelled = 0
        for ev in self.db.list_calendar_events(status="active", kind="companion", limit=500):
            if ev.source != "import":
                continue
            self.db.set_calendar_event_status(ev.id, "cancelled")
            cancelled += 1
        return cancelled

    def _push_unsynced_local_to_google(self) -> int:
        """Push active local + medicine mirrors that lack a Google event id."""
        cfg = self._google_cfg()
        if not cfg.configured:
            return 0
        n = 0
        for ev in self.db.list_calendar_events(status="active", limit=500):
            if ev.external_id:
                continue
            # Never re-push pure ICS-import leftovers; Google pull owns those.
            if ev.source == "import":
                continue
            if ev.kind not in {"companion", "medicine"}:
                continue
            if ev.source not in {"local", "google", ""} and not ev.uid.startswith("mb-med-"):
                continue
            try:
                body = build_google_event_body(
                    title=ev.title,
                    description=ev.description,
                    time_hhmm=ev.time_hhmm,
                    weekdays=ev.weekdays,
                    starts_at=ev.starts_at or self.now().date().isoformat(),
                    all_day=bool(ev.all_day or not ev.time_hhmm),
                    tz_name=ensure_timezone_name(self.tz()),
                )
                external_id = insert_google_event(cfg, body)
                self.db.set_calendar_external_id(ev.id, external_id)
                n += 1
            except Exception as exc:  # noqa: BLE001
                logger.info("Google push of %s failed: %s", ev.uid, exc)
        return n

    def push_schedule_to_google_async(self, schedule: Schedule, medicine: Medicine) -> None:
        """Background: ensure medicine mirror exists on Google after schedule save."""
        if not self._google_cfg().configured:
            return

        def _work() -> None:
            try:
                mirror = mirror_schedule(self.db, schedule, medicine)
                if mirror.external_id:
                    return
                note, ok = self._push_create_to_google(mirror)
                if not ok:
                    logger.info("Medicine Google push: %s", note)
            except Exception as exc:  # noqa: BLE001
                logger.info("Medicine Google push crashed: %s", exc)

        threading.Thread(
            target=_work, name=f"gcal-med-{schedule.id}", daemon=True
        ).start()

    def cancel_schedule_on_google_async(self, schedule_id: int) -> None:
        if not self._google_cfg().configured:
            return
        mirror = self.db.get_calendar_event_by_uid(medicine_mirror_uid(schedule_id))
        if mirror is None or not mirror.external_id:
            return
        external_id = mirror.external_id

        def _work() -> None:
            try:
                delete_google_event(self._google_cfg(), external_id)
            except Exception as exc:  # noqa: BLE001
                logger.info("Medicine Google delete crashed: %s", exc)

        threading.Thread(
            target=_work, name=f"gcal-med-del-{schedule_id}", daemon=True
        ).start()

    def _sync_medicine_against_schedule(
        self,
        *,
        match_name: str,
        weekdays: str,
        time_hhmm: str,
        person_id: int | None,
        uid: str,
    ) -> Literal["updated", "matched", "miss"]:
        """Match ICS medicine to an active schedule when the match is confident.

        Returns:
          ``updated`` — weekdays/time changed on a confident match (+ remirror)
          ``matched`` — confident match already aligned (remirror only)
          ``miss`` — no catalog medicine, no schedules, or ambiguous multi-dose
        """
        medicine = self._find_catalog_medicine(match_name, person_id)
        if medicine is None:
            return "miss"
        schedules = self.db.list_schedules_for_medicine(medicine.id)
        if not schedules:
            return "miss"
        schedule = self._pick_schedule_for_import(
            schedules, uid=uid, weekdays=weekdays, time_hhmm=time_hhmm
        )
        if schedule is None:
            return "miss"
        changed = schedule.weekdays != weekdays or schedule.time_hhmm != time_hhmm
        if changed:
            schedule = self.db.update_schedule(schedule.id, weekdays=weekdays, time_hhmm=time_hhmm)
        # Mirror is the stable mb-med-* row; do not reactivate the ICS-import proposal uid.
        mirror_schedule(self.db, schedule, medicine)
        return "updated" if changed else "matched"

    def _find_catalog_medicine(self, name: str, person_id: int | None) -> Medicine | None:
        med = self.db.get_medicine_by_name(name, person_id)
        if med is not None:
            return med
        if person_id is not None:
            return self.db.get_medicine_by_name(name, None)
        # No person on the import row: any active medicine with this name (prefer unassigned).
        for candidate in self.db.list_medicines():
            if candidate.name.casefold() == name.casefold():
                return candidate
        return None

    def _pick_schedule_for_import(
        self,
        schedules: list[Schedule],
        *,
        uid: str,
        weekdays: str,
        time_hhmm: str,
    ) -> Schedule | None:
        """Confident match only: exact weekdays+time, sole schedule, or prior ICS link.

        Never falls through to an arbitrary ``schedules[0]`` when multi-dose is ambiguous.
        """
        for schedule in schedules:
            if schedule.weekdays == weekdays and schedule.time_hhmm == time_hhmm:
                return schedule
        if len(schedules) == 1:
            return schedules[0]
        prior = self.db.get_calendar_event_by_uid(uid)
        if prior is not None and prior.time_hhmm:
            for schedule in schedules:
                if schedule.time_hhmm == prior.time_hhmm and (
                    not prior.weekdays or schedule.weekdays == prior.weekdays
                ):
                    return schedule
        return None

    def _looks_like_medicine(self, summary: str) -> bool:
        match = self.catalog.match(summary or "")
        return match.name is not None and match.how in {"exact", "alias", "fuzzy"}

    def list_upcoming_events(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Voice/tool: medicines + companion events whose local occurrences fall in ``when``."""
        body = body or {}
        when_raw = str(body.get("when") or "soon").strip() or "soon"
        now = self.now()
        try:
            window = _resolve_list_window(when_raw, now)
        except ValueError as exc:
            return {"error": str(exc), "speak_hint": str(exc)}

        medicines: list[dict[str, Any]] = []
        for schedule in self.db.list_active_schedules():
            med = self.db.get_medicine(schedule.medicine_id)
            if med is None:
                continue
            occs = _occurrences_in_window(
                weekdays=schedule.weekdays,
                time_hhmm=schedule.time_hhmm,
                window_start=window.start,
                window_end=window.end,
            )
            if not occs:
                continue
            medicines.append(
                {
                    "name": med.name,
                    "time_hhmm": schedule.time_hhmm,
                    "schedule_id": schedule.id,
                    "at": occs[0].isoformat(timespec="seconds"),
                }
            )
        medicines.sort(key=lambda m: m["at"])

        companion_sort: list[tuple[datetime, str]] = []
        for ev in self.db.list_calendar_events(status="active", kind="companion", limit=100):
            occs = _occurrences_in_window(
                weekdays=ev.weekdays,
                time_hhmm=ev.time_hhmm,
                starts_at=ev.starts_at,
                window_start=window.start,
                window_end=window.end,
                all_day=bool(ev.all_day or not ev.time_hhmm),
            )
            if not occs:
                continue
            label = (
                f"{ev.title} at {describe_time(ev.time_hhmm)}"
                if ev.time_hhmm
                else ev.title
            )
            companion_sort.append((occs[0], label))
        companion_sort.sort(key=lambda pair: pair[0])
        companion = [label for _, label in companion_sort]

        lines: list[str] = [
            f"Medicine: {m['name']} at {describe_time(m['time_hhmm'])}" for m in medicines
        ]
        lines.extend(companion)
        if not lines:
            hint = (
                f"Nothing special coming up {window.label}. "
                "You can add a medicine reminder or a calendar event."
            )
        else:
            hint = "Coming up: " + "; ".join(lines) + "."
        return {
            "status": "ok",
            "when": window.label,
            "window_start": window.start.isoformat(timespec="seconds"),
            "window_end": window.end.isoformat(timespec="seconds"),
            "medicines": medicines,
            "companion": companion,
            "speak_hint": hint,
        }

    def create_calendar_event(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Voice/tool: create a companion (non-medicine) event. Medicines use schedule_medicine."""
        body = body or {}
        title = " ".join(str(body.get("title") or body.get("name") or "").split())
        if not title:
            return {"error": "title is required", "speak_hint": "What should I put on the calendar?"}
        # Never create medicine schedules through this path (Med: … or catalog hit).
        if medicine_name_from_summary(title) or self._looks_like_medicine(title):
            return {
                "error": "use schedule_medicine for medicines",
                "speak_hint": (
                    "That sounds like a medicine. Use the medicine reminder tools instead of "
                    "the general calendar."
                ),
            }
        time_raw = str(body.get("time") or body.get("time_hhmm") or "").strip()
        weekdays_raw = str(body.get("weekdays") or body.get("days") or "").strip()
        when_raw = str(body.get("when") or body.get("date") or "").strip()
        now = self.now()
        starts_at = now.date().isoformat()
        # "tomorrow" / ISO / "in 3 days" → one event. Weekday names → recurring unless one-shot when.
        one_shot = False
        if when_raw:
            try:
                # ISO date alone, or relative phrase (tomorrow, Monday, …).
                if len(when_raw) == 10 and when_raw[4] == "-" and when_raw[7] == "-":
                    starts_at = date.fromisoformat(when_raw).isoformat()
                    if not weekdays_raw:
                        one_shot = True
                else:
                    window = parse_window(when_raw, now)
                    starts_at = window.start.date().isoformat()
                    if not weekdays_raw:
                        if is_one_shot_when(when_raw):
                            one_shot = True
                        else:
                            # "Monday" without "every" still means that weekday recurring.
                            weekdays_raw = when_raw
            except ValueError as exc:
                return {"error": str(exc), "speak_hint": f"I didn't understand the day. {exc}."}
        if one_shot:
            weekdays_raw = ""
        elif not weekdays_raw:
            weekdays_raw = "daily"
        try:
            time_hhmm = parse_time_hhmm(time_raw) if time_raw else ""
            weekdays = (
                ""
                if one_shot or not time_hhmm
                else parse_weekdays(weekdays_raw, as_of=now.date())
            )
        except ValueError as exc:
            return {"error": str(exc), "speak_hint": f"I didn't understand the time or day. {exc}."}
        person = None
        if body.get("person"):
            person, err = self._resolve_person(body.get("person"), for_change=False)
            if err and err.get("error"):
                return err
        # Anchor recurrence / Google start on the first matching day on/after starts_at.
        if time_hhmm and weekdays:
            starts_at = next_occurrence_date(
                weekdays, starts_at, as_of=date.fromisoformat(starts_at)
            ).isoformat()
        uid = new_uid("local")
        description = str(body.get("description") or "").strip()[:500]
        all_day = not bool(time_hhmm)
        ev = self.db.upsert_calendar_event(
            uid=uid,
            kind="companion",
            title=title,
            description=description,
            weekdays=weekdays,
            time_hhmm=time_hhmm,
            starts_at=starts_at,
            all_day=all_day,
            person_id=person.id if person else None,
            source="local",
            status="active",
        )
        if time_hhmm and weekdays:
            when = f"{describe_weekdays(weekdays)} at {describe_time(time_hhmm)}"
        elif time_hhmm:
            when = f"on {starts_at} at {describe_time(time_hhmm)}"
        else:
            when = "as an all-day note"
        google_note, google_ok, google_pending = self._push_create_to_google_voice(ev)
        gcfg = self._google_cfg()
        # Never ask "which calendar?" — the connected Google id is the only external one.
        if google_ok or (gcfg.configured and google_pending):
            hint = f"I've put “{title}” on your calendar {when}."
            if google_pending:
                hint += " Syncing to Google now."
        elif gcfg.configured:
            hint = (
                f"I've saved “{title}” locally {when}, "
                f"but Google sync failed. {google_note}"
            ).strip()
        else:
            hint = f"I've put “{title}” on your calendar {when}."
        return {
            "status": "ok",
            "event": _event_json(self.db.get_calendar_event(ev.id) or ev),
            "google_pushed": google_ok,
            "google_pending": google_pending,
            "google_configured": gcfg.configured,
            "google_calendar_id": gcfg.calendar_id,
            "speak_hint": hint,
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
        google_note = self._push_cancel_to_google(target)
        self.db.set_calendar_event_status(target.id, "cancelled")
        hint = f"Okay, I removed “{target.title}” from the calendar."
        if google_note:
            hint = f"{hint} {google_note}"
        return {
            "status": "ok",
            "speak_hint": hint,
        }

    def _push_create_to_google(self, ev: CalendarEvent) -> tuple[str, bool]:
        """Returns (note_for_speak, pushed_ok). Local row always remains."""
        cfg = self._google_cfg()
        if not cfg.configured:
            # Stay quiet when OAuth is unset — local companion calendar still works.
            return ("", False)
        try:
            body = build_google_event_body(
                title=ev.title,
                description=ev.description,
                time_hhmm=ev.time_hhmm,
                weekdays=ev.weekdays,
                starts_at=ev.starts_at,
                all_day=bool(ev.all_day or not ev.time_hhmm),
                tz_name=ensure_timezone_name(self.tz()),
            )
            external_id = insert_google_event(cfg, body)
            self.db.set_calendar_external_id(ev.id, external_id)
            return ("", True)
        except Exception as exc:  # noqa: BLE001
            logger.info("Google Calendar create failed: %s", exc)
            return (f"(Could not sync to Google: {str(exc)[:120]})", False)

    def _push_create_to_google_voice(
        self, ev: CalendarEvent
    ) -> tuple[str, bool, bool]:
        """Voice path: wait briefly for Google, else finish in a daemon thread.

        Returns ``(note, pushed_ok, pending)``. ``pending`` means sync continues
        in the background so the realtime voice turn can answer immediately.
        """
        cfg = self._google_cfg()
        if not cfg.configured:
            return ("", False, False)

        box: dict[str, Any] = {}

        def _work() -> None:
            note, ok = self._push_create_to_google(ev)
            box["note"] = note
            box["ok"] = ok

        thread = threading.Thread(
            target=_work,
            name=f"gcal-create-{ev.id}",
            daemon=True,
        )
        thread.start()
        thread.join(timeout=GOOGLE_VOICE_WAIT_S)
        if thread.is_alive():
            logger.info(
                "Google Calendar create still running after %.1fs; continuing in background",
                GOOGLE_VOICE_WAIT_S,
            )
            return ("", False, True)
        return (str(box.get("note") or ""), bool(box.get("ok")), False)

    def _push_cancel_to_google(self, ev: CalendarEvent) -> str:
        cfg = self._google_cfg()
        if not cfg.configured or not ev.external_id:
            return ""
        try:
            delete_google_event(cfg, ev.external_id)
            self.db.set_calendar_external_id(ev.id, "")
            return "I also removed it from Google Calendar."
        except Exception as exc:  # noqa: BLE001
            logger.info("Google Calendar delete failed: %s", exc)
            return f"(Could not remove it from Google: {str(exc)[:120]})"

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


def _resolve_list_window(when: str, now: datetime) -> TimeWindow:
    """Map ``when`` to a half-open window. ``soon`` / empty → [now, end_of_tomorrow)."""
    key = " ".join((when or "").strip().casefold().split())
    # parse_window rejects soon/empty — special-case the default list horizon here.
    if not key or key == "soon":
        # End of tomorrow = midnight at the start of the day after tomorrow.
        day_after_tomorrow = now.date() + timedelta(days=2)
        end_of_tomorrow = datetime(
            day_after_tomorrow.year,
            day_after_tomorrow.month,
            day_after_tomorrow.day,
            tzinfo=now.tzinfo,
        )
        return TimeWindow(start=now, end=end_of_tomorrow, label="soon")
    return parse_window(when, now)


def _day_matches_recurrence(d: date, weekdays: str, starts_at: str) -> bool:
    """Whether local calendar day ``d`` is an occurrence day for weekdays/starts_at."""
    wd = (weekdays or "").strip()
    starts = _starts_date(starts_at)
    if wd == "*":
        return starts is None or d >= starts
    if wd:
        allowed = {int(p) for p in wd.split(",") if p.strip() != ""}
        if d.weekday() not in allowed:
            return False
        return starts is None or d >= starts
    return starts is not None and d == starts


def _occurrences_in_window(
    *,
    weekdays: str,
    time_hhmm: str,
    window_start: datetime,
    window_end: datetime,
    starts_at: str = "",
    all_day: bool = False,
) -> list[datetime]:
    """Local wall-clock occurrences that fall in half-open [window_start, window_end)."""
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("window bounds must be timezone-aware")
    if window_end <= window_start:
        return []

    if all_day or not (time_hhmm or "").strip():
        hour, minute = 0, 0
    else:
        try:
            hour, minute = (int(x) for x in time_hhmm.split(":"))
        except ValueError:
            return []

    out: list[datetime] = []
    last_day = (window_end - timedelta(microseconds=1)).date()
    d = window_start.date()
    while d <= last_day:
        if _day_matches_recurrence(d, weekdays, starts_at):
            occ = datetime(d.year, d.month, d.day, hour, minute, tzinfo=window_start.tzinfo)
            if window_start <= occ < window_end:
                out.append(occ)
        d += timedelta(days=1)
    return out


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
            try:
                if self.service._google_cfg().configured:  # noqa: SLF001
                    result = self.service.sync_google_calendar()
                elif self.service.calendar_import_url():
                    result = self.service.sync_calendar_import()
                else:
                    continue
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
