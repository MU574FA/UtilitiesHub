"""Field extraction shared by CV Sorter and the Screenshot extractor.

A `FieldSpec` says "look for X and call it <key>".  `kind` picks the detector:

    name email phone job_title company     smart built-ins (see below)
    keywords regex label builtin           rule-based (same engine as DocPacker rules)
    today                                  today's date in `format`
    manual                                 left empty so you type it in

Add a new built-in: subclass FieldExtractor and decorate with @builtin("kind").
Everything is data (dataclasses <-> dicts) so the editors and settings file stay trivial.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Callable

from .fuzzy import FuzzyIndex, KeywordMatcher
from .rules import (EMAIL_RE, LEVELS, Rule, _detect_email, _detect_phone, evaluate_rule)
from .textutil import slugify, unique

log = logging.getLogger(__name__)

BUILTIN_KINDS = {"name": "Full name", "email": "Email", "phone": "Phone",
                 "job_title": "Job title", "company": "Company"}
RULE_KINDS = {"keywords": "Keywords", "regex": "Regex", "label": "After label", "builtin": "Detector"}
OTHER_KINDS = {"today": "Today's date", "manual": "Manual (typed)"}
ALL_KINDS = {**BUILTIN_KINDS, **RULE_KINDS, **OTHER_KINDS}

RESERVED_KEYS = set(BUILTIN_KINDS) | {"date", "time", "original", "filename", "stem", "ext", "year", "month", "day"}


@dataclass
class FieldSpec:
    key: str = ""
    label: str = ""
    kind: str = "regex"
    enabled: bool = True
    level: str = "optional"          # required: empty blocks auto-save; recommended: flagged; optional: quiet
    pattern: str = ""                # keywords / regex / label list / built-in detector name
    ignore_case: bool = True
    match: str = "word"
    fuzzy: bool = False
    multiple: bool = True            # keep every match (True) or only the first
    join: str = ", "                 # how several values are joined in a single cell
    max_len: int = 80                # label rules: cut the value
    format: str = "%d-%b-%Y"         # today
    normalize: str = ""              # phone: "" | digits | intl
    country_code: str = ""           # phone/intl: prefix for numbers that start with 0
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "FieldSpec":
        known = {f.name for f in fields(cls)}
        spec = cls(**{k: v for k, v in (data or {}).items() if k in known})
        if spec.level not in LEVELS:
            spec.level = "optional"
        if not spec.key:
            spec.key = slugify(spec.label or spec.kind)
        if not spec.label:
            spec.label = BUILTIN_KINDS.get(spec.kind, spec.key.replace("_", " ").title())
        return spec

    def to_dict(self) -> dict:
        return asdict(self)

    def rule(self) -> Rule:
        kind = {"keywords": "keyword"}.get(self.kind, self.kind)
        return Rule(label=self.label, kind=kind, pattern=self.pattern, ignore_case=self.ignore_case,
                    match=self.match, fuzzy=self.fuzzy, multiple=self.multiple, level=self.level)

    @property
    def searched(self) -> bool:
        """False for fields that are filled by the program or the user (no red 'not found')."""
        return self.kind not in ("today", "manual")


@dataclass
class ExtractContext:
    """Shared word lists / thresholds the smart built-ins use (global 'Matching' settings)."""
    name_stopwords: list[str] = field(default_factory=list)
    job_title_keywords: list[str] = field(default_factory=list)
    company_suffixes: list[str] = field(default_factory=list)
    known_companies: list[str] = field(default_factory=list)
    fuzzy_threshold: float = 0.8
    fuzzy_job_titles: bool = False
    company_snap: bool = True
    default_country_code: str = ""

    @classmethod
    def from_settings(cls, settings) -> "ExtractContext":
        return cls(settings.get("match.name_stopwords"), settings.get("match.job_title_keywords"),
                   settings.get("match.company_suffixes"), settings.get("match.known_companies"),
                   min(1.0, max(0.5, settings.get_int("match.fuzzy_threshold", 80) / 100)),
                   settings.get_bool("match.fuzzy_job_titles"), settings.get_bool("match.company_snap", True),
                   settings.get("match.default_country_code", ""))


# --------------------------------------------------------------------------- document wrapper
@dataclass
class Document:
    text: str
    path: Path | None = None
    lines: list[str] = field(init=False)
    _index: FuzzyIndex | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self.text = (self.text or "").replace(" ", " ").replace("\r", "")
        self.lines = [ln.strip() for ln in self.text.splitlines() if ln.strip()]

    @property
    def index(self) -> FuzzyIndex:
        if self._index is None:
            self._index = FuzzyIndex(self.text)
        return self._index


# --------------------------------------------------------------------------- helpers
NAME_TOKEN_RE = re.compile(r"[^\W\d_]+(?:['’\-.][^\W\d_]+)*\.?")
TITLE_SEPARATOR_RE = re.compile(r"\s+(?:at|@|[-–—|,])\s+", re.I)


def word_regex(words: list[str]) -> re.Pattern | None:
    parts = sorted((re.escape(w.strip()) for w in words if w.strip()), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(parts), re.I) if parts else None


def clean_value(value: str) -> str:
    """Drop trailing date ranges / bullets: 'Google Inc. 2019 - 2022' -> 'Google Inc.'"""
    value = re.sub(r"\s*[(\[]?\s*(?:19|20)\d{2}\b.*$", "", value)
    return " ".join(value.strip(" \t•·▪●*-–—|,;:").split())


def split_title_company(line: str) -> tuple[str, str]:
    parts = TITLE_SEPARATOR_RE.split(line, maxsplit=1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def normalize_phone(raw: str, mode: str, country_code: str = "") -> str:
    if not mode:
        return " ".join(raw.split())
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if country_code and digits.startswith("0"):
        digits = country_code.lstrip("+") + digits[1:]
    return ("+" + digits) if mode == "intl" and digits and not digits.startswith("+") else digits


# --------------------------------------------------------------------------- extractors
class FieldExtractor(ABC):
    def __init__(self, spec: FieldSpec, ctx: ExtractContext) -> None:
        self.spec, self.ctx = spec, ctx

    @abstractmethod
    def extract(self, doc: Document) -> list[str]:
        """Candidate values, best first."""


_BUILTIN: dict[str, type[FieldExtractor]] = {}


def builtin(kind: str):
    def deco(cls):
        _BUILTIN[kind] = cls
        return cls
    return deco


def title_matcher(ctx: ExtractContext) -> KeywordMatcher:
    return KeywordMatcher(ctx.job_title_keywords, ctx.fuzzy_job_titles, ctx.fuzzy_threshold)


@builtin("email")
class EmailExtractor(FieldExtractor):
    def extract(self, doc: Document) -> list[str]:
        text = re.sub(r"\s*@\s*", "@", doc.text)            # OCR often adds spaces around @
        return _detect_email(text)


@builtin("phone")
class PhoneExtractor(FieldExtractor):
    def extract(self, doc: Document) -> list[str]:
        out = [normalize_phone(p, self.spec.normalize, self.spec.country_code or self.ctx.default_country_code)
               for p in _detect_phone(doc.text)]
        return unique(out)


@builtin("name")
class NameExtractor(FieldExtractor):
    LABEL_RE = re.compile(r"^(?:full\s+)?name\s*[:\-–]\s*(.+)$", re.I)

    def __init__(self, spec, ctx) -> None:
        super().__init__(spec, ctx)
        self._stop = {w.lower() for w in ctx.name_stopwords}
        self._title_re = title_matcher(ctx)
        self._company_re = word_regex(ctx.company_suffixes)

    @staticmethod
    def _capitalised(token: str) -> bool:      # caseless scripts (Arabic, CJK) count as OK
        return token[0].isupper() or token[0].lower() == token[0].upper()

    def is_name(self, line: str) -> bool:
        if len(line) > 40 or any(c.isdigit() or c in "@:/," for c in line):
            return False
        tokens = line.split()
        if not 2 <= len(tokens) <= 4:
            return False
        if any(t.lower().strip(".") in self._stop for t in tokens):
            return False
        if not all(NAME_TOKEN_RE.fullmatch(t) for t in tokens):
            return False
        allowed_lower = 1 if len(tokens) >= 3 else 0         # particles: "van", "de", "bin"
        if sum(not self._capitalised(t) for t in tokens) > allowed_lower:
            return False
        return not any(p and p.search(line) for p in (self._title_re, self._company_re))

    @staticmethod
    def _tidy(name: str) -> str:
        name = " ".join(name.split())
        return name.title() if name.isupper() else name

    def _guess(self, raw: str) -> str | None:
        words = [w for w in re.sub(r"[_\-.\d]+", " ", raw).split() if w.lower() not in self._stop]
        candidate = " ".join(words).title()
        return candidate if self.is_name(candidate) else None

    def extract(self, doc: Document) -> list[str]:
        for line in doc.lines:                                # 1) explicit "Name: ..."
            m = self.LABEL_RE.match(line)
            if m and self.is_name(m.group(1).strip()):
                return [self._tidy(m.group(1))]
        for line in doc.lines[:15]:                           # 2) the first name-looking line
            line = line.strip(" \t-_:|•*")
            if self.is_name(line):
                return [self._tidy(line)]
        for email in _detect_email(doc.text):                 # 3) john.smith@x.com
            guess = self._guess(email.split("@")[0])
            if guess:
                return [guess]
        if doc.path:                                          # 4) file name
            guess = self._guess(doc.path.stem)
            if guess:
                return [guess]
        return []


@builtin("job_title")
class JobTitleExtractor(FieldExtractor):
    LABEL_RE = re.compile(
        r"^(?:job\s+title|current\s+(?:position|title|role)|desired\s+(?:position|title)"
        r"|position|title|role)\s*[:\-–]\s*(.+)$", re.I)

    def __init__(self, spec, ctx) -> None:
        super().__init__(spec, ctx)
        self._title_re = title_matcher(ctx)

    def extract(self, doc: Document) -> list[str]:
        for line in doc.lines:
            m = self.LABEL_RE.match(line)
            if m and clean_value(split_title_company(m.group(1))[0]):
                return [clean_value(split_title_company(m.group(1))[0])]
        if not self._title_re:
            return []
        for line in doc.lines:
            if len(line) > 80 or len(line.split()) > 10 or "@" in line or ". " in line.rstrip(". "):
                continue
            if not self._title_re.search(line):
                continue
            title = clean_value(split_title_company(line)[0])
            if title and self._title_re.search(title) and len(title.split()) <= 6:
                return [title]
        return []


@builtin("company")
class CompanyExtractor(FieldExtractor):
    """Heuristics + (optional) fuzzy look-up in the user's list of known companies."""
    LABEL_RE = re.compile(r"^(?:current\s+)?(?:company|employer|organi[sz]ation)\s*[:\-–]\s*(.+)$", re.I)

    def __init__(self, spec, ctx) -> None:
        super().__init__(spec, ctx)
        self._title_re = title_matcher(ctx)
        self._suffix_re = word_regex(ctx.company_suffixes)
        self._known = unique([c.strip() for c in ctx.known_companies if c.strip()])

    def extract(self, doc: Document) -> list[str]:
        detected = self._snap(self._heuristic(doc))
        known = self._known_in(doc)
        if detected and detected in known:
            ordered = [detected] + [k for k in known if k != detected]
        else:
            ordered = known + ([detected] if detected else [])
        return unique(ordered)

    def _known_in(self, doc: Document) -> list[str]:
        hits = []
        for name in self._known:
            hit = doc.index.find(name, self.ctx.fuzzy_threshold)
            if hit:
                hits.append((hit.offset, name))
        return [name for _, name in sorted(hits)]

    def _snap(self, value: str) -> str:
        if not value or not self.ctx.company_snap or not self._known:
            return value
        index = FuzzyIndex(value)
        best_name, best_score = value, 0.0
        for name in self._known:
            hit = index.find(name, self.ctx.fuzzy_threshold)
            if hit and hit.score > best_score:
                best_name, best_score = name, hit.score
        return best_name

    def _heuristic(self, doc: Document) -> str:
        for line in doc.lines:
            m = self.LABEL_RE.match(line)
            if m and clean_value(m.group(1)):
                return clean_value(m.group(1))
        if self._title_re:
            for line in doc.lines:
                if len(line.split()) <= 12 and self._title_re.search(line):
                    company = clean_value(split_title_company(line)[1])
                    if company and re.search(r"[^\W\d_]", company):
                        return company
        if self._suffix_re:
            for line in doc.lines:
                if len(line.split()) <= 8 and "@" not in line and self._suffix_re.search(line):
                    company = clean_value(line)
                    if company:
                        return company
        return ""


