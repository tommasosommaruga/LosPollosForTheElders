"""Story / memory listening session (no ServerVad patch — that muted TTS).

Tracks whose story is being told and when listening started so ``save_memory``
can pull the full spoken transcript even after a long pause. Turn-taking stays
stock Reachy Mini; the profile tells the model not to interrupt.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("medicine_buddy.listen_mode")


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class ListenModeMixin:
    """Mixed into MedicineBuddyService. Story session without live VAD changes."""

    _vad_applier: Callable[[bool], bool] | None
    _story_lock: threading.Lock
    _story_person: str | None
    _story_started_at: float | None
    _story_timeout_s: float

    def _init_listen_mode(self) -> None:
        self._vad_applier = None
        self._story_lock = threading.Lock()
        self._story_person = None
        self._story_started_at = None
        self._story_timeout_s = _env_float("MEDICINE_BUDDY_LISTEN_TIMEOUT_S", 1200.0)

    def set_vad_applier(self, applier: Callable[[bool], bool] | None) -> None:
        """Ignored — live VAD session updates muted Reachy speech previously."""
        del applier
        self._vad_applier = None

    def _expire_story_session_if_needed(self) -> None:
        with self._story_lock:
            if self._story_started_at is None:
                return
            if time.time() - self._story_started_at <= self._story_timeout_s:
                return
            logger.info("Story listen session timed out after %.0fs", self._story_timeout_s)
            self._story_person = None
            self._story_started_at = None

    def is_listen_patient(self) -> bool:
        self._expire_story_session_if_needed()
        with self._story_lock:
            return self._story_started_at is not None

    def story_session_person(self) -> str | None:
        self._expire_story_session_if_needed()
        with self._story_lock:
            return self._story_person

    def story_session_started_at(self) -> float | None:
        self._expire_story_session_if_needed()
        with self._story_lock:
            return self._story_started_at

    def listen_silence_ms(self) -> int:
        """Unused for audio; stock conversation app owns turn-taking."""
        return 0

    def set_listen_patient(
        self, enabled: bool, *, person: str | None = None
    ) -> dict[str, Any]:
        """Start/stop a story session. Does not change microphone VAD."""
        name = " ".join(str(person or "").split()).strip() or None
        with self._story_lock:
            if enabled:
                self._story_person = name
                self._story_started_at = time.time()
                logger.info("Story listen session started for %s", name or "(name later)")
                who = name or "them"
                return {
                    "status": "ok",
                    "enabled": True,
                    "person": name,
                    "silence_ms": 0,
                    "vad_applied": False,
                    "timeout_s": int(self._story_timeout_s),
                    "disabled": False,
                    "speak_hint": (
                        f"I'm ready to listen to {who}. Take your time with the story — "
                        "I will wait. When you are finished, say that you are done."
                    ),
                }
            self._story_person = None
            self._story_started_at = None
            logger.info("Story listen session stopped")
            return {
                "status": "ok",
                "enabled": False,
                "person": None,
                "silence_ms": 0,
                "vad_applied": False,
                "timeout_s": int(self._story_timeout_s),
                "disabled": False,
                "speak_hint": "Back to normal conversation.",
            }

    def listen_mode(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """API / tool entry for story listen session start/stop."""
        body = body or {}
        action = str(body.get("action") or "").strip().lower()
        person = body.get("person")
        if "enabled" in body:
            return self.set_listen_patient(bool(body.get("enabled")), person=person)
        if action in {"start", "on", "begin"}:
            return self.set_listen_patient(True, person=person)
        if action in {"stop", "off", "end"}:
            return self.set_listen_patient(False)
        self._expire_story_session_if_needed()
        with self._story_lock:
            return {
                "status": "ok",
                "enabled": self._story_started_at is not None,
                "person": self._story_person,
                "silence_ms": 0,
                "vad_applied": False,
                "disabled": False,
                "timeout_s": int(self._story_timeout_s),
                "message": (
                    "Story session tracks whose memory is being told and keeps their "
                    "spoken words for save_memory. Microphone turn-taking stays stock Reachy."
                ),
            }
