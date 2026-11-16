"""HTTP API so reachy_mini_conversation tools can drive Medicine Buddy remotely.

Also serves a read-only status page at http://127.0.0.1:8787/ showing schedules,
the allowed-medicine list, what's due and the dose log.
"""

from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger("medicine_buddy.api")

from medicine_buddy import __version__
from medicine_buddy.access import PeopleAccessMixin
from medicine_buddy.activities import ActivitiesMixin
from medicine_buddy.calendar_mirror import cancel_mirror, mirror_schedule
from medicine_buddy.calendar_service import CalendarMixin
from medicine_buddy.caregiver_alerts import CaregiverAlertsMixin
from medicine_buddy.checkins import CheckInsMixin
from medicine_buddy.companion_settings import CompanionSettingsMixin
from medicine_buddy.debug import DebugMixin
from medicine_buddy.family_page import render_family_page
from medicine_buddy.listen_mode import ListenModeMixin
from medicine_buddy.media_reset import MediaResetMixin
from medicine_buddy.settings import SettingsMixin
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.clock import ClockMixin
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database, Medicine, Person, Schedule
from medicine_buddy.dry_run import FakeAdherence, FakeArm, FakeVision
from medicine_buddy.arm_protocol import MoveObjectResult, new_request_id
from medicine_buddy.http_arm import HttpArmClient
from medicine_buddy.orchestrator import DoseStateMachine
from medicine_buddy.scheduler import find_due_doses
from medicine_buddy.silent_buddy import SilentBuddy
from medicine_buddy.transcript import TranscriptMixin
from medicine_buddy.voice_guard import VoiceGuard
from medicine_buddy.voice_setup import (
    describe_time,
    describe_weekdays,
    parse_time_hhmm,
    parse_weekdays,
)


def _when(s: Schedule) -> str:
    return f"{describe_weekdays(s.weekdays)} at {describe_time(s.time_hhmm)}"


def _schedule_json(s: Schedule) -> dict[str, Any]:
    return {"id": s.id, "weekdays": s.weekdays, "time_hhmm": s.time_hhmm, "spoken": _when(s)}


def _whose(person: Person | None) -> str:
    return f" for {person.name}" if person else ""


