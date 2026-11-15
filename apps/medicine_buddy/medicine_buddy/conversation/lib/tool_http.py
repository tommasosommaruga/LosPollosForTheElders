"""Shared HTTP helpers inlined for pollen external-tool loading."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8787"
# Short default keeps calendar/Google from stalling voice. Arm POSTs need longer.
DEFAULT_TIMEOUT_S = 25.0
# Arm pick/place can take up to MEDICINE_BUDDY_ARM_TIMEOUT_S (often 90); leave headroom.
ARM_TOOL_TIMEOUT_S = 100.0
_ARM_PATHS = ("/arm/move_object", "/doses/run", "/doses/adherence")


def base_url() -> str:
    return os.environ.get("MEDICINE_BUDDY_URL", DEFAULT_URL).rstrip("/")


def _timeout_for_path(path: str, override: float | None) -> float:
    if override is not None:
        return max(5.0, float(override))
    cleaned = (path or "").rstrip("/") or "/"
    if cleaned in _ARM_PATHS or cleaned.startswith("/arm/"):
        env = (os.environ.get("MEDICINE_BUDDY_ARM_TOOL_TIMEOUT_S") or "").strip()
        if env:
            try:
                return max(30.0, float(env))
            except ValueError:
                pass
        return ARM_TOOL_TIMEOUT_S
    return DEFAULT_TIMEOUT_S


def api_request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    url = f"{base_url()}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    wait_s = _timeout_for_path(path, timeout)
    try:
        with urllib.request.urlopen(req, timeout=wait_s) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except TimeoutError:
        return {
            "error": f"Medicine Buddy timed out after {wait_s:.0f}s at {path}",
            "speak_hint": (
                "That took too long on my side. Please try again in a moment, "
                "or do it by hand for now."
            ),
        }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"error": raw or str(exc)}
        if "error" not in payload:
            payload["error"] = str(exc)
        return payload
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        # urlopen may wrap socket timeout as URLError
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return {
                "error": f"Medicine Buddy timed out after {wait_s:.0f}s at {path}",
                "speak_hint": (
                    "That took too long on my side. Please try again in a moment, "
                    "or do it by hand for now."
                ),
            }
        return {
            "error": (
                f"Cannot reach Medicine Buddy at {base_url()} ({reason}). "
                "Is the Medicine Buddy app running?"
            )
        }
