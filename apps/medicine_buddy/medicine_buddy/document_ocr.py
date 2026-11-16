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


def paper_look_hint(jpeg_bytes: bytes) -> str | None:
    """If a dark paper-like mass is off-centre, suggest left/right/up/down for the head.

    Returns None when the page looks centred or framing cannot be measured.
    """
    if not jpeg_bytes:
        return None
    try:
        image = _open_rgb(jpeg_bytes)
        gray = image.convert("L")
        width, height = gray.size
        if width < 32 or height < 32:
            return None
        # Downsample for a cheap ink/edge centroid (darker than mid-grey).
        sample = gray.resize((64, 48))
        pixels = list(sample.getdata())
        sw, sh = sample.size
        mass = 0.0
        cx = 0.0
        cy = 0.0
        for i, value in enumerate(pixels):
            weight = max(0.0, 180.0 - float(value))
            if weight <= 0:
                continue
            mass += weight
            cx += weight * (i % sw)
            cy += weight * (i // sw)
        if mass < 1.0:
            return None
        cx /= mass
        cy /= mass
        dx = (cx / max(sw - 1, 1)) - 0.5
        dy = (cy / max(sh - 1, 1)) - 0.5
        # Need a clear bias before we move the head.
        if abs(dx) < 0.12 and abs(dy) < 0.12:
            return None
        if abs(dx) >= abs(dy):
            return "left" if dx < 0 else "right"
        return "up" if dy < 0 else "down"
    except Exception as exc:  # noqa: BLE001 — optional coaching only
        logger.debug("paper_look_hint failed: %s", exc)
        return None


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
        # Some broken frames yield an image with no histogram — Stat then crashes.
        if gray.size[0] < 8 or gray.size[1] < 8:
            raise ValueError("frame too small")
        hist = gray.histogram()
        if not hist:
            raise ValueError("empty histogram")
        stat = ImageStat.Stat(gray)
        mean = getattr(stat, "mean", None) or []
        stddev = getattr(stat, "stddev", None) or []
        if not mean or not stddev:
            raise ValueError("stat missing mean/stddev")
        brightness = float(mean[0])
        contrast = float(stddev[0])
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
