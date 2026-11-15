"""Wake greeting: upcoming schedules + spoken prompt (fake speaker)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.auth import make_secret
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.scheduler import find_upcoming_doses
from medicine_buddy.states import DoseState
from medicine_buddy.voice_guard import VoiceGuard
from medicine_buddy.wake import build_wake_prompt, deliver_wake_greeting

ZURICH = ZoneInfo("Europe/Zurich")


def at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, 16, h, m, tzinfo=ZURICH)  # Monday


class Speaker:
    def __init__(self, fail_times: int = 0) -> None:
        self.said: list[str] = []
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, text: str) -> bool:
        self.calls += 1
        if self.calls <= self.fail_times:
            return False
        self.said.append(text)
        return True


def test_find_upcoming_next_30_minutes(tmp_path: Path) -> None:
    db = Database(tmp_path / "u.db")
    person = db.add_person("Mario", "person", make_secret("old fishing boat"))
    asp = db.add_medicine("Aspirin", person_id=person.id)
    vit = db.add_medicine("Vitamin D", person_id=person.id)
    db.add_schedule(asp.id, "*", "09:15")
    db.add_schedule(vit.id, "*", "10:00")  # outside 30 min from 09:00
    db.add_schedule(asp.id, "*", "08:30")  # already past
    now = at(9, 0)
    up = find_upcoming_doses(db, now, within=timedelta(minutes=30), tz=ZURICH)
    assert len(up) == 1
    assert up[0].medicine_name == "Aspirin" and up[0].time_hhmm == "09:15"
    assert up[0].person_name == "Mario"
    db.close()


def test_upcoming_skips_already_taken(tmp_path: Path) -> None:
    """Taken in the current slot removes it from the wake list (uses real clock for event stamps)."""
    db = Database(tmp_path / "t.db")
    now = datetime.now(ZURICH).replace(second=0, microsecond=0)
    soon = (now + timedelta(minutes=10)).strftime("%H:%M")
    asp = db.add_medicine("Aspirin")
    db.add_schedule(asp.id, "*", soon)
    assert len(find_upcoming_doses(db, now, within=timedelta(minutes=30), tz=ZURICH)) == 1
    db.record_transition(asp.id, DoseState.DUE)
    db.record_transition(asp.id, DoseState.TAKEN)
    assert find_upcoming_doses(db, now, within=timedelta(minutes=30), tz=ZURICH) == []
    db.close()


def test_build_wake_prompt_lists_real_doses_only() -> None:
    from medicine_buddy.scheduler import UpcomingDose

    empty = build_wake_prompt([])
    assert "Nothing is scheduled" in empty
    assert "wake" in empty.lower() or "awake" in empty.lower()

    one = build_wake_prompt([
        UpcomingDose(1, 1, "Aspirin", "09:15", "Mario"),
    ])
    assert "Mario's Aspirin" in one and "9:15 AM" in one
    assert "Vitamin" not in one

    two = build_wake_prompt([
        UpcomingDose(1, 1, "Aspirin", "09:15", "Mario"),
        UpcomingDose(2, 2, "Vitamin D", "09:25", None),
    ])
    assert "Aspirin" in two and "Vitamin D" in two


def test_deliver_retries_until_speak_ok(tmp_path: Path) -> None:
    cat = MedicineCatalog({"Aspirin": []})
    svc = MedicineBuddyService(
        tmp_path / "w.db", AppConfig(dry_run=True), cat, VoiceGuard(cat, enabled=False),
    )
    svc.db.add_schedule(svc.db.add_medicine("Aspirin").id, "*", "09:15")
    # Freeze "now" via monkeypatch on service.now / tz
    svc.now = lambda: at(9, 0)  # type: ignore[method-assign]
    svc.tz = lambda: ZURICH  # type: ignore[method-assign]
    speaker = Speaker(fail_times=2)
    stop = threading.Event()
    out = deliver_wake_greeting(
        svc, speaker, within=timedelta(minutes=30), stop_event=stop,
        retry_every=0.01, give_up_after=1.0, now=at(9, 0),
    )
    assert out is not None and "Aspirin" in out
    assert speaker.calls == 3 and len(speaker.said) == 1
    svc.close()


def test_deliver_gives_up_when_stopped(tmp_path: Path) -> None:
    cat = MedicineCatalog({"Aspirin": []})
    svc = MedicineBuddyService(
        tmp_path / "s.db", AppConfig(dry_run=True), cat, VoiceGuard(cat, enabled=False),
    )
    speaker = Speaker(fail_times=100)
    stop = threading.Event()
    stop.set()
    assert deliver_wake_greeting(
        svc, speaker, stop_event=stop, retry_every=0.01, give_up_after=1.0, now=at(9, 0),
    ) is None
    svc.close()
