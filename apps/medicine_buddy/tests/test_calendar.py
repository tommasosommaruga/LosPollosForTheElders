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
from medicine_buddy.calendar_mirror import medicine_mirror_uid, mirror_schedule
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
    assert result["schedules_updated"] == 1
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
    # Idempotent re-sync must not recount an unchanged schedule.
    again = service.sync_calendar_import(ics_text=second)
    assert again["status"] == "ok"
    assert again["schedules_updated"] == 0


def test_ambiguous_multi_dose_ics_does_not_clobber_schedules(
    service: MedicineBuddyService,
) -> None:
    """Two schedules for one medicine + unmatched ICS time must not touch either row."""
    med = service.db.add_medicine("Aspirin")
    morning = service.db.add_schedule(med.id, "*", "08:00")
    evening = service.db.add_schedule(med.id, "*", "20:00")
    ics = build_calendar([
        build_vevent(
            uid="ambig-med@x",
            summary="Med: Aspirin",
            time_hhmm="12:00",
            weekdays="1,3",
        ),
    ])
    result = service.sync_calendar_import(ics_text=ics)
    assert result["status"] == "ok"
    assert result["schedules_updated"] == 0
    by_id = {s.id: s for s in service.db.list_active_schedules()}
    assert by_id[morning.id].time_hhmm == "08:00" and by_id[morning.id].weekdays == "*"
    assert by_id[evening.id].time_hhmm == "20:00" and by_id[evening.id].weekdays == "*"
    proposals = service.db.list_calendar_events(status="proposed", kind="medicine")
    assert len(proposals) == 1
    assert proposals[0].time_hhmm == "12:00"
    assert proposals[0].weekdays == "1,3"


def test_create_and_cancel_companion_event(service: MedicineBuddyService) -> None:
    created = service.create_calendar_event({
        "title": "Garden walk",
        "weekdays": "daily",
        "time": "10:00",
    })
    assert created["status"] == "ok"
    assert created["google_pushed"] is False
    assert created["google_configured"] is False
    assert "calendar" in created["speak_hint"].casefold()
    listed = service.list_upcoming_events({})
    assert listed["status"] == "ok"
    assert any("Garden walk" in line for line in listed["companion"])
    need = service.cancel_calendar_event({"title": "Garden walk", "confirmed": False})
    assert need["status"] == "needs_confirmation"
    done = service.cancel_calendar_event({"title": "Garden walk", "confirmed": True})
    assert done["status"] == "ok"
    assert service.db.list_calendar_events(status="active", kind="companion") == []


