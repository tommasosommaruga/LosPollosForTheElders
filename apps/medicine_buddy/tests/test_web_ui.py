"""In-app pages (/medicine on the app's web server): sign-in, family settings, debug."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from medicine_buddy.api import MedicineBuddyService  # noqa: E402
from medicine_buddy.catalog import MedicineCatalog  # noqa: E402
from medicine_buddy.config import AppConfig  # noqa: E402
from medicine_buddy.web_ui import MAX_FAILURES, attach_web_ui  # noqa: E402

CARE = "purple mountain sunrise"
MARIO = "old fishing boat"


@pytest.fixture
def svc(tmp_path: Path):
    s = MedicineBuddyService(tmp_path / "w.db", AppConfig(dry_run=True), MedicineCatalog({"Aspirin": []}))
    yield s
    s.close()


@pytest.fixture
def client(svc: MedicineBuddyService):
    app = FastAPI()
    attach_web_ui(app, svc)
    with TestClient(app, base_url="http://robot.local:7860") as c:
        yield c


def _family(svc: MedicineBuddyService) -> None:
    svc.register_person({"name": "Anna", "role": "caregiver", "passphrase": CARE, "passphrase_repeat": CARE})
    svc.register_person({"name": "Mario", "role": "person", "passphrase": MARIO, "passphrase_repeat": MARIO})
    svc.add_schedule({"person": "Mario", "name": "Aspirin", "weekdays": "daily", "time": "09:00", "passphrase": MARIO})


def _login(client: TestClient, phrase: str = CARE) -> None:
    r = client.post("/medicine/login", json={"passphrase": phrase})
    assert r.status_code == 200, r.text


def _token(client: TestClient, path: str) -> str:
    page = client.get(path).text
    return json.loads(re.search(r"const TOKEN = (\"[^\"]*\");", page).group(1))


def test_open_while_nobody_registered(client: TestClient) -> None:
    r = client.get("/medicine")
    assert r.status_code == 200 and "Medicine Buddy" in r.text and "sign in" not in r.text.lower()


def test_open_access_shows_dashboard_without_login(svc, client: TestClient) -> None:
    """Default: no caregiver login — Dashboard / Database / Family are open."""
    _family(svc)
    dash = client.get("/medicine").text
    assert "Aspirin" in dash and "Mario" in dash
    assert "sign in" not in dash.lower()
    assert client.get("/medicine/family").status_code == 200
    assert client.get("/medicine/data").status_code == 200
    for secret in (CARE, MARIO):
        assert secret not in dash


def test_optional_ui_login_accepts_any_family_member(svc, client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MEDICINE_BUDDY_REQUIRE_UI_LOGIN", "1")
    _family(svc)
    page = client.get("/medicine").text
    assert "registered person's passphrase" in page.lower() or "family passphrase" in page.lower()
    assert "Aspirin" not in page
    assert client.get("/medicine/debug/data").status_code == 401
    assert client.post("/medicine/login", json={"passphrase": MARIO}).status_code == 200  # not caregiver-only
    dash = client.get("/medicine").text
    assert "Aspirin" in dash and "Mario" in dash


def test_cookie_is_locked_down(svc, client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MEDICINE_BUDDY_REQUIRE_UI_LOGIN", "1")
    _family(svc)
    r = client.post("/medicine/login", json={"passphrase": CARE})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/medicine" in cookie


def test_guessing_is_rate_limited(svc, client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MEDICINE_BUDDY_REQUIRE_UI_LOGIN", "1")
    _family(svc)
    for _ in range(MAX_FAILURES):
        assert client.post("/medicine/login", json={"passphrase": "wrong guess here"}).status_code == 401
    r = client.post("/medicine/login", json={"passphrase": CARE})     # even the right one, for now
    assert r.status_code == 429 and "Too many" in r.json()["error"]
    assert any(b["action"] == "web login" for b in svc.security()["blocked"])


def test_family_settings_need_session_token(svc, client: TestClient) -> None:
    _family(svc)
    body = {"name": "Mario", "current": MARIO, "new": "blue garden kettle", "new_repeat": "blue garden kettle"}
    assert client.post("/medicine/settings/passphrase", json=body).status_code == 403          # no token
    assert client.post("/medicine/settings/passphrase", json=body,
                       headers={"X-Family-Token": "guess"}).status_code == 403
    token = _token(client, "/medicine/family")
    r = client.post("/medicine/settings/passphrase", json=body, headers={"X-Family-Token": token})
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_other_websites_refused(svc, client: TestClient) -> None:
    _family(svc)
    token = _token(client, "/medicine/family")
    evil = {"Origin": "https://evil.example", "X-Family-Token": token}
    assert client.post("/medicine/settings/suggest", json={}, headers=evil).status_code == 403
    assert client.post("/medicine/debug/toggle", json={"enabled": True}, headers=evil).status_code == 403
    assert client.post("/medicine/login", json={"passphrase": CARE},
                       headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_debug_switch_and_snapshot(svc, client: TestClient) -> None:
    _family(svc)
    svc.voice_heard({"text": f"my passphrase is {MARIO}"})          # spoken passphrase: must never be shown
    svc.guard.authenticated()      # sentences are checked when something needs a passphrase; do that now
    token = _token(client, "/medicine/debug")
    assert svc.debug_enabled is False
    r = client.post("/medicine/debug/toggle", json={"enabled": True}, headers={"X-Family-Token": token})
    assert r.json() == {"debug": True} and svc.debug_enabled
    assert logging.getLogger("medicine_buddy").level == logging.DEBUG

    snap = client.get("/medicine/debug/data").json()
    assert snap["debug"] is True and snap["data"]["people"] == 2 and snap["data"]["active_schedules"] == 1
    assert [u["name"] for u in snap["voice_guard"]["unlocked_people"]] == ["Mario"]
    text = json.dumps(snap)
    for secret in (CARE, MARIO, "fishing"):
        assert secret not in text

    client.post("/medicine/debug/toggle", json={"enabled": False}, headers={"X-Family-Token": token})
    assert svc.debug_enabled is False and logging.getLogger("medicine_buddy").level == logging.INFO


def test_request_log_keeps_http_status(svc: MedicineBuddyService) -> None:
    svc.record_request("POST", "/schedules", 400, 3.2, {"status": "needs_passphrase_setup", "error": "x"})
    entry = svc.requests[-1]
    assert entry["status"] == 400 and entry["result"] == "needs_passphrase_setup"


def test_logout_only_matters_when_ui_login_required(svc, client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MEDICINE_BUDDY_REQUIRE_UI_LOGIN", "1")
    _family(svc)
    _login(client)
    assert client.get("/medicine/debug/data").status_code == 200
    client.get("/medicine/logout")
    assert client.get("/medicine/debug/data").status_code == 401


def test_starting_page_instead_of_404() -> None:
    app = FastAPI()
    attach_web_ui(app, lambda: None)   # service not ready yet
    with TestClient(app) as c:
        r = c.get("/medicine")
        assert r.status_code == 503 and "starting" in r.text
        assert c.get("/medicine/debug/data").status_code == 503


def test_conversation_page_shows_text_and_hides_passphrases(svc, client: TestClient) -> None:
    _family(svc)
    svc.record_transcript("user", "what medicine should I take")
    svc.record_transcript("assistant", "You take Aspirin at nine.")
    svc.record_transcript("user", f"my passphrase is {MARIO}")
    page = client.get("/medicine/conversation")
    assert page.status_code == 200 and "Conversation" in page.text
    texts = []
    for _ in range(3):                       # unchecked lines show "..." until checked (few per request)
        d = client.get("/medicine/conversation/data").json()
        texts = [(ln["who"], ln["text"]) for ln in d["lines"]]
    assert ("person", "what medicine should I take") in texts
    assert ("reachy", "You take Aspirin at nine.") in texts
    assert ("person", "[passphrase hidden]") in texts
    assert MARIO not in json.dumps(d) and MARIO not in json.dumps(svc._tlines.copy().__repr__())
    tok = _token(client, "/medicine/conversation")
    assert client.post("/medicine/conversation/clear").status_code == 403            # needs the page token
    assert client.post("/medicine/conversation/clear", headers={"X-Family-Token": tok}).status_code == 200
    assert client.get("/medicine/conversation/data").json()["lines"] == []


def test_database_page_read_only_no_secrets(svc, client: TestClient) -> None:
    _family(svc)
    assert "Database" in client.get("/medicine/data").text
    d = client.get("/medicine/data/tables").json()
    assert client.get("/medicine/data/tables").status_code == 200
    names = {t["name"] for t in d["tables"]}
    assert {"people", "medicines", "schedules", "dose_events", "memories", "app_settings"} <= names
    blob = json.dumps(d).lower()
    assert "pass_hash" not in blob and "pass_salt" not in blob and CARE not in blob and MARIO not in blob
    sched = next(t for t in d["tables"] if t["name"] == "schedules")["rows"]
    assert any(r[1] == "Mario" and r[2] == "Aspirin" for r in sched)
