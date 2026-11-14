"""Medicine Buddy as a Reachy Mini app.

One process does everything:
- Pollen's conversation app (listening, thinking, speaking) with the medicine_caregiver
  profile LOCKED and our voice tools (medicines, people, family memories, safe web search),
- the Medicine Buddy API + status page + Family settings page on 127.0.0.1:8787,
- the voice guard, fed directly with the person's speech transcripts.

How it runs
- On the robot: installed into the robot's app environment it appears in Reachy Mini
  Control -> Applications; the daemon starts it as `python -u -m medicine_buddy.main`.
- From a laptop: `python -m medicine_buddy.main --host <robot-ip>` (or REACHY_HOST in .env).

Configuration (environment or a .env file, see .env.example at the repo root):
  REACHY_HOST              robot IP/hostname when running from a laptop
  MEDICINE_BUDDY_HOST      API bind address (default 127.0.0.1 - keep it local)
  MEDICINE_BUDDY_PORT      API port (default 8787)
  MEDICINE_BUDDY_DB        database file (default: see medicine_buddy.db.default_db_path)
  MEDICINE_BUDDY_VOICE_GUARD  1/0 (default 1)
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
CONVERSATION_DIR = PACKAGE_DIR / "conversation"
PROFILE = "medicine_caregiver"
logger = logging.getLogger("medicine_buddy.app")


def _load_dotenv() -> None:
    """.env in the current folder, then at the repo root / project folder (never overrides)."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv comes with the conversation app; optional otherwise
        return
    for candidate in (Path.cwd() / ".env", PACKAGE_DIR.parent / ".env", PACKAGE_DIR.parents[2] / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def _configure_environment() -> None:
    """Must run BEFORE the conversation app is imported: it reads these at import time."""
    _load_dotenv()
    port = os.environ.setdefault("MEDICINE_BUDDY_PORT", "8787")
    os.environ.setdefault("MEDICINE_BUDDY_URL", f"http://127.0.0.1:{port}")
    os.environ["REACHY_MINI_EXTERNAL_TOOLS_DIRECTORY"] = str(CONVERSATION_DIR / "external_tools")
    os.environ["REACHY_MINI_EXTERNAL_PROFILES_DIRECTORY"] = str(CONVERSATION_DIR / "external_profiles")
    os.environ["REACHY_MINI_CUSTOM_PROFILE"] = PROFILE
    os.environ["AUTOLOAD_EXTERNAL_TOOLS"] = "1"
    os.environ.setdefault("GST_DEBUG", "0")  # hide harmless WebRTC log spam


_configure_environment()

# Lock the profile before any other conversation-app module copies LOCKED_PROFILE.
import reachy_mini_conversation_app.config as _conv_config  # noqa: E402

_conv_config.LOCKED_PROFILE = PROFILE
_conv_config.config.REACHY_MINI_CUSTOM_PROFILE = PROFILE

import asyncio  # noqa: E402

from reachy_mini import ReachyMini, ReachyMiniApp  # noqa: E402
from reachy_mini_conversation_app import main as conversation  # noqa: E402
from reachy_mini_conversation_app.conversation_handler import ConversationHandler  # noqa: E402

from medicine_buddy.api import MedicineBuddyService, start_server  # noqa: E402
from medicine_buddy.config import AppConfig  # noqa: E402
from medicine_buddy.db import default_db_path  # noqa: E402

_SERVICE: MedicineBuddyService | None = None


def _install_transcript_hook() -> None:
    """Send the PERSON's final speech transcripts to the voice guard (never the assistant's).
    In-process: no AI tool can reach this path."""
    if getattr(ConversationHandler, "_medicine_buddy_hooked", False):
        return
    original = ConversationHandler._emit_transcript

    def emit_and_guard(self, role: str, text: str, final: bool = True) -> None:
        original(self, role, text, final)
        service = _SERVICE
        if service is not None and role == "user" and final and text and text.strip():
            # passphrase matching hashes words: keep it off the audio event loop
            threading.Thread(target=service.voice_heard, args=({"text": text.strip()},), daemon=True).start()

    ConversationHandler._emit_transcript = emit_and_guard
    ConversationHandler._medicine_buddy_hooked = True


class MedicineBuddy(ReachyMiniApp):
    """Reachy Mini app: medicine reminders per person, family memories, passphrase access control.

    Runs Pollen's conversation app inside (same calls as its ReachyMiniConversationApp class)."""

    # The conversation app's own settings UI (voice, language...); opened by Reachy Mini Control.
    custom_app_url = "http://0.0.0.0:7860/"
    dont_start_webserver = False

    def __init__(self, *args, **kwargs) -> None:
        conversation.start_import_warmup()  # preload heavy imports while the robot connects
        super().__init__(*args, **kwargs)

    def _run_conversation(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        asyncio.set_event_loop(asyncio.new_event_loop())
        args, _ = conversation.parse_args()
        instance_path = str(conversation.resolve_instance_path(self._get_instance_path().parent))
        conversation.run(args, robot=reachy_mini, app_stop_event=stop_event,
                         settings_app=self.settings_app, instance_path=instance_path)

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        global _SERVICE
        host = os.environ.get("MEDICINE_BUDDY_HOST", "127.0.0.1")
        port = int(os.environ.get("MEDICINE_BUDDY_PORT", "8787"))
        guard = os.environ.get("MEDICINE_BUDDY_VOICE_GUARD", "1").lower() not in {"0", "false", "no", "off"}
        db_path = default_db_path()
        service, server, _ = start_server(host, port, db_path, AppConfig(dry_run=True), voice_guard=guard)
        _SERVICE = service
        _install_transcript_hook()
        logger.info("Medicine Buddy API on http://%s:%d  (status page /, family settings /family)", host, port)
        logger.info("Database: %s  |  voice guard: %s  |  profile: %s (locked)", db_path, guard, PROFILE)
        try:
            self._run_conversation(reachy_mini, stop_event)
        finally:
            _SERVICE = None
            server.shutdown()
            server.server_close()
            service.close()


def _pop_option(name: str) -> str | None:
    if name in sys.argv:
        i = sys.argv.index(name)
        value = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        del sys.argv[i : i + 2]
        return value
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    robot_host = _pop_option("--host") or os.environ.get("REACHY_HOST")
    app = MedicineBuddy()
    try:
        # On the robot the daemon is local and no host is needed; from a laptop pass the robot's IP.
        app.wrapped_run(**({"host": robot_host} if robot_host and not app.daemon_on_localhost else {}))
    except KeyboardInterrupt:
        app.stop()
