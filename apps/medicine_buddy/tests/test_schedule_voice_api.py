"""Voice-facing schedule management: add / update / delete with read-back confirmation,
catalog enforcement, and the HTTP server + localhost status page."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService, make_handler
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig


@pytest.fixture
def service(tmp_path: Path):
    svc = MedicineBuddyService(
        tmp_path / "voice.db",
        AppConfig(dry_run=True, tray_labels=["ASPIRIN", "VITAMIN_D"]),
        MedicineCatalog({"Aspirin": ["asprin"], "Vitamin D": ["vit d"], "Metformin": []}),
    )
    yield svc
    svc.close()


# ---------------------------------------------------------------- add

def test_add_confirms_setup_correctly(service: MedicineBuddyService) -> None:
    r = service.add_schedule({"name": "asprin", "weekdays": "daily", "time": "9 AM"})
    assert r["status"] == "ok" and r["verified"] is True
    assert r["medicine"]["name"] == "Aspirin"  # recognized from the alias
    assert r["schedule"]["time_hhmm"] == "09:00"
    assert r["speak_hint"] == "Done. Aspirin is set up correctly: every day at 9 AM."


def test_add_rejects_medicine_not_in_catalog(service: MedicineBuddyService) -> None:
    r = service.add_schedule({"name": "chocolate", "weekdays": "daily", "time": "09:00"})
    assert "error" in r
    assert r["allowed_medicines"] == ["Aspirin", "Metformin", "Vitamin D"]
    assert "not on the medicine list" in r["speak_hint"]
    assert service.db.list_medicines() == []  # nothing stored


def test_add_twice_does_not_duplicate(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    r = service.add_schedule({"name": "aspirin", "weekdays": "every day", "time": "9:00"})
    assert r["already_existed"] is True
    assert len(service.db.list_active_schedules()) == 1


def test_add_bad_time_is_explained(service: MedicineBuddyService) -> None:
    r = service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "lunchtime"})
    assert "error" in r and "speak_hint" in r
    assert service.db.list_active_schedules() == []


# ---------------------------------------------------------------- update

def test_update_time_confirms_and_persists(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    r = service.update_schedule({"name": "aspirin", "new_time": "8 PM"})
    assert r["verified"] is True
    assert r["speak_hint"] == "Done. Aspirin is now set up correctly: every day at 8 PM."
    [s] = service.db.list_active_schedules()
    assert (s.time_hhmm, s.weekdays) == ("20:00", "*")


def test_update_days(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Vitamin D", "weekdays": "daily", "time": "13:00"})
    r = service.update_schedule({"name": "vit d", "new_weekdays": "weekdays"})
    assert "on weekdays at 1 PM" in r["speak_hint"]


def test_update_with_several_schedules_asks_which(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "21:00"})
    ask = service.update_schedule({"name": "Aspirin", "new_time": "10:00"})
    assert "Which one" in ask["speak_hint"]
    r = service.update_schedule({"name": "Aspirin", "time": "9 PM", "new_time": "22:00"})
    assert r["verified"] is True
    times = sorted(s.time_hhmm for s in service.db.list_active_schedules())
    assert times == ["09:00", "22:00"]


def test_update_unknown_or_unscheduled(service: MedicineBuddyService) -> None:
    assert "not on the medicine list" in service.update_schedule({"name": "candy", "new_time": "9"})["speak_hint"]
    r = service.update_schedule({"name": "Metformin", "new_time": "9"})
    assert "don't have any reminders" in r["speak_hint"]


def test_update_needs_something_to_change(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    assert "error" in service.update_schedule({"name": "Aspirin"})


# ---------------------------------------------------------------- delete

def test_delete_requires_confirmation(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    ask = service.delete_schedule({"name": "Aspirin"})
    assert ask["status"] == "needs_confirm"
    assert len(service.db.list_active_schedules()) == 1  # nothing removed yet

    done = service.delete_schedule({"name": "Aspirin", "confirmed": True})
    assert done["verified"] is True
    assert "You have no more reminders for Aspirin" in done["speak_hint"]
    assert service.db.list_active_schedules() == []
    assert service.list_medicines()["medicines"] == []


def test_delete_one_of_several(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "21:00"})
    choice = service.delete_schedule({"name": "Aspirin", "confirmed": True})
    assert choice["status"] == "needs_choice"
    r = service.delete_schedule({"name": "Aspirin", "time": "9 AM", "confirmed": True})
    assert "You still have Aspirin every day at 9 PM" in r["speak_hint"]


def test_delete_all(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "21:00"})
    r = service.delete_schedule({"name": "Aspirin", "all": True, "confirmed": True})
    assert len(r["removed"]) == 2 and service.db.list_active_schedules() == []


def test_deleted_schedule_can_be_added_again(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    service.delete_schedule({"name": "Aspirin", "confirmed": True})
    r = service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    assert r["verified"] is True and not r.get("already_existed")


def test_list_is_speakable(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Aspirin", "weekdays": "Mon, Wed", "time": "08:30"})
    hint = service.list_medicines()["speak_hint"]
    assert hint == "Your medicines: Aspirin on Monday and Wednesday at 8:30 AM."
    assert "*" not in hint


def test_dispense_understands_misspelled_name(service: MedicineBuddyService) -> None:
    service.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})
    r = service.run_dose({"medicine_name": "asprin", "confirmed": True})
    assert r["final_state"] == "Placed"


# ---------------------------------------------------------------- HTTP server + page

@pytest.fixture
def server(service: MedicineBuddyService):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _post(url: str, body: bytes, ctype: str = "application/json") -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_add_update_delete_and_page(server: str) -> None:
    code, r = _post(f"{server}/schedules", json.dumps({"name": "Aspirin", "weekdays": "daily", "time": "9 AM"}).encode())
    assert code == 200 and r["verified"]
    code, r = _post(f"{server}/schedules/update", json.dumps({"name": "Aspirin", "new_time": "10:15"}).encode())
    assert code == 200 and "10:15 AM" in r["speak_hint"]

    with urllib.request.urlopen(f"{server}/", timeout=5) as page:
        assert page.headers["Content-Type"].startswith("text/html")
        text = page.read().decode()
    assert "Medicine Buddy" in text and "Aspirin" in text and "10:15 AM" in text and "Vitamin D" in text

    code, r = _post(f"{server}/schedules/delete", json.dumps({"name": "Aspirin", "confirmed": True}).encode())
    assert code == 200 and r["verified"]
    with urllib.request.urlopen(f"{server}/", timeout=5) as page:
        assert "No schedules yet" in page.read().decode()


def test_http_bad_requests_get_answers(server: str) -> None:
    assert _post(f"{server}/schedules", b"{not json")[0] == 400
    assert _post(f"{server}/schedules", b"[1, 2]")[0] == 400  # used to crash the handler
    code, r = _post(f"{server}/schedules", json.dumps({"name": "chocolate", "time": "9"}).encode())
    assert code == 400 and "allowed_medicines" in r
    assert _post(f"{server}/nope", b"{}")[0] == 404


def test_page_escapes_html(service: MedicineBuddyService, server: str) -> None:
    med = service.db.add_medicine("<script>alert(1)</script>")  # bypass catalog on purpose
    service.db.add_schedule(med.id, "*", "09:00")
    with urllib.request.urlopen(f"{server}/", timeout=5) as page:
        text = page.read().decode()
    assert "<script>alert" not in text and "&lt;script&gt;" in text
