"""SQLite persistence for medicines, schedules, and dose events."""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from medicine_buddy.auth import StoredSecret
from medicine_buddy.states import ALLOWED_TRANSITIONS, DoseState

def default_db_path() -> Path:
    """Where the database lives, in order of preference:
    1. MEDICINE_BUDDY_DB (env / .env),
    2. a source checkout (apps/medicine_buddy/data/) - development on the laptop,
    3. an installed app (e.g. on the robot): a per-user data folder that survives
       reinstalling or updating the app (~/.local/share/medicine-buddy, %LOCALAPPDATA%)."""
    env = os.environ.get("MEDICINE_BUDDY_DB")
    if env:
        return Path(env).expanduser()
    project = Path(__file__).resolve().parent.parent
    if (project / "pyproject.toml").exists():
        return project / "data" / "medicine_buddy.db"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "medicine-buddy" / "medicine_buddy.db"


DEFAULT_DB_PATH = default_db_path()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Medicine:
    id: int
    name: str
    person_id: int | None = None  # None = legacy / not assigned to a person


ROLES = ("person", "caregiver")


@dataclass(frozen=True)
class Person:
    id: int
    name: str
    role: str  # "person" | "caregiver"
    secret: StoredSecret

    @property
    def is_caregiver(self) -> bool:
        return self.role == "caregiver"


MEMORY_KINDS = ("story", "recipe", "skill", "tradition", "advice", "family history", "other")


