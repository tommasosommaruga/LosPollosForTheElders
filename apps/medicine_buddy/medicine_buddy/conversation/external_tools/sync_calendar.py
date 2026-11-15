"""External tool: push/pull the connected Google calendar."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)


def _api_request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    helper = Path(__file__).resolve().parent.parent / "lib" / "tool_http.py"
    spec = importlib.util.spec_from_file_location("medicine_tool_http", helper)
    if spec is None or spec.loader is None:
        return {"error": f"missing helper {helper}"}
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.api_request(method, path, body)


class SyncCalendar(Tool):
    name = "sync_calendar"
    description = (
        "Synchronize Medicine Buddy with the connected Google Calendar "
        "(push local schedules/events, pull Google events). "
        "Use when they say sync / synchronize / update my calendar / push to Google. "
        "Do NOT ask which calendar — Google is already configured. "
        "Always read speak_hint aloud."
    )
    parameters_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps, kwargs
        payload = _api_request("POST", "/calendar/sync", {})
        logger.info("sync_calendar → %s", payload.get("status") or payload.get("error"))
        return payload
