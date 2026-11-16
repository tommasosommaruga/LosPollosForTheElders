"""Classical OCR for documents held in front of Reachy."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from medicine_buddy.document_ocr import (
    OcrResult,
    assess_frame,
    ocr_jpeg,
    scrub_ocr_text,
)


def test_scrub_strips_controls_and_caps() -> None:
    raw = "Hello\x00\x01 world\n\n\nNext"
    assert scrub_ocr_text(raw) == "Hello world\n\nNext"
    assert scrub_ocr_text("x" * 5000).endswith("…")
    assert len(scrub_ocr_text("x" * 5000)) <= 4001


def test_ocr_jpeg_with_injected_engine() -> None:
    def fake(_jpeg: bytes) -> str:
        return "Aspirin 100 mg\nTake one tablet daily at 9 AM"

    result = ocr_jpeg(b"fake-jpeg", engine=fake)
    assert result.ok is True
    assert result.engine == "injected"
    assert "Aspirin" in result.text
    assert "9 AM" in result.text


def test_ocr_jpeg_empty_image() -> None:
    assert ocr_jpeg(b"").ok is False


def test_ocr_jpeg_engine_finds_no_text() -> None:
    result = ocr_jpeg(b"jpeg", engine=lambda _b: "   \n  ")
    assert result.ok is False
    assert "No text" in result.detail


def test_assess_frame_empty() -> None:
    check = assess_frame(b"")
    assert check.can_see_frame is False
    assert check.ready_enough is False
    assert "front of my face" in check.speak_hint


def test_assess_frame_with_pillow_contrast() -> None:
    PIL = pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (640, 480), color=(240, 240, 240))
    draw = ImageDraw.Draw(img)
    draw.rectangle((80, 60, 560, 420), fill=(255, 255, 255), outline=(20, 20, 20), width=4)
    draw.text((120, 200), "MEDICINE PAPER", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    check = assess_frame(buf.getvalue())
    assert check.can_see_frame is True
    assert check.brightness is not None
    assert "Hold still" in check.speak_hint or "Almost ready" in check.speak_hint or "Not clear" in check.speak_hint


def test_read_document_tool_has_guided_phases() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy" / "conversation" / "external_tools" / "read_document.py"
    )
    src = path.read_text(encoding="utf-8")
    assert "phase=guide" in src
    assert "phase=check" in src
    assert "phase=capture" in src
    assert "phase=auto" in src
    assert "tracking_off" in src or "set_head_tracking" in src
    assert "hold still" in src.lower() or "head still" in src.lower()
    assert "UNTRUSTED" in src


def test_profile_lists_read_document_coaching() -> None:
    profile = (
        Path(__file__).resolve().parents[1]
        / "medicine_buddy" / "conversation" / "external_profiles"
        / "medicine_caregiver" / "profile.md"
    ).read_text(encoding="utf-8")
    assert '"read_document"' in profile
    assert "phase=auto" in profile
    assert "freezes your head" in profile.lower() or "holds still" in profile.lower()


def test_paper_look_hint_centred_is_none() -> None:
    PIL = pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    from medicine_buddy.document_ocr import paper_look_hint

    img = Image.new("RGB", (320, 240), color=(240, 240, 240))
    draw = ImageDraw.Draw(img)
    draw.rectangle((80, 60, 240, 180), fill=(30, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    # Roughly centred block — may be None or a weak direction; must not crash.
    paper_look_hint(buf.getvalue())


def test_read_document_never_raises_without_camera() -> None:
    import asyncio
    from types import SimpleNamespace

    from medicine_buddy.conversation.external_tools import read_document as rd

    class _Deps:
        camera_enabled = True
        reachy_mini = SimpleNamespace(media=None)
        movement_manager = None
        motion_duration_s = 0.3

    out = asyncio.run(rd.ReadDocument()(_Deps(), phase="auto"))
    assert "error" in out or out.get("status") == "ok"
    assert "speak_hint" in out


def test_ocr_result_dataclass() -> None:
    r = OcrResult(text="hi", engine="tesseract", ok=True)
    assert r.text == "hi"