class MedicineBuddyService(
    PeopleAccessMixin,
    SettingsMixin,
    CaregiverAlertsMixin,
    CompanionSettingsMixin,
    CalendarMixin,
    CheckInsMixin,
    ActivitiesMixin,
    DebugMixin,
    ClockMixin,
    TranscriptMixin,
    ListenModeMixin,
    MediaResetMixin,
):
    def __init__(
        self,
        db_path: Path,
        config: AppConfig | None = None,
        catalog: MedicineCatalog | None = None,
        guard: VoiceGuard | None = None,
    ) -> None:
        self.config = config or AppConfig(dry_run=True)
        self.db = Database(db_path)
        self.catalog = catalog or MedicineCatalog.load()
        # Disabled unless the server is started with --voice-guard (needs the launcher
        # that forwards the person's speech transcripts to POST /voice/heard).
        self.guard = guard or VoiceGuard(self.catalog, enabled=False)
        self.guard.identify = self._identify_speakers  # recognise spoken passphrases
        self._lock = threading.Lock()  # serializes multi-step operations
        self._init_settings()
        self._init_alerts()
        self._init_companion_settings()
        self._init_calendar()
        self._init_activities()
        self._init_debug()
        self._init_transcript()
        self._init_listen_mode()
        # Set from main.py: progress_speaker must not talk over pending tools;
        # completion_speaker waits until Reachy is idle (arm-done announce).
        self.progress_speaker: Callable[[str], bool] | None = None
        self.completion_speaker: Callable[[str], bool] | None = None
        self._arm_job_lock = threading.Lock()
        self._arm_job_busy = False
        self._arm_job_label: str | None = None

    def announce_progress(self, text: str) -> bool:
        """Best-effort spoken line. Skips if a tool is still pending (HF realtime rule)."""
        line = " ".join(str(text or "").split())
        if not line or self.progress_speaker is None:
            return False
        try:
            return bool(self.progress_speaker(line))
        except Exception as exc:  # noqa: BLE001 — never fail the arm path for TTS
            logger.warning("announce_progress failed: %s", exc)
            return False

    def announce_when_idle(self, text: str) -> bool:
        """Speak after tools finish — used when a background arm job completes."""
        line = " ".join(str(text or "").split())
        if not line:
            return False
        speaker = self.completion_speaker or self.progress_speaker
        if speaker is None:
            return False
        try:
            return bool(speaker(line))
        except Exception as exc:  # noqa: BLE001
            logger.warning("announce_when_idle failed: %s", exc)
            return False

    def arm_async_enabled(self) -> bool:
        """When ON (default), remote arm dispense returns immediately so Reachy can keep listening."""
        raw = (os.environ.get("MEDICINE_BUDDY_ARM_ASYNC") or "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    def arm_job_busy(self) -> bool:
        with self._arm_job_lock:
            return bool(self._arm_job_busy)

    def family_page(self) -> str:
        return render_family_page(self.settings_token)

    # ------------------------------------------------------------ voice guard input
    def voice_heard(self, body: dict[str, Any]) -> dict[str, Any]:
        """The person's real speech, forwarded by the launcher (loopback only)."""
        text = str(body.get("text") or "").strip()
        if not text:
            return {"error": "text is required"}
        u = self.guard.heard(text)
        # Activity log gets length only — never the utterance text.
        return {
            "status": "ok",
            "medicines": sorted(u.medicines),
            "yes": u.yes,
            "utterance_chars": len(text),
        }

    def security(self) -> dict[str, Any]:
        return self.guard.status()

    def close(self) -> None:
        self.db.close()

    def health(self) -> dict[str, Any]:
        arm_url = (self.config.arm_url or "").strip()
        arm_reachable: bool | None = None
        if arm_url:
            client = HttpArmClient.from_config(self.config)
            arm_reachable = bool(client and client.probe_reachable())
        return {
            "status": "ok",
            "dry_run": self.config.dry_run,
            "arm_url_configured": bool(arm_url),
            "arm_url": arm_url or None,
            "arm_reachable": arm_reachable,
            "encourage_self_care": self.encourage_self_care(),
            "demo_smooth": self.demo_smooth(),
            "arm_async": self.arm_async_enabled(),
            "arm_busy": self.arm_job_busy(),
            "service": "medicine-buddy",
            "version": __version__,
        }

    # ------------------------------------------------------------ catalog
    def catalog_names(self) -> dict[str, Any]:
        return {"medicines": self.catalog.names}

    def _recognize(self, spoken: Any) -> tuple[str | None, dict[str, Any] | None]:
        """(canonical name, None) or (None, error payload Reachy can read out)."""
        text = str(spoken or "").strip()
        if not text:
            return None, {"error": "medicine name is required", "allowed_medicines": self.catalog.names}
        result = self.catalog.match(text)
        if result.name:
            return result.name, None
        hint = self.catalog.not_recognized_hint(text, result)
        return None, {
            "error": f"medicine not recognized: {text!r}",
            "suggestions": result.suggestions,
            "allowed_medicines": self.catalog.names,
            "speak_hint": hint,
        }

    def _known_medicine(
        self, spoken: Any, person: Person | None = None
    ) -> tuple[Medicine | None, dict[str, Any] | None]:
        name, err = self._recognize(spoken)
        if err:
            # Demo smooth: free-form label (e.g. "small bottle") still goes to /v1/dispense
            # as medicine.name — same shape as the working arm curl probe.
            text = " ".join(str(spoken or "").split())
            if self.demo_smooth() and text:
                name = text
            else:
                return None, err
        medicine = self.db.get_medicine_by_name(name, person.id if person else None)
        if medicine is None:
            # Demo smooth: keep the schedule gate in code, but auto-enrol so the arm can grab.
            if self.demo_smooth():
                medicine = self.db.add_medicine(name, person.id if person else None)
                return medicine, None
            who = f"{person.name} doesn't" if person else "You don't"
            return None, {
                "error": f"no schedule saved for {name}{_whose(person)}",
                "speak_hint": f"{who} have any reminders set up for {name} yet.",
            }
        return medicine, None

    def _person_names(self) -> dict[int, str]:
        return {p.id: p.name for p in self.db.list_people()}

    # ------------------------------------------------------------ reading
    def list_medicines(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """All medicines, or one person's when body has `person`."""
        body = body or {}
        only: Person | None = None
        if body.get("person"):
            only, err = self._resolve_person(body.get("person"))
            if err:
                return err
        names = self._person_names()
        by_med: dict[int, list[dict[str, Any]]] = {}
        for schedule in self.db.list_active_schedules():
            by_med.setdefault(schedule.medicine_id, []).append(_schedule_json(schedule))
        meds = self.db.list_medicines(only.id if only else None, all_people=only is None)
        listed = [
            {"id": m.id, "name": m.name, "person": names.get(m.person_id), "schedules": by_med[m.id]}
            for m in meds if m.id in by_med
        ]
        if listed:
            def line(m: dict[str, Any]) -> str:
                who = f" ({m['person']})" if m["person"] and not only else ""
                return f"{m['name']}{who} {', and '.join(s['spoken'] for s in m['schedules'])}"
            owner = f"{only.name}'s" if only else "Your"
            hint = f"{owner} medicines: " + "; ".join(line(m) for m in listed) + "."
        else:
            hint = f"There are no medicine reminders{_whose(only)} yet."
        return {"medicines": listed, "speak_hint": hint}

    def due(self) -> dict[str, Any]:
        items = find_due_doses(self.db, tz=self.tz())
        names = self._person_names()
        owner = {m.id: names.get(m.person_id) for m in self.db.list_medicines()}

        def say(d) -> str:
            who = f" for {owner[d.medicine_id]}" if owner.get(d.medicine_id) else ""
            return f"{d.medicine_name}{who} ({describe_time(d.time_hhmm)})"

        return {
            "due": [
                {
                    "medicine_id": d.medicine_id,
                    "medicine_name": d.medicine_name,
                    "person": owner.get(d.medicine_id),
                    "schedule_id": d.schedule_id,
                    "time_hhmm": d.time_hhmm,
                }
                for d in items
            ],
            "speak_hint": (
                "No medicines are due right now."
                if not items
                else "Due now: " + ", ".join(say(d) for d in items)
                + ". Ask the person if you should bring it, then call dispense_medicine."
            ),
        }

    # ------------------------------------------------------------ add / update / delete
    def add_schedule(self, body: dict[str, Any]) -> dict[str, Any]:
        time_raw = str(body.get("time", body.get("time_hhmm", ""))).strip()
        weekdays_raw = str(body.get("weekdays") or "daily")
        name, err = self._recognize(body.get("name"))
        if err:
            return err
        if not time_raw:
            return {"error": "time is required", "speak_hint": f"What time should I remind you about {name}?"}
        try:
            weekdays = parse_weekdays(weekdays_raw, as_of=self.now().date())
            time_hhmm = parse_time_hhmm(time_raw)
        except ValueError as exc:
            return {"error": str(exc), "speak_hint": f"Sorry, I didn't understand that time or day. {exc}."}
        person, err = self._resolve_person(body.get("person"), for_change=True)
        just_enrolled = False
        if err and err.get("status") == "needs_passphrase_setup":
            # First-time medicine setup: register with new_passphrase (if they already said it
            # twice), then continue into scheduling — one conversational flow instead of two
            # failed tool calls.
            pending = {
                "action": "schedule_medicine",
                "name": name,
                "weekdays": weekdays_raw,
                "time": time_raw,
                "person": err.get("person") or body.get("person"),
            }
            enrolled = self._try_first_time_enrol(body, err, pending=pending)
            if enrolled is not None:
                return enrolled
            just_enrolled = True
            person, err = self._resolve_person(body.get("person"), for_change=True)
        if err:
            return err
        # Just enrolled with new_passphrase: that is the proof for this request (with the guard
        # on they already said it twice; with the guard off they typed it twice).
        auth_body = body
        if just_enrolled and body.get("new_passphrase"):
            auth_body = {**body, "passphrase": body["new_passphrase"]}
        blocked = (
            None if just_enrolled and self.guard.enabled
            else self._require_owner("add schedule", auth_body, person)
        ) or self.guard.require_mention("add schedule", name)
        if blocked:
            return blocked

        with self._lock:
            medicine = self.db.add_medicine(name, person.id if person else None)
            existing = self.db.find_active_schedule(medicine.id, weekdays, time_hhmm)
            if existing is not None:
                return {
                    "status": "ok",
                    "already_existed": True,
                    "verified": True,
                    "medicine": {"id": medicine.id, "name": medicine.name},
                    "schedule": _schedule_json(existing),
                    "speak_hint": (f"{medicine.name}{_whose(person)} {_when(existing)} was already set up, "
                                   "so nothing changed."),
                }
            schedule = self.db.add_schedule(medicine.id, weekdays, time_hhmm)
            mirror_schedule(self.db, schedule, medicine)
            self.push_schedule_to_google_async(schedule, medicine)
            return self._verified(medicine, schedule.id, weekdays, time_hhmm, "is set up", person)

    def _try_first_time_enrol(
        self, body: dict[str, Any], setup_err: dict[str, Any], *, pending: dict[str, Any]
    ) -> dict[str, Any] | None:
        """If the caller provided a new passphrase for an unknown person, register them now.

        Returns a response dict when enrolment was attempted (success path returns None so the
        caller can continue scheduling; failure returns the guided error). Returns a special
        needs_passphrase_setup dict when no passphrase was provided yet.
        """
        # Only `new_passphrase` enrols someone. Never treat `passphrase` (often the caregiver's)
        # as the new person's secret — that caused confusing "already in use" / failed setup.
        new_phrase = str(body.get("new_passphrase") or "").strip()
        person_name = str(setup_err.get("person") or body.get("person") or "").strip()
        if not new_phrase:
            return self._needs_passphrase_setup(person_name or "this person", pending=pending)
        reg = self.register_person({
            "name": person_name,
            "role": "person",
            "passphrase": new_phrase,
            "passphrase_repeat": str(body.get("passphrase_repeat") or new_phrase),
        })
        if reg.get("status") == "ok":
            return None  # registered — caller re-resolves and schedules
        # Keep the pending medicine request in the error so Reachy can retry after they speak.
        out = dict(reg)
        out.setdefault("status", "needs_passphrase_setup")
        out["pending"] = pending
        out["person"] = person_name
        if not out.get("speak_hint"):
            out["speak_hint"] = (
                f"I couldn't finish setting up {person_name} yet. Please have them say their "
                "new passphrase two times, then try the medicine reminder again."
            )
        return out

    def _verified(
        self, medicine: Medicine, schedule_id: int, weekdays: str, time_hhmm: str, verb: str,
        person: Person | None = None,
    ) -> dict[str, Any]:
        """Read the schedule back from the database and confirm it matches what was asked."""
        stored = self.db.get_schedule(schedule_id)
        ok = (
            stored is not None
            and stored.active
            and stored.weekdays == weekdays
            and stored.time_hhmm == time_hhmm
        )
        if not ok:
            return {
                "error": "saved schedule could not be verified",
                "verified": False,
                "speak_hint": f"Something went wrong saving {medicine.name}. Please check the medicine computer.",
            }
        return {
            "status": "ok",
            "verified": True,
            "medicine": {"id": medicine.id, "name": medicine.name},
            "person": person.name if person else None,
            "schedule": _schedule_json(stored),
            "speak_hint": f"Done. {medicine.name}{_whose(person)} {verb} correctly: {_when(stored)}.",
        }

    def _pick_schedules(
        self, medicine: Medicine, time_raw: Any
    ) -> tuple[list[Schedule], dict[str, Any] | None]:
        schedules = self.db.list_schedules_for_medicine(medicine.id)
        if not schedules:
            return [], {
                "error": f"no active schedule for {medicine.name}",
                "speak_hint": f"You don't have any reminders set up for {medicine.name}.",
            }
        if time_raw:
            try:
                wanted = parse_time_hhmm(str(time_raw))
            except ValueError as exc:
                return [], {"error": str(exc)}
            picked = [s for s in schedules if s.time_hhmm == wanted]
            if not picked:
                times = " and ".join(describe_time(s.time_hhmm) for s in schedules)
                return [], {
                    "error": f"{medicine.name} has no schedule at {wanted}",
                    "current_schedules": [_schedule_json(s) for s in schedules],
                    "speak_hint": f"{medicine.name} isn't set for {describe_time(wanted)}. It's set for {times}.",
                }
            return picked, None
        return schedules, None

    def update_schedule(self, body: dict[str, Any]) -> dict[str, Any]:
        """Change the time and/or days of an existing schedule.

        body: name, [time] = the schedule's current time (needed if it has several),
              [new_time], [new_weekdays]
        """
        new_time_raw = body.get("new_time")
        new_weekdays_raw = body.get("new_weekdays")
        if not new_time_raw and not new_weekdays_raw:
            return {"error": "give new_time and/or new_weekdays",
                    "speak_hint": "What should I change it to - a new time, or different days?"}
        try:
            new_time = parse_time_hhmm(str(new_time_raw)) if new_time_raw else None
            new_weekdays = (
                parse_weekdays(str(new_weekdays_raw), as_of=self.now().date())
                if new_weekdays_raw
                else None
            )
        except ValueError as exc:
            return {"error": str(exc), "speak_hint": f"Sorry, I didn't understand that. {exc}."}
        name, err = self._recognize(body.get("name"))
        if err:
            return err
        person, err = self._resolve_person(body.get("person"), for_change=True)
        if err:
            return err
        blocked = (self._require_owner("update schedule", body, person)
                   or self.guard.require_mention("update schedule", name))
        if blocked:
            return blocked

        with self._lock:
            medicine, err = self._known_medicine(name, person)
            if err:
                return err
            picked, err = self._pick_schedules(medicine, body.get("time"))
            if err:
                return err
            if len(picked) > 1:
                times = " and ".join(describe_time(s.time_hhmm) for s in picked)
                return {
                    "error": "several schedules match; pass time to choose one",
                    "current_schedules": [_schedule_json(s) for s in picked],
                    "speak_hint": f"{medicine.name} is set for {times}. Which one should I change?",
                }
            target = picked[0]
            weekdays = new_weekdays or target.weekdays
            time_hhmm = new_time or target.time_hhmm
            clash = self.db.find_active_schedule(medicine.id, weekdays, time_hhmm)
            if clash is not None and clash.id != target.id:
                return {"error": "that schedule already exists",
                        "speak_hint": f"{medicine.name} is already set {_when(clash)}."}
            updated = self.db.update_schedule(target.id, weekdays=weekdays, time_hhmm=time_hhmm)
            mirror_schedule(self.db, updated, medicine)
            self.push_schedule_to_google_async(updated, medicine)
            return self._verified(medicine, target.id, weekdays, time_hhmm, "is now set up", person)

    def delete_schedule(self, body: dict[str, Any]) -> dict[str, Any]:
        """Stop reminders for a medicine. Needs confirmed=true (Reachy must ask first).

        body: name, confirmed, [time] (pick one schedule), [all] (remove every schedule)
        """
        name, err = self._recognize(body.get("name"))
        if err:
            return err
        person, err = self._resolve_person(body.get("person"), for_change=True)
        if err:
            return err
        blocked = (self._require_owner("delete schedule", body, person)
                   or self.guard.require_mention("delete schedule", name))
        if blocked:
            return blocked

        with self._lock:
            medicine, err = self._known_medicine(name, person)
            if err:
                return err
            picked, err = self._pick_schedules(medicine, body.get("time"))
            if err:
                return err
            if len(picked) > 1 and not body.get("all"):
                times = " and ".join(describe_time(s.time_hhmm) for s in picked)
                return {
                    "status": "needs_choice",
                    "current_schedules": [_schedule_json(s) for s in picked],
                    "speak_hint": (f"{medicine.name} is set for {times}. Should I remove all of them, "
                                   "or just one? (call again with all=true, or with time)"),
                }
            described = " and ".join(_when(s) for s in picked)
            key = f"delete:{medicine.id}:{','.join(str(s.id) for s in picked)}"
            confirmed = bool(body.get("confirmed"))
            if confirmed and self.guard.enabled and not self.guard.has_pending(key):
                confirmed = False  # the AI didn't ask first: ask now instead of acting
            if confirmed:
                blocked = self.guard.require_yes("delete schedule", key)
                if blocked:
                    return blocked
            if not confirmed:
                self.guard.ask(key)
                return {
                    "status": "needs_confirm",
                    "speak_hint": (f"Are you sure you want me to stop reminding you about {medicine.name} "
                                   f"{described}? If they say yes, call delete_medicine_schedule again "
                                   "with confirmed=true."),
                }
            for s in picked:
                self.cancel_schedule_on_google_async(s.id)
                self.db.deactivate_schedule(s.id)
                cancel_mirror(self.db, s.id)
            # Verify by reading back
            still_active = {s.id for s in self.db.list_schedules_for_medicine(medicine.id)}
            if any(s.id in still_active for s in picked):
                return {"error": "delete could not be verified", "verified": False,
                        "speak_hint": "Something went wrong removing that reminder."}
            remaining = self.db.list_schedules_for_medicine(medicine.id)
            tail = (f" You still have {medicine.name} " + " and ".join(_when(s) for s in remaining) + "."
                    if remaining else f" You have no more reminders for {medicine.name}.")
            return {
                "status": "ok",
                "verified": True,
                "removed": [_schedule_json(s) for s in picked],
                "remaining": [_schedule_json(s) for s in remaining],
                "speak_hint": f"Done. I removed {medicine.name} {described}.{tail}",
            }

    # ------------------------------------------------------------ dose flow
    def _resolve_medicine(self, body: dict[str, Any]):
        medicine_id = body.get("medicine_id")
        if medicine_id is not None:
            return self.db.get_medicine(int(medicine_id)), None
        spoken = body.get("medicine_name") or body.get("name")
        if not spoken:
            return None, {"error": "medicine_id or medicine_name required"}
        person, err = self._resolve_person(body.get("person"), strict=True)
        if err:
            return None, err
        return self._known_medicine(spoken, person)

    def run_dose(self, body: dict[str, Any]) -> dict[str, Any]:
        """Dispense to bowl. TTS stays on conversation app; return speak_hint only.

        With a live remote arm (default), returns immediately with status=arm_working so
        Reachy can keep listening; the arm POST runs in a background thread and Speaky
        announces the result when done. Pass wait=true to block until finished (tests).
        """
        confirmed = bool(body.get("confirmed", False))
        # Default pause after place so Reachy conversation asks "did you take it?"
        stop_after_placed = bool(body.get("stop_after_placed", True))
        wait_sync = bool(body.get("wait", False)) or not self.arm_async_enabled()

        with self._lock:
            medicine, err = self._resolve_medicine(body)
            if err:
                return err
            if medicine is None:
                return {"error": f"Unknown medicine: {body.get('medicine_id')}"}

            key = f"dispense:{medicine.id}"
            if confirmed and self.guard.enabled and not self.guard.has_pending(key):
                confirmed = False  # the AI didn't ask first: ask now instead of acting
            if confirmed:
                blocked = self.guard.require_yes("dispense", key)
                if blocked:
                    return blocked
            if not confirmed:
                self.guard.ask(key)
                return {
                    "status": "needs_confirm",
                    "medicine": {"id": medicine.id, "name": medicine.name},
                    "speak_hint": (
                        f"Time for your {medicine.name} medicine — should I bring it? "
                        "If they say yes, call dispense_medicine again with confirmed=true."
                    ),
                }

            medicine_id = medicine.id
            medicine_name = medicine.name
            person_name = None
            if medicine.person_id is not None:
                owner = self.db.get_person(medicine.person_id)
                person_name = owner.name if owner else None

            remote = HttpArmClient.from_config(self.config)
            arm_unreachable = False
            if remote is not None and not remote.probe_reachable():
                arm_unreachable = True
                remote = None

        # Live arm: kick off in the background so the conversation tool returns now.
        if remote is not None and not wait_sync:
            with self._arm_job_lock:
                if self._arm_job_busy:
                    return {
                        "status": "arm_busy",
                        "medicine": {"id": medicine_id, "name": medicine_name},
                        "speak_hint": (
                            f"The arm is still busy with {self._arm_job_label or 'another item'}. "
                            "I'll tell you when it's free — keep talking with me in the meantime."
                        ),
                    }
                self._arm_job_busy = True
                self._arm_job_label = medicine_name
            threading.Thread(
                target=self._arm_dose_job,
                kwargs={
                    "medicine_id": medicine_id,
                    "medicine_name": medicine_name,
                    "person_name": person_name,
                    "stop_after_placed": stop_after_placed,
                    "remote": remote,
                },
                name=f"arm-dose-{medicine_id}",
                daemon=True,
            ).start()
            return {
                "status": "arm_working",
                "async": True,
                "arm_remote": True,
                "demo_mode": False,
                "medicine": {"id": medicine_id, "name": medicine_name},
                "speak_hint": (
                    f"Okay — I'm asking the arm to bring your {medicine_name} now. "
                    "Keep talking with me while it works; I'll tell you when it's in the bowl."
                ),
            }

        return self._run_dose_sync(
            medicine_id=medicine_id,
            medicine_name=medicine_name,
            person_name=person_name,
            stop_after_placed=stop_after_placed,
            remote=remote,
            arm_unreachable=arm_unreachable,
        )

    def _arm_dose_job(
        self,
        *,
        medicine_id: int,
        medicine_name: str,
        person_name: str | None,
        stop_after_placed: bool,
        remote: HttpArmClient,
    ) -> None:
        """Background: POST /v1/dispense, then speak the result so conversation stays free."""
        try:
            result = self._run_dose_sync(
                medicine_id=medicine_id,
                medicine_name=medicine_name,
                person_name=person_name,
                stop_after_placed=stop_after_placed,
                remote=remote,
                arm_unreachable=False,
                announce_start=False,
            )
            hint = str(result.get("speak_hint") or "").strip()
            if hint:
                # Drop "call mark_dose_taken" coach lines for ambient announce — keep it short.
                short = hint.split("Then ask")[0].split("After they")[0].strip()
                # Wait until no tool is pending — never conversation.say during pending tools.
                self.announce_when_idle(short or hint)
        except Exception as exc:  # noqa: BLE001 — never leave busy flag stuck
            logger.exception("Background arm dose failed for %s", medicine_name)
            self.announce_when_idle(
                f"Sorry — the arm request for {medicine_name} failed. {exc}"
            )
        finally:
            with self._arm_job_lock:
                self._arm_job_busy = False
                self._arm_job_label = None

    def _run_dose_sync(
        self,
        *,
        medicine_id: int,
        medicine_name: str,
        person_name: str | None,
        stop_after_placed: bool,
        remote: HttpArmClient | None,
        arm_unreachable: bool,
        announce_start: bool = True,
    ) -> dict[str, Any]:
        """Run the dose state machine and build the tool/announce payload."""
        with self._lock:
            buddy = SilentBuddy(confirm=True, adherence_taken=None)
            vision = FakeVision(self.config)
            arm: FakeArm | HttpArmClient = remote if remote is not None else FakeArm()
            if remote is not None and announce_start:
                self.announce_progress(
                    f"Okay — I'm asking the arm to bring your {medicine_name} now. "
                    "One moment."
                )
            adherence = FakeAdherence(buddy, auto_taken=None)
            machine = DoseStateMachine(
                self.db, buddy, vision, arm, adherence, self.config
            )
            final = machine.run_dose(
                medicine_id,
                stop_after_placed=stop_after_placed,
                person_name=person_name,
            )
            events = [
                {
                    "state": e.state.value,
                    "slot_id": e.slot_id,
                    "detail": e.detail,
                }
                for e in self.db.list_dose_events(medicine_id)[-12:]
            ]

        messages = buddy.messages
        demo = (self.config.dry_run and remote is None) or arm_unreachable
        arm_remote = remote is not None
        if final.value == "Placed":
            if arm_unreachable:
                speak_hint = (
                    f"I can't reach the arm right now, so please take your {medicine_name} "
                    "from your own box, then tell me when you took it. "
                    "After they tell you, call mark_dose_taken."
                )
                messages = [speak_hint]
            elif demo:
                speak_hint = (
                    f"Demo mode: the arm is only simulated, so I could not really bring your "
                    f"{medicine_name}. Please take it from your own box, then tell me when you took it. "
                    "After they tell you, call mark_dose_taken."
                )
                messages = [speak_hint]
            elif arm_remote:
                speak_hint = (
                    " ".join(messages[-2:])
                    if len(messages) >= 2
                    else (
                        f"The arm placed your {medicine_name} in the bowl. Please take it. "
                        "Then ask if they took it and call mark_dose_taken."
                    )
                )
                if "mark_dose_taken" not in speak_hint:
                    speak_hint = (
                        f"{speak_hint} Then ask if they took it and call mark_dose_taken."
                    )
            else:
                speak_hint = (
                    f"Your {medicine_name} is in the bowl. Please take it. "
                    "Then ask if they took it and call mark_dose_taken."
                )
        else:
            speak_hint = messages[-1] if messages else final.value

        return {
            "status": "ok",
            "final_state": final.value,
            "demo_mode": demo,
            "arm_remote": arm_remote,
            "medicine": {"id": medicine_id, "name": medicine_name},
            "messages": messages,
            "events": events,
            "speak_hint": speak_hint,
        }

    def mark_adherence(self, body: dict[str, Any]) -> dict[str, Any]:
        taken = body.get("taken")
        if taken is None:
            return {"error": "taken (boolean) is required"}
        taken_bool = bool(taken)
        blocked = self.guard.require_recent_speech("mark dose")
        if blocked:
            return blocked
        with self._lock:
            medicine, err = self._resolve_medicine(body)
            if err:
                return err
            if medicine is None:
                return {"error": "medicine_id or medicine_name required / unknown"}
            buddy = SilentBuddy(confirm=True)
            vision = FakeVision(self.config)
            arm = FakeArm()
            adherence = FakeAdherence(buddy, auto_taken=taken_bool)
            machine = DoseStateMachine(
                self.db, buddy, vision, arm, adherence, self.config
            )
            try:
                final = machine.finish_adherence(medicine.id, taken=taken_bool)
            except ValueError as exc:
                return {"error": str(exc)}

        return {
            "status": "ok",
            "final_state": final.value,
            "medicine": {"id": medicine.id, "name": medicine.name},
            "speak_hint": (
                "Thank you. I marked the dose as taken."
                if taken_bool
                else "I logged this dose as missed."
            ),
        }

    def move_object(self, body: dict[str, Any]) -> dict[str, Any]:
        """Ask arm PC to move a graspable non-medicine object (bottle, box, …).

        Confirm-gated like dispense. Does not touch dose_events or the medicine catalog.
        Empty ARM_URL → demo success so voice still works offline.

        Demo smooth / ARM_MOVE_VIA=dispense: prefer the working POST /v1/dispense path
        (medicine.name = spoken label) instead of /v1/move_object.
        """
        label = " ".join(str(body.get("object_label") or body.get("label") or "").split())
        if not label:
            return {
                "error": "object_label is required",
                "speak_hint": "What should I ask the arm to bring?",
            }
        # Prefer /v1/dispense — same as the working arm curl (name="small bottle").
        force_dispense = (os.environ.get("MEDICINE_BUDDY_ARM_MOVE_VIA") or "").strip().lower() in {
            "dispense", "v1/dispense", "/v1/dispense",
        }
        if self.demo_smooth() or force_dispense:
            return self.run_dose({
                "person": body.get("person"),
                "medicine_name": label,
                "confirmed": bool(body.get("confirmed", False)),
                "stop_after_placed": True,
                "wait": bool(body.get("wait", False)),
            })
        destination = " ".join(str(body.get("destination") or "bowl").split()) or "bowl"
        kind_raw = str(body.get("kind_hint") or "").strip()
        kind_hint = kind_raw or None
        confirmed = bool(body.get("confirmed", False))
        key = f"move_object:{label.casefold()}"

        with self._lock:
            if confirmed and self.guard.enabled and not self.guard.has_pending(key):
                confirmed = False
            if confirmed:
                blocked = self.guard.require_yes("move object", key)
                if blocked:
                    return blocked
            if not confirmed:
                self.guard.ask(key)
                return {
                    "status": "needs_confirm",
                    "object": {"label": label, "kind_hint": kind_hint},
                    "destination": destination,
                    "speak_hint": (
                        f"Should I ask the arm to bring the {label} to the {destination}? "
                        "If they say yes, call move_object again with confirmed=true."
                    ),
                }

            remote = HttpArmClient.from_config(self.config)
            if remote is not None and not remote.probe_reachable():
                # Fail fast: do not block the conversation on a dead arm PC.
                return {
                    "status": "arm_unreachable",
                    "ok": False,
                    "object": {"label": label, "kind_hint": kind_hint},
                    "destination": destination,
                    "arm_remote": False,
                    "demo_mode": False,
                    "speak_hint": (
                        f"I can't reach the arm right now, so I couldn't bring the {label}. "
                        f"Please take the {label} yourself for now."
                    ),
                }
            try:
                if remote is not None:
                    # Speak before the long POST so the person isn't left in silence.
                    self.announce_progress(
                        f"Okay — I'm asking the arm to bring the {label} to the "
                        f"{destination} now. One moment."
                    )
                    result = remote.move_object(
                        label=label,
                        destination=destination,
                        kind_hint=kind_hint,
                    )
                else:
                    result = MoveObjectResult(
                        request_id=new_request_id(),
                        ok=True,
                        final_state="placed",
                        detect_detail="demo mode — no arm URL; nothing moved",
                    )
            except Exception as exc:  # noqa: BLE001 — speak failure, do not crash conversation
                return {
                    "status": "arm_unreachable",
                    "ok": False,
                    "error": str(exc)[:300],
                    "object": {"label": label, "kind_hint": kind_hint},
                    "destination": destination,
                    "speak_hint": (
                        f"I can't reach the arm right now, so I couldn't bring the {label}. "
                        f"Please take the {label} yourself for now."
                    ),
                }

        if result.ok and result.final_state == "placed":
            demo = remote is None
            hint = (
                f"Demo mode: I would ask the arm to bring the {label} to the {destination}, "
                "but no arm is connected right now."
                if demo else
                f"I sent a request to the arm for the {label}. "
                f"Done — it placed it at the {destination}."
            )
            return {
                "status": "ok",
                "ok": True,
                "final_state": result.final_state,
                "object": {"label": label, "kind_hint": kind_hint},
                "destination": destination,
                "detect_detail": result.detect_detail,
                "arm_remote": remote is not None,
                "demo_mode": demo,
                "speak_hint": hint,
            }
        detail = result.error or result.detect_detail or "arm could not complete the move"
        return {
            "status": "error",
            "ok": False,
            "final_state": result.final_state,
            "error": detail,
            "object": {"label": label, "kind_hint": kind_hint},
            "destination": destination,
            "speak_hint": (
                f"The arm could not move the {label}. {detail}. "
                "You can try again, or move it by hand."
            ),
        }

    def events(self, medicine_id: int) -> dict[str, Any]:
        medicine = self.db.get_medicine(medicine_id)
        if medicine is None:
            return {"error": f"Unknown medicine_id={medicine_id}"}
        return {
            "medicine": {"id": medicine.id, "name": medicine.name},
            "events": [
                {
                    "state": e.state.value,
                    "slot_id": e.slot_id,
                    "detail": e.detail,
                    "created_at": e.created_at,
                }
                for e in self.db.list_dose_events(medicine_id)
            ],
        }

    # ------------------------------------------------------------ status page
    def status_page(self, nav: str | None = None) -> str:
        """The dashboard. `nav` replaces the default header link (used by the in-app UI)."""
        if nav is None:
            nav = ('<div style="font-size:14px;font-weight:500;margin:0 0 10px">'
                   '<a href="/family" style="color:var(--accent)">Family settings &rarr;</a></div>')
        esc = html.escape
        meds = self.db.list_medicines()
        names = {m.id: m.name for m in meds}
        people = self.db.list_people()
        person_name = {p.id: p.name for p in people}
        owner = {m.id: person_name.get(m.person_id, "unassigned") for m in meds}
        schedules = self.db.list_active_schedules()
        due_ids = {d.schedule_id for d in find_due_doses(self.db, tz=self.tz())}
        events = self.db.list_dose_events(limit=30)[::-1]

        rows = "".join(
            f"<tr{' class=due' if s.id in due_ids else ''}><td>{esc(owner.get(s.medicine_id, '?'))}</td>"
            f"<td>{esc(names.get(s.medicine_id, '?'))}</td>"
            f"<td>{esc(describe_time(s.time_hhmm))}</td><td>{esc(describe_weekdays(s.weekdays))}</td>"
            f"<td>{'Due now' if s.id in due_ids else ''}</td><td class=muted>#{s.id}</td></tr>"
            for s in schedules
        ) or "<tr><td colspan=6 class=muted>No schedules yet. Ask Reachy to set one up.</td></tr>"

        people_rows = "".join(
            f"<tr><td>{esc(p.name)}</td><td>{'Caregiver' if p.is_caregiver else 'Family member'}</td>"
            f"<td>{len(self.db.list_medicines(p.id, all_people=False))}</td>"
            f"<td>{len(self.db.list_memories(p.id))}</td></tr>"
            for p in people
        ) or ("<tr><td colspan=4 class=muted>Nobody registered: open mode, anyone can change medicines. "
              "Ask Reachy to register the caregiver first.</td></tr>")
        mem_rows = "".join(
            f"<tr><td class=muted>{esc(m.created_at[:10])}</td><td>{esc(person_name.get(m.person_id, '?'))}</td>"
            f"<td>{esc(m.kind)}</td><td><details><summary>{esc(m.title)}</summary>"
            f"<div class=memtext>{esc(m.text)}</div></details></td></tr>"
            for m in self.db.list_memories(limit=50)
        ) or "<tr><td colspan=4 class=muted>No memories saved yet.</td></tr>"

        def local(ts: str) -> str:
            try:
                return datetime.fromisoformat(ts).astimezone().strftime("%d %b %H:%M:%S")
            except ValueError:
                return ts

        log = "".join(
            f"<tr><td class=muted>{esc(local(e.created_at))}</td><td>{esc(names.get(e.medicine_id, '?'))}</td>"
            f"<td><span class='st st-{esc(e.state.value.lower())}'>{esc(e.state.value)}</span></td>"
            f"<td class=muted>{esc(e.detail)}</td></tr>"
            for e in events
        ) or "<tr><td colspan=4 class=muted>No doses yet.</td></tr>"

        shown = self.catalog.names[:40]  # the Swiss catalog has ~1,500 names: don't render them all
        chips = "".join(f"<span class=chip>{esc(n)}</span>" for n in shown)
        if len(self.catalog.names) > len(shown):
            chips += f"<span class=chip>+{len(self.catalog.names) - len(shown)} more</span>"
        mode = ("DEMO MODE - the arm is simulated, nothing is really dispensed"
                if self.config.dry_run else "hardware")

        sec = self.guard.status()
        if sec["enabled"]:
            guard_line = (f"<b class=ok>Voice guard ON</b> &middot; medicine changes need the person's own words "
                          f"&middot; {sec['utterances_heard']} utterances heard")
        else:
            guard_line = ("<b class=warn>Voice guard OFF</b> &middot; start with <code>serve --voice-guard</code> "
                          "and the conversation launcher to protect against prompt injection")
        blocked = "".join(
            f"<tr><td class=muted>{esc(datetime.fromtimestamp(b['at']).strftime('%H:%M:%S'))}</td>"
            f"<td>{esc(b['action'])}</td><td>{esc(b['reason'])}</td></tr>"
            for b in reversed(sec["blocked"])
        ) or "<tr><td colspan=3 class=muted>Nothing blocked.</td></tr>"

        return _PAGE.format(rows=rows, log=log, chips=chips, mode=esc(mode), guard=guard_line,
                            blocked=blocked, people=people_rows, memories=mem_rows, nav=nav,
                            now=datetime.now().strftime("%H:%M:%S"))


_PAGE = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Medicine Buddy</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#77756f;--line:#e4e2dc;--accent:#2f6f4f;--due:#fff4d6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--card:#1f1f1d;--fg:#ecebe6;--muted:#9a978f;--line:#33322e;--accent:#7cc39c;--due:#3a3220}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:900px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:15px;margin:0 0 10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}}
.sub{{color:var(--muted);margin-bottom:20px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}}
.sub button{{font:inherit;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);padding:6px 12px;cursor:pointer}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse}} td,th{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-weight:600;font-size:13px;color:var(--muted)}} tr:last-child td{{border-bottom:0}}
tr.due td{{background:var(--due)}} .muted{{color:var(--muted)}}
.chip{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:3px 10px;margin:0 6px 6px 0}}
.st{{font-weight:600}} .st-taken{{color:var(--accent)}} .st-missed,.st-failed{{color:#c0392b}}
.ok{{color:var(--accent)}} .warn{{color:#c0392b}} code{{font-size:13px}}
summary{{cursor:pointer}} .memtext{{white-space:pre-wrap;margin-top:6px;color:var(--fg)}}
a{{color:var(--accent)}}
.mb-nav{{position:sticky;top:0;z-index:20;background:var(--bg);padding:8px 0 10px;margin:0 0 14px;
  border-bottom:1px solid var(--line);font-size:14px;font-weight:500;display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center}}
.mb-nav a{{color:var(--accent);text-decoration:none}} .mb-nav a.mb-nav-active{{font-weight:700;text-decoration:underline}}
.mb-nav-who{{color:var(--muted);margin-left:auto}}
</style></head><body><main>
{nav}
<h1>Medicine Buddy</h1>
<div class=sub>
  <span>Mode: {mode} &middot; updated {now} &middot; no auto-reload</span>
  <button type=button onclick="location.reload()">Refresh</button>
</div>
<section><h2>People</h2><table><tr><th>Name</th><th>Role</th><th>Medicines</th><th>Memories</th></tr>{people}</table></section>
<section><h2>Schedules</h2><table><tr><th>Person</th><th>Medicine</th><th>Time</th><th>Days</th><th></th><th></th></tr>{rows}</table></section>
<section><h2>Family memories</h2><table><tr><th>Saved</th><th>From</th><th>Kind</th><th>Title (click to read)</th></tr>{memories}</table></section>
<section><h2>Allowed medicines</h2>{chips}</section>
<section><h2>Recent doses</h2><table><tr><th>When</th><th>Medicine</th><th>State</th><th>Detail</th></tr>{log}</table></section>
<section><h2>Security</h2><p>{guard}</p><table><tr><th>When</th><th>Blocked action</th><th>Why</th></tr>{blocked}</table></section>
</main>
<script>try {{ sessionStorage.removeItem("mb_start_delay_ms"); }} catch (e) {{}}</script>
</body></html>"""


# ---------------------------------------------------------------- HTTP plumbing

def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise ValueError("bad Content-Length") from exc
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    return body


def _send(handler: BaseHTTPRequestHandler, code: int, body: bytes, content_type: str) -> None:
    # No Access-Control-Allow-Origin: other websites must not be able to call this API
    # from the user's browser (it used to send "*", which allowed exactly that).
    handler.send_response(code)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "no-referrer")
    if content_type.startswith("text/html"):
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_json(handler: BaseHTTPRequestHandler, code: int, payload: dict[str, Any]) -> None:
    handler._last_payload = payload  # type: ignore[attr-defined]  # outcome for the debug log
    _send(handler, code, json.dumps(payload).encode("utf-8"), "application/json")


def make_handler(service: MedicineBuddyService) -> type[BaseHTTPRequestHandler]:
    post_routes = {
        "/schedules": service.add_schedule,
        "/schedules/update": service.update_schedule,
        "/schedules/delete": service.delete_schedule,
        "/doses/run": service.run_dose,
        "/doses/adherence": service.mark_adherence,
        "/arm/move_object": service.move_object,
        "/voice/heard": service.voice_heard,  # loopback only, see _post
        "/voice/listen_mode": service.listen_mode,  # loopback only — patient story listening
        "/medicines/list": service.list_medicines,
        "/people/register": service.register_person,
        "/people/list": service.list_people_info,
        "/memories/save": service.save_memory,
        "/memories/recall": service.recall_memories,
        "/memories/forget": service.forget_memory,
        "/family/call": service.start_family_call,
        "/activities/list": service.list_activities,
        "/activities/suggest": service.suggest_activity,
        "/calendar/upcoming": service.list_upcoming_events,
        "/calendar/create": service.create_calendar_event,
        "/calendar/cancel": service.cancel_calendar_event,
        "/calendar/sync": service.sync_calendar_voice,
        "/clock/now": service.clock_now,
    }
    # Typed on the Family settings page only: never reachable by an AI tool.
    settings_routes = {
        "/settings/people": service.settings_people,
        "/settings/suggest": service.settings_suggest,
        "/settings/register": service.settings_register,
        "/settings/passphrase": service.settings_change_passphrase,
        "/settings/timezone": service.settings_timezone,
        "/settings/require_passphrases": service.settings_require_passphrases,
        "/settings/caregiver_alerts": service.settings_caregiver_alerts,
        "/settings/encourage_self_care": service.settings_encourage_self_care,
        "/settings/demo_smooth": service.settings_demo_smooth,
        "/settings/family_call": service.settings_family_call,
        "/settings/checkins": service.settings_checkins,
        "/settings/activity_nudges": service.settings_activity_nudges,
        "/settings/reset_microphone": service.reset_microphone,
        "/settings/calendar": service.settings_calendar,
        "/settings/calendar_accept": service.accept_calendar_proposal,
        "/settings/calendar_reject": service.reject_calendar_proposal,
    }
    loopback = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            line = f"[api] {self.address_string()} - {format % args}"
            try:
                print(line)
            except UnicodeEncodeError:  # never let a console encoding problem break a request
                print(line.encode("ascii", "replace").decode("ascii"))

        def _own_hosts(self) -> set[str]:
            port = self.server.server_address[1]
            return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

        def _foreign_origin(self) -> bool:
            """True for requests a different website made from the user's browser (CSRF).
            Tools and scripts send no Origin header and are unaffected."""
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                return True
            origin = self.headers.get("Origin")
            if origin is None:
                return False
            # Only our own pages, addressed as localhost. (Trusting the Host header here would
            # let a DNS-rebinding site that points its name at 127.0.0.1 through.)
            return urlparse(origin).netloc.lower() not in self._own_hosts()

        def _local_page_request(self) -> bool:
            """Family page / settings: this computer, addressed as localhost (blocks DNS rebinding)."""
            return (self.client_address[0] in loopback
                    and str(self.headers.get("Host", "")).lower() in self._own_hosts())

        def do_OPTIONS(self) -> None:  # noqa: N802
            # No cross-origin access is offered, so pre-flight requests are always refused.
            _send_json(self, 403, {"error": "cross-origin requests are not allowed"})

        def send_response(self, code: int, message: str | None = None) -> None:
            self._status_code = code
            super().send_response(code, message)

        def _handle(self, method: str, fn) -> None:
            start = time.perf_counter()
            self._status_code, self._last_payload = 0, None
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — always answer, never drop the request
                _send_json(self, 500, {"error": f"internal error: {exc}"})
            finally:
                path = urlparse(self.path).path
                if path not in {"/health"}:  # skip noisy liveness polling
                    service.record_request(method, path, self._status_code,
                                           (time.perf_counter() - start) * 1000, self._last_payload)

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET", self._get)

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST", self._post)

        def _get(self) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                _send(self, 200, service.status_page().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/family":
                if not self._local_page_request():
                    _send_json(self, 403, {"error": "the family settings page only opens on this computer "
                                                    "at http://127.0.0.1:<port>/family"})
                    return
                _send(self, 200, service.family_page().encode("utf-8"), "text/html; charset=utf-8")
                return
            simple = {
                "/health": service.health,
                "/medicines": service.list_medicines,
                "/due": service.due,
                "/catalog": service.catalog_names,
                "/security": service.security,
            }
            if path in simple:
                _send_json(self, 200, simple[path]())
                return
            if path.startswith("/medicines/") and path.endswith("/events"):
                parts = path.split("/")
                try:
                    medicine_id = int(parts[2])
                except (IndexError, ValueError):
                    _send_json(self, 400, {"error": "bad medicine id"})
                    return
                payload = service.events(medicine_id)
                _send_json(self, 404 if "error" in payload else 200, payload)
                return
            # /calendar/<token>.ics — subscribe URL for Outlook / Google / Apple
            if path.startswith("/calendar/") and path.endswith(".ics"):
                token = path[len("/calendar/"):-len(".ics")]
                ics = service.export_ics_for_token(token)
                if ics is None:
                    _send_json(self, 404, {"error": "unknown or revoked calendar link"})
                    return
                _send(self, 200, ics.encode("utf-8"), "text/calendar; charset=utf-8")
                return
            _send_json(self, 404, {"error": f"unknown path {path}"})

        def _post(self) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if self._foreign_origin():
                _send_json(self, 403, {"error": "requests from other websites are not allowed"})
                return
            if path in settings_routes:
                if not self._local_page_request() or not service.check_settings_token(
                        self.headers.get("X-Family-Token")):
                    _send_json(self, 403, {"error": "settings can only be changed from the family page on this computer"})
                    return
                if not str(self.headers.get("Content-Type", "")).startswith("application/json"):
                    _send_json(self, 415, {"error": "JSON expected"})
                    return
            try:
                body = _read_json(self)
            except (json.JSONDecodeError, ValueError) as exc:
                _send_json(self, 400, {"error": f"invalid JSON: {exc}"})
                return
            if path in settings_routes:
                payload = settings_routes[path](body)
                _send_json(self, 400 if "error" in payload else 200, payload)
                return
            route = post_routes.get(path)
            if route is None:
                _send_json(self, 404, {"error": f"unknown path {path}"})
                return
            if path in {"/voice/heard", "/voice/listen_mode"} and self.client_address[0] not in loopback:
                _send_json(self, 403, {"error": "voice control is only accepted from this computer"})
                return
            payload = route(body)
            _send_json(self, 400 if "error" in payload else 200, payload)

    return Handler


def start_server(
    host: str,
    port: int,
    db_path: Path,
    config: AppConfig | None = None,
    voice_guard: bool = False,
) -> tuple[MedicineBuddyService, ThreadingHTTPServer, threading.Thread]:
    """Start the API in a background thread (used by the Reachy Mini app). Stop with
    `server.shutdown(); service.close()`."""
    catalog = MedicineCatalog.load()
    service = MedicineBuddyService(db_path, config, catalog, VoiceGuard(catalog, enabled=voice_guard))
    server = ThreadingHTTPServer((host, port), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, name="medicine-buddy-api", daemon=True)
    thread.start()
    return service, server, thread


def serve(
    host: str,
    port: int,
    db_path: Path,
    config: AppConfig | None = None,
    voice_guard: bool = False,
) -> None:
    catalog = MedicineCatalog.load()
    service = MedicineBuddyService(db_path, config, catalog, VoiceGuard(catalog, enabled=voice_guard))
    handler = make_handler(service)
    print(f"Voice guard: {'ON - medicine changes need the person to say so' if voice_guard else 'OFF'}")
    server = ThreadingHTTPServer((host, port), handler)
    shown = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    print(f"Medicine Buddy API on http://{host}:{port} (dry_run={service.config.dry_run})")
    print(f"Status page: http://{shown}:{port}/   database: {Path(db_path).resolve()}")
    print("Endpoints: GET /health /medicines /due /catalog /security  "
          "POST /schedules /schedules/update /schedules/delete /doses/run /doses/adherence /voice/heard")
    try:
        server.serve_forever()
    finally:
        service.close()
        server.server_close()