def test_create_when_tomorrow_pushes_google(
    service: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.db.set_setting("timezone", "Europe/Zurich")
    monkeypatch.setattr(service, "now", lambda: SUN_EVENING)
    monkeypatch.setenv("MEDICINE_BUDDY_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("MEDICINE_BUDDY_GOOGLE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("MEDICINE_BUDDY_GOOGLE_REFRESH_TOKEN", "rtok")
    service.db.set_setting("google_calendar_id", "tommaso-test@group.calendar.google.com")

    inserted: list[dict] = []

    def fake_insert(cfg, body):  # noqa: ANN001
        inserted.append(body)
        assert cfg.calendar_id.startswith("tommaso-test")
        assert body["summary"] == "Take medicine A, B, C"
        # "tomorrow" must be one-shot — not a weekly RRULE.
        assert "recurrence" not in body
        assert body["start"]["dateTime"].startswith("2026-11-16T01:00:00")
        return "gcal-event-1"

    monkeypatch.setattr(
        "medicine_buddy.calendar_service.insert_google_event", fake_insert
    )
    monkeypatch.setattr(
        "medicine_buddy.calendar_service.delete_google_event",
        lambda cfg, eid: None,
    )

    created = service.create_calendar_event({
        "title": "Take medicine A, B, C",
        "when": "tomorrow",
        "time": "1 AM",
    })
    assert created["status"] == "ok"
    assert created["google_pushed"] is True
    assert created["google_configured"] is True
    assert "calendar" in created["speak_hint"].casefold()
    assert created["event"]["starts_at"].startswith("2026-11-16")
    assert created["event"]["time_hhmm"] == "01:00"
    assert created["event"]["weekdays"] == ""
    assert created["event"]["external_id"] == "gcal-event-1"
    assert len(inserted) == 1

    cancelled = service.cancel_calendar_event({
        "title": "Take medicine A, B, C",
        "confirmed": True,
    })
    assert cancelled["status"] == "ok"
    assert "Google" in cancelled["speak_hint"]


def test_create_every_monday_still_recurring(
    service: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.db.set_setting("timezone", "Europe/Zurich")
    monkeypatch.setattr(service, "now", lambda: SUN_EVENING)
    created = service.create_calendar_event({
        "title": "Gym reminder",
        "weekdays": "monday",
        "time": "3 AM",
    })
    assert created["status"] == "ok"
    assert created["event"]["weekdays"] == "0"
    assert created["event"]["time_hhmm"] == "03:00"


def test_create_calendar_event_starts_at_uses_service_now(
    service: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default starts_at follows Family clock (self.now), not the host date.today()."""
    service.db.set_setting("timezone", "Europe/Zurich")
    monkeypatch.setattr(service, "now", lambda: SUN_EVENING)
    created = service.create_calendar_event({
        "title": "Garden walk",
        "weekdays": "daily",
        "time": "10:00",
    })
    assert created["status"] == "ok"
    assert created["event"]["starts_at"] == "2026-11-15"


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
    assert first["google_hint"]
    assert "Google" in first["google_hint"]
    assert first["google_write"]["configured"] is False
    assert first["unified"] == []
    assert "last_sync" in first and "last_sync_error" in first
    rotated = service.settings_calendar({"rotate_token": True})
    assert rotated["export_token"] != first["export_token"]
    saved = service.settings_calendar({"import_url": "https://example.com/cal.ics"})
    assert saved["import_url"] == "https://example.com/cal.ics"
    with_gcal = service.settings_calendar({"google_calendar_id": "tommaso-test@group.calendar.google.com"})
    assert with_gcal["google_calendar_id"] == "tommaso-test@group.calendar.google.com"


def test_settings_calendar_unified_list(service: MedicineBuddyService) -> None:
    med = service.db.add_medicine("Aspirin")
    service.db.add_schedule(med.id, "0,2,4", "09:00")
    service.db.upsert_calendar_event(
        uid="local-walk@medicine-buddy",
        kind="companion",
        title="Garden walk",
        time_hhmm="15:00",
        weekdays="*",
        source="local",
        status="active",
    )
    payload = service.settings_calendar({})
    assert payload["status"] == "ok"
    kinds = {row["kind"] for row in payload["unified"]}
    assert kinds == {"medicine", "companion"}
    med_row = next(r for r in payload["unified"] if r["kind"] == "medicine")
    assert med_row["title"] == medicine_summary("Aspirin")
    assert med_row["weekdays"] == "0,2,4"
    assert med_row["time_hhmm"] == "09:00"
    assert med_row["status"] == "active"
    assert med_row["source"] == "local"
    companion = next(r for r in payload["unified"] if r["kind"] == "companion")
    assert companion["title"] == "Garden walk"
    assert companion["time_hhmm"] == "15:00"
    assert set(med_row) == {"kind", "title", "weekdays", "time_hhmm", "source", "status"}


def test_unified_list_skips_legacy_non_mirror_medicine(
    service: MedicineBuddyService,
) -> None:
    """Legacy active medicine rows (non mb-med-*) must not duplicate the mirror."""
    med = service.db.add_medicine("Aspirin")
    schedule = service.db.add_schedule(med.id, "*", "09:00")
    mirror_schedule(service.db, schedule, med)
    service.db.upsert_calendar_event(
        uid="legacy-import-med@x",
        kind="medicine",
        title=medicine_summary("Aspirin"),
        time_hhmm="09:00",
        weekdays="*",
        medicine_name="Aspirin",
        source="import",
        status="active",
    )
    payload = service.settings_calendar({})
    med_rows = [r for r in payload["unified"] if r["kind"] == "medicine"]
    assert len(med_rows) == 1
    assert med_rows[0]["title"] == medicine_summary("Aspirin")
    assert med_rows[0]["time_hhmm"] == "09:00"


def test_google_pull_merges_into_local_by_external_id(
    service: MedicineBuddyService,
) -> None:
    """Pulled gcal-* uid must update the local row that already has that Google id."""
    from medicine_buddy.calendar_ics import ParsedEvent

    local = service.db.upsert_calendar_event(
        uid="local-walk@medicine-buddy",
        kind="companion",
        title="Garden walk",
        time_hhmm="10:00",
        weekdays="*",
        source="local",
        external_id="abc123",
    )
    result = service._apply_parsed_calendar_items(
        [
            ParsedEvent(
                uid="gcal-abc123@google",
                summary="Garden walk (updated)",
                description="from google",
                time_hhmm="11:00",
                weekdays="*",
                starts_at=None,
                all_day=False,
                medicine_hint=None,
            )
        ],
        source="google",
    )
    assert result["companion_upserted"] == 1
    active = service.db.list_calendar_events(status="active", kind="companion")
    assert len(active) == 1
    assert active[0].id == local.id
    assert active[0].uid == "local-walk@medicine-buddy"
    assert active[0].external_id == "abc123"
    assert active[0].title == "Garden walk (updated)"
    assert active[0].time_hhmm == "11:00"


def test_dedupe_cancels_orphan_gcal_twin(service: MedicineBuddyService) -> None:
    """Existing local+gcal twins for the same Google id → cancel the gcal-* copy."""
    local = service.db.upsert_calendar_event(
        uid="local-physio@medicine-buddy",
        kind="companion",
        title="Physio",
        time_hhmm="14:00",
        weekdays="1,3",
        source="local",
        external_id="g1",
    )
    twin = service.db.upsert_calendar_event(
        uid="gcal-g1@google",
        kind="companion",
        title="Physio",
        time_hhmm="14:00",
        weekdays="1,3",
        source="google",
        external_id="g1",
    )
    cancelled = service.dedupe_google_calendar_twins()
    assert cancelled == 1
    assert service.db.get_calendar_event(local.id).status == "active"
    assert service.db.get_calendar_event(twin.id).status == "cancelled"
    assert len(service.db.list_calendar_events(status="active", kind="companion")) == 1


def test_admin_delete_calendar_and_dose_rows(service: MedicineBuddyService) -> None:
    from medicine_buddy.states import DoseState

    ev = service.db.upsert_calendar_event(
        uid="del-me@medicine-buddy",
        kind="companion",
        title="Temp",
        time_hhmm="09:00",
        weekdays="*",
    )
    med = service.db.add_medicine("Aspirin")
    dose = service.db.record_transition(med.id, DoseState.DUE, detail="test")
    snap = service.database_snapshot()
    names = {t["name"] for t in snap["tables"]}
    assert "calendar_events" in names
    assert service.admin_delete_row("calendar_events", ev.id)["status"] == "ok"
    assert service.db.get_calendar_event(ev.id) is None
    assert service.admin_delete_row("dose_events", dose.id)["status"] == "ok"
    assert service.db.list_dose_events(medicine_id=med.id) == []
    bad = service.admin_delete_row("people", 1)
    assert "error" in bad


def test_never_repush_gcal_rows_even_without_external_id(
    service: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gcal-* twins must never create NEW Google events (the duplicate factory)."""
    monkeypatch.setenv("MEDICINE_BUDDY_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("MEDICINE_BUDDY_GOOGLE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("MEDICINE_BUDDY_GOOGLE_REFRESH_TOKEN", "rtok")
    service.db.set_setting("google_calendar_id", "cal@group.calendar.google.com")
    service.db.upsert_calendar_event(
        uid="gcal-oldid@google",
        kind="companion",
        title="Gym",
        time_hhmm="03:00",
        weekdays="0",
        source="google",
        external_id="",  # empty — old bug path
    )
    inserts: list[dict] = []
    monkeypatch.setattr(
        "medicine_buddy.calendar_service.insert_google_event",
        lambda cfg, body: inserts.append(body) or "new-id",
    )
    assert service._push_unsynced_local_to_google() == 0
    assert inserts == []


def test_fingerprint_dedupe_cancels_gcal_twin_of_local(
    service: MedicineBuddyService,
) -> None:
    """Same title/time/days → keep local, cancel google twin (even with different Google ids)."""
    local = service.db.upsert_calendar_event(
        uid="local-gym@medicine-buddy",
        kind="companion",
        title="Gym",
        time_hhmm="03:00",
        weekdays="0",
        source="local",
        external_id="master1",
    )
    twin = service.db.upsert_calendar_event(
        uid="gcal-other@google",
        kind="companion",
        title="Gym",
        time_hhmm="03:00",
        weekdays="0",
        source="google",
        external_id="instance2",
    )
    n = service.dedupe_google_calendar_twins()
    assert n >= 1
    assert service.db.get_calendar_event(local.id).status == "active"
    assert service.db.get_calendar_event(twin.id).status == "cancelled"


def test_google_pull_merges_by_title_time_weekdays(
    service: MedicineBuddyService,
) -> None:
    from medicine_buddy.calendar_ics import ParsedEvent

    local = service.db.upsert_calendar_event(
        uid="local-sandwich@medicine-buddy",
        kind="companion",
        title="make a sandwich",
        time_hhmm="00:00",
        weekdays="4",
        source="local",
        external_id="old-google-id",
    )
    service._apply_parsed_calendar_items(
        [
            ParsedEvent(
                uid="gcal-brand-new-id@google",
                summary="make a sandwich",
                description="",
                time_hhmm="00:00",
                weekdays="4",
                starts_at=None,
                all_day=False,
                medicine_hint=None,
            )
        ],
        source="google",
    )
    active = service.db.list_calendar_events(status="active", kind="companion")
    assert len(active) == 1
    assert active[0].id == local.id
    assert active[0].external_id == "brand-new-id"


def test_purge_cancelled_calendar_rows(service: MedicineBuddyService) -> None:
    keep = service.db.upsert_calendar_event(
        uid="keep@medicine-buddy",
        kind="companion",
        title="Keep",
        time_hhmm="10:00",
        weekdays="*",
        status="active",
    )
    gone = service.db.upsert_calendar_event(
        uid="gone@medicine-buddy",
        kind="companion",
        title="Gone",
        time_hhmm="11:00",
        weekdays="*",
        status="active",
    )
    service.db.set_calendar_event_status(gone.id, "cancelled")
    result = service.admin_purge_calendar_junk()
    assert result["status"] == "ok"
    assert result["hard_deleted_cancelled"] >= 1
    assert service.db.get_calendar_event(keep.id) is not None
    assert service.db.get_calendar_event(gone.id) is None


def test_admin_delete_calendar_also_removes_google(
    service: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDICINE_BUDDY_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("MEDICINE_BUDDY_GOOGLE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("MEDICINE_BUDDY_GOOGLE_REFRESH_TOKEN", "rtok")
    service.db.set_setting("google_calendar_id", "cal@group.calendar.google.com")
    deleted: list[str] = []
    monkeypatch.setattr(
        "medicine_buddy.calendar_service.delete_google_event",
        lambda cfg, eid: deleted.append(eid),
    )
    ev = service.db.upsert_calendar_event(
        uid="local-x@medicine-buddy",
        kind="companion",
        title="X",
        time_hhmm="12:00",
        weekdays="0",
        source="local",
        external_id="gid-1",
    )
    twin = service.db.upsert_calendar_event(
        uid="gcal-gid-2@google",
        kind="companion",
        title="X",
        time_hhmm="12:00",
        weekdays="0",
        source="google",
        external_id="gid-2",
    )
    assert service.admin_delete_row("calendar_events", ev.id)["status"] == "ok"
    assert service.db.get_calendar_event(ev.id) is None
    assert service.db.get_calendar_event(twin.id) is None  # same fingerprint gone
    assert "gid-1" in deleted and "gid-2" in deleted


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
