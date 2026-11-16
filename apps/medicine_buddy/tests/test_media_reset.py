"""Reset-microphone helper (daemon volume + clear audio)."""

from __future__ import annotations

from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy import media_reset as media_mod
from medicine_buddy.voice_guard import VoiceGuard


@pytest.fixture
def svc(tmp_path: Path) -> MedicineBuddyService:
    cat = MedicineCatalog({"Aspirin": []})
    return MedicineBuddyService(
        tmp_path / "mic.db",
        AppConfig(dry_run=True),
        cat,
        VoiceGuard(cat, enabled=False),
    )


def test_reset_microphone_success(svc: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_http(method, url, body=None, *, timeout_s):
        calls.append(f"{method} {url}")
        if url.endswith("/clear_incoming_audio"):
            return 200, {"ok": True}, ""
        if url.endswith("/microphone/set"):
            assert body == {"volume": 100}
            return 200, {"volume": 100}, ""
        if url.endswith("/microphone/current"):
            return 200, {"volume": 100, "device": "Reachy Mini Audio"}, ""
        return 404, None, "missing"

    monkeypatch.setattr(media_mod, "_http_json", fake_http)
    out = svc.reset_microphone({"volume": 100})
    assert out["ok"] is True
    assert out["current_volume"] == 100
    assert any("clear_incoming_audio" in c for c in calls)
    assert any("microphone/set" in c for c in calls)


def test_reset_microphone_retries_then_fails(
    svc: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_http(method, url, body=None, *, timeout_s):
        if "clear" in url:
            return 200, {}, ""
        if url.endswith("/set"):
            return 0, None, "timed out"
        if url.endswith("/current"):
            return 200, {"volume": 10}, ""
        return 0, None, "fail"

    monkeypatch.setattr(media_mod, "_http_json", fake_http)
    out = svc.reset_microphone({})
    assert out["ok"] is False
    assert out["status"] == "error"
    assert sum(1 for s in out["steps"] if s["step"].startswith("microphone_set")) == 3
