"""HTTP API so reachy_mini_conversation tools can drive Medicine Buddy remotely."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.dry_run import FakeAdherence, FakeArm, FakeVision
from medicine_buddy.orchestrator import DoseStateMachine
from medicine_buddy.scheduler import find_due_doses
from medicine_buddy.silent_buddy import SilentBuddy
from medicine_buddy.voice_setup import parse_time_hhmm, parse_weekdays


class MedicineBuddyService:
    def __init__(self, db_path: Path, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig(dry_run=True)
        self.db = Database(db_path)
        self._lock = threading.Lock()

    def close(self) -> None:
        self.db.close()

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "dry_run": self.config.dry_run,
            "service": "medicine-buddy",
            "version": "0.1.0",
        }

    def list_medicines(self) -> dict[str, Any]:
        meds = self.db.list_medicines()
        schedules = self.db.list_active_schedules()
        by_med = {s.medicine_id: [] for s in schedules}
        for schedule in schedules:
            by_med.setdefault(schedule.medicine_id, []).append(
                {
                    "id": schedule.id,
                    "weekdays": schedule.weekdays,
                    "time_hhmm": schedule.time_hhmm,
                }
            )
        return {
            "medicines": [
                {
                    "id": m.id,
                    "name": m.name,
                    "schedules": by_med.get(m.id, []),
                }
                for m in meds
            ]
        }

    def add_schedule(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name", "")).strip()
        weekdays_raw = str(body.get("weekdays", "daily"))
        time_raw = str(body.get("time", body.get("time_hhmm", "")))
        if not name or not time_raw:
            return {"error": "name and time are required"}
        try:
            weekdays = parse_weekdays(weekdays_raw)
            time_hhmm = parse_time_hhmm(time_raw)
        except ValueError as exc:
            return {"error": str(exc)}
        with self._lock:
            medicine = self.db.add_medicine(name)
            schedule = self.db.add_schedule(medicine.id, weekdays, time_hhmm)
        return {
            "status": "ok",
            "medicine": {"id": medicine.id, "name": medicine.name},
            "schedule": {
                "id": schedule.id,
                "weekdays": schedule.weekdays,
                "time_hhmm": schedule.time_hhmm,
            },
            "speak_hint": (
                f"Saved {medicine.name} for {schedule.weekdays} at {schedule.time_hhmm}."
            ),
        }

    def due(self) -> dict[str, Any]:
        items = find_due_doses(self.db)
        return {
            "due": [
                {
                    "medicine_id": d.medicine_id,
                    "medicine_name": d.medicine_name,
                    "schedule_id": d.schedule_id,
                    "time_hhmm": d.time_hhmm,
                }
                for d in items
            ],
            "speak_hint": (
                "No medicines are due right now."
                if not items
                else "Due now: "
                + ", ".join(d.medicine_name for d in items)
                + ". Ask the person if you should bring it, then call dispense_medicine."
            ),
        }

    def _resolve_medicine(self, body: dict[str, Any]):
        medicine_id = body.get("medicine_id")
        medicine_name = body.get("medicine_name") or body.get("name")
        if medicine_id is not None:
            return self.db.get_medicine(int(medicine_id))
        if medicine_name:
            return self.db.get_medicine_by_name(str(medicine_name))
        return None

    def run_dose(self, body: dict[str, Any]) -> dict[str, Any]:
        """Dispense to bowl. TTS stays on conversation app; return speak_hint only."""
        confirmed = bool(body.get("confirmed", False))
        # Default pause after place so Reachy conversation asks "did you take it?"
        stop_after_placed = bool(body.get("stop_after_placed", True))

        with self._lock:
            medicine = self._resolve_medicine(body)
            if medicine is None and not (
                body.get("medicine_id") or body.get("medicine_name") or body.get("name")
            ):
                return {"error": "medicine_id or medicine_name required"}
            if medicine is None:
                return {
                    "error": (
                        f"Unknown medicine: "
                        f"{body.get('medicine_name') or body.get('name') or body.get('medicine_id')}"
                    )
                }

            if not confirmed:
                return {
                    "status": "needs_confirm",
                    "medicine": {"id": medicine.id, "name": medicine.name},
                    "speak_hint": (
                        f"Time for your {medicine.name} medicine — should I bring it? "
                        "If they say yes, call dispense_medicine again with confirmed=true."
                    ),
                }

            buddy = SilentBuddy(confirm=True, adherence_taken=None)
            vision = FakeVision(self.config)
            arm = FakeArm()
            adherence = FakeAdherence(buddy, auto_taken=None)
            machine = DoseStateMachine(
                self.db, buddy, vision, arm, adherence, self.config
            )
            final = machine.run_dose(
                medicine.id, stop_after_placed=stop_after_placed
            )
            events = [
                {
                    "state": e.state.value,
                    "slot_id": e.slot_id,
                    "detail": e.detail,
                }
                for e in self.db.list_dose_events(medicine.id)[-12:]
            ]

        speak_hint = buddy.messages[-1] if buddy.messages else final.value
        if final.value == "Placed":
            speak_hint = (
                f"Your {medicine.name} is in the bowl. Please take it. "
                "Then ask if they took it and call mark_dose_taken."
            )

        return {
            "status": "ok",
            "final_state": final.value,
            "medicine": {"id": medicine.id, "name": medicine.name},
            "messages": buddy.messages,
            "events": events,
            "speak_hint": speak_hint,
        }

    def mark_adherence(self, body: dict[str, Any]) -> dict[str, Any]:
        taken = body.get("taken")
        if taken is None:
            return {"error": "taken (boolean) is required"}
        taken_bool = bool(taken)
        with self._lock:
            medicine = self._resolve_medicine(body)
            if medicine is None:
                return {"error": "medicine_id or medicine_name required / unknown"}
            buddy = SilentBuddy(confirm=True)
            vision = FakeVision(self.config)
            arm = FakeArm()
            adherence = FakeAdherence(buddy, auto_taken=taken_bool)
            machine = DoseStateMachine(
                self.db, buddy, vision, arm, adherence, self.config
            )
            try:
                final = machine.finish_adherence(medicine.id, taken=taken_bool)
            except ValueError as exc:
                return {"error": str(exc)}

        return {
            "status": "ok",
            "final_state": final.value,
            "medicine": {"id": medicine.id, "name": medicine.name},
            "speak_hint": (
                "Thank you. I marked the dose as taken."
                if taken_bool
                else "I logged this dose as missed."
            ),
        }

    def events(self, medicine_id: int) -> dict[str, Any]:
        medicine = self.db.get_medicine(medicine_id)
        if medicine is None:
            return {"error": f"Unknown medicine_id={medicine_id}"}
        return {
            "medicine": {"id": medicine.id, "name": medicine.name},
            "events": [
                {
                    "state": e.state.value,
                    "slot_id": e.slot_id,
                    "detail": e.detail,
                    "created_at": e.created_at,
                }
                for e in self.db.list_dose_events(medicine_id)
            ],
        }


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _send_json(handler: BaseHTTPRequestHandler, code: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(service: MedicineBuddyService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            print(f"[api] {self.address_string()} - {format % args}")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                _send_json(self, 200, service.health())
                return
            if path == "/medicines":
                _send_json(self, 200, service.list_medicines())
                return
            if path == "/due":
                _send_json(self, 200, service.due())
                return
            if path.startswith("/medicines/") and path.endswith("/events"):
                parts = path.split("/")
                # /medicines/{id}/events
                try:
                    medicine_id = int(parts[2])
                except (IndexError, ValueError):
                    _send_json(self, 400, {"error": "bad medicine id"})
                    return
                payload = service.events(medicine_id)
                code = 404 if "error" in payload else 200
                _send_json(self, code, payload)
                return
            _send_json(self, 404, {"error": f"unknown path {path}"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                body = _read_json(self)
            except json.JSONDecodeError:
                _send_json(self, 400, {"error": "invalid JSON"})
                return
            if path == "/schedules":
                payload = service.add_schedule(body)
                code = 400 if "error" in payload else 200
                _send_json(self, code, payload)
                return
            if path == "/doses/run":
                payload = service.run_dose(body)
                code = 400 if "error" in payload else 200
                _send_json(self, code, payload)
                return
            if path == "/doses/adherence":
                payload = service.mark_adherence(body)
                code = 400 if "error" in payload else 200
                _send_json(self, code, payload)
                return
            _send_json(self, 404, {"error": f"unknown path {path}"})

    return Handler


def serve(
    host: str,
    port: int,
    db_path: Path,
    config: AppConfig | None = None,
) -> None:
    service = MedicineBuddyService(db_path, config)
    handler = make_handler(service)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Medicine Buddy API on http://{host}:{port} (dry_run={service.config.dry_run})")
    print("Endpoints: GET /health /medicines /due  POST /schedules /doses/run")
    try:
        server.serve_forever()
    finally:
        service.close()
        server.server_close()
