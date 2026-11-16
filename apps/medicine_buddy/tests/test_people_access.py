"""People, passphrases, access control and family memories."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.auth import (
    hash_phrase,
    make_secret,
    normalize_phrase,
    phrase_in_text,
    phrase_occurrences,
    secret_in_text,
    validate_new_passphrase,
)
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.voice_guard import AUTH_WINDOW, VoiceGuard

CARE = "purple mountain sunrise"
MARIO = "old fishing boat"
ROSA = "red garden roses"


def _lock_down(svc: MedicineBuddyService, *, voice_guard: bool = False) -> None:
    """Turn on the master passphrase switch (default is off for demos)."""
    svc.settings_require_passphrases({"enabled": True})
    svc.guard.enabled = voice_guard


# ---------------------------------------------------------------- passphrase basics

def test_normalize_and_find_in_speech() -> None:
    assert normalize_phrase("  Purple, Mountain-SUNRISE! ") == "purple mountain sunrise"
    assert phrase_in_text(CARE, "ok my passphrase is purple mountain sunrise, thanks")
    assert not phrase_in_text(CARE, "purple mountain")
    assert phrase_occurrences(CARE, f"{CARE} then {CARE}") == 2
    assert phrase_occurrences(CARE, CARE) == 1
    s = make_secret(CARE)
    assert secret_in_text(s, "it's Purple mountain sunrise.")
    assert not secret_in_text(s, "purple sunrise mountain")
    assert s.matches("PURPLE mountain sunrise") and not s.matches("purple mountain")


def test_hash_is_salted() -> None:
    a, b = make_secret(CARE), make_secret(CARE)
    assert a.salt != b.salt and a.digest != b.digest
    assert hash_phrase(CARE, a.salt) == a.digest


@pytest.mark.parametrize("phrase,ok", [
    ("purple mountain sunrise", True),
    ("one", False),                 # one word
    ("a b", False),                 # too short
    ("mario mario", False),         # repeated word / just the name
    ("my name is mario", False),    # just the name
    ("w " * 9, False),              # too many words
])
def test_passphrase_rules(phrase: str, ok: bool) -> None:
    assert (validate_new_passphrase(phrase, "Mario") is None) is ok


# ---------------------------------------------------------------- guard OFF: passphrase as argument

@pytest.fixture
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Access-control tests need strict schedule gates (demo_smooth defaults ON).
    monkeypatch.setenv("MEDICINE_BUDDY_DEMO_SMOOTH", "0")
    s = MedicineBuddyService(
        tmp_path / "p.db",
        AppConfig(dry_run=True, tray_labels=["ASPIRIN"]),
        MedicineCatalog({"Aspirin": [], "Vitamin D": [], "Metformin": []}),
    )
    _lock_down(s, voice_guard=False)  # passphrase-in-body checks (guard speech layer off)
    yield s
    s.close()


def _family(svc: MedicineBuddyService) -> None:
    assert svc.register_person({"name": "Anna", "role": "caregiver", "passphrase": CARE,
                                "passphrase_repeat": CARE})["status"] == "ok"
    for name, phrase in (("Mario", MARIO), ("Rosa", ROSA)):
        r = svc.register_person({"name": name, "role": "person", "passphrase": phrase,
                                 "passphrase_repeat": phrase, "caregiver_passphrase": CARE})
        assert r["status"] == "ok", r


def test_family_member_can_set_up_own_passphrase_without_caregiver(svc: MedicineBuddyService) -> None:
    r = svc.register_person({"name": "Mario", "role": "person", "passphrase": MARIO, "passphrase_repeat": MARIO})
    assert r["status"] == "ok" and r["person"]["role"] == "person"


def test_unknown_person_is_asked_to_set_up_a_passphrase_first(svc: MedicineBuddyService) -> None:
    _family(svc)
    r = svc.add_schedule({"person": "luca", "name": "Aspirin", "weekdays": "daily", "time": "09:00", "passphrase": CARE})
    assert r["status"] == "needs_passphrase_setup" and r["person"] == "Luca"
    assert "steps" in r and r.get("pending", {}).get("action") == "schedule_medicine"
    assert "passphrase" in r["speak_hint"].lower()
    # Caregiver passphrase must NOT enrol Luca by accident
    assert svc.db.get_person_by_name("Luca") is None and svc.db.list_active_schedules() == []
    # One-shot setup: new_passphrase + medicine details
    r = svc.add_schedule({
        "person": "Luca", "name": "Aspirin", "weekdays": "daily", "time": "09:00",
        "new_passphrase": "green kite river", "passphrase_repeat": "green kite river",
    })
    assert r["verified"] and svc.db.get_person_by_name("Luca") is not None


def test_similar_names_never_mix_up_timetables(svc: MedicineBuddyService) -> None:
    _family(svc)  # Mario exists, Maria does not
    r = svc.add_schedule({"person": "Maria", "name": "Aspirin", "weekdays": "daily", "time": "09:00", "passphrase": CARE})
    assert r["status"] == "needs_passphrase_setup"           # not silently Mario
    assert svc.list_medicines({"person": "Mario"})["medicines"] == []
    assert "error" in svc.run_dose({"person": "Maria", "medicine_name": "Aspirin", "confirmed": True})
    assert svc.recall_memories({"person": "Mari"})["person"]["name"] == "Mario"  # questions stay forgiving


def test_passphrase_must_be_repeated_identically(svc: MedicineBuddyService) -> None:
    r = svc.register_person({"name": "Anna", "role": "caregiver", "passphrase": CARE,
                             "passphrase_repeat": "purple mountain sunset"})
    assert "didn't match" in r["speak_hint"]
    assert svc.db.list_people() == []


def test_passphrase_never_stored_or_returned(svc: MedicineBuddyService, tmp_path: Path) -> None:
    r = svc.register_person({"name": "Anna", "role": "caregiver", "passphrase": CARE, "passphrase_repeat": CARE})
    assert CARE not in str(r) and "purple" not in str(svc.list_people_info())
    svc.db.close()
    raw = (tmp_path / "p.db").read_bytes()
    assert b"purple" not in raw and b"sunrise" not in raw
    svc.db = Database(tmp_path / "p.db")


def test_becoming_caregiver_no_longer_needs_existing_caregiver(svc: MedicineBuddyService) -> None:
    """Caregiver is a label only — a second caregiver can enrol without the first's passphrase."""
    svc.register_person({"name": "Anna", "role": "caregiver", "passphrase": CARE, "passphrase_repeat": CARE})
    eve = {"name": "Eve", "role": "caregiver", "passphrase": "quiet sneaky fox", "passphrase_repeat": "quiet sneaky fox"}
    assert svc.register_person(eve)["status"] == "ok"
    assert {p.name for p in svc.db.list_people() if p.is_caregiver} == {"Anna", "Eve"}


