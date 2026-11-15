"""Start/stop a story listen session after the person's name is known.

Does not change microphone VAD (that muted TTS before). Marks whose story is
being told so save_memory can keep the full spoken transcript.
"""

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
    needs_response: ClassVar[bool] = False
    description = (
        "Call BEFORE they tell a long story, recipe, or family memory — and ONLY after you "
        "know whose memory it is (ask their name first if needed). action=start with `person` "
        "opens a story session so their spoken words are kept for save_memory. Then stay mostly "
        "quiet and let them talk. action=stop when they finish or after save_memory. "
        "Do not use for short chat or medicine questions."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "stop"],
                "description": "start = ready to listen; stop = story session over",
            },
            "person": {
                "type": "string",
                "description": "Whose story this is (required on start when known)",
            },
        },
        "required": ["action"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        action = str(kwargs.get("action") or "").strip().lower()
        person = kwargs.get("person")
        body: dict[str, Any] = {"action": action}
        if person:
            body["person"] = person
        payload = _api_request("POST", "/voice/listen_mode", body)
        logger.info("listen_patiently → %s", payload)
        return payload
