"""Missed-dose caregiver alerts (ntfy / webhook) and Family settings."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.reminders import ReminderLoop
from medicine_buddy.states import DoseState
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
    cat = MedicineCatalog({"Aspirin": [], "Vitamin D": []})
    s = MedicineBuddyService(tmp_path / "a.db", AppConfig(dry_run=True), cat, VoiceGuard(cat, enabled=False))
    s.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    yield s
    s.close()


def test_missed_dose_sends_one_alert(svc: MedicineBuddyService) -> None:
    posts: list[tuple[str, str, str]] = []

    def fake_post(url: str, title: str, message: str) -> tuple[bool, str]:
        posts.append((url, title, message))
        return True, "HTTP 200"

    svc.alert_post = fake_post
    svc.db.set_setting("caregiver_alert_url", "https://ntfy.sh/family-demo")
    svc.db.set_setting("caregiver_alerts_enabled", "1")
    loop = ReminderLoop(svc, Speaker())
    for minute in (0, 10, 20):
        loop.tick(at(9, minute))
    assert loop.tick(at(10, 5)) == []
    assert [e.state for e in svc.db.list_dose_events()] == [DoseState.DUE, DoseState.MISSED]
    assert len(posts) == 1
    assert "Aspirin" in posts[0][1]
    assert "Missed" in posts[0][2]
    acts = svc.db.query_activity_events(via="alert", limit=10)
    assert any(a.path == "/alerts/missed" for a in acts)


def test_alerts_disabled_skips_post(svc: MedicineBuddyService) -> None:
    posts: list[tuple[str, str, str]] = []
    svc.alert_post = lambda u, t, m: posts.append((u, t, m)) or (True, "ok")
    svc.db.set_setting("caregiver_alert_url", "https://ntfy.sh/family-demo")
    svc.db.set_setting("caregiver_alerts_enabled", "0")
    loop = ReminderLoop(svc, Speaker())
    for minute in (0, 10, 20):
        loop.tick(at(9, minute))
    loop.tick(at(10, 5))
    assert posts == []


def test_settings_test_button(svc: MedicineBuddyService) -> None:
    posts: list[tuple[str, str, str]] = []
    svc.alert_post = lambda u, t, m: posts.append((u, t, m)) or (True, "HTTP 200")
    r = svc.settings_caregiver_alerts({
        "url": "https://ntfy.sh/family-demo",
        "enabled": True,
        "test": True,
    })
    assert r["status"] == "ok" and r["test"]["ok"] is True
    assert len(posts) == 1 and "Test alert" in posts[0][2]


def test_bad_alert_url_rejected(svc: MedicineBuddyService) -> None:
    r = svc.settings_caregiver_alerts({"url": "ftp://evil.example/x"})
    assert "error" in r
    assert svc.alert_url() == ""
