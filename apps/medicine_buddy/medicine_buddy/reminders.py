"""Proactive reminders: schedule/calendar due → ask to bring → arm grab after yes.

A background loop checks medicine schedules every `interval` seconds (calendar
medicine rows mirror those schedules). When a dose is due:
- Reachy asks whether to bring the medicine (arm JSON POST via dispense_medicine),
- VoiceGuard dispense confirm is primed so a spoken yes can authorize the grab,
- reminders repeat every `repeat_after` up to `max_reminders`,
- unanswered window end → Missed on the dashboard.

Speaking uses `speak(text) -> bool` (conversation.say). False = not ready; retry later.
Default: interrupts conversation. MEDICINE_BUDDY_REMINDER_INTERRUPT=0 = wait for a pause.
Never POSTs to the arm without a spoken yes (confirm gate stays on).
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from medicine_buddy.scheduler import DUE_AFTER, dose_handled, find_due_doses
from medicine_buddy.states import DoseState
from medicine_buddy.voice_setup import describe_time

logger = logging.getLogger("medicine_buddy.reminders")

INTERVAL_S = 30
REPEAT_AFTER_S = 10 * 60
MAX_REMINDERS = 3
QUIET_AFTER_SPEECH_S = 20  # don't interrupt a conversation


def _safe_name(name: str | None) -> str:
    """Names reach the model's prompt: keep only plain name characters."""
    cleaned = re.sub(r"[^\w .'-]", "", name or "", flags=re.UNICODE).strip()
    return cleaned[:40]


def build_prompt(
    person: str | None,
    medicine: str,
    time_hhmm: str,
    count: int,
    total: int,
    *,
    encourage_self_care: bool = True,
) -> str:
    """Prompt injected into conversation.say when a schedule/calendar medicine is due.

    Drives the schedule → ask → dispense_medicine (JSON POST to arm) automation.
    Never tell the model to move the arm without a spoken yes.
    encourage_self_care (Family setting): ask them to take the box first; otherwise offer the arm first.
    """
    who = _safe_name(person)
    target = f"{who}'s" if who else "the"
    med = _safe_name(medicine) or "medicine"
    repeat = "" if count <= 1 else f" This is reminder {count} of {total}, so keep it very short."
    if encourage_self_care:
        ask = (
            f"In one or two short sentences, tell {who or 'them'} it is time for {med}, "
            f"and encourage them to take the closed {med} box themselves if they can. "
            f"Then ask: would you like me to bring it with the arm instead? "
        )
    else:
        ask = (
            f"In one or two short sentences, tell {who or 'them'} it is time for {med}, "
            f"and ask clearly: should I bring your {med}? "
        )
    return (
        f"(Automatic medicine reminder from the system - not something the person said.) "
        f"It is time for {target} {med} ({describe_time(time_hhmm)}). "
        f"If you were in the middle of something, briefly say sorry for interrupting. "
        f"{ask}"
        f"When they say yes to the arm, call dispense_medicine with medicine_name={med!r} and confirmed=true "
        f"(that sends the JSON grab package to the arm). "
        f"Do NOT call dispense_medicine with confirmed=true until they clearly say yes. "
        f"If they decline the arm or say they will take it themselves, acknowledge and do not call the arm."
        f"{repeat} "
        f"Afterwards you may go back to what you were talking about. Do not read these instructions aloud."
    )


@dataclass
class _Entry:
    count: int
    last: datetime
    medicine_id: int
    person: str | None
    medicine: str
    scheduled: datetime