def test_duplicate_name_or_passphrase_rejected(svc: MedicineBuddyService) -> None:
    _family(svc)
    r = svc.register_person({"name": "mario", "role": "person", "passphrase": "blue sky today",
                             "passphrase_repeat": "blue sky today", "caregiver_passphrase": CARE})
    assert "already registered" in r["error"]
    r = svc.register_person({"name": "Luca", "role": "person", "passphrase": MARIO,
                             "passphrase_repeat": MARIO, "caregiver_passphrase": CARE})
    assert "already used" in r["speak_hint"]


def test_any_family_passphrase_can_change_timetable(svc: MedicineBuddyService) -> None:
    """Easy mode: when passphrases are ON, any registered passphrase authorises (not owner-only)."""
    _family(svc)
    add = {"person": "Mario", "name": "Aspirin", "weekdays": "daily", "time": "09:00"}
    assert svc.add_schedule(add)["blocked"] is True                            # no passphrase
    assert svc.add_schedule({**add, "passphrase": "totally wrong phrase here"})["blocked"] is True
    r = svc.add_schedule({**add, "passphrase": ROSA})                          # another family member OK
    assert r["verified"] and "Aspirin" in r["speak_hint"] and "Mario" in r["speak_hint"]

    upd = {"person": "Mario", "name": "Aspirin", "new_time": "10:00"}
    assert svc.update_schedule({**upd, "passphrase": ROSA})["verified"]
    dele = {"person": "Mario", "name": "Aspirin", "confirmed": True}
    assert svc.delete_schedule(dele)["blocked"] is True
    assert svc.delete_schedule({**dele, "passphrase": MARIO})["verified"]


