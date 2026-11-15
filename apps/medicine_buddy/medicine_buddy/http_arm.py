"""HTTP client: send structured arm.v1 JSON commands to the Maker Arm PC."""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

from medicine_buddy.arm_protocol import (
    CHECK_TAKEN_PATH,
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
    new_request_id,
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

    def dispense(
        self,
        *,
        medicine_id: int,
        medicine_name: str,
        person: str | None = None,
        slot_hint: int | None = None,
        request_id: str | None = None,
    ) -> DispenseResult:
        rid = request_id or new_request_id()
        cmd = DispenseCommand(
            request_id=rid,
            medicine=MedicineRef(id=medicine_id, name=medicine_name, person=person),
            slot_hint=slot_hint,
            ocr_min_confidence=self.ocr_min_confidence,
            dry_run=self.dry_run,
        )
        raw = self._post(DISPENSE_PATH, cmd.to_json())
        result = DispenseResult.from_dict(raw, fallback_request_id=rid)
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
        rid = request_id or new_request_id()
        cmd = MoveObjectCommand(
            request_id=rid,
            object=ObjectRef(label=label, kind_hint=kind_hint or None),
            destination=destination or "bowl",
            dry_run=self.dry_run,
        )
        raw = self._post(MOVE_OBJECT_PATH, cmd.to_json())
        result = MoveObjectResult.from_dict(raw, fallback_request_id=rid)
        self.last_move = result
        logger.info(
            "arm move_object_result request_id=%s ok=%s state=%s label=%s",
            result.request_id, result.ok, result.final_state, label,
        )
        return result

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
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read(4000).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            logger.error("arm POST failed HTTP %s at %s: %s", exc.code, url, err_body or exc.reason)
            raise RuntimeError(f"arm HTTP {exc.code} at {url}: {err_body or exc.reason}") from exc
        except urllib.error.URLError as exc:
            logger.error("arm unreachable at %s: %s", url, exc.reason)
            raise RuntimeError(f"arm unreachable at {url}: {exc.reason}") from exc
        parsed = parse_json_body(raw)
        logger.info("arm response %s ← %s", path, parsed)
        return parsed
