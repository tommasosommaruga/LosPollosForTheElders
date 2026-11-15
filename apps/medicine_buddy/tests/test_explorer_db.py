"""Explorer DB helpers: activity, search, dose query, adherence, person records."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from medicine_buddy.auth import make_secret
from medicine_buddy.db import ACTIVITY_MAX_ROWS, Database, public_person
from medicine_buddy.states import DoseState


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "explorer.db")
    yield database
    database.close()


def _people(db: Database):
    care = db.add_person("Anna", "caregiver", make_secret("purple mountain sunrise"))
    mario = db.add_person("Mario", "person", make_secret("old fishing boat"))
    maria = db.add_person("Maria", "person", make_secret("quiet garden path"))
    return care, mario, maria


def test_activity_insert_query_filter_pagination(db: Database) -> None:
    a = db.insert_activity_event(
        at_ts=time.time(), via="api", method="POST", path="/schedules", status=200, ms=12.5,
        result="ok",
    )
    b = db.insert_activity_event(
        at_ts=time.time(), via="app-ui", method="GET", path="/medicine", status=200, ms=3.0,
    )
    db.insert_activity_event(
        at_ts=time.time(), via="api", method="POST", path="/doses/run", status=400, ms=8.0,
        error="blocked",
    )
    assert db.count_activity_events() == 3
    assert db.count_activity_events(via="api") == 2
    assert db.count_activity_events(status_min=400) == 1
    page = db.query_activity_events(via="api", limit=1, offset=0)
    assert len(page) == 1 and page[0].path == "/doses/run"
    newer = db.query_activity_events(since_id=a.id)
    assert [e.id for e in newer] == [b.id, newer[-1].id] if len(newer) >= 2 else [e.id for e in newer]
    assert all(e.id > a.id for e in newer)
    assert len(newer) == 2
    assert db.get_activity_event(a.id) == a


def test_search_people_exact_not_fuzzy(db: Database) -> None:
    _people(db)
    assert [p.name for p in db.search_people("Mario")] == ["Mario"]
    assert [p.name for p in db.search_people("Maria")] == ["Maria"]
    # Prefix may match both; fuzzy Maria↔Mario must never collapse to one wrong person
    prefix = {p.name for p in db.search_people("Mar")}
    assert prefix == {"Maria", "Mario"}
    assert db.search_people("Marioo") == []  # no difflib


def test_search_memories_attribution(db: Database) -> None:
    _, mario, maria = _people(db)
    db.add_memory(mario.id, "story", "Lake day", "We swam in the lake.")
    db.add_memory(maria.id, "recipe", "Soup", "Add beans and rosemary.")
    hits = db.search_memories("lake")
    assert len(hits) == 1
    assert hits[0]["person_id"] == mario.id and hits[0]["person_name"] == "Mario"
    assert db.count_memories_search("soup") == 1
    assert db.count_memories_search(person_id=maria.id) == 1
    assert db.search_memories(person_id=mario.id, kind="recipe") == []


def test_query_dose_events_and_adherence(db: Database) -> None:
    _, mario, _ = _people(db)
    asp = db.add_medicine("Aspirin", person_id=mario.id)
    db.record_transition(asp.id, DoseState.DUE)
    db.record_transition(asp.id, DoseState.REMINDED)
    db.record_transition(asp.id, DoseState.TAKEN)
    db.record_transition(asp.id, DoseState.DUE)
    db.record_transition(asp.id, DoseState.MISSED)
    rows = db.query_dose_events(person_id=mario.id, limit=10)
    assert len(rows) == 5
    assert rows[0]["person_name"] == "Mario" and rows[0]["medicine_name"] == "Aspirin"
    assert db.count_dose_events_query(person_id=mario.id, state="Taken") == 1
    report = db.adherence_report(person_id=mario.id, days=7)
    assert report["taken"] == 1 and report["missed"] == 1 and report["failed"] == 0
    assert report["adherence_pct"] == 50.0


def test_get_person_records_and_public_person(db: Database) -> None:
    _, mario, _ = _people(db)
    asp = db.add_medicine("Aspirin", person_id=mario.id)
    db.add_schedule(asp.id, "*", "09:00")
    db.add_memory(mario.id, "story", "Garden", "Roses every spring.")
    db.insert_activity_event(
        at_ts=time.time(), via="api", method="POST", path="/memories/save",
        status=200, ms=5.0, person_id=mario.id, result="ok",
    )
    dossier = db.get_person_records(mario.id)
    assert dossier is not None
    assert dossier["person"] == public_person(mario)
    assert "pass_hash" not in str(dossier) and "secret" not in str(dossier).lower()
    assert dossier["counts"]["medicines"] == 1
    assert dossier["counts"]["memories"] == 1
    assert dossier["counts"]["activity"] == 1
    assert len(dossier["schedules"]) == 1
    assert db.get_person_records(99999) is None


def test_activity_prune_caps_rows(db: Database, monkeypatch: pytest.Monkeypatch) -> None:
    monkeypatch.setattr("medicine_buddy.db.ACTIVITY_MAX_ROWS", 5)
    for i in range(8):
        db.insert_activity_event(
            at_ts=time.time(), via="api", method="GET", path=f"/h{i}", status=200, ms=1.0,
        )
    assert db.count_activity_events() == 5
    # oldest pruned
    paths = {e.path for e in db.query_activity_events(limit=20)}
    assert "/h0" not in paths and "/h7" in paths
    assert ACTIVITY_MAX_ROWS >= 5
