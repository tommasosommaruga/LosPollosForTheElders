"""Cross-platform launcher: Pollen's conversation app + Medicine Buddy tools + voice guard.

Run it with the conversation app's Python (Windows / macOS / Linux):

    <conversation-app-venv>/python integrations/reachy_conversation/run_conversation.py --host 172.20.10.5

What it adds on top of `reachy-mini-conversation-app`:
- loads this repo's medicine tools + medicine_caregiver profile (no .env needed),
- --host lets you target the robot by IP (the app itself has no host option),
- forwards the PERSON's final speech transcripts to Medicine Buddy (POST /voice/heard)
  so the server's voice guard can check that medicine changes were really asked for out
  loud. The LLM has no tool that can reach that endpoint, so text from web pages, the
  camera or memory can't fake it. Start the server with `medicine-buddy serve --voice-guard`.

Other flags (e.g. --ui, --debug) are passed through to the conversation app.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _forward_heard(url: str, text: str) -> None:
    def send() -> None:
        try:
            req = urllib.request.Request(
                f"{url}/voice/heard",
                data=json.dumps({"text": text}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3).close()
        except Exception as exc:  # noqa: BLE001 - never break the conversation
            print(f"[voice-guard] could not forward transcript: {exc}", file=sys.stderr)

    threading.Thread(target=send, daemon=True).start()


def main() -> None:
    args = sys.argv[1:]
    host = os.environ.get("REACHY_HOST", "reachy-mini.local")
    if "--host" in args:
        i = args.index("--host")
        host = args[i + 1]
        del args[i : i + 2]

    os.environ.setdefault("MEDICINE_BUDDY_URL", "http://127.0.0.1:8787")
    os.environ["REACHY_MINI_EXTERNAL_TOOLS_DIRECTORY"] = str(HERE / "external_tools")
    os.environ["REACHY_MINI_EXTERNAL_PROFILES_DIRECTORY"] = str(HERE / "external_profiles")
    os.environ["REACHY_MINI_CUSTOM_PROFILE"] = "medicine_caregiver"
    os.environ["AUTOLOAD_EXTERNAL_TOOLS"] = "1"
    os.environ.setdefault("GST_DEBUG", "0")  # hide WebRTC log spam
    buddy_url = os.environ["MEDICINE_BUDDY_URL"].rstrip("/")

    import reachy_mini_conversation_app.main as app_main
    from reachy_mini_conversation_app.conversation_handler import ConversationHandler

    # 1) Robot by IP
    real_reachy = app_main.ReachyMini

    def reachy_at_host(*a, **kw):
        kw.setdefault("host", host)
        kw.setdefault("connection_mode", "network")
        return real_reachy(*a, **kw)

    app_main.ReachyMini = reachy_at_host

    # 2) Forward the person's final transcripts (never the assistant's) to the voice guard
    original_emit = ConversationHandler._emit_transcript

    def emit_and_forward(self, role: str, text: str, final: bool = True) -> None:
        original_emit(self, role, text, final)
        if role == "user" and final and text and text.strip():
            _forward_heard(buddy_url, text.strip())

    ConversationHandler._emit_transcript = emit_and_forward

    print(f"Robot: {host} | Medicine Buddy: {buddy_url} | profile: medicine_caregiver | "
          "voice transcripts -> /voice/heard")
    sys.argv = [sys.argv[0], *args]
    app_main.main()


if __name__ == "__main__":
    main()
