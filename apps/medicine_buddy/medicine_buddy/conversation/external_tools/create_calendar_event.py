"""External tool: create a companion (non-medicine) calendar event."""

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


class CreateCalendarEvent(Tool):
    name = "create_calendar_event"
    description = (
        "Add a companion calendar event (doctor visit, family visit, walk, note). "
        "When Google Calendar write is connected, this also creates the event in Google. "
        "NOT for catalog medicines — use schedule_medicine for those. "
        "title: short name; when: tomorrow/today/YYYY-MM-DD (optional); "
        "weekdays: daily/weekdays/Mon,Wed/tomorrow; time: 09:00 or 1 AM (optional for all-day). "
        "Always read speak_hint aloud."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Event title, e.g. Physio appointment"},
            "when": {
                "type": "string",
                "description": "Day: tomorrow, today, a weekday name, or YYYY-MM-DD",
            },
            "weekdays": {
                "type": "string",
                "description": "daily, weekdays, Mon,Wed, or tomorrow (recurring that weekday)",
            },
            "time": {"type": "string", "description": "Local time HH:MM or 3 PM; omit for all-day"},
            "person": {"type": "string", "description": "Whose event, if known"},
            "description": {"type": "string", "description": "Optional short note"},
        },
        "required": ["title"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body: dict[str, Any] = {
            "title": kwargs.get("title"),
            "time": kwargs.get("time"),
            "person": kwargs.get("person"),
            "description": kwargs.get("description"),
        }
        if kwargs.get("when"):
            body["when"] = kwargs["when"]
        if kwargs.get("weekdays"):
            body["weekdays"] = kwargs["weekdays"]
        elif not kwargs.get("when"):
            body["weekdays"] = "daily"
        payload = _api_request("POST", "/calendar/create", body)
        logger.info("create_calendar_event → %s", payload.get("status") or payload.get("error"))
        return payload