def test_medicines_are_kept_per_person(svc: MedicineBuddyService) -> None:
    _family(svc)
    svc.add_schedule({"person": "Mario", "name": "Aspirin", "weekdays": "daily", "time": "09:00", "passphrase": CARE})
    svc.add_schedule({"person": "Rosa", "name": "Aspirin", "weekdays": "daily", "time": "21:00", "passphrase": CARE})
    svc.add_schedule({"person": "Rosa", "name": "Vitamin D", "weekdays": "weekdays", "time": "13:00", "passphrase": CARE})
    assert svc.list_medicines({"person": "Mario"})["speak_hint"] == "Mario's medicines: Aspirin every day at 9 AM."
    rosa = svc.list_medicines({"person": "rosa"})["speak_hint"]
    assert "Aspirin every day at 9 PM" in rosa and "Vitamin D on weekdays at 1 PM" in rosa and "9 AM" not in rosa
    # deleting Rosa's aspirin leaves Mario's alone
    svc.delete_schedule({"person": "Rosa", "name": "Aspirin", "confirmed": True, "passphrase": CARE})
    assert "Aspirin" in svc.list_medicines({"person": "Mario"})["speak_hint"]
    assert "Aspirin" not in svc.list_medicines({"person": "Rosa"})["speak_hint"]


def test_person_required_when_several(svc: MedicineBuddyService) -> None:
    _family(svc)
    r = svc.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00", "passphrase": CARE})
    assert "Who is this for?" in r["speak_hint"] and "Mario" in r["speak_hint"]


def test_dispense_for_a_person(svc: MedicineBuddyService) -> None:
    _family(svc)
    svc.add_schedule({"person": "Mario", "name": "Aspirin", "weekdays": "daily", "time": "09:00", "passphrase": CARE})
    assert svc.run_dose({"person": "Mario", "medicine_name": "Aspirin", "confirmed": True})["final_state"] == "Placed"
    assert "error" in svc.run_dose({"person": "Rosa", "medicine_name": "Aspirin", "confirmed": True})


# ---------------------------------------------------------------- family memories

POLENTA = ("Boil two litres of salted water, pour in 500 grams of coarse cornmeal slowly while stirring, "
           "then stir for 45 minutes with a wooden spoon, always in the same direction like my mother did.")


def test_save_and_recall_memories(svc: MedicineBuddyService) -> None:
    _family(svc)
    m = {"person": "Rosa", "kind": "recipe", "title": "Nonna's polenta", "text": POLENTA}
    assert svc.save_memory(m)["blocked"] is True                       # nobody proved themselves
    # Easy mode: any registered passphrase can save for anyone
    r = svc.save_memory({**m, "passphrase": MARIO})
    assert r["verified"] and "rosa's memories" in r["speak_hint"].lower()
    assert r["memory"]["text"] == POLENTA
    assert r["chars_saved"] == len(POLENTA)
    svc.save_memory({"person": "Mario", "kind": "story", "title": "The winter of 1956",
                     "text": "The lake froze and we walked across it to the next village.", "passphrase": CARE})

    # the grandson asks by name, misheard / with a family word in front
    got = svc.recall_memories({"person": "nonna rosa", "topic": "polenta"})
    assert got["person"]["name"] == "Rosa" and got["memories"][0]["text"] == POLENTA
    assert (
        "word for word" in got["speak_hint"].lower()
        or "verbatim" in got["speak_hint"].lower()
        or "exactly" in got["speak_hint"].lower()
    )
    assert svc.recall_memories({"person": "Mario", "kind": "recipe"})["total_matching"] == 0
    assert svc.recall_memories({"person": "Mario"})["memories"][0]["title"] == "The winter of 1956"
    assert "don't know anyone called" in svc.recall_memories({"person": "Giuseppe"})["speak_hint"]


