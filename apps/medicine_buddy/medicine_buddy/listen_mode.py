"""Patient listening mode: longer server-VAD silence while someone tells a story/memory.

Everyday chat keeps a short silence; only an explicit start (tool / API) stretches it.
Cleared by stop, successful save_memory, or a safety timeout.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("medicine_buddy.listen_mode")

# Applied to Hugging Face Realtime ServerVad.silence_duration_ms
NORMAL_SILENCE_MS = int(os.environ.get("MEDICINE_BUDDY_NORMAL_SILENCE_MS", "700"))
LISTEN_SILENCE_MS = int(os.environ.get("MEDICINE_BUDDY_LISTEN_SILENCE_MS", "3500"))
LISTEN_TIMEOUT_S = float(os.environ.get("MEDICINE_BUDDY_LISTEN_TIMEOUT_S", "600"))


class ListenModeMixin:
    """Mixed into MedicineBuddyService. Needs nothing else from the service."""

    _listen_lock: threading.Lock
    _listen_until: float
    _listen_active: bool
    # Optional: apply VAD on the live realtime session (True=patient, False=normal).
    _vad_applier: Callable[[bool], bool] | None

    def _init_listen_mode(self) -> None:
        self._listen_lock = threading.Lock()
        self._listen_until = 0.0
        self._listen_active = False
        self._vad_applier = None

    def set_vad_applier(self, applier: Callable[[bool], bool] | None) -> None:
        """Wired from main.py once the conversation handler exists."""
        self._vad_applier = applier

    def is_listen_patient(self) -> bool:
        """True while patient mode is on; auto-expires after the safety timeout."""
        with self._listen_lock:
            if not self._listen_active:
                return False
            if time.monotonic() >= self._listen_until:
                self._listen_active = False
                self._listen_until = 0.0
                logger.info("Patient listen mode expired (timeout)")
                self._apply_vad_unlocked(False)
                return False
            return True

    def listen_silence_ms(self) -> int:
        return LISTEN_SILENCE_MS if self.is_listen_patient() else NORMAL_SILENCE_MS

    def set_listen_patient(self, enabled: bool) -> dict[str, Any]:
        """Turn patient listening on or off and push VAD to the live session if possible."""
        with self._listen_lock:
            was = self._listen_active
            self._listen_active = bool(enabled)
            self._listen_until = (time.monotonic() + LISTEN_TIMEOUT_S) if enabled else 0.0
            applied = self._apply_vad_unlocked(bool(enabled))
        silence = LISTEN_SILENCE_MS if enabled else NORMAL_SILENCE_MS
        logger.info(
            "Patient listen %s (was %s, vad_applied=%s, silence_ms=%s)",
            "ON" if enabled else "OFF", was, applied, silence,
        )
        return {
            "status": "ok",
            "enabled": bool(enabled),
            "silence_ms": silence,
            "vad_applied": applied,
            "timeout_s": LISTEN_TIMEOUT_S if enabled else 0,
            "speak_hint": (
                "I'm listening — take your time. Tell me when you are finished."
                if enabled else
                "Back to normal listening."
            ),
        }

    def listen_mode(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """API / tool entry: `{enabled: bool}` or `{action: start|stop}` or empty = status."""
        body = body or {}
        if "enabled" in body:
            return self.set_listen_patient(bool(body.get("enabled")))
        action = str(body.get("action") or "").strip().lower()
        if action in {"start", "on", "begin"}:
            return self.set_listen_patient(True)
        if action in {"stop", "off", "end"}:
            return self.set_listen_patient(False)
        # status
        active = self.is_listen_patient()
        return {
            "status": "ok",
            "enabled": active,
            "silence_ms": LISTEN_SILENCE_MS if active else NORMAL_SILENCE_MS,
            "normal_silence_ms": NORMAL_SILENCE_MS,
            "listen_silence_ms": LISTEN_SILENCE_MS,
        }

    def _apply_vad_unlocked(self, patient: bool) -> bool:
        applier = self._vad_applier
        if applier is None:
            return False
        try:
            return bool(applier(patient))
        except Exception:  # noqa: BLE001 — never break the API if the session is mid-reconnect
            logger.exception("Failed to apply VAD silence for patient=%s", patient)
            return False
