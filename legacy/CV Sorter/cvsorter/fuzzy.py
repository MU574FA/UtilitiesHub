"""Similarity ("fuzzy") matching, tolerant of OCR errors and spelling variants.

Uses RapidFuzz when installed (faster), otherwise falls back to the standard library.
Rules that keep false positives low:
  * terms shorter than MIN_FUZZY_LEN characters (SQL, AWS, Git...) must match exactly;
  * the first letter must match (OCR rarely gets that wrong and it makes search fast);
  * a candidate's length may differ from the term by at most ~25%.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import NamedTuple

try:
    from rapidfuzz.fuzz import ratio as _rapid_ratio

    def ratio(a: str, b: str) -> float:
        return _rapid_ratio(a, b) / 100
except ImportError:  # standard-library fallback
    from difflib import SequenceMatcher

    def ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b, autojunk=False).ratio()

MIN_FUZZY_LEN = 5


def normalise(text: str) -> str:
    """Lower-case, strip punctuation (keeps + and # so C++ / C# survive)."""
    return " ".join(re.sub(r"[^\w+#]+", " ", text.casefold()).split())


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
        """{first letter: {window text: offset}} for windows of `size` words."""
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
def fold_accents(text: str) -> str:
    """'José Núñez' -> 'Jose Nunez'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalise_name(name: str) -> str:
    return normalise(fold_accents(name))


def name_similarity(a: str, b: str) -> float:
    """Similarity of two *normalised* names, 0..1.

    Word order is ignored ("Smith John" = "John Smith") and a name that simply
    has extra words ("John A. Smith" vs "John Smith") scores at least 0.95.
    """
    if a == b:
        return 1.0
    tokens_a, tokens_b = a.split(), b.split()
    score = ratio(" ".join(sorted(tokens_a)), " ".join(sorted(tokens_b)))
    set_a, set_b = set(tokens_a), set(tokens_b)
    if min(len(set_a), len(set_b)) >= 2 and (set_a <= set_b or set_b <= set_a):
        score = max(score, 0.95)
    return score
