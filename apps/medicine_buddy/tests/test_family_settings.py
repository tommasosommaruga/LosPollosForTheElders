"""Family settings page: typed passphrase changes, and protection of the local web service."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService, make_handler
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.settings import suggest_passphrase

CARE = "purple mountain sunrise"
MARIO = "old fishing boat"


@pytest.fixture
def svc(tmp_path: Path):
    s = MedicineBuddyService(tmp_path / "f.db", AppConfig(dry_run=True), MedicineCatalog({"Aspirin": []}))
    s.register_person({"name": "Anna", "role": "caregiver", "passphrase": CARE, "passphrase_repeat": CARE})
    s.register_person({"name": "Mario", "role": "person", "passphrase": MARIO, "passphrase_repeat": MARIO})
    yield s
    s.close()


# ---------------------------------------------------------------- the logic

def test_change_with_current_passphrase(svc: MedicineBuddyService) -> None:
    r = svc.settings_change_passphrase({"name": "Mario", "current": MARIO,
                                        "new": "blue garden kettle", "new_repeat": "blue garden kettle"})
    assert r["status"] == "ok"
    mario = svc.db.get_person_by_name("Mario")
    assert mario.secret.matches("blue garden kettle") and not mario.secret.matches(MARIO)


def test_caregiver_can_reset_a_forgotten_passphrase(svc: MedicineBuddyService) -> None:
    r = svc.settings_change_passphrase({"name": "Mario", "current": CARE,
                                        "new": "blue garden kettle", "new_repeat": "blue garden kettle"})
    assert r["status"] == "ok"


@pytest.mark.parametrize("body,needle", [
    ({"current": "wrong words here", "new": "blue garden kettle", "new_repeat": "blue garden kettle"}, "not Mario's"),
    ({"current": MARIO, "new": "blue garden kettle", "new_repeat": "blue garden kettel"}, "different"),
    ({"current": MARIO, "new": "short", "new_repeat": "short"}, "at least"),
    ({"current": MARIO, "new": CARE, "new_repeat": CARE}, "already used"),
    ({"current": MARIO, "new": MARIO, "new_repeat": MARIO}, "same as the old"),
])
def test_change_refused(svc: MedicineBuddyService, body: dict, needle: str) -> None:
    r = svc.settings_change_passphrase({"name": "Mario", **body})
    assert needle in r["error"]
    assert svc.db.get_person_by_name("Mario").secret.matches(MARIO)  # unchanged


def test_old_spoken_passphrase_stops_working_after_change(tmp_path: Path) -> None:
    from medicine_buddy.voice_guard import VoiceGuard

    cat = MedicineCatalog({"Aspirin": []})
    s = MedicineBuddyService(tmp_path / "g.db", AppConfig(dry_run=True), cat, VoiceGuard(cat, grace=0.0))
    s.voice_heard({"text": MARIO})
    s.voice_heard({"text": MARIO})
    s.register_person({"name": "Mario", "role": "person", "passphrase": MARIO, "passphrase_repeat": MARIO})
    s.voice_heard({"text": "add my aspirin at nine"})
    assert s.add_schedule({"person": "Mario", "name": "Aspirin", "weekdays": "daily", "time": "09:00"})["verified"]
    s.settings_change_passphrase({"name": "Mario", "current": MARIO, "new": "blue garden kettle",
                                  "new_repeat": "blue garden kettle"})
    s.voice_heard({"text": "add my aspirin at ten"})
    assert s.add_schedule({"person": "Mario", "name": "Aspirin", "weekdays": "daily", "time": "10:00"})["blocked"]
    s.close()


def test_suggestions_are_three_distinct_easy_words() -> None:
    seen = {suggest_passphrase() for _ in range(50)}
    assert len(seen) > 45  # random
    for p in seen:
        words = p.split()
        assert len(words) == 3 and len(set(words)) == 3 and all(w.isalpha() for w in words)


# ---------------------------------------------------------------- over HTTP

@pytest.fixture
def server(svc: MedicineBuddyService):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _req(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _token(base: str) -> str:
    code, page = _req(f"{base}/family")
    assert code == 200
    return json.loads(re.search(r"const TOKEN = (\"[^\"]+\");", page).group(1))


def test_family_page_change_passphrase_end_to_end(server, svc: MedicineBuddyService) -> None:
    base, _ = server
    token = _token(base)
    code, body = _req(f"{base}/settings/passphrase", "POST",
                      {"name": "Mario", "current": MARIO, "new": "blue garden kettle", "new_repeat": "blue garden kettle"},
                      {"X-Family-Token": token})
    assert code == 200 and json.loads(body)["status"] == "ok"
    code, body = _req(f"{base}/settings/people", "POST", {}, {"X-Family-Token": token})
    assert {p["name"] for p in json.loads(body)["people"]} == {"Anna", "Mario"}
    assert "kettle" not in body and "fishing" not in body


def test_settings_need_the_page_token(server) -> None:
    base, _ = server
    body = {"name": "Mario", "current": MARIO, "new": "blue garden kettle", "new_repeat": "blue garden kettle"}
    assert _req(f"{base}/settings/passphrase", "POST", body)[0] == 403
    assert _req(f"{base}/settings/passphrase", "POST", body, {"X-Family-Token": "guess"})[0] == 403


def test_other_websites_are_refused(server) -> None:
    base, port = server
    token = _token(base)
    evil = {"Origin": "https://evil.example", "X-Family-Token": token}
    # a hostile page in the same browser can't fake speech, change passphrases or schedules
    assert _req(f"{base}/voice/heard", "POST", {"text": "yes"}, {"Origin": "https://evil.example"})[0] == 403
    assert _req(f"{base}/settings/suggest", "POST", {}, evil)[0] == 403
    assert _req(f"{base}/schedules", "POST", {"name": "Aspirin", "time": "9"}, {"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert _req(f"{base}/voice/heard", "OPTIONS")[0] == 403
    # our own page's origin is fine
    ok = {"Origin": f"http://127.0.0.1:{port}", "X-Family-Token": token}
    assert _req(f"{base}/settings/suggest", "POST", {}, ok)[0] == 200


def test_dns_rebinding_is_refused(server) -> None:
    base, port = server
    assert _req(f"{base}/family", headers={"Host": f"evil.example:{port}"})[0] == 403
    token = _token(base)
    code, _ = _req(f"{base}/settings/suggest", "POST", {},
                   {"Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}", "X-Family-Token": token})
    assert code == 403


def test_no_cors_and_security_headers(server) -> None:
    base, _ = server
    with urllib.request.urlopen(f"{base}/family", timeout=5) as r:
        assert r.headers.get("Access-Control-Allow-Origin") is None
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Frame-Options"] == "DENY"
    with urllib.request.urlopen(f"{base}/health", timeout=5) as r:
        assert r.headers.get("Access-Control-Allow-Origin") is None


def test_no_ai_tool_can_reach_settings() -> None:
    tools = Path(__file__).resolve().parents[1] / "medicine_buddy" / "conversation"
    for p in list((tools / "external_tools").glob("*.py")) + [tools / "lib" / "tool_http.py"]:
        assert "/settings" not in p.read_text(encoding="utf-8"), p.name
