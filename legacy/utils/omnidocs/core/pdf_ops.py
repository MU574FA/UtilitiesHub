"""PyMuPDF Document Operations Engine.
Supports merging PDFs, DOCX, and images; manual page rotation and reordering;
thumbnail rendering; cover page generation; and bookmark management.
"""
from __future__ import annotations

import io
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image

try:
    import pymupdf as fitz
except Exception:
    try:
        import fitz
    except Exception:
        fitz = None

try:
    import docx
except Exception:
    docx = None

log = logging.getLogger(__name__)

# Standard A4 points (72 points per inch)
A4_WIDTH = 595.28
A4_HEIGHT = 841.89
DEFAULT_MARGIN = 28.0


def is_pdf(path: Path | str) -> bool:
    return str(path).lower().endswith(".pdf")


def is_image(path: Path | str) -> bool:
    ext = Path(path).suffix.lower()
    return ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif")


def is_docx(path: Path | str) -> bool:
    return Path(path).suffix.lower() in (".docx", ".doc")


def render_page_thumbnail(doc_or_path: Any, page_num: int = 0, dpi: int = 120) -> Image.Image | None:
    """Renders a PDF page or image file to a PIL Image."""
    if fitz is None:
        return None
    try:
        if isinstance(doc_or_path, (str, Path)):
            p = Path(doc_or_path)
            if is_image(p):
                with Image.open(p) as img:
                    return img.copy()
            doc = fitz.open(str(p))
            should_close = True
        else:
            doc = doc_or_path
            should_close = False

        if page_num < 0 or page_num >= len(doc):
            return None

        page = doc[page_num]
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        if should_close:
            doc.close()
        return img
    except Exception as exc:
        log.error("Failed to render page %d thumbnail: %s", page_num, exc)
        return None


def get_document_page_count(path: Path | str) -> int:
    """Returns number of pages in a document (1 for images)."""
    p = Path(path)
    if is_image(p):
        return 1
    if not is_pdf(p):
        return 1
    if fitz is None:
        return 1
    try:
        with fitz.open(str(p)) as doc:
            return len(doc)
    except Exception:
        return 1