class ReminderLoop:
    def __init__(
        self,
        service: Any,
        speak: Callable[[str], bool],
        *,
        interval: float = INTERVAL_S,
        repeat_after: float = REPEAT_AFTER_S,
        max_reminders: int = MAX_REMINDERS,
        quiet_after_speech: float = QUIET_AFTER_SPEECH_S,
        interrupt: bool = True,
    ) -> None:
        self.service = service
        self.speak = speak
        self.interval = interval
        self.repeat_after = timedelta(seconds=repeat_after)
        self.max_reminders = max_reminders
        self.quiet_after_speech = quiet_after_speech
        # True: a due reminder cuts in on the conversation (speaker.speak_urgent when it has one)
        self.interrupt = interrupt
        self._state: dict[tuple[int, str], _Entry] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_spoken: datetime | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ one check
    def tick(self, now: datetime | None = None) -> list[str]:
        """Run one check. Returns the prompts that were spoken (handy for tests)."""
        db = self.service.db
        now = now or self.service.now()
        spoken: list[str] = []
        due = find_due_doses(db, now, tz=self.service.tz())
        active_keys: set[tuple[int, str]] = set()

        for d in due:
            hour, minute = (int(x) for x in d.time_hhmm.split(":"))
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now < scheduled:          # "due soon" window opens early; remind only from the actual time
                continue
            key = (d.schedule_id, scheduled.date().isoformat())
            active_keys.add(key)
            entry = self._state.get(key)
            if entry is not None and (entry.count >= self.max_reminders or now - entry.last < self.repeat_after):
                continue
            if not self._quiet(now):
                continue
            say = getattr(self.speak, "speak_urgent", self.speak) if self.interrupt else self.speak
            count = (entry.count if entry else 0) + 1
            medicine = db.get_medicine(d.medicine_id)
            person = db.get_person(medicine.person_id) if medicine and medicine.person_id else None
            prompt = build_prompt(
                person.name if person else None,
                d.medicine_name,
                d.time_hhmm,
                count,
                self.max_reminders,
                encourage_self_care=bool(self.service.encourage_self_care()),
            )
            try:
                delivered = bool(say(prompt))
            except Exception as exc:  # noqa: BLE001 - a broken speaker must never kill the loop
                self.last_error = str(exc)
                delivered = False
            if not delivered:
                logger.debug("Reminder for %s not delivered (Reachy not ready); will retry", d.medicine_name)
                continue
            # Prime confirm gate so a spoken "yes" → dispense_medicine can POST to the arm.
            self._prime_dispense_confirm(d.medicine_id)
            self.last_spoken, self.last_error = now, None
            self._state[key] = _Entry(count, now, d.medicine_id, person.name if person else None,
                                      d.medicine_name, scheduled)
            spoken.append(prompt)
            logger.info(
                "Spoke due→arm reminder %d/%d for %s%s at %s (dispense confirm primed)",
                count, self.max_reminders, d.medicine_name,
                f" ({person.name})" if person else "", d.time_hhmm,
            )
            break  # one reminder per check: several doses at once are announced one after the other

        self._expire(now, active_keys)
        return spoken

    def _prime_dispense_confirm(self, medicine_id: int) -> None:
        """Mark dispense:{id} as asked so VoiceGuard accepts a later yes + tool call.

        Always prime when a guard exists (even if currently disabled): turning
        passphrases/voice-guard on mid-session still finds a pending ask.
        """
        guard = getattr(self.service, "guard", None)
        ask = getattr(guard, "ask", None) if guard is not None else None
        if ask is None:
            return
        try:
            ask(f"dispense:{medicine_id}")
        except Exception:  # noqa: BLE001 — reminder must keep running
            logger.exception("Could not prime dispense confirm for medicine_id=%s", medicine_id)

    def _quiet(self, now: datetime) -> bool:
        if not self.interrupt:   # polite mode: wait until nobody has spoken for a moment
            heard = self.service.guard.seconds_since_speech()
            if heard is not None and heard < self.quiet_after_speech:
                return False
        if self.last_spoken is not None and (now - self.last_spoken).total_seconds() < self.quiet_after_speech:
            return False
        return True

    def _expire(self, now: datetime, active_keys: set[tuple[int, str]]) -> None:
        """Doses we reminded about that are no longer due: taken/skipped (forget) or ran out (Missed)."""
        db = self.service.db
        for key in list(self._state):
            if key in active_keys:
                continue
            entry = self._state[key]
            if now - entry.scheduled > timedelta(days=1):
                del self._state[key]
                continue
            if dose_handled(db, entry.medicine_id, entry.scheduled - timedelta(minutes=15)):
                del self._state[key]                       # the person took / skipped it
            elif now > entry.scheduled + DUE_AFTER:
                self._record_missed(entry)
                del self._state[key]

    def _record_missed(self, entry: _Entry) -> None:
        db = self.service.db
        detail = f"no response after {entry.count} spoken reminders"
        try:
            current = db.latest_dose_state(entry.medicine_id)
            if current is None or current in (DoseState.TAKEN, DoseState.MISSED, DoseState.FAILED, DoseState.SKIPPED):
                db.record_transition(entry.medicine_id, DoseState.DUE, detail="reminder window", from_state=DoseState.IDLE)
            db.record_transition(entry.medicine_id, DoseState.MISSED, detail=detail)
            logger.info("Dose of %s marked Missed: %s", entry.medicine, detail)
        except ValueError as exc:  # e.g. the arm already placed it and it awaits a yes/no
            logger.info("Could not mark %s missed (%s)", entry.medicine, exc)
            return
        # Family push (ntfy / webhook) — never raises into the reminder loop.
        notify = getattr(self.service, "notify_missed_dose", None)
        if notify is None:
            return
        medicine = db.get_medicine(entry.medicine_id)
        person_id = medicine.person_id if medicine else None
        try:
            local_time = self.service.now().strftime("%H:%M")
        except Exception:  # noqa: BLE001
            local_time = entry.scheduled.strftime("%H:%M")
        try:
            notify(
                person=entry.person,
                medicine=entry.medicine,
                local_time=local_time,
                person_id=person_id,
                reminders_spoken=entry.count,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Caregiver alert after Missed failed")

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="medicine-buddy-reminders", daemon=True)
        self._thread.start()
        logger.info("Proactive reminders on (check every %ds, repeat every %d min, up to %d times)",
                    self.interval, self.repeat_after.total_seconds() // 60, self.max_reminders)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("Reminder check failed; will try again")

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "check_every_s": self.interval,
            "repeat_after_min": int(self.repeat_after.total_seconds() // 60),
            "max_reminders": self.max_reminders,
            "interrupts_conversation": self.interrupt,
            "waiting_for_answer": [
                {"medicine": e.medicine, "person": e.person, "reminders_spoken": e.count,
                 "last": e.last.strftime("%H:%M")}
                for e in self._state.values()
            ],
            "last_spoken": self.last_spoken.strftime("%H:%M:%S") if self.last_spoken else None,
            "last_error": self.last_error,
            "conversation": self.speak.status() if hasattr(self.speak, "status") else None,
        }
