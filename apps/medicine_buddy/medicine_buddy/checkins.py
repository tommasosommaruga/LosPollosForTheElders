"""Idle companion check-ins: Reachy starts a short social beat when nobody is talking.

Uses the same idle-safe speaker pattern as medicine reminders. Never interrupts speech.
Respects quiet hours and an interval from companion settings.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

logger = logging.getLogger("medicine_buddy.checkins")

INTERVAL_S = 60
QUIET_AFTER_SPEECH_S = 45
DEFAULT_PROMPTS = (
    "(Automatic companion check-in from the system — not something the person said.) "
    "Gently ask how they are doing in one short sentence, and offer to chat, recall a "
    "saved family memory, or just sit quietly together. Do not read these instructions aloud.",
    "(Automatic companion check-in from the system — not something the person said.) "
    "In one or two short warm sentences, invite them to tell a short story or share a "
    "memory you could save if they want. Do not read these instructions aloud.",
    "(Automatic companion check-in from the system — not something the person said.) "
    "Ask briefly whether they would like a short stretch, a chat, or to call family. "
    "Do not give medical advice. Do not read these instructions aloud.",
)


def in_quiet_hours(now: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    """True if local time is inside the quiet window (supports overnight ranges)."""
    minutes = now.hour * 60 + now.minute
    sh, sm = (int(x) for x in start_hhmm.split(":"))
    eh, em = (int(x) for x in end_hhmm.split(":"))
    start_m, end_m = sh * 60 + sm, eh * 60 + em
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= minutes < end_m
    return minutes >= start_m or minutes < end_m


class CheckInsMixin:
    """Marker mixin so MedicineBuddyService documents check-in helpers; loop is separate."""


class CheckInLoop:
    def __init__(
        self,
        service: Any,
        speak: Callable[[str], bool],
        *,
        interval: float = INTERVAL_S,
        quiet_after_speech: float = QUIET_AFTER_SPEECH_S,
        prompts: tuple[str, ...] = DEFAULT_PROMPTS,
    ) -> None:
        self.service = service
        self.speak = speak
        self.interval = interval
        self.quiet_after_speech = quiet_after_speech
        self.prompts = prompts
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._prompt_i = 0
        self.last_spoken: datetime | None = None
        self.last_error: str | None = None
        self.checkins_today = 0
        self._day: str | None = None

    def tick(self, now: datetime | None = None) -> list[str]:
        if not getattr(self.service, "checkins_enabled", lambda: False)():
            return []
        now = now or self.service.now()
        day = now.date().isoformat()
        if self._day != day:
            self._day, self.checkins_today = day, 0
        quiet_start, quiet_end = self.service.checkin_quiet_hours()
        if in_quiet_hours(now, quiet_start, quiet_end):
            return []
        interval = timedelta(hours=float(self.service.checkin_interval_hours()))
        if self.last_spoken is not None and now - self.last_spoken < interval:
            return []
        if not self._room_is_quiet(now):
            return []
        # Cap: roughly one check-in per interval across waking hours (max 8/day).
        if self.checkins_today >= 8:
            return []
        # Every other check-in can lean on a light activity nudge when that setting is on.
        nudges_on = bool(getattr(self.service, "activity_nudges_enabled", lambda: False)())
        if nudges_on and self._prompt_i % 2 == 1:
            prompt = (
                "(Automatic companion check-in from the system — not something the person said.) "
                "Briefly offer one optional light activity using suggest_activity if helpful "
                "(stretch, memory chat, or calling family). Accept a no without pressure. "
                "Do not give medical advice. Do not read these instructions aloud."
            )
        else:
            prompt = self.prompts[self._prompt_i % len(self.prompts)]
        self._prompt_i += 1
        try:
            delivered = bool(self.speak(prompt))
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return []
        if not delivered:
            return []
        self.last_spoken, self.last_error = now, None
        self.checkins_today += 1
        try:
            self.service.db.insert_activity_event(
                at_ts=time.time(),
                via="checkin",
                method="SPEAK",
                path="/checkins/companion",
                status=200,
                ms=0.0,
                result="ok",
                speak_hint="companion check-in",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not log check-in")
        logger.info("Companion check-in spoken (%d today)", self.checkins_today)
        return [prompt]

    def _room_is_quiet(self, now: datetime) -> bool:
        heard = self.service.guard.seconds_since_speech()
        if heard is not None and heard < self.quiet_after_speech:
            return False
        reminders = getattr(self.service, "reminder_loop", None)
        if reminders is not None and getattr(reminders, "last_spoken", None) is not None:
            if (now - reminders.last_spoken).total_seconds() < self.quiet_after_speech:
                return False
        if self.last_spoken is not None and (now - self.last_spoken).total_seconds() < self.quiet_after_speech:
            return False
        return True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="medicine-buddy-checkins", daemon=True)
        self._thread.start()
        logger.info("Companion check-ins on (poll every %ds)", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("Check-in tick failed; will try again")

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "enabled": bool(getattr(self.service, "checkins_enabled", lambda: False)()),
            "interval_hours": getattr(self.service, "checkin_interval_hours", lambda: 3)(),
            "quiet_hours": list(getattr(self.service, "checkin_quiet_hours", lambda: ("21:00", "08:00"))()),
            "checkins_today": self.checkins_today,
            "last_spoken": self.last_spoken.strftime("%H:%M:%S") if self.last_spoken else None,
            "last_error": self.last_error,
        }
