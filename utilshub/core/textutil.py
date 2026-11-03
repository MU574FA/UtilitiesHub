"""Small text helpers shared by every tool (no GUI, no third-party imports)."""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_AR_MARKS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")  # tashkeel + tatweel


def slugify(text: str) -> str:
    """'Years of Exp.' -> 'years_of_exp' (usable as a {placeholder})."""
    return re.sub(r"\W+", "_", text.strip().lower()).strip("_") or "field"


def split_list(text: str) -> list[str]:
    """Split on commas / semicolons / new lines, dropping blanks."""
    return [p.strip() for p in re.split(r"[,;\n]+", text or "") if p.strip()]


def normalise_ext(ext: str) -> str:
    ext = ext.strip().lower()
    return ext if ext.startswith(".") else f".{ext}"


def parse_ext_list(text: str) -> set[str]:
    return {normalise_ext(e) for e in re.split(r"[,;\s]+", text or "") if e.strip(". ")}


def unique(items: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def fold_accents(text: str) -> str:
    """'José Núñez' -> 'Jose Nunez'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def arabic_fold(text: str) -> str:
    """Make Arabic spelling variants comparable (alef forms, ya/alif-maqsura, ta marbuta, diacritics)."""
    text = text.translate(_AR_DIGITS)
    text = _AR_MARKS.sub("", text)
    for src, dst in (("أإآٱ", "ا"), ("ى", "ي"), ("ة", "ه"), ("ؤ", "و"), ("ئ", "ي")):
        for ch in src:
            text = text.replace(ch, dst)
    return text


def has_arabic(text: str) -> bool:
    return any("؀" <= c <= "ۿ" or "ݐ" <= c <= "ݿ" or "ﭐ" <= c <= "﷿"
               or "ﹰ" <= c <= "﻿" for c in text)


def normalise(text: str) -> str:
    """Lower-case, strip punctuation (keeps + and # so C++ / C# survive)."""
    return " ".join(re.sub(r"[^\w+#]+", " ", text.casefold()).split())


def normalise_name(name: str) -> str:
    """Name key used for similarity: accent-folded, Arabic-folded, lower-case, punctuation-free."""
    return normalise(arabic_fold(fold_accents(name)))


def norm_ascii(text: str) -> str:
    """Letters and digits only, single spaces - used for keyword look-ups in OCR text."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def shingles(text: str, k: int = 3) -> set[str]:
    words = norm_ascii(text).split()
    return {" ".join(words[i:i + k]) for i in range(max(0, len(words) - k + 1))}


def sanitize_filename(name: str, max_len: int = 150) -> str:
    name = _ILLEGAL.sub("", name)
    return " ".join(name.split()).strip(" .")[:max_len].strip(" .")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def parse_page_ranges(text: str, total: int) -> list[int]:
    """'1-3, 5, 8-' -> [0, 1, 2, 4, 7, ...] (0-based, ordered, de-duplicated, clipped to `total`).

    Raises ValueError for text that is not a page list.
    """
    pages: list[int] = []
    for part in re.split(r"[,;\s]+", (text or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*(-|\.\.|:)?\s*(\d*)", part)
        if not m or not (m.group(1) or m.group(3)):
            raise ValueError(f"'{part}' is not a page or range")
        a, dash, b = m.group(1), m.group(2), m.group(3)
        lo = int(a) if a else 1
        hi = int(b) if b else (total if dash else lo)
        if lo > hi:
            lo, hi = hi, lo
        for p in range(max(1, lo), min(total, hi) + 1):
            if p - 1 not in pages:
                pages.append(p - 1)
    return pages


def format_page_ranges(pages: list[int]) -> str:
    """[0,1,2,4] -> '1-3, 5' (input 0-based)."""
    out, run = [], []
    for p in sorted(set(pages)):
        if run and p == run[-1] + 1:
            run.append(p)
            continue
        if run:
            out.append(run)
        run = [p]
    if run:
        out.append(run)
    return ", ".join(f"{r[0] + 1}" if len(r) == 1 else f"{r[0] + 1}-{r[-1] + 1}" for r in out)


def truncate(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"
