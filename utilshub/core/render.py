"""Render files to images for previews / thumbnails (no Tk here - safe in worker threads)."""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

from PIL import Image, ImageSequence

from .imaging import IMAGE_EXTS, open_rgb, rotate_cw
from .office import OFFICE_EXTS, OfficeError, to_pdf_cached
from .settings import data_dir

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None

TEXT_EXTS = {".txt", ".md", ".csv", ".log"}


class PreviewUnavailable(Exception):
    """No visual preview possible (the reason is the message)."""


class FileRenderer:
    """Opens a file once and renders any page at any width / rotation (cached)."""

    def __init__(self, libreoffice_cmd: str = "", cache_items: int = 120) -> None:
        self.cmd = libreoffice_cmd
        self._cache: OrderedDict = OrderedDict()
        self._cache_items = cache_items
        self._lock = threading.RLock()
        self._pdf_for: dict[str, Path] = {}

    # ------------------------------------------------------------------ info
    def kind(self, path: str | Path) -> str:
        ext = Path(path).suffix.lower()
        if ext == ".pdf":
            return "pdf"
        if ext in IMAGE_EXTS:
            return "image"
        if ext in OFFICE_EXTS:
            return "office"
        if ext in TEXT_EXTS:
            return "text"
        return "other"

    def _pdf_path(self, path: Path) -> Path:
        key = str(path)
        with self._lock:
            if key in self._pdf_for and self._pdf_for[key].exists():
                return self._pdf_for[key]
        try:
            pdf = to_pdf_cached(path, data_dir() / "cache", self.cmd)
        except OfficeError as exc:
            raise PreviewUnavailable(f"{exc}\nShowing the extracted text instead.") from exc
        with self._lock:
            self._pdf_for[key] = pdf
        return pdf

    def page_count(self, path: str | Path) -> int:
        path = Path(path)
        kind = self.kind(path)
        try:
            if kind == "image":
                with Image.open(path) as im:
                    return getattr(im, "n_frames", 1)
            if kind in ("pdf", "office"):
                if fitz is None:
                    raise PreviewUnavailable("PyMuPDF is not installed, so PDFs cannot be previewed.")
                with fitz.open(self._pdf_path(path) if kind == "office" else path) as doc:
                    return len(doc)
        except PreviewUnavailable:
            raise
        except Exception as exc:
            raise PreviewUnavailable(f"Could not open the file: {exc}") from exc
        raise PreviewUnavailable("This file type has no visual preview - see the extracted text tab.")

    # ------------------------------------------------------------------ rendering
    def render(self, path: str | Path, page: int = 0, width: int = 800, rotation: int = 0,
               fix: dict | None = None) -> Image.Image:
        """Page `page` (0-based) scaled to `width` px, rotated clockwise by `rotation` extra degrees."""
        path = Path(path)
        try:
            mtime = path.stat().st_mtime_ns
        except OSError as exc:
            raise PreviewUnavailable(f"Could not open the file: {exc}") from exc
        key = (str(path), mtime, page, width, rotation % 360, tuple(sorted((fix or {}).items())))
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        kind = self.kind(path)
        try:
            if kind == "image":
                with Image.open(path) as im:
                    frames = getattr(im, "n_frames", 1)
                    im.seek(min(page, frames - 1))
                    img = open_rgb(im.copy()) if frames > 1 else open_rgb(path)
                if fix and fix.get("rot"):
                    img = rotate_cw(img, fix["rot"])
                img = rotate_cw(img, rotation)
                if img.width != width:
                    img = img.resize((width, max(1, round(img.height * width / img.width))),
                                     Image.Resampling.LANCZOS if width < img.width else Image.Resampling.BICUBIC)
            elif kind in ("pdf", "office"):
                if fitz is None:
                    raise PreviewUnavailable("PyMuPDF is not installed, so PDFs cannot be previewed.")
                src = self._pdf_path(path) if kind == "office" else path
                with fitz.open(src) as doc:
                    pg = doc[page]
                    rect = pg.rect          # already includes the page's own /Rotate
                    final_w = rect.height if rotation % 180 else rect.width   # width after our extra turn
                    zoom = width / max(1.0, final_w)
                    pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                    img = rotate_cw(Image.frombytes("RGB", (pix.width, pix.height), pix.samples), rotation)
            else:
                raise PreviewUnavailable("This file type has no visual preview - see the extracted text tab.")
        except PreviewUnavailable:
            raise
        except IndexError as exc:
            raise PreviewUnavailable("That page does not exist.") from exc
        except Exception as exc:
            raise PreviewUnavailable(f"Could not render a preview: {exc}") from exc
        with self._lock:
            self._cache[key] = img
            while len(self._cache) > self._cache_items:
                self._cache.popitem(last=False)
        return img

    def page_size(self, path: str | Path, page: int = 0) -> tuple[float, float]:
        """Aspect-ratio source: (width, height) of the unrotated page in arbitrary units."""
        path = Path(path)
        kind = self.kind(path)
        if kind == "image":
            with Image.open(path) as im:
                return im.size
        if kind in ("pdf", "office") and fitz is not None:
            with fitz.open(self._pdf_path(path) if kind == "office" else path) as doc:
                r = doc[min(page, len(doc) - 1)].rect
                return r.width, r.height
        return (595.0, 842.0)

    def thumbnail(self, path: str | Path, page: int = 0, size: int = 160, rotation: int = 0,
                  fix: dict | None = None) -> Image.Image:
        img = self.render(path, page, size * 2 if size < 200 else size, rotation, fix)
        thumb = img.copy()
        thumb.thumbnail((size, int(size * 1.5)), Image.Resampling.LANCZOS)
        return thumb

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
