"""External tool: ask the Maker Arm to move a graspable non-medicine object."""

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


class MoveObject(Tool):
    name = "move_object"
    description = (
        "Legacy arm helper. For the live demo prefer dispense_medicine instead "
        "(including bottles / small bottle / blank pill) — that POSTs /v1/dispense. "
        "If you do call this, it redirects to the same dispense path when demo smooth is on. "
        "Refuse people, hot liquids, sharps, or opening packaging."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "object_label": {
                "type": "string",
                "description": "What to move, as the person said it, e.g. water bottle",
            },
            "destination": {
                "type": "string",
                "description": "Where to place it, e.g. bowl or table (default bowl)",
            },
            "kind_hint": {
                "type": "string",
                "description": "Optional: bottle, box, cup, or similar for the arm team",
            },
            "confirmed": {
                "type": "boolean",
                "description": "True only after the person agreed to the move",
            },
        },
        "required": ["object_label", "confirmed"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body = {
            "object_label": kwargs.get("object_label"),
            "destination": kwargs.get("destination") or "bowl",
            "kind_hint": kwargs.get("kind_hint"),
            "confirmed": bool(kwargs.get("confirmed", False)),
        }
        payload = _api_request("POST", "/arm/move_object", body)
        logger.info("move_object → %s", payload)
        return payload
