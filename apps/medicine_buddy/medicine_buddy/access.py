"""People, passphrases, access control and the family memory store.

Roles
- person / caregiver labels still exist for display, but gating is easy-mode by default:
  when passphrase checks are ON, **any** registered person's passphrase authorises changes
  (not caregiver-only). When checks are OFF (default), voice changes need no passphrase.
Reading (what medicines does Mario take? tell me Grandma's polenta recipe) is always open.

Setting up
- A person gets their passphrase the first time something is saved for them: Reachy asks
  them to choose one and say it twice (self-enrolment, no admin needed).
- Passphrases are CHANGED on the Family settings page (typed, never spoken - see
  settings.py), not by talking to the robot.
- The in-app UI (/medicine) is open by default — Dashboard, Database, History, Family, Debug.

How a passphrase is checked (only when require_passphrases is ON)
- With the voice guard ON: a human must have SAID a registered passphrase in the last 5 minutes.
- With the voice guard OFF: the tool argument `passphrase` is checked against the hash.
Passphrases are never stored in clear, never returned, and never stored in memories.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Callable

from medicine_buddy.auth import (
    make_secret,
    normalize_phrase,
    secret_in_text,
    validate_new_passphrase,
)
from medicine_buddy.db import MEMORY_KINDS, ROLES, Memory, Person
from medicine_buddy.names import name_key, strip_titles

MAX_MEMORY_CHARS = 6000
MAX_RECALL = 5


class PeopleAccessMixin:
    """Mixed into MedicineBuddyService (needs self.db, self.guard, self._lock)."""

    # ------------------------------------------------------------ identity
    def _identify_speakers(self, text: str) -> set[int]:
        """Ids of people whose passphrase occurs in this piece of real speech."""
        return {p.id for p in self.db.list_people() if secret_in_text(p.secret, text)}

    def _resolve_person(
        self, spoken: Any, *, allow_default: bool = True, for_change: bool = False, strict: bool = False
    ) -> tuple[Person | None, dict | None]:
        """Find a registered person by name.

        for_change=True (saving / changing someone's data): no fuzzy guessing - "Maria"
        must never land on Mario's timetable - and an unknown name asks for first-time
        passphrase setup. strict=True (dispensing): no fuzzy guessing either, but no setup
        prompt. For questions, close spellings are accepted.
        Returns (None, None) when no name is given and no family member exists (legacy)."""
        people = self.db.list_people()
        text = " ".join(str(spoken or "").split())
        if not text:
            patients = [p for p in people if not p.is_caregiver]
            if not patients:
                return None, None  # no family members yet: unassigned, as before
            if allow_default and len(patients) == 1:
                return patients[0], None
            names = ", ".join(p.name for p in people)
            return None, {"error": "person is required",
                          "speak_hint": f"Who is this for? I know: {names}."}
        exact = self.db.get_person_by_name(text)
        if exact:
            return exact, None
        key = name_key(text)
        core = strip_titles(key)                      # "grandpa Mario" / "nonna Rosa" -> "mario" / "rosa"
        if core and core != key:
            titled = self.db.get_person_by_name(core)
            if titled:
                return titled, None
        # NOTE: a longer name such as "Mario Bianchi" is NOT mapped onto the registered "Mario":
        # a different person with the same first name must never act on someone else's data.
        if not for_change:
            if not strict:
                by_key = {name_key(p.name): p for p in people}
                close = difflib.get_close_matches(core or key, list(by_key), n=2, cutoff=0.75)
                if len(close) == 1:
                    return by_key[close[0]], None
            names = ", ".join(p.name for p in people) or "nobody yet"
            return None, {"error": f"unknown person: {text!r}", "known_people": [p.name for p in people],
                          "speak_hint": f"I don't know anyone called {text}. I know: {names}."}
        name = text.title() if text.islower() else text
        return None, self._needs_passphrase_setup(name)

    @staticmethod
    def _needs_passphrase_setup(name: str, *, pending: dict[str, Any] | None = None) -> dict[str, Any]:
        """Clear first-time setup instructions for Reachy to read aloud and follow."""
        out: dict[str, Any] = {
            "error": f"{name} has no passphrase yet",
            "status": "needs_passphrase_setup",
            "person": name,
            "next_step": "ask_passphrase_twice_then_retry",
            "steps": [
                f"Ask {name} to invent a passphrase of three or four easy words.",
                f"Ask {name} to say those words out loud.",
                "Ask them to say the same words one more time.",
                "Call schedule_medicine again with new_passphrase set to those words "
                "(or call register_person, then retry the medicine request).",
            ],
            "speak_hint": (
                f"{name} is not set up yet. To keep {name}'s medicines safe I need a passphrase. "
                f"{name}, please choose three or four easy words, say them now, then say them "
                "one more time. When you've said them twice, I'll finish setting things up."
            ),
        }
        if pending:
            out["pending"] = pending
        return out

    def _require_auth(
        self, action: str, body: dict[str, Any], allowed: Callable[[Person], bool], who: str
    ) -> dict[str, Any] | None:
        """None if someone allowed has proven themselves, else a refusal Reachy can read."""
        # Master switch (Family settings / MEDICINE_BUDDY_REQUIRE_PASSPHRASES): off = no checks.
        if not self.require_passphrases():
            return None
        people = self.db.list_people()
        if not people:
            return None  # open mode: nobody registered yet
        candidates = [p for p in people if allowed(p)]
        speak = f"For that I need {who} passphrase. Please say it."
        if not candidates:
            return self.guard.refuse(action, f"nobody can authorise this ({who})",
                                     "Nobody is registered yet. Add someone on the Family settings page, "
                                     "or set up a passphrase by voice the first time.")
        if self.guard.enabled:
            ok = self.guard.wait_authenticated(lambda ids: any(p.id in ids for p in candidates))
            if ok:
                return None
            return self.guard.refuse(action, f"{who} passphrase not heard", speak)
        phrase = str(body.get("passphrase") or "")
        if phrase and any(p.secret.matches(phrase) for p in candidates):
            return None
        return self.guard.refuse(action, f"{who} passphrase missing or wrong", speak)

    def _require_caregiver(self, action: str, body: dict[str, Any]) -> dict[str, Any] | None:
        """Legacy name: any registered person may authorise (caregiver-only gating disabled)."""
        return self._require_auth(action, body, lambda p: True, "a family member's")

    def _require_owner(self, action: str, body: dict[str, Any], person: Person | None) -> dict[str, Any] | None:
        """Any registered person's passphrase authorises timetable / memory changes (easy mode)."""
        del person
        return self._require_auth(action, body, lambda p: True, "a family member's")

    # ------------------------------------------------------------ people
    def register_person(self, body: dict[str, Any], *, typed: bool = False) -> dict[str, Any]:
        """First-time passphrase setup.

        - role 'person' or 'caregiver': self-enrolment; caregiver is a label only (no extra gates).
        Spoken (typed=False): the new passphrase must be said twice (heard twice with the guard).
        Typed on the settings page (typed=True): it must be typed twice."""
        name = " ".join(str(body.get("name") or "").split())
        role = str(body.get("role") or "person").strip().lower()
        phrase = str(body.get("passphrase") or "")
        repeat = str(body.get("passphrase_repeat") or "")
        if not name:
            return {"error": "name is required", "speak_hint": "What is the person's name?"}
        if not name_key(name) or len(name) > 60:
            return {"error": "the name must contain letters and be at most 60 characters",
                    "speak_hint": "Please tell me the person's name again, in plain words."}
        if role not in ROLES:
            return {"error": f"role must be one of {ROLES}"}
        # Caregiver-only approval for extra caregivers is disabled (ease of use).
        problem = validate_new_passphrase(phrase, name)
        if problem:
            return {"error": problem, "speak_hint": f"Please choose another passphrase: {problem}."}

        # The passphrase must be given TWICE so a typo or mishearing can't lock someone out.
        # With the voice guard ON, only REAL speech counts (tool args alone never unlock).
        # Saying it twice in one sentence also counts — STT often merges short repeats.
        if typed:
            if normalize_phrase(phrase) != normalize_phrase(repeat):
                return {"error": "the two passphrases are different",
                        "speak_hint": "The two typed passphrases didn't match. Please type the same words twice."}
        elif self.guard.enabled:
            if not self.guard.wait_phrase_said(phrase, 2):
                return {
                    **self.guard.refuse(
                        "register person",
                        "new passphrase not heard twice",
                        f"I still need to hear {name}'s new passphrase two times in their own voice. "
                        "Please say the three or four words now, then say them again.",
                    ),
                    "status": "needs_passphrase_repeat",
                    "next_step": "ask_them_to_say_passphrase_twice",
                    "person": name,
                }
        elif normalize_phrase(phrase) != normalize_phrase(repeat or ""):
            return {"error": "the two passphrases are different",
                    "speak_hint": "The two passphrases didn't match. Let's try again: say it twice."}

        with self._lock:
            existing = self.db.get_person_by_name(name)
            if existing:
                return {"error": f"{name} is already registered",
                        "speak_hint": (f"{existing.name} is already registered, so I won't register that name "
                                       "again. If you are a different person with the same name, please use a "
                                       "name that tells you apart, like your full name. To change a passphrase, "
                                       "use the Family settings page.")}
            if any(p.secret.matches(phrase) for p in self.db.list_people()):
                return {"error": "passphrase already in use",
                        "speak_hint": "That passphrase is already used by someone else. Please choose another."}
            try:
                person = self.db.add_person(name, role, make_secret(phrase))
            except ValueError as exc:      # lost a race with another registration of the same name
                return {"error": str(exc), "speak_hint": "That name was just registered by someone else."}
        self.guard.reidentify()
        return {
            "status": "ok",
            "person": {"name": person.name, "role": person.role},
            "speak_hint": "Done. {} is registered as {}. I will never repeat the passphrase, so please "
                          "remember it.".format(person.name, "the caregiver" if person.is_caregiver else "a family member"),
        }

    def list_people_info(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        people = self.db.list_people()
        out = []
        for p in people:
            mems = self.db.list_memories(p.id)
            meds = self.db.list_medicines(p.id, all_people=False)
            out.append({"name": p.name, "role": p.role, "memories": len(mems), "medicines": len(meds)})
        if not out:
            hint = "Nobody is registered yet. Each person sets up a passphrase the first time I save something for them."
        else:
            parts = []
            for p in out:
                detail = "caregiver" if p["role"] == "caregiver" else "{} memories".format(p["memories"])
                parts.append("{} ({})".format(p["name"], detail))
            hint = "I know: " + "; ".join(parts) + "."
        return {"people": out, "speak_hint": hint}

    # ------------------------------------------------------------ memories
    def save_memory(self, body: dict[str, Any]) -> dict[str, Any]:
        """Store a story / recipe / skill / tradition for a person (their passphrase or the caregiver's)."""
        person, err = self._resolve_person(body.get("person"), allow_default=False, for_change=True)
        if err:
            return err
        if person is None:
            return {"error": "register people first",
                    "speak_hint": "To keep memories for someone, the caregiver first needs to register them."}
        text = str(body.get("text") or "").strip()
        if not text:
            return {"error": "text is required", "speak_hint": "What should I save?"}
        if len(text) > MAX_MEMORY_CHARS:
            return {"error": f"too long (max {MAX_MEMORY_CHARS} characters)",
                    "speak_hint": "That's a lot to keep in one go. Could we save it in smaller parts?"}
        if any(secret_in_text(p.secret, text) for p in self.db.list_people()):
            return {"error": "memory contains a passphrase",
                    "speak_hint": "That contains a passphrase, so I won't save it."}
        blocked = self._require_owner("save memory", body, person)
        if blocked:
            return blocked
        kind = str(body.get("kind") or "story").strip().lower()
        with self._lock:
            mem = self.db.add_memory(person.id, kind, str(body.get("title") or ""), text)
            stored = self.db.get_memory(mem.id)
        if stored is None or stored.text != text.strip():
            return {"error": "memory could not be verified", "speak_hint": "Something went wrong saving that."}
        # Story/memory dictation is done — restore snappy turn-taking.
        if hasattr(self, "set_listen_patient") and self.is_listen_patient():
            self.set_listen_patient(False)
        return {
            "status": "ok",
            "verified": True,
            "memory": {"id": stored.id, "person": person.name, "kind": stored.kind, "title": stored.title},
            "speak_hint": f"Saved in {person.name}'s memories: the {stored.kind} \"{stored.title}\".",
        }

    def recall_memories(self, body: dict[str, Any]) -> dict[str, Any]:
        """Find a person's memories by name, optionally by kind and topic words. Open to family."""
        person, err = self._resolve_person(body.get("person"), allow_default=False)
        if err:
            return err
        if person is None:
            return {"error": "nobody registered yet", "speak_hint": "I don't have anyone's memories yet."}
        kind = str(body.get("kind") or "").strip().lower() or None
        if kind and kind not in MEMORY_KINDS:
            kind = None
        mems = self.db.list_memories(person.id, kind)
        topic = normalize_phrase(str(body.get("topic") or ""))
        if topic:
            words = [w for w in topic.split() if len(w) > 2]

            def score(m: Memory) -> int:
                hay = normalize_phrase(f"{m.title} {m.kind} {m.text}")
                return sum(hay.count(w) for w in words)

            scored = sorted(((score(m), m) for m in mems), key=lambda x: -x[0])
            mems = [m for s, m in scored if s > 0]
        found = mems[:MAX_RECALL]
        meds = []
        for m in self.db.list_medicines(person.id, all_people=False):
            sch = self.db.list_schedules_for_medicine(m.id)
            if sch:
                meds.append({"name": m.name, "times": [s.time_hhmm for s in sch]})
        if found:
            what = ", ".join(f"the {m.kind} \"{m.title}\"" for m in found)
            hint = f"I have {len(mems)} matching memories from {person.name}, for example {what}."
        elif topic or kind:
            hint = f"I couldn't find anything like that in {person.name}'s memories."
        else:
            hint = f"{person.name} hasn't saved any memories yet."
        return {
            "person": {"name": person.name, "role": person.role},
            "total_matching": len(mems),
            "memories": [
                {"id": m.id, "kind": m.kind, "title": m.title, "text": m.text, "saved": m.created_at[:10]}
                for m in found
            ],
            "medicines": meds,
            "speak_hint": hint,
            "note": "These were told by the family. Retell them warmly and faithfully; don't invent details.",
        }

    def forget_memory(self, body: dict[str, Any]) -> dict[str, Any]:
        """Delete one memory (its owner or the caregiver). Needs confirmed=true after asking."""
        person, err = self._resolve_person(body.get("person"), allow_default=False, for_change=True)
        if err:
            return err
        if person is None:
            return {"error": "nobody registered yet"}
        try:
            memory_id = int(body.get("memory_id"))
        except (TypeError, ValueError):
            return {"error": "memory_id is required (use recall_person_memories to find it)"}
        mem = self.db.get_memory(memory_id)
        if mem is None or mem.person_id != person.id:
            return {"error": "no such memory for this person"}
        blocked = self._require_owner("forget memory", body, person)
        if blocked:
            return blocked
        key = f"forget:{mem.id}"
        confirmed = bool(body.get("confirmed"))
        if confirmed and self.guard.enabled and not self.guard.has_pending(key):
            confirmed = False
        if confirmed:
            blocked = self.guard.require_yes("forget memory", key)
            if blocked:
                return blocked
        if not confirmed:
            self.guard.ask(key)
            return {"status": "needs_confirm",
                    "speak_hint": f"Should I really delete the {mem.kind} \"{mem.title}\" from {person.name}'s memories?"}
        self.db.delete_memory(mem.id)
        return {"status": "ok", "speak_hint": f"Deleted \"{mem.title}\" from {person.name}'s memories."}