class RuleFieldExtractor(FieldExtractor):
    """keywords / regex / label / builtin detector - all through the shared Rule engine."""

    def extract(self, doc: Document) -> list[str]:
        rule = self.spec.rule()
        res = evaluate_rule(rule, doc.text, "", self.ctx.fuzzy_threshold, doc.index if rule.fuzzy else None)
        if res.error:
            log.warning("Field %r: %s", self.spec.label, res.error)
        values = res.values
        if self.spec.kind == "label":
            values = [v[: self.spec.max_len] for v in values]
        if self.spec.kind == "keywords":                       # drop 'Engineer' inside 'Mechanical Engineer'
            values = [v for v in values if not any(v.lower() != o.lower() and v.lower() in o.lower() for o in values)]
        return values


class TodayExtractor(FieldExtractor):
    def extract(self, doc: Document) -> list[str]:
        try:
            return [datetime.now().strftime(self.spec.format or "%d-%b-%Y")]
        except ValueError:
            return [datetime.now().strftime("%d-%b-%Y")]


class ManualExtractor(FieldExtractor):
    def extract(self, doc: Document) -> list[str]:
        return []


def make_extractor(spec: FieldSpec, ctx: ExtractContext) -> FieldExtractor:
    if spec.kind in _BUILTIN:
        return _BUILTIN[spec.kind](spec, ctx)
    if spec.kind == "today":
        return TodayExtractor(spec, ctx)
    if spec.kind == "manual":
        return ManualExtractor(spec, ctx)
    return RuleFieldExtractor(spec, ctx)


