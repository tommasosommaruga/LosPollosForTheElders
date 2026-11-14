"""HTTP API so reachy_mini_conversation tools can drive Medicine Buddy remotely.

Also serves a read-only status page at http://127.0.0.1:8787/ showing schedules,
the allowed-medicine list, what's due and the dose log.
"""

from __future__ import annotations

import html
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from medicine_buddy.access import PeopleAccessMixin
from medicine_buddy.family_page import render_family_page
from medicine_buddy.settings import SettingsMixin
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database, Medicine, Person, Schedule
from medicine_buddy.dry_run import FakeAdherence, FakeArm, FakeVision
from medicine_buddy.orchestrator import DoseStateMachine
from medicine_buddy.scheduler import find_due_doses
from medicine_buddy.silent_buddy import SilentBuddy
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


class MedicineBuddyService(PeopleAccessMixin, SettingsMixin):
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

    def family_page(self) -> str:
        return render_family_page(self.settings_token)

    # ------------------------------------------------------------ voice guard input
    def voice_heard(self, body: dict[str, Any]) -> dict[str, Any]:
        """The person's real speech, forwarded by the launcher (loopback only)."""
        text = str(body.get("text") or "").strip()
        if not text:
            return {"error": "text is required"}
        u = self.guard.heard(text)
        return {"status": "ok", "medicines": sorted(u.medicines), "yes": u.yes}

    def security(self) -> dict[str, Any]:
        return self.guard.status()

    def close(self) -> None:
        self.db.close()

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "dry_run": self.config.dry_run,
            "service": "medicine-buddy",
            "version": "0.3.0",
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
            return None, err
        medicine = self.db.get_medicine_by_name(name, person.id if person else None)
        if medicine is None:
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
        items = find_due_doses(self.db)
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
            weekdays = parse_weekdays(weekdays_raw)
            time_hhmm = parse_time_hhmm(time_raw)
        except ValueError as exc:
            return {"error": str(exc), "speak_hint": f"Sorry, I didn't understand that time or day. {exc}."}
        person, err = self._resolve_person(body.get("person"), for_change=True)
        if err:
            return err
        blocked = self._require_owner("add schedule", body, person) or self.guard.require_mention("add schedule", name)
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
            return self._verified(medicine, schedule.id, weekdays, time_hhmm, "is set up", person)

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
            new_weekdays = parse_weekdays(str(new_weekdays_raw)) if new_weekdays_raw else None
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
            self.db.update_schedule(target.id, weekdays=weekdays, time_hhmm=time_hhmm)
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
                self.db.deactivate_schedule(s.id)
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
        """Dispense to bowl. TTS stays on conversation app; return speak_hint only."""
        confirmed = bool(body.get("confirmed", False))
        # Default pause after place so Reachy conversation asks "did you take it?"
        stop_after_placed = bool(body.get("stop_after_placed", True))

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

            buddy = SilentBuddy(confirm=True, adherence_taken=None)
            vision = FakeVision(self.config)
            arm = FakeArm()
            adherence = FakeAdherence(buddy, auto_taken=None)
            machine = DoseStateMachine(
                self.db, buddy, vision, arm, adherence, self.config
            )
            final = machine.run_dose(
                medicine.id, stop_after_placed=stop_after_placed
            )
            events = [
                {
                    "state": e.state.value,
                    "slot_id": e.slot_id,
                    "detail": e.detail,
                }
                for e in self.db.list_dose_events(medicine.id)[-12:]
            ]

        speak_hint = buddy.messages[-1] if buddy.messages else final.value
        if final.value == "Placed":
            speak_hint = (
                f"Your {medicine.name} is in the bowl. Please take it. "
                "Then ask if they took it and call mark_dose_taken."
            )

        return {
            "status": "ok",
            "final_state": final.value,
            "medicine": {"id": medicine.id, "name": medicine.name},
            "messages": buddy.messages,
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
    def status_page(self) -> str:
        esc = html.escape
        meds = self.db.list_medicines()
        names = {m.id: m.name for m in meds}
        people = self.db.list_people()
        person_name = {p.id: p.name for p in people}
        owner = {m.id: person_name.get(m.person_id, "unassigned") for m in meds}
        schedules = self.db.list_active_schedules()
        due_ids = {d.schedule_id for d in find_due_doses(self.db)}
        events = self.db.list_dose_events()[-30:][::-1]

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

        chips = "".join(f"<span class=chip>{esc(n)}</span>" for n in self.catalog.names)
        mode = "dry-run (simulated arm)" if self.config.dry_run else "hardware"

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
                            blocked=blocked, people=people_rows, memories=mem_rows,
                            now=datetime.now().strftime("%H:%M:%S"))


