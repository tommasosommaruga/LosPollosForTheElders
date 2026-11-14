"""Names: one canonical form, so two spellings of a name can never be two people (and never mix up)."""

from __future__ import annotations

import logging
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.auth import make_secret
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.names import name_key, strip_titles

OLD, NEW = "old fishing boat", "green kite river"


@pytest.fixture
def svc(tmp_path: Path):
    s = MedicineBuddyService(tmp_path / "n.db", AppConfig(dry_run=True), MedicineCatalog({"Aspirin": []}))
    yield s
    s.close()


def reg(s: MedicineBuddyService, name: str, phrase: str = OLD, **kw) -> dict:
    return s.register_person({"name": name, "role": kw.pop("role", "person"), "passphrase": phrase,
                              "passphrase_repeat": phrase, **kw})


# ---------------------------------------------------------------- the key itself

@pytest.mark.parametrize("a,b", [
    ("Nicolò", "NICOLÒ"), ("Nicolò", "nicolo"), ("José", unicodedata.normalize("NFD", "José")),
    ("Élodie", "elodie"), ("  Mario   Rossi ", "mario rossi"), ("Mary-Ann", "mary ann"), ("O'Brien", "obrien"[:1] + "'" + "brien"),
    ("Müller", "MULLER"),
])
def test_same_name_same_key(a: str, b: str) -> None:
    assert name_key(a) == name_key(b)


@pytest.mark.parametrize("a,b", [("Mario", "Maria"), ("Mario", "Mario Rossi"), ("Luca", "Luka")])
def test_different_names_different_keys(a: str, b: str) -> None:
    assert name_key(a) != name_key(b)


def test_titles_are_stripped() -> None:
    assert strip_titles(name_key("Grandpa Mario")) == "mario"
    assert strip_titles(name_key("my nonna Rosa")) == "rosa"
    assert strip_titles("mario") == "mario"


# ---------------------------------------------------------------- no duplicates, on every path

@pytest.mark.parametrize("second", ["Nicolò", "NICOLÒ", "nicolo", "Nicòlo", " nicolò  "])
def test_accent_and_case_duplicates_are_refused(svc: MedicineBuddyService, second: str) -> None:
    assert reg(svc, "Nicolò")["status"] == "ok"
    r = reg(svc, second, NEW)
    assert "already registered" in r["error"]
    assert [p.name for p in svc.db.list_people()] == ["Nicolò"]
    assert svc.db.get_person_by_name("Nicolò").secret.matches(OLD)      # untouched: nothing overridden
    assert not svc.db.get_person_by_name("Nicolò").secret.matches(NEW)


def test_duplicate_message_tells_a_namesake_what_to_do(svc: MedicineBuddyService) -> None:
    reg(svc, "Mario")
    hint = reg(svc, "mario", NEW)["speak_hint"]
    assert "different person with the same name" in hint and "full name" in hint and "Family settings" in hint


def test_duplicate_refused_on_the_family_page_path_too(svc: MedicineBuddyService) -> None:
    assert svc.settings_register({"name": "José", "role": "person", "passphrase": OLD, "passphrase_repeat": OLD})["status"] == "ok"
    r = svc.settings_register({"name": unicodedata.normalize("NFD", "José").upper(), "role": "caregiver",
                               "passphrase": NEW, "passphrase_repeat": NEW})
    assert "already registered" in r["error"]


def test_db_level_guard_even_if_the_service_check_is_bypassed(tmp_path: Path) -> None:
    db = Database(tmp_path / "d.db")
    db.add_person("Nicolò", "person", make_secret(OLD))
    with pytest.raises(ValueError, match="already registered"):
        db.add_person("NICOLÒ", "person", make_secret(NEW))
    with pytest.raises(sqlite3.IntegrityError):       # and the unique index backs it up
        db._conn.execute("INSERT INTO people (name, name_key, role, pass_salt, pass_hash, pass_words, pass_iterations, created_at)"
                         " VALUES ('x', 'nicolo', 'person', x'00', x'00', 3, 1, '2026')")
    db.close()


@pytest.mark.parametrize("bad", ["!!!", "   ", "", "x" * 61])
def test_bad_names_are_rejected_not_crashing(svc: MedicineBuddyService, bad: str) -> None:
    r = reg(svc, bad)
    assert "error" in r and svc.db.list_people() == []


