"""Listen-mode API kept for compatibility — patient VAD stretching is disabled.

Speech turn-taking stays stock Reachy Mini / Pollen conversation (no ServerVad
silence patches). Calls are no-ops so old tools or debug pages never change audio.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("medicine_buddy.listen_mode")


class ListenModeMixin:
    """Mixed into MedicineBuddyService. Patient VAD mode is retired (always off)."""

    _vad_applier: Callable[[bool], bool] | None

    def _init_listen_mode(self) -> None:
        self._vad_applier = None

    def set_vad_applier(self, applier: Callable[[bool], bool] | None) -> None:
        """Ignored — live VAD session updates are not used (they muted TTS)."""
        del applier
        self._vad_applier = None

    def is_listen_patient(self) -> bool:
        return False

    def listen_silence_ms(self) -> int:
        """Unused; stock conversation app owns turn-taking."""
        return 0

    def set_listen_patient(self, enabled: bool) -> dict[str, Any]:
        """No-op: never stretch VAD silence; keep Reachy Mini speech-to-speech defaults."""
        if enabled:
            logger.info("Patient listen requested but disabled — using stock Reachy turn-taking")
        return {
            "status": "ok",
            "enabled": False,
            "silence_ms": 0,
            "vad_applied": False,
            "timeout_s": 0,
            "disabled": True,
            "speak_hint": (
                "Listening uses normal Reachy Mini speech turn-taking "
                "(no extra wait for long stories)."
            ),
        }

    def listen_mode(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """API / tool entry — always reports disabled stock listening."""
        body = body or {}
        if "enabled" in body or str(body.get("action") or "").strip().lower() in {
            "start", "on", "begin", "stop", "off", "end",
        }:
            return self.set_listen_patient(bool(body.get("enabled"))
                                           or str(body.get("action") or "").lower()
                                           in {"start", "on", "begin"})
        return {
            "status": "ok",
            "enabled": False,
            "silence_ms": 0,
            "disabled": True,
            "normal_silence_ms": 0,
            "listen_silence_ms": 0,
            "message": "Patient VAD listening is disabled; stock Reachy Mini turn-taking only.",
        }
