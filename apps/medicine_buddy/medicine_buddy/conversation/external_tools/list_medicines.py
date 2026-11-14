"""External tool: list saved medicines and schedules."""

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


class ListMedicines(Tool):
    name = "list_medicines"
    description = (
        "List medicines and their reminder times. Pass `person` (a name) to list one person's medicines, e.g. when someone asks what Grandpa Mario takes; omit it to list everyone's."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Optional: whose medicines (a name)"},
        },
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body = {"person": kwargs["person"]} if kwargs.get("person") else {}
        payload = _api_request("POST", "/medicines/list", body)
        logger.info("list_medicines → %s", payload)
        return payload