# --------------------------------------------------------------------------- orchestration
class FieldEngine:
    """Run an ordered list of FieldSpecs over a text. One bad field never breaks the others."""

    def __init__(self, specs: list[FieldSpec], ctx: ExtractContext) -> None:
        self.specs = [s for s in specs if s.enabled and s.key]
        self.extractors = [make_extractor(s, ctx) for s in self.specs]

    def extract(self, text: str, path: Path | None = None) -> dict[str, list[str]]:
        doc = Document(text, path)
        out: dict[str, list[str]] = {}
        for ex in self.extractors:
            try:
                values = ex.extract(doc)
            except Exception:
                log.exception("Field %s failed", ex.spec.key)
                values = []
            if not ex.spec.multiple and ex.spec.kind in RULE_KINDS:
                values = values[:1]
            out[ex.spec.key] = values
        return out

    def missing(self, values: dict[str, list[str]], levels: tuple[str, ...] = ("required",)) -> list[FieldSpec]:
        """Searched fields of the given necessity levels that came back empty."""
        return [s for s in self.specs if s.searched and s.level in levels and not values.get(s.key)]


def parse_specs(items: list) -> list[FieldSpec]:
    """Settings value (list of dicts) -> unique-keyed FieldSpecs."""
    specs, seen = [], set()
    for item in items or []:
        spec = item if isinstance(item, FieldSpec) else FieldSpec.from_dict(item)
        if spec.key in seen:
            continue
        seen.add(spec.key)
        specs.append(spec)
    return specs


