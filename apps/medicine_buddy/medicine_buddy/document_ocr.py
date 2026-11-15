"""Classical OCR for a paper held in front of Reachy Mini (buddy camera).

Not VLA / not Maker Arm tray OCR. Takes a JPEG frame → plain text. Text is
UNTRUSTED (same rules as web/camera content).

Also assesses framing (light / contrast) so Reachy can coach position before capture.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("medicine_buddy.document_ocr")

# Optional engine: (jpeg_bytes) -> raw text
OcrEngine = Callable[[bytes], str]

_MAX_CHARS = 4000
PHASES = frozenset({"guide", "check", "capture"})


@dataclass(frozen=True)
class OcrResult:
    text: str
    engine: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class FrameCheck:
    """Quick look at a JPEG before full OCR — for spoken position feedback."""

    can_see_frame: bool
    ready_enough: bool
    brightness: float | None  # 0–255 if measured
    contrast: float | None    # grayscale std-dev if measured
    width: int | None
    height: int | None
    speak_hint: str
    tips: tuple[str, ...] = ()


def scrub_ocr_text(raw: str) -> str:
    """Keep readable text; drop nulls / extreme control chars; cap length."""
    cleaned = (raw or "").replace("\x00", " ")
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > _MAX_CHARS:
        cleaned = cleaned[:_MAX_CHARS].rstrip() + "…"
    return cleaned


def _open_rgb(jpeg_bytes: bytes):
    from PIL import Image  # type: ignore[import-untyped]

    image = Image.open(io.BytesIO(jpeg_bytes))
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    return image


def assess_frame(jpeg_bytes: bytes) -> FrameCheck:
    """Coach holding position: light, contrast, and whether a capture is worth trying."""
    if not jpeg_bytes:
        return FrameCheck(
            can_see_frame=False,
            ready_enough=False,
            brightness=None,
            contrast=None,
            width=None,
            height=None,
            speak_hint=(
                "I cannot see a picture yet. Hold the medicine paper flat in front of my face, "
                "about an arm's length away, and we will try again."
            ),
            tips=("hold paper in front of face",),
        )
    try:
        from PIL import ImageStat  # type: ignore[import-untyped]

        image = _open_rgb(jpeg_bytes)
        gray = image.convert("L")
        stat = ImageStat.Stat(gray)
        brightness = float(stat.mean[0])
        contrast = float(stat.stddev[0])
        width, height = image.size
    except ImportError:
        return FrameCheck(
            can_see_frame=True,
            ready_enough=True,
            brightness=None,
            contrast=None,
            width=None,
            height=None,
            speak_hint=(
                "I can take a picture. Hold the paper still and flat in front of me for a second, "
                "then I will capture and read it."
            ),
            tips=("hold still",),
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("Frame assess failed: %s", exc)
        return FrameCheck(
            can_see_frame=False,
            ready_enough=False,
            brightness=None,
            contrast=None,
            width=None,
            height=None,
            speak_hint="I got a bad picture. Move the paper a little and hold it steady.",
            tips=("retry",),
        )

    tips: list[str] = []
    if brightness < 45:
        tips.append("too dark — face a lamp or window")
    elif brightness > 220:
        tips.append("too bright or shiny — tilt the paper to cut glare")
    if contrast < 18:
        tips.append("I barely see edges — hold the whole page closer and fill more of my view")
    if width < 320 or height < 240:
        tips.append("image is small — come a little closer")

    ready = not tips or (brightness >= 45 and brightness <= 220 and contrast >= 18)
    if ready and not tips:
        speak = (
            "Good — I can see the page. Hold still for one short second while I take the picture, "
            "then I will read it."
        )
        tips = ["hold still ~1 second"]
    elif ready:
        speak = (
            "Almost ready. " + " ".join(tips) + " "
            "When it looks steady, hold still for one short second and I will take the picture."
        )
    else:
        speak = (
            "Not clear enough yet. " + " ".join(tips) + " "
            "Adjust the paper, then ask me to check again."
        )
        ready = False

    return FrameCheck(
        can_see_frame=True,
        ready_enough=ready,
        brightness=round(brightness, 1),
        contrast=round(contrast, 1),
        width=width,
        height=height,
        speak_hint=speak,
        tips=tuple(tips),
    )


def tesseract_engine(jpeg_bytes: bytes) -> str:
    """Requires Pillow + pytesseract + a system Tesseract install."""
    import pytesseract  # type: ignore[import-untyped]

    image = _open_rgb(jpeg_bytes)
    return pytesseract.image_to_string(image) or ""


def resolve_engine(override: OcrEngine | None = None) -> tuple[OcrEngine, str] | tuple[None, str]:
    if override is not None:
        return override, "injected"
    try:
        import PIL  # noqa: F401
        import pytesseract  # noqa: F401
    except ImportError:
        return None, (
            "OCR packages missing. On this machine: pip install 'medicine_buddy[ocr]' "
            "and install Tesseract (e.g. brew install tesseract)."
        )
    try:
        import pytesseract as _pt

        _pt.get_tesseract_version()
    except Exception as exc:  # noqa: BLE001 — binary missing or broken
        return None, (
            f"Tesseract is not available ({exc}). Install it (brew/apt install tesseract) "
            "then retry with the paper in front of Reachy."
        )
    return tesseract_engine, "tesseract"


def ocr_jpeg(jpeg_bytes: bytes, *, engine: OcrEngine | None = None) -> OcrResult:
    """Run classical OCR on a JPEG. Never raises for normal failures."""
    if not jpeg_bytes:
        return OcrResult(text="", engine="none", ok=False, detail="empty image")
    resolved, name_or_err = resolve_engine(engine)
    if resolved is None:
        return OcrResult(text="", engine="none", ok=False, detail=str(name_or_err))
    engine_name = name_or_err
    try:
        raw = resolved(jpeg_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("OCR failed")
        return OcrResult(text="", engine=engine_name, ok=False, detail=str(exc) or "ocr failed")
    text = scrub_ocr_text(raw)
    if not text:
        return OcrResult(
            text="",
            engine=engine_name,
            ok=False,
            detail="No text found. Hold the page flat and closer to Reachy's camera, then try again.",
        )
    return OcrResult(text=text, engine=engine_name, ok=True, detail="ok")
