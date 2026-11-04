"""Scan cleaner engine: detect what is wrong with a scanned page and fix it.  No Tk, safe in worker threads.

    spec = CleanSpec()                                        # what to do (from settings or a preset)
    ana = analyse_image(page_image, spec, ocr_cfg)             # what is wrong: turned, tilted, borders, blank
    img = process_page(page_image, spec, ana, PageEdit(skew=1.2))     # fix it; PageEdit = manual overrides
    clean_file("scan.pdf", "scan_clean.pdf", spec, analyses, edits)   # whole file (images and PDFs)

Pipeline per page:  rotate -> straighten -> crop borders -> denoise -> enhance -> brightness / contrast -> sharpen.
Detected values (PageAnalysis) and the user's manual values (PageEdit) are kept apart: a PageEdit field that is
None means "automatic", so one click can always go back to what the program found.
"""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Callable, Iterator

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .imaging import FITZ_LOCK, IMAGE_EXTS, open_rgb, rotate_cw

try:
    import numpy as np
except ImportError:
    np = None
try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None

log = logging.getLogger(__name__)

ENHANCE_MODES = [("none", "Leave the colours"), ("clean", "Clean (white paper, keep colour)"),
                 ("gray", "Grey scale"), ("bw", "Black & white")]
CROP_MODES = [("auto", "Trim borders automatically"), ("none", "Do not crop")]


class CleanError(Exception):
    """Something the user should read (message is friendly)."""


# --------------------------------------------------------------------------- settings
@dataclass
class CleanSpec:
    rotate: bool = True              # turn sideways / upside-down pages upright (needs Tesseract to detect)
    deskew: bool = True              # straighten slightly tilted pages
    crop: str = "auto"               # auto | none
    crop_margin: float = 0.015       # keep this much paper around the content (share of the page)
    enhance: str = "clean"           # none | clean | gray | bw
    denoise: int = 0                 # 0 off, 1 light, 2 strong (slow)
    sharpen: bool = False
    brightness: int = 0              # -100 .. 100
    contrast: int = 0                # -100 .. 100
    drop_blank: bool = True          # leave out pages with (almost) nothing on them
    blank_threshold: float = 0.001   # share of the page that must be ink to count as "not blank"
    dpi: int = 200                   # resolution PDF pages are rendered at
    jpeg_quality: int = 90
    keep_text_pages: bool = True     # PDF pages that already have real text are copied untouched
    searchable: bool = False         # run OCR on the finished PDF so it can be searched (needs Tesseract)

    @classmethod
    def from_settings(cls, s, prefix: str = "scan_cleaner.") -> "CleanSpec":
        spec = cls()
        for f in fields(cls):
            default = getattr(spec, f.name)
            key = prefix + f.name
            if isinstance(default, bool):
                v = s.get_bool(key, default)
            elif isinstance(default, int):
                v = s.get_int(key, default)
            elif isinstance(default, float):
                v = s.get_float(key, default)
            else:
                v = s.get(key, default)
            setattr(spec, f.name, v)
        return spec


PRESETS: dict[str, tuple[str, dict]] = {
    "document": ("Document – clean white pages", dict(rotate=True, deskew=True, crop="auto", enhance="clean", denoise=0,
                                                    sharpen=False, drop_blank=True)),
    "bw": ("Black & white – smallest files", dict(rotate=True, deskew=True, crop="auto", enhance="bw", denoise=1,
                                                sharpen=False, drop_blank=True)),
    "photo_id": ("ID / passport / photo – keep colours", dict(rotate=True, deskew=False, crop="auto", enhance="none",
                                                            denoise=0, sharpen=False, drop_blank=False)),
    "faded": ("Faded or old scan", dict(rotate=True, deskew=True, crop="auto", enhance="gray", denoise=1, sharpen=True,
                                      drop_blank=True)),
    "minimal": ("Only turn and straighten", dict(rotate=True, deskew=True, crop="none", enhance="none", denoise=0,
                                                sharpen=False, drop_blank=False)),
}


def apply_preset(spec: CleanSpec, preset_id: str) -> CleanSpec:
    return replace(spec, **PRESETS[preset_id][1]) if preset_id in PRESETS else spec


# --------------------------------------------------------------------------- detection
Box = tuple[float, float, float, float]          # relative left, top, right, bottom (0..1)


