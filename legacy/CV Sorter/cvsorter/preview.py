"""Render a visual preview of the ORIGINAL file (PNG pages) for the verification pop-up.

Images and PDFs are rendered directly; Word / PowerPoint / RTF / ODF go through LibreOffice
(PDF export). Plain text has no visual preview (the extracted-text tab shows it).
Runs in a worker thread - it never touches Tk.
"""
from __future__ import annotations

import io
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

from .config import Settings
from .readers import ReaderError, libreoffice_convert

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
OFFICE_EXTS = {".docx", ".doc", ".rtf", ".odt", ".pptx", ".ppt", ".odp"}


class PreviewUnavailable(Exception):
    """No visual preview possible (the reason is the message)."""


@dataclass
class PreviewResult:
    pages: list[bytes]   # PNG data, one per page
    total: int           # pages in the file (may be more than were rendered)


def _png(image: Image.Image, width: int) -> bytes:
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.width > width:
        image = image.resize((width, max(1, round(image.height * width / image.width))),
                             Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _image_pages(path: Path, max_pages: int, width: int) -> PreviewResult:
    with Image.open(path) as img:
        total = getattr(img, "n_frames", 1)
        pages = []
        for index, frame in enumerate(ImageSequence.Iterator(img)):
            if index >= max_pages:
                break
            pages.append(_png(frame.copy(), width))
    return PreviewResult(pages, total)


def _pdf_pages(path: Path, max_pages: int, width: int) -> PreviewResult:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise PreviewUnavailable("PyMuPDF is not installed, so PDFs cannot be previewed.") from exc
    with fitz.open(path) as pdf:
        total, pages = len(pdf), []
        for index in range(min(total, max_pages)):
            page = pdf[index]
            zoom = width / page.rect.width
            pages.append(page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png"))
    return PreviewResult(pages, total)


def render_preview(path: Path, settings: Settings) -> PreviewResult:
    ext = path.suffix.lower()
    max_pages, width = max(1, settings.preview_pages), max(300, settings.preview_width)
    try:
        if ext in IMAGE_EXTS:
            return _image_pages(path, max_pages, width)
        if ext == ".pdf":
            return _pdf_pages(path, max_pages, width)
        if ext in OFFICE_EXTS:
            with tempfile.TemporaryDirectory() as tmp:
                pdf = libreoffice_convert(path, settings, "pdf", Path(tmp))
                return _pdf_pages(pdf, max_pages, width)
    except ReaderError as exc:
        raise PreviewUnavailable(f"{exc}\nShowing the extracted text instead.") from exc
    except OSError as exc:
        raise PreviewUnavailable(f"Could not render a preview: {exc}") from exc
    raise PreviewUnavailable("This file type has no visual preview - see the extracted text tab.")
