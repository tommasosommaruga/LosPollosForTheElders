"""Make Reachy speak on its own, through the conversation app's JSON-RPC (`conversation.say`).

The conversation app serves a WebSocket at /rpc on the app's web port (7860). `say` injects a
message and the model voices it (not word-for-word). Two things can go wrong:
- no conversation yet -> "no active session": `speak` returns False and the caller retries later;
- Reachy is busy (thinking, speaking, or a tool call is waiting for its result): the speech service
  rejects the new reply with "Cannot generate a response while function call outputs are pending"
  and the reminder is lost. So `RpcSpeaker` first WATCHES the app's own status notifications
  (conversation.turn / conversation.activity) and only speaks when Reachy is idle.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("medicine_buddy.speaker")

BUSY_STATES = {"listening", "thinking", "speaking"}


def _rpc_call(url: str, method: str, params: dict[str, Any], timeout: float = 10.0) -> bool:
    """One JSON-RPC call on a fresh connection. True when the app accepted it."""
    from websockets.sync.client import connect

    request_id = 1
    message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    try:
        with connect(url, open_timeout=3, close_timeout=2) as ws:
            ws.send(json.dumps(message))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("%s: no answer within %.0fs", method, timeout)
                    return False
                reply = json.loads(ws.recv(timeout=remaining))
                if reply.get("id") != request_id:      # notifications (turn / transcript ...) are not ours
                    continue
                if "error" in reply:
                    logger.info("%s refused: %s", method, (reply["error"] or {}).get("message"))
                    return False
                return True
    except Exception as exc:  # noqa: BLE001 - not reachable / not ready: the caller retries
        logger.debug("%s failed: %s", method, exc)
        return False


def _say_once(url: str, text: str, timeout: float = 10.0) -> bool:
    return _rpc_call(url, "conversation.say", {"text": text}, timeout)


def _interrupt_once(url: str, timeout: float = 5.0) -> bool:
    return _rpc_call(url, "conversation.interrupt", {}, timeout)


def make_rpc_speaker(url: str = "ws://127.0.0.1:7860/rpc", timeout: float = 10.0) -> Callable[[str], bool]:
    """Plain speaker without busy-checking (see RpcSpeaker for the one the app uses)."""
    return lambda text: _say_once(url, text, timeout)


class RpcSpeaker:
    """`speak(text) -> bool` that only speaks when Reachy is idle.

    A background thread keeps a connection to /rpc open and follows the app's own state:
    `conversation.turn` {state: listening | thinking | speaking | ready} and
    `conversation.activity` {reason: tool_call_received | tool_result_ready | ...}.
    Reachy counts as idle when the last state is "ready" (or nothing was seen yet), no tool call is
    waiting, and the last event is at least `idle_grace` seconds old. A state that stays "busy" for
    `stale_after` seconds with no events at all is treated as stuck and counts as idle.
    If the watcher can't connect, the state is unknown and speaking is allowed (the old behaviour).
    """

    def __init__(
        self,
        url: str = "ws://127.0.0.1:7860/rpc",
        *,
        timeout: float = 10.0,
        idle_grace: float = 4.0,
        stale_after: float = 45.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.idle_grace = idle_grace
        self.stale_after = stale_after
        self._clock = clock
        self._lock = threading.Lock()
        self.state: str | None = None
        self.pending_tools = 0
        self.last_event = clock()
        self.connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ watching
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="medicine-buddy-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _watch(self) -> None:
        from websockets.sync.client import connect

        while not self._stop.is_set():
            try:
                with connect(self.url, open_timeout=3, close_timeout=1) as ws:
                    self.connected = True
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        self._on_message(raw)
            except Exception as exc:  # noqa: BLE001 - not up yet / dropped: reconnect
                logger.debug("conversation watcher: %s", exc)
            finally:
                self.connected = False
            self._stop.wait(2.0)

    def _on_message(self, raw: Any) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(msg, dict):          # null, [], 5 ...: ignore, never drop the connection over it
            return
        method, params = msg.get("method"), msg.get("params")
        if method not in ("conversation.turn", "conversation.activity") or not isinstance(params, dict):
            return
        with self._lock:
            self.last_event = self._clock()
            if method == "conversation.turn":
                self.state = params.get("state")
                if self.state == "ready":
                    self.pending_tools = 0              # the reply is finished: nothing can still be waiting
            else:
                reason = params.get("reason")
                if reason == "tool_call_received":
                    self.pending_tools += 1
                elif reason == "tool_result_ready":
                    self.pending_tools = max(0, self.pending_tools - 1)

    # ------------------------------------------------------------------ speaking
    def is_idle(self) -> bool:
        with self._lock:
            quiet_for = self._clock() - self.last_event
            if quiet_for >= self.stale_after:
                return True                            # nothing heard for a long time: don't wait forever
            if self.pending_tools > 0 or self.state in BUSY_STATES:
                return False
            return quiet_for >= self.idle_grace

    def __call__(self, text: str) -> bool:
        if not self.is_idle():
            logger.debug("Reachy is busy (%s, %d tool call(s)); not speaking now", self.state, self.pending_tools)
            return False
        if not _say_once(self.url, text, self.timeout):
            return False
        with self._lock:                               # busy until the app's own events say otherwise
            self.state, self.last_event = "thinking", self._clock()
        return True

    def speak_urgent(self, text: str) -> bool:
        """Speak NOW, cutting in on Reachy and the person (medicine reminders).

        If Reachy is talking or thinking, its reply is stopped first with the app's own
        `conversation.interrupt` (the same as when the person talks over it); the app then
        queues our line after the cancelled reply. The only thing we wait for is a tool call
        that is still running: cutting in then makes the speech service reject the reply."""
        with self._lock:
            quiet_for = self._clock() - self.last_event
            stale = quiet_for >= self.stale_after
            if self.pending_tools > 0 and not stale:
                logger.debug("Tool call running; urgent line waits for its result")
                return False
            busy = self.state in BUSY_STATES and not stale
        if busy:
            _interrupt_once(self.url)          # best effort: if it fails, say() still queues the line
        if not _say_once(self.url, text, self.timeout):
            return False
        with self._lock:
            self.state, self.last_event = "thinking", self._clock()
        return True

    def speak(self, text: str) -> bool:
        """Alias for ``self(text)`` — speak only when Reachy is idle."""
        return self(text)

    def speak_progress(self, text: str) -> bool:
        """Best-effort line while waiting. Never calls say() if a tool is still pending.

        HuggingFace realtime rejects new replies with
        "Cannot generate a response while function call outputs are pending" — so we
        must NOT bypass the pending_tools gate (unlike an earlier mistaken version).
        """
        line = " ".join(str(text or "").split())
        if not line:
            return False
        with self._lock:
            if self.pending_tools > 0:
                logger.debug("speak_progress skipped: %d tool call(s) still pending", self.pending_tools)
                return False
        return self.speak(line)

    def speak_when_idle(
        self,
        text: str,
        *,
        attempts: int = 40,
        delay_s: float = 1.5,
    ) -> bool:
        """Retry speak() until Reachy is idle (for background arm-done announcements)."""
        line = " ".join(str(text or "").split())
        if not line:
            return False
        for _ in range(max(1, int(attempts))):
            if self.speak(line):
                return True
            time.sleep(max(0.2, float(delay_s)))
        logger.warning("speak_when_idle gave up after %s tries: %s", attempts, line[:80])
        return False

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"watching": self.connected, "state": self.state, "pending_tools": self.pending_tools,
                    "idle": None, "quiet_for_s": round(self._clock() - self.last_event, 1)}
