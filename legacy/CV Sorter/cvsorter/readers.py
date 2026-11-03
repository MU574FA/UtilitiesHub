"""Turn any supported file into plain text, using OCR where needed.

To support a new file type, write a function and decorate it:

    @register(".epub")
    def read_epub(path: Path, settings: Settings) -> ReadResult: ...
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageSequence

from . import ocr
from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class ReadResult:
    text: str
    used_ocr: bool = False


class ReaderError(Exception):
    """The file could not be read."""


Reader = Callable[[Path, Settings], ReadResult]
_READERS: dict[str, Reader] = {}


def register(*extensions: str):
    def decorator(func: Reader) -> Reader:
        for ext in extensions:
            _READERS[ext.lower()] = func
        return func
    return decorator


def supported_extensions() -> list[str]:
    return sorted(_READERS)


def read_document(path: Path, settings: Settings) -> ReadResult:
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        raise ReaderError(f"Unsupported file type: {path.suffix or '(none)'}")
    return reader(path, settings)


def _safe_ocr(image: Image.Image, settings: Settings) -> str:
    """OCR that degrades gracefully (used where plain text may already exist)."""
    try:
        return ocr.ocr_image(image, settings)
    except ocr.OcrUnavailable as exc:
        log.warning("OCR skipped: %s", exc)
        return ""


# --------------------------------------------------------------------------- plain text
@register(".txt", ".md", ".csv", ".log")
def read_text(path: Path, settings: Settings) -> ReadResult:
    raw = path.read_bytes()
    encodings = ["utf-8-sig", "cp1252"]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.insert(0, "utf-16")
    for enc in encodings:
        try:
            return ReadResult(raw.decode(enc))
        except UnicodeError:
            continue
    return ReadResult(raw.decode("latin-1", errors="replace"))


# --------------------------------------------------------------------------- images
@register(".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif")
def read_image(path: Path, settings: Settings) -> ReadResult:
    if not settings.ocr_enabled:
        raise ReaderError("OCR is disabled in Settings, cannot read images.")
    try:
        with Image.open(path) as img:
            pages = [ocr.ocr_image(frame.copy(), settings) for frame in ImageSequence.Iterator(img)]
    except ocr.OcrUnavailable as exc:
        raise ReaderError(str(exc)) from exc
    return ReadResult("\n".join(pages), used_ocr=True)


# --------------------------------------------------------------------------- PDF
@register(".pdf")
def read_pdf(path: Path, settings: Settings) -> ReadResult:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise ReaderError("PyMuPDF is not installed (pip install PyMuPDF)") from exc

    chunks: list[str] = []
    used_ocr = False
    with fitz.open(path) as pdf:
        for page in pdf:
            text = page.get_text("text") or ""
            if settings.ocr_enabled and len(text.strip()) < settings.ocr_min_chars:
                pix = page.get_pixmap(dpi=settings.ocr_dpi, alpha=False)
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                ocr_text = _safe_ocr(image, settings)
                if ocr_text.strip():
                    text, used_ocr = f"{text}\n{ocr_text}", True
            chunks.append(text)
    return ReadResult("\n".join(chunks), used_ocr)


# --------------------------------------------------------------------------- DOCX
def _paragraph_texts(root) -> list[str]:
    """Text of every paragraph under `root`, including tables and text boxes."""
    lines = []
    for para in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        pieces = para.xpath("./w:r/w:t | ./w:hyperlink/w:r/w:t")
        line = "".join(p.text or "" for p in pieces).strip()
        if line:
            lines.append(line)
    return lines


@register(".docx")
def read_docx(path: Path, settings: Settings) -> ReadResult:
    try:
        import docx
    except ImportError as exc:
        raise ReaderError("python-docx is not installed (pip install python-docx)") from exc

    document = docx.Document(str(path))
    lines: list[str] = []
    for section in document.sections:  # CVs often put the name in the header
        lines += _paragraph_texts(section.header._element)
    lines += _paragraph_texts(document.element.body)
    text = "\n".join(lines)

    used_ocr = False
    if settings.ocr_enabled and len(text.strip()) < settings.ocr_min_chars:
        for rel in document.part.rels.values():  # CV pasted in as a picture?
            if "image" in rel.reltype:
                try:
                    image = Image.open(io.BytesIO(rel.target_part.blob))
                except OSError:
                    continue
                extra = _safe_ocr(image, settings)
                if extra.strip():
                    text, used_ocr = f"{text}\n{extra}", True
    return ReadResult(text, used_ocr)


# --------------------------------------------------------------------------- PPTX
@register(".pptx")
def read_pptx(path: Path, settings: Settings) -> ReadResult:
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except ImportError as exc:
        raise ReaderError("python-pptx is not installed (pip install python-pptx)") from exc

    lines: list[str] = []
    used_ocr = False

    def walk(shapes) -> None:
        nonlocal used_ocr
        for shape in shapes:
            try:
                shape_type = shape.shape_type
            except NotImplementedError:  # some exotic shapes have no type
                shape_type = None
            if shape_type == MSO_SHAPE_TYPE.GROUP:
                walk(shape.shapes)
            elif shape_type == MSO_SHAPE_TYPE.PICTURE and settings.ocr_enabled:
                try:
                    text = _safe_ocr(Image.open(io.BytesIO(shape.image.blob)), settings)
                except (OSError, ValueError):
                    continue
                if text.strip():
                    lines.append(text)
                    used_ocr = True
            elif getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.extend(c.text for c in row.cells if c.text.strip())
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                lines.extend(p.text for p in shape.text_frame.paragraphs if p.text.strip())

    for slide in Presentation(str(path)).slides:
        walk(slide.shapes)
    return ReadResult("\n".join(lines), used_ocr)


# --------------------------------------------------------------------------- legacy (LibreOffice)
_LIBREOFFICE_LOCK = threading.Lock()   # LibreOffice cannot run two headless conversions at once


def libreoffice_convert(path: Path, settings: Settings, target: str, outdir: Path) -> Path:
    """Convert `path` to `target` format (docx, pptx, pdf...) inside `outdir`."""
    exe = settings.libreoffice_cmd or shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise ReaderError(
            f"{path.suffix} needs LibreOffice for conversion. Install it or set its path in Settings."
        )
    with _LIBREOFFICE_LOCK:
        try:
            subprocess.run([exe, "--headless", "--convert-to", target, "--outdir", str(outdir), str(path)],
                           check=True, capture_output=True, timeout=120)
        except (subprocess.SubprocessError, OSError) as exc:
            raise ReaderError(f"LibreOffice conversion failed: {exc}") from exc
    converted = next(outdir.glob(f"*.{target}"), None)
    if converted is None:
        raise ReaderError("LibreOffice produced no output.")
    return converted


def _convert_with_libreoffice(path: Path, settings: Settings, target: str) -> ReadResult:
    with tempfile.TemporaryDirectory() as tmp:
        return read_document(libreoffice_convert(path, settings, target, Path(tmp)), settings)


@register(".doc", ".rtf", ".odt")
def read_legacy_word(path: Path, settings: Settings) -> ReadResult:
    return _convert_with_libreoffice(path, settings, "docx")


@register(".ppt", ".odp")
def read_legacy_slides(path: Path, settings: Settings) -> ReadResult:
    return _convert_with_libreoffice(path, settings, "pptx")
