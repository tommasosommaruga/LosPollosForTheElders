"""SQLite persistence for medicines, schedules, and dose events."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def _schedule(row: sqlite3.Row) -> Schedule:
    return Schedule(
        id=row["id"],
        medicine_id=row["medicine_id"],
        weekdays=row["weekdays"],
        time_hhmm=row["time_hhmm"],
        timezone=row["timezone"],
        active=bool(row["active"]),
    )


def _event(row: sqlite3.Row) -> DoseEvent:
    return DoseEvent(
        id=row["id"],
        medicine_id=row["medicine_id"],
        state=DoseState(row["state"]),
        slot_id=row["slot_id"],
        detail=row["detail"],
        created_at=row["created_at"],
    )


_SCHEDULE_COLS = "id, medicine_id, weekdays, time_hhmm, timezone, active"
_EVENT_COLS = "id, medicine_id, state, slot_id, detail, created_at"


class Database:
    """Thread-safe: one connection shared by the HTTP server's threads, guarded by a lock."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    # ---- low-level helpers (every statement goes through the lock)
    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init_schema(self) -> None:
        with self._lock:
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

    # ---- medicines
    def add_medicine(self, name: str) -> Medicine:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Medicine name must not be empty")
        with self._lock:
            existing = self.get_medicine_by_name(cleaned)
            if existing is not None:
                return existing
            cur = self._write(
                "INSERT INTO medicines (name, created_at) VALUES (?, ?)",
                (cleaned, utc_now_iso()),
            )
            return Medicine(id=int(cur.lastrowid), name=cleaned)

    def get_medicine_by_name(self, name: str) -> Medicine | None:
        row = self._one(
            "SELECT id, name FROM medicines WHERE name = ? COLLATE NOCASE", (name.strip(),)
        )
        return None if row is None else Medicine(id=row["id"], name=row["name"])

    def get_medicine(self, medicine_id: int) -> Medicine | None:
        row = self._one("SELECT id, name FROM medicines WHERE id = ?", (medicine_id,))
        return None if row is None else Medicine(id=row["id"], name=row["name"])

    def list_medicines(self) -> list[Medicine]:
        rows = self._all("SELECT id, name FROM medicines ORDER BY name")
        return [Medicine(id=r["id"], name=r["name"]) for r in rows]

    # ---- schedules
    def add_schedule(
        self,
        medicine_id: int,
        weekdays: str,
        time_hhmm: str,
        timezone_name: str = "local",
    ) -> Schedule:
        cur = self._write(
            """
            INSERT INTO schedules (medicine_id, weekdays, time_hhmm, timezone, active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (medicine_id, weekdays, time_hhmm, timezone_name),
        )
        return Schedule(
            id=int(cur.lastrowid),
            medicine_id=medicine_id,
            weekdays=weekdays,
            time_hhmm=time_hhmm,
            timezone=timezone_name,
            active=True,
        )

    def get_schedule(self, schedule_id: int) -> Schedule | None:
        row = self._one(f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE id = ?", (schedule_id,))
        return None if row is None else _schedule(row)

    def list_active_schedules(self) -> list[Schedule]:
        rows = self._all(
            f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE active = 1 ORDER BY time_hhmm, id"
        )
        return [_schedule(r) for r in rows]

    def list_schedules_for_medicine(self, medicine_id: int, *, active_only: bool = True) -> list[Schedule]:
        sql = f"SELECT {_SCHEDULE_COLS} FROM schedules WHERE medicine_id = ?"
        if active_only:
            sql += " AND active = 1"
        return [_schedule(r) for r in self._all(sql + " ORDER BY time_hhmm, id", (medicine_id,))]

    def find_active_schedule(self, medicine_id: int, weekdays: str, time_hhmm: str) -> Schedule | None:
        row = self._one(
            f"""SELECT {_SCHEDULE_COLS} FROM schedules
                WHERE medicine_id = ? AND weekdays = ? AND time_hhmm = ? AND active = 1""",
            (medicine_id, weekdays, time_hhmm),
        )
        return None if row is None else _schedule(row)

    def update_schedule(
        self,
        schedule_id: int,
        *,
        weekdays: str | None = None,
        time_hhmm: str | None = None,
    ) -> Schedule:
        with self._lock:
            current = self.get_schedule(schedule_id)
            if current is None or not current.active:
                raise ValueError(f"No active schedule with id={schedule_id}")
            self._write(
                "UPDATE schedules SET weekdays = ?, time_hhmm = ? WHERE id = ?",
                (weekdays or current.weekdays, time_hhmm or current.time_hhmm, schedule_id),
            )
            updated = self.get_schedule(schedule_id)
            assert updated is not None
            return updated

    def deactivate_schedule(self, schedule_id: int) -> bool:
        """Soft delete: keeps the row (and dose history) but stops reminders. True if changed."""
        cur = self._write(
            "UPDATE schedules SET active = 0 WHERE id = ? AND active = 1", (schedule_id,)
        )
        return cur.rowcount == 1

    # ---- dose events
    def latest_dose_state(self, medicine_id: int) -> DoseState | None:
        row = self._one(
            "SELECT state FROM dose_events WHERE medicine_id = ? ORDER BY id DESC LIMIT 1",
            (medicine_id,),
        )
        return None if row is None else DoseState(row["state"])

    def record_transition(
        self,
        medicine_id: int,
        new_state: DoseState,
        *,
        slot_id: int | None = None,
        detail: str = "",
        from_state: DoseState | None = None,
    ) -> DoseEvent:
        with self._lock:
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
            cur = self._write(
                """
                INSERT INTO dose_events (medicine_id, state, slot_id, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (medicine_id, new_state.value, slot_id, detail, now),
            )
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
            rows = self._all(f"SELECT {_EVENT_COLS} FROM dose_events ORDER BY id")
        else:
            rows = self._all(
                f"SELECT {_EVENT_COLS} FROM dose_events WHERE medicine_id = ? ORDER BY id",
                (medicine_id,),
            )
        return [_event(r) for r in rows]
