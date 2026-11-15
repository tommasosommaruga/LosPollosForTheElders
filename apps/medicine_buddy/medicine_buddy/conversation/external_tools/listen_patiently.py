"""External tool: stretch speech end-of-turn silence while someone tells a story or memory."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, ClassVar

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


class ListenPatiently(Tool):
    name = "listen_patiently"
    # Starting listen mode must not force a spoken "OK I'm listening" that cuts them off.
    needs_response: ClassVar[bool] = False
    description = (
        "Mark that you are in patient-listening mode for a LONG story or recipe only. "
        "Call action=start once when a long dictation clearly begins; action=stop when they "
        "finish or after save_memory. Do not use for normal chat or medicine questions. "
        "Keep brief warm spoken acknowledgements — never go fully silent."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "stop"],
                "description": "start = longer silence while they speak; stop = normal snappy turns",
            },
        },
        "required": ["action"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        action = str(kwargs.get("action") or "").strip().lower()
        if action not in {"start", "stop"}:
            return {"error": "action must be start or stop"}
        payload = _api_request("POST", "/voice/listen_mode", {"action": action})
        logger.info("listen_patiently(%s) → %s", action, payload)
        return payload
