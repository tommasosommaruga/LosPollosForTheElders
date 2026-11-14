"""SQLite persistence for medicines, schedules, and dose events."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from medicine_buddy.states import ALLOWED_TRANSITIONS, DoseState

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "medicine_buddy.db"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Medicine:
    id: int
    name: str


@dataclass(frozen=True)
class Schedule:
    id: int
    medicine_id: int
    weekdays: str  # comma-separated Mon=0 .. Sun=6, or daily=*
    time_hhmm: str
    timezone: str
    active: bool


@dataclass(frozen=True)
class DoseEvent:
    id: int
    medicine_id: int
    state: DoseState
    slot_id: int | None
    detail: str
    created_at: str


class Database:
    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: API uses ThreadingHTTPServer; callers must serialize writes.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS medicines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                medicine_id INTEGER NOT NULL REFERENCES medicines(id),
                weekdays TEXT NOT NULL,
                time_hhmm TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'local',
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS dose_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                medicine_id INTEGER NOT NULL REFERENCES medicines(id),
                state TEXT NOT NULL,
                slot_id INTEGER,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def add_medicine(self, name: str) -> Medicine:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Medicine name must not be empty")
        now = utc_now_iso()
        existing = self.get_medicine_by_name(cleaned)
        if existing is not None:
            return existing
        cur = self._conn.execute(
            "INSERT INTO medicines (name, created_at) VALUES (?, ?)",
            (cleaned, now),
        )
        self._conn.commit()
        return Medicine(id=int(cur.lastrowid), name=cleaned)

    def get_medicine_by_name(self, name: str) -> Medicine | None:
        row = self._conn.execute(
            "SELECT id, name FROM medicines WHERE name = ? COLLATE NOCASE",
            (name.strip(),),
        ).fetchone()
        if row is None:
            return None
        return Medicine(id=row["id"], name=row["name"])

    def get_medicine(self, medicine_id: int) -> Medicine | None:
        row = self._conn.execute(
            "SELECT id, name FROM medicines WHERE id = ?",
            (medicine_id,),
        ).fetchone()
        if row is None:
            return None
        return Medicine(id=row["id"], name=row["name"])

    def list_medicines(self) -> list[Medicine]:
        rows = self._conn.execute(
            "SELECT id, name FROM medicines ORDER BY name"
        ).fetchall()
        return [Medicine(id=r["id"], name=r["name"]) for r in rows]

    def add_schedule(
        self,
        medicine_id: int,
        weekdays: str,
        time_hhmm: str,
        timezone_name: str = "local",
    ) -> Schedule:
        cur = self._conn.execute(
            """
            INSERT INTO schedules (medicine_id, weekdays, time_hhmm, timezone, active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (medicine_id, weekdays, time_hhmm, timezone_name),
        )
        self._conn.commit()
        return Schedule(
            id=int(cur.lastrowid),
            medicine_id=medicine_id,
            weekdays=weekdays,
            time_hhmm=time_hhmm,
            timezone=timezone_name,
            active=True,
        )

    def list_active_schedules(self) -> list[Schedule]:
        rows = self._conn.execute(
            """
            SELECT id, medicine_id, weekdays, time_hhmm, timezone, active
            FROM schedules WHERE active = 1
            """
        ).fetchall()
        return [
            Schedule(
                id=r["id"],
                medicine_id=r["medicine_id"],
                weekdays=r["weekdays"],
                time_hhmm=r["time_hhmm"],
                timezone=r["timezone"],
                active=bool(r["active"]),
            )
            for r in rows
        ]

    def latest_dose_state(self, medicine_id: int) -> DoseState | None:
        row = self._conn.execute(
            """
            SELECT state FROM dose_events
            WHERE medicine_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (medicine_id,),
        ).fetchone()
        if row is None:
            return None
        return DoseState(row["state"])

    def record_transition(
        self,
        medicine_id: int,
        new_state: DoseState,
        *,
        slot_id: int | None = None,
        detail: str = "",
        from_state: DoseState | None = None,
    ) -> DoseEvent:
        current = self.latest_dose_state(medicine_id)
        # New dose cycle may start from Idle even if previous dose ended terminal.
        effective_from = from_state
        if effective_from is None:
            if current is None or current in {
                DoseState.TAKEN,
                DoseState.MISSED,
                DoseState.FAILED,
                DoseState.SKIPPED,
            }:
                effective_from = DoseState.IDLE
            else:
                effective_from = current

        allowed = ALLOWED_TRANSITIONS[effective_from]
        if new_state not in allowed:
            raise ValueError(
                f"Illegal transition {effective_from.value} → {new_state.value}"
            )

        now = utc_now_iso()
        cur = self._conn.execute(
            """
            INSERT INTO dose_events (medicine_id, state, slot_id, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (medicine_id, new_state.value, slot_id, detail, now),
        )
        self._conn.commit()
        return DoseEvent(
            id=int(cur.lastrowid),
            medicine_id=medicine_id,
            state=new_state,
            slot_id=slot_id,
            detail=detail,
            created_at=now,
        )

    def list_dose_events(self, medicine_id: int | None = None) -> list[DoseEvent]:
        if medicine_id is None:
            rows = self._conn.execute(
                """
                SELECT id, medicine_id, state, slot_id, detail, created_at
                FROM dose_events ORDER BY id
                """
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, medicine_id, state, slot_id, detail, created_at
                FROM dose_events WHERE medicine_id = ? ORDER BY id
                """,
                (medicine_id,),
            ).fetchall()
        return [
            DoseEvent(
                id=r["id"],
                medicine_id=r["medicine_id"],
                state=DoseState(r["state"]),
                slot_id=r["slot_id"],
                detail=r["detail"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
