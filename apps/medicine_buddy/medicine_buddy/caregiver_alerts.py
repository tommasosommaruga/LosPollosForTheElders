"""Push a short notice to the caregiver when a dose is marked Missed.

Configured with MEDICINE_BUDDY_ALERT_URL (ntfy topic URL or any webhook that accepts a
POST) and/or the Family settings page. The DB setting wins over the env var. Disabled
when no URL is set or when the enable flag is off.

Body is plain text (works with ntfy.sh). Optional Title / Priority headers help ntfy.
No passphrase or extra medical detail is included — only person, medicine, and local time
(same level of info as the Family dashboard).
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger("medicine_buddy.alerts")

ALERT_URL_KEY = "caregiver_alert_url"
ALERT_ENABLED_KEY = "caregiver_alerts_enabled"
ENV_ALERT_URL = "MEDICINE_BUDDY_ALERT_URL"
ENV_ALERT_ENABLED = "MEDICINE_BUDDY_ALERTS"

# Injected in tests; production uses urllib.
PostFn = Callable[[str, str, str], tuple[bool, str]]


def _env_truthy(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes", "on"}


def validate_alert_url(url: str) -> str:
    """Return a cleaned http(s) URL or raise ValueError."""
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > 500:
        raise ValueError("That URL is too long")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Use an http or https URL (for example https://ntfy.sh/your-topic)")
    return cleaned


def default_post(url: str, title: str, message: str) -> tuple[bool, str]:
    """POST plain text to url. Returns (ok, detail)."""
    data = message.encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Title": title[:120],
            "Priority": "high",
            "Tags": "pill,warning",
            "User-Agent": "medicine-buddy-alerts",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            code = getattr(resp, "status", 200)
            if 200 <= int(code) < 300:
                return True, f"HTTP {code}"
            return False, f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc) or exc.__class__.__name__


class CaregiverAlertsMixin:
    """Mixed into MedicineBuddyService (needs self.db, self.now)."""

    alert_post: PostFn

    def _init_alerts(self) -> None:
        self.alert_post = default_post
        self.last_alert_error: str | None = None
        self.last_alert_at: float | None = None

    def alert_url(self) -> str:
        saved = self.db.get_setting(ALERT_URL_KEY)
        if saved is not None and str(saved).strip() != "":
            return str(saved).strip()
        return os.environ.get(ENV_ALERT_URL, "").strip()

    def alerts_enabled(self) -> bool:
        saved = self.db.get_setting(ALERT_ENABLED_KEY)
        if saved is not None and str(saved).strip() != "":
            return str(saved).strip().lower() in {"1", "true", "yes", "on"}
        # Default on when a URL exists (env or setting); off otherwise.
        if not self.alert_url():
            return False
        return _env_truthy(ENV_ALERT_ENABLED, "1")

    def settings_caregiver_alerts(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Report or update alert URL / enable flag; optional `test`: true sends a sample."""
        body = body or {}
        if "url" in body:
            try:
                cleaned = validate_alert_url(str(body.get("url") or ""))
            except ValueError as exc:
                return {"error": str(exc)}
            self.db.set_setting(ALERT_URL_KEY, cleaned)
        if "enabled" in body:
            want = bool(body.get("enabled"))
            self.db.set_setting(ALERT_ENABLED_KEY, "1" if want else "0")

        url = self.alert_url()
        enabled = self.alerts_enabled()
        out: dict[str, Any] = {
            "status": "ok",
            "url": url,
            "enabled": enabled,
            "configured": bool(url),
            "last_error": self.last_alert_error,
            "message": (
                "Caregiver alerts are ON."
                if enabled and url else
                "Caregiver alerts are OFF."
                if not enabled else
                "Set an alert URL (for example an ntfy topic) to notify the family of missed doses."
            ),
        }
        if body.get("test"):
            sent = self.send_caregiver_alert(
                title="Medicine Buddy test",
                message="Test alert from Medicine Buddy. Missed-dose notices will look like this.",
                person_id=None,
                path="/alerts/test",
            )
            out["test"] = sent
            if not sent.get("ok"):
                out["error"] = sent.get("detail") or "Could not send the test alert"
                return out
            out["message"] = "Test alert sent."
        return out

    def send_caregiver_alert(
        self,
        *,
        title: str,
        message: str,
        person_id: int | None = None,
        path: str = "/alerts/missed",
    ) -> dict[str, Any]:
        """Send one alert if enabled and a URL is set. Always safe to call."""
        if not self.alerts_enabled():
            return {"ok": False, "skipped": True, "detail": "alerts disabled"}
        url = self.alert_url()
        if not url:
            return {"ok": False, "skipped": True, "detail": "no alert URL"}
        try:
            ok, detail = self.alert_post(url, title, message)
        except Exception as exc:  # noqa: BLE001 — never break the reminder loop
            ok, detail = False, str(exc) or exc.__class__.__name__
        self.last_alert_at = time.time()
        self.last_alert_error = None if ok else detail
        try:
            self.db.insert_activity_event(
                at_ts=time.time(),
                via="alert",
                method="POST",
                path=path,
                status=200 if ok else 502,
                ms=0.0,
                result="ok" if ok else "error",
                error=None if ok else detail,
                speak_hint=message[:200],
                person_id=person_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not log caregiver alert activity")
        if ok:
            logger.info("Caregiver alert sent (%s): %s", detail, title)
        else:
            logger.warning("Caregiver alert failed (%s): %s", detail, title)
        return {"ok": ok, "detail": detail, "url_host": urlparse(url).netloc}

    def notify_missed_dose(
        self,
        *,
        person: str | None,
        medicine: str,
        local_time: str,
        person_id: int | None = None,
        reminders_spoken: int = 0,
    ) -> dict[str, Any]:
        who = (person or "Someone").strip() or "Someone"
        med = (medicine or "medicine").strip() or "medicine"
        title = f"Missed dose: {med}"
        extra = f" after {reminders_spoken} reminders" if reminders_spoken else ""
        message = (
            f"Medicine Buddy: {who}'s {med} was marked Missed at {local_time}{extra}. "
            f"Please check in when you can."
        )
        return self.send_caregiver_alert(
            title=title, message=message, person_id=person_id, path="/alerts/missed",
        )
