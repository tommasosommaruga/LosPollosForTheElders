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
        "weekdays: 'daily', 'weekdays', 'weekends', names like 'Mon,Wed,Fri', or 'today'/'tomorrow' "
        "(those become a recurring reminder for that weekday). time: '09:00' or '9 AM'. "
        "FIRST-TIME SETUP: if the person is not registered yet, the tool returns needs_passphrase_setup. "
        "Ask them to choose 3–4 easy words and SAY THEM TWICE out loud, then call this tool again with "
        "new_passphrase set to those words (do not invent a passphrase; only use words they said). "
        "That registers them and saves the reminder in one step. "
        "Always read the returned speak_hint aloud. "
        "To change or remove an existing reminder use update_medicine_schedule / delete_medicine_schedule."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Whose medicines (the person's name). Omit only if there is just one person."},
            "passphrase": {"type": "string", "description": "Existing passphrase (only if voice guard is off). Never invent one. Never repeat it aloud."},
            "new_passphrase": {
                "type": "string",
                "description": (
                    "ONLY for first-time setup of a new person: the 3–4 words they just said twice. "
                    "Never invent this. Omit once they are already registered."
                ),
            },
            "name": {"type": "string", "description": "Medicine name, e.g. Aspirin"},
            "weekdays": {
                "type": "string",
                "description": (
                    "daily, weekdays, weekends, names like Mon,Wed,Fri, or today/tomorrow "
                    "(today/tomorrow store a recurring schedule for that weekday)"
                ),
            },
            "time": {"type": "string", "description": "Local time HH:MM"},
        },
        "required": ["name", "weekdays", "time"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body = {
            "person": kwargs.get("person"),
            "passphrase": kwargs.get("passphrase"),
            "name": kwargs.get("name"),
            "weekdays": kwargs.get("weekdays", "daily"),
            "time": kwargs.get("time"),
        }
        if kwargs.get("new_passphrase"):
            body["new_passphrase"] = kwargs["new_passphrase"]
        payload = _api_request("POST", "/schedules", body)
        logger.info("schedule_medicine → %s", {k: v for k, v in payload.items() if k != "pending"})
        return payload
