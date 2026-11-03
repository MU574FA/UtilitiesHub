"""Optical Character Recognition (OCR), Computer Vision & Image Enhancement Engine."""
from __future__ import annotations

import logging
import math
import os
import re
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps

try:
    import pytesseract
except Exception:
    pytesseract = None

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None

log = logging.getLogger(__name__)


def setup_tesseract(custom_path: str = "") -> bool:
    """Configures pytesseract with binary executable path."""
    if pytesseract is None:
        return False
    
    candidates = [
        custom_path,
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.environ.get("TESSERACT_PATH", ""),
        "tesseract",
    ]
    for p in candidates:
        if p and (os.path.isfile(p) or p == "tesseract"):
            pytesseract.pytesseract.tesseract_cmd = p
            try:
                ver = pytesseract.get_tesseract_version()
                log.info("Tesseract OCR initialized successfully: %s (v%s)", p, ver)
                return True
            except Exception:
                continue
    return False


def preprocess_image(img: Image.Image, auto_upscale: bool = True, contrast_boost: bool = True) -> Image.Image:
    """
    Standardizes image for OCR:
    - Grayscale conversion
    - Lanczos 2x upscaling for small images (<1600px width)
    - Autocontrast optimization
    """
    res = img.convert("L")
    if auto_upscale and res.width < 1600:
        res = res.resize((res.width * 2, res.height * 2), Image.Resampling.LANCZOS)
    if contrast_boost:
        try:
            res = ImageOps.autocontrast(res, cutoff=1)
        except Exception:
            pass
    return res


def detect_and_fix_orientation(img: Image.Image) -> tuple[Image.Image, int]:
    """
    Leverages Tesseract OSD (Orientation and Script Detection)
    to detect 90°, 180°, or 270° orientation and rotate upright.
    """
    if pytesseract is None:
        return img, 0
    try:
        osd = pytesseract.image_to_osd(img)
        rot_match = re.search(r"Rotate:\s*(\d+)", osd)
        if rot_match:
            angle = int(rot_match.group(1))
            if angle in (90, 180, 270):
                # Rotate image clockwise
                rotated = img.rotate(360 - angle, expand=True)
                return rotated, angle
    except Exception as exc:
        log.debug("Orientation detection skipped or failed: %s", exc)
    return img, 0


def detect_and_fix_skew(img: Image.Image) -> tuple[Image.Image, float]:
    """
    Straightens tilted documents using OpenCV Hough Line Transform.
    """
    if cv2 is None or np is None:
        return img, 0.0
    try:
        arr = np.array(img.convert("L"))
        edges = cv2.Canny(arr, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 100, minLineLength=arr.shape[1] // 4, maxLineGap=20)
        
        if lines is not None and len(lines) > 0:
            angles = []
            for line in lines:
                x1, y1, x2, y2 = line[0]
                dx = x2 - x1
                dy = y2 - y1
                if abs(dx) > abs(dy):
                    angle = math.degrees(math.atan2(dy, dx))
                    if abs(angle) < 45:
                        angles.append(angle)
            if angles:
                median_angle = float(np.median(angles))
                if abs(median_angle) > 0.5:
                    # Straighten
                    (h, w) = arr.shape[:2]
                    center = (w // 2, h // 2)
                    M = cv2.getRotationMatrix2D(center, median_angle, 1.0)
                    rotated = cv2.warpAffine(arr, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
                    return Image.fromarray(rotated), median_angle
    except Exception as exc:
        log.debug("Deskew skipped or failed: %s", exc)
    return img, 0.0


def ocr_image(img: Image.Image, lang: str = "eng", psm: int = 3) -> str:
    """Run Tesseract OCR on a PIL Image."""
    if pytesseract is None:
        return ""
    try:
        config = f"--psm {psm}"
        return pytesseract.image_to_string(img, lang=lang, config=config)
    except Exception as exc:
        log.warning("OCR failed: %s", exc)
        return ""


def ocr_mrz_region(img: Image.Image, lang: str = "eng") -> str:
    """
    Dedicated OCR for Passport / ID Machine Readable Zone (MRZ):
    Crops the bottom 25%, binarizes for contrast, and runs OCR with PSM 6.
    """
    if pytesseract is None:
        return ""
    try:
        w, h = img.size
        mrz_crop = img.crop((0, int(h * 0.72), w, h))
        prep = preprocess_image(mrz_crop, auto_upscale=True, contrast_boost=True)
        config = "--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<"
        return pytesseract.image_to_string(prep, lang=lang, config=config)
    except Exception as exc:
        log.debug("MRZ OCR failed: %s", exc)
        return ""


def repair_email_whitespace(text: str) -> str:
    """Repairs erroneous spaces inserted around '@' by OCR engines."""
    if not text:
        return ""
    # "john . doe @ gmail . com" -> "john.doe@gmail.com"
    return re.sub(r"([\w.+-]+)\s*@\s*([\w-]+(?:\s*\.\s*[\w-]+)+)",
                  lambda m: m.group(1).replace(" ", "") + "@" + m.group(2).replace(" ", ""),
                  text)


def detect_faces(img: Image.Image) -> list[tuple[int, int, int, int]]:
    """
    Executes OpenCV Haar Cascade facial detection.
    Returns list of (x, y, w, h) bounding boxes.
    """
    if cv2 is None or np is None:
        return []
    try:
        arr = np.array(img.convert("L"))
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        face_cascade = cv2.CascadeClassifier(cascade_path)
        faces = face_cascade.detectMultiScale(arr, scaleFactor=1.1, minNeighbors=4, minSize=(60, 60))
        return [tuple(f) for f in faces]
    except Exception as exc:
        log.debug("Face detection error: %s", exc)
        return []


def has_exif_camera(img: Image.Image) -> bool:
    """
    Examines EXIF metadata to determine if the image originates from a smartphone camera.
    Returns True if camera make/model or typical camera software tags are present.
    """
    try:
        exif = img.getexif()
        if not exif:
            return False
        
        tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
        make = str(tags.get("Make", "")).lower()
        model = str(tags.get("Model", "")).lower()
        software = str(tags.get("Software", "")).lower()
        
        camera_brands = ["apple", "samsung", "huawei", "xiaomi", "oppo", "vivo", "google", "sony", "oneplus"]
        if any(b in make or b in model for b in camera_brands):
            return True
        if "camera" in software or "ios" in software:
            return True
    except Exception:
        pass
    return False

