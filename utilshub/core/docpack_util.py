"""DocPacker helpers without any Tk: custom field keys, per-part page info, PDF compression, cover page drawing."""
from __future__ import annotations

import os
import re
from dataclasses import replace

from . import arabic
from .pdfops import COMPRESSION_LEVELS, fitz
from .readers import DocInfo
from .templates import render, render_line

_log = __import__("logging").getLogger(__name__)


def custom_keys(labels: list[str]) -> dict[str, str]:
    """label -> placeholder name for the user's custom fields ('Passport no.' -> passport_no)."""
    out: dict[str, str] = {}
    for i, label in enumerate(labels, 1):
        key = re.sub(r"[^a-z0-9]+", "_", label.encode("ascii", "ignore").decode().lower()).strip("_")
        if not key or key[0].isdigit() or key in out.values():
            key = f"field{i}"
        out[label] = key
    return out


def page_info(info: DocInfo, pages: list[int]) -> DocInfo:
    """`info` restricted to some (0-based) pages: what recognition and checks see for one part of a file."""
    pick = lambda seq: [seq[p] for p in pages if p < len(seq)]          # noqa: E731
    texts = pick(info.page_texts)
    return replace(info, text="\n".join(texts) if info.page_texts else info.text, pages=len(pages), page_texts=texts,
                   page_meta=pick(info.page_meta), page_rot=pick(info.page_rot), hash="")


def compress_at_least(path: str, level: int, target_bytes: int = 0) -> int:
    """Compress `path` in place, starting at `level` and going stronger until `target_bytes` is met. Returns the size."""
    size = os.path.getsize(path)
    tmp = path + ".tmp.pdf"
    for lvl in range(max(0, min(level, len(COMPRESSION_LEVELS) - 1)), len(COMPRESSION_LEVELS)):
        q, _maxpx, dpi = COMPRESSION_LEVELS[lvl]
        with fitz.open(path) as doc:
            if dpi and hasattr(doc, "rewrite_images"):
                try:
                    doc.rewrite_images(dpi_threshold=dpi + 40, dpi_target=dpi, quality=q or 90)
                except Exception:
                    _log.debug("ignored error", exc_info=True)
            doc.save(tmp, garbage=4, deflate=True, clean=True)
        new = os.path.getsize(tmp)
        if new <= size:
            os.replace(tmp, path)
            size = new
        else:
            os.remove(tmp)
        if target_bytes <= 0 or size <= target_bytes:
            break
    return size


def draw_cover(page, vals: dict[str, str], cfg: dict, rows: list[tuple[str, str]], starts: dict[int, int],
               missing: list[str]) -> None:
    """Draw the cover page the user designed in the settings (title, free lines, checklist, missing documents).

    Arabic text is shaped and right-aligned; everything else starts at the left margin.
    """
    right, y = page.rect.width - 50, 70
    try:
        title = render(cfg.get("title") or "", vals).strip()
    except ValueError:
        title = vals.get("name", "")
    if title:
        arabic.draw_text(page, 50, y, title, 18, bold=True, right=right)
        y += 24
    for tpl in cfg.get("lines") or []:
        try:
            line = render_line(str(tpl), vals)
        except ValueError:
            continue
        if line:
            arabic.draw_text(page, 50, y, line, 11, color=(0.3, 0.3, 0.3), right=right)
            y += 18
    y += 16
    if cfg.get("checklist", True):
        for i, (tname, worst) in enumerate(rows):
            mark = {"fail": "[X]", "warn": "[!]", "manual": "[ ]"}.get(worst, "[v]")
            page.insert_text((50, y), mark, fontsize=11, fontname="helv")
            arabic.draw_text(page, 80, y, tname[:70], 11)
            if cfg.get("refs", True) and i in starts:
                page.insert_text((470, y), f"p. {starts[i]}", fontsize=11, fontname="helv")
            y += 18
            if y > 780:
                break
    if cfg.get("missing", True):
        for tname in missing:
            page.insert_text((50, y + 6), "[X]", fontsize=11, fontname="hebo", color=(0.75, 0, 0))
            arabic.draw_text(page, 80, y + 6, f"MISSING: {tname}"[:80], 11, bold=True, color=(0.75, 0, 0))
            y += 18
