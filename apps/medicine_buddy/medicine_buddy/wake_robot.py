"""Wake the robot up by itself when a medicine reminder is due.

If Reachy is in its sleep pose (head down, motors off) it is woken with the SDK's own wake-up
movement before it speaks, so the person sees and hears it come alive at the scheduled time.
An awake robot is left alone. Never raises: a failed wake-up must not stop the reminder.

Thread safety: reminders run on a background thread while the conversation app owns the same
ReachyMini daemon WebSocket on the main thread. Concurrent ``enable_motors`` / ``wake_up`` can
drop that socket (``ConnectionError: Lost connection with the server``) and exit the app.
``MEDICINE_BUDDY_WAKE_ROBOT`` therefore defaults to off in ``main.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("medicine_buddy.wake_robot")

MIN_GAP_S = 30.0  # don't re-check the pose (or re-run the movement) more often than this


def make_waker(robot: Any, *, min_gap: float = MIN_GAP_S) -> Callable[[], bool]:
    """Returns wake() -> True when the robot was asleep and has just been woken."""
    lock = threading.Lock()
    last = [0.0]

    def wake() -> bool:
        with lock:
            now = time.monotonic()
            if last[0] and now - last[0] < min_gap:
                return False
            last[0] = now
            try:
                from reachy_mini_conversation_app import app_lifecycle
                return bool(app_lifecycle.wake_up_if_sleeping(robot, logger))
            except Exception:  # noqa: BLE001
                logger.exception("Could not check/wake the robot")
                return False

    return wake
