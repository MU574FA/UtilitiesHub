"""Turn any supported file into text + page information (OCR where needed).

    info = read_document(path, OcrConfig.from_settings(settings), ReadOptions(...))

`DocInfo` is the one record every tool works with (CV Sorter, DocPacker, PDF Desk ...).
Support a new file type with:

    @register(".epub")
    def read_epub(path, cfg, opts) -> DocInfo: ...
"""
from __future__ import annotations

import hashlib
import io
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageSequence

from . import ocr
from .imaging import apply_fix, has_camera_exif, open_rgb, rotate_cw
from .mrz import looks_like_passport
from .office import OfficeError, convert

try:
    import pymupdf as fitz
except ImportError:                                  # older PyMuPDF
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None

PDF_EXTS = {".pdf"}
SCAN_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}


class ReaderError(Exception):
    """The file could not be read (message is user-friendly)."""


@dataclass
class ReadOptions:
    ocr: bool = True              # allow OCR at all
    rotate: bool = True           # auto-detect sideways pages
    mrz: bool = True              # passport MRZ pass
    deskew: bool = True
    min_chars: int | None = None  # override OcrConfig.min_chars
    page_images: bool = False     # keep the biggest embedded image of photo-like pages (DocPacker)


@dataclass
class DocInfo:
    path: str
    kind: str = "other"           # pdf | image | office | text | other
    text: str = ""
    pages: int = 1
    page_texts: list[str] = field(default_factory=list)
    page_rot: list[int] = field(default_factory=list)       # auto-detected clockwise rotation per page
    page_meta: list[dict] = field(default_factory=list)
    w: int = 0
    h: int = 0
    exif_cam: bool = False
    hash: str = ""
    used_ocr: bool = False
    error: str = ""
    fix: dict = field(default_factory=dict)                 # images: {"rot":..., "skew":...}
    img_bytes: bytes | None = None                          # biggest embedded image of a photo-like page

    @property
    def name(self) -> str:
        return Path(self.path).name


Reader = Callable[[Path, "ocr.OcrConfig", ReadOptions], DocInfo]
_READERS: dict[str, Reader] = {}


def register(*extensions: str):
    def deco(func: Reader) -> Reader:
        for ext in extensions:
            _READERS[ext.lower()] = func
        return func
    return deco


def supported_extensions() -> list[str]:
    return sorted(_READERS)


def file_hash(path: Path) -> str:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def read_document(path: Path | str, cfg: "ocr.OcrConfig", opts: ReadOptions | None = None) -> DocInfo:
    """Never raises: failures are reported through DocInfo.error."""
    path = Path(path)
    opts = opts or ReadOptions()
    reader = _READERS.get(path.suffix.lower())
    info = DocInfo(str(path))
    if reader is None:
        info.error = f"Unsupported file type: {path.suffix or '(none)'}"
        return info
    try:
        info = reader(path, cfg, opts)
    except (ReaderError, OfficeError, ocr.OcrUnavailable) as exc:
        info.error = str(exc)
    except Exception as exc:                          # corrupt file etc.
        info.error = f"{exc.__class__.__name__}: {exc}"
    info.path = str(path)
    info.hash = info.hash or file_hash(path)
    if not info.page_texts and info.text:
        info.page_texts = [info.text]
    return info


def _need_ocr(text: str, cfg, opts: ReadOptions) -> bool:
    limit = opts.min_chars if opts.min_chars is not None else cfg.min_chars
    return cfg.enabled and opts.ocr and len(text.strip()) < limit


# --------------------------------------------------------------------------- plain text
@register(".txt", ".md", ".csv", ".log")
def read_text(path: Path, cfg, opts) -> DocInfo:
    raw = path.read_bytes()
    encodings = ["utf-8-sig", "cp1252"]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.insert(0, "utf-16")
    text = None
    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except UnicodeError:
            continue
    return DocInfo(str(path), "text", text if text is not None else raw.decode("latin-1", errors="replace"))


# --------------------------------------------------------------------------- images
@register(*SCAN_IMAGE_EXTS)
def read_image(path: Path, cfg, opts) -> DocInfo:
    info = DocInfo(str(path), "image")
    info.exif_cam = has_camera_exif(path)
    with Image.open(path) as probe:
        frames = getattr(probe, "n_frames", 1)
    info.pages = frames
    if not (cfg.enabled and opts.ocr):
        img = open_rgb(path)
        info.w, info.h = img.size
        return info
    if frames > 1:                                          # multi-page TIFF
        texts = []
        with Image.open(path) as im:
            for frame in ImageSequence.Iterator(im):
                texts.append(ocr.safe_ocr(frame.copy(), cfg))
        info.page_texts, info.text, info.used_ocr = texts, "\n".join(texts), True
        info.page_rot = [0] * frames
        with Image.open(path) as im:
            info.w, info.h = im.size
        return info
    img = open_rgb(path)
    fix: dict = {}
    if cfg.autorotate and opts.rotate:                      # Tesseract OSD fails on photos without text
        rot = ocr.detect_rotation(img, cfg)
        if rot:
            img = apply_fix(img, {"rot": rot})
            fix["rot"] = rot
    text = ocr.ocr_image(img, cfg)
    if cfg.mrz and opts.mrz and looks_like_passport(text):
        mz = ocr.ocr_mrz(img, cfg)
        if mz:
            text = mz + "\n" + text
    if cfg.deskew and opts.deskew and len(text.strip()) >= 80:      # text documents only, not photos
        sk = ocr.detect_skew(img)
        if sk:
            fix["skew"] = sk
    info.text, info.used_ocr, info.fix = text, True, fix
    info.w, info.h = img.size
    info.page_texts = [text]
    info.page_rot = [fix.get("rot", 0)]
    return info


