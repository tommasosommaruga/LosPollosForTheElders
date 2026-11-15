"""Runtime configuration with sensible defaults (override via agents.local later)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class AppConfig:
    dry_run: bool = True
    ocr_min_confidence: float = 0.65
    adherence_timeout_seconds: float = 5.0
    # Maker Arm PC: when set, confirm → HTTP JSON dispense + one-shot camera check_taken
    arm_url: str = ""
    arm_timeout_seconds: float = 90.0
    tray_labels: list[str] = field(
        default_factory=lambda: ["ASPIRIN", "VITAMIN_D", "EMPTY", "EMPTY"]
    )
    # Dry-run OCR scores keyed by normalized label
    dry_run_scores: dict[str, float] = field(
        default_factory=lambda: {"ASPIRIN": 0.92, "VITAMIN_D": 0.88}
    )


def load_app_config(*, dry_run: bool | None = None) -> AppConfig:
    """Build config from env. dry_run=None → MEDICINE_BUDDY_DRY_RUN (default true)."""
    if dry_run is None:
        dry_run = os.environ.get("MEDICINE_BUDDY_DRY_RUN", "1").lower() not in {
            "0", "false", "no", "off",
        }
    try:
        ocr = float(os.environ.get("MEDICINE_BUDDY_OCR_MIN_CONFIDENCE", "0.65"))
    except ValueError:
        ocr = 0.65
    try:
        arm_timeout = float(os.environ.get("MEDICINE_BUDDY_ARM_TIMEOUT_S", "90"))
    except ValueError:
        arm_timeout = 90.0
    try:
        adhere = float(os.environ.get("MEDICINE_BUDDY_ADHERENCE_TIMEOUT_S", "5"))
    except ValueError:
        adhere = 5.0
    return AppConfig(
        dry_run=bool(dry_run),
        ocr_min_confidence=max(0.0, min(ocr, 1.0)),
        adherence_timeout_seconds=max(1.0, adhere),
        arm_url=(os.environ.get("MEDICINE_BUDDY_ARM_URL") or "").strip(),
        arm_timeout_seconds=max(5.0, arm_timeout),
    )
