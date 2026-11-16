"""Demo smooth: ease catalog/schedule gates without deleting the code paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.voice_guard import VoiceGuard


@pytest.fixture
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedicineBuddyService:
    monkeypatch.setenv("MEDICINE_BUDDY_DEMO_SMOOTH", "1")
    monkeypatch.delenv("MEDICINE_BUDDY_REQUIRE_PASSPHRASES", raising=False)
    cat = MedicineCatalog({"Aspirin": [], "Paracetamol": ["paracetamol", "parol"]})
    return MedicineBuddyService(
        tmp_path / "demo.db",
        AppConfig(dry_run=True, tray_labels=["ASPIRIN"]),
        cat,
        VoiceGuard(cat, enabled=False),
    )


def test_demo_smooth_default_on(svc: MedicineBuddyService) -> None:
    assert svc.demo_smooth() is True
    assert svc.health()["demo_smooth"] is True


def test_demo_smooth_can_turn_off(svc: MedicineBuddyService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDICINE_BUDDY_DEMO_SMOOTH", "0")
    assert svc.demo_smooth() is False
    off = svc.settings_demo_smooth({"enabled": False})
    assert off["enabled"] is False  # env still wins when set
    monkeypatch.delenv("MEDICINE_BUDDY_DEMO_SMOOTH")
    svc.settings_demo_smooth({"enabled": False})
    assert svc.demo_smooth() is False


def _enrol(svc: MedicineBuddyService, name: str = "Tommaso") -> None:
    phrase = "misty lantern pumpkin"
    out = svc.register_person({
        "name": name,
        "role": "person",
        "passphrase": phrase,
        "passphrase_repeat": phrase,
    })
    assert out.get("status") == "ok", out


def test_dispense_without_schedule_auto_enrols(svc: MedicineBuddyService) -> None:
    _enrol(svc)
    person = svc.db.get_person_by_name("Tommaso")
    assert person is not None
    assert svc.db.get_medicine_by_name("Aspirin", person.id) is None
    out = svc.run_dose({
        "person": "Tommaso",
        "medicine_name": "Aspirin",
        "confirmed": True,
        "stop_after_placed": True,
    })
    assert out.get("final_state") == "Placed", out
    assert svc.db.get_medicine_by_name("Aspirin", person.id) is not None


def test_unknown_label_uses_dispense_not_move_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free-form labels must hit /v1/dispense (medicine.name), like the working arm curl."""
    from medicine_buddy import api as api_mod
    from medicine_buddy.arm_protocol import DispenseResult
    from medicine_buddy.http_arm import HttpArmClient

    class _OkDispense(HttpArmClient):
        def probe_reachable(self, timeout_s: float = 2.0) -> bool:
            return True

        def dispense(self, **kwargs):
            self.last_name = kwargs.get("medicine_name")
            return DispenseResult(
                request_id="r1", ok=True, final_state="placed",
                detect_detail="looks_like:small bottle", place_verified=False,
            )

    monkeypatch.setenv("MEDICINE_BUDDY_DEMO_SMOOTH", "1")
    monkeypatch.setattr(api_mod, "HttpArmClient", _OkDispense)
    cat = MedicineCatalog({"Aspirin": []})
    svc = MedicineBuddyService(
        tmp_path / "bottle.db",
        AppConfig(dry_run=False, arm_url="http://arm.test:9090"),
        cat,
        VoiceGuard(cat, enabled=False),
    )
    ask = svc.run_dose({"medicine_name": "small bottle", "confirmed": False})
    assert ask.get("status") == "needs_confirm"
    assert ask.get("medicine", {}).get("name") == "small bottle"
    done = svc.run_dose({"medicine_name": "small bottle", "confirmed": True})
    assert done.get("final_state") == "Placed", done
    assert done.get("medicine", {}).get("name") == "small bottle"
    assert done.get("arm_remote") is True
    assert "object" not in done


def test_move_object_redirects_to_dispense(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medicine_buddy import api as api_mod
    from medicine_buddy.arm_protocol import DispenseResult
    from medicine_buddy.http_arm import HttpArmClient

    class _OkDispense(HttpArmClient):
        def probe_reachable(self, timeout_s: float = 2.0) -> bool:
            return True

        def dispense(self, **kwargs):
            return DispenseResult(
                request_id="r1", ok=True, final_state="placed",
                detect_detail="ok", place_verified=False,
            )

        def move_object(self, **kwargs):  # pragma: no cover — must not be used
            raise AssertionError("move_object must not be called; use dispense")

    monkeypatch.setenv("MEDICINE_BUDDY_DEMO_SMOOTH", "1")
    monkeypatch.setattr(api_mod, "HttpArmClient", _OkDispense)
    cat = MedicineCatalog({"Aspirin": []})
    svc = MedicineBuddyService(
        tmp_path / "redir.db",
        AppConfig(dry_run=False, arm_url="http://arm.test:9090"),
        cat,
        VoiceGuard(cat, enabled=False),
    )
    ask = svc.move_object({"object_label": "small bottle", "confirmed": False})
    assert ask.get("status") == "needs_confirm"
    assert ask.get("medicine", {}).get("name") == "small bottle"
    done = svc.move_object({"object_label": "small bottle", "confirmed": True})
    assert done.get("final_state") == "Placed", done
    assert done.get("medicine", {}).get("name") == "small bottle"


def test_strict_mode_still_requires_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEDICINE_BUDDY_DEMO_SMOOTH", "0")
    cat = MedicineCatalog({"Aspirin": []})
    svc = MedicineBuddyService(
        tmp_path / "strict.db",
        AppConfig(dry_run=True),
        cat,
        VoiceGuard(cat, enabled=False),
    )
    _enrol(svc)
    err = svc.run_dose({
        "person": "Tommaso",
        "medicine_name": "Aspirin",
        "confirmed": True,
    })
    assert "error" in err
    assert "schedule" in (err.get("error") or "").lower() or "reminders" in (err.get("speak_hint") or "").lower()