_PAGE = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta http-equiv=refresh content=15>
<title>Medicine Buddy</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#77756f;--line:#e4e2dc;--accent:#2f6f4f;--due:#fff4d6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--card:#1f1f1d;--fg:#ecebe6;--muted:#9a978f;--line:#33322e;--accent:#7cc39c;--due:#3a3220}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:900px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:15px;margin:0 0 10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}}
.sub{{color:var(--muted);margin-bottom:20px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse}} td,th{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-weight:600;font-size:13px;color:var(--muted)}} tr:last-child td{{border-bottom:0}}
tr.due td{{background:var(--due)}} .muted{{color:var(--muted)}}
.chip{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:3px 10px;margin:0 6px 6px 0}}
.st{{font-weight:600}} .st-taken{{color:var(--accent)}} .st-missed,.st-failed{{color:#c0392b}}
.ok{{color:var(--accent)}} .warn{{color:#c0392b}} code{{font-size:13px}}
summary{{cursor:pointer}} .memtext{{white-space:pre-wrap;margin-top:6px;color:var(--fg)}}
</style></head><body><main>
<h1>Medicine Buddy <a href="/family" style="font-size:14px;font-weight:500;margin-left:10px;color:var(--accent)">Family settings &rarr;</a></h1>
<div class=sub>Mode: {mode} &middot; updated {now} &middot; refreshes every 15 s</div>
<section><h2>People</h2><table><tr><th>Name</th><th>Role</th><th>Medicines</th><th>Memories</th></tr>{people}</table></section>
<section><h2>Schedules</h2><table><tr><th>Person</th><th>Medicine</th><th>Time</th><th>Days</th><th></th><th></th></tr>{rows}</table></section>
<section><h2>Family memories</h2><table><tr><th>Saved</th><th>From</th><th>Kind</th><th>Title (click to read)</th></tr>{memories}</table></section>
<section><h2>Allowed medicines</h2>{chips}</section>
<section><h2>Recent doses</h2><table><tr><th>When</th><th>Medicine</th><th>State</th><th>Detail</th></tr>{log}</table></section>
<section><h2>Security</h2><p>{guard}</p><table><tr><th>When</th><th>Blocked action</th><th>Why</th></tr>{blocked}</table></section>
</main></body></html>"""


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
    _send(handler, code, json.dumps(payload).encode("utf-8"), "application/json")


def make_handler(service: MedicineBuddyService) -> type[BaseHTTPRequestHandler]:
    post_routes = {
        "/schedules": service.add_schedule,
        "/schedules/update": service.update_schedule,
        "/schedules/delete": service.delete_schedule,
        "/doses/run": service.run_dose,
        "/doses/adherence": service.mark_adherence,
        "/voice/heard": service.voice_heard,  # loopback only, see _post
        "/medicines/list": service.list_medicines,
        "/people/register": service.register_person,
        "/people/list": service.list_people_info,
        "/memories/save": service.save_memory,
        "/memories/recall": service.recall_memories,
        "/memories/forget": service.forget_memory,
    }
    # Typed on the Family settings page only: never reachable by an AI tool.
    settings_routes = {
        "/settings/people": service.settings_people,
        "/settings/suggest": service.settings_suggest,
        "/settings/register": service.settings_register,
        "/settings/passphrase": service.settings_change_passphrase,
    }
    loopback = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            print(f"[api] {self.address_string()} - {format % args}")

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

        def do_GET(self) -> None:  # noqa: N802
            try:
                self._get()
            except Exception as exc:  # noqa: BLE001 — always answer, never drop the request
                _send_json(self, 500, {"error": f"internal error: {exc}"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._post()
            except Exception as exc:  # noqa: BLE001
                _send_json(self, 500, {"error": f"internal error: {exc}"})

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
            if path == "/voice/heard" and self.client_address[0] not in loopback:
                _send_json(self, 403, {"error": "voice transcripts are only accepted from this computer"})
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
