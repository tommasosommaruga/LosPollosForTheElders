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
        "Save a story, recipe, skill, tradition, advice or family history. Call ONLY after they said "
        "yes to saving (or clearly asked you to save). BEFORE calling, briefly say you are saving it "
        "in their memories now. Prefer: ask name → listen_patiently(start, person=…) → they tell → "
        "ask to save → speak 'saving…' → save_memory. Pass the same `person`. CRITICAL: put the "
        "COMPLETE story in `text` — every sentence, NEVER summarize. Also pass `tone_note` "
        "(how they told it: warm, laughing, quiet, sad…). Server keeps listen-session speech too. "
        "Never include a passphrase in the text."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "person": {
                "type": "string",
                "description": "Whose memory this is — the name you asked before the story",
            },
            "kind": {"type": "string",
                     "enum": ["story", "recipe", "skill", "tradition", "advice", "family history", "other"]},
            "title": {"type": "string", "description": "Short title, e.g. 'Nonna's polenta' or 'The winter of 1956'"},
            "text": {
                "type": "string",
                "description": (
                    "FULL verbatim story in their words — not a summary. Include every detail they said."
                ),
            },
            "tone_note": {
                "type": "string",
                "description": (
                    "Short cue for how they told it (e.g. 'warm, laughing' or 'quiet, nostalgic'). "
                    "Used only when speaking it back word for word."
                ),
            },
            "passphrase": {"type": "string", "description": "Their passphrase as they just said it (only needed if the voice guard is off)"},
        },
        "required": ["person", "kind", "title", "text"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body = {
            k: kwargs.get(k)
            for k in ("person", "kind", "title", "text", "tone_note", "passphrase")
            if kwargs.get(k)
        }
        payload = _api_request("POST", "/memories/save", body)
        logger.info("save_memory → %s", payload)
        return payload
