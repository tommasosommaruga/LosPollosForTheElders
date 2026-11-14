"""Buddy that does not speak — Reachy conversation app owns TTS/STT."""

from __future__ import annotations


class SilentBuddy:
    """Records prompts; answers yes/no from pre-supplied API flags."""

    def __init__(
        self,
        *,
        confirm: bool = False,
        adherence_taken: bool | None = None,
    ) -> None:
        self.confirm = confirm
        self.adherence_taken = adherence_taken
        self.messages: list[str] = []
        self._yes_no_calls = 0

    def speak(self, message: str) -> None:
        self.messages.append(message)

    def ask_yes_no(self, prompt: str) -> bool:
        self.messages.append(prompt)
        self._yes_no_calls += 1
        # First yes/no in a dose run is confirm-before-move.
        if self._yes_no_calls == 1:
            return self.confirm
        # Later prompts (e.g. adherence voice fallback).
        if self.adherence_taken is None:
            return False
        return self.adherence_taken