def docx_to_pdf_bytes(path: Path | str) -> bytes | None:
    """
    Converts a Word .docx document into a clean PDF.
    First attempts headless LibreOffice if available; otherwise uses python-docx & PyMuPDF.
    """
    p = Path(path)
    # Check headless LibreOffice
    soffice_cmd = "soffice" if os.name != "nt" else r"C:\Program Files\LibreOffice\program\soffice.exe"
    if os.path.exists(soffice_cmd) or shutil_which("soffice"):
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as tmp_dir:
                cmd = [soffice_cmd, "--headless", "--convert-to", "pdf", str(p), "--outdir", tmp_dir]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                pdf_cand = Path(tmp_dir) / f"{p.stem}.pdf"
                if pdf_cand.exists():
                    return pdf_cand.read_bytes()
        except Exception:
            pass

    # Pure Python fallback using python-docx and PyMuPDF
    if docx is None or fitz is None:
        return None

    try:
        doc_in = docx.Document(str(p))
        pdf_out = fitz.open()
        
        # Add page
        page = pdf_out.new_page(width=A4_WIDTH, height=A4_HEIGHT)
        margin = 40.0
        y = margin
        usable_w = A4_WIDTH - (margin * 2)
        
        for para in doc_in.paragraphs:
            text = para.text.strip()
            if not text:
                y += 12
                continue
            
            fontsize = 11.0
            fontname = "helv"
            if para.style and "Heading 1" in para.style.name:
                fontsize = 18.0
                fontname = "hebo"
                y += 10
            elif para.style and "Heading 2" in para.style.name:
                fontsize = 14.0
                fontname = "hebo"
                y += 6

            rect = fitz.Rect(margin, y, margin + usable_w, A4_HEIGHT - margin)
            rc = page.insert_textbox(rect, text, fontsize=fontsize, fontname=fontname, color=(0.1, 0.1, 0.1))
            
            # Check if overflowed page
            if rc < 0:
                page = pdf_out.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                y = margin
                rect = fitz.Rect(margin, y, margin + usable_w, A4_HEIGHT - margin)
                page.insert_textbox(rect, text, fontsize=fontsize, fontname=fontname, color=(0.1, 0.1, 0.1))
            
            # Estimate advance
            lines = max(1, len(text) // 65 + 1)
            y += lines * (fontsize + 4)
            if y > A4_HEIGHT - margin - 30:
                page = pdf_out.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                y = margin
                
        out_bytes = pdf_out.write()
        pdf_out.close()
        return out_bytes
    except Exception as exc:
        log.error("Failed to convert docx %s: %s", path, exc)
        return None


def shutil_which(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None


def rotate_pdf_page(path: Path | str, page_num: int, degrees: int = 90) -> bool:
    """Rotates a specific page in a PDF file in-place."""
    if fitz is None:
        return False
    try:
        doc = fitz.open(str(path))
        if 0 <= page_num < len(doc):
            cur = doc[page_num].rotation
            doc[page_num].set_rotation((cur + degrees) % 360)
            doc.saveIncr()
            doc.close()
            return True
        doc.close()
    except Exception as exc:
        log.error("Failed to rotate page %d: %s", page_num, exc)
    return False


def build_merged_pdf(items: list[dict[str, Any]], output_path: Path | str,
                     img_mode: str = "a4", margin_pt: float = DEFAULT_MARGIN,
                     toc_entries: list[list[Any]] | None = None,
                     cover_info: dict[str, Any] | None = None) -> bool:
    """
    Merges a list of items into a unified PDF.
    Item format:
      {
        'path': Path/str,
        'kind': 'pdf' | 'image' | 'docx',
        'rotation': 0, 90, 180, 270 (optional rotation adjustment),
        'pages': [0, 1, 2] (optional page range selection),
        'title': str (optional bookmark label)
      }
    """
    if fitz is None:
        log.error("PyMuPDF is required to merge documents.")
        return False

    out_doc = fitz.open()
    toc = []

    # Optional Cover Page
    if cover_info:
        draw_cover_page(out_doc, cover_info)
        toc.append([1, "Cover Page", 1])

    for item in items:
        p = Path(item["path"])
        kind = item.get("kind", "pdf" if is_pdf(p) else "image" if is_image(p) else "docx")
        rot = item.get("rotation", 0)
        title = item.get("title", p.name)
        start_page = len(out_doc) + 1

        if kind == "pdf":
            try:
                src_doc = fitz.open(str(p))
                selected_pages = item.get("pages", list(range(len(src_doc))))
                for p_idx in selected_pages:
                    if 0 <= p_idx < len(src_doc):
                        out_doc.insert_pdf(src_doc, from_page=p_idx, to_page=p_idx)
                        if rot:
                            cur_page = out_doc[-1]
                            cur_page.set_rotation((cur_page.rotation + rot) % 360)
                src_doc.close()
                toc.append([1, title, start_page])
            except Exception as exc:
                log.error("Failed to merge PDF %s: %s", p, exc)

        elif kind == "image":
            try:
                with Image.open(p) as img:
                    # Apply rotation if requested
                    if rot:
                        img = img.rotate(360 - rot, expand=True)
                    
                    # Convert to RGB if needed
                    if img.mode not in ("RGB", "L"):
                        img = img.convert("RGB")
                    
                    w_px, h_px = img.size
                    img_bytes_io = io.BytesIO()
                    img.save(img_bytes_io, format="JPEG", quality=92)
                    img_bytes = img_bytes_io.getvalue()

                    if img_mode == "own":
                        pw = w_px * 0.75
                        ph = h_px * 0.75
                        page = out_doc.new_page(width=pw, height=ph)
                        page.insert_image(fitz.Rect(0, 0, pw, ph), stream=img_bytes)
                    else:
                        is_landscape = w_px > h_px
                        pw = A4_HEIGHT if is_landscape else A4_WIDTH
                        ph = A4_WIDTH if is_landscape else A4_HEIGHT
                        page = out_doc.new_page(width=pw, height=ph)
                        
                        max_w = pw - (margin_pt * 2)
                        max_h = ph - (margin_pt * 2)
                        scale = min(max_w / w_px, max_h / h_px)
                        draw_w = w_px * scale
                        draw_h = h_px * scale
                        x = (pw - draw_w) / 2
                        y = (ph - draw_h) / 2
                        page.insert_image(fitz.Rect(x, y, x + draw_w, y + draw_h), stream=img_bytes)
                    
                    toc.append([1, title, start_page])
            except Exception as exc:
                log.error("Failed to merge image %s: %s", p, exc)

        elif kind == "docx":
            pdf_bytes = docx_to_pdf_bytes(p)
            if pdf_bytes:
                src_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                out_doc.insert_pdf(src_doc)
                src_doc.close()
                toc.append([1, title, start_page])

    # Apply bookmarks / TOC
    if toc_entries:
        out_doc.set_toc(toc_entries)
    elif toc:
        out_doc.set_toc(toc)

    out_doc.set_metadata({"title": Path(output_path).stem, "producer": "OmniDocs Studio"})
    out_doc.save(str(output_path), garbage=4, deflate=True, clean=True)
    out_doc.close()
    return True


def draw_cover_page(doc: Any, info: dict[str, Any]) -> None:
    """Generates an executive cover page with profile info and requirement checklist."""
    page = doc.new_page(width=A4_WIDTH, height=A4_HEIGHT)
    
    # Header banner
    page.draw_rect(fitz.Rect(0, 0, A4_WIDTH, 100), color=(0.1, 0.4, 0.35), fill=(0.1, 0.4, 0.35))
    page.insert_text(fitz.Point(36, 60), info.get("title", "DOCUMENT PACKAGE"), fontsize=22, fontname="hebo", color=(1, 1, 1))
    
    # Subheader date
    date_str = info.get("date", "")
    if date_str:
        page.insert_text(fitz.Point(36, 82), f"Generated: {date_str}", fontsize=10, fontname="helv", color=(0.9, 0.9, 0.9))
        
    y = 130
    # Profile grid
    profile = info.get("profile", {})
    if profile:
        page.insert_text(fitz.Point(36, y), "APPLICANT PROFILE", fontsize=12, fontname="hebo", color=(0.2, 0.2, 0.2))
        y += 18
        
        for k, v in profile.items():
            if v:
                page.insert_text(fitz.Point(40, y), f"{k}:", fontsize=10, fontname="hebo", color=(0.3, 0.3, 0.3))
                page.insert_text(fitz.Point(160, y), str(v), fontsize=10, fontname="helv", color=(0.1, 0.1, 0.1))
                y += 16
        y += 20

    # Checklist
    checklist = info.get("checklist", [])
    if checklist:
        page.insert_text(fitz.Point(36, y), "VERIFICATION CHECKLIST", fontsize=12, fontname="hebo", color=(0.2, 0.2, 0.2))
        y += 18
        
        for item in checklist:
            # status icon, requirement name, status note
            icon = item.get("icon", "✔")
            name = item.get("name", "")
            status = item.get("status", "")
            
            icon_color = (0.1, 0.6, 0.3) if icon == "✔" else (0.8, 0.2, 0.2) if icon == "✖" else (0.8, 0.5, 0.0)
            page.insert_text(fitz.Point(40, y), icon, fontsize=11, fontname="hebo", color=icon_color)
            page.insert_text(fitz.Point(60, y), name, fontsize=10, fontname="helv", color=(0.1, 0.1, 0.1))
            if status:
                page.insert_text(fitz.Point(340, y), status, fontsize=9, fontname="helv", color=(0.4, 0.4, 0.4))
            y += 18

