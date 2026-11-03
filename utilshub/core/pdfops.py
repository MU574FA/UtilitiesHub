"""PDF building blocks used by PDF Desk and DocPacker (PyMuPDF).

    pieces = [Piece("a.pdf"), Piece("scan.jpg", rotate=90), Piece("b.pdf", pages=[0, 2])]
    result = assemble(pieces, "out.pdf", AssembleOptions(page_size="a4"))

A `Piece` is "one source file (or some of its pages) plus the manual corrections the user made":
extra clockwise rotation per page, image straightening, which pages to keep, a bookmark title.
"""
from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

from . import arabic
from .textutil import has_arabic
from .imaging import IMAGE_EXTS, apply_fix, open_rgb, rotate_cw, to_jpeg_bytes
from .office import OFFICE_EXTS, OfficeError, convert, docx_fallback_pdf
from .ocr import find_libreoffice

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

# (image quality, longest image side in px, PDF image dpi): level 0 = the user's own settings
COMPRESSION_LEVELS = [(None, None, None), (80, 2400, 170), (65, 1800, 130), (50, 1400, 100), (40, 1100, 80)]


class PdfError(Exception):
    """A user-facing PDF problem."""


@dataclass
class Piece:
    path: str
    pages: list[int] | None = None                  # 0-based source pages to use (None = all)
    rotate: int = 0                                 # extra clockwise degrees for every page
    page_rotate: dict[int, int] = field(default_factory=dict)   # extra clockwise degrees per source page
    fix: dict | None = None                         # images: {"rot", "skew"} from auto-detection
    title: str = ""                                 # bookmark
    skip: bool = False

    @property
    def kind(self) -> str:
        ext = Path(self.path).suffix.lower()
        return "pdf" if ext == ".pdf" else "image" if ext in IMAGE_EXTS else "office" if ext in OFFICE_EXTS else "other"


@dataclass
class AssembleOptions:
    page_size: str = "a4"              # a4 | letter | fit (page matches each image)
    orientation: str = "auto"          # image pages: auto (landscape photos get landscape pages) | portrait
    margin: int = 24
    quality: int = 90
    max_px: int | None = None          # shrink images before embedding
    dpi: int | None = None             # re-compress images inside added PDFs
    bookmarks: bool = True
    cover_pages: int = 0               # blank pages inserted first (the caller draws on them)
    metadata: dict = field(default_factory=dict)
    page_numbers: str = ""             # "" | "{n}" | "Page {n} of {total}"
    watermark: str = ""
    libreoffice_cmd: str = ""
    office_fallback: bool = True       # plain python-docx rendering when LibreOffice is missing


@dataclass
class AssembleResult:
    path: str
    size: int
    pages: int
    starts: dict[int, int]             # piece index -> first page number (1-based) in the output
    skipped: list[str] = field(default_factory=list)


def page_size_points(name: str) -> tuple[float, float]:
    return fitz.paper_size("letter" if name.lower() == "letter" else "a4")


# --------------------------------------------------------------------------- reading
def pdf_page_count(path: str | Path) -> int:
    with fitz.open(path) as doc:
        return len(doc)


def office_to_pdf(path: Path, tmp: Path, opts: AssembleOptions) -> Path:
    try:
        return convert(path, "pdf", tmp, opts.libreoffice_cmd)
    except OfficeError:
        if opts.office_fallback and path.suffix.lower() == ".docx":
            out = tmp / (path.stem + ".pdf")
            docx_fallback_pdf(path, out)
            return out
        raise


def _insert_image_page(out, img: Image.Image, opts: AssembleOptions) -> None:
    w, h = img.size
    quality = opts.quality
    data = to_jpeg_bytes(img, quality, opts.max_px)
    if opts.page_size == "fit":
        pw, ph = w * 0.75, h * 0.75
        page = out.new_page(width=pw, height=ph)
        page.insert_image(fitz.Rect(0, 0, pw, ph), stream=data)
        return
    pw, ph = page_size_points(opts.page_size)
    if opts.orientation == "auto" and w > h:
        pw, ph = ph, pw
    m = opts.margin
    page = out.new_page(width=pw, height=ph)
    page.insert_image(fitz.Rect(m, m, pw - m, ph - m), stream=data, keep_proportion=True)


def _rotate_added_pages(out, first_index: int, count: int, piece: Piece, src_indices: list[int]) -> None:
    for k in range(count):
        extra = piece.rotate + piece.page_rotate.get(src_indices[k], 0) if k < len(src_indices) else piece.rotate
        if extra % 360:
            pg = out[first_index + k]
            pg.set_rotation((pg.rotation + extra) % 360)


def assemble(pieces: list[Piece], out_path: str | Path, opts: AssembleOptions | None = None,
             before_save: Callable[["fitz.Document", dict[int, int]], None] | None = None,
             progress: Callable[[int, int, str], None] | None = None) -> AssembleResult:
    """Build one PDF from `pieces`.  `before_save(doc, starts)` can draw a cover etc."""
    opts = opts or AssembleOptions()
    out = fitz.open()
    toc: list[list] = []
    starts: dict[int, int] = {}
    skipped: list[str] = []
    placed: dict[tuple, int] = {}
    for _ in range(opts.cover_pages):
        w, h = page_size_points(opts.page_size if opts.page_size != "fit" else "a4")
        out.new_page(width=w, height=h)
    active = [(i, p) for i, p in enumerate(pieces) if not p.skip]
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for n, (idx, piece) in enumerate(active):
            if progress:
                progress(n, len(active), Path(piece.path).name)
            key = (piece.path, tuple(piece.pages) if piece.pages is not None else None, piece.rotate,
                   tuple(sorted(piece.page_rotate.items())))
            if key in placed:                         # identical pages already in the output
                starts[idx] = placed[key]
                continue
            start = len(out) + 1
            try:
                _append_piece(out, piece, opts, tmp)
            except (OfficeError, PdfError, OSError, ValueError, RuntimeError) as exc:
                skipped.append(f"{Path(piece.path).name}: {exc}")
                continue
            placed[key] = starts[idx] = start
            if piece.title:
                toc.append([1, piece.title, start])
    if before_save:
        before_save(out, starts)
    if opts.page_numbers:
        add_page_numbers(out, opts.page_numbers, skip_first=opts.cover_pages)
    if opts.watermark:
        add_watermark(out, opts.watermark)
    if opts.bookmarks and toc:
        out.set_toc(sorted(toc, key=lambda t: t[2]))
    if opts.metadata:
        out.set_metadata({k: str(v) for k, v in opts.metadata.items()})
    if opts.dpi and hasattr(out, "rewrite_images"):
        try:
            out.rewrite_images(dpi_threshold=opts.dpi + 40, dpi_target=opts.dpi, quality=opts.quality)
        except Exception:
            pass
    pages = len(out)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(out_path), garbage=4 if opts.dpi else 3, deflate=True)
    out.close()
    return AssembleResult(str(out_path), os.path.getsize(out_path), pages, starts, skipped)


def _append_piece(out, piece: Piece, opts: AssembleOptions, tmp: Path) -> None:
    path = Path(piece.path)
    if not path.exists():
        raise PdfError("file not found")
    first = len(out)
    kind = piece.kind
    if kind == "image":
        img = apply_fix(open_rgb(path), piece.fix)
        img = rotate_cw(img, piece.rotate + piece.page_rotate.get(0, 0))
        _insert_image_page(out, img, opts)
        return
    if kind == "office":
        src_path = office_to_pdf(path, tmp, opts)
    elif kind == "pdf":
        src_path = path
    else:
        raise PdfError("unsupported file type")
    with fitz.open(src_path) as src:
        total = len(src)
        indices = [p for p in (piece.pages if piece.pages is not None else range(total)) if 0 <= p < total]
        if not indices:
            raise PdfError("no pages selected")
        for p in indices:
            out.insert_pdf(src, from_page=p, to_page=p)
    _rotate_added_pages(out, first, len(out) - first, piece, indices)


# --------------------------------------------------------------------------- page decoration
def add_page_numbers(doc, template: str = "Page {n} of {total}", skip_first: int = 0) -> None:
    total = len(doc) - skip_first
    for i, page in enumerate(doc):
        if i < skip_first:
            continue
        text = template.replace("{n}", str(i + 1 - skip_first)).replace("{total}", str(total))
        r = page.rect
        if has_arabic(text):
            arabic.draw_text(page, (r.width - arabic.text_width(text, 9)) / 2, r.height - 14, text, 9, color=(0.35, 0.35, 0.35))
            continue
        page.insert_textbox(fitz.Rect(0, r.height - 28, r.width, r.height - 8), text, fontsize=9,
                            fontname="helv", align=1, color=(0.35, 0.35, 0.35), rotate=0)


