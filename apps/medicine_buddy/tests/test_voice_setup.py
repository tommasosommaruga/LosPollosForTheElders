from __future__ import annotations

import pytest

from medicine_buddy.voice_setup import parse_time_hhmm, parse_weekdays


def test_parse_weekdays_daily() -> None:
    assert parse_weekdays("daily") == "*"


def test_parse_weekdays_list() -> None:
    assert parse_weekdays("Mon, Wed, Fri") == "0,2,4"


def test_parse_time() -> None:
    assert parse_time_hhmm("9:05") == "09:05"


def test_parse_time_invalid() -> None:
    with pytest.raises(ValueError):
        parse_time_hhmm("25:00")
