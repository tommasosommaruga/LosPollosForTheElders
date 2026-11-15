"""Patient story listening: longer VAD silence only while listen mode is on."""

from __future__ import annotations

from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.listen_mode import LISTEN_SILENCE_MS, NORMAL_SILENCE_MS


@pytest.fixture
def svc(tmp_path: Path):
    s = MedicineBuddyService(
        tmp_path / "l.db", AppConfig(dry_run=True), MedicineCatalog({"Aspirin": []}),
    )
    yield s
    s.close()


def test_default_is_normal_silence(svc: MedicineBuddyService) -> None:
    assert svc.is_listen_patient() is False
    assert svc.listen_silence_ms() == NORMAL_SILENCE_MS
    status = svc.listen_mode({})
    assert status["enabled"] is False and status["silence_ms"] == NORMAL_SILENCE_MS


def test_start_and_stop_via_action(svc: MedicineBuddyService) -> None:
    on = svc.listen_mode({"action": "start"})
    assert on["enabled"] is True and on["silence_ms"] == LISTEN_SILENCE_MS
    assert svc.is_listen_patient() is True
    # Live VAD apply is intentionally off (session.update muted TTS); flag still toggles.
    assert on.get("vad_applied") is False

    off = svc.listen_mode({"action": "stop"})
    assert off["enabled"] is False and off["silence_ms"] == NORMAL_SILENCE_MS
    assert svc.is_listen_patient() is False


def test_enabled_flag_and_timeout_expiry(svc: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr("medicine_buddy.listen_mode.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr("medicine_buddy.listen_mode.LISTEN_TIMEOUT_S", 10.0)

    svc.set_listen_patient(True)
    assert svc.is_listen_patient() is True
    clock["t"] += 11.0
    assert svc.is_listen_patient() is False


def test_save_memory_clears_listen_mode(svc: MedicineBuddyService) -> None:
    svc.settings_require_passphrases({"enabled": False})
    svc.register_person({
        "name": "Mario", "role": "person",
        "passphrase": "old fishing boat", "passphrase_repeat": "old fishing boat",
    })
    svc.set_listen_patient(True)
    assert svc.is_listen_patient() is True
    r = svc.save_memory({
        "person": "Mario", "kind": "story", "title": "The lake",
        "text": "We swam until sunset and walked home singing.",
    })
    assert r.get("verified")
    assert svc.is_listen_patient() is False


def test_listen_patiently_tool_file_exists() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy" / "conversation" / "external_tools" / "listen_patiently.py"
    )
    text = path.read_text(encoding="utf-8")
    assert "listen_patiently" in text and "needs_response" in text


def test_profile_lists_listen_and_expression_tools() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy" / "conversation" / "external_profiles"
        / "medicine_caregiver" / "profile.md"
    )
    text = path.read_text(encoding="utf-8")
    for name in ("listen_patiently", "play_emotion", "dance", "stop_dance", "stop_emotion", "sweep_look"):
        assert name in text
