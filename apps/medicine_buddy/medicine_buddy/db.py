"""SQLite persistence for medicines, schedules, and dose events."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from medicine_buddy.auth import StoredSecret
from medicine_buddy.names import name_key
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


@dataclass(frozen=True)
class ActivityEvent:
    """Privacy-safe request/tool outcome. Never contains bodies, transcripts, or secrets."""

    id: int
    at_ts: float
    via: str
    method: str
    path: str
    status: int
    ms: float
    result: str | None = None
    error: str | None = None
    blocked: bool | None = None
    final_state: str | None = None
    verified: bool | None = None
    speak_hint: str | None = None
    utterance_chars: int | None = None
    person_id: int | None = None


CALENDAR_KINDS = ("companion", "medicine")
CALENDAR_STATUSES = ("active", "cancelled", "proposed", "rejected")


@dataclass(frozen=True)
class CalendarEvent:
    """Companion calendar row (and medicine import proposals). Medicine *schedules* stay in schedules."""

    id: int
    uid: str
    kind: str  # companion | medicine
    title: str
    description: str
    weekdays: str  # '*' / '0,2' / '' for one-shot without recurrence
    time_hhmm: str
    starts_at: str
    ends_at: str
    all_day: bool
    person_id: int | None
    medicine_name: str | None
    source: str  # local | import
    status: str  # active | cancelled | proposed | rejected
    external_id: str  # Google Calendar event id when pushed
    created_at: str
    updated_at: str


# Keep the robot DB lean: drop oldest rows past this budget / age.
ACTIVITY_MAX_ROWS = 5_000
ACTIVITY_MAX_AGE_DAYS = 30


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
                    name_key TEXT,
                    role TEXT NOT NULL CHECK (role IN ('person', 'caregiver')),
                    pass_salt BLOB NOT NULL,
                    pass_hash BLOB NOT NULL,
                    pass_words INTEGER NOT NULL,
                    pass_iterations INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
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

                CREATE TABLE IF NOT EXISTS activity_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at_ts REAL NOT NULL,
                    via TEXT NOT NULL,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    ms REAL NOT NULL,
                    result TEXT,
                    error TEXT,
                    blocked INTEGER,
                    final_state TEXT,
                    verified INTEGER,
                    speak_hint TEXT,
                    utterance_chars INTEGER,
                    person_id INTEGER REFERENCES people(id)
                );

                CREATE TABLE IF NOT EXISTS calendar_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uid TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL CHECK (kind IN ('companion', 'medicine')),
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    weekdays TEXT NOT NULL DEFAULT '',
                    time_hhmm TEXT NOT NULL DEFAULT '',
                    starts_at TEXT NOT NULL DEFAULT '',
                    ends_at TEXT NOT NULL DEFAULT '',
                    all_day INTEGER NOT NULL DEFAULT 0,
                    person_id INTEGER REFERENCES people(id),
                    medicine_name TEXT,
                    source TEXT NOT NULL DEFAULT 'local',
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'cancelled', 'proposed', 'rejected')),
                    external_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._migrate_medicines_per_person()
            self._migrate_people_name_key()
            self._migrate_calendar_external_id()
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_medicine_person_name "
                "ON medicines (COALESCE(person_id, 0), name COLLATE NOCASE)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_activity_at ON activity_events (at_ts)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_activity_via ON activity_events (via)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_activity_person ON activity_events (person_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_calendar_status ON calendar_events (status, kind)"
            )
            self._conn.commit()

    def _migrate_people_name_key(self) -> None:
        """Older databases compared names only by ASCII case. Add the canonical key (see names.py),
        fill it in, and make it unique. Two EXISTING people whose names collapse to the same key are
        both kept (nothing is deleted or merged): the later one gets a '~<id>' suffix so the index can
        be built, and a warning is logged so the caregiver can rename one of them."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(people)")}
        if "name_key" not in cols:
            self._conn.execute("ALTER TABLE people ADD COLUMN name_key TEXT")
        rows = self._conn.execute("SELECT id, name, name_key FROM people ORDER BY id").fetchall()
        seen: set[str] = set()
        for r in rows:
            key = name_key(r["name"]) or f"person {r['id']}"
            if key in seen:
                logging.getLogger("medicine_buddy.db").warning(
                    "People #%s '%s' has the same name as an earlier person; kept both - please rename one.",
                    r["id"], r["name"])
                key = f"{key}~{r['id']}"
            seen.add(key)
            if r["name_key"] != key:
                self._conn.execute("UPDATE people SET name_key = ? WHERE id = ?", (key, r["id"]))
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_people_name_key ON people (name_key)")

    def _migrate_calendar_external_id(self) -> None:
        """Store Google Calendar event ids for voice create/cancel push."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(calendar_events)")}
        if "external_id" not in cols:
            self._conn.execute(
                "ALTER TABLE calendar_events ADD COLUMN external_id TEXT NOT NULL DEFAULT ''"
            )

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
        key = name_key(cleaned)
        if not key:
            raise ValueError("Person name must contain letters")
        with self._lock:
            if self.get_person_by_name(cleaned) is not None:
                raise ValueError(f"{cleaned} is already registered")
            cur = self._write(
                """INSERT INTO people (name, name_key, role, pass_salt, pass_hash, pass_words, pass_iterations, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (cleaned, key, role, secret.salt, secret.digest, secret.words, secret.iterations, utc_now_iso()),
            )
            return Person(int(cur.lastrowid), cleaned, role, secret)

    def get_person(self, person_id: int) -> Person | None:
        row = self._one("SELECT * FROM people WHERE id = ?", (person_id,))
        return None if row is None else self._person(row)

    def get_person_by_name(self, name: str) -> Person | None:
        """Exact person by canonical name: case, accents and spacing never matter (names.py)."""
        key = name_key(name)
        if not key:
            return None
        row = self._one("SELECT * FROM people WHERE name_key = ?", (key,))
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

    # ---- small key/value settings (time zone ...)
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM app_settings WHERE key = ?", (key,))
        return default if row is None else row["value"]

    def set_setting(self, key: str, value: str) -> None:
        """Empty value removes the setting (back to the default)."""
        if value:
            self._write(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        else:
            self._write("DELETE FROM app_settings WHERE key = ?", (key,))

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

    # ---- companion calendar (medicine schedules stay in `schedules`)
    _CAL_COLS = (
        "id, uid, kind, title, description, weekdays, time_hhmm, starts_at, ends_at, "
        "all_day, person_id, medicine_name, source, status, external_id, created_at, updated_at"
    )

    @staticmethod
    def _calendar_event(row: sqlite3.Row) -> CalendarEvent:
        keys = row.keys()
        return CalendarEvent(
            id=row["id"],
            uid=row["uid"],
            kind=row["kind"],
            title=row["title"],
            description=row["description"] or "",
            weekdays=row["weekdays"] or "",
            time_hhmm=row["time_hhmm"] or "",
            starts_at=row["starts_at"] or "",
            ends_at=row["ends_at"] or "",
            all_day=bool(row["all_day"]),
            person_id=row["person_id"],
            medicine_name=row["medicine_name"],
            source=row["source"] or "local",
            status=row["status"],
            external_id=(row["external_id"] if "external_id" in keys else "") or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def upsert_calendar_event(
        self,
        *,
        uid: str,
        kind: str,
        title: str,
        description: str = "",
        weekdays: str = "",
        time_hhmm: str = "",
        starts_at: str = "",
        ends_at: str = "",
        all_day: bool = False,
        person_id: int | None = None,
        medicine_name: str | None = None,
        source: str = "local",
        status: str = "active",
        external_id: str | None = None,
    ) -> CalendarEvent:
        if kind not in CALENDAR_KINDS:
            raise ValueError(f"Unknown calendar kind: {kind}")
        if status not in CALENDAR_STATUSES:
            raise ValueError(f"Unknown calendar status: {status}")
        title = " ".join((title or "").split())[:200]
        if not title:
            raise ValueError("Calendar title must not be empty")
        now = utc_now_iso()
        existing = self.get_calendar_event_by_uid(uid)
        if existing is None:
            ext = (external_id or "").strip()
            cur = self._write(
                f"""INSERT INTO calendar_events
                    (uid, kind, title, description, weekdays, time_hhmm, starts_at, ends_at,
                     all_day, person_id, medicine_name, source, status, external_id,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uid, kind, title, description.strip(), weekdays, time_hhmm, starts_at, ends_at,
                    1 if all_day else 0, person_id, medicine_name, source, status, ext, now, now,
                ),
            )
            row = self._one(f"SELECT {self._CAL_COLS} FROM calendar_events WHERE id = ?", (cur.lastrowid,))
            assert row is not None
            return self._calendar_event(row)
        ext = existing.external_id if external_id is None else external_id.strip()
        self._write(
            """UPDATE calendar_events SET kind = ?, title = ?, description = ?, weekdays = ?,
               time_hhmm = ?, starts_at = ?, ends_at = ?, all_day = ?, person_id = ?,
               medicine_name = ?, source = ?, status = ?, external_id = ?, updated_at = ?
             WHERE uid = ?""",
            (
                kind, title, description.strip(), weekdays, time_hhmm, starts_at, ends_at,
                1 if all_day else 0, person_id, medicine_name, source, status, ext, now, uid,
            ),
        )
        updated = self.get_calendar_event_by_uid(uid)
        assert updated is not None
        return updated

    def set_calendar_external_id(self, event_id: int, external_id: str) -> CalendarEvent | None:
        self._write(
            "UPDATE calendar_events SET external_id = ?, updated_at = ? WHERE id = ?",
            ((external_id or "").strip(), utc_now_iso(), event_id),
        )
        return self.get_calendar_event(event_id)

    def get_calendar_event(self, event_id: int) -> CalendarEvent | None:
        row = self._one(f"SELECT {self._CAL_COLS} FROM calendar_events WHERE id = ?", (event_id,))
        return None if row is None else self._calendar_event(row)

    def get_calendar_event_by_uid(self, uid: str) -> CalendarEvent | None:
        row = self._one(f"SELECT {self._CAL_COLS} FROM calendar_events WHERE uid = ?", (uid,))
        return None if row is None else self._calendar_event(row)

    def list_calendar_events(
        self,
        *,
        status: str | None = "active",
        kind: str | None = None,
        limit: int = 200,
    ) -> list[CalendarEvent]:
        sql = f"SELECT {self._CAL_COLS} FROM calendar_events WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        rows = self._all(sql + " ORDER BY time_hhmm, id LIMIT ?", (*params, max(1, min(limit, 500))))
        return [self._calendar_event(r) for r in rows]

    def set_calendar_event_status(self, event_id: int, status: str) -> CalendarEvent | None:
        if status not in CALENDAR_STATUSES:
            raise ValueError(f"Unknown calendar status: {status}")
        self._write(
            "UPDATE calendar_events SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now_iso(), event_id),
        )
        return self.get_calendar_event(event_id)

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

    def list_dose_events(self, medicine_id: int | None = None, limit: int | None = None) -> list[DoseEvent]:
        """Oldest first. `limit`: only the most recent N (still oldest first)."""
        sql, params = f"SELECT {_EVENT_COLS} FROM dose_events", []
        if medicine_id is not None:
            sql += " WHERE medicine_id = ?"
            params.append(medicine_id)
        if limit:
            rows = self._all(sql + " ORDER BY id DESC LIMIT ?", (*params, int(limit)))[::-1]
        else:
            rows = self._all(sql + " ORDER BY id", tuple(params))
        return [_event(r) for r in rows]

    # ---- activity events (privacy-safe request / tool outcomes)
    @staticmethod
    def _activity(row: sqlite3.Row) -> ActivityEvent:
        blocked = row["blocked"]
        verified = row["verified"]
        return ActivityEvent(
            id=row["id"],
            at_ts=float(row["at_ts"]),
            via=row["via"],
            method=row["method"],
            path=row["path"],
            status=int(row["status"]),
            ms=float(row["ms"]),
            result=row["result"],
            error=row["error"],
            blocked=None if blocked is None else bool(blocked),
            final_state=row["final_state"],
            verified=None if verified is None else bool(verified),
            speak_hint=row["speak_hint"],
            utterance_chars=row["utterance_chars"],
            person_id=row["person_id"],
        )

    def insert_activity_event(
        self,
        *,
        at_ts: float,
        via: str,
        method: str,
        path: str,
        status: int,
        ms: float,
        result: str | None = None,
        error: str | None = None,
        blocked: bool | None = None,
        final_state: str | None = None,
        verified: bool | None = None,
        speak_hint: str | None = None,
        utterance_chars: int | None = None,
        person_id: int | None = None,
    ) -> ActivityEvent:
        """Insert one activity row and prune old / excess rows. Safe fields only."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO activity_events
                   (at_ts, via, method, path, status, ms, result, error, blocked,
                    final_state, verified, speak_hint, utterance_chars, person_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    at_ts, via, method, path, status, ms,
                    result, error,
                    None if blocked is None else int(blocked),
                    final_state,
                    None if verified is None else int(verified),
                    speak_hint, utterance_chars, person_id,
                ),
            )
            event_id = int(cur.lastrowid)
            self._prune_activity_events_locked()
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM activity_events WHERE id = ?", (event_id,)).fetchone()
            assert row is not None
            return self._activity(row)

    def _prune_activity_events_locked(self) -> None:
        """Caller holds the lock. Drop rows older than ACTIVITY_MAX_AGE_DAYS, then cap row count."""
        cutoff = time.time() - (ACTIVITY_MAX_AGE_DAYS * 86400)
        self._conn.execute("DELETE FROM activity_events WHERE at_ts < ?", (cutoff,))
        count = self._conn.execute("SELECT COUNT(*) AS n FROM activity_events").fetchone()["n"]
        if count > ACTIVITY_MAX_ROWS:
            excess = int(count) - ACTIVITY_MAX_ROWS
            self._conn.execute(
                "DELETE FROM activity_events WHERE id IN "
                "(SELECT id FROM activity_events ORDER BY id ASC LIMIT ?)",
                (excess,),
            )

    def _activity_filters(
        self,
        *,
        since_id: int | None = None,
        via: str | None = None,
        status_min: int | None = None,
        status_max: int | None = None,
        path_prefix: str | None = None,
        person_id: int | None = None,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = ["1=1"]
        params: list[Any] = []
        if since_id is not None:
            clauses.append("id > ?")
            params.append(since_id)
        if via:
            clauses.append("via = ?")
            params.append(via)
        if status_min is not None:
            clauses.append("status >= ?")
            params.append(status_min)
        if status_max is not None:
            clauses.append("status <= ?")
            params.append(status_max)
        if path_prefix:
            clauses.append("path LIKE ?")
            params.append(path_prefix + "%")
        if person_id is not None:
            clauses.append("person_id = ?")
            params.append(person_id)
        if since_ts is not None:
            clauses.append("at_ts >= ?")
            params.append(since_ts)
        if until_ts is not None:
            clauses.append("at_ts <= ?")
            params.append(until_ts)
        return " AND ".join(clauses), params

    def query_activity_events(
        self,
        *,
        since_id: int | None = None,
        via: str | None = None,
        status_min: int | None = None,
        status_max: int | None = None,
        path_prefix: str | None = None,
        person_id: int | None = None,
        since_ts: float | None = None,
        until_ts: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ActivityEvent]:
        where, params = self._activity_filters(
            since_id=since_id, via=via, status_min=status_min, status_max=status_max,
            path_prefix=path_prefix, person_id=person_id, since_ts=since_ts, until_ts=until_ts,
        )
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        # Cursor mode (since_id): ascending so callers append. Otherwise newest first.
        order = "id ASC" if since_id is not None else "id DESC"
        rows = self._all(
            f"SELECT * FROM activity_events WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [self._activity(r) for r in rows]

    def count_activity_events(
        self,
        *,
        since_id: int | None = None,
        via: str | None = None,
        status_min: int | None = None,
        status_max: int | None = None,
        path_prefix: str | None = None,
        person_id: int | None = None,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ) -> int:
        where, params = self._activity_filters(
            since_id=since_id, via=via, status_min=status_min, status_max=status_max,
            path_prefix=path_prefix, person_id=person_id, since_ts=since_ts, until_ts=until_ts,
        )
        row = self._one(f"SELECT COUNT(*) AS n FROM activity_events WHERE {where}", tuple(params))
        return int(row["n"])

    def get_activity_event(self, event_id: int) -> ActivityEvent | None:
        row = self._one("SELECT * FROM activity_events WHERE id = ?", (event_id,))
        return None if row is None else self._activity(row)

    def get_dose_event_detail(self, event_id: int) -> dict[str, Any] | None:
        row = self._one(
            """SELECT e.id, e.medicine_id, e.state, e.slot_id, e.detail, e.created_at,
                      med.name AS medicine_name, med.person_id, p.name AS person_name
               FROM dose_events e
               JOIN medicines med ON med.id = e.medicine_id
               LEFT JOIN people p ON p.id = med.person_id
               WHERE e.id = ?""",
            (event_id,),
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "medicine_id": row["medicine_id"],
            "medicine_name": row["medicine_name"],
            "person_id": row["person_id"],
            "person_name": row["person_name"],
            "state": row["state"],
            "slot_id": row["slot_id"],
            "detail": row["detail"],
            "created_at": row["created_at"],
        }

    # ---- explorer search helpers
    def search_people(self, query: str) -> list[Person]:
        """Exact name_key match, or prefix on name_key. Never fuzzy (Maria must not match Mario)."""
        key = name_key(query)
        if not key:
            return []
        exact = self.get_person_by_name(query)
        if exact is not None:
            return [exact]
        rows = self._all(
            "SELECT * FROM people WHERE name_key LIKE ? ORDER BY name",
            (key + "%",),
        )
        return [self._person(r) for r in rows]

    def search_memories(
        self,
        text: str | None = None,
        *,
        person_id: int | None = None,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Memory hits with person attribution (id + name). Text match is substring, case-insensitive."""
        clauses = ["1=1"]
        params: list[Any] = []
        if person_id is not None:
            clauses.append("m.person_id = ?")
            params.append(person_id)
        if kind:
            clauses.append("m.kind = ?")
            params.append(kind)
        if since:
            clauses.append("m.created_at >= ?")
            params.append(since)
        if until:
            clauses.append("m.created_at <= ?")
            params.append(until)
        if text and text.strip():
            clauses.append("(m.title LIKE ? OR m.text LIKE ? OR m.kind LIKE ?)")
            needle = f"%{text.strip()}%"
            params.extend([needle, needle, needle])
        where = " AND ".join(clauses)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        rows = self._all(
            f"""SELECT m.*, p.name AS person_name FROM memories m
                JOIN people p ON p.id = m.person_id
                WHERE {where} ORDER BY m.id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )
        return [
            {
                "id": r["id"],
                "person_id": r["person_id"],
                "person_name": r["person_name"],
                "kind": r["kind"],
                "title": r["title"],
                "text": r["text"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def count_memories_search(
        self,
        text: str | None = None,
        *,
        person_id: int | None = None,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int:
        clauses = ["1=1"]
        params: list[Any] = []
        if person_id is not None:
            clauses.append("person_id = ?")
            params.append(person_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        if text and text.strip():
            clauses.append("(title LIKE ? OR text LIKE ? OR kind LIKE ?)")
            needle = f"%{text.strip()}%"
            params.extend([needle, needle, needle])
        row = self._one(
            f"SELECT COUNT(*) AS n FROM memories WHERE {' AND '.join(clauses)}", tuple(params)
        )
        return int(row["n"])

    def query_dose_events(
        self,
        *,
        person_id: int | None = None,
        medicine_id: int | None = None,
        state: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Dose events with medicine + person attribution. Newest first."""
        clauses = ["1=1"]
        params: list[Any] = []
        if person_id is not None:
            clauses.append("med.person_id = ?")
            params.append(person_id)
        if medicine_id is not None:
            clauses.append("e.medicine_id = ?")
            params.append(medicine_id)
        if state:
            clauses.append("e.state = ?")
            params.append(state)
        if since:
            clauses.append("e.created_at >= ?")
            params.append(since)
        if until:
            clauses.append("e.created_at <= ?")
            params.append(until)
        where = " AND ".join(clauses)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        rows = self._all(
            f"""SELECT e.id, e.medicine_id, e.state, e.slot_id, e.detail, e.created_at,
                       med.name AS medicine_name, med.person_id,
                       p.name AS person_name
                FROM dose_events e
                JOIN medicines med ON med.id = e.medicine_id
                LEFT JOIN people p ON p.id = med.person_id
                WHERE {where}
                ORDER BY e.id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )
        return [
            {
                "id": r["id"],
                "medicine_id": r["medicine_id"],
                "medicine_name": r["medicine_name"],
                "person_id": r["person_id"],
                "person_name": r["person_name"],
                "state": r["state"],
                "slot_id": r["slot_id"],
                "detail": r["detail"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def count_dose_events_query(
        self,
        *,
        person_id: int | None = None,
        medicine_id: int | None = None,
        state: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> int:
        clauses = ["1=1"]
        params: list[Any] = []
        if person_id is not None:
            clauses.append("med.person_id = ?")
            params.append(person_id)
        if medicine_id is not None:
            clauses.append("e.medicine_id = ?")
            params.append(medicine_id)
        if state:
            clauses.append("e.state = ?")
            params.append(state)
        if since:
            clauses.append("e.created_at >= ?")
            params.append(since)
        if until:
            clauses.append("e.created_at <= ?")
            params.append(until)
        row = self._one(
            f"""SELECT COUNT(*) AS n FROM dose_events e
                JOIN medicines med ON med.id = e.medicine_id
                WHERE {' AND '.join(clauses)}""",
            tuple(params),
        )
        return int(row["n"])

    def adherence_report(self, person_id: int | None = None, days: int = 7) -> dict[str, Any]:
        """Aggregate Taken / Missed / Failed from real dose_events in the last `days`."""
        days = max(1, min(int(days), 90))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
        clauses = ["e.created_at >= ?", "e.state IN ('Taken', 'Missed', 'Failed')"]
        params: list[Any] = [since]
        if person_id is not None:
            clauses.append("med.person_id = ?")
            params.append(person_id)
        rows = self._all(
            f"""SELECT e.state, COUNT(*) AS n FROM dose_events e
                JOIN medicines med ON med.id = e.medicine_id
                WHERE {' AND '.join(clauses)}
                GROUP BY e.state""",
            tuple(params),
        )
        counts = {"Taken": 0, "Missed": 0, "Failed": 0}
        for r in rows:
            if r["state"] in counts:
                counts[r["state"]] = int(r["n"])
        total = sum(counts.values())
        taken = counts["Taken"]
        return {
            "days": days,
            "since": since,
            "person_id": person_id,
            "taken": taken,
            "missed": counts["Missed"],
            "failed": counts["Failed"],
            "total_terminal": total,
            "adherence_pct": round(100.0 * taken / total, 1) if total else None,
        }

    def get_person_records(self, person_id: int) -> dict[str, Any] | None:
        """Full person dossier for the explorer. None if the person does not exist."""
        person = self.get_person(person_id)
        if person is None:
            return None
        medicines = self.list_medicines(person_id, all_people=False)
        schedules = []
        for med in medicines:
            for s in self.list_schedules_for_medicine(med.id, active_only=False):
                schedules.append({
                    "id": s.id,
                    "medicine_id": med.id,
                    "medicine_name": med.name,
                    "weekdays": s.weekdays,
                    "time_hhmm": s.time_hhmm,
                    "timezone": s.timezone,
                    "active": s.active,
                })
        memories = [
            {
                "id": m.id,
                "kind": m.kind,
                "title": m.title,
                "text": m.text,
                "created_at": m.created_at,
            }
            for m in self.list_memories(person_id=person_id, limit=100)
        ]
        doses = self.query_dose_events(person_id=person_id, limit=50)
        return {
            "person": public_person(person),
            "medicines": [{"id": m.id, "name": m.name, "person_id": m.person_id} for m in medicines],
            "schedules": schedules,
            "memories": memories,
            "dose_events": doses,
            "counts": {
                "medicines": len(medicines),
                "schedules": len(schedules),
                "memories": self.count_memories_search(person_id=person_id),
                "dose_events": self.count_dose_events_query(person_id=person_id),
                "activity": self.count_activity_events(person_id=person_id),
            },
        }

    _COUNTABLE = frozenset({
        "people", "medicines", "schedules", "memories", "dose_events", "activity_events",
        "calendar_events",
    })

    def count_rows(self, table: str, where: str = "") -> int:
        """COUNT(*) of a known table (the pages refresh often: never load whole tables to count them)."""
        if table not in self._COUNTABLE:
            raise ValueError(f"cannot count {table!r}")
        # where is only used with trusted literals from our own code (e.g. "active = 1")
        row = self._one(f"SELECT COUNT(*) AS n FROM {table}" + (f" WHERE {where}" if where else ""))
        return int(row["n"])


def public_person(person: Person) -> dict[str, Any]:
    """Serialize a person for API/UI responses — never includes passphrase material."""
    return {"id": person.id, "name": person.name, "role": person.role, "is_caregiver": person.is_caregiver}