# ---------------------------------------------------------------- lookups never mix people up

def test_titles_find_the_right_person(svc: MedicineBuddyService) -> None:
    reg(svc, "Mario")
    reg(svc, "Rosa", NEW)
    for spoken, expected in [("grandpa Mario", "Mario"), ("Nonna Rosa", "Rosa"), ("my uncle MARIO", "Mario")]:
        p, err = svc._resolve_person(spoken, for_change=True)
        assert err is None and p.name == expected


def test_a_different_person_with_the_same_first_name_is_not_mario(svc: MedicineBuddyService) -> None:
    reg(svc, "Mario")
    p, err = svc._resolve_person("Mario Bianchi", for_change=True)
    assert p is None and err["status"] == "needs_passphrase_setup" and err["person"] == "Mario Bianchi"
    # ...so he can enrol under his own name, and nothing of Mario's changes
    assert reg(svc, "Mario Bianchi", NEW)["status"] == "ok"
    assert svc._resolve_person("Mario", for_change=True)[0].name == "Mario"
    assert svc._resolve_person("mario bianchi", for_change=True)[0].name == "Mario Bianchi"
    assert svc.db.get_person_by_name("Mario").secret.matches(OLD)


def test_questions_do_not_guess_a_longer_name_either(svc: MedicineBuddyService) -> None:
    reg(svc, "Mario")
    r = svc.recall_memories({"person": "Mario Bianchi"})
    assert "don't know anyone called" in r["speak_hint"]
    assert svc.recall_memories({"person": "Mari"})["person"]["name"] == "Mario"      # a slip is still forgiven


def test_change_passphrase_hits_the_right_person(svc: MedicineBuddyService) -> None:
    reg(svc, "Nicolò")
    reg(svc, "Nicola", NEW)
    r = svc.settings_change_passphrase({"name": "NICOLO", "current": OLD, "new": "blue garden kettle",
                                        "new_repeat": "blue garden kettle"})
    assert r["status"] == "ok"
    assert svc.db.get_person_by_name("Nicolò").secret.matches("blue garden kettle")
    assert svc.db.get_person_by_name("Nicola").secret.matches(NEW)           # the other one is untouched


# ---------------------------------------------------------------- existing databases

def _legacy_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            role TEXT NOT NULL CHECK (role IN ('person','caregiver')), pass_salt BLOB NOT NULL, pass_hash BLOB NOT NULL,
            pass_words INTEGER NOT NULL, pass_iterations INTEGER NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO people (name, role, pass_salt, pass_hash, pass_words, pass_iterations, created_at) VALUES
            ('Mario', 'person', x'01', x'01', 3, 1, '2026-01-01'),
            ('Nicolò', 'person', x'02', x'02', 3, 1, '2026-01-02'),
            ('NICOLÒ', 'person', x'03', x'03', 3, 1, '2026-01-03');
    """)
    con.commit()
    con.close()


def test_existing_duplicates_are_kept_flagged_and_new_ones_blocked(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    with caplog.at_level(logging.WARNING, logger="medicine_buddy.db"):
        db = Database(path)
    assert sorted(p.name for p in db.list_people()) == sorted(["Mario", "Nicolò", "NICOLÒ"])   # nobody deleted or merged
    assert "same name as an earlier person" in caplog.text
    keys = [r["name_key"] for r in db._conn.execute("SELECT name_key FROM people ORDER BY id")]
    assert keys[0] == "mario" and keys[1] == "nicolo" and keys[2].startswith("nicolo~")
    assert db.get_person_by_name("nicolo").name == "Nicolò"                          # the older one wins the name
    with pytest.raises(ValueError):
        db.add_person("nicolò", "person", make_secret(OLD))
    db.close()
    Database(path).close()                                                           # re-opening is a no-op


def test_fresh_and_migrated_databases_agree(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    _legacy_db(path)
    Database(path).close()
    db = Database(path)
    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(people)")}
    assert "name_key" in cols
    assert any(r["name"] == "ux_people_name_key" for r in db._conn.execute("PRAGMA index_list(people)"))
    db.close()
