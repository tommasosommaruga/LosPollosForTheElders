"""Companion calendar: ICS build/parse, import proposals, export feed."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.calendar_ics import (
    build_calendar,
    build_vevent,
    medicine_name_from_summary,
    medicine_summary,
    parse_ics,
    weekdays_to_byday,
)
from medicine_buddy.calendar_mirror import medicine_mirror_uid
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.voice_guard import VoiceGuard

ZURICH = ZoneInfo("Europe/Zurich")
# Sunday evening — tomorrow is Monday.
SUN_EVENING = datetime(2026, 11, 15, 20, 0, tzinfo=ZURICH)


@pytest.fixture
def catalog() -> MedicineCatalog:
    return MedicineCatalog({"Aspirin": ["asa"], "Vitamin D": ["vit d"]})


@pytest.fixture
def service(tmp_path: Path, catalog: MedicineCatalog) -> MedicineBuddyService:
    db_path = tmp_path / "cal.db"
    guard = VoiceGuard(catalog, enabled=False)
    return MedicineBuddyService(db_path, AppConfig(dry_run=True), catalog, guard)


def test_medicine_summary_roundtrip() -> None:
    assert medicine_name_from_summary(medicine_summary("Aspirin")) == "Aspirin"
    assert medicine_name_from_summary("Physio at clinic") is None


def test_ics_build_and_parse_recurring() -> None:
    block = build_vevent(
        uid="test-1@medicine-buddy",
        summary=medicine_summary("Aspirin"),
        time_hhmm="09:00",
        weekdays="0,2,4",
    )
    text = build_calendar([block])
    assert "BEGIN:VCALENDAR" in text
    assert "Med: Aspirin" in text
    assert f"BYDAY={weekdays_to_byday('0,2,4')}" in text.replace("\r\n", "")
    parsed = parse_ics(text)
    assert len(parsed) == 1
    assert parsed[0].medicine_hint == "Aspirin"
    assert parsed[0].time_hhmm == "09:00"
    assert parsed[0].weekdays == "0,2,4"


def test_db_calendar_upsert(tmp_path: Path) -> None:
    db = Database(tmp_path / "c.db")
    ev = db.upsert_calendar_event(
        uid="u1",
        kind="companion",
        title="Call Maria",
        time_hhmm="15:00",
        weekdays="*",
    )
    assert ev.id > 0
    again = db.upsert_calendar_event(
        uid="u1",
        kind="companion",
        title="Call Maria (updated)",
        time_hhmm="16:00",
        weekdays="*",
    )
    assert again.id == ev.id
    assert again.title.startswith("Call Maria")
    assert again.time_hhmm == "16:00"


def test_export_contains_medicine_and_companion(service: MedicineBuddyService) -> None:
    med = service.db.add_medicine("Aspirin")
    service.db.add_schedule(med.id, "*", "09:00")
    service.db.upsert_calendar_event(
        uid="local-1@medicine-buddy",
        kind="companion",
        title="Physio",
        time_hhmm="14:00",
        weekdays="1,3",
    )
    ics = service.build_export_ics()
    assert "Med: Aspirin" in ics
    assert "Physio" in ics
    token = service.calendar_export_token()
    assert service.export_ics_for_token(token) == ics
    assert service.export_ics_for_token("wrong-token-value!!") is None


def test_import_medicine_becomes_proposal_until_accepted(service: MedicineBuddyService) -> None:
    ics = build_calendar([
        build_vevent(
            uid="imp-med@x",
            summary="Med: Aspirin",
            time_hhmm="08:30",
            weekdays="*",
        ),
        build_vevent(
            uid="imp-comp@x",
            summary="Lunch with Nonna",
            time_hhmm="12:00",
            weekdays="0",
        ),
    ])
    result = service.sync_calendar_import(ics_text=ics)
    assert result["status"] == "ok"
    assert result["proposals"] == 1
    assert result["companion_upserted"] == 1
    assert service.db.list_active_schedules() == []
    proposals = service.db.list_calendar_events(status="proposed", kind="medicine")
    assert len(proposals) == 1
    accepted = service.accept_calendar_proposal({"id": proposals[0].id})
    assert accepted["status"] == "ok"
    schedules = service.db.list_active_schedules()
    assert len(schedules) == 1
    assert schedules[0].time_hhmm == "08:30"
    med = service.db.get_medicine(schedules[0].medicine_id)
    assert med is not None and med.name == "Aspirin"
    # Accept must leave a medicine mirror under the stable mb-med-* uid.
    mirror = service.db.get_calendar_event_by_uid(medicine_mirror_uid(schedules[0].id))
    assert mirror is not None
    assert mirror.kind == "medicine" and mirror.status == "active"
    assert mirror.medicine_name == "Aspirin"
    assert mirror.time_hhmm == "08:30"


def test_import_companion_missing_from_feed_is_cancelled(service: MedicineBuddyService) -> None:
    """Active source=import companions not in the latest feed become cancelled; local stays."""
    first = build_calendar([
        build_vevent(
            uid="imp-comp@x",
            summary="Lunch with Nonna",
            time_hhmm="12:00",
            weekdays="0",
        ),
        build_vevent(
            uid="imp-keep@x",
            summary="Physio",
            time_hhmm="14:00",
            weekdays="1",
        ),
    ])
    service.sync_calendar_import(ics_text=first)
    service.db.upsert_calendar_event(
        uid="local-garden@medicine-buddy",
        kind="companion",
        title="Garden walk",
        time_hhmm="10:00",
        weekdays="*",
        source="local",
        status="active",
    )
    second = build_calendar([
        build_vevent(
            uid="imp-keep@x",
            summary="Physio",
            time_hhmm="14:00",
            weekdays="1",
        ),
    ])
    result = service.sync_calendar_import(ics_text=second)
    assert result["status"] == "ok"
    cancelled = service.db.get_calendar_event_by_uid("imp-comp@x")
    assert cancelled is not None and cancelled.status == "cancelled"
    kept = service.db.get_calendar_event_by_uid("imp-keep@x")
    assert kept is not None and kept.status == "active"
    local = service.db.get_calendar_event_by_uid("local-garden@medicine-buddy")
    assert local is not None and local.status == "active"


def test_resync_updates_accepted_medicine_schedule_time(service: MedicineBuddyService) -> None:
    """After accept, ICS time/weekday changes update the linked schedule and remirror."""
    first = build_calendar([
        build_vevent(
            uid="imp-med@x",
            summary="Med: Aspirin",
            time_hhmm="08:30",
            weekdays="*",
        ),
    ])
    service.sync_calendar_import(ics_text=first)
    proposal = service.db.list_calendar_events(status="proposed", kind="medicine")[0]
    assert service.accept_calendar_proposal({"id": proposal.id})["status"] == "ok"
    before = service.db.list_active_schedules()
    assert len(before) == 1 and before[0].time_hhmm == "08:30"

    second = build_calendar([
        build_vevent(
            uid="imp-med@x",
            summary="Med: Aspirin",
            time_hhmm="09:15",
            weekdays="0,2,4",
        ),
    ])
    result = service.sync_calendar_import(ics_text=second)
    assert result["status"] == "ok"
    # No new proposal when an active schedule already exists for that medicine.
    assert service.db.list_calendar_events(status="proposed", kind="medicine") == []
    schedules = service.db.list_active_schedules()
    assert len(schedules) == 1
    assert schedules[0].time_hhmm == "09:15"
    assert schedules[0].weekdays == "0,2,4"
    mirror = service.db.get_calendar_event_by_uid(medicine_mirror_uid(schedules[0].id))
    assert mirror is not None
    assert mirror.time_hhmm == "09:15"
    assert mirror.weekdays == "0,2,4"


def test_create_and_cancel_companion_event(service: MedicineBuddyService) -> None:
    created = service.create_calendar_event({
        "title": "Garden walk",
        "weekdays": "daily",
        "time": "10:00",
    })
    assert created["status"] == "ok"
    listed = service.list_upcoming_events({})
    assert listed["status"] == "ok"
    assert any("Garden walk" in line for line in listed["companion"])
    need = service.cancel_calendar_event({"title": "Garden walk", "confirmed": False})
    assert need["status"] == "needs_confirmation"
    done = service.cancel_calendar_event({"title": "Garden walk", "confirmed": True})
    assert done["status"] == "ok"
    assert service.db.list_calendar_events(status="active", kind="companion") == []


def test_list_upcoming_when_tomorrow_includes_monday_med(
    service: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sun evening + when=tomorrow → Monday 09:00 schedule is in the window."""
    service.db.set_setting("timezone", "Europe/Zurich")
    monkeypatch.setattr(service, "now", lambda: SUN_EVENING)
    med = service.db.add_medicine("Aspirin")
    service.db.add_schedule(med.id, "0", "09:00")  # Monday
    listed = service.list_upcoming_events({"when": "tomorrow"})
    assert listed["status"] == "ok"
    names = [m["name"] for m in listed["medicines"]]
    assert "Aspirin" in names
    assert "Aspirin" in listed["speak_hint"]


