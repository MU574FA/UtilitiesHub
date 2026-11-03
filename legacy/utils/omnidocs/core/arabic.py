"""Bilingual Arabic and Latin Normalization & Text Processing Utilities."""
from __future__ import annotations

import re
import unicodedata

EASTERN_ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
WESTERN_DIGITS = "0123456789"
DIGIT_TRANS = str.maketrans(EASTERN_ARABIC_DIGITS, WESTERN_DIGITS)

# Diacritics, harakat, tatweel
HARAKAT_RE = re.compile(r"[\u064B-\u065F\u0670\u0640]")
LATIN_DIACRITICS_RE = re.compile(r"[\u0300-\u036F]")

# Orthographic mappings for Arabic variants
AR_ALEF_RE = re.compile(r"[أإآٱ]")
ARABIC_RANGE_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def translate_arabic_digits(text: str) -> str:
    """Map Eastern Arabic digits (٠-٩) to Western standard digits (0-9)."""
    if not text:
        return ""
    return str(text).translate(DIGIT_TRANS)


def has_arabic(text: str) -> bool:
    """Return True if text contains any Arabic characters."""
    if not text:
        return False
    return bool(ARABIC_RANGE_RE.search(str(text)))


def normalize_arabic_and_latin(text: str) -> str:
    """
    Exhaustive bilingual normalization engine matching the specification:
    1. Eastern Arabic digits -> Western digits
    2. NFKD decomposition & Latin combining accent removal
    3. Arabic harakat, tashkeel, tatweel & dagger alef stripping
    4. Orthographic unification:
       - [أإآٱ] -> ا
       - ى -> ي
       - ة -> ه
       - ؤ -> و
       - ئ -> ي
    5. Punctuation scrubbing & case folding
    """
    if text is None:
        return ""
    s = str(text)
    
    # 1. Digits
    s = s.translate(DIGIT_TRANS)
    
    # 2. Decompose and strip Latin diacritics
    s = unicodedata.normalize("NFKD", s)
    s = LATIN_DIACRITICS_RE.sub("", s)
    
    # 3. Strip Arabic harakat, tatweel, etc.
    s = HARAKAT_RE.sub("", s)
    
    # 4. Lowercase Latin
    s = s.lower()
    
    # 5. Arabic letter unification
    s = AR_ALEF_RE.sub("ا", s)
    s = s.replace("ى", "ي")
    s = s.replace("ة", "ه")
    s = s.replace("ؤ", "و")
    s = s.replace("ئ", "ي")
    
    # 6. Punctuation scrubbing: keep only letters, numbers, and spaces
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    
    # 7. Collapse whitespace and trim
    return " ".join(s.split())


def prepare_name_tokens(raw: str) -> dict[str, Any]:
    """Helper for fuzzy name matching: returns normalized string, token list, and sorted tokens."""
    norm = normalize_arabic_and_latin(raw)
    tokens = norm.split() if norm else []
    sorted_tokens = " ".join(sorted(tokens))
    return {
        "raw": raw,
        "norm": norm,
        "tokens": tokens,
        "sorted": sorted_tokens,
    }

