"""Office-file conversion through LibreOffice (headless) with a serialising lock."""
from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path

from .ocr import find_libreoffice

OFFICE_EXTS = {".docx", ".doc", ".rtf", ".odt", ".pptx", ".ppt", ".odp", ".xlsx", ".xls", ".ods"}
WORD_EXTS = {".docx", ".doc", ".rtf", ".odt"}
SLIDE_EXTS = {".pptx", ".ppt", ".odp"}
_LOCK = threading.Lock()          # LibreOffice cannot run two headless conversions at once


class OfficeError(Exception):
    """A conversion failed (the message is user-friendly)."""


def convert(path: Path, target: str, outdir: Path, cmd: str = "", timeout: int = 180) -> Path:
    """Convert `path` to `target` ('pdf', 'docx', 'pptx'...) inside `outdir`."""
    exe = find_libreoffice(cmd)
    if not exe:
        raise OfficeError(f"{path.suffix} needs LibreOffice for conversion. Install it or set its path "
                          "in Settings > OCR & converters.")
    with _LOCK:
        try:
            subprocess.run([exe, "--headless", "--convert-to", target, "--outdir", str(outdir), str(path)],
                           check=True, capture_output=True, timeout=timeout)
        except (subprocess.SubprocessError, OSError) as exc:
            raise OfficeError(f"LibreOffice conversion failed: {exc}") from exc
    produced = next(outdir.glob(f"*.{target}"), None)
    if produced is None:
        raise OfficeError("LibreOffice produced no output.")
    return produced


def to_pdf_cached(path: Path, cache_dir: Path, cmd: str = "") -> Path:
    """PDF copy of an office file, cached by path + modification time."""
    import hashlib
    st = path.stat()
    key = hashlib.sha1(f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:20]
    target = cache_dir / f"{key}.pdf"
    if target.exists():
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        produced = convert(path, "pdf", Path(tmp), cmd)
        target.write_bytes(produced.read_bytes())
    return target


def docx_fallback_pdf(path: Path, out_pdf: Path) -> None:
    """Very plain Word -> PDF (text, headings, tables) for machines without LibreOffice."""
    import html as _html

    import pymupdf as fitz
    import docx

    d = docx.Document(str(path))
    parts: list[str] = []
    for para in d.paragraphs:
        text = _html.escape(para.text)
        if not text.strip():
            parts.append("<p>&nbsp;</p>")
            continue
        style = (para.style.name or "").lower() if para.style is not None else ""
        if style.startswith("heading") or style == "title":
            parts.append(f"<h2>{text}</h2>")
        else:
            parts.append(f"<p dir='auto'>{text}</p>")
    for table in d.tables:
        rows = "".join("<tr>" + "".join(f"<td style='border:1px solid #888;padding:2px'>{_html.escape(c.text)}</td>"
                                        for c in row.cells) + "</tr>" for row in table.rows)
        parts.append(f"<table style='border-collapse:collapse'>{rows}</table>")
    story = fitz.Story("<body style='font-family:sans-serif;font-size:11pt'>" + "".join(parts) + "</body>")
    writer = fitz.DocumentWriter(str(out_pdf))
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (50, 50, -50, -50)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
