"""Which clock reminders use.

The robot's own clock is usually UTC while the family lives somewhere else, so "9 AM" would fire
at the wrong hour. The time zone comes from (first match wins):
  1. the setting saved on the Family settings page,
  2. MEDICINE_BUDDY_TZ (env / .env), e.g. Europe/Zurich,
  3. the machine's own time zone.
"""

from __future__ import annotations

import os
from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SETTING_KEY = "timezone"


def load_zone(name: str) -> ZoneInfo:
    """ZoneInfo for an IANA name such as 'Europe/Zurich'; ValueError with a friendly message otherwise."""
    cleaned = (name or "").strip()
    if not cleaned or len(cleaned) > 64 or cleaned.startswith(("/", ".")) or ".." in cleaned:
        raise ValueError("Please give a time zone name like Europe/Zurich")
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"'{cleaned}' is not a known time zone. Use a name like Europe/Zurich or America/New_York.") from None


class ClockMixin:
    """Mixed into MedicineBuddyService (needs self.db)."""

    def tz_name(self) -> tuple[str, str]:
        """(name or '', where it came from: 'setting' | 'env' | 'system')."""
        saved = self.db.get_setting(SETTING_KEY)
        if saved:
            return saved, "setting"
        env = os.environ.get("MEDICINE_BUDDY_TZ", "").strip()
        if env:
            return env, "env"
        return "", "system"

    def tz(self) -> tzinfo | None:
        name, _ = self.tz_name()
        if not name:
            return None
        try:
            return load_zone(name)
        except ValueError:
            return None  # a bad value must never take reminders down: fall back to the system clock

    def now(self) -> datetime:
        zone = self.tz()
        return datetime.now(zone) if zone else datetime.now().astimezone()

    def settings_timezone(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """No `timezone` in body: report. With `timezone`: validate + save ('' = go back to default)."""
        body = body or {}
        if "timezone" in body:
            requested = str(body.get("timezone") or "").strip()
            if requested:
                try:
                    load_zone(requested)
                except ValueError as exc:
                    return {"error": str(exc)}
            self.db.set_setting(SETTING_KEY, requested)
        name, source = self.tz_name()
        now = self.now()
        return {
            "status": "ok",
            "timezone": name or "",
            "source": source,
            "robot_time": now.strftime("%H:%M"),
            "robot_date": now.strftime("%a %d %b %Y"),
            "utc_offset": now.strftime("%z"),
            "zone_label": name or now.tzname() or "system",
        }

    def clock_now(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read-only Family-TZ clock for voice tools. Never saves timezone (ignores body)."""
        del body
        payload = self.settings_timezone({})
        if "error" in payload:
            return payload
        now = self.now()
        payload["now_iso"] = now.isoformat(timespec="seconds")
        payload["weekday"] = now.strftime("%A")
        return payload
