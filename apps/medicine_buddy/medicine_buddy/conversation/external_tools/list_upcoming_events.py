"""External tool: list upcoming medicines and companion calendar events."""

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


class ListUpcomingEvents(Tool):
    name = "list_upcoming_events"
    description = (
        "List medicines and companion calendar events (appointments, visits, notes) in a time window. "
        "Pass when as a relative phrase (today, tomorrow, next week, soon, YYYY-MM-DD). "
        "Default when=soon covers from now through end of tomorrow. "
        "Always read the returned speak_hint aloud. Never invent events."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "description": (
                    "Relative window: soon (default), today, tomorrow, next week, "
                    "yesterday, a weekday, or YYYY-MM-DD"
                ),
            },
        },
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        when = kwargs.get("when")
        body: dict[str, Any] = {}
        if when is not None and str(when).strip():
            body["when"] = str(when).strip()
        payload = _api_request("POST", "/calendar/upcoming", body)
        logger.info("list_upcoming_events → %s", payload.get("status") or payload.get("error"))
        return payload
