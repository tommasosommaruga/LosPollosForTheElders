"""Adaptive assist, family call bridge, check-ins, and activity suggestions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.checkins import CheckInLoop, in_quiet_hours
from medicine_buddy.config import AppConfig
from medicine_buddy.voice_guard import VoiceGuard

ZURICH = ZoneInfo("Europe/Zurich")


def at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, 16, h, m, tzinfo=ZURICH)


class Speaker:
    def __init__(self) -> None:
        self.said: list[str] = []

    def __call__(self, text: str) -> bool:
        self.said.append(text)
        return True


@pytest.fixture
def svc(tmp_path: Path):
    cat = MedicineCatalog({"Aspirin": []})
    s = MedicineBuddyService(tmp_path / "c.db", AppConfig(dry_run=True), cat, VoiceGuard(cat, enabled=False))
    s.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    yield s
    s.close()


def test_family_call_needs_setup_then_ok(svc: MedicineBuddyService) -> None:
    missing = svc.start_family_call({})
    assert missing["status"] == "needs_setup"
    assert "Family settings" in missing["speak_hint"]

    bad = svc.settings_family_call({"url": "not-a-url"})
    assert "error" in bad

    saved = svc.settings_family_call({
        "url": "https://meet.google.com/abc-defg-hij",
        "label": "Call the children",
    })
    assert saved["configured"] is True
    ready = svc.start_family_call({})
    assert ready["status"] == "ok"
    assert ready["url"].startswith("https://meet.google.com/")
    assert "Call the children" in ready["speak_hint"] or "meet.google.com" in ready["speak_hint"]


def test_quiet_hours_overnight() -> None:
    assert in_quiet_hours(at(22, 0), "21:00", "08:00")
    assert in_quiet_hours(at(3, 0), "21:00", "08:00")
    assert not in_quiet_hours(at(10, 0), "21:00", "08:00")
    assert in_quiet_hours(at(13, 0), "12:00", "14:00")
    assert not in_quiet_hours(at(15, 0), "12:00", "14:00")


def test_checkin_speaks_when_idle_and_respects_quiet(svc: MedicineBuddyService) -> None:
    svc.settings_checkins({
        "enabled": True,
        "interval_hours": 1,
        "quiet_start": "21:00",
        "quiet_end": "08:00",
    })
    speaker = Speaker()
    loop = CheckInLoop(svc, speaker, quiet_after_speech=0)
    assert loop.tick(at(22, 0)) == []  # quiet hours
    out = loop.tick(at(10, 0))
    assert len(out) == 1 and "check-in" in out[0].lower()
    assert loop.tick(at(10, 30)) == []  # interval not elapsed
    assert loop.tick(at(11, 5))  # next window
    acts = svc.db.query_activity_events(via="checkin", limit=10)
    assert len(acts) >= 1


def test_checkins_can_be_disabled(svc: MedicineBuddyService) -> None:
    svc.settings_checkins({"enabled": False})
    loop = CheckInLoop(svc, Speaker(), quiet_after_speech=0)
    assert loop.tick(at(10, 0)) == []


def test_suggest_activity_rotate_and_log(svc: MedicineBuddyService) -> None:
    first = svc.suggest_activity({"kind": "motor"})
    assert first["status"] == "ok"
    assert first["activity"]["kind"] == "motor"
    assert "Optional activity" in first["speak_hint"]

    logged = svc.suggest_activity({
        "activity_id": first["activity"]["id"],
        "response": "accepted",
    })
    assert logged["logged"] == "accepted"
    acts = svc.db.query_activity_events(via="activity", limit=10)
    assert any(a.result == "accepted" for a in acts)

    declined = svc.suggest_activity({"activity_id": "memory_question", "response": "declined"})
    assert declined["logged"] == "declined"
    assert declined["activity"]["kind"] == "cognitive"


def test_profile_lists_new_tools() -> None:
    from pathlib import Path

    profile = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy" / "conversation" / "external_profiles"
        / "medicine_caregiver" / "profile.md"
    ).read_text(encoding="utf-8")
    assert "start_family_call" in profile
    assert "suggest_activity" in profile
    assert "Should I bring your" in profile
