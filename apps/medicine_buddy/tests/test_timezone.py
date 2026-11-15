"""P0 #5: reminders follow the family's time zone, not the robot's (usually UTC)."""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from medicine_buddy.api import MedicineBuddyService, make_handler
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.clock import load_zone
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.scheduler import find_due_doses

ZURICH = ZoneInfo("Europe/Zurich")


@pytest.fixture
def db(tmp_path: Path):
    d = Database(tmp_path / "tz.db")
    med = d.add_medicine("Aspirin")
    d.add_schedule(med.id, "*", "09:00")
    yield d
    d.close()


@pytest.fixture
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MEDICINE_BUDDY_TZ", raising=False)
    s = MedicineBuddyService(tmp_path / "s.db", AppConfig(dry_run=True), MedicineCatalog({"Aspirin": []}))
    yield s
    s.close()


def test_09_local_is_due_at_09_local_even_when_utc_is_08(db: Database) -> None:
    # 08:00 UTC on a winter day = 09:00 in Zurich
    when = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
    assert len(find_due_doses(db, when, tz=ZURICH)) == 1
    # without the zone (the robot's UTC clock) the same instant is NOT 09:00 yet -> 1 hour early: inside the
    # 15-minute pre-window? no -> not due. This was the bug.
    assert find_due_doses(db, when, tz=timezone.utc) == []


def test_naive_time_is_read_in_the_family_zone(db: Database) -> None:
    assert len(find_due_doses(db, datetime(2026, 11, 16, 9, 5), tz=ZURICH)) == 1
    assert find_due_doses(db, datetime(2026, 11, 16, 12, 0), tz=ZURICH) == []


def test_daylight_saving_switch(db: Database) -> None:
    # 2026-07-15: Zurich is UTC+2, so 09:00 local = 07:00 UTC
    assert len(find_due_doses(db, datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc), tz=ZURICH)) == 1
    assert find_due_doses(db, datetime(2026, 7, 15, 8, 30, tzinfo=timezone.utc), tz=ZURICH) == []


def test_weekday_uses_the_local_date(tmp_path: Path) -> None:
    d = Database(tmp_path / "w.db")
    med = d.add_medicine("Aspirin")
    d.add_schedule(med.id, "0", "00:30")          # Mondays 00:30 local
    sun_2330_utc = datetime(2026, 11, 15, 23, 30, tzinfo=timezone.utc)   # Sunday UTC = Monday 00:30 in Zurich
    assert len(find_due_doses(d, sun_2330_utc, tz=ZURICH)) == 1
    assert find_due_doses(d, sun_2330_utc, tz=timezone.utc) == []
    d.close()


def test_setting_overrides_env_and_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDICINE_BUDDY_TZ", "Asia/Tokyo")
    s = MedicineBuddyService(tmp_path / "r.db", AppConfig(dry_run=True), MedicineCatalog({"Aspirin": []}))
    assert s.settings_timezone({})["timezone"] == "Asia/Tokyo" and s.settings_timezone({})["source"] == "env"
    assert s.settings_timezone({"timezone": "Europe/Zurich"})["source"] == "setting"
    s.close()
    s2 = MedicineBuddyService(tmp_path / "r.db", AppConfig(dry_run=True), MedicineCatalog({"Aspirin": []}))
    assert s2.settings_timezone({})["timezone"] == "Europe/Zurich"
    assert s2.settings_timezone({"timezone": ""})["source"] == "env"       # cleared -> back to env
    s2.close()


@pytest.mark.parametrize("bad", ["Mars/Olympus", "../../etc/passwd", "/etc/localtime", "x" * 100, "Europe/Zurich\n"[:-1] + "!"])
def test_bad_zone_names_are_rejected(svc: MedicineBuddyService, bad: str) -> None:
    assert "error" in svc.settings_timezone({"timezone": bad})
    assert svc.settings_timezone({})["timezone"] == ""            # nothing saved


def test_corrupt_saved_zone_falls_back_to_system_clock(svc: MedicineBuddyService) -> None:
    svc.db.set_setting("timezone", "Not/AZone")
    assert svc.tz() is None
    assert svc.now().tzinfo is not None
    assert isinstance(svc.due()["due"], list)                    # reminders keep working


def test_load_zone_accepts_real_names() -> None:
    assert str(load_zone("Europe/Zurich")) == "Europe/Zurich"


def test_due_and_debug_use_the_zone(svc: MedicineBuddyService) -> None:
    svc.settings_timezone({"timezone": "Pacific/Auckland"})
    assert svc.debug_snapshot()["timezone"]["timezone"] == "Pacific/Auckland"
    assert svc.debug_snapshot()["now"].endswith(("+12:00", "+13:00"))


def test_settings_timezone_report_includes_robot_clock(svc: MedicineBuddyService) -> None:
    svc.settings_timezone({"timezone": "Europe/Zurich"})
    report = svc.settings_timezone({})
    assert report["status"] == "ok"
    assert report["timezone"] == "Europe/Zurich"
    assert re.fullmatch(r"\d{2}:\d{2}", report["robot_time"])
    assert report["robot_date"]  # e.g. "Sun 15 Nov 2026"


def test_clock_now_adds_iso_and_weekday(svc: MedicineBuddyService) -> None:
    svc.settings_timezone({"timezone": "Europe/Zurich"})
    payload = svc.clock_now({"timezone": "America/New_York"})  # body ignored — read-only
    assert payload["timezone"] == "Europe/Zurich"
    assert "T" in payload["now_iso"]
    assert payload["weekday"] in {
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    }
    assert payload["robot_date"]
    assert payload["robot_time"]


def test_clock_now_http_route(svc: MedicineBuddyService) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        req = urllib.request.Request(
            f"{base}/clock/now",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        payload = json.loads(urllib.request.urlopen(req, timeout=5).read())
        assert payload["status"] == "ok"
        assert "now_iso" in payload and "weekday" in payload
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_get_robot_time_tool_module_imports() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy"
        / "conversation"
        / "external_tools"
        / "get_robot_time.py"
    )
    src = path.read_text(encoding="utf-8")
    assert 'name = "get_robot_time"' in src
    assert '"/clock/now"' in src
    profile = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy"
        / "conversation"
        / "external_profiles"
        / "medicine_caregiver"
        / "profile.md"
    ).read_text(encoding="utf-8")
    assert '"get_robot_time"' in profile


# ---- the page route (HTTP, :8787)

def test_timezone_route_and_page(svc: MedicineBuddyService) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        page = urllib.request.urlopen(f"{base}/family", timeout=5).read().decode()
        token = json.loads(re.search(r"const TOKEN = (\"[^\"]+\");", page).group(1))
        assert "Clock &amp; time zone" in page and "tz-use-device" in page

        def post(body: dict, tok: str | None = token) -> dict:
            req = urllib.request.Request(f"{base}/settings/timezone", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **({"X-Family-Token": tok} if tok else {})})
            try:
                return json.loads(urllib.request.urlopen(req, timeout=5).read())
            except urllib.error.HTTPError as e:
                return {"http": e.code, **json.loads(e.read())}

        assert post({"timezone": "Europe/Zurich"})["timezone"] == "Europe/Zurich"
        assert post({"timezone": "Nope/Nope"})["error"]
        assert post({"timezone": "Asia/Tokyo"}, tok=None)["http"] == 403          # needs the page token
    finally:
        httpd.shutdown()
        httpd.server_close()
