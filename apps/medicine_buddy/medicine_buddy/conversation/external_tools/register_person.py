"""External tool: register a person (family member or caregiver) with a spoken passphrase."""

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


class RegisterPerson(Tool):
    name = "register_person"
    description = (
        "Register someone with a passphrase when they only want an account (no medicine yet). "
        "For medicine setup prefer schedule_medicine with new_passphrase after they say 3–4 words twice. "
        "With the voice guard, only real speech counts — tool args alone are not enough. "
        "role 'person' needs nothing else; role 'caregiver' needs an existing caregiver's passphrase "
        "if one exists. Never invent or repeat a passphrase aloud. To CHANGE one, use Family settings."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The person's name, e.g. 'Mario'"},
            "role": {"type": "string", "enum": ["person", "caregiver"]},
            "passphrase": {"type": "string", "description": "New passphrase, first time said"},
            "passphrase_repeat": {"type": "string", "description": "New passphrase, second time said"},
            "caregiver_passphrase": {"type": "string", "description": "Caregiver's passphrase (not needed for the first caregiver)"},
        },
        "required": ["name", "role", "passphrase", "passphrase_repeat"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        del deps
        body = {
            "name": kwargs.get("name"),
            "role": kwargs.get("role", "person"),
            "passphrase": kwargs.get("passphrase"),
            "passphrase_repeat": kwargs.get("passphrase_repeat"),
        }
        if kwargs.get("caregiver_passphrase"):  # only used when the voice guard is off
            body["caregiver_passphrase"] = kwargs["caregiver_passphrase"]
        payload = _api_request("POST", "/people/register", body)
        logger.info("register_person → %s", {k: v for k, v in payload.items() if k != "person"})
        return payload
