"""HTTP client: send structured arm.v1 JSON commands to the Maker Arm PC."""

from __future__ import annotations

import logging
import os
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

# Fail-fast before a long POST when the arm PC is offline / wrong network.
PROBE_TIMEOUT_S = 2.0

from medicine_buddy.arm_protocol import (
    CHECK_TAKEN_PATH,
    DEFAULT_DESTINATION,
    DISPENSE_PATH,
    MOVE_OBJECT_PATH,
    CheckTakenCommand,
    CheckTakenResult,
    DispenseCommand,
    DispenseResult,
    MedicineRef,
    MoveObjectCommand,
    MoveObjectResult,
    ObjectRef,
    dispense_actions_for,
    new_request_id,
    normalize_destination,
    parse_json_body,
)
from medicine_buddy.config import AppConfig
from medicine_buddy.ports import AdherenceResult, PlaceVerifyResult

logger = logging.getLogger("medicine_buddy.http_arm")


def normalize_arm_base_url(url: str) -> str:
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ARM_URL must be http(s)://host:port")
    return cleaned


class HttpArmClient:
    """Remote arm: one-shot dispense + one-shot taken check over HTTP JSON."""

    supports_remote_protocol = True

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 60.0,
        dry_run: bool = False,
        ocr_min_confidence: float = 0.65,
    ) -> None:
        self.base_url = normalize_arm_base_url(base_url)
        if not self.base_url:
            raise ValueError("ARM_URL is empty")
        self.timeout_s = max(5.0, float(timeout_s))
        self.dry_run = dry_run
        self.ocr_min_confidence = ocr_min_confidence
        self.last_dispense: DispenseResult | None = None
        self.last_check: CheckTakenResult | None = None
        self.last_move: MoveObjectResult | None = None
        # Compatibility with ArmPort when orchestrator falls back to local steps
        self._last_slot: int | None = None

    @classmethod
    def from_config(cls, config: AppConfig) -> "HttpArmClient | None":
        url = (config.arm_url or "").strip()
        if not url:
            return None
        return cls(
            url,
            timeout_s=config.arm_timeout_seconds,
            dry_run=config.dry_run,
            ocr_min_confidence=config.ocr_min_confidence,
        )

    def probe_reachable(self, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
        """True if TCP connect to the arm host:port succeeds quickly.

        Used so voice tools can keep talking when the arm PC is down, instead of
        blocking for the full POST timeout (often 60–90s).
        """
        parsed = urlparse(self.base_url)
        host = parsed.hostname
        if not host:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=max(0.2, float(timeout_s))):
                return True
        except OSError as exc:
            logger.warning("arm probe failed %s:%s — %s", host, port, exc)
            return False

    def dispense(
        self,
        *,
        medicine_id: int,
        medicine_name: str,
        person: str | None = None,
        slot_hint: int | None = None,
        destination: str = DEFAULT_DESTINATION,
        request_id: str | None = None,
    ) -> DispenseResult:
        rid = request_id or new_request_id()
        dest = normalize_destination(destination)
        cmd = DispenseCommand(
            request_id=rid,
            medicine=MedicineRef(id=medicine_id, name=medicine_name, person=person),
            slot_hint=slot_hint,
            ocr_min_confidence=self.ocr_min_confidence,
            timeout_seconds=self.timeout_s,
            destination=dest,
            actions=dispense_actions_for(dest),
            dry_run=self.dry_run,
        )
        try:
            raw = self._post(DISPENSE_PATH, cmd.to_json())
            result = DispenseResult.from_dict(raw, fallback_request_id=rid)
        except RuntimeError as exc:
            if not _is_timeout_error(exc):
                raise
            # Object gone / wrong label / arm searching forever — fail cleanly so busy unlocks.
            result = DispenseResult(
                request_id=rid,
                ok=False,
                final_state="failed",
                detect_detail="timeout",
                error=(
                    f"arm timed out after {self.timeout_s:.0f}s looking for "
                    f"{medicine_name!r} (object missing or not recognized)"
                ),
            )
        self.last_dispense = result
        self._last_slot = result.slot_id
        logger.info(
            "arm dispense_result request_id=%s ok=%s state=%s slot=%s",
            result.request_id, result.ok, result.final_state, result.slot_id,
        )
        return result

    def check_taken(
        self,
        *,
        medicine_id: int,
        medicine_name: str,
        person: str | None = None,
        slot_id: int | None = None,
        timeout_seconds: float | None = None,
        request_id: str | None = None,
    ) -> CheckTakenResult:
        rid = request_id or new_request_id()
        cmd = CheckTakenCommand(
            request_id=rid,
            medicine=MedicineRef(id=medicine_id, name=medicine_name, person=person),
            slot_id=slot_id if slot_id is not None else self._last_slot,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else 8.0,
            dry_run=self.dry_run,
        )
        raw = self._post(CHECK_TAKEN_PATH, cmd.to_json())
        result = CheckTakenResult.from_dict(raw, fallback_request_id=rid)
        self.last_check = result
        logger.info(
            "arm check_taken_result request_id=%s ok=%s taken=%s",
            result.request_id, result.ok, result.taken,
        )
        return result

    def move_object(
        self,
        *,
        label: str,
        destination: str = "bowl",
        kind_hint: str | None = None,
        request_id: str | None = None,
    ) -> MoveObjectResult:
        """POST /v1/move_object; if the arm PC only has /v1/dispense (404), fall back.

        Several arm builds only implement dispense and locate by spoken name
        (e.g. "small bottle") — same JSON shape as a medicine dispense.
        """
        rid = request_id or new_request_id()
        force_dispense = (os.environ.get("MEDICINE_BUDDY_ARM_MOVE_VIA") or "").strip().lower() in {
            "dispense", "v1/dispense", "/v1/dispense",
        }
        dest = normalize_destination(destination)
        if force_dispense:
            result = self._move_via_dispense(label=label, destination=dest, request_id=rid)
        else:
            cmd = MoveObjectCommand(
                request_id=rid,
                object=ObjectRef(label=label, kind_hint=kind_hint or None),
                destination=dest,
                timeout_seconds=self.timeout_s,
                dry_run=self.dry_run,
            )
            try:
                raw = self._post(MOVE_OBJECT_PATH, cmd.to_json())
                result = MoveObjectResult.from_dict(raw, fallback_request_id=rid)
            except RuntimeError as exc:
                if _is_timeout_error(exc):
                    result = MoveObjectResult(
                        request_id=rid,
                        ok=False,
                        final_state="failed",
                        detect_detail="timeout",
                        error=(
                            f"arm timed out after {self.timeout_s:.0f}s looking for "
                            f"{label!r} (object missing or not recognized)"
                        ),
                    )
                # Arm returns HTTP 404 {"error":"not found"} when the path is missing.
                elif "HTTP 404" not in str(exc):
                    raise
                else:
                    logger.info(
                        "arm /v1/move_object not found; falling back to /v1/dispense for %r",
                        label,
                    )
                    result = self._move_via_dispense(
                        label=label, destination=dest, request_id=rid,
                    )
        self.last_move = result
        logger.info(
            "arm move_object_result request_id=%s ok=%s state=%s label=%s",
            result.request_id, result.ok, result.final_state, label,
        )
        return result

    def _move_via_dispense(
        self,
        *,
        label: str,
        destination: str = DEFAULT_DESTINATION,
        request_id: str,
    ) -> MoveObjectResult:
        """Map a graspable label onto /v1/dispense (arm locates by name)."""
        dispense = self.dispense(
            medicine_id=0,
            medicine_name=label,
            person=None,
            destination=destination,
            request_id=request_id,
        )
        return MoveObjectResult(
            request_id=dispense.request_id,
            ok=dispense.ok,
            final_state=dispense.final_state,
            detect_detail=dispense.detect_detail or (dispense.error or ""),
            error=dispense.error,
        )

    def to_adherence(self, result: CheckTakenResult) -> AdherenceResult:
        if result.taken is True:
            return AdherenceResult(taken=True, via=result.via or "camera", detail=result.detail or "arm one-shot")
        if result.taken is False:
            return AdherenceResult(
                taken=False,
                via=result.via or "camera",
                detail=result.detail or result.error or "arm one-shot: not taken",
            )
        return AdherenceResult(
            taken=False,
            via="timeout_missed",
            detail=result.detail or result.error or "arm camera inconclusive",
        )

    # ---- ArmPort-compatible stubs (local path unused when remote_dispense is used)
    def replay_slot_to_bowl(self, slot_id: int) -> None:
        raise RuntimeError(
            f"HttpArmClient uses remote /v1/dispense; do not call replay_slot_to_bowl({slot_id})"
        )

    def verify_place(self, slot_id: int) -> PlaceVerifyResult:
        last = self.last_dispense
        if last and last.slot_id == slot_id:
            return PlaceVerifyResult(ok=last.place_verified and last.ok, detail=last.detect_detail)
        return PlaceVerifyResult(ok=False, detail="no remote dispense result for this slot")

    def _post(self, path: str, body: str) -> dict[str, Any]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        logger.info("arm POST %s → %s", path, body)
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "MedicineBuddy-arm.v1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 — URL validated
                raw = resp.read(2_000_000)
        except TimeoutError as exc:
            logger.error("arm POST timed out after %.0fs at %s", self.timeout_s, url)
            raise RuntimeError(
                f"arm timed out after {self.timeout_s:.0f}s at {url}"
            ) from exc
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read(4000).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            logger.error("arm POST failed HTTP %s at %s: %s", exc.code, url, err_body or exc.reason)
            raise RuntimeError(f"arm HTTP {exc.code} at {url}: {err_body or exc.reason}") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                logger.error("arm POST timed out after %.0fs at %s", self.timeout_s, url)
                raise RuntimeError(
                    f"arm timed out after {self.timeout_s:.0f}s at {url}"
                ) from exc
            logger.error("arm unreachable at %s: %s", url, reason)
            raise RuntimeError(f"arm unreachable at {url}: {reason}") from exc
        parsed = parse_json_body(raw)
        logger.info("arm response %s ← %s", path, parsed)
        return parsed


def _is_timeout_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text
