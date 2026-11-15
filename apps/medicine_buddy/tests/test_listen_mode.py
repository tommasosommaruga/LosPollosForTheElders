"""Patient VAD listening is disabled — stock Reachy Mini turn-taking only."""

from __future__ import annotations

from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.voice_guard import VoiceGuard


@pytest.fixture
def svc(tmp_path: Path) -> MedicineBuddyService:
    catalog = MedicineCatalog({"Aspirin": []})
    return MedicineBuddyService(
        tmp_path / "t.db", AppConfig(dry_run=True), catalog, VoiceGuard(catalog, enabled=False),
    )


def test_listen_mode_always_disabled(svc: MedicineBuddyService) -> None:
    assert svc.is_listen_patient() is False
    status = svc.listen_mode({})
    assert status["enabled"] is False
    assert status.get("disabled") is True


def test_start_stop_are_noops(svc: MedicineBuddyService) -> None:
    on = svc.listen_mode({"action": "start"})
    assert on["enabled"] is False and on.get("vad_applied") is False
    assert svc.is_listen_patient() is False
    off = svc.listen_mode({"action": "stop"})
    assert off["enabled"] is False
    assert svc.is_listen_patient() is False


def test_set_listen_patient_never_enables(svc: MedicineBuddyService) -> None:
    svc.set_listen_patient(True)
    assert svc.is_listen_patient() is False


def test_listen_patiently_not_in_profile_tools() -> None:
    profile = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy" / "conversation" / "external_profiles"
        / "medicine_caregiver" / "profile.md"
    )
    text = profile.read_text(encoding="utf-8")
    # Front-matter tool list must not invite the model to call it.
    head = text.split("+++")[1] if text.count("+++") >= 2 else text
    assert "listen_patiently" not in head
    for name in ("play_emotion", "dance", "stop_dance", "stop_emotion", "sweep_look", "save_memory"):
        assert name in head


def test_listen_patiently_tool_is_noop() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy" / "conversation" / "external_tools" / "listen_patiently.py"
    )
    text = path.read_text(encoding="utf-8")
    assert "disabled" in text.lower() and "stock" in text.lower()
