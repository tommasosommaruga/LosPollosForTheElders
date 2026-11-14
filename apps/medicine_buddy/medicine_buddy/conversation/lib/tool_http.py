"""Shared HTTP helpers inlined for pollen external-tool loading."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8787"


def base_url() -> str:
    return os.environ.get("MEDICINE_BUDDY_URL", DEFAULT_URL).rstrip("/")


def api_request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{base_url()}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
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
        return {
            "error": (
                f"Cannot reach Medicine Buddy at {base_url()} ({exc.reason}). "
                "Is `medicine-buddy serve` running on the arm PC?"
            )
        }
