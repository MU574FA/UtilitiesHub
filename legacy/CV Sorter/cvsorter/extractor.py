"""Field extraction: built-in extractors + user-defined regex / keyword fields.

Add a new built-in field by subclassing FieldExtractor and decorating it with
@builtin; then add its key/label to config.BUILTIN_FIELDS.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .config import BUILTIN_FIELDS, CustomField, Settings, split_list
from .fuzzy import FuzzyIndex, KeywordMatcher

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str


@dataclass
class Document:
    text: str
    path: Path
    lines: list[str] = field(init=False)
    _index: FuzzyIndex | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self.text = self.text.replace("\u00a0", " ").replace("\r", "")
        self.lines = [ln.strip() for ln in self.text.splitlines() if ln.strip()]

    @property
    def index(self) -> FuzzyIndex:
        """Shared similarity index of the whole text (built on first use)."""
        if self._index is None:
            self._index = FuzzyIndex(self.text)
        return self._index


# --------------------------------------------------------------------------- helpers
EMAIL_RE = re.compile(r"[\w.%+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w@])(?:\+|00)?\(?\d[\d ().\-]{6,}\d(?![\w@])")
NAME_TOKEN_RE = re.compile(r"[^\W\d_]+(?:['’\-.][^\W\d_]+)*\.?")
TITLE_SEPARATOR_RE = re.compile(r"\s+(?:at|@|[-–—|,])\s+", re.I)


def unique(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def find_emails(text: str) -> list[str]:
    return unique([m.group(0).strip(".").lower() for m in EMAIL_RE.finditer(text)])


def word_regex(words: list[str]) -> re.Pattern | None:
    parts = sorted((re.escape(w.strip()) for w in words if w.strip()), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(parts), re.I) if parts else None


def clean_value(value: str) -> str:
    """Drop trailing date ranges / bullets: 'Google Inc. 2019 - 2022' -> 'Google Inc.'"""
    value = re.sub(r"\s*[(\[]?\s*(?:19|20)\d{2}\b.*$", "", value)
    return " ".join(value.strip(" \t•·▪●*-–—|,;:").split())


def split_title_company(line: str) -> tuple[str, str]:
    """'Senior Dev at Acme' / 'Senior Dev, Acme' -> ('Senior Dev', 'Acme')."""
    parts = TITLE_SEPARATOR_RE.split(line, maxsplit=1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def similarity_threshold(settings: Settings) -> float:
    return min(1.0, max(0.5, settings.fuzzy_threshold / 100))


def title_matcher(settings: Settings) -> KeywordMatcher:
    return KeywordMatcher(settings.job_title_keywords, settings.fuzzy_job_titles,
                          similarity_threshold(settings))


# --------------------------------------------------------------------------- base class
class FieldExtractor(ABC):
    key: str = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def extract(self, doc: Document) -> list[str]:
        """Return the candidate values (best first)."""


_BUILTIN: dict[str, type[FieldExtractor]] = {}


def builtin(cls: type[FieldExtractor]) -> type[FieldExtractor]:
    _BUILTIN[cls.key] = cls
    return cls


# --------------------------------------------------------------------------- built-ins
@builtin
class EmailExtractor(FieldExtractor):
    key = "email"

    def extract(self, doc: Document) -> list[str]:
        return find_emails(doc.text)


@builtin
class PhoneExtractor(FieldExtractor):
    key = "phone"

    @staticmethod
    def _plausible(raw: str) -> bool:
        digits = re.sub(r"\D", "", raw)
        if not 8 <= len(digits) <= 15:
            return False
        tokens = re.findall(r"\d+", raw)
        if all(len(t) == 4 and t[:2] in ("19", "20") for t in tokens):  # "2018 - 2022"
            return False
        return True

    def extract(self, doc: Document) -> list[str]:
        found, seen = [], set()
        for match in PHONE_RE.finditer(doc.text):
            raw = match.group(0)
            digits = re.sub(r"\D", "", raw)
            if self._plausible(raw) and digits not in seen:
                seen.add(digits)
                found.append(" ".join(raw.split()).strip(" .-"))
        return found


@builtin
class NameExtractor(FieldExtractor):
    key = "name"
    LABEL_RE = re.compile(r"^(?:full\s+)?name\s*[:\-–]\s*(.+)$", re.I)

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._stop = {w.lower() for w in settings.name_stopwords}
        self._title_re = title_matcher(settings)
        self._company_re = word_regex(settings.company_suffixes)

    @staticmethod
    def _capitalised(token: str) -> bool:  # caseless scripts (Arabic, CJK) count as OK
        return token[0].isupper() or token[0].lower() == token[0].upper()

    def _is_name(self, line: str) -> bool:
        if len(line) > 40 or any(c.isdigit() or c in "@:/," for c in line):
            return False
        tokens = line.split()
        if not 2 <= len(tokens) <= 4:
            return False
        if any(t.lower().strip(".") in self._stop for t in tokens):
            return False
        if not all(NAME_TOKEN_RE.fullmatch(t) for t in tokens):
            return False
        allowed_lower = 1 if len(tokens) >= 3 else 0  # particles: "van", "de", "bin"
        if sum(not self._capitalised(t) for t in tokens) > allowed_lower:
            return False
        for pattern in (self._title_re, self._company_re):
            if pattern and pattern.search(line):
                return False
        return True

    @staticmethod
    def _tidy(name: str) -> str:
        name = " ".join(name.split())
        return name.title() if name.isupper() else name

    def _guess(self, raw: str) -> str | None:
        words = [w for w in re.sub(r"[_\-.\d]+", " ", raw).split() if w.lower() not in self._stop]
        candidate = " ".join(words).title()
        return candidate if self._is_name(candidate) else None

    def extract(self, doc: Document) -> list[str]:
        for line in doc.lines:  # 1) explicit "Name: ..."
            m = self.LABEL_RE.match(line)
            if m and self._is_name(m.group(1).strip()):
                return [self._tidy(m.group(1))]
        for line in doc.lines[:15]:  # 2) the first name-looking line
            if self._is_name(line):
                return [self._tidy(line)]
        for email in find_emails(doc.text):  # 3) john.smith@x.com
            guess = self._guess(email.split("@")[0])
            if guess:
                return [guess]
        guess = self._guess(doc.path.stem)  # 4) file name
        return [guess] if guess else []


@builtin
class JobTitleExtractor(FieldExtractor):
    key = "job_title"
    LABEL_RE = re.compile(
        r"^(?:job\s+title|current\s+(?:position|title|role)|desired\s+(?:position|title)"
        r"|position|title|role)\s*[:\-–]\s*(.+)$", re.I)

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._title_re = title_matcher(settings)

    def extract(self, doc: Document) -> list[str]:
        for line in doc.lines:
            m = self.LABEL_RE.match(line)
            if m and clean_value(split_title_company(m.group(1))[0]):
                return [clean_value(split_title_company(m.group(1))[0])]
        if not self._title_re:
            return []
        for line in doc.lines:
            if len(line) > 80 or len(line.split()) > 10 or "@" in line or line.endswith("."):
                continue
            if not self._title_re.search(line):
                continue
            title = clean_value(split_title_company(line)[0])
            if title and self._title_re.search(title) and len(title.split()) <= 6:
                return [title]
        return []


@builtin
class CompanyExtractor(FieldExtractor):
    """Heuristics + (optional) fuzzy look-up in the user's list of known companies."""
    key = "company"
    LABEL_RE = re.compile(
        r"^(?:current\s+)?(?:company|employer|organi[sz]ation)\s*[:\-–]\s*(.+)$", re.I)

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._title_re = title_matcher(settings)
        self._suffix_re = word_regex(settings.company_suffixes)
        self._known = unique([c.strip() for c in settings.known_companies if c.strip()])
        self._threshold = similarity_threshold(settings)

    def extract(self, doc: Document) -> list[str]:
        detected = self._snap(self._heuristic(doc))
        known = self._known_in(doc)
        if detected and detected in known:          # detected company is a known one: it leads
            ordered = [detected] + [k for k in known if k != detected]
        else:                                       # trust the curated list first
            ordered = known + ([detected] if detected else [])
        return unique(ordered)

    # -- look-up in the known-company list ---------------------------------
    def _known_in(self, doc: Document) -> list[str]:
        """Known companies found (typos tolerated), in order of appearance."""
        hits = []
        for name in self._known:
            hit = doc.index.find(name, self._threshold)
            if hit:
                hits.append((hit.offset, name))
        return [name for _, name in sorted(hits)]

    def _snap(self, value: str) -> str:
        """'Gogle LLC' -> 'Google' when 'Google' is a known company."""
        if not value or not self.settings.company_snap or not self._known:
            return value
        index = FuzzyIndex(value)
        best_name, best_score = value, 0.0
        for name in self._known:
            hit = index.find(name, self._threshold)
            if hit and hit.score > best_score:
                best_name, best_score = name, hit.score
        return best_name

    # -- heuristics ----------------------------------------------------------
    def _heuristic(self, doc: Document) -> str:
        for line in doc.lines:
            m = self.LABEL_RE.match(line)
            if m and clean_value(m.group(1)):
                return clean_value(m.group(1))
        if self._title_re:  # "Software Engineer at Acme"
            for line in doc.lines:
                if len(line.split()) <= 12 and self._title_re.search(line):
                    company = clean_value(split_title_company(line)[1])
                    if company and re.search(r"[^\W\d_]", company):
                        return company
        if self._suffix_re:  # any short line that looks like "Acme Inc."
            for line in doc.lines:
                if len(line.split()) <= 8 and "@" not in line and self._suffix_re.search(line):
                    company = clean_value(line)
                    if company:
                        return company
        return ""


