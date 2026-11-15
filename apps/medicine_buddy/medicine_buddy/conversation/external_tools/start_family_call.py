"""External tool: family video call — coming soon stub (no live bridge yet)."""

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


class StartFamilyCall(Tool):
    name = "start_family_call"
    description = (
        "Family video call is coming soon and not available yet. Call this when they ask to "
        "video-call family, then read the speak_hint aloud — tell them to use their phone or "
        "tablet for now. Never invent a video link."
    )
    parameters_schema = {"type": "object", "properties": {}, "required": []}

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps, kwargs
        payload = _api_request("POST", "/family/call", {})
        logger.info("start_family_call → %s", payload.get("status") or payload.get("error"))
        return payload
