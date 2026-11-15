"""Deprecated no-op: patient VAD silence stretching was removed.

Kept so an old profile or cached tool list cannot break; turn-taking is stock Reachy Mini.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies

logger = logging.getLogger(__name__)


class ListenPatiently(Tool):
    name = "listen_patiently"
    needs_response: ClassVar[bool] = False
    description = (
        "Deprecated. Do not call. Speech uses normal Reachy Mini turn-taking. "
        "For stories, just listen and ask whether to save_memory when they finish."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "stop"],
                "description": "Ignored — patient listening is disabled",
            },
        },
        "required": ["action"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps, kwargs
        logger.info("listen_patiently called but disabled (stock Reachy turn-taking)")
        return {
            "status": "ok",
            "enabled": False,
            "disabled": True,
            "vad_applied": False,
            "speak_hint": (
                "Patient listening is off. Use normal conversation; "
                "ask about save_memory when they finish a story."
            ),
        }
