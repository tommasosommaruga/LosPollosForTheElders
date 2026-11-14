"""Family settings: change passphrases and add people by TYPING on a local page, not by voice.

Why a page and not the robot: a passphrase change spoken aloud would be heard by everyone
in the room and by the conversation AI. Typed on http://127.0.0.1:8787/family, the new
passphrase never passes through the AI at all.

Protection of the page's endpoints (/settings/*):
- only from this computer (loopback address),
- only from the page itself: a random per-start token is embedded in the page and must be
  sent back in the X-Family-Token header (blocks other websites open in the same browser),
- the server also rejects any request whose Origin header is not this server (api.py),
- every change needs the person's CURRENT passphrase or a caregiver's passphrase.
"""

from __future__ import annotations

import hmac
import secrets
from typing import Any

from medicine_buddy.auth import make_secret, normalize_phrase, validate_new_passphrase

# Easy to say, easy for speech recognition, no sound-alikes (sea/see, two/to, knight/night).
_ADJECTIVES = """
amber brave bright calm cheerful clever cosy crisp curly dusty early fancy fluffy friendly
gentle giant golden happy honest humble jolly lively lucky mellow merry mighty misty noble
orange patient playful polite proud purple quiet rapid rosy rusty silver sleepy snowy sparkly
spicy steady sunny swift tidy tiny velvet violet warm wild windy wise woolly yellow
""".split()
_NOUNS = """
anchor apple badger banjo basket beacon biscuit blossom bucket butter cabin camel candle
canyon castle cello cherry chimney cinnamon clover cobbler comet compass cookie copper cricket
dolphin donkey dragon falcon feather fiddle forest fountain garden ginger giraffe guitar hammock
harbour hazel helmet honey island jasmine kettle kitten ladder lantern lemon lighthouse lobster
mango meadow melon mitten muffin nutmeg olive orchard otter paddle panda parrot pebble pepper
piano pickle pillow pirate pocket pony potato puffin pumpkin rabbit radish raven ribbon river
rocket saddle salmon sandal scooter shovel spider spinach squirrel teapot thimble tiger tomato
tractor trumpet tulip turtle umbrella valley violin wagon walnut whistle window zebra
""".split()


def suggest_passphrase() -> str:
    """Three random easy words, e.g. 'misty lantern pumpkin' (~19 bits, chosen by a CSPRNG)."""
    while True:
        words = [secrets.choice(_ADJECTIVES), secrets.choice(_NOUNS), secrets.choice(_NOUNS)]
        if len(set(words)) == 3:
            return " ".join(words)


class SettingsMixin:
    """Mixed into MedicineBuddyService (needs self.db, self.guard, self._lock, register_person)."""

    settings_token: str

    def _init_settings(self) -> None:
        self.settings_token = secrets.token_urlsafe(24)

    def check_settings_token(self, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(str(token), self.settings_token)

    def settings_suggest(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"passphrase": suggest_passphrase()}

    def settings_people(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"people": [
            {"name": p.name, "role": p.role,
             "medicines": len(self.db.list_medicines(p.id, all_people=False)),
             "memories": len(self.db.list_memories(p.id))}
            for p in self.db.list_people()
        ]}

    def settings_register(self, body: dict[str, Any]) -> dict[str, Any]:
        """Add a person or caregiver from the page (typed twice instead of said twice)."""
        r = self.register_person(body, typed=True)
        r.pop("speak_hint", None)
        return r

    def settings_change_passphrase(self, body: dict[str, Any]) -> dict[str, Any]:
        """body: name, current (the person's current passphrase OR a caregiver's), new, new_repeat."""
        name = " ".join(str(body.get("name") or "").split())
        current = str(body.get("current") or "")
        new = str(body.get("new") or "")
        repeat = str(body.get("new_repeat") or "")
        with self._lock:
            person = self.db.get_person_by_name(name)
            if person is None:
                return {"error": f"{name or 'That person'} is not registered"}
            people = self.db.list_people()
            authorised = person.secret.matches(current) or any(
                p.secret.matches(current) for p in people if p.is_caregiver
            )
            if not authorised:
                self.guard.refuse("change passphrase", f"wrong current passphrase for {person.name}",
                                  "")  # logged on the Security panel
                return {"error": f"That is not {person.name}'s current passphrase or a caregiver's passphrase"}
            if normalize_phrase(new) != normalize_phrase(repeat):
                return {"error": "The two new passphrases are different"}
            problem = validate_new_passphrase(new, person.name)
            if problem:
                return {"error": problem[0].upper() + problem[1:]}
            if any(p.secret.matches(new) for p in people if p.id != person.id):
                return {"error": "That passphrase is already used by someone else"}
            if person.secret.matches(new):
                return {"error": "The new passphrase is the same as the old one"}
            self.db.update_person_secret(person.id, make_secret(new))
            updated = self.db.get_person(person.id)
        if updated is None or not updated.secret.matches(new):
            return {"error": "The change could not be verified"}
        self.guard.reidentify()
        return {"status": "ok", "message": f"{person.name}'s passphrase was changed and checked."}