def test_save_memory_prefers_full_spoken_transcript_over_short_summary(
    svc: MedicineBuddyService,
) -> None:
    """Model paraphrase must not wipe the long story the person actually said."""
    _family(svc)
    full = (
        "What an amazing hackathon I've been doing. I've been vibe coding for the whole time. "
        "I really like this, and I really like the vibe, I like joking, I like making code and "
        "pushing code. It's really nice, and I like to move the arm randomly and do a lot of "
        "things like that, and also Reachy Mini is my favorite of all time, it's literally the "
        "beast of this hackathon. Reachy is the companion I always wanted and I love him."
    )
    svc.record_transcript("user", full)
    svc.record_transcript("user", "Finish here the story and save it, please.")
    short = "He had an amazing time at the hackathon vibe coding with Reachy Mini!"
    saved = svc.save_memory({
        "person": "Mario",
        "kind": "story",
        "title": "Vibe coding hackathon",
        "text": short,
        "passphrase": MARIO,
    })
    assert saved["verified"] is True
    assert saved["source"] == "transcript"
    assert "vibe coding" in saved["memory"]["text"].lower()
    assert "beast of this hackathon" in saved["memory"]["text"].lower()
    assert "love him" in saved["memory"]["text"].lower()
    assert len(saved["memory"]["text"]) > len(short) * 2

    told = svc.recall_memories({"person": "Mario", "topic": "hackathon"})
    assert "beast of this hackathon" in told["memories"][0]["text"].lower()
    hint = told["speak_hint"].lower()
    assert "word for word" in hint or "verbatim" in hint or "exactly" in hint


def test_save_memory_stores_tone_note_and_confirms_saving(svc: MedicineBuddyService) -> None:
    _family(svc)
    r = svc.save_memory({
        "person": "Mario",
        "kind": "story",
        "title": "Uncle Marco",
        "text": "Uncle Marco picked me up every time I fell.",
        "tone_note": "warm, laughing",
        "passphrase": MARIO,
    })
    assert r["verified"] is True
    assert r["memory"]["tone_note"] == "warm, laughing"
    assert "saving" in r["speak_hint"].lower() or "saved" in r["speak_hint"].lower()
    got = svc.recall_memories({"person": "Mario", "topic": "Marco"})
    assert got["memories"][0]["tone_note"] == "warm, laughing"
    assert "word for word" in got["speak_hint"].lower() or "exactly" in got["speak_hint"].lower()
    assert "warm" in got["speak_hint"].lower() or "tone" in got["speak_hint"].lower()


def test_save_memory_fast_when_passphrases_off(tmp_path: Path) -> None:
    """Demo mode must not PBKDF2-scan a long story (that caused robot tool timeouts)."""
    import time

    s = MedicineBuddyService(
        tmp_path / "fast.db",
        AppConfig(dry_run=True, tray_labels=["ASPIRIN"]),
        MedicineCatalog({"Aspirin": []}),
    )
    try:
        assert s.require_passphrases() is False
        # Demo auto-registers people without spoken passphrase setup.
        long_story = ("Once upon a time in the village " * 80).strip()
        assert len(long_story.split()) > 300
        t0 = time.perf_counter()
        r = s.save_memory({
            "person": "Tommaso",
            "kind": "story",
            "title": "Village summers",
            "text": long_story,
            "tone_note": "nostalgic, soft",
        })
        elapsed = time.perf_counter() - t0
        assert r.get("verified") is True, r
        assert r["memory"]["text"] == long_story
        assert r["memory"]["tone_note"] == "nostalgic, soft"
        assert elapsed < 2.0, f"save took {elapsed:.2f}s — passphrase scan still too slow"
    finally:
        s.close()


def test_memory_cannot_contain_a_passphrase(svc: MedicineBuddyService) -> None:
    _family(svc)
    r = svc.save_memory({"person": "Mario", "kind": "other", "title": "secret",
                         "text": f"my passphrase is {MARIO}", "passphrase": MARIO})
    assert "contains a passphrase" in r["speak_hint"]
    assert svc.db.list_memories() == []


