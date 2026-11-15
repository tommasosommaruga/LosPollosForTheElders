"""Relative phrase → half-open [start, end) windows (pinned Zurich Sunday)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from medicine_buddy.relative_time import TimeWindow, parse_window

NOW = datetime(2026, 11, 15, 15, 30, tzinfo=ZoneInfo("Europe/Zurich"))


def test_tomorrow():
    w = parse_window("tomorrow", NOW)
    assert w.start.date().isoformat() == "2026-11-16"
    assert w.end.date().isoformat() == "2026-11-17"


def test_next_week():
    w = parse_window("next week", NOW)
    assert w.start.date().isoformat() == "2026-11-16"  # Mon
    assert w.end.date().isoformat() == "2026-11-23"


def test_last_week():
    w = parse_window("last week", NOW)
    assert w.start.date().isoformat() == "2026-11-02"
    assert w.end.date().isoformat() == "2026-11-09"


def test_in_three_days():
    w = parse_window("in 3 days", NOW)
    assert w.start.date().isoformat() == "2026-11-18"


def test_today_and_tonight():
    for phrase in ("today", "tonight"):
        w = parse_window(phrase, NOW)
        assert w.start.date().isoformat() == "2026-11-15"
        assert w.end.date().isoformat() == "2026-11-16"
        assert w.start.tzinfo == NOW.tzinfo


def test_yesterday():
    w = parse_window("yesterday", NOW)
    assert w.start.date().isoformat() == "2026-11-14"
    assert w.end.date().isoformat() == "2026-11-15"


def test_this_week():
    w = parse_window("this week", NOW)
    assert w.start.date().isoformat() == "2026-11-09"  # Mon of current week
    assert w.end.date().isoformat() == "2026-11-16"


def test_n_days_ago():
    w = parse_window("2 days ago", NOW)
    assert w.start.date().isoformat() == "2026-11-13"
    assert w.end.date().isoformat() == "2026-11-14"


def test_iso_date():
    w = parse_window("2026-11-20", NOW)
    assert w.start.date().isoformat() == "2026-11-20"
    assert w.end.date().isoformat() == "2026-11-21"


def test_iso_date_range():
    w = parse_window("2026-11-16..2026-11-18", NOW)
    assert w.start.date().isoformat() == "2026-11-16"
    assert w.end.date().isoformat() == "2026-11-19"  # inclusive end → half-open next day


def test_bare_weekday_past_this_week_rolls_forward():
    # NOW is Sunday; Monday this week (Nov 9) is past → next Monday Nov 16
    w = parse_window("monday", NOW)
    assert w.start.date().isoformat() == "2026-11-16"
    assert w.end.date().isoformat() == "2026-11-17"


def test_bare_weekday_today_or_ahead():
    # Sunday is today (≥ today) → this Sunday
    w = parse_window("sunday", NOW)
    assert w.start.date().isoformat() == "2026-11-15"
    assert w.end.date().isoformat() == "2026-11-16"


def test_unknown_raises_friendly_value_error():
    with pytest.raises(ValueError, match="(?i)rephrase|unknown|not understand|could not"):
        parse_window("whenever", NOW)


def test_returns_time_window_with_label():
    w = parse_window("tomorrow", NOW)
    assert isinstance(w, TimeWindow)
    assert w.label
    assert w.start < w.end
