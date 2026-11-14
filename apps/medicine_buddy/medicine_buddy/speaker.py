"""Make Reachy speak on its own, through the conversation app's JSON-RPC (`conversation.say`).

The conversation app serves a WebSocket at /rpc on the app's web port (7860). `say` injects a
message and the model voices it (not word-for-word). It fails with "no active session" until the
conversation is connected, in which case `speak` returns False and the caller retries later.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable

logger = logging.getLogger("medicine_buddy.speaker")


def make_rpc_speaker(url: str = "ws://127.0.0.1:7860/rpc", timeout: float = 10.0) -> Callable[[str], bool]:
    request_id = 1

    def speak(text: str) -> bool:
        from websockets.sync.client import connect

        message = {"jsonrpc": "2.0", "id": request_id, "method": "conversation.say", "params": {"text": text}}
        try:
            with connect(url, open_timeout=3, close_timeout=2) as ws:
                ws.send(json.dumps(message))
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning("conversation.say: no answer within %.0fs", timeout)
                        return False
                    reply = json.loads(ws.recv(timeout=remaining))
                    if reply.get("id") != request_id:      # notifications (turn / transcript ...) are not ours
                        continue
                    if "error" in reply:
                        logger.info("conversation.say refused: %s", (reply["error"] or {}).get("message"))
                        return False
                    return True
        except Exception as exc:  # noqa: BLE001 - not reachable / not ready: the caller retries
            logger.debug("conversation.say failed: %s", exc)
            return False

    return speak
