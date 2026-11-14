"""Hardware Reachy buddy stub — dry-run uses ConsoleBuddy."""

from __future__ import annotations


class HardwareReachyBuddy:
    def speak(self, message: str) -> None:
        raise NotImplementedError(
            f"Reachy TTS not wired yet. Message was: {message!r}. Use --dry-run."
        )

    def ask_yes_no(self, prompt: str) -> bool:
        raise NotImplementedError(
            f"Reachy STT/confirm not wired yet. Prompt was: {prompt!r}. Use --dry-run."
        )
