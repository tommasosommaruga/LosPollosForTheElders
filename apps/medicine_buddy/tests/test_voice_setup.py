from __future__ import annotations

from datetime import date

import pytest

from medicine_buddy.voice_setup import (
    describe_time,
    describe_weekdays,
    parse_time_hhmm,
    parse_weekdays,
)


def test_parse_weekdays_daily() -> None:
    assert parse_weekdays("daily") == "*"


def test_tomorrow_is_weekday_code() -> None:
    # Sunday 2026-11-15 → tomorrow = Monday = 0 (Mon=0 … Sun=6)
    assert parse_weekdays("tomorrow", as_of=date(2026, 11, 15)) == "0"


def test_today_is_weekday_code() -> None:
    # Sunday 2026-11-15 → today = Sunday = 6
    assert parse_weekdays("today", as_of=date(2026, 11, 15)) == "6"


def test_parse_weekdays_list() -> None:
    assert parse_weekdays("Mon, Wed, Fri") == "0,2,4"


@pytest.mark.parametrize("raw,expected", [
    ("every day", "*"),
    ("weekdays", "0,1,2,3,4"),
    ("weekends", "5,6"),
    ("monday and friday", "0,4"),
    ("Mondays", "0"),
    ("every tuesday", "1"),
    ("mon tue wed thu fri sat sun", "*"),
])
def test_parse_weekdays_spoken(raw: str, expected: str) -> None:
    assert parse_weekdays(raw) == expected


def test_parse_weekdays_unknown() -> None:
    with pytest.raises(ValueError):
        parse_weekdays("someday")


def test_parse_time() -> None:
    assert parse_time_hhmm("9:05") == "09:05"


@pytest.mark.parametrize("raw,expected", [
    ("9", "09:00"),
    ("9 am", "09:00"),
    ("9AM", "09:00"),
    ("8:30 pm", "20:30"),
    ("12 pm", "12:00"),
    ("12 am", "00:00"),
    ("21.30", "21:30"),
    ("2130", "21:30"),
    ("noon", "12:00"),
    ("7 o'clock", "07:00"),
])
def test_parse_time_spoken(raw: str, expected: str) -> None:
    assert parse_time_hhmm(raw) == expected


@pytest.mark.parametrize("raw", ["25:00", "13 pm", "lunchtime", "9:75", ""])
def test_parse_time_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_time_hhmm(raw)


def test_describe_for_speech() -> None:
    assert describe_weekdays("*") == "every day"
    assert describe_weekdays("0,1,2,3,4") == "on weekdays"
    assert describe_weekdays("0") == "every Monday"
    assert describe_weekdays("0,4") == "on Monday and Friday"
    assert describe_weekdays("0,2,4") == "on Monday, Wednesday and Friday"
    assert describe_time("09:00") == "9 AM"
    assert describe_time("20:30") == "8:30 PM"
    assert describe_time("00:00") == "12 AM"
