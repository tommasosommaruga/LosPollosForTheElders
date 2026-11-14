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
from dataclasses import dataclass

ITERATIONS = 20_000  # each spoken n-gram is hashed when checking speech, so keep this moderate
MIN_WORDS = 2
MIN_LETTERS = 10
MAX_WORDS = 8


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
    norm = normalize_phrase(phrase)
    if not norm:
        return False
    return norm in word_windows(text, len(norm.split()))


def secret_in_text(secret: StoredSecret, text: str) -> bool:
    return any(secret.matches(w) for w in word_windows(text, secret.words))
