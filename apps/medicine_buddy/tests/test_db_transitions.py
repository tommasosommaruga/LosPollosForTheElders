from __future__ import annotations

from pathlib import Path

import pytest

from medicine_buddy.db import Database
from medicine_buddy.states import DoseState


def test_records_legal_transitions(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    med = db.add_medicine("Aspirin")
    db.record_transition(med.id, DoseState.DUE, from_state=DoseState.IDLE)
    db.record_transition(med.id, DoseState.REMINDED)
    db.record_transition(med.id, DoseState.CONFIRMED)
    events = db.list_dose_events(med.id)
    assert [e.state for e in events] == [
        DoseState.DUE,
        DoseState.REMINDED,
        DoseState.CONFIRMED,
    ]
    db.close()


def test_rejects_illegal_transition(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    med = db.add_medicine("Aspirin")
    db.record_transition(med.id, DoseState.DUE, from_state=DoseState.IDLE)
    with pytest.raises(ValueError, match="Illegal transition"):
        db.record_transition(med.id, DoseState.PICKING)
    db.close()
