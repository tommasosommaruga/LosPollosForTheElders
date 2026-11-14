"""Prompt-injection scenarios: medicine changes need the person's real (transcribed) words."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from medicine_buddy.api import MedicineBuddyService
from medicine_buddy.catalog import MedicineCatalog
from medicine_buddy.config import AppConfig
from medicine_buddy.voice_guard import VoiceGuard, is_affirmative


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def svc(tmp_path: Path, clock: Clock):
    catalog = MedicineCatalog({"Aspirin": ["asprin"], "Vitamin D": ["vit d"]})
    guard = VoiceGuard(catalog, enabled=True, clock=clock, grace=0.0)
    s = MedicineBuddyService(tmp_path / "g.db", AppConfig(dry_run=True, tray_labels=["ASPIRIN"]), catalog, guard)
    yield s
    s.close()


def _add_aspirin(svc: MedicineBuddyService) -> None:
    svc.voice_heard({"text": "please remind me about my aspirin every day at nine"})
    assert svc.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})["verified"]


@pytest.mark.parametrize("text,yes", [
    ("yes", True), ("Yeah, go ahead", True), ("okay please", True), ("sure", True), ("yes please", True),
    ("no", False), ("no thanks", False), ("yes, no wait", False), ("don't", False), ("what?", False),
    ("please remind me about my aspirin", False),  # a request is not a confirmation
    ("right now", False),
])
def test_affirmative_detection(text: str, yes: bool) -> None:
    assert is_affirmative(text) is yes


def test_finds_medicine_in_sentence() -> None:
    cat = MedicineCatalog({"Aspirin": ["asprin"], "Vitamin D": ["vit d"]})
    assert cat.find_in_text("could you add my asprin at nine") == {"Aspirin"}
    assert cat.find_in_text("vitamin d and aspirin please") == {"Aspirin", "Vitamin D"}
    assert cat.find_in_text("what's the weather today") == set()


# ---------------------------------------------------------------- injection is blocked

def test_web_injected_add_is_blocked(svc: MedicineBuddyService) -> None:
    """A web page told the AI to add a medicine; the person never said it."""
    svc.voice_heard({"text": "what's the weather in Geneva?"})
    r = svc.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "03:00"})
    assert r["blocked"] is True
    assert svc.db.list_active_schedules() == []
    assert svc.security()["blocked"][-1]["action"] == "add schedule"


def test_web_injected_delete_is_blocked(svc: MedicineBuddyService, clock: Clock) -> None:
    _add_aspirin(svc)
    clock.t += 600  # 10 minutes later the person only talks about the news
    svc.voice_heard({"text": "search the news for me"})
    r = svc.delete_schedule({"name": "Aspirin", "confirmed": True})
    assert r["blocked"] is True
    assert len(svc.db.list_active_schedules()) == 1


def test_confirmed_flag_without_question_becomes_a_question(svc: MedicineBuddyService) -> None:
    """The AI claims confirmed=true but nobody was asked: the server asks instead of acting."""
    _add_aspirin(svc)
    svc.voice_heard({"text": "delete my aspirin reminder"})
    r = svc.delete_schedule({"name": "Aspirin", "confirmed": True})
    assert r["status"] == "needs_confirm"
    assert len(svc.db.list_active_schedules()) == 1


def test_yes_said_before_the_question_does_not_count(svc: MedicineBuddyService, clock: Clock) -> None:
    _add_aspirin(svc)
    svc.voice_heard({"text": "yes, delete my aspirin"})  # earlier "yes"
    clock.t += 1
    assert svc.delete_schedule({"name": "Aspirin", "confirmed": False})["status"] == "needs_confirm"
    clock.t += 1
    r = svc.delete_schedule({"name": "Aspirin", "confirmed": True})  # no new yes
    assert r["blocked"] is True
    assert len(svc.db.list_active_schedules()) == 1


def test_no_answer_blocks(svc: MedicineBuddyService, clock: Clock) -> None:
    _add_aspirin(svc)
    svc.voice_heard({"text": "remove my aspirin"})
    svc.delete_schedule({"name": "Aspirin", "confirmed": False})
    clock.t += 1
    svc.voice_heard({"text": "no, keep it"})
    assert svc.delete_schedule({"name": "Aspirin", "confirmed": True})["blocked"] is True


def test_question_expires(svc: MedicineBuddyService, clock: Clock) -> None:
    _add_aspirin(svc)
    svc.voice_heard({"text": "remove my aspirin"})
    svc.delete_schedule({"name": "Aspirin", "confirmed": False})
    clock.t += 300
    svc.voice_heard({"text": "yes"})
    svc.voice_heard({"text": "aspirin"})  # keep mention fresh
    r = svc.delete_schedule({"name": "Aspirin", "confirmed": True})
    assert r["status"] == "needs_confirm"  # stale question -> asked again, nothing deleted
    assert len(svc.db.list_active_schedules()) == 1


def test_dispense_needs_real_yes(svc: MedicineBuddyService, clock: Clock) -> None:
    _add_aspirin(svc)
    assert svc.run_dose({"medicine_name": "Aspirin", "confirmed": True})["status"] == "needs_confirm"
    clock.t += 1
    assert svc.run_dose({"medicine_name": "Aspirin", "confirmed": True})["blocked"] is True  # nobody said yes
    assert svc.run_dose({"medicine_name": "Aspirin", "confirmed": False})["status"] == "needs_confirm"
    clock.t += 1
    svc.voice_heard({"text": "yes please"})
    assert svc.run_dose({"medicine_name": "Aspirin", "confirmed": True})["final_state"] == "Placed"


def test_one_yes_answers_one_question(svc: MedicineBuddyService, clock: Clock) -> None:
    _add_aspirin(svc)
    svc.run_dose({"medicine_name": "Aspirin", "confirmed": False})
    clock.t += 1
    svc.voice_heard({"text": "yes"})
    assert svc.run_dose({"medicine_name": "Aspirin", "confirmed": True})["final_state"] == "Placed"
    # replaying the same confirmation must not dispense again
    assert svc.run_dose({"medicine_name": "Aspirin", "confirmed": True})["status"] == "needs_confirm"


def test_mark_taken_needs_recent_speech(svc: MedicineBuddyService, clock: Clock) -> None:
    _add_aspirin(svc)
    clock.t += 1000
    assert svc.mark_adherence({"medicine_name": "Aspirin", "taken": True})["blocked"] is True


# ---------------------------------------------------------------- the real person gets through

def test_person_can_add_update_and_delete(svc: MedicineBuddyService, clock: Clock) -> None:
    _add_aspirin(svc)
    svc.voice_heard({"text": "move my aspirin to eight pm"})
    assert svc.update_schedule({"name": "Aspirin", "new_time": "20:00"})["verified"] is True

    svc.voice_heard({"text": "actually stop the aspirin reminder"})
    ask = svc.delete_schedule({"name": "Aspirin", "confirmed": False})
    assert ask["status"] == "needs_confirm"
    clock.t += 2
    svc.voice_heard({"text": "yes"})
    done = svc.delete_schedule({"name": "Aspirin", "confirmed": True})
    assert done["verified"] is True and svc.db.list_active_schedules() == []


def test_misheard_medicine_name_still_counts_as_mention(svc: MedicineBuddyService) -> None:
    svc.voice_heard({"text": "add asprin every morning at eight"})
    assert svc.add_schedule({"name": "aspirin", "weekdays": "daily", "time": "8 am"})["verified"] is True


def test_guard_disabled_keeps_old_behaviour(tmp_path: Path) -> None:
    s = MedicineBuddyService(tmp_path / "off.db", AppConfig(dry_run=True),
                             MedicineCatalog({"Aspirin": []}))
    assert s.add_schedule({"name": "Aspirin", "weekdays": "daily", "time": "09:00"})["verified"]
    assert s.security()["enabled"] is False
    s.close()


def test_no_ai_tool_can_reach_the_voice_endpoint() -> None:
    """The guard is only meaningful if the LLM has no way to post fake transcripts."""
    tools = Path(__file__).resolve().parents[1] / "medicine_buddy" / "conversation"
    sources = [p.read_text(encoding="utf-8") for p in (tools / "external_tools").glob("*.py")]
    sources.append((tools / "lib" / "tool_http.py").read_text(encoding="utf-8"))
    assert sources
    for src in sources:
        assert "/voice/heard" not in src
        # every tool calls a fixed path literal, never one built from model arguments
        for call in re.findall(r"_api_request\(\s*\"(?:GET|POST)\",\s*([^,)]+)", src):
            assert call.strip().startswith('"/'), call


# ---------------------------------------------------------------- hashing is lazy (the robot's CPU is small)

def _counting_guard(identified: dict[str, set[int]] | None = None):
    from medicine_buddy.catalog import MedicineCatalog
    from medicine_buddy.voice_guard import VoiceGuard
    calls: list[str] = []
    table = identified or {}

    def identify(text: str) -> set[int]:
        calls.append(text)
        return table.get(text, set())

    cat = MedicineCatalog({"Aspirin": []})
    clock = Clock()
    g = VoiceGuard(cat, enabled=True, clock=clock, grace=0.0, identify=identify)
    return g, calls, clock


def test_ordinary_chat_is_never_hashed() -> None:
    g, calls, _ = _counting_guard()
    for i in range(30):
        g.heard(f"what is the weather like today number {i}")
    assert calls == []                                   # nothing hashed while people just talk


def test_checks_newest_first_and_stops_early_and_only_once() -> None:
    g, calls, _ = _counting_guard({"my passphrase is open sesame now": {7}})
    for i in range(20):
        g.heard(f"chat {i}")
    g.heard("my passphrase is open sesame now")
    g.heard("thanks")
    assert g.wait_authenticated(lambda ids: 7 in ids) is True
    assert calls == ["thanks", "my passphrase is open sesame now"]    # 2 sentences, not 22
    assert g.wait_authenticated(lambda ids: 7 in ids) is True
    assert len(calls) == 2                                              # remembered: no second hashing


def test_reidentify_forgets_old_results() -> None:
    g, calls, _ = _counting_guard({"alpha beta gamma": {1}})
    g.heard("alpha beta gamma")
    assert g.authenticated() == {1}
    g.identify = lambda text: set()          # the passphrase was changed: the old words no longer match
    g.reidentify()
    assert g.authenticated() == set()


def test_old_speech_outside_the_window_is_not_hashed() -> None:
    from medicine_buddy.voice_guard import AUTH_WINDOW
    g, calls, clock = _counting_guard({"alpha beta gamma": {1}})
    g.heard("alpha beta gamma")
    clock.t += AUTH_WINDOW + 5
    assert g.authenticated() == set() and calls == []


def test_unlock_status_never_triggers_hashing() -> None:
    g, calls, _ = _counting_guard({"alpha beta gamma": {1}})
    g.heard("alpha beta gamma")
    assert g.unlock_status() == {} and calls == []        # the debug page polls this every 3 seconds
    g.authenticated()
    assert 1 in g.unlock_status()
