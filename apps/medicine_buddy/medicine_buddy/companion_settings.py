"""Companion / adaptive-care settings stored in app_settings (Family page + env).

- encourage_self_care: prefer asking the person to take the box themselves before the arm
- family_call_url / family_call_label: remote video link for caregivers
- check-in enable + quiet hours + interval
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

ENCOURAGE_SELF_KEY = "encourage_self_care"
FAMILY_CALL_URL_KEY = "family_call_url"
FAMILY_CALL_LABEL_KEY = "family_call_label"
CHECKIN_ENABLED_KEY = "checkins_enabled"
CHECKIN_INTERVAL_KEY = "checkin_interval_hours"
CHECKIN_QUIET_START_KEY = "checkin_quiet_start"
CHECKIN_QUIET_END_KEY = "checkin_quiet_end"
ACTIVITY_NUDGES_KEY = "activity_nudges_enabled"

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def validate_http_url(url: str, *, what: str = "URL") -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > 500:
        raise ValueError(f"That {what} is too long")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Use an http or https {what} (Meet, FaceTime, WhatsApp, Zoom, …)")
    return cleaned


def parse_hhmm(value: str, *, label: str) -> str:
    cleaned = (value or "").strip()
    m = _HHMM.match(cleaned)
    if not m:
        raise ValueError(f"{label} must look like 21:00")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _flag_from_db_or_env(db_get, key: str, env: str, default: bool) -> bool:
    saved = db_get(key)
    if saved is not None and str(saved).strip() != "":
        return str(saved).strip().lower() in {"1", "true", "yes", "on"}
    raw = os.environ.get(env)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return default


class CompanionSettingsMixin:
    """Mixed into MedicineBuddyService (needs self.db)."""

    def _init_companion_settings(self) -> None:
        return

    def encourage_self_care(self) -> bool:
        return _flag_from_db_or_env(
            self.db.get_setting, ENCOURAGE_SELF_KEY, "MEDICINE_BUDDY_ENCOURAGE_SELF", True,
        )

    def settings_encourage_self_care(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        if "enabled" in body:
            want = bool(body.get("enabled"))
            self.db.set_setting(ENCOURAGE_SELF_KEY, "1" if want else "0")
        enabled = self.encourage_self_care()
        return {
            "status": "ok",
            "enabled": enabled,
            "message": (
                "Encourage self-care is ON — Reachy asks the person to take the box themselves first."
                if enabled else
                "Encourage self-care is OFF — Reachy offers to bring the medicine straight away."
            ),
        }

    def family_call_url(self) -> str:
        return (self.db.get_setting(FAMILY_CALL_URL_KEY) or "").strip()

    def family_call_label(self) -> str:
        label = (self.db.get_setting(FAMILY_CALL_LABEL_KEY) or "").strip()
        return label or "family call"

    def settings_family_call(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        if "url" in body:
            try:
                cleaned = validate_http_url(str(body.get("url") or ""), what="call link")
            except ValueError as exc:
                return {"error": str(exc)}
            self.db.set_setting(FAMILY_CALL_URL_KEY, cleaned)
        if "label" in body:
            label = " ".join(str(body.get("label") or "").split())[:80]
            self.db.set_setting(FAMILY_CALL_LABEL_KEY, label)
        url = self.family_call_url()
        label = self.family_call_label()
        return {
            "status": "ok",
            "url": url,
            "label": label,
            "configured": bool(url),
            "message": (
                f"Family call link saved ({label})."
                if url else
                "No family call link yet. Paste a Meet / FaceTime / WhatsApp / Zoom URL."
            ),
        }

    def start_family_call(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Voice / tool entry: never invent a URL; point at the Family page if missing."""
        del body
        url = self.family_call_url()
        label = self.family_call_label()
        if not url:
            return {
                "status": "needs_setup",
                "configured": False,
                "speak_hint": (
                    "There is no family video-call link set up yet. Ask a caregiver to open the "
                    "Family settings page on the Medicine Buddy computer and paste a call link "
                    "(Google Meet, FaceTime, WhatsApp or Zoom)."
                ),
            }
        return {
            "status": "ok",
            "configured": True,
            "url": url,
            "label": label,
            "speak_hint": (
                f"A caregiver can start the {label} from the Family settings page on the Medicine "
                f"Buddy computer — tap Open call. The link is ready: {urlparse(url).netloc}."
            ),
        }

    def checkins_enabled(self) -> bool:
        return _flag_from_db_or_env(
            self.db.get_setting, CHECKIN_ENABLED_KEY, "MEDICINE_BUDDY_CHECKINS", True,
        )

    def checkin_interval_hours(self) -> float:
        saved = self.db.get_setting(CHECKIN_INTERVAL_KEY)
        raw = saved if saved not in (None, "") else os.environ.get("MEDICINE_BUDDY_CHECKIN_HOURS", "3")
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            hours = 3.0
        return max(0.5, min(hours, 24.0))

    def checkin_quiet_hours(self) -> tuple[str, str]:
        start = self.db.get_setting(CHECKIN_QUIET_START_KEY) or os.environ.get(
            "MEDICINE_BUDDY_CHECKIN_QUIET_START", "21:00",
        )
        end = self.db.get_setting(CHECKIN_QUIET_END_KEY) or os.environ.get(
            "MEDICINE_BUDDY_CHECKIN_QUIET_END", "08:00",
        )
        try:
            return parse_hhmm(str(start), label="Quiet start"), parse_hhmm(str(end), label="Quiet end")
        except ValueError:
            return "21:00", "08:00"

    def settings_checkins(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        if "enabled" in body:
            self.db.set_setting(CHECKIN_ENABLED_KEY, "1" if bool(body.get("enabled")) else "0")
        if "interval_hours" in body:
            try:
                hours = float(body.get("interval_hours"))
            except (TypeError, ValueError):
                return {"error": "interval_hours must be a number"}
            hours = max(0.5, min(hours, 24.0))
            self.db.set_setting(CHECKIN_INTERVAL_KEY, str(hours))
        if "quiet_start" in body or "quiet_end" in body:
            start = str(body.get("quiet_start") if "quiet_start" in body else self.checkin_quiet_hours()[0])
            end = str(body.get("quiet_end") if "quiet_end" in body else self.checkin_quiet_hours()[1])
            try:
                start_s = parse_hhmm(start, label="Quiet start")
                end_s = parse_hhmm(end, label="Quiet end")
            except ValueError as exc:
                return {"error": str(exc)}
            self.db.set_setting(CHECKIN_QUIET_START_KEY, start_s)
            self.db.set_setting(CHECKIN_QUIET_END_KEY, end_s)
        quiet_start, quiet_end = self.checkin_quiet_hours()
        enabled = self.checkins_enabled()
        return {
            "status": "ok",
            "enabled": enabled,
            "interval_hours": self.checkin_interval_hours(),
            "quiet_start": quiet_start,
            "quiet_end": quiet_end,
            "message": (
                f"Companion check-ins are ON every {self.checkin_interval_hours():g} h "
                f"(quiet {quiet_start}–{quiet_end})."
                if enabled else
                "Companion check-ins are OFF."
            ),
        }

    def activity_nudges_enabled(self) -> bool:
        return _flag_from_db_or_env(
            self.db.get_setting, ACTIVITY_NUDGES_KEY, "MEDICINE_BUDDY_ACTIVITY_NUDGES", True,
        )

    def settings_activity_nudges(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        if "enabled" in body:
            self.db.set_setting(ACTIVITY_NUDGES_KEY, "1" if bool(body.get("enabled")) else "0")
        enabled = self.activity_nudges_enabled()
        return {
            "status": "ok",
            "enabled": enabled,
            "message": (
                "Activity nudges are ON — Reachy may gently suggest a short stretch or chat."
                if enabled else
                "Activity nudges are OFF."
            ),
        }
