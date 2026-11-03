"""Clipboard Monitoring & Spreadsheet Row Formatting Engine."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from PIL import Image, ImageGrab

try:
    import pyperclip
except Exception:
    pyperclip = None

log = logging.getLogger(__name__)


def get_clipboard_image() -> Image.Image | None:
    """Retrieves image bitmap or copied image file from system clipboard."""
    try:
        grabbed = ImageGrab.grabclipboard()
        if isinstance(grabbed, Image.Image):
            return grabbed
        if isinstance(grabbed, list) and grabbed:
            # Copied file paths from explorer
            from pathlib import Path
            p = Path(grabbed[0])
            if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"):
                return Image.open(p)
    except Exception as exc:
        log.debug("Clipboard grab error: %s", exc)
    return None


def compute_image_hash(img: Image.Image) -> str:
    """Generates MD5 hash from raw image bytes for deduplication."""
    try:
        return hashlib.md5(img.tobytes()).hexdigest()
    except Exception:
        return ""


def copy_to_clipboard(text: str) -> bool:
    """Copies text to the system clipboard."""
    if pyperclip is not None:
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            pass
    return False


def format_spreadsheet_row(values: list[str]) -> str:
    """Concatenates cell values with tab characters (\\t), collapsing internal newlines."""
    cleaned = []
    for v in values:
        s = str(v).replace("\r", " ").replace("\n", " ").replace("\t", " ")
        cleaned.append(" ".join(s.split()))
    return "\t".join(cleaned)

