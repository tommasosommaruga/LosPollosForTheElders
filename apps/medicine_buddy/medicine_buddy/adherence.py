"""Hardware adherence stub — dry-run uses FakeAdherence."""

from __future__ import annotations

from medicine_buddy.ports import AdherenceResult, BuddyPort


class HardwareAdherence:
    def __init__(self, buddy: BuddyPort) -> None:
        self.buddy = buddy

    def watch_or_ask(self, medicine_name: str, timeout_seconds: float) -> AdherenceResult:
        raise NotImplementedError(
            "Reachy camera adherence not wired yet. Use FakeAdherence via --dry-run. "
            f"medicine={medicine_name!r} timeout={timeout_seconds}"
        )
