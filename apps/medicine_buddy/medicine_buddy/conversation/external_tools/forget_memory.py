"""External tool: delete one saved memory (owner or caregiver; asks first)."""

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


class ForgetMemory(Tool):
    name = "forget_memory"
    description = (
        "Delete one saved memory. Find its memory_id first with recall_person_memories. "
        "First call with confirmed=false, ask the question it returns, and only call again with "
        "confirmed=true after a clear yes. Needs the owner's or the caregiver's passphrase."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Whose memory"},
            "memory_id": {"type": "integer"},
            "confirmed": {"type": "boolean"},
            "passphrase": {"type": "string", "description": "Only needed if the voice guard is off"},
        },
        "required": ["person", "memory_id", "confirmed"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body = {k: kwargs.get(k) for k in ("person", "memory_id", "confirmed", "passphrase") if k in kwargs}
        payload = _api_request("POST", "/memories/forget", body)
        logger.info("forget_memory → %s", payload)
        return payload