@dataclass
class PageAnalysis:
    rot: int = 0                     # clockwise degrees (0 / 90 / 180 / 270) that make the page upright
    skew: float = 0.0                # PIL rotate() angle that straightens the text
    crop: Box | None = None          # content box found (after turning + straightening), None = nothing to trim
    ink: float = 0.0                 # share of the page that is ink
    blank: bool = False
    native: bool = False             # PDF page that already has real text (copied untouched)
    dark_edges: bool = False         # scanner shadow / black borders found

    def issues(self) -> list[str]:
        if self.native:
            return ["has text (kept as is)"]
        out = []
        if self.blank:
            out.append("blank")
        if self.rot:
            out.append(f"turned {self.rot}°")
        if abs(self.skew) >= 0.5:
            out.append(f"tilted {abs(self.skew):.1f}°")
        if self.dark_edges:
            out.append("dark borders")
        elif self.crop and (1 - (self.crop[2] - self.crop[0]) * (self.crop[3] - self.crop[1])) > 0.08:
            out.append("wide margins")
        return out


@dataclass
class PageEdit:
    """Manual choices for one page.  None = automatic."""
    rot: int | None = None           # absolute clockwise rotation to apply
    skew: float | None = None
    crop: str | Box | None = None    # "none", "auto" or a Box
    enhance: str | None = None
    brightness: int | None = None
    contrast: int | None = None
    keep: bool | None = None         # True = never drop as blank, False = always drop

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))


