"""Runtime configuration with sensible defaults (override via agents.local later)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AppConfig:
    dry_run: bool = True
    ocr_min_confidence: float = 0.65
    adherence_timeout_seconds: float = 5.0
    tray_labels: list[str] = field(
        default_factory=lambda: ["ASPIRIN", "VITAMIN_D", "EMPTY", "EMPTY"]
    )
    # Dry-run OCR scores keyed by normalized label
    dry_run_scores: dict[str, float] = field(
        default_factory=lambda: {"ASPIRIN": 0.92, "VITAMIN_D": 0.88}
    )
