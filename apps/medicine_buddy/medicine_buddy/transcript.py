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
import re
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from medicine_buddy.auth import secret_in_text
from medicine_buddy.calendar_mirror import cancel_mirror

MAX_LINES = 200
CHECKS_PER_REQUEST = 4
HIDDEN = "[passphrase hidden]"
_ROLES = {"user": "person", "assistant": "reachy"}
# Short confirms / save prompts — not part of the story body.
_SKIP_UTTERANCE = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|please|thanks|thank you|"
    r"save (it|this|that)|please save.*|finish( here)?( the story)?.*|"
    r"store (it|this)|keep (it|this)|that'?s (all|it)|done)$",
    re.IGNORECASE,
)
_META_TAIL = re.compile(
    r"[\s.,]*((please\s+)?(finish|save|store|keep)\b.{0,100})$",
    re.IGNORECASE,
)


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

    def recent_user_story_text(
        self,
        *,
        max_chars: int = 6000,
        max_age_s: float = 900.0,
        max_lines: int = 40,
    ) -> str:
        """Join recent person utterances into one story body (for faithful memory saves).

        Skips tiny confirmations like "yes" / "save it". Strips a trailing
        "finish… save it" meta-instruction when present. Empty if nothing useful.
        """
        now = time.time()
        with self._tlock:
            lines = [
                ln for ln in self._tlines
                if ln["who"] == "person" and now - ln["at"] <= max_age_s
            ]
        chunks: list[str] = []
        for ln in lines[-max_lines:]:
            text = (ln.get("text") or "").strip()
            if not text or text in {HIDDEN, "..."}:
                continue
            if _SKIP_UTTERANCE.match(text) or len(text) < 12:
                continue
            chunks.append(text)
        if not chunks:
            return ""
        body = " ".join(chunks).strip()
        body = _META_TAIL.sub("", body).strip()
        return body[: max(0, int(max_chars))]

    # ------------------------------------------------------------ database view
    def database_snapshot(self) -> dict[str, Any]:
        """Every table, without passphrase hashes/salts. Admin can delete rows from the UI."""
        q = self.db._all
        people = {r["id"]: r["name"] for r in q("SELECT id, name FROM people")}
        meds = {r["id"]: r["name"] for r in q("SELECT id, name FROM medicines")}

        def who(pid: Any) -> str:
            return people.get(pid, "unassigned") if pid is not None else "unassigned"

        tables = [
            {"name": "people", "note": "passphrases are stored only as salted hashes and never shown; "
             "delete people on the Family page",
             "deletable": False,
             "columns": ["id", "name", "role", "created"],
             "rows": [[r["id"], r["name"], r["role"], r["created_at"][:19]]
                      for r in q("SELECT id, name, role, created_at FROM people ORDER BY id")]},
            {"name": "medicines", "note": "delete removes schedules, dose history, and calendar mirrors",
             "deletable": True,
             "columns": ["id", "person", "medicine", "created"],
             "rows": [[r["id"], who(r["person_id"]), r["name"], r["created_at"][:19]]
                      for r in q("SELECT * FROM medicines ORDER BY id")]},
            {"name": "schedules", "note": "delete deactivates the schedule (history kept)",
             "deletable": True,
             "columns": ["id", "person", "medicine", "days", "time", "active"],
             "rows": [[r["id"], who(r["owner"]), meds.get(r["medicine_id"], "?"), r["weekdays"],
                       r["time_hhmm"], "yes" if r["active"] else "no"]
                      for r in q("SELECT s.*, m.person_id AS owner FROM schedules s "
                                 "LEFT JOIN medicines m ON m.id = s.medicine_id ORDER BY s.id")]},
            {"name": "calendar_events",
             "note": "all statuses, newest first (last 200). Delete removes the row; "
             "use this to clear Google sync duplicates",
             "deletable": True,
             "columns": ["id", "kind", "title", "days", "time", "status", "source", "uid", "google_id"],
             "rows": [[r["id"], r["kind"], r["title"], r["weekdays"], r["time_hhmm"], r["status"],
                       r["source"], r["uid"], r["external_id"] or ""]
                      for r in q(
                          "SELECT id, kind, title, weekdays, time_hhmm, status, source, uid, external_id "
                          "FROM calendar_events ORDER BY id DESC LIMIT 200"
                      )]},
            {"name": "dose_events", "note": "newest first, last 200",
             "deletable": True,
             "columns": ["id", "when (UTC)", "medicine", "state", "detail"],
             "rows": [[r["id"], r["created_at"][:19].replace("T", " "), meds.get(r["medicine_id"], "?"),
                       r["state"], r["detail"]]
                      for r in q("SELECT * FROM dose_events ORDER BY id DESC LIMIT 200")]},
            {"name": "memories", "note": "full text, newest first",
             "deletable": True,
             "columns": ["id", "person", "kind", "title", "text", "saved"],
             "rows": [[r["id"], who(r["person_id"]), r["kind"], r["title"], r["text"], r["created_at"][:19]]
                      for r in q("SELECT * FROM memories ORDER BY id DESC LIMIT 200")]},
            {"name": "app_settings", "note": "delete clears that setting key",
             "deletable": True, "id_column": "key",
             "columns": ["key", "value"],
             "rows": [[r["key"], r["value"]] for r in q("SELECT key, value FROM app_settings ORDER BY key")]},
        ]
        path = str(self.db.path)
        try:
            size = os.stat(path).st_size
        except OSError:
            size = None
        return {"path": path, "size_bytes": size, "tables": tables}

    def admin_delete_row(
        self,
        table: str,
        row_id: int | None = None,
        *,
        key: str | None = None,
    ) -> dict[str, Any]:
        """Admin delete from the Database page. People must use the Family page."""
        name = (table or "").strip()
        if name == "people":
            return {
                "error": "Delete people on the Family page.",
                "speak_hint": "Use the Family page to manage people.",
            }
        if name == "calendar_events":
            if row_id is None:
                return {"error": "id required"}
            if not self.db.delete_calendar_event(int(row_id)):
                return {"error": "row not found"}
            return {"status": "ok", "deleted": {"table": name, "id": int(row_id)}}
        if name == "dose_events":
            if row_id is None:
                return {"error": "id required"}
            if not self.db.delete_dose_event(int(row_id)):
                return {"error": "row not found"}
            return {"status": "ok", "deleted": {"table": name, "id": int(row_id)}}
        if name == "memories":
            if row_id is None:
                return {"error": "id required"}
            if not self.db.delete_memory(int(row_id)):
                return {"error": "row not found"}
            return {"status": "ok", "deleted": {"table": name, "id": int(row_id)}}
        if name == "schedules":
            if row_id is None:
                return {"error": "id required"}
            sid = int(row_id)
            if not self.db.deactivate_schedule(sid):
                return {"error": "row not found"}
            cancel_mirror(self.db, sid)
            cancel_google = getattr(self, "cancel_schedule_on_google_async", None)
            if callable(cancel_google):
                cancel_google(sid)
            return {"status": "ok", "deleted": {"table": name, "id": sid}}
        if name == "medicines":
            if row_id is None:
                return {"error": "id required"}
            mid = int(row_id)
            for schedule in self.db.list_schedules_for_medicine(mid, active_only=False):
                cancel_mirror(self.db, schedule.id)
                cancel_google = getattr(self, "cancel_schedule_on_google_async", None)
                if callable(cancel_google):
                    cancel_google(schedule.id)
            if not self.db.delete_medicine_cascade(mid):
                return {"error": "row not found"}
            return {"status": "ok", "deleted": {"table": name, "id": mid}}
        if name == "app_settings":
            setting_key = (key or "").strip()
            if not setting_key:
                return {"error": "key required"}
            self.db.set_setting(setting_key, "")
            return {"status": "ok", "deleted": {"table": name, "key": setting_key}}
        return {"error": f"Cannot delete from {name or 'unknown table'}"}