@dataclass(frozen=True)
class Memory:
    """Something a person wants to keep and pass on: a story, recipe, skill, tradition..."""

    id: int
    person_id: int
    kind: str
    title: str
    text: str
    created_at: str


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
                CREATE TABLE IF NOT EXISTS people (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    role TEXT NOT NULL CHECK (role IN ('person', 'caregiver')),
                    pass_salt BLOB NOT NULL,
                    pass_hash BLOB NOT NULL,
                    pass_words INTEGER NOT NULL,
                    pass_iterations INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER NOT NULL REFERENCES people(id),
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS medicines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER REFERENCES people(id),
                    name TEXT NOT NULL COLLATE NOCASE,
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
            self._migrate_medicines_per_person()
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_medicine_person_name "
                "ON medicines (COALESCE(person_id, 0), name COLLATE NOCASE)"
            )
            self._conn.commit()

    def _migrate_medicines_per_person(self) -> None:
        """v0.1/v0.2 databases had globally-unique medicine names and no person_id.
        Rebuild the table keeping every row (ids unchanged, so schedules and dose
        history stay linked); old medicines become 'unassigned' (person_id NULL)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(medicines)")}
        if "person_id" in cols:
            return
        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._conn.executescript(
                """
                BEGIN;
                CREATE TABLE medicines_v3 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER REFERENCES people(id),
                    name TEXT NOT NULL COLLATE NOCASE,
                    created_at TEXT NOT NULL
                );
                INSERT INTO medicines_v3 (id, person_id, name, created_at)
                    SELECT id, NULL, name, created_at FROM medicines;
                DROP TABLE medicines;
                ALTER TABLE medicines_v3 RENAME TO medicines;
                COMMIT;
                """
            )
        finally:
            self._conn.execute("PRAGMA foreign_keys = ON")

    # ---- people
    @staticmethod
    def _person(row: sqlite3.Row) -> Person:
        return Person(
            id=row["id"],
            name=row["name"],
            role=row["role"],
            secret=StoredSecret(
                bytes(row["pass_salt"]), bytes(row["pass_hash"]), row["pass_words"], row["pass_iterations"]
            ),
        )

    def add_person(self, name: str, role: str, secret: StoredSecret) -> Person:
        cleaned = " ".join(name.split())
        if not cleaned:
            raise ValueError("Person name must not be empty")
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        with self._lock:
            if self.get_person_by_name(cleaned) is not None:
                raise ValueError(f"{cleaned} is already registered")
            cur = self._write(
                """INSERT INTO people (name, role, pass_salt, pass_hash, pass_words, pass_iterations, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (cleaned, role, secret.salt, secret.digest, secret.words, secret.iterations, utc_now_iso()),
            )
            return Person(int(cur.lastrowid), cleaned, role, secret)

    def get_person(self, person_id: int) -> Person | None:
        row = self._one("SELECT * FROM people WHERE id = ?", (person_id,))
        return None if row is None else self._person(row)

    def get_person_by_name(self, name: str) -> Person | None:
        row = self._one("SELECT * FROM people WHERE name = ? COLLATE NOCASE", (" ".join(name.split()),))
        return None if row is None else self._person(row)

    def list_people(self) -> list[Person]:
        return [self._person(r) for r in self._all("SELECT * FROM people ORDER BY name")]

    def update_person_secret(self, person_id: int, secret: StoredSecret) -> None:
        cur = self._write(
            "UPDATE people SET pass_salt = ?, pass_hash = ?, pass_words = ?, pass_iterations = ? WHERE id = ?",
            (secret.salt, secret.digest, secret.words, secret.iterations, person_id),
        )
        if cur.rowcount != 1:
            raise ValueError(f"No person with id={person_id}")

    def has_caregiver(self) -> bool:
        return self._one("SELECT 1 FROM people WHERE role = 'caregiver' LIMIT 1") is not None

    # ---- memories (stories, recipes, skills, traditions ...)
    @staticmethod
    def _memory(r: sqlite3.Row) -> Memory:
        return Memory(r["id"], r["person_id"], r["kind"], r["title"], r["text"], r["created_at"])

    def add_memory(self, person_id: int, kind: str, title: str, text: str) -> Memory:
        body = text.strip()
        if not body:
            raise ValueError("Memory text must not be empty")
        kind = kind if kind in MEMORY_KINDS else "other"
        title = " ".join(title.split())[:120] or body.split(".")[0][:60]
        now = utc_now_iso()
        cur = self._write(
            "INSERT INTO memories (person_id, kind, title, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (person_id, kind, title, body, now),
        )
        return Memory(int(cur.lastrowid), person_id, kind, title, body, now)

    def get_memory(self, memory_id: int) -> Memory | None:
        row = self._one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return None if row is None else self._memory(row)

    def list_memories(self, person_id: int | None = None, kind: str | None = None, limit: int = 200) -> list[Memory]:
        sql, params = "SELECT * FROM memories WHERE 1=1", []
        if person_id is not None:
            sql += " AND person_id = ?"
            params.append(person_id)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        rows = self._all(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
        return [self._memory(r) for r in rows]

    def delete_memory(self, memory_id: int) -> bool:
        return self._write("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount == 1

    # ---- medicines
    @staticmethod
    def _medicine(row: sqlite3.Row) -> Medicine:
        return Medicine(id=row["id"], name=row["name"], person_id=row["person_id"])

    def add_medicine(self, name: str, person_id: int | None = None) -> Medicine:
        """Medicines are per person: two people can both have 'Aspirin' with separate
        schedules and dose history. person_id=None is the legacy/unassigned bucket."""
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Medicine name must not be empty")
        with self._lock:
            existing = self.get_medicine_by_name(cleaned, person_id)
            if existing is not None:
                return existing
            cur = self._write(
                "INSERT INTO medicines (person_id, name, created_at) VALUES (?, ?, ?)",
                (person_id, cleaned, utc_now_iso()),
            )
            return Medicine(id=int(cur.lastrowid), name=cleaned, person_id=person_id)

    def get_medicine_by_name(self, name: str, person_id: int | None = None) -> Medicine | None:
        row = self._one(
            "SELECT id, name, person_id FROM medicines WHERE name = ? COLLATE NOCASE AND person_id IS ?",
            (name.strip(), person_id),
        )
        return None if row is None else self._medicine(row)

    def get_medicine(self, medicine_id: int) -> Medicine | None:
        row = self._one("SELECT id, name, person_id FROM medicines WHERE id = ?", (medicine_id,))
        return None if row is None else self._medicine(row)

    def list_medicines(self, person_id: int | None = None, *, all_people: bool = True) -> list[Medicine]:
        """All medicines (default), or only one person's when all_people=False."""
        if all_people:
            rows = self._all("SELECT id, name, person_id FROM medicines ORDER BY name, person_id")
        else:
            rows = self._all(
                "SELECT id, name, person_id FROM medicines WHERE person_id IS ? ORDER BY name", (person_id,)
            )
        return [self._medicine(r) for r in rows]

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
