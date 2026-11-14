from __future__ import annotations

import json
from pathlib import Path

import pytest

from medicine_buddy.catalog import MedicineCatalog


@pytest.fixture
def catalog() -> MedicineCatalog:
    return MedicineCatalog({
        "Aspirin": ["asprin"],
        "Vitamin D": ["vitamin d3", "vit d"],
        "Metformin": [],
        "Paracetamol": ["acetaminophen"],
    })


@pytest.mark.parametrize("spoken,expected", [
    ("Aspirin", "Aspirin"),
    ("aspirin", "Aspirin"),
    ("my aspirin pills", "Aspirin"),       # filler words
    ("asprin", "Aspirin"),                 # alias
    ("aspirine", "Aspirin"),               # close spelling
    ("Vitamin D", "Vitamin D"),
    ("vit d", "Vitamin D"),
    ("vitamin-D3", "Vitamin D"),           # punctuation
    ("metformine", "Metformin"),
    ("acetaminophen", "Paracetamol"),
])
def test_recognizes(catalog: MedicineCatalog, spoken: str, expected: str) -> None:
    assert catalog.match(spoken).name == expected


@pytest.mark.parametrize("spoken", ["chocolate", "", "   ", "ibuprofen", "the medicine"])
def test_rejects_unknown(catalog: MedicineCatalog, spoken: str) -> None:
    assert catalog.match(spoken).name is None


def test_unknown_hint_lists_allowed_names(catalog: MedicineCatalog) -> None:
    result = catalog.match("chocolate")
    hint = catalog.not_recognized_hint("chocolate", result)
    assert "not on the medicine list" in hint
    assert "Aspirin" in hint and "Metformin" in hint


def test_near_miss_gives_suggestion(catalog: MedicineCatalog) -> None:
    result = catalog.match("metfr")  # too far to accept, close enough to suggest
    assert result.name is None
    assert "Metformin" in result.suggestions


def test_default_catalog_file_loads() -> None:
    catalog = MedicineCatalog.load()
    assert "Aspirin" in catalog.names and "Vitamin D" in catalog.names  # the demo tray labels


def test_catalog_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "cat.json"
    f.write_text(json.dumps({"medicines": [{"name": "Warfarin", "aliases": []}]}), encoding="utf-8")
    monkeypatch.setenv("MEDICINE_BUDDY_CATALOG", str(f))
    assert MedicineCatalog.load().names == ["Warfarin"]


def test_empty_catalog_rejected() -> None:
    with pytest.raises(ValueError):
        MedicineCatalog({})
