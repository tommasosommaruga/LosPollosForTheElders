"""External tool: run arm dispense after the person confirms."""

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


class DispenseMedicine(Tool):
    name = "dispense_medicine"
    description = (
        "Bring something with the Maker Arm via POST /v1/dispense. "
        "Use for catalog medicines (Aspirin, Paracetamol) AND for bottles / blank pill / "
        "'small bottle' / what they see — put their words in medicine_name. "
        "Works even if not due / not scheduled (demo smooth). Prefer this over move_object. "
        "Ask once if needed; call with confirmed=true after yes or after they already "
        "said bring/give/take it. BEFORE that call, briefly say you are asking the arm. "
        "Returns quickly with status=arm_working — read speak_hint, KEEP chatting/listening; "
        "the arm finishes in the background and Reachy will announce when done. "
        "destination defaults to bowl; pass table/hand/etc when they ask for another place. "
        "Do not invent dosing advice."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Whose medicines (the person's name). Omit only if there is just one person."},
            "medicine_name": {"type": "string", "description": "Medicine name to dispense"},
            "destination": {
                "type": "string",
                "description": "Where to place it (default bowl). e.g. bowl, table, hand",
            },
            "confirmed": {
                "type": "boolean",
                "description": "True only after the person agreed to bring it",
            },
        },
        "required": ["medicine_name", "confirmed"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body: dict[str, Any] = {
            "person": kwargs.get("person"),
            "medicine_name": kwargs.get("medicine_name"),
            "confirmed": bool(kwargs.get("confirmed", False)),
            "stop_after_placed": True,
            "destination": kwargs.get("destination") or "bowl",
        }
        payload = _api_request("POST", "/doses/run", body)
        logger.info("dispense_medicine → %s", payload)
        return payload
