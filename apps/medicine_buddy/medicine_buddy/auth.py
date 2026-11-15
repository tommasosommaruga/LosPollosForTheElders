"""Spoken passphrases: normalisation, hashing, validation, and finding them in speech.

Passphrases are SPOKEN, so they go through speech-to-text. We normalise them (lower case,
no punctuation, numbers as written by the recogniser) and compare word sequences.

Only a salted PBKDF2 hash is stored. Spoken phrases have little entropy, so the hash
protects against someone casually reading the database file, not against a determined
offline attack - keep the database file private.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass

ITERATIONS = 20_000  # each spoken n-gram is hashed when checking speech, so keep this moderate
MIN_WORDS = 2
MIN_LETTERS = 10
MAX_WORDS = 8
# Long stories × many people × PBKDF2 must not stall voice tools (robot ~25s HTTP timeout).
DEFAULT_SCAN_MAX_WINDOWS = 400
DEFAULT_SCAN_BUDGET_S = 3.0


def normalize_phrase(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def new_salt() -> bytes:
    return os.urandom(16)


def hash_phrase(phrase: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", normalize_phrase(phrase).encode("utf-8"), salt, iterations)


@dataclass(frozen=True)
class StoredSecret:
    salt: bytes
    digest: bytes
    words: int
    iterations: int

    def matches(self, phrase: str) -> bool:
        return hmac.compare_digest(hash_phrase(phrase, self.salt, self.iterations), self.digest)


def make_secret(phrase: str) -> StoredSecret:
    norm = normalize_phrase(phrase)
    salt = new_salt()
    return StoredSecret(salt, hash_phrase(norm, salt), len(norm.split()), ITERATIONS)


def validate_new_passphrase(phrase: str, person_name: str) -> str | None:
    """Return a reason the passphrase is unsuitable, or None if it's fine."""
    norm = normalize_phrase(phrase)
    words = norm.split()
    if len(words) < MIN_WORDS:
        return f"the passphrase needs at least {MIN_WORDS} words"
    if len(words) > MAX_WORDS:
        return f"the passphrase can have at most {MAX_WORDS} words"
    if sum(c.isalpha() for c in norm) < MIN_LETTERS:
        return "the passphrase is too short"
    name_words = set(normalize_phrase(person_name).split())
    if set(words) <= name_words | {"my", "name", "is", "i", "am", "the", "password", "passphrase"}:
        return "the passphrase must not just be the name"
    if len(set(words)) == 1:
        return "the passphrase must not repeat one word"
    return None


def word_windows(text: str, size: int) -> list[str]:
    """All contiguous `size`-word sequences in what was said."""
    words = normalize_phrase(text).split()
    return [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]


def phrase_in_text(phrase: str, text: str) -> bool:
    return phrase_occurrences(phrase, text) > 0


def phrase_occurrences(phrase: str, text: str) -> int:
    """How many non-overlapping times `phrase` appears as contiguous words in `text`.

    Saying the passphrase twice in one breath ("blue sky lake blue sky lake") counts as 2,
    which makes first-time voice setup less brittle when speech-to-text merges utterances.
    """
    target = normalize_phrase(phrase).split()
    words = normalize_phrase(text).split()
    n = len(target)
    if n == 0 or len(words) < n:
        return 0
    count = 0
    i = 0
    while i + n <= len(words):
        if words[i : i + n] == target:
            count += 1
            i += n
        else:
            i += 1
    return count


@dataclass(frozen=True)
class SecretScanResult:
    found: bool
    completed: bool  # False if a time/window budget stopped the scan early


def secret_in_text(
    secret: StoredSecret,
    text: str,
    *,
    max_windows: int | None = None,
    deadline: float | None = None,
) -> bool:
    """True if a salted passphrase hash matches any contiguous word window in ``text``.

    Optional ``max_windows`` / ``deadline`` stop early (returns False without a match).
    Default is a full scan — callers that must stay fast should pass budgets.
    """
    return scan_secret_in_text(
        secret, text, max_windows=max_windows, deadline=deadline
    ).found


def scan_secret_in_text(
    secret: StoredSecret,
    text: str,
    *,
    max_windows: int | None = None,
    deadline: float | None = None,
) -> SecretScanResult:
    windows = word_windows(text, secret.words)
    limited = max_windows is not None and len(windows) > max(0, int(max_windows))
    if limited:
        windows = windows[: max(0, int(max_windows))]
    for window in windows:
        if deadline is not None and time.monotonic() > deadline:
            return SecretScanResult(found=False, completed=False)
        if secret.matches(window):
            return SecretScanResult(found=True, completed=True)
    return SecretScanResult(found=False, completed=not limited)


def scan_any_secret_in_text(
    secrets: list[StoredSecret],
    text: str,
    *,
    max_windows_per_secret: int = DEFAULT_SCAN_MAX_WINDOWS,
    budget_s: float = DEFAULT_SCAN_BUDGET_S,
) -> SecretScanResult:
    """Scan all secrets; ``completed=False`` if the time/window budget ran out."""
    if not secrets or not (text or "").strip():
        return SecretScanResult(found=False, completed=True)
    deadline = time.monotonic() + max(0.05, float(budget_s))
    for secret in secrets:
        if time.monotonic() > deadline:
            return SecretScanResult(found=False, completed=False)
        part = scan_secret_in_text(
            secret,
            text,
            max_windows=max_windows_per_secret,
            deadline=deadline,
        )
        if part.found:
            return SecretScanResult(found=True, completed=True)
        if not part.completed:
            return SecretScanResult(found=False, completed=False)
    return SecretScanResult(found=False, completed=True)
