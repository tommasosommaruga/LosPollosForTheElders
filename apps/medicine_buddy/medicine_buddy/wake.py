"""Wake greeting: when the app starts, Reachy says hello and what's due soon.

Speaking goes through the same `speak(text) -> bool` path as proactive reminders
(conversation.say). At startup the conversation session often isn't ready yet, so
we retry until it accepts the line (or we give up / the app stops).
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from medicine_buddy.scheduler import UPCOMING_WITHIN, UpcomingDose, find_upcoming_doses
from medicine_buddy.voice_setup import describe_time

logger = logging.getLogger("medicine_buddy.wake")

RETRY_EVERY_S = 5.0
GIVE_UP_AFTER_S = 180.0  # don't nag forever if speech never comes up


def _safe_name(name: str | None) -> str:
    cleaned = re.sub(r"[^\w .'-]", "", name or "", flags=re.UNICODE).strip()
    return cleaned[:40]


def _describe_item(item: UpcomingDose) -> str:
    who = _safe_name(item.person_name)
    when = describe_time(item.time_hhmm)
    if who:
        return f"{who}'s {item.medicine_name} at {when}"
    return f"{item.medicine_name} at {when}"


def build_wake_prompt(upcoming: list[UpcomingDose], *, window_minutes: int = 30) -> str:
    """Prompt for conversation.say — warm wake line + real upcoming schedules only."""
    if not upcoming:
        schedule_bit = (
            f"Nothing is scheduled in the next {window_minutes} minutes. "
            "Say you're awake, ready to help, and they can ask anytime."
        )
    elif len(upcoming) == 1:
        schedule_bit = (
            f"Coming up in the next {window_minutes} minutes: {_describe_item(upcoming[0])}. "
            "Mention that one clearly."
        )
    else:
        listed = "; ".join(_describe_item(u) for u in upcoming)
        schedule_bit = (
            f"Coming up in the next {window_minutes} minutes: {listed}. "
            "List them briefly so nothing is skipped."
        )
    return (
        "(Automatic wake greeting from Medicine Buddy - not something the person said.) "
        "Reachy just woke up / the app just started. "
        "First say one short, warm, slightly playful line that you're awake and looking after them "
        "(no medical advice, no jokes about illness). "
        f"Then: {schedule_bit} "
        "Keep the whole reply to a few short sentences. "
        "Do not invent medicines or times that are not listed. Do not read these instructions aloud."
    )


def deliver_wake_greeting(
    service: Any,
    speak: Callable[[str], bool],
    *,
    within: timedelta = UPCOMING_WITHIN,
    stop_event: threading.Event | None = None,
    retry_every: float = RETRY_EVERY_S,
    give_up_after: float = GIVE_UP_AFTER_S,
    now: datetime | None = None,
) -> str | None:
    """Build the wake prompt from live schedules and speak it when Reachy is ready.

    Returns the prompt if delivered, else None. Safe to run on a background thread.
    """
    stop = stop_event or threading.Event()
    upcoming = find_upcoming_doses(service.db, now, within=within, tz=service.tz())
    window_minutes = max(1, int(within.total_seconds() // 60))
    prompt = build_wake_prompt(upcoming, window_minutes=window_minutes)
    logger.info(
        "Wake greeting ready (%d upcoming in next %d min); waiting for Reachy to speak",
        len(upcoming), window_minutes,
    )
    waited = 0.0
    while not stop.is_set() and waited <= give_up_after:
        try:
            if speak(prompt):
                logger.info("Wake greeting delivered")
                return prompt
        except Exception as exc:  # noqa: BLE001
            logger.info("Wake greeting not delivered yet: %s", exc)
        if stop.wait(retry_every):
            break
        waited += retry_every
    logger.info("Wake greeting skipped (Reachy not ready after %.0fs)", waited)
    return None


def start_wake_greeting(
    service: Any,
    speak: Callable[[str], bool],
    stop_event: threading.Event,
    *,
    within: timedelta = UPCOMING_WITHIN,
) -> threading.Thread:
    """Fire-and-forget wake greeting on a daemon thread."""

    def _run() -> None:
        try:
            deliver_wake_greeting(service, speak, within=within, stop_event=stop_event)
        except Exception:  # noqa: BLE001
            logger.exception("Wake greeting failed")

    thread = threading.Thread(target=_run, name="medicine-buddy-wake", daemon=True)
    thread.start()
    return thread
