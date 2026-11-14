"""External tool: mark adherence after Reachy asked the person."""

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


class MarkDoseTaken(Tool):
    name = "mark_dose_taken"
    description = (
        "After the medicine is in the bowl, ask if they took it. Then call this with "
        "taken=true or taken=false to log adherence. Never give medical advice."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Whose medicines (the person's name). Omit only if there is just one person."},
            "medicine_name": {"type": "string", "description": "Medicine name"},
            "taken": {
                "type": "boolean",
                "description": "Whether the person took the dose",
            },
        },
        "required": ["medicine_name", "taken"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        payload = _api_request(
            "POST",
            "/doses/adherence",
            {
                "person": kwargs.get("person"),
                "medicine_name": kwargs.get("medicine_name"),
                "taken": bool(kwargs.get("taken", False)),
            },
        )
        logger.info("mark_dose_taken → %s", payload)
        return payload
