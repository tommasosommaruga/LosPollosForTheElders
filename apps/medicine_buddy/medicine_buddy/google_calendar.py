"""Push companion events to Google Calendar via OAuth refresh token (stdlib only).

ICS subscribe/import cannot create events in Google. When
``MEDICINE_BUDDY_GOOGLE_CLIENT_ID``, ``_CLIENT_SECRET``, ``_REFRESH_TOKEN``, and a
calendar id are set, voice create/cancel also write to that Google calendar.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from medicine_buddy.calendar_ics import (
    ParsedEvent,
    byday_to_weekdays,
    medicine_name_from_summary,
    weekdays_to_byday,
)

logger = logging.getLogger("medicine_buddy.google_calendar")

TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL_API = "https://www.googleapis.com/calendar/v3"
# Keep short so voice tools do not stall the realtime session ("function outputs pending").
HTTP_TIMEOUT_S = 8

ENV_CLIENT_ID = "MEDICINE_BUDDY_GOOGLE_CLIENT_ID"
ENV_CLIENT_SECRET = "MEDICINE_BUDDY_GOOGLE_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "MEDICINE_BUDDY_GOOGLE_REFRESH_TOKEN"
ENV_CALENDAR_ID = "MEDICINE_BUDDY_GOOGLE_CALENDAR_ID"
SETTING_CALENDAR_ID = "google_calendar_id"


@dataclass(frozen=True)
class GoogleCalConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    calendar_id: str

    @property
    def configured(self) -> bool:
        return bool(
            self.client_id and self.client_secret and self.refresh_token and self.calendar_id
        )


def load_google_cal_config(calendar_id_from_db: str | None = None) -> GoogleCalConfig:
    """Credentials from env; calendar id from DB setting or env (DB wins when non-empty)."""
    cal_id = (calendar_id_from_db or "").strip() or (os.environ.get(ENV_CALENDAR_ID) or "").strip()
    return GoogleCalConfig(
        client_id=(os.environ.get(ENV_CLIENT_ID) or "").strip(),
        client_secret=(os.environ.get(ENV_CLIENT_SECRET) or "").strip(),
        refresh_token=(os.environ.get(ENV_REFRESH_TOKEN) or "").strip(),
        calendar_id=cal_id,
    )


def google_write_status(cfg: GoogleCalConfig) -> dict[str, Any]:
    """Family-page safe status (no secrets)."""
    missing: list[str] = []
    if not cfg.client_id:
        missing.append("client_id")
    if not cfg.client_secret:
        missing.append("client_secret")
    if not cfg.refresh_token:
        missing.append("refresh_token")
    if not cfg.calendar_id:
        missing.append("calendar_id")
    return {
        "configured": cfg.configured,
        "calendar_id": cfg.calendar_id,
        "missing": missing,
        "hint": (
            "Voice create/cancel will also write to this Google calendar."
            if cfg.configured
            else (
                "Set MEDICINE_BUDDY_GOOGLE_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN in .env "
                "(run scripts/google_calendar_oauth.py once), and paste the calendar id below "
                "(Google Calendar → Settings → Integrate calendar → Calendar ID)."
            )
        ),
    }


def build_google_event_body(
    *,
    title: str,
    description: str,
    time_hhmm: str,
    weekdays: str,
    starts_at: str,
    all_day: bool,
    tz_name: str,
) -> dict[str, Any]:
    """Map a local companion row into a Google Calendar events.insert body."""
    zone = tz_name or "UTC"
    start_day = _parse_starts_date(starts_at) or date.today()
    body: dict[str, Any] = {
        "summary": title,
        "description": description or "",
    }
    if all_day or not time_hhmm:
        body["start"] = {"date": start_day.isoformat()}
        body["end"] = {"date": (start_day + timedelta(days=1)).isoformat()}
    else:
        hour, minute = (int(x) for x in time_hhmm.split(":", 1))
        start_dt = datetime(start_day.year, start_day.month, start_day.day, hour, minute)
        end_dt = start_dt + timedelta(minutes=30)
        body["start"] = {"dateTime": start_dt.isoformat(timespec="seconds"), "timeZone": zone}
        body["end"] = {"dateTime": end_dt.isoformat(timespec="seconds"), "timeZone": zone}
        rrule = _rrule_for_weekdays(weekdays)
        if rrule:
            body["recurrence"] = [rrule]
    return body


def insert_google_event(cfg: GoogleCalConfig, event_body: dict[str, Any]) -> str:
    """Create an event; return Google event id. Raises RuntimeError on failure."""
    if not cfg.configured:
        raise RuntimeError("Google Calendar write is not configured")
    token = _access_token(cfg)
    url = f"{CAL_API}/calendars/{urllib.parse.quote(cfg.calendar_id, safe='')}/events"
    raw = _http_json("POST", url, event_body, token=token)
    event_id = str(raw.get("id") or "").strip()
    if not event_id:
        raise RuntimeError("Google Calendar did not return an event id")
    return event_id


def delete_google_event(cfg: GoogleCalConfig, event_id: str) -> None:
    """Delete by Google event id. Missing events are treated as success."""
    if not cfg.configured or not event_id:
        return
    token = _access_token(cfg)
    url = (
        f"{CAL_API}/calendars/{urllib.parse.quote(cfg.calendar_id, safe='')}"
        f"/events/{urllib.parse.quote(event_id, safe='')}"
    )
    try:
        _http_json("DELETE", url, None, token=token, expect_empty=True)
    except RuntimeError as exc:
        if "404" in str(exc) or "410" in str(exc):
            return
        raise


def probe_google_calendar(cfg: GoogleCalConfig) -> dict[str, Any]:
    """Lightweight connectivity check via events.list (matches calendar.events scope).

    ``calendars.get`` needs broader ``calendar`` scope and 403s with events-only tokens.
    """
    if not cfg.configured:
        return {"ok": False, "error": "not configured", **google_write_status(cfg)}
    try:
        token = _access_token(cfg)
        cal = urllib.parse.quote(cfg.calendar_id, safe="")
        # events.list is allowed with auth/calendar.events (our OAuth scope).
        url = f"{CAL_API}/calendars/{cal}/events?maxResults=1&singleEvents=true"
        meta = _http_json("GET", url, None, token=token)
        return {
            "ok": True,
            "summary": cfg.calendar_id,
            "calendar_id": cfg.calendar_id,
            "events_seen": len(meta.get("items") or []),
            "time_zone": "",
        }
    except Exception as exc:  # noqa: BLE001
        logger.info("Google Calendar probe failed: %s", exc)
        return {"ok": False, "error": str(exc)[:240], "calendar_id": cfg.calendar_id}


def list_google_events_as_parsed(
    cfg: GoogleCalConfig,
    *,
    tz_name: str,
    days_ahead: int = 60,
    max_results: int = 250,
) -> list[ParsedEvent]:
    """Pull upcoming (+ recurring master) events into ParsedEvent for local sync."""
    if not cfg.configured:
        return []
    token = _access_token(cfg)
    cal = urllib.parse.quote(cfg.calendar_id, safe="")
    now = datetime.now().astimezone()
    time_min = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(days=max(1, days_ahead))).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode(
        {
            "maxResults": str(max(1, min(max_results, 2500))),
            "singleEvents": "false",
            "timeMin": time_min,
            "timeMax": time_max,
            "orderBy": "updated",
        }
    )
    url = f"{CAL_API}/calendars/{cal}/events?{query}"
    raw = _http_json("GET", url, None, token=token)
    zone = ZoneInfo(tz_name) if tz_name else None
    out: list[ParsedEvent] = []
    for item in raw.get("items") or []:
        parsed = _google_item_to_parsed(item, zone)
        if parsed is not None:
            out.append(parsed)
    return out


def _google_item_to_parsed(item: dict[str, Any], tz: ZoneInfo | None) -> ParsedEvent | None:
    status = str(item.get("status") or "").lower()
    if status == "cancelled":
        return None
    gid = str(item.get("id") or "").strip()
    if not gid:
        return None
    summary = str(item.get("summary") or "").strip() or "(untitled)"
    description = str(item.get("description") or "").strip()
    start = item.get("start") or {}
    end = item.get("end") or {}
    del end  # unused; duration not stored locally
    all_day = "date" in start and "dateTime" not in start
    time_hhmm: str | None = None
    starts_at: str | None = None
    if all_day:
        starts_at = str(start.get("date") or "")[:10] or None
    else:
        raw_dt = str(start.get("dateTime") or "")
        if not raw_dt:
            return None
        try:
            dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            if tz is not None:
                dt = dt.astimezone(tz)
            time_hhmm = f"{dt.hour:02d}:{dt.minute:02d}"
            starts_at = dt.replace(tzinfo=None).isoformat(timespec="seconds")
        except ValueError:
            return None
    weekdays: str | None = None
    recurrence = item.get("recurrence") or []
    for line in recurrence:
        text = str(line)
        if "BYDAY=" in text.upper():
            m = re.search(r"BYDAY=([^;]+)", text, re.IGNORECASE)
            if m:
                weekdays = byday_to_weekdays(m.group(1))
        elif "FREQ=DAILY" in text.upper():
            weekdays = "*"
    if weekdays is None and time_hhmm and starts_at and not all_day:
        try:
            weekdays = str(datetime.fromisoformat(starts_at).weekday())
        except ValueError:
            weekdays = None
    return ParsedEvent(
        uid=f"gcal-{gid}@google",
        summary=summary,
        description=description,
        time_hhmm=time_hhmm,
        weekdays=weekdays,
        starts_at=starts_at,
        all_day=all_day,
        medicine_hint=medicine_name_from_summary(summary),
    )


def _rrule_for_weekdays(weekdays: str) -> str | None:
    raw = (weekdays or "").strip()
    if not raw:
        return None
    if raw == "*":
        return "RRULE:FREQ=DAILY"
    byday = weekdays_to_byday(raw)
    if not byday or byday.count(",") == 6:
        return "RRULE:FREQ=DAILY"
    return f"RRULE:FREQ=WEEKLY;BYDAY={byday}"


def _parse_starts_date(starts_at: str) -> date | None:
    if not starts_at:
        return None
    try:
        if "T" in starts_at:
            return datetime.fromisoformat(starts_at).date()
        return date.fromisoformat(starts_at[:10])
    except ValueError:
        return None


def _access_token(cfg: GoogleCalConfig) -> str:
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": cfg.refresh_token,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Google token refresh failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Google token refresh network error: {exc}") from exc
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Google token refresh returned no access_token")
    return token


def _http_json(
    method: str,
    url: str,
    body: dict[str, Any] | None,
    *,
    token: str,
    expect_empty: bool = False,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read()
            if expect_empty or not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"Google Calendar API {method} failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Google Calendar API network error: {exc}") from exc


def next_occurrence_date(weekdays: str, starts_at: str, *, as_of: date) -> date:
    """First occurrence on/after ``as_of`` (used when starts_at is missing)."""
    parsed = _parse_starts_date(starts_at)
    if parsed is not None and parsed >= as_of:
        return parsed
    raw = (weekdays or "").strip()
    if not raw or raw == "*":
        return as_of
    allowed = {int(p) for p in raw.split(",") if p.strip() != ""}
    for offset in range(0, 8):
        candidate = as_of + timedelta(days=offset)
        if candidate.weekday() in allowed:
            return candidate
    return as_of


def ensure_timezone_name(tz: ZoneInfo | None) -> str:
    if tz is None:
        return "UTC"
    return getattr(tz, "key", None) or str(tz) or "UTC"
