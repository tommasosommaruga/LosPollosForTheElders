"""Non-clinical motor and cognitive activity suggestions.

These are gentle encouragements — not physiotherapy, not medical advice. The person
can decline without friction. Optional idle nudges reuse the check-in speaker path.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("medicine_buddy.activities")

# kind: motor | cognitive | social
ACTIVITY_CATALOG: list[dict[str, str]] = [
    {
        "id": "stand_stretch",
        "kind": "motor",
        "title": "Stand and stretch",
        "prompt": (
            "If they feel steady and a caregiver is nearby when needed, invite them to stand "
            "up slowly from the chair and stretch their arms for about ten seconds, then sit. "
            "Say this is optional and they can skip it."
        ),
    },
    {
        "id": "seated_reach",
        "kind": "motor",
        "title": "Seated reach",
        "prompt": (
            "Invite a short seated reach: stretch one arm toward a safe object on the table, "
            "then the other arm. Keep it light and optional — not exercise therapy."
        ),
    },
    {
        "id": "short_walk",
        "kind": "motor",
        "title": "Short walk",
        "prompt": (
            "If they use a walker or cane, suggest a short walk in the room with their usual "
            "aid when they feel ready. Remind them they can say no. Never offer to lift or "
            "support them."
        ),
    },
    {
        "id": "memory_question",
        "kind": "cognitive",
        "title": "Gentle memory chat",
        "prompt": (
            "Ask one warm open question about a favourite place, song, or recipe — then offer "
            "to save it as a family memory if they wish. Do not quiz or score them."
        ),
    },
    {
        "id": "name_three",
        "kind": "cognitive",
        "title": "Name three things",
        "prompt": (
            "Play a light game: ask them to name three things they can see in the room. Keep "
            "it playful, never a test, and thank them for playing."
        ),
    },
    {
        "id": "chat_companion",
        "kind": "social",
        "title": "Two minutes of chat",
        "prompt": (
            "Offer to simply chat for a couple of minutes about whatever they like — weather, "
            "family, or a saved memory. You are a companion for a short conversation."
        ),
    },
    {
        "id": "call_family",
        "kind": "social",
        "title": "Call family",
        "prompt": (
            "Ask whether they would like to call family. If yes, use start_family_call. "
            "Do not invent a video link."
        ),
    },
]


def _by_id(activity_id: str | None) -> dict[str, str] | None:
    if not activity_id:
        return None
    key = str(activity_id).strip().lower()
    for item in ACTIVITY_CATALOG:
        if item["id"] == key:
            return item
    return None


def _by_kind(kind: str | None) -> list[dict[str, str]]:
    if not kind:
        return list(ACTIVITY_CATALOG)
    want = str(kind).strip().lower()
    return [a for a in ACTIVITY_CATALOG if a["kind"] == want]


class ActivitiesMixin:
    """Mixed into MedicineBuddyService."""

    _activity_rotate: int

    def _init_activities(self) -> None:
        self._activity_rotate = 0

    def list_activities(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        kind = body.get("kind")
        items = _by_kind(str(kind) if kind else None)
        return {
            "status": "ok",
            "activities": [
                {"id": a["id"], "kind": a["kind"], "title": a["title"]} for a in items
            ],
            "speak_hint": (
                "These are optional light activities — not medical therapy. "
                "Ask which they fancy, or call suggest_activity."
            ),
        }

    def suggest_activity(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Pick one activity (by id/kind or rotate) and return a speak_hint for Reachy."""
        body = body or {}
        if not hasattr(self, "_activity_rotate"):
            self._init_activities()
        chosen = _by_id(body.get("activity_id") or body.get("id"))
        if chosen is None:
            pool = _by_kind(str(body["kind"]) if body.get("kind") else None)
            if not pool:
                return {
                    "error": "No activities of that kind",
                    "speak_hint": "I only have light stretch, memory chat, or social ideas.",
                }
            chosen = pool[self._activity_rotate % len(pool)]
            self._activity_rotate += 1

        response = str(body.get("response") or "").strip().lower()
        logged = None
        if response in {"accepted", "declined", "yes", "no", "skip"}:
            accepted = response in {"accepted", "yes"}
            logged = "accepted" if accepted else "declined"
            try:
                self.db.insert_activity_event(
                    at_ts=time.time(),
                    via="activity",
                    method="POST",
                    path=f"/activities/{chosen['id']}",
                    status=200,
                    ms=0.0,
                    result=logged,
                    speak_hint=chosen["title"],
                )
            except Exception:  # noqa: BLE001
                logger.exception("Could not log activity response")

        return {
            "status": "ok",
            "activity": {
                "id": chosen["id"],
                "kind": chosen["kind"],
                "title": chosen["title"],
            },
            "logged": logged,
            "speak_hint": (
                f"Optional activity — {chosen['title']}. {chosen['prompt']} "
                "If they decline, accept warmly and move on. Never pressure them."
            ),
        }
