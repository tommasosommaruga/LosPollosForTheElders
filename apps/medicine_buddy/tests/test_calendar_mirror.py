"""Medicine schedule ↔ calendar_events mirror."""

from __future__ import annotations

from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.auth import make_secret
from medicine_buddy.calendar_ics import medicine_summary
from medicine_buddy.calendar_mirror import (
    backfill_medicine_mirrors,
    cancel_mirror,
    medicine_mirror_uid,
    mirror_schedule,
)
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.voice_guard import VoiceGuard


@pytest.fixture
def catalog() -> MedicineCatalog:
    return MedicineCatalog({"Aspirin": ["asa"], "Vitamin D": ["vit d"]})


@pytest.fixture
def service(tmp_path: Path, catalog: MedicineCatalog) -> MedicineBuddyService:
    guard = VoiceGuard(catalog, enabled=False)
    return MedicineBuddyService(tmp_path / "mirror.db", AppConfig(dry_run=True), catalog, guard)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "mirror_unit.db")


def test_mirror_schedule_upserts_medicine_row(db: Database) -> None:
    med = db.add_medicine("Aspirin")
    schedule = db.add_schedule(med.id, "*", "09:00")
    ev = mirror_schedule(db, schedule, med)
    assert ev.kind == "medicine"
    assert ev.status == "active"
    assert ev.uid == medicine_mirror_uid(schedule.id)
    assert ev.uid == f"mb-med-{schedule.id}@medicine-buddy"
    assert ev.medicine_name == "Aspirin"
    assert ev.title == medicine_summary("Aspirin")
    assert ev.weekdays == "*"
    assert ev.time_hhmm == "09:00"
    assert ev.person_id is None
    again = mirror_schedule(db, schedule, med)
    assert again.id == ev.id


def test_mirror_schedule_copies_person_id(db: Database) -> None:
    person = db.add_person("Mario", "person", make_secret("old fishing boat"))
    med = db.add_medicine("Aspirin", person.id)
    schedule = db.add_schedule(med.id, "0,2,4", "08:30")
    ev = mirror_schedule(db, schedule, med)
    assert ev.person_id == person.id
    assert ev.weekdays == "0,2,4"
    assert ev.time_hhmm == "08:30"


def test_cancel_mirror_sets_cancelled(db: Database) -> None:
    med = db.add_medicine("Aspirin")
    schedule = db.add_schedule(med.id, "*", "09:00")
    mirror_schedule(db, schedule, med)
    cancelled = cancel_mirror(db, schedule.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.uid == medicine_mirror_uid(schedule.id)
    assert cancel_mirror(db, 99999) is None


def test_backfill_medicine_mirrors_active_only(db: Database) -> None:
    asp = db.add_medicine("Aspirin")
    vit = db.add_medicine("Vitamin D")
    s1 = db.add_schedule(asp.id, "*", "09:00")
    s2 = db.add_schedule(vit.id, "1,3", "13:00")
    dead = db.add_schedule(asp.id, "*", "21:00")
    db.deactivate_schedule(dead.id)
    count = backfill_medicine_mirrors(db)
    assert count == 2
    active = db.list_calendar_events(status="active", kind="medicine")
    assert {e.uid for e in active} == {
        medicine_mirror_uid(s1.id),
        medicine_mirror_uid(s2.id),
    }
    assert db.get_calendar_event_by_uid(medicine_mirror_uid(dead.id)) is None


def test_backfill_cancels_legacy_non_mirror_medicine(db: Database) -> None:
    asp = db.add_medicine("Aspirin")
    schedule = db.add_schedule(asp.id, "*", "09:00")
    db.upsert_calendar_event(
        uid="legacy-import-med@x",
        kind="medicine",
        title=medicine_summary("Aspirin"),
        time_hhmm="09:00",
        weekdays="*",
        medicine_name="Aspirin",
        source="import",
        status="active",
    )
    count = backfill_medicine_mirrors(db)
    assert count == 1
    legacy = db.get_calendar_event_by_uid("legacy-import-med@x")
    assert legacy is not None and legacy.status == "cancelled"
    mirror = db.get_calendar_event_by_uid(medicine_mirror_uid(schedule.id))
    assert mirror is not None and mirror.status == "active"


def test_add_schedule_api_creates_mirror(service: MedicineBuddyService) -> None:
    r = service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    assert r["status"] == "ok"
    sid = r["schedule"]["id"]
    ev = service.db.get_calendar_event_by_uid(medicine_mirror_uid(sid))
    assert ev is not None
    assert ev.kind == "medicine"
    assert ev.status == "active"
    assert ev.medicine_name == "Aspirin"
    assert ev.time_hhmm == "09:00"


def test_update_schedule_api_updates_mirror(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    r = service.update_schedule({"name": "Aspirin", "new_time": "20:00"})
    assert r["status"] == "ok"
    sid = r["schedule"]["id"]
    ev = service.db.get_calendar_event_by_uid(medicine_mirror_uid(sid))
    assert ev is not None
    assert ev.time_hhmm == "20:00"
    assert ev.status == "active"


def test_delete_schedule_api_cancels_mirror(service: MedicineBuddyService) -> None:
    added = service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    sid = added["schedule"]["id"]
    deleted = service.delete_schedule({"name": "Aspirin", "confirmed": True})
    assert deleted["status"] == "ok"
    ev = service.db.get_calendar_event_by_uid(medicine_mirror_uid(sid))
    assert ev is not None
    assert ev.status == "cancelled"


def test_export_ics_not_duplicated_by_mirror(service: MedicineBuddyService) -> None:
    """Export still builds med VEVENTs from schedules only — mirror must not double them."""
    r = service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    sid = r["schedule"]["id"]
    assert service.db.get_calendar_event_by_uid(medicine_mirror_uid(sid)) is not None
    ics = service.build_export_ics()
    assert ics.count("Med: Aspirin") == 1
    assert ics.count(f"UID:mb-med-{sid}@medicine-buddy") == 1


def test_init_calendar_backfills_existing_schedules(
    tmp_path: Path, catalog: MedicineCatalog
) -> None:
    db_path = tmp_path / "backfill.db"
    db = Database(db_path)
    med = db.add_medicine("Aspirin")
    schedule = db.add_schedule(med.id, "*", "09:00")
    assert db.get_calendar_event_by_uid(medicine_mirror_uid(schedule.id)) is None
    db.close()

    guard = VoiceGuard(catalog, enabled=False)
    service = MedicineBuddyService(db_path, AppConfig(dry_run=True), catalog, guard)
    ev = service.db.get_calendar_event_by_uid(medicine_mirror_uid(schedule.id))
    assert ev is not None
    assert ev.kind == "medicine"
    assert ev.status == "active"
    assert ev.medicine_name == "Aspirin"