# --------------------------------------------------------------------------- custom fields
def extract_custom(spec: CustomField, text: str, threshold: float = 0.8,
                   index: FuzzyIndex | None = None) -> list[str]:
    """Run a user-defined field. Raises re.error for an invalid regex."""
    flags = re.I if spec.ignore_case else 0
    found: list[str] = []
    if spec.kind == "keywords":
        keywords = split_list(spec.pattern)
        if spec.fuzzy and spec.ignore_case:        # similarity match: typos / OCR errors
            index = index or FuzzyIndex(text)
            found = [kw for kw in keywords if index.find(kw, threshold)]
        else:                                      # exact whole-word match
            found = [kw for kw in keywords
                     if re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text, flags)]
    elif spec.pattern.strip():
        regex = re.compile(spec.pattern, flags | re.M)
        for m in regex.finditer(text):
            value = m.group(1) if regex.groups else m.group(0)
            if value and value.strip():
                found.append(" ".join(value.split()))
    found = unique(found)
    return found if spec.multiple else found[:1]


class CustomFieldExtractor(FieldExtractor):
    def __init__(self, settings: Settings, spec: CustomField) -> None:
        super().__init__(settings)
        self.spec = spec
        self.key = spec.key

    def extract(self, doc: Document) -> list[str]:
        try:
            return extract_custom(self.spec, doc.text,
                                  similarity_threshold(self.settings), doc.index)
        except re.error as exc:
            log.warning("Invalid regex in custom field %r: %s", self.spec.name, exc)
            return []