def test_forget_memory_asks_first(svc: MedicineBuddyService) -> None:
    _family(svc)
    saved = svc.save_memory({"person": "Mario", "kind": "advice", "title": "Be patient",
                             "text": "Never rush the polenta.", "passphrase": MARIO})
    mid = saved["memory"]["id"]
    assert svc.forget_memory({"person": "Mario", "memory_id": mid, "confirmed": False,
                              "passphrase": ROSA})["status"] == "needs_confirm"
    assert svc.forget_memory({"person": "Mario", "memory_id": mid, "confirmed": True,
                              "passphrase": ROSA})["status"] == "ok"
    assert svc.db.list_memories() == []


def test_people_list_and_page(svc: MedicineBuddyService) -> None:
    _family(svc)
    svc.save_memory({"person": "Mario", "kind": "story", "title": "The lake", "text": "It froze.", "passphrase": MARIO})
    info = svc.list_people_info()
    assert {p["name"] for p in info["people"]} == {"Anna", "Mario", "Rosa"}
    assert "Mario (1 memories)" in info["speak_hint"] and "Anna (caregiver)" in info["speak_hint"]
    page = svc.status_page()
    assert "Family memories" in page and "The lake" in page and "Caregiver" in page
    for secret in (CARE, MARIO, ROSA):
        assert secret not in page


# ---------------------------------------------------------------- guard ON: passphrase must be SPOKEN

class Clock:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def gsvc(tmp_path: Path, clock: Clock):
    cat = MedicineCatalog({"Aspirin": []})
    s = MedicineBuddyService(tmp_path / "gp.db", AppConfig(dry_run=True, tray_labels=["ASPIRIN"]), cat,
                             VoiceGuard(cat, enabled=True, clock=clock, grace=0.0))
    _lock_down(s, voice_guard=True)
    yield s
    s.close()


def _say(s: MedicineBuddyService, clock: Clock, text: str) -> None:
    clock.t += 1
    s.voice_heard({"text": text})


def test_registration_needs_passphrase_said_twice(gsvc: MedicineBuddyService, clock: Clock) -> None:
    reg = {"name": "Anna", "role": "caregiver", "passphrase": CARE, "passphrase_repeat": CARE}
    _say(gsvc, clock, f"I'm Anna the caregiver, my passphrase is {CARE}")
    assert gsvc.register_person(reg)["blocked"] is True       # only said once, args don't count
    _say(gsvc, clock, CARE)
    assert gsvc.register_person(reg)["status"] == "ok"


def test_registration_accepts_passphrase_twice_in_one_utterance(
    gsvc: MedicineBuddyService, clock: Clock,
) -> None:
    reg = {"name": "Anna", "role": "caregiver", "passphrase": CARE, "passphrase_repeat": CARE}
    _say(gsvc, clock, f"{CARE} {CARE}")
    assert gsvc.register_person(reg)["status"] == "ok"