def add_watermark(doc, text: str) -> None:
    for page in doc:
        r = page.rect
        size = max(24, min(80, r.width / max(4, len(text)) * 1.6))
        spin = (fitz.Point(r.width / 2, r.height / 2), fitz.Matrix(1, 1).prerotate(35))
        if has_arabic(text):
            arabic.draw_text(page, r.width * 0.12, r.height * 0.55, text, size, color=(0.8, 0.8, 0.8),
                             fill_opacity=0.35, overlay=True, morph=spin)
            continue
        page.insert_text((r.width * 0.12, r.height * 0.55), text, fontsize=size,
                         fontname="helv", color=(0.8, 0.8, 0.8), fill_opacity=0.35, overlay=True, morph=spin)


# --------------------------------------------------------------------------- other tools
def split_pdf(path: str | Path, ranges: list[list[int]], out_dir: str | Path, stem: str | None = None) -> list[str]:
    """Write one PDF per page-range (0-based page lists)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or Path(path).stem
    written = []
    with fitz.open(path) as src:
        for i, pages in enumerate(ranges, 1):
            doc = fitz.open()
            for p in pages:
                doc.insert_pdf(src, from_page=p, to_page=p)
            target = out_dir / f"{stem}_part{i}.pdf"
            doc.save(str(target), garbage=3, deflate=True)
            doc.close()
            written.append(str(target))
    return written


def extract_pages(path: str | Path, pages: list[int], out_path: str | Path) -> str:
    with fitz.open(path) as src:
        doc = fitz.open()
        for p in pages:
            doc.insert_pdf(src, from_page=p, to_page=p)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(out_path), garbage=3, deflate=True)
        doc.close()
    return str(out_path)


def compress_pdf(path: str | Path, out_path: str | Path, target_bytes: int = 0) -> tuple[int, int]:
    """Re-save with stronger and stronger image shrinking until `target_bytes` is met.

    Returns (final size, level used)."""
    size, level = 0, 0
    for level, (q, _maxpx, dpi) in enumerate(COMPRESSION_LEVELS):
        with fitz.open(path) as doc:
            if dpi and hasattr(doc, "rewrite_images"):
                try:
                    doc.rewrite_images(dpi_threshold=dpi + 40, dpi_target=dpi, quality=q or 90)
                except Exception:
                    pass
            doc.save(str(out_path), garbage=4, deflate=True, clean=True)
        size = os.path.getsize(out_path)
        if target_bytes <= 0 or size <= target_bytes:
            break
    return size, level


def rotate_pdf_pages(path: str | Path, rotations: dict[int, int], out_path: str | Path) -> str:
    with fitz.open(path) as doc:
        for p, deg in rotations.items():
            if 0 <= p < len(doc):
                doc[p].set_rotation((doc[p].rotation + deg) % 360)
        doc.save(str(out_path) if str(out_path) != str(path) else str(out_path) + ".tmp", garbage=3, deflate=True)
    if str(out_path) == str(path):
        os.replace(str(out_path) + ".tmp", str(path))
    return str(out_path)


def rotate_image_file(path: str | Path, degrees: int, out_path: str | Path | None = None) -> str:
    """Rotate an image file on disk (lossless for JPEG where possible is not attempted)."""
    out_path = str(out_path or path)
    with Image.open(path) as im:
        im.load()
        rotated = rotate_cw(open_rgb(path), degrees) if im.mode not in ("RGBA", "LA", "P") else \
            im.convert("RGBA").rotate(-degrees, expand=True)
        fmt = (im.format or "PNG")
        rotated.save(out_path, fmt if fmt != "JPEG" or rotated.mode == "RGB" else "PNG", quality=95)
    return out_path


def make_searchable(path: str | Path, out_path: str | Path, cfg) -> int:
    """OCR every page and write a PDF with an invisible text layer. Returns pages processed."""
    from . import ocr
    out = fitz.open()
    with fitz.open(path) as src:
        for page in src:
            pix = page.get_pixmap(dpi=max(150, cfg.dpi), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            part = fitz.open("pdf", ocr.make_searchable_pdf(img, cfg))
            out.insert_pdf(part)
            part.close()
    out.save(str(out_path), garbage=3, deflate=True)
    n = len(out)
    out.close()
    return n


def set_pdf_metadata(path: str | Path, meta: dict) -> None:
    tmp = str(path) + ".tmp"
    with fitz.open(path) as doc:
        doc.set_metadata({k: str(v) for k, v in meta.items()})
        doc.save(tmp, garbage=3, deflate=True)
    os.replace(tmp, str(path))