# --------------------------------------------------------------------------- orchestration
def field_definitions(settings: Settings) -> list[FieldDef]:
    """Ordered, de-duplicated list of the fields that are active."""
    defs = [FieldDef(k, BUILTIN_FIELDS[k]) for k in BUILTIN_FIELDS if k in settings.builtin_fields]
    keys = {d.key for d in defs}
    for spec in settings.custom_fields:
        if spec.name.strip() and spec.key not in keys:
            keys.add(spec.key)
            defs.append(FieldDef(spec.key, spec.name.strip()))
    return defs


class CVExtractor:
    def __init__(self, settings: Settings) -> None:
        self.extractors: list[FieldExtractor] = [
            _BUILTIN[d.key](settings) for d in field_definitions(settings) if d.key in _BUILTIN
        ]
        builtin_keys = {e.key for e in self.extractors}
        for spec in settings.custom_fields:
            if spec.name.strip() and spec.key not in builtin_keys:
                builtin_keys.add(spec.key)
                self.extractors.append(CustomFieldExtractor(settings, spec))

    def extract(self, text: str, path: Path) -> dict[str, list[str]]:
        doc = Document(text, path)
        result: dict[str, list[str]] = {}
        for extractor in self.extractors:
            try:
                result[extractor.key] = extractor.extract(doc)
            except Exception:  # one bad extractor must never break the rest
                log.exception("Extractor %s failed", extractor.key)
                result[extractor.key] = []
        return result
