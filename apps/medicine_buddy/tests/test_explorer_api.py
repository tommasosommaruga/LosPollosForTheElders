"""Auth-gated /medicine/api explorer endpoints + dual-write activity privacy."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from medicine_buddy.api import MedicineBuddyService  # noqa: E402
from medicine_buddy.catalog import MedicineCatalog  # noqa: E402
from medicine_buddy.config import AppConfig  # noqa: E402
from medicine_buddy.explorer_api import assert_safe_payload  # noqa: E402
from medicine_buddy.states import DoseState  # noqa: E402
from medicine_buddy.web_ui import attach_web_ui  # noqa: E402

CARE = "purple mountain sunrise"
MARIO = "old fishing boat"
MARIA = "quiet garden path"


@pytest.fixture
def svc(tmp_path: Path):
    s = MedicineBuddyService(tmp_path / "ex.db", AppConfig(dry_run=True), MedicineCatalog({"Aspirin": []}))
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
    svc.register_person({"name": "Maria", "role": "person", "passphrase": MARIA, "passphrase_repeat": MARIA})
    svc.add_schedule({"person": "Mario", "name": "Aspirin", "weekdays": "daily", "time": "09:00", "passphrase": MARIO})
    svc.save_memory({
        "person": "Mario", "kind": "story", "title": "Lake day",
        "text": "We swam in the lake near the village.", "passphrase": MARIO,
    })
    svc.save_memory({
        "person": "Maria", "kind": "recipe", "title": "Bean soup",
        "text": "Add rosemary and beans.", "passphrase": MARIA,
    })


def _login(client: TestClient, phrase: str = CARE) -> None:
    r = client.post("/medicine/login", json={"passphrase": phrase})
    assert r.status_code == 200, r.text


def _seed_doses(svc: MedicineBuddyService) -> None:
    meds = svc.db.list_medicines()
    asp = next(m for m in meds if m.name == "Aspirin")
    svc.db.record_transition(asp.id, DoseState.DUE)
    svc.db.record_transition(asp.id, DoseState.REMINDED)
    svc.db.record_transition(asp.id, DoseState.TAKEN)


def test_explorer_apis_open_by_default(svc, client: TestClient) -> None:
    _family(svc)
    for path in (
        "/medicine/api/activity",
        "/medicine/api/search?q=Mario",
        "/medicine/api/people",
        "/medicine/api/reports/adherence",
        "/medicine/api/agent",
    ):
        assert client.get(path).status_code == 200
    assert client.get("/medicine/history").status_code == 200


def test_explorer_apis_require_sign_in_when_locked(svc, client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MEDICINE_BUDDY_REQUIRE_UI_LOGIN", "1")
    _family(svc)
    for path in (
        "/medicine/api/activity",
        "/medicine/api/search?q=Mario",
        "/medicine/api/people",
        "/medicine/api/reports/adherence",
        "/medicine/api/agent",
    ):
        assert client.get(path).status_code == 401
    assert client.get("/medicine/history", follow_redirects=False).status_code == 303


def test_activity_dual_write_and_cursor(svc, client: TestClient) -> None:
    _family(svc)
    # dashboard track → activity_events (open UI, no login)
    client.get("/medicine")
    assert svc.db.count_activity_events() >= 1
    r = client.get("/medicine/api/activity?limit=20")
    assert r.status_code == 200
    data = r.json()
    assert_safe_payload(data)
    assert data["total"] >= 1 and data["items"]
    since = data["items"][0]["id"]
    svc.record_request("POST", "/schedules", 200, 5.0, {"status": "ok"}, via="api")
    cursor = client.get(f"/medicine/api/activity?since_id={since}&limit=50").json()
    assert all(i["id"] > since for i in cursor["items"])
    assert "pass_hash" not in json.dumps(data)
    assert CARE not in json.dumps(data)


def test_voice_heard_logs_length_not_text(svc: MedicineBuddyService) -> None:
    secret = "never store this utterance about purple mountain sunrise"
    out = svc.voice_heard({"text": secret})
    assert out["status"] == "ok"
    assert out["utterance_chars"] == len(secret)
    svc.record_request("POST", "/voice/heard", 200, 2.0, out, via="api")
    rows = svc.db.query_activity_events(path_prefix="/voice/heard", limit=5)
    assert rows and rows[0].utterance_chars == len(secret)
    blob = json.dumps([r.__dict__ for r in rows])
    assert secret not in blob
    assert "purple mountain" not in blob


def test_search_exact_person_and_stories(svc, client: TestClient) -> None:
    _family(svc)
    _login(client)
    mario = client.get("/medicine/api/search?q=Mario&kind=person").json()
    assert mario["total"] == 1 and mario["items"][0]["title"] == "Mario"
    # Maria must not be returned for exact Mario search
    assert all(i["title"] != "Maria" for i in mario["items"])
    bad = client.get("/medicine/api/search?person=Marioo")
    assert bad.status_code == 400
    lake = client.get("/medicine/api/search?q=lake&kind=memory").json()
    assert lake["total"] >= 1
    assert lake["items"][0]["person_name"] == "Mario"
    assert_safe_payload(lake)


def test_person_records_and_detail(svc, client: TestClient) -> None:
    _family(svc)
    _seed_doses(svc)
    _login(client)
    people = client.get("/medicine/api/people").json()["items"]
    mario = next(p for p in people if p["name"] == "Mario")
    assert "pass_hash" not in mario and "secret" not in mario
    dossier = client.get(f"/medicine/api/people/{mario['id']}/records")
    assert dossier.status_code == 200
    body = dossier.json()
    assert_safe_payload(body)
    assert body["person"]["name"] == "Mario"
    assert body["counts"]["memories"] >= 1
    assert client.get("/medicine/api/people/99999/records").status_code == 404

    mem_id = body["memories"][0]["id"]
    mem = client.get(f"/medicine/api/records/memory/{mem_id}").json()
    assert mem["title"] and "lake" in mem["text"].lower()
    dose_id = body["dose_events"][0]["id"]
    dose = client.get(f"/medicine/api/records/dose_event/{dose_id}").json()
    assert dose["timeline"] and dose["medicine_name"] == "Aspirin"

    page = client.get(f"/medicine/people/{mario['id']}")
    assert page.status_code == 200 and "Lake day" in page.text and CARE not in page.text
    hist = client.get("/medicine/history")
    assert hist.status_code == 200 and "Activity timeline" in hist.text


def test_adherence_and_agent(svc, client: TestClient) -> None:
    _family(svc)
    _seed_doses(svc)
    _login(client)
    people = client.get("/medicine/api/people").json()["items"]
    mario = next(p for p in people if p["name"] == "Mario")
    report = client.get(f"/medicine/api/reports/adherence?person={mario['id']}&days=7").json()
    assert report["taken"] >= 1 and report["person_name"] == "Mario"
    assert_safe_payload(report)
    agent = client.get("/medicine/api/agent").json()
    assert "uptime_s" in agent and "data" in agent
    assert_safe_payload(agent)
    assert "pass_hash" not in json.dumps(agent)


def test_history_nav_on_dashboard(svc, client: TestClient) -> None:
    _family(svc)
    _login(client)
    dash = client.get("/medicine").text
    assert "/medicine/history" in dash
    assert re.search(r">History<", dash)
