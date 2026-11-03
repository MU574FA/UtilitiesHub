"""Similarity ("fuzzy") matching, tolerant of OCR errors, spelling variants and Arabic forms.

Uses RapidFuzz when installed (much faster), otherwise a pure-Python fallback.
Rules that keep false positives low for *keyword* look-ups:
  * terms shorter than MIN_FUZZY_LEN characters (SQL, AWS, Git...) must match exactly;
  * the first letter must match (OCR rarely gets that wrong and it makes search fast);
  * a candidate's length may differ from the term by at most ~25%.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import NamedTuple

from .textutil import normalise, normalise_name

try:  # optional speed-up
    from rapidfuzz.distance import Levenshtein as _Lev

    def lev_ratio(a: str, b: str) -> float:
        """1 - edit distance / longest length  (0..1)."""
        return _Lev.normalized_similarity(a, b)
except ImportError:  # pure python fallback
    def lev_ratio(a: str, b: str) -> float:
        if a == b:
            return 1.0
        n, m = len(a), len(b)
        if not n or not m:
            return 0.0
        prev = list(range(m + 1))
        for i in range(1, n + 1):
            cur = [i] + [0] * m
            ca = a[i - 1]
            for j in range(1, m + 1):
                cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != b[j - 1]))
            prev = cur
        return 1 - prev[m] / max(n, m)

ratio = lev_ratio
MIN_FUZZY_LEN = 5


class Hit(NamedTuple):
    score: float   # 1.0 = exact
    offset: int    # character position in the normalised text (for ordering)


class FuzzyIndex:
    """Pre-processes one text so many terms can be searched in it quickly."""

    def __init__(self, text: str) -> None:
        self.norm = normalise(text)
        self.words = self.norm.split()
        self._padded = f" {self.norm} "
        self._offsets: list[int] = []
        pos = 0
        for word in self.words:
            self._offsets.append(self.norm.index(word, pos))
            pos = self._offsets[-1] + len(word)
        self._windows: dict[int, dict[str, dict[str, int]]] = {}

    def _windows_of(self, size: int) -> dict[str, dict[str, int]]:
        if size not in self._windows:
            groups: dict[str, dict[str, int]] = defaultdict(dict)
            for i in range(len(self.words) - size + 1):
                window = " ".join(self.words[i:i + size])
                groups[window[0]].setdefault(window, self._offsets[i])
            self._windows[size] = groups
        return self._windows[size]

    def find(self, term: str, threshold: float = 0.8, fuzzy: bool = True) -> Hit | None:
        """Best match of `term` scoring >= threshold, or None."""
        wanted = normalise(term)
        if not wanted:
            return None
        pos = self._padded.find(f" {wanted} ")
        if pos >= 0:
            return Hit(1.0, pos)
        if not fuzzy or len(wanted) < MIN_FUZZY_LEN:
            return None
        best: Hit | None = None
        n_words = len(wanted.split())
        max_diff = max(2, len(wanted) // 4)
        for size in sorted({max(1, n_words - 1), n_words, n_words + 1}):
            for window, offset in self._windows_of(size).get(wanted[0], {}).items():
                if abs(len(window) - len(wanted)) > max_diff:
                    continue
                score = ratio(wanted, window)
                if score >= threshold and (best is None or score > best.score):
                    best = Hit(score, offset)
        return best


class KeywordMatcher:
    """Drop-in for a compiled word-list regex: `.search(text)` and truthiness."""

    def __init__(self, words: list[str], fuzzy: bool = False, threshold: float = 0.8) -> None:
        self._words = [w.strip() for w in words if w.strip()]
        parts = sorted((re.escape(w) for w in self._words), key=len, reverse=True)
        self._regex = re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(parts), re.I) if parts else None
        self.fuzzy, self.threshold = fuzzy, threshold

    def __bool__(self) -> bool:
        return self._regex is not None

    def search(self, text: str) -> bool:
        if self._regex is None:
            return False
        if self._regex.search(text):
            return True
        if not self.fuzzy:
            return False
        index = FuzzyIndex(text)
        return any(index.find(word, self.threshold) for word in self._words)


# --------------------------------------------------------------------------- person names
class PreppedName(NamedTuple):
    s: str
    tokens: tuple
    sorted: str


def prep_name(raw: str) -> PreppedName:
    s = normalise_name(raw or "")
    tokens = tuple(s.split())
    return PreppedName(s, tokens, " ".join(sorted(tokens)))


def name_score(a: PreppedName, b: PreppedName) -> float:
    """Similarity of two prepared names, 0..1.

    Combines: whole-string similarity, word-order-insensitive similarity and a
    token-by-token fuzzy match that rewards covering every word of the shorter name.
    """
    if not a.s or not b.s:
        return 0.0
    if a.s == b.s:
        return 1.0
    r1 = lev_ratio(a.s, b.s)
    r2 = 1.0 if a.sorted == b.sorted else lev_ratio(a.sorted, b.sorted)
    small, large = (a.tokens, b.tokens) if len(a.tokens) <= len(b.tokens) else (b.tokens, a.tokens)
    num = den = 0.0
    for tok in small:
        best = 0.0
        for other in large:
            r = lev_ratio(tok, other)
            if r > best:
                best = r
                if r == 1.0:
                    break
        num += best * len(tok)
        den += len(tok)
    tf = num / den if den else 0.0
    cov = len(small) / len(large) if large else 0.0
    return max(r1, r2, tf * (0.85 + 0.15 * cov))


def name_similarity(a: str, b: str) -> float:
    """Similarity of two *normalised* name strings (see textutil.normalise_name), 0..1."""
    return name_score(prep_name_normalised(a), prep_name_normalised(b))


def prep_name_normalised(s: str) -> PreppedName:
    tokens = tuple(s.split())
    return PreppedName(s, tokens, " ".join(sorted(tokens)))
