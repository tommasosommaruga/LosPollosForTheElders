from __future__ import annotations

from pathlib import Path

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.config import AppConfig


def test_schedule_dispense_adherence_via_api(tmp_path: Path) -> None:
    service = MedicineBuddyService(
        tmp_path / "api.db",
        AppConfig(dry_run=True, tray_labels=["ASPIRIN", "EMPTY"]),
    )
    saved = service.add_schedule(
        {"name": "Aspirin", "weekdays": "daily", "time": "09:00"}
    )
    assert saved["status"] == "ok"

    needs = service.run_dose({"medicine_name": "Aspirin", "confirmed": False})
    assert needs["status"] == "needs_confirm"

    placed = service.run_dose({"medicine_name": "Aspirin", "confirmed": True})
    assert placed["final_state"] == "Placed"

    taken = service.mark_adherence({"medicine_name": "Aspirin", "taken": True})
    assert taken["final_state"] == "Taken"
    service.close()
