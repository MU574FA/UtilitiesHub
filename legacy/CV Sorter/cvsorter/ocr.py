"""OCR helpers built on Tesseract (via pytesseract)."""
from __future__ import annotations

from PIL import Image, ImageOps

from .config import Settings


class OcrUnavailable(Exception):
    """Tesseract (or pytesseract / a language pack) is missing or failing."""


def _engine(settings: Settings):
    try:
        import pytesseract
    except ImportError as exc:
        raise OcrUnavailable("pytesseract is not installed (pip install pytesseract)") from exc
    pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd or "tesseract"
    return pytesseract


def check_tesseract(settings: Settings) -> str:
    """Return the Tesseract version string or raise OcrUnavailable."""
    engine = _engine(settings)
    try:
        return str(engine.get_tesseract_version())
    except Exception as exc:  # TesseractNotFoundError, OSError ...
        raise OcrUnavailable(
            "Tesseract executable not found. Install it and/or set its path in Settings > OCR."
        ) from exc


def _preprocess(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("L")
    if min(image.size) < 1000:  # small scans OCR much better when upscaled
        image = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
    return ImageOps.autocontrast(image)


def ocr_image(image: Image.Image, settings: Settings) -> str:
    engine = _engine(settings)
    try:
        return engine.image_to_string(_preprocess(image), lang=settings.ocr_languages or "eng")
    except engine.TesseractNotFoundError as exc:
        raise OcrUnavailable("Tesseract executable not found (see Settings > OCR).") from exc
    except engine.TesseractError as exc:
        raise OcrUnavailable(f"Tesseract failed: {exc}") from exc
