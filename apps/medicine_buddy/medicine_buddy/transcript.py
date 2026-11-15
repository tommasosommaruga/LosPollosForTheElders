"""What Reachy and the person said (live conversation view) + a read-only view of the database.

Transcript privacy:
- kept in MEMORY only (last 200 lines), never written to the database or to log files,
  so it is gone when the app restarts,
- a line containing someone's spoken passphrase is replaced by "[passphrase hidden]". Checking
  hashes words (slow on the robot), so it is done lazily and a few lines at a time while the
  page is open; unchecked lines show as "..." until then.
The database view never includes passphrase hashes or salts.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from medicine_buddy.auth import secret_in_text

MAX_LINES = 200
CHECKS_PER_REQUEST = 4
HIDDEN = "[passphrase hidden]"
_ROLES = {"user": "person", "assistant": "reachy"}


class TranscriptMixin:
    """Mixed into MedicineBuddyService (needs self.db)."""

    def _init_transcript(self) -> None:
        self._tlock = threading.Lock()
        self._tlines: deque[dict[str, Any]] = deque(maxlen=MAX_LINES)
        self._tseq = 0

    def record_transcript(self, role: str, text: str) -> None:
        """Called (in-process) for every FINAL transcript line of the conversation."""
        text = (text or "").strip()
        if not text or role not in _ROLES:
            return
        with self._tlock:
            self._tseq += 1
            self._tlines.append({"id": self._tseq, "at": time.time(), "who": _ROLES[role],
                                 "text": text, "checked": False})

    def _check_lines(self, budget: int) -> None:
        """Look for spoken passphrases in at most `budget` unchecked lines (newest first)."""
        with self._tlock:
            todo = [ln for ln in reversed(self._tlines) if not ln["checked"]][:budget]
        if not todo:
            return
        people = self.db.list_people()
        for ln in todo:
            hit = any(secret_in_text(p.secret, ln["text"]) for p in people)
            with self._tlock:
                if hit:
                    ln["text"] = HIDDEN
                ln["checked"] = True

    def transcript_snapshot(self, after: int = 0) -> dict[str, Any]:
        self._check_lines(CHECKS_PER_REQUEST)
        with self._tlock:
            lines = [
                {"id": ln["id"], "time": datetime.fromtimestamp(ln["at"]).strftime("%H:%M:%S"),
                 "who": ln["who"], "text": ln["text"] if ln["checked"] else "..."}
                for ln in self._tlines if ln["id"] > after
            ]
            last = self._tseq
        return {"lines": lines, "last_id": last}

    def clear_transcript(self) -> dict[str, Any]:
        with self._tlock:
            self._tlines.clear()
        return {"status": "cleared"}

    # ------------------------------------------------------------ database view
    def database_snapshot(self) -> dict[str, Any]:
        """Every table, read-only, without passphrase hashes/salts."""
        q = self.db._all
        people = {r["id"]: r["name"] for r in q("SELECT id, name FROM people")}
        meds = {r["id"]: r["name"] for r in q("SELECT id, name FROM medicines")}

        def who(pid: Any) -> str:
            return people.get(pid, "unassigned") if pid is not None else "unassigned"

        tables = [
            {"name": "people", "note": "passphrases are stored only as salted hashes and never shown",
             "columns": ["id", "name", "role", "created"],
             "rows": [[r["id"], r["name"], r["role"], r["created_at"][:19]]
                      for r in q("SELECT id, name, role, created_at FROM people ORDER BY id")]},
            {"name": "medicines", "columns": ["id", "person", "medicine", "created"],
             "rows": [[r["id"], who(r["person_id"]), r["name"], r["created_at"][:19]]
                      for r in q("SELECT * FROM medicines ORDER BY id")]},
            {"name": "schedules", "columns": ["id", "person", "medicine", "days", "time", "active"],
             "rows": [[r["id"], who(r["owner"]), meds.get(r["medicine_id"], "?"), r["weekdays"],
                       r["time_hhmm"], "yes" if r["active"] else "no"]
                      for r in q("SELECT s.*, m.person_id AS owner FROM schedules s "
                                 "LEFT JOIN medicines m ON m.id = s.medicine_id ORDER BY s.id")]},
            {"name": "dose_events", "note": "newest first, last 200",
             "columns": ["id", "when (UTC)", "medicine", "state", "detail"],
             "rows": [[r["id"], r["created_at"][:19].replace("T", " "), meds.get(r["medicine_id"], "?"),
                       r["state"], r["detail"]]
                      for r in q("SELECT * FROM dose_events ORDER BY id DESC LIMIT 200")]},
            {"name": "memories", "note": "full text, newest first",
             "columns": ["id", "person", "kind", "title", "text", "saved"],
             "rows": [[r["id"], who(r["person_id"]), r["kind"], r["title"], r["text"], r["created_at"][:19]]
                      for r in q("SELECT * FROM memories ORDER BY id DESC LIMIT 200")]},
            {"name": "app_settings", "columns": ["key", "value"],
             "rows": [[r["key"], r["value"]] for r in q("SELECT key, value FROM app_settings ORDER BY key")]},
        ]
        path = str(self.db.path)
        try:
            size = os.stat(path).st_size
        except OSError:
            size = None
        return {"path": path, "size_bytes": size, "tables": tables}
