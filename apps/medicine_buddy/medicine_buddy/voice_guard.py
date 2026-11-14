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
AUTH_WINDOW = 300.0  # a spoken passphrase unlocks that person for 5 minutes
TEXT_RETENTION = 300.0  # transcript text is kept in memory only, and only this long
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
    # person ids whose passphrase was said; None = not checked yet (checked lazily, see authenticated())
    authenticated: set[int] | None = None


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
    # text -> ids of people whose passphrase occurs in it (set by the service)
    identify: Callable[[str], set[int]] = lambda text: set()
    _heard: deque = field(default_factory=lambda: deque(maxlen=100))
    _pending: dict[str, Pending] = field(default_factory=dict)
    blocked: deque = field(default_factory=lambda: deque(maxlen=20))
    _cond: threading.Condition = field(default_factory=threading.Condition)

    # ---- input from the launcher (the person's real speech)
    def heard(self, text: str) -> Utterance:
        text = (text or "").strip()
        # No passphrase hashing here: this runs for EVERY sentence, and hashing is slow on the robot's
        # small CPU. It is done lazily, only when something actually needs a passphrase.
        u = Utterance(self.clock(), text, self.catalog.find_in_text(text), is_affirmative(text))
        with self._cond:
            now = self.clock()
            for old in self._heard:  # forget old wording (may contain passphrases)
                if now - old.at > TEXT_RETENTION:
                    old.text = ""
            self._heard.append(u)
            self._cond.notify_all()
        return u

    # ---- passphrases
    def _identified(self, u: Utterance) -> set[int]:
        """Whose passphrase is in this sentence (hashed at most once, then remembered)."""
        if u.authenticated is None:
            u.authenticated = self.identify(u.text) if u.text else set()
        return u.authenticated

    def authenticated(self, stop: Callable[[set[int]], bool] | None = None) -> set[int]:
        """People whose passphrase a human said within AUTH_WINDOW. Checks the newest sentences
        first and stops as soon as `stop(found)` is true, so most requests hash one sentence or none."""
        now = self.clock()
        found: set[int] = set()
        for u in reversed(list(self._heard)):
            if now - u.at > AUTH_WINDOW:
                break
            found |= self._identified(u)
            if stop is not None and stop(found):
                break
        return found

    def reidentify(self) -> None:
        """People or passphrases changed: forget earlier results so recent speech is re-checked
        (lazily) against the new data. A passphrase said while registering counts straight away,
        and one that was just replaced stops counting."""
        with self._cond:
            for u in self._heard:
                u.authenticated = None
            self._cond.notify_all()

    def wait_authenticated(self, wanted: Callable[[set[int]], bool]) -> bool:
        return self._wait_for(lambda: wanted(self.authenticated(stop=wanted)))

    def times_said(self, matcher: Callable[[str], bool], window: float = MENTION_WINDOW) -> int:
        """How many separate recent utterances satisfy `matcher` (used for 'say it twice')."""
        now = self.clock()
        return sum(1 for u in list(self._heard) if u.text and now - u.at <= window and matcher(u.text))

    def wait_times_said(self, matcher: Callable[[str], bool], count: int) -> bool:
        return self._wait_for(lambda: self.times_said(matcher) >= count)

    def refuse(self, action: str, reason: str, speak: str) -> dict[str, Any]:
        return self._refuse(action, reason, speak)

    def _wait_for(self, predicate: Callable[[], bool]) -> bool:
        """Wait (up to `grace`) until predicate() is true. The predicate runs WITHOUT the lock held:
        it may hash speech, and incoming transcripts must never wait for that."""
        deadline = time.monotonic() + self.grace
        while True:
            if predicate():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with self._cond:
                self._cond.wait(min(remaining, 0.25))

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
            return any(medicine in u.medicines and now - u.at <= MENTION_WINDOW for u in list(self._heard))
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
            return any(now - u.at <= RECENT_WINDOW for u in list(self._heard))
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
            return any(u.yes and u.at > pending.asked_at for u in list(self._heard))  # strictly after
        if not self._wait_for(said_yes):
            return self._refuse(action, "the person did not say yes",
                                "I didn't hear you say yes, so I haven't done anything.")
        with self._cond:
            pending.used = True  # one answer per question
        return None

    def unlock_status(self) -> dict[int, int]:
        """person id -> seconds their spoken passphrase still counts (debug page). Sentences are checked
        when something needs a passphrase, so this lists the ones checked so far."""
        now = self.clock()
        left: dict[int, int] = {}
        for u in list(self._heard):
            remaining = int(AUTH_WINDOW - (now - u.at))
            if remaining > 0 and u.authenticated:      # only results already computed
                for pid in u.authenticated:
                    left[pid] = max(left.get(pid, 0), remaining)
        return left

    def pending_status(self) -> list[dict[str, Any]]:
        """Open confirmation questions (for the debug page)."""
        now = self.clock()
        return [
            {"action": p.key.split(":")[0], "seconds_left": int(CONFIRM_WINDOW - (now - p.asked_at))}
            for p in self._pending.values()
            if not p.used and now - p.asked_at <= CONFIRM_WINDOW
        ]

    def seconds_since_speech(self) -> int | None:
        if not self._heard:
            return None
        return int(self.clock() - self._heard[-1].at)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "utterances_heard": len(self._heard),
            "blocked": list(self.blocked),
        }
