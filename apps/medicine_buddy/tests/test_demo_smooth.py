"""Demo smooth: ease catalog/schedule gates without deleting the code paths."""

from __future__ import annotations

import threading
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


def test_delete_finds_paracetamol_despite_empty_demo_shell(svc: MedicineBuddyService) -> None:
    """Regression: demo_smooth used to create an empty person-scoped Paracetamol on delete,
    then report 'no active schedule' while the real reminder lived on another row."""
    _enrol(svc)
    person = svc.db.get_person_by_name("Tommaso")
    assert person is not None
    real = svc.db.add_medicine("Paracetamol", None)  # unassigned / legacy bucket
    svc.db.add_schedule(real.id, "0,1,2,3,4,5,6", "09:00")
    svc.db.add_medicine("Paracetamol", person.id)  # empty shell (old bug leftover)

    ask = svc.delete_schedule({"name": "Paracetamol", "person": "Tommaso", "confirmed": False})
    assert ask.get("status") == "needs_confirm", ask
    assert "Paracetamol" in (ask.get("speak_hint") or "")

    done = svc.delete_schedule({"name": "Paracetamol", "person": "Tommaso", "confirmed": True})
    assert done.get("verified") is True, done
    assert svc.db.list_schedules_for_medicine(real.id) == []


def test_delete_does_not_auto_create_empty_medicine(svc: MedicineBuddyService) -> None:
    _enrol(svc)
    before = len(svc.db.list_medicines())
    out = svc.delete_schedule({"name": "Paracetamol", "person": "Tommaso", "confirmed": True})
    assert "error" in out
    assert "no active schedule" in out["error"] or "no schedule saved" in out["error"]
    assert len(svc.db.list_medicines()) == before


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
    done = svc.run_dose({
        "medicine_name": "small bottle", "confirmed": True, "wait": True,
    })
    assert done.get("final_state") == "Placed", done
    assert done.get("medicine", {}).get("name") == "small bottle"
    assert done.get("arm_remote") is True
    assert "object" not in done


def test_remote_dispense_returns_immediately_async(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time
    from medicine_buddy import api as api_mod
    from medicine_buddy.arm_protocol import DispenseResult
    from medicine_buddy.http_arm import HttpArmClient

    started = threading.Event()
    release = threading.Event()
    heard: list[str] = []

    class _SlowDispense(HttpArmClient):
        def probe_reachable(self, timeout_s: float = 2.0) -> bool:
            return True

        def dispense(self, **kwargs):
            started.set()
            release.wait(timeout=5)
            return DispenseResult(
                request_id="r1", ok=True, final_state="placed",
                detect_detail="ok", place_verified=False,
            )

    monkeypatch.setenv("MEDICINE_BUDDY_DEMO_SMOOTH", "1")
    monkeypatch.setenv("MEDICINE_BUDDY_ARM_ASYNC", "1")
    monkeypatch.setattr(api_mod, "HttpArmClient", _SlowDispense)
    cat = MedicineCatalog({"Aspirin": []})
    svc = MedicineBuddyService(
        tmp_path / "async.db",
        AppConfig(dry_run=False, arm_url="http://arm.test:9090"),
        cat,
        VoiceGuard(cat, enabled=False),
    )
    svc.progress_speaker = lambda text: heard.append(text) or True
    t0 = time.perf_counter()
    out = svc.run_dose({"medicine_name": "Aspirin", "confirmed": True})
    elapsed = time.perf_counter() - t0
    assert out.get("status") == "arm_working", out
    assert elapsed < 1.0, f"tool blocked for {elapsed:.2f}s"
    assert "keep talking" in (out.get("speak_hint") or "").lower()
    assert started.wait(timeout=2)
    assert svc.arm_job_busy() is True
    release.set()
    for _ in range(50):
        if not svc.arm_job_busy():
            break
        time.sleep(0.05)
    assert svc.arm_job_busy() is False
    assert any("Aspirin" in h or "bowl" in h.lower() for h in heard), heard


def test_release_arm_unlocks_busy_while_search_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time
    from medicine_buddy import api as api_mod
    from medicine_buddy.arm_protocol import DispenseResult
    from medicine_buddy.http_arm import HttpArmClient

    started = threading.Event()
    release = threading.Event()

    class _SlowDispense(HttpArmClient):
        def probe_reachable(self, timeout_s: float = 2.0) -> bool:
            return True

        def dispense(self, **kwargs):
            started.set()
            release.wait(timeout=5)
            return DispenseResult(
                request_id="r1", ok=False, final_state="failed",
                error="object gone", detect_detail="miss",
            )

    monkeypatch.setenv("MEDICINE_BUDDY_DEMO_SMOOTH", "1")
    monkeypatch.setenv("MEDICINE_BUDDY_ARM_ASYNC", "1")
    monkeypatch.setattr(api_mod, "HttpArmClient", _SlowDispense)
    cat = MedicineCatalog({"ice cream": []})
    svc = MedicineBuddyService(
        tmp_path / "release.db",
        AppConfig(dry_run=False, arm_url="http://arm.test:9090"),
        cat,
        VoiceGuard(cat, enabled=False),
    )
    first = svc.run_dose({"medicine_name": "ice cream", "confirmed": True})
    assert first.get("status") == "arm_working"
    assert started.wait(timeout=2)
    assert svc.arm_job_busy() is True
    busy = svc.run_dose({"medicine_name": "ice cream", "confirmed": True})
    assert busy.get("status") == "arm_busy"
    freed = svc.release_arm_job({})
    assert freed.get("ok") is True and freed.get("was_busy") is True
    assert svc.arm_job_busy() is False
    release.set()
    time.sleep(0.2)
    # Stale job must not re-lock after a force release.
    assert svc.arm_job_busy() is False


def test_dispense_timeout_becomes_failed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from medicine_buddy.http_arm import HttpArmClient

    arm = HttpArmClient("http://arm.test:9090", timeout_s=12.0)

    def _boom(path: str, body: str):
        raise RuntimeError(f"arm timed out after 12s at http://arm.test:9090{path}")

    monkeypatch.setattr(arm, "_post", _boom)
    result = arm.dispense(medicine_id=1, medicine_name="ice cream")
    assert result.ok is False and result.final_state == "failed"
    assert "timed out" in (result.error or "").lower()
    assert "ice cream" in (result.error or "").lower()


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
    done = svc.move_object({
        "object_label": "small bottle", "confirmed": True, "wait": True,
    })
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
