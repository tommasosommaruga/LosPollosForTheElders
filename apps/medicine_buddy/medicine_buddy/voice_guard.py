"""VoiceGuard: medicine actions only happen when the PERSON asked, not when text told the AI to.

The conversation LLM can be manipulated by text it reads (web pages, camera images,
its own memory). It also controls the tool arguments, so a `confirmed=true` flag set by
the LLM proves nothing. VoiceGuard instead checks the speech-to-text transcript of
what the person really said, which the launcher forwards to this server on a local
channel the LLM has no tool for.

Rules (when enabled):
- add / update / delete a schedule: the person must have named that medicine in
  something they said within the last MENTION_WINDOW seconds.
- delete / dispense: in addition, Reachy must have asked (a pending question is created)
  and the person must have said yes AFTER that question, within CONFIRM_WINDOW seconds.
  Each question can be answered once.
- mark dose taken / missed: the person must have spoken within RECENT_WINDOW seconds.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from medicine_buddy.catalog import MedicineCatalog

MENTION_WINDOW = 180.0
CONFIRM_WINDOW = 120.0
RECENT_WINDOW = 120.0
# Speech-to-text can finish slightly AFTER the AI calls the tool, so wait a moment.
TRANSCRIPT_GRACE = 5.0

# A bare "please" or "right" is NOT a yes: "please remind me about aspirin" is a request,
# and must not also count as the answer to a later confirmation question.
_YES = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|please do|yes please|go ahead|do it|correct|absolutely|"
    r"of course|affirmative|that's right|that is right|si|oui|ja)\b",
    re.IGNORECASE,
)
_NO = re.compile(r"\b(no|nope|don't|do not|not|stop|cancel|wait|never|nah)\b", re.IGNORECASE)


def is_affirmative(text: str) -> bool:
    return bool(_YES.search(text)) and not _NO.search(text)


@dataclass
class Utterance:
    at: float
    text: str
    medicines: set[str]
    yes: bool


@dataclass
class Pending:
    key: str
    asked_at: float
    used: bool = False


@dataclass
class VoiceGuard:
    catalog: MedicineCatalog
    enabled: bool = True
    clock: Callable[[], float] = time.monotonic
    grace: float = TRANSCRIPT_GRACE
    _heard: deque = field(default_factory=lambda: deque(maxlen=100))
    _pending: dict[str, Pending] = field(default_factory=dict)
    blocked: deque = field(default_factory=lambda: deque(maxlen=20))
    _cond: threading.Condition = field(default_factory=threading.Condition)

    # ---- input from the launcher (the person's real speech)
    def heard(self, text: str) -> Utterance:
        text = (text or "").strip()
        u = Utterance(self.clock(), text, self.catalog.find_in_text(text), is_affirmative(text))
        with self._cond:
            self._heard.append(u)
            self._cond.notify_all()
        return u

    def _wait_for(self, predicate: Callable[[], bool]) -> bool:
        deadline = time.monotonic() + self.grace
        with self._cond:
            while True:
                if predicate():
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)

    def _refuse(self, action: str, reason: str, speak: str) -> dict[str, Any]:
        self.blocked.append({"at": time.time(), "action": action, "reason": reason})
        return {
            "error": f"blocked by voice guard: {reason}",
            "blocked": True,
            "speak_hint": speak,
        }

    # ---- checks used by the API
    def require_mention(self, action: str, medicine: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        def mentioned() -> bool:
            now = self.clock()
            return any(medicine in u.medicines and now - u.at <= MENTION_WINDOW for u in self._heard)
        if self._wait_for(mentioned):
            return None
        return self._refuse(
            action,
            f"{medicine} was not named by the person",
            f"For safety I only change medicine reminders when you ask me yourself. "
            f"Could you tell me again what you'd like me to do with {medicine}?",
        )

    def require_recent_speech(self, action: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        def spoke() -> bool:
            now = self.clock()
            return any(now - u.at <= RECENT_WINDOW for u in self._heard)
        if self._wait_for(spoke):
            return None
        return self._refuse(action, "no recent speech from the person",
                            "Could you tell me yourself whether you took it?")

    def ask(self, key: str) -> None:
        """Reachy is about to ask the person to confirm `key`."""
        with self._cond:
            self._pending[key] = Pending(key, self.clock())

    def has_pending(self, key: str) -> bool:
        p = self._pending.get(key)
        return p is not None and not p.used and self.clock() - p.asked_at <= CONFIRM_WINDOW

    def require_yes(self, action: str, key: str) -> dict[str, Any] | None:
        """The person must have said yes after Reachy asked about exactly `key`."""
        if not self.enabled:
            return None
        pending = self._pending.get(key)
        if pending is None or pending.used or self.clock() - pending.asked_at > CONFIRM_WINDOW:
            return self._refuse(action, "no open question for this action",
                                "Let me ask you first.")
        def said_yes() -> bool:
            return any(u.yes and u.at > pending.asked_at for u in self._heard)  # strictly after
        if not self._wait_for(said_yes):
            return self._refuse(action, "the person did not say yes",
                                "I didn't hear you say yes, so I haven't done anything.")
        with self._cond:
            pending.used = True  # one answer per question
        return None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "utterances_heard": len(self._heard),
            "blocked": list(self.blocked),
        }
