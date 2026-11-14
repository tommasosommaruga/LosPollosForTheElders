"""External tool: stop reminders for a medicine (asks for confirmation first)."""

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


class DeleteMedicineSchedule(Tool):
    name = "delete_medicine_schedule"
    description = (
        "Remove a medicine reminder (stop reminding). First call with confirmed=false: it returns "
        "a question to ask the person. Only call again with confirmed=true after they clearly say yes. "
        "If the medicine has several times, pass `time` to remove one, or all=true to remove every one. "
        "Read the returned speak_hint aloud."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Whose medicines (the person's name). Omit only if there is just one person."},
            "passphrase": {"type": "string", "description": "The passphrase of the person whose list it is (or the caregiver's), as just said. Only needed if the voice guard is off. Never repeat it aloud."},
            "name": {"type": "string", "description": "Medicine name as the person said it"},
            "confirmed": {"type": "boolean", "description": "True only after the person agreed to remove it"},
            "time": {"type": "string", "description": "Optional: which time to remove, e.g. '9 PM'"},
            "all": {"type": "boolean", "description": "Optional: remove every schedule for this medicine"},
        },
        "required": ["name", "confirmed"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body: dict[str, Any] = {
            "name": kwargs.get("name"),
            "confirmed": bool(kwargs.get("confirmed", False)),
            "all": bool(kwargs.get("all", False)),
        }
        for key in ("person", "passphrase"):
            if kwargs.get(key):
                body[key] = kwargs[key]
        if kwargs.get("time"):
            body["time"] = kwargs["time"]
        payload = _api_request("POST", "/schedules/delete", body)
        logger.info("delete_medicine_schedule → %s", payload)
        return payload
