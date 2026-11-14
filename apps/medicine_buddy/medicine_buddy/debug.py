"""Debug mode: a switch, a small log of recent requests, and a snapshot of internal state.

Privacy by design - nothing here ever contains what people SAID or any passphrase:
- requests are logged as method, path, status, duration and the outcome fields
  (status / error / blocked / speak_hint) - never request bodies,
- the voice guard is shown as counts, who is unlocked (names) and open questions.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import time
from collections import deque
from datetime import datetime
from typing import Any

_LOGGERS = ("medicine_buddy",)
_OUTCOME_KEYS = ("status", "error", "blocked", "final_state", "verified", "speak_hint")


class DebugMixin:
    """Mixed into MedicineBuddyService (needs self.db, self.guard, self.config, self.catalog, self.now)."""

    def _init_debug(self) -> None:
        self.started_at = time.time()
        self.requests: deque[dict[str, Any]] = deque(maxlen=150)
        self.debug_enabled = False
        self.set_debug(os.environ.get("MEDICINE_BUDDY_DEBUG", "0").lower() in {"1", "true", "yes", "on"})

    def set_debug(self, enabled: bool) -> dict[str, Any]:
        self.debug_enabled = bool(enabled)
        level = logging.DEBUG if self.debug_enabled else logging.INFO
        for name in _LOGGERS:
            logging.getLogger(name).setLevel(level)
        logging.getLogger("medicine_buddy").info("Debug mode %s", "ON" if self.debug_enabled else "OFF")
        return {"debug": self.debug_enabled}

    def record_request(self, method: str, path: str, status: int, ms: float,
                       payload: dict[str, Any] | None = None, via: str = "api") -> None:
        outcome = {}
        for k in _OUTCOME_KEYS:
            if payload and k in payload:
                v = payload[k]
                # the response's own "status" (e.g. "ok") must not overwrite the HTTP status code
                outcome["result" if k == "status" else k] = v[:160] if isinstance(v, str) else v
        entry = {"at": time.time(), "via": via, "method": method, "path": path.split("?")[0],
                 "status": status, "ms": round(ms, 1), **outcome}
        self.requests.append(entry)
        if self.debug_enabled:
            logging.getLogger("medicine_buddy.requests").debug(
                "%s %s %s -> %s (%.0f ms) %s", via, method, entry["path"], status, ms,
                {k: v for k, v in outcome.items() if k != "speak_hint"})

    def debug_snapshot(self) -> dict[str, Any]:
        people = {p.id: p for p in self.db.list_people()}
        g = self.guard
        unlocked = [{"name": people[pid].name if pid in people else f"#{pid}", "seconds_left": s}
                    for pid, s in sorted(g.unlock_status().items(), key=lambda x: -x[1])]
        try:
            import importlib.metadata as md
            versions = {pkg: md.version(pkg) for pkg in ("medicine_buddy", "reachy-mini", "reachy_mini_conversation_app")
                        if _installed(md, pkg)}
        except Exception:  # noqa: BLE001
            versions = {}
        return {
            "debug": self.debug_enabled,
            "now": self.now().isoformat(timespec="seconds"),
            "timezone": self.settings_timezone({}),
            "reminders": (self.reminder_loop.status() if getattr(self, "reminder_loop", None)
                          else {"running": False, "note": "proactive reminders are off"}),
            "uptime_s": int(time.time() - self.started_at),
            "app": {
                "versions": versions,
                "python": sys.version.split()[0],
                "platform": platform.platform(terse=True),
                "dry_run": self.config.dry_run,
                "database": str(self.db.path),
                "catalog_size": len(self.catalog.names),
            },
            "voice_guard": {
                "enabled": g.enabled,
                "utterances_heard": len(g._heard),
                "seconds_since_last_speech": g.seconds_since_speech(),
                "unlocked_people": unlocked,
                "open_questions": g.pending_status(),
                "blocked": [
                    {**b, "at": datetime.fromtimestamp(b["at"]).strftime("%H:%M:%S")} for b in list(g.blocked)[::-1]
                ],
            },
            "data": {
                "people": len(people),
                "caregivers": sum(1 for p in people.values() if p.is_caregiver),
                "medicines": self.db.count_rows("medicines"),
                "active_schedules": self.db.count_rows("schedules", "active = 1"),
                "memories": self.db.count_rows("memories"),
                "dose_events": self.db.count_rows("dose_events"),
            },
            "recent_requests": [
                {**r, "at": datetime.fromtimestamp(r["at"]).strftime("%H:%M:%S")} for r in list(self.requests)[::-1][:60]
            ],
        }


def _installed(md: Any, pkg: str) -> bool:
    try:
        md.version(pkg)
        return True
    except md.PackageNotFoundError:
        return False
