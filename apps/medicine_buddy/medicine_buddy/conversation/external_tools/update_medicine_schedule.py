"""External tool: change the time or days of an existing medicine schedule."""

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


class UpdateMedicineSchedule(Tool):
    name = "update_medicine_schedule"
    description = (
        "Change WHEN an already-saved medicine reminder happens (new time and/or new days). "
        "Use when the person says e.g. 'move my Aspirin to 8 PM' or 'Vitamin D only on weekdays'. "
        "If the medicine has several times, pass `time` = the current time to change. "
        "Read the returned speak_hint aloud: it confirms the change was saved correctly, "
        "or asks which schedule / says the medicine name was not recognized."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Whose medicines (the person's name). Omit only if there is just one person."},
            "passphrase": {"type": "string", "description": "The passphrase of the person whose list it is (or the caregiver's), as just said. Only needed if the voice guard is off. Never repeat it aloud."},
            "name": {"type": "string", "description": "Medicine name as the person said it"},
            "time": {"type": "string", "description": "Optional: the CURRENT time of the schedule to change, e.g. '09:00' or '9 AM'"},
            "new_time": {"type": "string", "description": "Optional: new time, e.g. '20:00' or '8 PM'"},
            "new_weekdays": {"type": "string", "description": "Optional: new days, e.g. 'daily', 'weekdays', 'Mon,Wed,Fri'"},
        },
        "required": ["name"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body = {k: kwargs.get(k) for k in ("name", "time", "new_time", "new_weekdays", "person", "passphrase") if kwargs.get(k)}
        payload = _api_request("POST", "/schedules/update", body)
        logger.info("update_medicine_schedule → %s", payload)
        return payload
