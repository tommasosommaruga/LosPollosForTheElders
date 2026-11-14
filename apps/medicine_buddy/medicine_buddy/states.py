"""Dose event states for the dispense pipeline."""

from __future__ import annotations

from enum import Enum


class DoseState(str, Enum):
    IDLE = "Idle"
    DUE = "Due"
    REMINDED = "Reminded"
    CONFIRMED = "Confirmed"
    LOCATING = "Locating"
    PICKING = "Picking"
    PLACED = "Placed"
    WATCHING = "Watching"
    TAKEN = "Taken"
    MISSED = "Missed"
    FAILED = "Failed"
    SKIPPED = "Skipped"


TERMINAL_STATES = frozenset(
    {
        DoseState.TAKEN,
        DoseState.MISSED,
        DoseState.FAILED,
        DoseState.SKIPPED,
    }
)


ALLOWED_TRANSITIONS: dict[DoseState, frozenset[DoseState]] = {
    DoseState.IDLE: frozenset({DoseState.DUE}),
    DoseState.DUE: frozenset(
        {DoseState.REMINDED, DoseState.TAKEN, DoseState.MISSED, DoseState.FAILED}
    ),
    DoseState.REMINDED: frozenset(
        {
            DoseState.CONFIRMED,
            DoseState.SKIPPED,
            DoseState.TAKEN,   # person says they took it (no arm involved)
            DoseState.MISSED,  # person says they did not / reminder window ran out
            DoseState.FAILED,
        }
    ),
    DoseState.CONFIRMED: frozenset({DoseState.LOCATING, DoseState.FAILED}),
    DoseState.LOCATING: frozenset({DoseState.PICKING, DoseState.FAILED}),
    DoseState.PICKING: frozenset({DoseState.PLACED, DoseState.FAILED}),
    DoseState.PLACED: frozenset({DoseState.WATCHING, DoseState.FAILED}),
    DoseState.WATCHING: frozenset(
        {DoseState.TAKEN, DoseState.MISSED, DoseState.FAILED}
    ),
    DoseState.TAKEN: frozenset(),
    DoseState.MISSED: frozenset(),
    DoseState.FAILED: frozenset(),
    DoseState.SKIPPED: frozenset(),
}