def test_list_upcoming_empty_window_honest(
    service: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.db.set_setting("timezone", "Europe/Zurich")
    monkeypatch.setattr(service, "now", lambda: SUN_EVENING)
    med = service.db.add_medicine("Aspirin")
    service.db.add_schedule(med.id, "0", "09:00")  # Monday only
    # Yesterday has no Monday occurrence
    listed = service.list_upcoming_events({"when": "yesterday"})
    assert listed["status"] == "ok"
    assert listed["medicines"] == []
    assert listed["companion"] == []
    assert "nothing" in listed["speak_hint"].casefold() or "no" in listed["speak_hint"].casefold()


def test_list_upcoming_companion_in_when_window(
    service: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.db.set_setting("timezone", "Europe/Zurich")
    monkeypatch.setattr(service, "now", lambda: SUN_EVENING)
    service.db.upsert_calendar_event(
        uid="walk@local",
        kind="companion",
        title="Garden walk",
        time_hhmm="10:00",
        weekdays="0",  # Monday
        starts_at="2026-11-16",
    )
    listed = service.list_upcoming_events({"when": "tomorrow"})
    assert any("Garden walk" in line for line in listed["companion"])
    empty = service.list_upcoming_events({"when": "yesterday"})
    assert empty["companion"] == []


def test_create_rejects_medicine_titles(service: MedicineBuddyService) -> None:
    bad = service.create_calendar_event({"title": "Med: Aspirin", "time": "9 AM"})
    assert "error" in bad
    bad2 = service.create_calendar_event({"title": "Aspirin", "time": "9 AM"})
    assert "error" in bad2


def test_settings_calendar_roundtrip(service: MedicineBuddyService) -> None:
    first = service.settings_calendar({})
    assert first["status"] == "ok"
    assert first["export_token"]
    rotated = service.settings_calendar({"rotate_token": True})
    assert rotated["export_token"] != first["export_token"]
    saved = service.settings_calendar({"import_url": "https://example.com/cal.ics"})
    assert saved["import_url"] == "https://example.com/cal.ics"


def _one(dtstart_line: str) -> str:
    return f"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nSUMMARY:Visit\r\n{dtstart_line}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"


def test_parse_ics_converts_utc_and_tzid_to_family_zone() -> None:
    from zoneinfo import ZoneInfo
    paris = ZoneInfo("Europe/Paris")
    # Google-style UTC export: 13:30Z in November = 14:30 in Paris (the reported 1 h bug)
    ev = parse_ics(_one("DTSTART:20261116T133000Z"), tz=paris)[0]
    assert ev.time_hhmm == "14:30" and ev.starts_at == "2026-11-16T14:30:00"
    # Summer time: 12:30Z = 14:30 CEST
    assert parse_ics(_one("DTSTART:20260716T123000Z"), tz=paris)[0].time_hhmm == "14:30"
    # TZID in the same zone is kept; another zone is converted
    assert parse_ics(_one("DTSTART;TZID=Europe/Zurich:20261116T143000"), tz=paris)[0].time_hhmm == "14:30"
    assert parse_ics(_one("DTSTART;TZID=Europe/London:20261116T133000"), tz=paris)[0].time_hhmm == "14:30"
    # Floating time is local wall-clock time: unchanged
    assert parse_ics(_one("DTSTART:20261116T143000"), tz=paris)[0].time_hhmm == "14:30"
    # UTC late evening rolls into the next local day (weekday follows the local date)
    late = parse_ics(_one("DTSTART:20261115T233000Z"), tz=paris)[0]
    assert late.starts_at == "2026-11-16T00:30:00" and late.weekdays == "0"