# --------------------------------------------------------------------------- PDF
def _page_image_meta(doc, page, text: str) -> dict:
    meta = dict(w=0, h=0, data=None)
    if len(text.strip()) >= 80:
        return meta
    try:
        imgs = page.get_images(full=True)
        if imgs:
            best = max(imgs, key=lambda x: x[2] * x[3])
            ex = doc.extract_image(best[0])
            meta.update(w=ex["width"], h=ex["height"], data=ex["image"])
    except Exception:
        pass
    return meta


@register(".pdf")
def read_pdf(path: Path, cfg, opts) -> DocInfo:
    if fitz is None:
        raise ReaderError("PyMuPDF is not installed (pip install PyMuPDF)")
    info = DocInfo(str(path), "pdf")
    with fitz.open(path) as pdf:
        info.pages = len(pdf)
        ocr_budget = cfg.max_pages if cfg.max_pages > 0 else 10 ** 6
        for index, page in enumerate(pdf):
            text = page.get_text("text") or ""
            rot = 0
            if _need_ocr(text, cfg, opts) and index < ocr_budget:
                try:
                    pix = page.get_pixmap(dpi=cfg.dpi, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    if cfg.autorotate and opts.rotate:
                        rot = ocr.detect_rotation(img, cfg)
                        if rot:
                            img = rotate_cw(img, rot)
                    extra = ocr.ocr_image(img, cfg)
                    if extra.strip():
                        text, info.used_ocr = f"{text}\n{extra}", True
                    if cfg.mrz and opts.mrz and looks_like_passport(text):
                        mz = ocr.ocr_mrz(img, cfg)
                        if mz:
                            text = mz + "\n" + text
                except ocr.OcrUnavailable as exc:
                    info.error = f"OCR: {exc}"
                except Exception as exc:
                    info.error = f"OCR: {exc}"
            info.page_rot.append(rot)
            info.page_texts.append(text)
            if opts.page_images:
                info.page_meta.append(_page_image_meta(pdf, page, text))
    info.text = "\n".join(info.page_texts)
    return info


# --------------------------------------------------------------------------- DOCX / PPTX
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph_texts(root) -> list[str]:
    lines = []
    for para in root.iter(_W + "p"):
        pieces = para.xpath("./w:r/w:t | ./w:hyperlink/w:r/w:t")
        line = "".join(p.text or "" for p in pieces).strip()
        if line:
            lines.append(line)
    return lines


@register(".docx")
def read_docx(path: Path, cfg, opts) -> DocInfo:
    try:
        import docx
    except ImportError as exc:
        raise ReaderError("python-docx is not installed (pip install python-docx)") from exc
    document = docx.Document(str(path))
    lines: list[str] = []
    for section in document.sections:                # CVs often put the name in the header
        lines += _paragraph_texts(section.header._element)
    lines += _paragraph_texts(document.element.body)
    text = "\n".join(lines)
    info = DocInfo(str(path), "office", text)
    if _need_ocr(text, cfg, opts):
        for rel in document.part.rels.values():      # CV pasted in as a picture?
            if "image" in rel.reltype:
                try:
                    image = Image.open(io.BytesIO(rel.target_part.blob))
                except OSError:
                    continue
                extra = ocr.safe_ocr(image, cfg)
                if extra.strip():
                    info.text, info.used_ocr = f"{info.text}\n{extra}", True
    return info


@register(".pptx")
def read_pptx(path: Path, cfg, opts) -> DocInfo:
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except ImportError as exc:
        raise ReaderError("python-pptx is not installed (pip install python-pptx)") from exc
    lines: list[str] = []
    used = False

    def walk(shapes) -> None:
        nonlocal used
        for shape in shapes:
            try:
                shape_type = shape.shape_type
            except NotImplementedError:
                shape_type = None
            if shape_type == MSO_SHAPE_TYPE.GROUP:
                walk(shape.shapes)
            elif shape_type == MSO_SHAPE_TYPE.PICTURE and cfg.enabled and opts.ocr:
                try:
                    text = ocr.safe_ocr(Image.open(io.BytesIO(shape.image.blob)), cfg)
                except (OSError, ValueError):
                    continue
                if text.strip():
                    lines.append(text)
                    used = True
            elif getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.extend(c.text for c in row.cells if c.text.strip())
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                lines.extend(p.text for p in shape.text_frame.paragraphs if p.text.strip())

    for slide in Presentation(str(path)).slides:
        walk(slide.shapes)
    return DocInfo(str(path), "office", "\n".join(lines), used_ocr=used)


def _via_libreoffice(path: Path, cfg, opts, target: str) -> DocInfo:
    with tempfile.TemporaryDirectory() as tmp:
        converted = convert(path, target, Path(tmp), cfg.libreoffice_cmd)
        info = read_document(converted, cfg, opts)
    if info.error:
        raise ReaderError(info.error)
    info.path = str(path)
    return info


@register(".doc", ".rtf", ".odt")
def read_legacy_word(path: Path, cfg, opts) -> DocInfo:
    return _via_libreoffice(path, cfg, opts, "docx")


@register(".ppt", ".odp")
def read_legacy_slides(path: Path, cfg, opts) -> DocInfo:
    return _via_libreoffice(path, cfg, opts, "pptx")