def estimate_background(img: Image.Image) -> tuple[int, int, int]:
    """Median colour of the outer frame of the picture (the 'paper' or the scanner bed)."""
    small = img.convert("RGB").resize((64, 64), Image.Resampling.BOX)
    px = small.load()
    ring = [px[x, y] for x in range(64) for y in (0, 1, 62, 63)] + [px[x, y] for y in range(64) for x in (0, 1, 62, 63)]
    return tuple(sorted(c[i] for c in ring)[len(ring) // 2] for i in range(3))     # type: ignore[return-value]


def _gray_array(img: Image.Image, side: int):
    g = img.convert("L")
    g.thumbnail((side, side))
    return np.asarray(g, dtype=np.float32)


def _trim_dark_edges(a, max_share: float = 0.25) -> tuple[int, int, int, int]:
    """Rows / columns of scanner shadow at each edge of a bright page: (top, bottom, left, right) counts."""
    h, w = a.shape
    paper = float(np.median(a[h // 4:3 * h // 4, w // 4:3 * w // 4]))
    if paper < 140:
        return 0, 0, 0, 0                                      # dark page / photo: nothing to call a shadow
    dark = paper * 0.55

    def run(means, limit):
        n = 0
        while n < limit and means[n] < dark:
            n += 1
        return n

    lim_h, lim_w = int(h * max_share), int(w * max_share)
    rows, cols = a.mean(axis=1), a.mean(axis=0)
    return run(rows, lim_h), run(rows[::-1], lim_h), run(cols, lim_w), run(cols[::-1], lim_w)


def content_box(img: Image.Image, margin: float = 0.015, side: int = 800) -> tuple[Box | None, bool]:
    """(box of the real content as shares of the page, found dark edges).  Box None = nothing worth trimming."""
    if np is None:
        return None, False
    try:
        a = _gray_array(img, side)
        h, w = a.shape
        t, b, l, r = _trim_dark_edges(a)
        dark_edges = max(t, b, l, r) >= 3
        if dark_edges:
            t, b, l, r = (t + 2 if t else 0, b + 2 if b else 0, l + 2 if l else 0, r + 2 if r else 0)
        inner = a[t:h - b, l:w - r]
        if inner.size == 0:
            return None, False
        ih, iw = inner.shape
        k = max(2, int(0.02 * min(ih, iw)))
        ring = np.concatenate([inner[:k].ravel(), inner[-k:].ravel(), inner[:, :k].ravel(), inner[:, -k:].ravel()])
        bg = float(np.median(ring))
        mask = np.abs(inner - bg) > 30
        rows = mask.mean(axis=1) > max(0.004, 2.0 / iw)
        cols = mask.mean(axis=0) > max(0.004, 2.0 / ih)
        if not rows.any() or not cols.any():
            return None, dark_edges
        r0, r1 = int(np.argmax(rows)), int(ih - np.argmax(rows[::-1]))
        c0, c1 = int(np.argmax(cols)), int(iw - np.argmax(cols[::-1]))
        left, top, right, bottom = (l + c0) / w, (t + r0) / h, (l + c1) / w, (t + r1) / h
        left, top, right, bottom = max(0.0, left - margin), max(0.0, top - margin), min(1.0, right + margin), min(1.0, bottom + margin)
        area = (right - left) * (bottom - top)
        if area > 0.97 or area < 0.15:                          # nothing to trim, or the detection is not to be trusted
            return None, dark_edges
        return (left, top, right, bottom), dark_edges
    except Exception:
        log.exception("content_box failed")
        return None, False


def ink_fraction(img: Image.Image, side: int = 600) -> float:
    """Share of the page (without its outer 4 %) that is clearly darker than the paper."""
    if np is None:
        return 1.0
    a = _gray_array(img, side)
    h, w = a.shape
    mh, mw = max(1, int(h * 0.04)), max(1, int(w * 0.04))
    region = a[mh:h - mh, mw:w - mw]
    if region.size == 0:
        return 1.0
    paper = float(np.median(region))
    if float(region.std()) > 45:                                # lots of tonal variation: a picture, not an empty page
        return 1.0
    return float((region < paper - 45).mean())


def _doc_like(img: Image.Image) -> bool:
    """Bright paper with a little ink: worth straightening.  Photos are left alone."""
    if np is None:
        return False
    a = _gray_array(img, 400)
    return float(np.median(a)) > 150 and 0.003 < float((a < np.median(a) - 50).mean()) < 0.4


def analyse_image(img: Image.Image, spec: CleanSpec | None = None, cfg=None, side: int = 1400) -> PageAnalysis:
    """Find out what is wrong with one page.  `cfg` (an ocr.OcrConfig) enables detection of sideways pages."""
    spec = spec or CleanSpec()
    work = img.convert("RGB")
    work.thumbnail((side, side))
    ana = PageAnalysis()
    ana.ink = ink_fraction(work)
    ana.blank = ana.ink < spec.blank_threshold
    if ana.blank:
        return ana
    from . import ocr
    if cfg is not None and getattr(cfg, "enabled", True):
        try:
            ana.rot = ocr.detect_rotation(work, cfg)
        except Exception:
            ana.rot = 0
    work = rotate_cw(work, ana.rot)
    if _doc_like(work):
        # Measure the tilt on the page itself, with the lighting evened out: the dark scanner bed and a shaded
        # side both fool the detector (it returns 0 degrees) when it looks at the raw scan.
        box, _dark = content_box(work, 0.0)
        region = work.crop((round(box[0] * work.width), round(box[1] * work.height),
                            round(box[2] * work.width), round(box[3] * work.height))) if box else work
        ana.skew = ocr.detect_skew(flatten_illumination(region.convert("L")))
        if ana.skew:
            work = work.rotate(ana.skew, expand=True, resample=Image.Resampling.BICUBIC,
                               fillcolor=estimate_background(work))
    ana.crop, ana.dark_edges = content_box(work, spec.crop_margin)
    return ana


# --------------------------------------------------------------------------- fixing
def _otsu(img_l: Image.Image) -> int:
    hist = img_l.histogram()[:256]
    total = sum(hist)
    sum_all = sum(i * h for i, h in enumerate(hist))
    best_t, best_var, w0, sum0 = 128, -1.0, 0, 0.0
    for t in range(256):
        w0 += hist[t]
        if w0 == 0:
            continue
        w1 = total - w0
        if w1 == 0:
            break
        sum0 += t * hist[t]
        m0, m1 = sum0 / w0, (sum_all - sum0) / w1
        var = w0 * w1 * (m0 - m1) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def flatten_illumination(img: Image.Image) -> Image.Image:
    """Even out shadows and yellowed paper: divide by a smooth estimate of the background."""
    if np is None:
        return ImageOps.autocontrast(img, cutoff=1)
    step = max(1, round(max(img.size) / 300))
    small = img.resize((max(8, img.width // step), max(8, img.height // step)), Image.Resampling.BOX)
    bg = small.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(4)).resize(img.size, Image.Resampling.BICUBIC)
    a = np.asarray(img, dtype=np.float32)
    b = np.maximum(np.asarray(bg, dtype=np.float32), 1.0)
    return Image.fromarray(np.clip(a / b * 255.0, 0, 255).astype(np.uint8), img.mode)


def _brightness_contrast(img: Image.Image, brightness: int, contrast: int) -> Image.Image:
    if brightness:
        img = ImageEnhance.Brightness(img).enhance(max(0.0, 1 + brightness / 100))
    if contrast:
        img = ImageEnhance.Contrast(img).enhance(max(0.0, 1 + contrast / 100))
    return img


def enhance_image(img: Image.Image, mode: str, brightness: int = 0, contrast: int = 0) -> Image.Image:
    if mode == "none":
        return _brightness_contrast(img, brightness, contrast)
    if mode in ("gray", "bw"):
        img = img.convert("L")
    img = ImageOps.autocontrast(flatten_illumination(img), cutoff=(1, 0))
    img = _brightness_contrast(img, brightness, contrast)
    if mode == "bw":
        t = _otsu(img)
        img = img.point(lambda p, t=t: 255 if p > t else 0)
    return img


def effective(spec: CleanSpec, ana: PageAnalysis | None, edit: PageEdit | None) -> dict:
    """The values that will really be used for a page (manual wins over detected wins over default)."""
    e = edit or PageEdit()
    rot = e.rot if e.rot is not None else (ana.rot if ana and spec.rotate else 0)
    if e.skew is not None:
        skew = e.skew
    elif ana and spec.deskew and (e.rot is None or (e.rot - ana.rot) % 180 == 0):
        skew = ana.skew
    else:
        skew = 0.0
    crop = e.crop if e.crop is not None else ("auto" if spec.crop == "auto" else "none")
    return {"rot": rot % 360, "skew": skew, "crop": crop,
            "enhance": e.enhance if e.enhance is not None else spec.enhance,
            "brightness": e.brightness if e.brightness is not None else spec.brightness,
            "contrast": e.contrast if e.contrast is not None else spec.contrast}


def geometry(img: Image.Image, spec: CleanSpec, ana: PageAnalysis | None = None, edit: PageEdit | None = None,
             with_crop: bool = True) -> tuple[Image.Image, Box | None]:
    """Turn, straighten and (optionally) crop.  Returns the image and the crop box that applies (even if not cut)."""
    v = effective(spec, ana, edit)
    img = img.convert("RGB")
    if v["rot"]:
        img = rotate_cw(img, v["rot"])
    if v["skew"]:
        img = img.rotate(v["skew"], expand=True, resample=Image.Resampling.BICUBIC, fillcolor=estimate_background(img))
    box: Box | None = None
    if isinstance(v["crop"], tuple):
        box = v["crop"]
    elif v["crop"] == "auto":
        box, _dark = content_box(img, spec.crop_margin)
    if box and with_crop:
        w, h = img.size
        img = img.crop((round(box[0] * w), round(box[1] * h), round(box[2] * w), round(box[3] * h)))
    return img, box


def process_page(img: Image.Image, spec: CleanSpec, ana: PageAnalysis | None = None,
                 edit: PageEdit | None = None) -> Image.Image:
    """Apply the whole pipeline to one page."""
    v = effective(spec, ana, edit)
    img, _box = geometry(img, spec, ana, edit)
    if spec.denoise:
        img = img.filter(ImageFilter.MedianFilter(3 if spec.denoise == 1 else 5))
    img = enhance_image(img, v["enhance"], v["brightness"], v["contrast"])
    if spec.sharpen and img.mode != "1" and v["enhance"] != "bw":
        img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=110, threshold=2))
    return img


def is_binary(img: Image.Image) -> bool:
    return img.mode == "L" and img.getcolors(maxcolors=3) is not None and len(img.getcolors(maxcolors=3)) <= 2


# --------------------------------------------------------------------------- reading pages
def _kind(path) -> str:
    ext = Path(path).suffix.lower()
    return "pdf" if ext == ".pdf" else "image" if ext in IMAGE_EXTS else "other"


def supported(path) -> bool:
    return _kind(path) != "other" and (_kind(path) == "image" or fitz is not None)


def page_total(path: str | Path) -> int:
    kind = _kind(path)
    if kind == "image":
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1)
    if kind == "pdf" and fitz is not None:
        with FITZ_LOCK, fitz.open(path) as doc:
            return len(doc)
    raise CleanError(f"{Path(path).name}: this file type cannot be cleaned")


def load_page(path: str | Path, index: int = 0, dpi: int = 200, max_side: int = 0) -> Image.Image:
    """One page as an RGB picture.  `max_side` > 0 renders it small (for previews and analysis)."""
    kind = _kind(path)
    if kind == "image":
        with Image.open(path) as im:
            frames = getattr(im, "n_frames", 1)
            if frames > 1:
                im.seek(min(index, frames - 1))
                img = open_rgb(im.copy())
            else:
                img = open_rgb(path)
        if max_side:
            img.thumbnail((max_side, max_side))
        return img
    if kind == "pdf" and fitz is not None:
        with FITZ_LOCK, fitz.open(path) as doc:
            if doc.needs_pass:
                raise CleanError(f"{Path(path).name} is password protected")
            pg = doc[index]
            longest_pt = max(pg.rect.width, pg.rect.height, 1.0)
            if max_side:
                zoom = max_side / longest_pt
            else:
                zoom = min(dpi / 72.0, 6000.0 / longest_pt)          # never render a monster
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    raise CleanError(f"{Path(path).name}: this file type cannot be cleaned")


def pdf_page_is_native(path: str | Path, index: int, min_chars: int = 80) -> bool:
    if fitz is None:
        return False
    with FITZ_LOCK, fitz.open(path) as doc:
        return len(doc[index].get_text().strip()) >= min_chars


def analyse_file(path: str | Path, spec: CleanSpec, cfg=None, cancelled: Callable[[], bool] | None = None,
                 progress: Callable[[int, int, str], None] | None = None) -> list[PageAnalysis]:
    """Analyse every page of a file at preview size."""
    n = page_total(path)
    out: list[PageAnalysis] = []
    for i in range(n):
        if cancelled and cancelled():
            break
        if progress:
            progress(i, n, f"{Path(path).name} page {i + 1}")
        if _kind(path) == "pdf" and spec.keep_text_pages and pdf_page_is_native(path, i):
            out.append(PageAnalysis(native=True))
            continue
        out.append(analyse_image(load_page(path, i, max_side=1400), spec, cfg))
    return out


# --------------------------------------------------------------------------- writing
@dataclass
class CleanResult:
    src: str
    out: str
    pages_in: int = 0
    pages_out: int = 0
    dropped: list[int] = field(default_factory=list)
    bytes_in: int = 0
    bytes_out: int = 0
    notes: list[str] = field(default_factory=list)


def output_suffix(src: str | Path, frames: int = 1) -> str:
    ext = Path(src).suffix.lower()
    if ext == ".pdf":
        return ".pdf"
    if frames > 1 or ext in (".tif", ".tiff"):
        return ".tif"
    return ".jpg" if ext in (".jpg", ".jpeg") else ".webp" if ext == ".webp" else ".bmp" if ext == ".bmp" else ".png"


def encode_page(img: Image.Image, quality: int = 90) -> tuple[bytes, str]:
    """(bytes, 'png' | 'jpeg') - 1-bit PNG for black & white pages, JPEG for everything else."""
    buf = io.BytesIO()
    if is_binary(img):
        img.convert("1").save(buf, "PNG", optimize=True)
        return buf.getvalue(), "png"
    img.convert("RGB" if img.mode not in ("L", "RGB") else img.mode).save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue(), "jpeg"


def save_images(frames: list[Image.Image], out_path: str | Path, quality: int = 90) -> None:
    out_path = Path(out_path)
    ext = out_path.suffix.lower()
    first, rest = frames[0], frames[1:]
    if ext in (".tif", ".tiff"):
        binary = all(is_binary(f) for f in frames)
        conv = [f.convert("1") if binary else (f if f.mode in ("L", "RGB") else f.convert("RGB")) for f in frames]
        conv[0].save(out_path, "TIFF", save_all=len(conv) > 1, append_images=conv[1:],
                     compression="group4" if binary else "tiff_adobe_deflate")
    elif ext in (".jpg", ".jpeg"):
        first.convert("RGB" if first.mode not in ("L", "RGB") else first.mode).save(out_path, "JPEG", quality=quality, optimize=True)
    elif ext == ".webp":
        first.convert("RGB").save(out_path, "WEBP", quality=quality)
    elif ext == ".bmp":
        first.convert("RGB").save(out_path, "BMP")
    else:
        (first.convert("1") if is_binary(first) else first).save(out_path, "PNG", optimize=True)
    if rest and ext not in (".tif", ".tiff"):
        raise CleanError("Only TIFF can hold several pages")


def _drop(spec: CleanSpec, ana: PageAnalysis | None, edit: PageEdit | None, total: int) -> bool:
    if edit and edit.keep is not None:
        return not edit.keep
    return bool(spec.drop_blank and ana and ana.blank and total > 1)


def clean_file(src: str | Path, out_path: str | Path, spec: CleanSpec, analyses: list[PageAnalysis] | None = None,
               edits: dict[int, PageEdit] | None = None, cfg=None,
               cancelled: Callable[[], bool] | None = None,
               progress: Callable[[int, int, str], None] | None = None) -> CleanResult:
    """Clean a whole image or PDF into `out_path` (never touches the original)."""
    src, out_path = Path(src), Path(out_path)
    edits = edits or {}
    n = page_total(src)
    res = CleanResult(str(src), str(out_path), pages_in=n, bytes_in=src.stat().st_size)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kind = _kind(src)

    def analysis_for(i: int, img: Image.Image) -> PageAnalysis:
        if analyses and i < len(analyses):
            return analyses[i]
        return analyse_image(img, spec, cfg)

    if kind == "image":
        frames: list[Image.Image] = []
        for i in range(n):
            if cancelled and cancelled():
                raise CleanError("Cancelled")
            if progress:
                progress(i, n, f"{src.name} page {i + 1}")
            img = load_page(src, i)
            ana = analysis_for(i, img)
            if _drop(spec, ana, edits.get(i), n):
                res.dropped.append(i)
                continue
            frames.append(process_page(img, spec, ana, edits.get(i)))
        if not frames:
            raise CleanError("Every page looked blank - nothing was written")
        save_images(frames, out_path, spec.jpeg_quality)
        res.pages_out = len(frames)
    else:
        _clean_pdf(src, out_path, spec, analyses, edits, cfg, cancelled, progress, res, analysis_for)
    res.bytes_out = out_path.stat().st_size
    if res.dropped:
        res.notes.append(f"left out {len(res.dropped)} blank page(s)")
    return res


def _clean_pdf(src: Path, out_path: Path, spec: CleanSpec, analyses, edits, cfg, cancelled, progress,
               res: CleanResult, analysis_for) -> None:
    if fitz is None:
        raise CleanError("PyMuPDF is not installed, so PDFs cannot be cleaned")
    n = res.pages_in
    tmp = out_path.with_name(out_path.name + ".tmp")
    with FITZ_LOCK:
        doc_out = fitz.open()
    try:
        for i in range(n):
            if cancelled and cancelled():
                raise CleanError("Cancelled")
            if progress:
                progress(i, n, f"{src.name} page {i + 1}")
            edit = edits.get(i)
            native = spec.keep_text_pages and (analyses[i].native if analyses and i < len(analyses)
                                              else pdf_page_is_native(src, i))
            if native and (edit is None or edit.keep is not False):
                with FITZ_LOCK, fitz.open(src) as doc_in:
                    doc_out.insert_pdf(doc_in, from_page=i, to_page=i)
                    if edit and edit.rot:
                        pg = doc_out[len(doc_out) - 1]
                        pg.set_rotation((pg.rotation + edit.rot) % 360)
                continue
            img = load_page(src, i, dpi=spec.dpi)
            ana = analysis_for(i, img)
            if _drop(spec, ana, edit, n):
                res.dropped.append(i)
                continue
            clean = process_page(img, spec, ana, edit)
            data, _fmt = encode_page(clean, spec.jpeg_quality)
            with FITZ_LOCK:
                page = doc_out.new_page(width=clean.width * 72.0 / spec.dpi, height=clean.height * 72.0 / spec.dpi)
                page.insert_image(page.rect, stream=data)
        if not len(doc_out):
            raise CleanError("Every page looked blank - nothing was written")
        res.pages_out = len(doc_out)
        with FITZ_LOCK:
            doc_out.save(str(tmp), garbage=3, deflate=True)
    finally:
        with FITZ_LOCK:
            doc_out.close()
    if spec.searchable and cfg is not None:
        from . import pdfops
        try:
            pdfops.make_searchable(tmp, out_path, cfg)
            res.notes.append("made searchable")
            tmp.unlink(missing_ok=True)
            return
        except Exception as exc:
            res.notes.append(f"could not make it searchable ({exc})")
    os.replace(tmp, out_path)


# --------------------------------------------------------------------------- preview helper (used by the editor)
def preview_image(small: Image.Image, spec: CleanSpec, ana: PageAnalysis | None, edit: PageEdit | None,
                  original: bool = False) -> Image.Image:
    """What the page will look like, on an already reduced picture.  `original=True` shows the untouched page."""
    return small.convert("RGB") if original else process_page(small, spec, ana, edit)
