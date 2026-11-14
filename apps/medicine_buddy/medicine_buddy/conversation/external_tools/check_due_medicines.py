"""External tool: list medicines due this minute."""

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


class CheckDueMedicines(Tool):
    name = "check_due_medicines"
    description = (
        "Check which medicines are due right now according to the Medicine Buddy "
        "schedule on the arm computer. Call this when the user asks about medicine "
        "time, or when you want to start a reminder."
    )
    parameters_schema = {"type": "object", "properties": {}, "required": []}

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps, kwargs
        payload = _api_request("GET", "/due")
        logger.info("check_due_medicines → %s", payload)
        return payload
