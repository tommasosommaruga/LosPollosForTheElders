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
from urllib.parse import urlparse

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
from medicine_buddy.calendar_service import CalendarSyncLoop  # noqa: E402
from medicine_buddy.config import load_app_config  # noqa: E402
from medicine_buddy.db import default_db_path  # noqa: E402
from medicine_buddy.checkins import CheckInLoop  # noqa: E402
from medicine_buddy.reminders import ReminderLoop  # noqa: E402
from medicine_buddy.speaker import RpcSpeaker  # noqa: E402
from medicine_buddy.wake import start_wake_greeting  # noqa: E402
from medicine_buddy.wake_robot import make_waker  # noqa: E402
from medicine_buddy.web_ui import attach_web_ui  # noqa: E402

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
        if service is not None and final:
            service.record_transcript(role, text)   # live Conversation page (memory only)
        if service is not None and role == "user" and final and text and text.strip():
            # passphrase matching hashes words: keep it off the audio event loop
            threading.Thread(target=service.voice_heard, args=({"text": text.strip()},), daemon=True).start()

    ConversationHandler._emit_transcript = emit_and_guard
    ConversationHandler._medicine_buddy_hooked = True


class MedicineBuddy(ReachyMiniApp):
    """Reachy Mini app: medicine reminders per person, family memories, passphrase access control.

    Runs Pollen's conversation app inside (same calls as its ReachyMiniConversationApp class)."""

    # Reachy Mini Control opens this page: the Medicine Buddy dashboard (sign-in, family settings,
    # debug). Pollen's voice & language settings stay at "/" on the same server.
    custom_app_url = "http://0.0.0.0:7860/medicine"
    dont_start_webserver = False
    # Override via REACHY_MINI_MEDIA_BACKEND or --media-backend (e.g. no_media on a Mac
    # where GStreamer/WebRTC is broken). None = SDK "default".
    request_media_backend = os.environ.get("REACHY_MINI_MEDIA_BACKEND") or None

    def __init__(self, *args, **kwargs) -> None:
        conversation.start_import_warmup()  # preload heavy imports while the robot connects
        super().__init__(*args, **kwargs)
        if self.settings_app is not None:
            # Registered now (the web server starts before run()); shows "starting..." until ready.
            attach_web_ui(self.settings_app, lambda: _SERVICE)

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
        # dry_run stays true until arm URL is live; ARM_URL enables structured HTTP arm.v1 signals.
        service, server, _ = start_server(
            host, port, db_path, load_app_config(dry_run=True), voice_guard=guard,
        )
        _SERVICE = service
        _install_transcript_hook()
        # Never patch HuggingFace ServerVad — stock Reachy Mini speech-to-speech only.
        # (Earlier patient-listen session.update muted TTS; that path is fully removed.)
        reminders = None
        checkins = None
        calendar_sync = None
        speaker = None
        if os.environ.get("MEDICINE_BUDDY_CALENDAR_SYNC", "1").lower() not in {"0", "false", "no", "off"}:
            calendar_sync = CalendarSyncLoop(service)
            service.calendar_sync_loop = calendar_sync
            calendar_sync.start()
        if os.environ.get("MEDICINE_BUDDY_REMINDERS", "1").lower() not in {"0", "false", "no", "off"}:
            ui_port = urlparse(self.custom_app_url).port or 7860
            speaker = RpcSpeaker(f"ws://127.0.0.1:{ui_port}/rpc")   # waits until Reachy is idle
            speaker.start()
            # Default OFF: wake_up/enable_motors from the reminder thread races the conversation
            # app's daemon WebSocket and can raise ConnectionError ("Lost connection with the
            # server") and kill the whole app. Opt in with MEDICINE_BUDDY_WAKE_ROBOT=1 only after
            # verifying reminders on a quiet robot.
            waker = None
            if os.environ.get("MEDICINE_BUDDY_WAKE_ROBOT", "0").lower() in {"1", "true", "yes", "on"}:
                waker = make_waker(reachy_mini)
            reminders = ReminderLoop(service, speaker, wake=waker)
            service.reminder_loop = reminders   # shown on the debug page
            reminders.start()
        if os.environ.get("MEDICINE_BUDDY_CHECKINS", "1").lower() not in {"0", "false", "no", "off"}:
            if speaker is None:
                ui_port = urlparse(self.custom_app_url).port or 7860
                speaker = RpcSpeaker(f"ws://127.0.0.1:{ui_port}/rpc")
                speaker.start()
            checkins = CheckInLoop(service, speaker)
            service.checkin_loop = checkins
            checkins.start()
        # Wake line + "what's due in the next 30 min" once speech is ready (same speaker as reminders).
        wake_on = os.environ.get("MEDICINE_BUDDY_WAKE_GREETING", "1").lower() not in {
            "0", "false", "no", "off",
        }
        if wake_on:
            if speaker is None:
                ui_port = urlparse(self.custom_app_url).port or 7860
                speaker = RpcSpeaker(f"ws://127.0.0.1:{ui_port}/rpc")
                speaker.start()
            start_wake_greeting(service, speaker, stop_event)
        logger.info("Medicine Buddy API on http://%s:%d  (status page /, family settings /family)", host, port)
        logger.info("In-app pages (open by default): http://<this-machine>:7860/medicine")
        logger.info(
            "Database: %s  |  require passphrases: %s  |  voice guard: %s  |  wake greeting: %s  |  profile: %s (locked)",
            db_path, service.require_passphrases(), service.guard.enabled, wake_on, PROFILE,
        )
        try:
            self._run_conversation(reachy_mini, stop_event)
        finally:
            _SERVICE = None
            if calendar_sync is not None:
                calendar_sync.stop()
            if checkins is not None:
                checkins.stop()
            if reminders is not None:
                reminders.stop()
            if speaker is not None:
                speaker.stop()
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
    media_backend = _pop_option("--media-backend") or os.environ.get("REACHY_MINI_MEDIA_BACKEND")
    if media_backend:
        MedicineBuddy.request_media_backend = media_backend
    elif robot_host and sys.platform == "darwin":
        # Laptop → wireless robot: Mac GStreamer/WebRTC often crashes (_gst_structure_is_writable).
        # no_media gets the app running; reinstall on the robot for full robot mic/speaker AV.
        MedicineBuddy.request_media_backend = "no_media"
        logging.getLogger("medicine_buddy.app").warning(
            "macOS + --host: using media backend 'no_media' (GStreamer WebRTC is broken on many Macs). "
            "Set REACHY_MINI_MEDIA_BACKEND=default to try the normal path, or run the app on the robot."
        )
    app = MedicineBuddy()
    try:
        # From a laptop, always pass the robot host and force network mode — a leftover local
        # reachy-mini on :8000 must not hijack the connection (that caused localhost WebRTC).
        run_kwargs: dict = {}
        if robot_host:
            run_kwargs["host"] = robot_host
            app.daemon_on_localhost = False
        app.wrapped_run(**run_kwargs)
    except KeyboardInterrupt:
        app.stop()
