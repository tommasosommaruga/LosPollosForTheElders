"""Reset Reachy Mini microphone via the daemon REST API (demo recovery).

The conversation ``volume_control`` tool often hits ``POST /api/volume/microphone/set``
timeouts while audio is live. This helper retries a mic set to 100% and clears
incoming audio buffers so listening can recover without restarting the whole app.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("medicine_buddy.media_reset")

DEFAULT_DAEMON = "http://127.0.0.1:8000"
DEFAULT_MIC_VOLUME = 100
SET_TIMEOUT_S = 4.0
GET_TIMEOUT_S = 3.0


def daemon_base_url() -> str:
    return (os.environ.get("MEDICINE_BUDDY_DAEMON_URL") or DEFAULT_DAEMON).rstrip("/")


def _http_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    *,
    timeout_s: float,
) -> tuple[int, dict[str, Any] | None, str]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
            parsed: dict[str, Any] | None
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw[:200]}
            return int(resp.status), parsed, ""
    except urllib.error.HTTPError as exc:
        err = ""
        try:
            err = exc.read(800).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err = str(exc.reason)
        return int(exc.code), None, err or str(exc.reason)
    except Exception as exc:  # noqa: BLE001 — surface timeout / connection as step error
        return 0, None, str(exc)


class MediaResetMixin:
    """Mixed into MedicineBuddyService."""

    def reset_microphone(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Set mic to 100% (retry) + clear incoming audio. Safe to mash during a demo."""
        body = body or {}
        try:
            volume = int(body.get("volume", DEFAULT_MIC_VOLUME))
        except (TypeError, ValueError):
            volume = DEFAULT_MIC_VOLUME
        volume = max(0, min(100, volume))

        base = daemon_base_url()
        steps: list[dict[str, Any]] = []

        # 1) Clear stuck input buffers (best-effort).
        code, payload, err = _http_json(
            "POST", f"{base}/api/media/clear_incoming_audio", {}, timeout_s=GET_TIMEOUT_S,
        )
        steps.append({
            "step": "clear_incoming_audio",
            "ok": 200 <= code < 300,
            "http": code or None,
            "detail": err or payload,
        })

        # 2) Set mic volume with a few short tries (Control UI often times out once).
        set_ok = False
        set_payload: dict[str, Any] | None = None
        set_err = ""
        for attempt in range(1, 4):
            code, payload, err = _http_json(
                "POST",
                f"{base}/api/volume/microphone/set",
                {"volume": volume},
                timeout_s=SET_TIMEOUT_S,
            )
            steps.append({
                "step": f"microphone_set_try_{attempt}",
                "ok": 200 <= code < 300,
                "http": code or None,
                "detail": err or payload,
            })
            if 200 <= code < 300:
                set_ok = True
                set_payload = payload
                break
            set_err = err or f"HTTP {code}"

        # 3) Read back current level.
        code, current, err = _http_json(
            "GET", f"{base}/api/volume/microphone/current", timeout_s=GET_TIMEOUT_S,
        )
        steps.append({
            "step": "microphone_current",
            "ok": 200 <= code < 300,
            "http": code or None,
            "detail": err or current,
        })
        current_vol = None
        if isinstance(current, dict) and "volume" in current:
            try:
                current_vol = int(current["volume"])
            except (TypeError, ValueError):
                current_vol = None

        ok = set_ok or (current_vol is not None and current_vol >= max(80, volume - 5))
        if ok:
            logger.info("Microphone reset ok volume=%s current=%s", volume, current_vol)
            return {
                "status": "ok",
                "ok": True,
                "volume": volume,
                "current_volume": current_vol,
                "daemon": base,
                "steps": steps,
                "message": (
                    f"Microphone reset — volume set to {volume}%"
                    + (f" (now {current_vol}%)." if current_vol is not None else ".")
                ),
                "speak_hint": "I've reset the microphone. Please try speaking again.",
            }

        logger.warning("Microphone reset failed: %s", set_err or steps)
        return {
            "status": "error",
            "ok": False,
            "volume": volume,
            "current_volume": current_vol,
            "daemon": base,
            "steps": steps,
            "error": set_err or "could not set microphone volume",
            "message": (
                "Could not reset the microphone through the robot daemon. "
                "Try Reachy Mini Control → restart the app, or reboot the robot."
            ),
            "speak_hint": "I couldn't reset the microphone from here. Please check the robot control page.",
        }
