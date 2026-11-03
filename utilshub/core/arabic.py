"""Arabic (right-to-left, joined letters) text for PDF drawing.

PyMuPDF's built-in fonts have no Arabic glyphs and draw text left-to-right unshaped, which shows up as "? ? ?" or as
disconnected, reversed letters.  `draw_text` picks a system font that has Arabic, joins the letters
(arabic-reshaper) and puts them in visual order (python-bidi), so cover pages and watermarks read correctly.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from .textutil import has_arabic

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:                                   # optional: text is then drawn as it is
    arabic_reshaper = get_display = None

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

_WIN = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
FONT_CANDIDATES = [  # (regular, bold)
    (_WIN / "tahoma.ttf", _WIN / "tahomabd.ttf"), (_WIN / "arial.ttf", _WIN / "arialbd.ttf"),
    (_WIN / "segoeui.ttf", _WIN / "segoeuib.ttf"),
    (Path("/Library/Fonts/Arial Unicode.ttf"), Path("/Library/Fonts/Arial Unicode.ttf")),
    (Path("/System/Library/Fonts/Supplemental/Arial.ttf"), Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")),
    (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
]


def available() -> bool:
    """True when both the shaping libraries and a font are present."""
    return arabic_reshaper is not None and find_font() is not None


def find_font(bold: bool = False) -> str | None:
    for regular, heavy in FONT_CANDIDATES:
        path = heavy if bold and heavy.exists() else regular
        if path.exists():
            return str(path)
    return None


def visual(text: str) -> str:
    """Logical-order text -> what must be drawn left-to-right (joined letters, mixed Arabic/Latin kept right)."""
    if not text or arabic_reshaper is None or not has_arabic(text):
        return text
    return get_display(arabic_reshaper.reshape(text))


@lru_cache(maxsize=4)
def _font(path: str) -> "fitz.Font":
    return fitz.Font(fontfile=path)


def text_width(text: str, size: float, bold: bool = False) -> float:
    font = find_font(bold) if has_arabic(text) else None
    if font:
        return _font(font).text_length(visual(text), fontsize=size)
    return fitz.get_text_length(text, fontname="hebo" if bold else "helv", fontsize=size)


def draw_text(page, x: float, y: float, text: str, size: float = 11, bold: bool = False,
              color: tuple = (0, 0, 0), right: float | None = None, **kw) -> None:
    """Draw one line.  `right` = x of the right edge: the text is right-aligned to it.

    Arabic text is right-aligned to `right` automatically when it is given; without it the text starts at `x`.
    """
    arabic = has_arabic(text)
    font = find_font(bold) if arabic else None
    if arabic and font:
        text = visual(text)
        name = "uhb" if bold else "uhr"
        page.insert_font(fontname=name, fontfile=font)
        if right is not None:
            x = right - _font(font).text_length(text, fontsize=size)
        page.insert_text((x, y), text, fontsize=size, fontname=name, color=color, **kw)
        return
    name = "hebo" if bold else "helv"
    page.insert_text((x, y), text, fontsize=size, fontname=name, color=color, **kw)