def join_values(values: list[str], spec: FieldSpec | None) -> str:
    return (spec.join if spec else ", ").join(values) if values else ""


# --------------------------------------------------------------------------- defaults
def default_cv_fields() -> list[dict]:
    return [s.to_dict() for s in [
        FieldSpec("name", "Full name", "name", level="required"),
        FieldSpec("email", "Email", "email", level="recommended"),
        FieldSpec("phone", "Phone", "phone", level="recommended"),
        FieldSpec("job_title", "Job title", "job_title"),
        FieldSpec("company", "Company", "company"),
        FieldSpec("linkedin", "LinkedIn", "builtin", pattern="linkedin", multiple=False, enabled=False),
        FieldSpec("years_of_experience", "Years of experience", "regex",
                  pattern=r"(\d{1,2})\+?\s*(?:years|yrs)", multiple=False),
        FieldSpec("skills", "Skills", "keywords", multiple=True, fuzzy=True,
                  pattern="Python, Java, JavaScript, SQL, C++, Excel, Docker, AWS, Linux, Git"),
    ]]


def default_screenshot_fields() -> list[dict]:
    return [s.to_dict() for s in [
        FieldSpec("date", "Date", "today", enabled=False),
        FieldSpec("name", "Name", "name", enabled=False),
        FieldSpec("title", "Job Title", "keywords", enabled=False, multiple=False,
                  pattern="Machine Operator, HSE Inspector, General Worker, Mechanical Engineer, Store Keeper, "
                          "Electrical Engineer, Electrical Technician"),
        FieldSpec("phone", "Phone", "phone", normalize="digits"),
        FieldSpec("email", "Email", "email"),
        FieldSpec("iban", "IBAN", "builtin", pattern="iban", enabled=False, ignore_case=False),
        FieldSpec("notes", "Notes", "manual", enabled=False),
        FieldSpec("nationality", "Nationality", "keywords", enabled=False, multiple=False,
                  pattern="Iraqi = iraq\nIndian = india\nEgyptian = egypt\nPakistani = pakistan\n"
                          "Bangladeshi = bangladesh\nNepali = nepal"),
        FieldSpec("gender", "Gender", "keywords", enabled=False, multiple=False, pattern="Male, Female"),
        FieldSpec("age", "Age", "regex", enabled=False, multiple=False, pattern=r"\bage\s*[:\-]?\s*(\d{2})\b"),
        FieldSpec("experience", "Experience (yrs)", "regex", enabled=False, multiple=False,
                  pattern=r"(\d{1,2})\+?\s*(?:years?|yrs?)\b"),
        FieldSpec("education", "Education", "keywords", enabled=False, multiple=False,
                  pattern="PhD = phd, doctorate\nMaster = master, msc\nBachelor = bachelor, bsc, b.sc\nDiploma = diploma"),
        FieldSpec("location", "Location", "label", enabled=False, multiple=False,
                  pattern="Location, Current Location, City, Address"),
        FieldSpec("id_number", "ID Number", "label", enabled=False, multiple=False,
                  pattern="National ID, ID No, ID Number, Passport No"),
        FieldSpec("salary", "Expected Salary", "label", enabled=False, multiple=False,
                  pattern="Expected Salary, Salary Expectation, Salary"),
        FieldSpec("linkedin", "LinkedIn", "builtin", enabled=False, multiple=False, pattern="linkedin"),
        FieldSpec("skills", "Skills", "keywords", enabled=False, multiple=True,
                  pattern="AutoCAD, Excel, PLC, Welding, Forklift, Safety, SAP"),
    ]]
