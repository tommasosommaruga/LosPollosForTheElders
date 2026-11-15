#!/usr/bin/env python3
"""Arm PC HTTP listener for medicine_buddy.arm.v1 (Buddy POSTs here after voice yes).

Run on the arm machine (example IP 172.20.10.3):

    python3 integrations/arm/arm_listener.py

Buddy config:

    MEDICINE_BUDDY_ARM_URL=http://172.20.10.3:9090

Replace the three `run_*` functions below with real Maker Arm / camera calls.
Do not change the JSON response shapes — Buddy parses them.
"""

from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SCHEMA = "medicine_buddy.arm.v1"
logger = logging.getLogger("arm_listener")


# ---------------------------------------------------------------------------
# MINIMUM arm-team hooks — replace these bodies with your SDK / camera code.
# Keep the returned dict keys the same.
# ---------------------------------------------------------------------------

def run_dispense(cmd: dict[str, Any]) -> dict[str, Any]:
    """Called on POST /v1/dispense. Locate label → pick closed box → bowl → verify.

    Replace this stub with your motion pipeline. On failure set ok=False and
    final_state to "failed" or "aborted".
    """
    medicine = cmd.get("medicine") or {}
    dry_run = bool(cmd.get("dry_run"))
    logger.info(
        "DISPENSE medicine=%s dry_run=%s request_id=%s",
        medicine.get("name"), dry_run, cmd.get("request_id"),
    )

    # --- YOUR CODE HERE (example): ---
    # if dry_run:
    #     return _dispense_ok(cmd, slot_id=1, label=..., score=1.0, detail="dry_run")
    # slot, label, score = your_ocr.locate(medicine["name"], min_conf=cmd["ocr_min_confidence"])
    # if score < cmd["ocr_min_confidence"]:
    #     return _dispense_fail(cmd, "low_ocr_confidence")
    # your_arm.pick_slot_to_bowl(slot)
    # if not your_arm.verify_in_bowl():
    #     return _dispense_fail(cmd, "place_verify_failed", slot_id=slot)
    # return _dispense_ok(cmd, slot_id=slot, label=label, score=score, detail="box_in_bowl")

    return _dispense_ok(
        cmd,
        slot_id=1,
        label=str(medicine.get("name") or "UNKNOWN"),
        score=0.9,
        detail="listener_stub — replace run_dispense()",
    )


def run_check_taken(cmd: dict[str, Any]) -> dict[str, Any]:
    """Called on POST /v1/check_taken. One-shot camera: did they take it?

    taken: True | False | None (None = inconclusive → Buddy asks by voice).
    """
    logger.info("CHECK_TAKEN request_id=%s slot=%s", cmd.get("request_id"), cmd.get("slot_id"))

    # --- YOUR CODE HERE: ---
    # taken = your_camera.one_shot_taken(timeout=cmd.get("timeout_seconds", 8))
    # return _check_ok(cmd, taken=taken, via="camera", detail="...")

    return _check_ok(cmd, taken=None, via="camera", detail="listener_stub — replace run_check_taken()")


def run_move_object(cmd: dict[str, Any]) -> dict[str, Any]:
    """Called on POST /v1/move_object. Non-medicine graspable → destination."""
    obj = cmd.get("object") or {}
    logger.info(
        "MOVE_OBJECT label=%s dest=%s request_id=%s",
        obj.get("label"), cmd.get("destination"), cmd.get("request_id"),
    )

    # --- YOUR CODE HERE: ---
    # your_arm.locate_pick_place(obj["label"], cmd.get("destination", "bowl"))

    return {
        "schema": SCHEMA,
        "message_type": "move_object_result",
        "request_id": cmd.get("request_id"),
        "ok": True,
        "final_state": "placed",
        "detect_detail": "listener_stub — replace run_move_object()",
        "error": None,
    }


def _dispense_ok(
    cmd: dict[str, Any],
    *,
    slot_id: int,
    label: str,
    score: float,
    detail: str,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "message_type": "dispense_result",
        "request_id": cmd.get("request_id"),
        "ok": True,
        "final_state": "placed",
        "slot_id": slot_id,
        "label": label,
        "score": score,
        "place_verified": True,
        "detect_detail": detail,
        "error": None,
    }


def _dispense_fail(
    cmd: dict[str, Any],
    error: str,
    *,
    slot_id: int | None = None,
    final_state: str = "failed",
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "message_type": "dispense_result",
        "request_id": cmd.get("request_id"),
        "ok": False,
        "final_state": final_state,
        "slot_id": slot_id,
        "label": None,
        "score": None,
        "place_verified": False,
        "detect_detail": error,
        "error": error,
    }


def _check_ok(
    cmd: dict[str, Any],
    *,
    taken: bool | None,
    via: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "message_type": "check_taken_result",
        "request_id": cmd.get("request_id"),
        "ok": True,
        "taken": taken,
        "via": via,
        "detail": detail,
        "error": None,
    }


# ---------------------------------------------------------------------------
# HTTP server — usually leave as-is
# ---------------------------------------------------------------------------

class ArmHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        self._send(200, {
            "ok": True,
            "listening": True,
            "schema": SCHEMA,
            "paths": ["/v1/dispense", "/v1/check_taken", "/v1/move_object"],
        })

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"ok": False, "error": f"invalid_json: {exc}"})
            return

        logger.info("POST %s body=%s", self.path, json.dumps(data, ensure_ascii=False))

        if self.path == "/v1/dispense":
            self._send(200, run_dispense(data))
            return
        if self.path == "/v1/check_taken":
            self._send(200, run_check_taken(data))
            return
        if self.path == "/v1/move_object":
            self._send(200, run_move_object(data))
            return
        self._send(404, {"ok": False, "error": f"unknown_path:{self.path}"})

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Medicine Buddy arm.v1 listener")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9090, help="Port (default 9090)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = ThreadingHTTPServer((args.host, args.port), ArmHandler)
    logger.info("Arm listening on http://%s:%s — waiting for Buddy POSTs", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopped")
        server.server_close()


if __name__ == "__main__":
    main()
