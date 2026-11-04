"""Image helpers: robust opening, rotation fixes, JPEG encoding."""
from __future__ import annotations

import io
import threading
from pathlib import Path

from PIL import Image, ImageOps

# MuPDF is not thread safe: every module that touches PyMuPDF from a worker thread takes this lock.
FITZ_LOCK = threading.RLock()

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}


def open_rgb(source) -> Image.Image:
    """Open an image (path, bytes or an open PIL image), apply EXIF rotation, release the file handle, return an RGB copy."""
    if isinstance(source, Image.Image):                       # an already open picture (e.g. one TIFF frame)
        img = ImageOps.exif_transpose(source)
        img.load()
        return img.convert("RGB")
    src = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    with Image.open(src) as im:
        img = ImageOps.exif_transpose(im)
        img.load()
        return img.convert("RGB")


def rotate_cw(img: Image.Image, degrees: int) -> Image.Image:
    degrees %= 360
    if not degrees:
        return img
    return img.rotate(-degrees, expand=True, fillcolor=(255, 255, 255))


def apply_fix(img: Image.Image, fix: dict | None) -> Image.Image:
    """Apply a stored {"rot": clockwise degrees, "skew": degrees} correction."""
    if not fix:
        return img
    if fix.get("rot"):
        img = rotate_cw(img, fix["rot"])
    if fix.get("skew"):
        img = img.rotate(fix["skew"], expand=True, resample=Image.BICUBIC, fillcolor=(255, 255, 255))
    return img


def has_camera_exif(path) -> bool:
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            return any(ex.get(k) for k in (271, 272))        # Make / Model
    except Exception:
        return False


def to_jpeg_bytes(img: Image.Image, quality: int = 90, max_px: int | None = None) -> bytes:
    img = img.convert("RGB")
    if max_px:
        img = img.copy()
        img.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def fit_within(img: Image.Image, width: int, height: int | None = None) -> Image.Image:
    height = height or 10 ** 6
    scale = min(width / img.width, height / img.height, 1.0)
    if scale >= 1.0:
        return img
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                      Image.Resampling.LANCZOS)


def image_size(path) -> tuple[int, int]:
    try:
        with Image.open(path) as im:
            return ImageOps.exif_transpose(im).size
    except Exception:
        return (0, 0)


def is_image(path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS
