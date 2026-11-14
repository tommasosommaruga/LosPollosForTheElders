"""External tool: save a medicine schedule via Medicine Buddy API."""

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


class ScheduleMedicine(Tool):
    name = "schedule_medicine"
    description = (
        "Save a NEW medicine reminder. Use when they say they take a medicine on certain days/times. "
        "Only medicines on the allowed list are accepted; pass the name as the person said it and the "
        "server recognizes it (or returns suggestions to ask about). "
        "weekdays: 'daily', 'weekdays', 'weekends' or names like 'Mon,Wed,Fri'. time: '09:00' or '9 AM'. "
        "Always read the returned speak_hint aloud: it confirms the reminder was set up correctly. "
        "To change or remove an existing reminder use update_medicine_schedule / delete_medicine_schedule."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Medicine name, e.g. Aspirin"},
            "weekdays": {"type": "string", "description": "daily or weekday list"},
            "time": {"type": "string", "description": "Local time HH:MM"},
        },
        "required": ["name", "weekdays", "time"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        payload = _api_request(
            "POST",
            "/schedules",
            {
                "name": kwargs.get("name"),
                "weekdays": kwargs.get("weekdays", "daily"),
                "time": kwargs.get("time"),
            },
        )
        logger.info("schedule_medicine → %s", payload)
        return payload
