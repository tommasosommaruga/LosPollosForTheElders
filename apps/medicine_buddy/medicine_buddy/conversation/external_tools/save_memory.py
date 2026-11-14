"""External tool: keep a story, recipe, skill or tradition in a person's family memories."""

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


class SaveMemory(Tool):
    name = "save_memory"
    description = (
        "Save something a person wants to pass on to their children and grandchildren: a story, "
        "recipe, skill, tradition, advice or piece of family history. ONLY call this after you asked "
        "'Would you like me to save this in your memories?' and they said yes. Write `text` as faithfully "
        "as possible in their own words (for a recipe keep ingredients and steps). Saving needs that "
        "person's passphrase or the caregiver's, said recently. Never include a passphrase in the text."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Whose memory this is (their name)"},
            "kind": {"type": "string",
                     "enum": ["story", "recipe", "skill", "tradition", "advice", "family history", "other"]},
            "title": {"type": "string", "description": "Short title, e.g. 'Nonna's polenta' or 'The winter of 1956'"},
            "text": {"type": "string", "description": "The full story / recipe / skill in their words"},
            "passphrase": {"type": "string", "description": "Their passphrase as they just said it (only needed if the voice guard is off)"},
        },
        "required": ["person", "kind", "title", "text"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body = {k: kwargs.get(k) for k in ("person", "kind", "title", "text", "passphrase") if kwargs.get(k)}
        payload = _api_request("POST", "/memories/save", body)
        logger.info("save_memory → %s", payload)
        return payload
