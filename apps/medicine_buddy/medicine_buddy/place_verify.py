"""Place-verification helpers (heuristics live behind ArmPort.verify_place)."""

from __future__ import annotations

from medicine_buddy.ports import PlaceVerifyResult


def summarize_place_verify(result: PlaceVerifyResult) -> str:
    status = "ok" if result.ok else "fail"
    return f"place_verify={status}: {result.detail}"
