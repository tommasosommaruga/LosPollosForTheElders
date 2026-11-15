"""Story listen session: name first, keep spoken words for save_memory (no VAD patch)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.auth import make_secret
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.voice_guard import VoiceGuard


@pytest.fixture
def svc(tmp_path: Path) -> MedicineBuddyService:
    catalog = MedicineCatalog({"Aspirin": []})
    return MedicineBuddyService(
        tmp_path / "t.db", AppConfig(dry_run=True), catalog, VoiceGuard(catalog, enabled=False),
    )


def test_listen_mode_start_stop_tracks_person(svc: MedicineBuddyService) -> None:
    assert svc.is_listen_patient() is False
    on = svc.listen_mode({"action": "start", "person": "Adarsh"})
    assert on["enabled"] is True
    assert on.get("vad_applied") is False
    assert on["person"] == "Adarsh"
    assert svc.is_listen_patient() is True
    assert svc.story_session_person() == "Adarsh"
    assert svc.story_session_started_at() is not None
    off = svc.listen_mode({"action": "stop"})
    assert off["enabled"] is False
    assert svc.is_listen_patient() is False
    assert svc.story_session_person() is None


def test_save_memory_uses_session_person_and_transcript(svc: MedicineBuddyService) -> None:
    svc.db.add_person("Adarsh", "person", make_secret("old fishing boat"))
    svc.listen_mode({"action": "start", "person": "Adarsh"})
    svc.record_transcript(
        "user",
        "My grandmother made poha every Sunday with mustard seeds and lemon.",
    )
    svc.record_transcript("user", "yes please save it")
    result = svc.save_memory({
        "kind": "story",
        "title": "Sunday poha",
        "text": "short summary",  # too short vs transcript
    })
    assert result["status"] == "ok"
    assert result["memory"]["person"] == "Adarsh"
    assert "poha" in result["memory"]["text"].casefold()
    assert result["source"] == "transcript"
    assert svc.is_listen_patient() is False


def test_story_text_prefers_lines_after_session_start(svc: MedicineBuddyService) -> None:
    svc.record_transcript("user", "Earlier chat about the weather today was boring filler.")
    time.sleep(0.05)
    svc.listen_mode({"action": "start", "person": "Tommaso"})
    time.sleep(0.05)
    svc.record_transcript(
        "user",
        "Every summer we picked figs at Nonna's village under the mulberry tree.",
    )
    body = svc.recent_user_story_text()
    assert "figs" in body.casefold()
    assert "weather" not in body.casefold()


def test_listen_patiently_in_profile_tools() -> None:
    profile = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy" / "conversation" / "external_profiles"
        / "medicine_caregiver" / "profile.md"
    )
    text = profile.read_text(encoding="utf-8")
    head = text.split("+++")[1] if text.count("+++") >= 2 else text
    assert "listen_patiently" in head
    assert "Name first" in text or "name first" in text.casefold()


def test_listen_patiently_tool_posts_person() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy" / "conversation" / "external_tools" / "listen_patiently.py"
    )
    text = path.read_text(encoding="utf-8")
    assert "person" in text and "action=start" in text.casefold() or "action" in text