def test_schedule_new_passphrase_after_said_twice(gsvc: MedicineBuddyService, clock: Clock) -> None:
    """First-time medicine setup: say passphrase twice, then schedule_medicine with new_passphrase."""
    _say(gsvc, clock, "please add aspirin for Mario at nine")
    r = gsvc.add_schedule({"person": "Mario", "name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    assert r["status"] == "needs_passphrase_setup"
    _say(gsvc, clock, f"my passphrase is {MARIO}")
    _say(gsvc, clock, MARIO)
    r = gsvc.add_schedule({
        "person": "Mario", "name": "Aspirin", "weekdays": "daily", "time": "09:00",
        "new_passphrase": MARIO,
    })
    assert r.get("verified") is True
    assert gsvc.db.get_person_by_name("Mario") is not None


def test_spoken_passphrase_unlocks_for_five_minutes(gsvc: MedicineBuddyService, clock: Clock) -> None:
    _say(gsvc, clock, CARE)
    _say(gsvc, clock, CARE)
    gsvc.register_person({"name": "Anna", "role": "caregiver", "passphrase": CARE, "passphrase_repeat": CARE})
    clock.t += AUTH_WINDOW + 10  # the registration was long ago

    add = {"name": "Aspirin", "weekdays": "daily", "time": "09:00"}
    _say(gsvc, clock, "please add aspirin at nine every day")
    # the AI "remembers" the passphrase and passes it: must NOT work when the guard is on
    assert gsvc.add_schedule({**add, "passphrase": CARE})["blocked"] is True

    _say(gsvc, clock, f"ok, {CARE}")
    assert gsvc.add_schedule(add)["verified"]
    clock.t += AUTH_WINDOW + 1
    _say(gsvc, clock, "and add aspirin at ten too")
    assert gsvc.add_schedule({**add, "time": "10:00"})["blocked"] is True  # unlock expired


def test_spoken_first_time_setup_then_own_list(gsvc: MedicineBuddyService, clock: Clock) -> None:
    """Voice flow: unknown Mario -> asked to set up -> says it twice -> changes his own list."""
    _say(gsvc, clock, "please add aspirin for Mario at nine")
    r = gsvc.add_schedule({"person": "Mario", "name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    assert r["status"] == "needs_passphrase_setup"
    _say(gsvc, clock, f"my passphrase is {MARIO}")
    _say(gsvc, clock, MARIO)
    assert gsvc.register_person({"name": "Mario", "role": "person", "passphrase": MARIO,
                                 "passphrase_repeat": MARIO})["status"] == "ok"
    _say(gsvc, clock, "add my aspirin at nine every day")
    assert gsvc.add_schedule({"person": "Mario", "name": "Aspirin", "weekdays": "daily", "time": "09:00"})["verified"]
    # Easy mode: Rosa's spoken passphrase can also manage Mario's list
    _say(gsvc, clock, ROSA)
    _say(gsvc, clock, ROSA)
    gsvc.register_person({"name": "Rosa", "role": "person", "passphrase": ROSA, "passphrase_repeat": ROSA})
    clock.t += AUTH_WINDOW + 10
    _say(gsvc, clock, f"{ROSA}, delete mario's aspirin")
    ask = gsvc.delete_schedule({"person": "Mario", "name": "Aspirin", "confirmed": False})
    assert ask.get("status") == "needs_confirm"
    _say(gsvc, clock, "yes please")
    r = gsvc.delete_schedule({"person": "Mario", "name": "Aspirin", "confirmed": True})
    assert r.get("verified") or r.get("status") == "ok"
    assert gsvc.save_memory({"person": "Rosa", "kind": "story", "title": "The lake",
                             "text": "It froze in 1956."})["verified"]


# ---------------------------------------------------------------- migration of older databases

def test_old_database_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE medicines (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                                created_at TEXT NOT NULL);
        CREATE TABLE schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, medicine_id INTEGER NOT NULL REFERENCES medicines(id),
                                weekdays TEXT NOT NULL, time_hhmm TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'local',
                                active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE dose_events (id INTEGER PRIMARY KEY AUTOINCREMENT, medicine_id INTEGER NOT NULL REFERENCES medicines(id),
                                  state TEXT NOT NULL, slot_id INTEGER, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        INSERT INTO medicines (id, name, created_at) VALUES (7, 'Aspirin', '2026-11-14T08:00:00+00:00');
        INSERT INTO schedules (medicine_id, weekdays, time_hhmm) VALUES (7, '*', '09:00');
    """)
    con.commit()
    con.close()

    db = Database(path)
    [m] = db.list_medicines()
    assert (m.id, m.name, m.person_id) == (7, "Aspirin", None)       # id kept, now unassigned
    assert db.list_active_schedules()[0].medicine_id == 7            # schedule still linked
    p = db.add_person("Mario", "person", make_secret(MARIO))
    assert db.add_medicine("Aspirin", p.id).id != 7                  # same name allowed per person
    db.close()
    Database(path).close()                                           # re-opening is a no-op
