"""Find things in OCR / document text: by regular expression or by similarity (typos, word order, Arabic spelling)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .fuzzy import name_similarity
from .textutil import normalise_name


@dataclass
class TextHit:
    pattern: str
    text: str            # what was found
    score: float         # 1.0 for regex hits
    page: int            # 1-based
    line: int            # 1-based, within the page
    context: str = ""    # the whole line


def split_patterns(raw: str) -> list[str]:
    """One pattern / name per line; blank lines ignored."""
    return [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]


def regex_hits(pages: list[str], patterns: list[str], ignore_case: bool = True) -> list[TextHit]:
    """Every match of every pattern.  Raises ValueError('<pattern>: <why>') for an invalid regex."""
    flags = re.I | re.M if ignore_case else re.M
    out: list[TextHit] = []
    for pat in patterns:
        try:
            rx = re.compile(pat, flags)
        except re.error as exc:
            raise ValueError(f"{pat}: {exc}") from exc
        for pno, text in enumerate(pages, 1):
            lines = text.splitlines()
            starts = [0]
            for ln in lines:
                starts.append(starts[-1] + len(ln) + 1)
            for m in rx.finditer(text):
                if not m.group(0):
                    continue
                lno = max(0, sum(1 for s in starts[1:] if s <= m.start()))
                out.append(TextHit(pat, m.group(0), 1.0, pno, lno + 1, lines[lno] if lno < len(lines) else ""))
    return out


def similar_hits(pages: list[str], terms: list[str], threshold: float = 0.8) -> list[TextHit]:
    """Word groups in the text that look like one of `terms`.

    Each line is scanned with windows of (words in term - 1 .. + 1) words; per line and term the best
    non-overlapping windows at or above `threshold` are kept.
    """
    out: list[TextHit] = []
    for term in terms:
        k = max(1, len(term.split()))
        norm_term = normalise_name(term)
        for pno, text in enumerate(pages, 1):
            for lno, line in enumerate(text.splitlines(), 1):
                words = line.split()
                if not words:
                    continue
                cands = []
                for size in sorted({max(1, k - 1), k, k + 1}):
                    for i in range(0, len(words) - size + 1):
                        s = name_similarity(norm_term, normalise_name(" ".join(words[i:i + size])))
                        if s >= threshold:
                            cands.append((s, i, i + size))
                taken: list[tuple[int, int]] = []
                for s, a, b in sorted(cands, key=lambda c: (-c[0], c[2] - c[1])):
                    if all(b <= x or a >= y for x, y in taken):
                        taken.append((a, b))
                        out.append(TextHit(term, " ".join(words[a:b]), s, pno, lno, line))
    out.sort(key=lambda h: (h.page, h.line, -h.score))
    return out


def find_text(pages: list[str], patterns: list[str], mode: str = "regex", threshold: float = 0.8,
              ignore_case: bool = True) -> list[TextHit]:
    return regex_hits(pages, patterns, ignore_case) if mode == "regex" else similar_hits(pages, patterns, threshold)
