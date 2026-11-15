"""External tool: cancel a companion calendar event."""

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


class CancelCalendarEvent(Tool):
    name = "cancel_calendar_event"
    description = (
        "Remove a companion calendar event by title. Not for medicine reminders "
        "(use delete_medicine_schedule). Ask first with confirmed=false, then confirmed=true after yes."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Event title to remove"},
            "confirmed": {"type": "boolean", "description": "true only after the person clearly says yes"},
        },
        "required": ["title"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body = {
            "title": kwargs.get("title"),
            "confirmed": bool(kwargs.get("confirmed")),
        }
        payload = _api_request("POST", "/calendar/cancel", body)
        logger.info("cancel_calendar_event → %s", payload.get("status") or payload.get("error"))
        return payload
