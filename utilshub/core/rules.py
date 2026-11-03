"""The rule engine: one small, friendly model for "does this text contain X?".

A `Rule` is a keyword list, a regex, a "Label: value" lookup or a named built-in detector.
Every rule can be switched on/off and has a *necessity level*:

    required     must hit - a miss is a failure (blocks / flags red)
    recommended  should hit - a miss is a warning (flags orange)
    optional     nice to have - a miss is only information

The same rule object drives: document-type recognition and checks (DocPacker),
custom extraction fields (CV Sorter, Screenshot) and the Rule tester in the editors.

Keyword syntax (one per line, or separated by , or ;):
    Python, Java, SQL                    plain words
    Iraqi = iraq, baghdad, basra         "canonical = variants": reports the canonical name
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from dataclasses import asdict, dataclass, field, fields
from typing import Callable

from .fuzzy import FuzzyIndex
from .textutil import normalise, unique

LEVELS = ("required", "recommended", "optional")
LEVEL_LABELS = {"required": "Required", "recommended": "Recommended", "optional": "Optional"}
LEVEL_HELP = {"required": "must be found - a miss is a failure",
              "recommended": "should be found - a miss is a warning",
              "optional": "nice to have - a miss is only information"}
LEVEL_STATUS = {"required": "fail", "recommended": "warn", "optional": "info"}
KINDS = ("keyword", "regex", "label", "builtin")
KIND_LABELS = {"keyword": "Keywords", "regex": "Regex", "label": "After label", "builtin": "Built-in"}
MATCH_MODES = (("word", "Whole word"), ("prefix", "Word starts with"), ("contains", "Contains"))
SCOPES = (("text", "Document text"), ("filename", "File name"))

_DASH = r"[:\-–—]"


# --------------------------------------------------------------------------- built-in detectors
EMAIL_RE = re.compile(r"[\w.%+\-]+@[\w\-]+(?:\.[\w\-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w@])(?:\+|00)?\(?\d[\d ().\-]{6,}\d(?![\w@])")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')]+", re.I)
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)?linkedin\.com/(?:in|pub)/[\w\-%]+/?", re.I)


def _detect_email(text: str) -> list[str]:
    return unique(m.group(0).strip(".").lower() for m in EMAIL_RE.finditer(text))


def _plausible_phone(raw: str) -> bool:
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15:
        return False
    tokens = re.findall(r"\d+", raw)
    if all(len(t) == 4 and t[:2] in ("19", "20") for t in tokens):        # "2018 - 2022"
        return False
    return True


def _detect_phone(text: str) -> list[str]:
    found, seen = [], set()
    for m in PHONE_RE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if _plausible_phone(raw) and digits not in seen:
            seen.add(digits)
            found.append(" ".join(raw.split()).strip(" .-"))
    return found


def _detect_date(text: str) -> list[str]:
    from .dates import find_dates, fmt_date
    return unique(fmt_date(d) for d in find_dates(text))


BUILTIN_DETECTORS: dict[str, tuple[str, Callable[[str], list[str]]]] = {
    "email": ("E-mail address", _detect_email),
    "phone": ("Phone number", _detect_phone),
    "url": ("Web link", lambda t: unique(m.group(0).rstrip(".,;") for m in URL_RE.finditer(t))),
    "iban": ("IBAN", lambda t: unique(" ".join(m.group(0).split()) for m in IBAN_RE.finditer(t))),
    "linkedin": ("LinkedIn profile", lambda t: unique(m.group(0) for m in LINKEDIN_RE.finditer(t))),
    "date": ("Date", _detect_date),
}


# --------------------------------------------------------------------------- keyword parsing
def parse_keywords(pattern: str) -> list[tuple[str, list[str]]]:
    """'a, b' / 'Canon = x, y' -> [(canonical, [variants...]), ...]."""
    out: list[tuple[str, list[str]]] = []
    for line in re.split(r"[\n;]+", pattern or ""):
        line = line.strip()
        if not line:
            continue
        if "=" in line:
            canon, _, rest = line.partition("=")
            canon = canon.strip()
            variants = [v.strip() for v in re.split(r"[,|]", rest) if v.strip()]
            if canon:
                out.append((canon, unique([canon, *variants])))
        else:
            out.extend((w.strip(), [w.strip()]) for w in line.split(",") if w.strip())
    return out


# --------------------------------------------------------------------------- the rule
@dataclass
class Rule:
    label: str = ""
    kind: str = "keyword"          # keyword | regex | label | builtin
    pattern: str = ""
    enabled: bool = True
    level: str = "recommended"     # required | recommended | optional
    weight: float = 1.0            # strength when used for document recognition
    ignore_case: bool = True
    match: str = "word"            # keyword rules: word | prefix | contains
    fuzzy: bool = False            # tolerate typos / OCR errors (keyword rules)
    fuzzy_level: int = 0           # similarity %, 0 = the global default
    compact: bool = False          # ignore all whitespace (for IBANs, MRZ ...)
    scope: str = "text"            # text | filename
    multiple: bool = True          # keep every match (False: first only)
    note: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    # -- (de)serialisation tolerant of old / partial data
    @classmethod
    def from_dict(cls, data: dict) -> "Rule":
        known = {f.name for f in fields(cls)}
        rule = cls(**{k: v for k, v in (data or {}).items() if k in known})
        if rule.level not in LEVELS:
            rule.level = "recommended"
        if rule.kind not in KINDS:
            rule.kind = "keyword"
        return rule

    def to_dict(self) -> dict:
        return asdict(self)

    # -- validation
    def error(self) -> str:
        """Human-readable problem with this rule, or ''."""
        if not self.pattern.strip():
            return "Nothing to look for yet."
        if self.kind == "regex":
            try:
                re.compile(self.pattern)
            except re.error as exc:
                return f"Invalid regex: {exc}"
        if self.kind == "builtin" and self.pattern.strip() not in BUILTIN_DETECTORS:
            return "Unknown built-in detector."
        return ""

    @property
    def status_if_missing(self) -> str:
        return LEVEL_STATUS.get(self.level, "warn")

    def display_name(self) -> str:
        return self.label or (self.pattern.strip().splitlines() or [""])[0][:40] or "(empty rule)"


@dataclass
class RuleResult:
    rule: Rule
    hit: bool
    values: list[str] = field(default_factory=list)
    spans: list[tuple[int, int]] = field(default_factory=list)     # in the original text (not for compact rules)
    error: str = ""

    @property
    def first(self) -> str:
        return self.values[0] if self.values else ""


# --------------------------------------------------------------------------- evaluation
def _flags(rule: Rule) -> int:
    return re.I if rule.ignore_case else 0


def _keyword_regex(word: str, rule: Rule) -> re.Pattern:
    esc = re.escape(word)
    if rule.compact:
        esc = r"\s*".join(re.escape(c) for c in word if not c.isspace())
        return re.compile(esc, _flags(rule))
    if rule.match == "word":
        esc = rf"(?<!\w){esc}(?!\w)" if word[:1].isalnum() or word[:1] == "_" else esc
    elif rule.match == "prefix":
        esc = rf"(?<!\w){esc}"
    return re.compile(esc, _flags(rule))


def evaluate_rule(rule: Rule, text: str, filename: str = "", threshold: float = 0.8,
                  index: FuzzyIndex | None = None) -> RuleResult:
    """Run one rule. Never raises (a broken rule reports `error`)."""
    if rule.scope == "filename":
        haystack = normalise(Path(filename).stem) if filename else ""
    else:
        haystack = text
    err = rule.error()
    if err:
        return RuleResult(rule, False, error=err)
    if not haystack:
        return RuleResult(rule, False)
    thr = (rule.fuzzy_level / 100) if rule.fuzzy_level else threshold
    values: list[str] = []
    spans: list[tuple[int, int]] = []
    try:
        if rule.kind == "keyword":
            for canon, variants in parse_keywords(rule.pattern):
                for variant in variants:
                    m = _keyword_regex(variant, rule).search(haystack)
                    if m:
                        values.append(canon)
                        spans.append(m.span())
                        break
                else:
                    if rule.fuzzy and rule.scope == "text":
                        index = index or FuzzyIndex(haystack)
                        if any(index.find(v, thr) for v in variants):
                            values.append(canon)
        elif rule.kind == "regex":
            body = re.sub(r"\s+", "", haystack).upper() if rule.compact else haystack
            rx = re.compile(rule.pattern, _flags(rule) | re.M)
            for m in rx.finditer(body):
                value = m.group(1) if rx.groups else m.group(0)
                if value and value.strip():
                    values.append(" ".join(value.split()))
                    if not rule.compact:
                        spans.append(m.span(1) if rx.groups and m.group(1) else m.span())
        elif rule.kind == "label":
            labels = "|".join(re.escape(x.strip()) for x in re.split(r"[,;\n]+", rule.pattern) if x.strip())
            rx = re.compile(rf"(?:{labels})\s*{_DASH}\s*([^\n]+)", _flags(rule))
            for m in rx.finditer(haystack):
                value = m.group(1).strip()[:80]
                if value:
                    values.append(value)
                    spans.append(m.span(1))
        elif rule.kind == "builtin":
            detector = BUILTIN_DETECTORS[rule.pattern.strip()][1]
            values = detector(haystack)
            for v in values:
                i = haystack.find(v)
                if i >= 0:
                    spans.append((i, i + len(v)))
    except re.error as exc:
        return RuleResult(rule, False, error=f"Invalid regex: {exc}")
    values = unique(values)
    if not rule.multiple:
        values = values[:1]
    return RuleResult(rule, bool(values), values, spans[:200])


def evaluate_rules(rules: list[Rule], text: str, filename: str = "", threshold: float = 0.8) -> list[RuleResult]:
    index: FuzzyIndex | None = None
    out = []
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.fuzzy and rule.scope == "text" and index is None:
            index = FuzzyIndex(text)
        out.append(evaluate_rule(rule, text, filename, threshold, index))
    return out


# --------------------------------------------------------------------------- document-type scoring
def score_rules(rules: list[Rule], text: str, filename: str = "", needed: float = 1.0,
                threshold: float = 0.8, index: FuzzyIndex | None = None) -> tuple[float, list[RuleResult]]:
    """0..1 'how much does this document look like it was described by `rules`?'

    * a missed *required* rule makes the score 0 (hard gate);
    * recommended rules add `weight`, optional rules add half of it;
    * the total is compared with `needed` (e.g. "2 of the signals").
    """
    results, total, gate_ok = [], 0.0, True
    for rule in rules:
        if not rule.enabled:
            continue
        res = evaluate_rule(rule, text, filename, threshold, index)
        results.append(res)
        if res.error:
            continue
        if not res.hit:
            if rule.level == "required":
                gate_ok = False
            continue
        count = len(res.values) if rule.kind == "keyword" else 1      # every keyword found counts
        total += rule.weight * count * (0.5 if rule.level == "optional" else 1.0)
    if not gate_ok:
        return 0.0, results
    return min(1.0, total / max(0.01, needed)), results


# --------------------------------------------------------------------------- ready-made rules
def _lib(label: str, kind: str, pattern: str, group: str, **kw) -> tuple[str, Rule]:
    return group, Rule(label=label, kind=kind, pattern=pattern, **kw)


RULE_LIBRARY: list[tuple[str, Rule]] = [
    _lib("E-mail address", "builtin", "email", "Contact"),
    _lib("Phone number", "builtin", "phone", "Contact"),
    _lib("Web link", "builtin", "url", "Contact"),
    _lib("LinkedIn profile", "builtin", "linkedin", "Contact", multiple=False),
    _lib("IBAN", "builtin", "iban", "Banking", compact=False),
    _lib("IBAN (OCR tolerant)", "regex", r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", "Banking", compact=True),
    _lib("SWIFT / BIC", "keyword", "swift, bic", "Banking"),
    _lib("Account number label", "regex", r"account\s*(no|number|#)", "Banking"),
    _lib("Any date", "builtin", "date", "Dates"),
    _lib("Date (10-Oct-2026 / 10 October 2026)", "regex", r"\d{1,2}[-/. ]\w{3,9}[-/. ]\d{2,4}", "Dates"),
    _lib("Years of experience", "regex", r"(\d{1,2})\+?\s*(?:years?|yrs?)\b", "CV", multiple=False),
    _lib("Age", "regex", r"\bage\s*[:\-]?\s*(\d{2})\b", "CV", multiple=False),
    _lib("Nationality (label)", "label", "Nationality, Citizenship", "CV", multiple=False),
    _lib("Location (label)", "label", "Location, Current Location, City, Address", "CV", multiple=False),
    _lib("Expected salary (label)", "label", "Expected Salary, Salary Expectation, Salary", "CV", multiple=False),
    _lib("Education level", "keyword", "PhD = phd, doctorate\nMaster = master, msc\nBachelor = bachelor, bsc, b.sc\nDiploma = diploma",
         "CV", multiple=False),
    _lib("Gender", "keyword", "Male, Female", "CV", multiple=False),
    _lib("ID / passport number (label)", "label", "National ID, ID No, ID Number, Passport No", "IDs", multiple=False),
    _lib("Passport MRZ line", "regex", r"P<[A-Z]{3}[A-Z<]{20,}", "IDs", compact=True, ignore_case=False),
    _lib("Passport words", "keyword", "passport, nationality, date of birth, surname, given names", "IDs"),
    _lib("Confidential", "keyword", "confidential", "Letters"),
    _lib("Signature / stamp words", "keyword", "signature, signed, stamp, authorised signatory", "Letters"),
]

REGEX_SNIPPETS: list[tuple[str, str]] = [
    ("Digit", r"\d"), ("Digits (1 or more)", r"\d+"), ("Letter or digit", r"\w"),
    ("Word", r"\w+"), ("Any whitespace", r"\s*"), ("Any text on the line", r".*"),
    ("Optional text", r"(?:...)?"), ("Either / or", r"(?:one|two)"),
    ("Capture group (the value kept)", r"(...)"), ("Word boundary", r"\b"),
    ("Start of line", r"^"), ("End of line", r"$"),
    ("Date dd/mm/yyyy", r"\d{1,2}[/.-]\d{1,2}[/.-]\d{4}"),
    ("Email", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), ("Number with decimals", r"\d+(?:[.,]\d+)?"),
    ("Capital letters run", r"[A-Z]{2,}"),
]
