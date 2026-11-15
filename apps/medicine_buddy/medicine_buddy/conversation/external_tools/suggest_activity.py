"""External tool: suggest a light non-clinical motor, cognitive or social activity."""

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


class SuggestActivity(Tool):
    name = "suggest_activity"
    description = (
        "Suggest one optional light activity (stretch, memory chat, short walk with their aid, "
        "or calling family). Not medical therapy. After they accept or decline, call again with "
        "response set to accepted or declined so it is logged for caregivers."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "Optional: motor, cognitive, or social",
            },
            "activity_id": {
                "type": "string",
                "description": "Optional fixed id such as stand_stretch or memory_question",
            },
            "response": {
                "type": "string",
                "description": "accepted or declined after they answer (optional)",
            },
        },
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body = {
            "kind": kwargs.get("kind"),
            "activity_id": kwargs.get("activity_id"),
            "response": kwargs.get("response"),
        }
        payload = _api_request("POST", "/activities/suggest", body)
        logger.info("suggest_activity → %s", payload)
        return payload
