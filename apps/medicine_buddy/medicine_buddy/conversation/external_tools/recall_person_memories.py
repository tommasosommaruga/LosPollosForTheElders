"""External tool: look up a person's family memories (and medicines) by their name."""

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


class RecallPersonMemories(Tool):
    name = "recall_person_memories"
    description = (
        "Find what a family member saved: stories, recipes, skills, traditions, advice, family history. "
        "Use when someone asks about another person by name, e.g. 'tell me a story grandpa Mario told you' "
        "or 'what was Nonna Rosa's polenta recipe'. Pass the person's name, optionally a kind and topic "
        "words. Then speak the returned `text` WORD FOR WORD — exactly, verbatim; do not rephrase. "
        "Use `tone_note` only for delivery. NEVER summarize. NEVER invent words not in `text`."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "The person's name, e.g. 'Mario'"},
            "kind": {"type": "string",
                     "enum": ["story", "recipe", "skill", "tradition", "advice", "family history", "other"]},
            "topic": {"type": "string", "description": "Optional words to look for, e.g. 'polenta' or 'war'"},
        },
        "required": ["person"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body = {k: kwargs.get(k) for k in ("person", "kind", "topic") if kwargs.get(k)}
        payload = _api_request("POST", "/memories/recall", body)
        logger.info("recall_person_memories → %s results", payload.get("total_matching"))
        return payload
