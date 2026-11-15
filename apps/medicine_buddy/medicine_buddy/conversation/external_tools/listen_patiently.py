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
        "Enter or leave patient listening for a story, recipe, or memory the person is dictating. "
        "Call with action=start as soon as they begin telling something they may want saved — "
        "this waits longer through short pauses so you do not interrupt. "
        "Call action=stop when they say they are finished / that's the end, or after you saved "
        "the memory. During patient listening stay mostly quiet (a soft mm-hmm is fine); do not "
        "ask questions mid-story. Everyday chat does not need this tool."
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
