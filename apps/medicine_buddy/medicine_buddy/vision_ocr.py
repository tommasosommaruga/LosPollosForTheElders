"""Hardware vision OCR stub — dry-run uses FakeVision instead."""

from __future__ import annotations

from medicine_buddy.config import AppConfig
from medicine_buddy.ports import OcrMatch


class HardwareVisionOcr:
    """Placeholder for dual-cam OCR (EasyOCR/Tesseract) against tray labels."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def locate_medicine(self, medicine_name: str) -> OcrMatch | None:
        raise NotImplementedError(
            "Hardware OCR not wired yet. Use FakeVision via --dry-run. "
            f"Requested medicine={medicine_name!r}"
        )
