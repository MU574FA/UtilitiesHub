"""Document types ("requirements") and checks - the brain of DocPacker.

A `DocType` describes one document you expect in a package (passport, bank letter ...):
  signals  - rules that recognise it (toggle each, set necessity + weight)
  checks   - things to verify once a file is assigned (each with a necessity level)

Statuses of a check:  ok | fail | warn | info | skip | manual | manual_ok
Recognition and some checks can use the local AI (core/aidocs.py): `DocType.use_ai` / `ai_hint` feed the AI's
recognition, the "AI question" check type asks the model a yes/no question, and the name checks honour `ai.names_mode`.
With AI off everything behaves exactly as before.

A check's *level* decides how a miss is reported:
  required -> fail,  recommended -> warn,  optional -> info  (info never blocks a package)
"""
from __future__ import annotations

import datetime as dt
import difflib
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from .dates import add_months, find_dates, fmt_date
from .fuzzy import FuzzyIndex
from .imaging import open_rgb
from .mrz import extract_surname, parse_mrz, passport_expiry_date
from .readers import DocInfo
from .rules import LEVEL_STATUS, Rule, evaluate_rule, score_rules
from .textutil import norm_ascii

LEVEL_TO_STATUS = LEVEL_STATUS        # required->fail recommended->warn optional->info


# --------------------------------------------------------------------------- data
@dataclass
class Check:
    label: str = ""
    type: str = "regex"
    arg: str = ""
    level: str = "required"
    enabled: bool = True
    compact: bool = False
    ignore_case: bool = True
    use_image: bool = False          # AI question: let the model look at the first page's picture (signature, stamp ...)

    @classmethod
    def from_dict(cls, data: dict) -> "Check":
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in (data or {}).items() if k in known}
        # old config used level "fail"/"warn"
        d["level"] = {"fail": "required", "warn": "recommended", "info": "optional"}.get(d.get("level"), d.get("level", "required"))
        return cls(**d)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def status_if_failed(self) -> str:
        return LEVEL_TO_STATUS.get(self.level, "fail")


@dataclass
class DocType:
    id: str = ""
    name: str = ""
    enabled: bool = True
    required: bool = True
    kinds: list[str] = field(default_factory=lambda: ["pdf", "image"])
    min_score: float = 0.35          # how confident the match must be (strictness)
    min_signals: float = 1.0         # signal weight that counts as "fully recognised"
    low_text_bonus: bool = False     # image with almost no text (a photo) gets a bonus
    signals: list[Rule] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    note: str = ""
    use_ai: bool = True              # may the AI assign files / pages to this type (when AI recognition is on)?
    ai_hint: str = ""                # plain-words description for the AI, e.g. "letter from a bank confirming an IBAN"

    @classmethod
    def from_dict(cls, data: dict) -> "DocType":
        if "any" in data or "regex" in data or "hints" in data:             # DocPacker v1-3 format
            return from_legacy(data)
        d = {k: v for k, v in data.items() if k in {f.name for f in fields(cls)} - {"signals", "checks"}}
        dt_ = cls(**d)
        dt_.signals = [Rule.from_dict(x) for x in data.get("signals", [])]
        dt_.checks = [Check.from_dict(x) for x in data.get("checks", [])]
        if not dt_.id:
            dt_.id = re.sub(r"\W+", "_", dt_.name.lower()).strip("_") or "doc"
        return dt_

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in ("signals", "checks")}
        d["signals"] = [r.to_dict() for r in self.signals]
        d["checks"] = [c.to_dict() for c in self.checks]
        return d


def from_legacy(old: dict) -> DocType:
    """Convert a DocPacker 1.x requirement dict into the rule-based model."""
    signals: list[Rule] = []
    if old.get("any"):
        signals.append(Rule("Keywords", "keyword", ", ".join(old["any"]), level="recommended", match="contains",
                            compact=False))
    for i, rx in enumerate(old.get("regex") or []):
        signals.append(Rule(f"Pattern {i + 1}", "regex", rx, level="recommended", ignore_case=False))
    for w in old.get("all") or []:
        signals.append(Rule(w, "keyword", w, level="required", match="contains"))
    if old.get("hints"):
        signals.append(Rule("File name hints", "keyword", ", ".join(old["hints"]), level="recommended",
                            scope="filename", match="prefix", weight=0.4))
    checks = []
    for c in old.get("checks") or []:
        checks.append(Check.from_dict(c))
    return DocType(old.get("id") or re.sub(r"\W+", "_", old.get("name", "doc").lower()), old.get("name", ""),
                   old.get("enabled", True), old.get("required", True), list(old.get("kinds") or ["pdf", "image"]),
                   float(old.get("min_score", 0.35)), float(old.get("any_min", 1)),
                   bool(old.get("low_text_bonus", False)), signals, checks)


# --------------------------------------------------------------------------- scoring
def score_doctype(dtype: DocType, info: DocInfo, filename: str = "", threshold: float = 0.8,
                  index: FuzzyIndex | None = None) -> float:
    """0..1 confidence that `info` is a `dtype` document."""
    if dtype.kinds and info.kind not in dtype.kinds:
        return 0.0
    text_rules = [r for r in dtype.signals if r.scope == "text"]
    name_rules = [r for r in dtype.signals if r.scope == "filename" and r.enabled]
    if any(r.enabled for r in text_rules):
        if index is None and any(r.fuzzy for r in text_rules):
            index = FuzzyIndex(info.text)
        sc, _ = score_rules(text_rules, info.text, "", dtype.min_signals, threshold, index)
    else:
        sc = 0.0
    for rule in name_rules:
        res = evaluate_rule(rule, "", filename, threshold)
        if res.hit:
            sc += rule.weight * (0.5 if rule.level == "optional" else 1.0)
        elif rule.level == "required":
            return 0.0
    if dtype.low_text_bonus and info.kind == "image" and len(info.text.strip()) < 80:
        sc += 0.5
    return min(1.0, sc)


# --------------------------------------------------------------------------- checks
@dataclass
class CheckContext:
    """What checks need to know besides the document itself."""
    name: str = ""
    surname: str = ""
    joining: dt.date | None = None
    fuzzy_name: bool = True
    sample_terms: list[str] = field(default_factory=list)
    datecheck: bool = True
    mrz: bool = True
    ai: Any = None                   # core.aidocs.AiContext (None = Rules only)


def same_name(a: str, b: str, fuzzy: bool = True) -> bool:
    a, b = norm_ascii(a), norm_ascii(b)
    return bool(a and b) and (a == b or (fuzzy and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85))


def name_in_text(name: str, text: str, fuzzy: bool) -> bool | None:
    toks = [t for t in norm_ascii(name).split() if len(t) > 1]
    if not toks:
        return None
    nt = norm_ascii(text)
    words = set(nt.split())
    for t in toks:
        if t in nt:
            continue
        if fuzzy and difflib.get_close_matches(t, words, n=1, cutoff=0.85):
            continue
        return False
    return True


Result = tuple[str, str]        # (status, detail)


@dataclass(frozen=True)
class CheckType:
    label: str
    help: str
    arg_label: str = ""
    arg_default: str = ""
    func: Callable[[Check, DocInfo, CheckContext], Result] | None = None
    manual: bool = False
    uses_rule_options: bool = False


CHECK_TYPES: dict[str, CheckType] = {}


def check_type(key: str, label: str, help: str = "", arg_label: str = "", arg_default: str = "",
               manual: bool = False, uses_rule_options: bool = False):
    def deco(fn):
        CHECK_TYPES[key] = CheckType(label, help, arg_label, arg_default, fn, manual, uses_rule_options)
        return fn
    return deco


def _miss(ch: Check, detail: str) -> Result:
    return ch.status_if_failed, detail


@check_type("regex", "Text matches a pattern (regex)", "The document text must match the regular expression.",
            "Regex", r"", uses_rule_options=True)
def _c_regex(ch, info, ctx):
    txt = re.sub(r"\s+", "", info.text).upper() if ch.compact else info.text
    try:
        ok = bool(re.search(ch.arg, txt, re.I if ch.ignore_case else 0))
    except re.error as e:
        return "warn", f"bad regex: {e}"
    return ("ok", "") if ok else _miss(ch, "not found in text")


@check_type("keywords", "Text contains words", "At least one of the listed words must appear.",
            "Words (comma separated)", "", uses_rule_options=True)
def _c_keywords(ch, info, ctx):
    words = [w.strip() for w in re.split(r"[,;\n]+", ch.arg) if w.strip()]
    nt = norm_ascii(info.text)
    compact = nt.replace(" ", "")
    hit = [w for w in words if norm_ascii(w) in nt or norm_ascii(w).replace(" ", "") in compact]
    return (("ok", ", ".join(hit)) if hit else _miss(ch, "none of the words found")) if words else ("skip", "no words set")


@check_type("manual", "Manual confirmation (double-click)", "You tick this yourself (signature, stamp, logo ...).",
            manual=True)
def _c_manual(ch, info, ctx):
    return "manual", ""


@check_type("ai_question", "AI question (yes / no) - local Ollama",
            "The local AI answers a yes/no question about the document (e.g. 'Is the letter signed and stamped?'). "
            "Settings > AI > 'AI questions in checklists' decides whether the AI answers, you confirm it yourself, "
            "or both. Tick 'Let the AI look at the picture' for signatures, stamps and logos (needs a vision model).",
            "Question", "Is the letter signed and stamped?")
def _c_ai_question(ch, info, ctx):
    from .aidocs import run_ai_check
    return run_ai_check(ch, info, ctx)


@check_type("has_text", "Readable text present", "The document must contain readable text (OCR or text layer).",
            "Minimum characters", "40")
def _c_has_text(ch, info, ctx):
    n = len(info.text.strip())
    return ("ok", f"{n} characters") if n >= int(ch.arg or 40) else _miss(ch, f"only {n} characters of text")


@check_type("surname_filled", "Surname is filled in on the form", "")
def _c_surname_filled(ch, info, ctx):
    return ("ok", ctx.surname) if ctx.surname.strip() else _miss(ch, "surname is empty (fill it in or check the passport MRZ)")


@check_type("mrz_valid", "Passport MRZ check digits are valid", "Validates all ICAO check digits of the MRZ.")
def _c_mrz(ch, info, ctx):
    if not ctx.mrz:
        return "skip", "switched off in Settings"
    mz = parse_mrz(info.text)
    if not mz:
        return _miss(ch, "MRZ not found")
    if not mz["has_line2"]:
        return _miss(ch, "MRZ second line not readable")
    bad = [n for n, ok in mz["checks"] if not ok]
    return ("ok", "all check digits valid") if not bad else \
        _miss(ch, "check digit wrong: " + ", ".join(bad) + " (OCR misread or altered passport)")


@check_type("passport_expiry", "Passport valid N months after joining", "Needs the joining date.",
            "Months", "6")
def _c_expiry(ch, info, ctx):
    if not ctx.datecheck:
        return "skip", "switched off in Settings"
    exp = passport_expiry_date(info.text)
    if not exp:
        return _miss(ch, "could not read the expiry date")
    if not ctx.joining:
        return "skip", "joining date is invalid"
    need = add_months(ctx.joining, int(ch.arg or 6))
    return ("ok", f"expires {fmt_date(exp)}") if exp >= need else \
        _miss(ch, f"expires {fmt_date(exp)}; must be valid until {fmt_date(need)}")


@check_type("doc_age", "Issued within N days", "Uses the newest date found in the document.", "Days", "90")
def _c_age(ch, info, ctx):
    if not ctx.datecheck:
        return "skip", "switched off in Settings"
    today = dt.date.today()
    ds = [d for d in find_dates(info.text) if d <= today + dt.timedelta(days=2)]
    if not ds:
        return _miss(ch, "no date found in the document")
    newest, limit = max(ds), int(ch.arg or 90)
    age = (today - newest).days
    return ("ok", f"dated {fmt_date(newest)} ({age} days ago)") if age <= limit else \
        _miss(ch, f"dated {fmt_date(newest)} - {age} days ago, limit {limit}")


@check_type("no_exif_camera", "Scanned, not a phone photo", "Images with camera EXIF data look like phone photos.")
def _c_exif(ch, info, ctx):
    if info.kind != "image":
        return "ok", "PDF"
    return _miss(ch, "camera EXIF found - looks like a phone photo") if info.exif_cam else ("ok", "no camera EXIF")


@check_type("min_res", "Minimum resolution (short side, px)", "", "Pixels", "1000")
def _c_res(ch, info, ctx):
    if info.kind != "image":
        return "skip", "n/a for PDF"
    m = min(info.w, info.h)
    return ("ok", f"{m}px") if m >= int(ch.arg or 0) else _miss(ch, f"{m}px < {ch.arg}px")


@check_type("portrait", "Portrait orientation", "")
def _c_portrait(ch, info, ctx):
    if info.kind != "image":
        return "skip", "n/a"
    return ("ok", "") if info.h >= info.w else _miss(ch, f"{info.w}x{info.h} is landscape")


@check_type("face", "A face is detected (OpenCV)", "Exactly one face must be found.")
def _c_face(ch, info, ctx):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return "skip", "OpenCV not installed"
    if info.kind != "image":
        return "skip", "n/a"
    try:
        src = info.img_bytes or info.path
        g = cv2.cvtColor(np.array(open_rgb(src)), cv2.COLOR_RGB2GRAY)
        cc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        n = len(cc.detectMultiScale(g, 1.1, 5))
        return ("ok", f"{n} face(s)") if n == 1 else _miss(ch, f"{n} faces found")
    except Exception as e:
        return "skip", str(e)


@check_type("passport_surname", "Surname present in the passport", "")
def _c_pp_surname(ch, info, ctx):
    sn, src = extract_surname(info.text)
    return ("ok", f"{sn} ({src})") if sn else _miss(ch, src)


@check_type("surname_matches", "Surname matches the form", "Compares the passport surname with the form.")
def _c_surname_matches(ch, info, ctx):
    sn, src = extract_surname(info.text)
    want = ctx.surname.strip()
    if not want:
        return _miss(ch, "surname is empty on the cover form")
    if not sn:
        return "warn", "could not read surname from passport"
    from .aidocs import names_same
    same, why = names_same(ctx, sn, want, lambda: same_name(sn, want, ctx.fuzzy_name))
    if not same:
        return _miss(ch, f"passport says '{sn}', form says '{want}'" + (f" ({why})" if why else ""))
    if ctx.name.strip() and norm_ascii(want) not in norm_ascii(ctx.name):
        return "warn", "surname is not part of the full name field"
    return "ok", f"{sn} matches" + (f" ({why})" if why else "")


@check_type("surname_in_text", "Surname appears in the document", "")
def _c_surname_in(ch, info, ctx):
    want = ctx.surname.strip()
    if not want:
        return _miss(ch, "surname is empty on the cover form")
    if name_in_text(want, "x", ctx.fuzzy_name) is None:
        return "skip", "surname too short to check"
    from .aidocs import name_in_text_check
    return name_in_text_check(ch, ctx, info, want, "surname", lambda: name_in_text(want, info.text, ctx.fuzzy_name),
                              f"'{want}' not found in document")


@check_type("name_in_text", "Full name appears in the document", "")
def _c_name_in(ch, info, ctx):
    if name_in_text(ctx.name, "x", ctx.fuzzy_name) is None:
        return "skip", "enter full name first"
    from .aidocs import name_in_text_check
    return name_in_text_check(ch, ctx, info, ctx.name, "name", lambda: name_in_text(ctx.name, info.text, ctx.fuzzy_name),
                              "name not found in document")


@check_type("no_sample_terms", "Sample data NOT used", "Fails if any 'sample term' (Settings) appears.")
def _c_sample(ch, info, ctx):
    terms = [x.strip() for x in ctx.sample_terms if x.strip()]
    if not terms:
        return "skip", "add sample terms in Settings"
    found = [x for x in terms if norm_ascii(x) in norm_ascii(info.text)]
    return (ch.status_if_failed, f"sample data found: {', '.join(found)}") if found else ("ok", "")


@check_type("min_pages", "Minimum number of pages", "", "Pages", "1")
def _c_minpages(ch, info, ctx):
    return ("ok", "") if info.pages >= int(ch.arg or 1) else _miss(ch, f"{info.pages} page(s)")


@check_type("max_pages", "Maximum number of pages", "", "Pages", "3")
def _c_maxpages(ch, info, ctx):
    return ("ok", "") if info.pages <= int(ch.arg or 99) else _miss(ch, f"{info.pages} page(s) - limit {ch.arg}")


@check_type("max_size_mb", "Maximum file size (MB)", "", "MB", "5")
def _c_size(ch, info, ctx):
    try:
        mb = Path(info.path).stat().st_size / 1e6
    except OSError:
        return "skip", "file not found"
    return ("ok", f"{mb:.1f} MB") if mb <= float(ch.arg or 5) else _miss(ch, f"{mb:.1f} MB > {ch.arg} MB")


def run_check(ch: Check, info: DocInfo, ctx: CheckContext) -> Result:
    ctype = CHECK_TYPES.get(ch.type)
    if ctype is None or ctype.func is None:
        return "skip", "unknown check"
    try:
        return ctype.func(ch, info, ctx)
    except Exception as exc:                       # a broken check must never break the package
        return "warn", f"check failed: {exc}"


# --------------------------------------------------------------------------- defaults
def _kw(label, words, **kw) -> Rule:
    return Rule(label, "keyword", words, match=kw.pop("match", "contains"), **kw)


def default_doctypes() -> list[dict]:
    types = [
        DocType("passport", "Passport copy (scanner, full page)", signals=[
            _kw("Passport words", "passport, passeport, date of birth, nationality, surname, given names"),
            Rule("MRZ start", "regex", r"P<[A-Z]{3}", level="recommended", ignore_case=False, compact=True),
            Rule("MRZ filler", "regex", r"[A-Z0-9<]{20,}<<", level="recommended", ignore_case=False, compact=True),
            Rule("File name hints", "keyword", "passport, pp", scope="filename", match="prefix", weight=0.4),
        ], min_signals=2, checks=[
            Check("Surname is not empty", "surname_filled"),
            Check("Surname present in passport", "passport_surname"),
            Check("Surname matches cover form", "surname_matches"),
            Check("Scanned, not a phone photo", "no_exif_camera"),
            Check("High enough resolution", "min_res", "1000", "recommended"),
            Check("MRZ (machine-readable zone) visible", "regex", "<<", "recommended"),
            Check("MRZ check digits valid", "mrz_valid", level="recommended"),
            Check("Passport valid 6+ months after joining", "passport_expiry", "6"),
        ]),
        DocType("photo", "Half-length photo", kinds=["image"], min_score=0.3, low_text_bonus=True, signals=[
            Rule("File name hints", "keyword", "photo, half, portrait, selfie, pic, img", scope="filename",
                 match="prefix", weight=0.4)], checks=[
            Check("Portrait orientation", "portrait", level="recommended"),
            Check("Face detected", "face", level="recommended"),
            Check("Min. resolution", "min_res", "600", "recommended"),
        ]),
        DocType("bank", "Bank account letter", signals=[
            _kw("Bank words", "iban, swift, account number, bank, confidential, account number and iban confirmation"),
            Rule("File name hints", "keyword", "bank, iban, account", scope="filename", match="prefix", weight=0.4),
        ], min_signals=3, checks=[
            Check("Subject: Account number and IBAN confirmation", "regex", r"account\s*number\s*and\s*iban\s*confirmation"),
            Check("IBAN present", "regex", r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact=True),
            Check("Swift / BIC present", "regex", r"swift|bic"),
            Check("Account number present", "regex", r"account\s*(no|number|#)"),
            Check("Date present", "regex", r"\d{1,2}[-/. ]\w{3,9}[-/. ]\d{2,4}|\w{3,9}\s+\d{1,2},?\s+\d{4}", "recommended"),
            Check("Confidentiality statement", "regex", "confidential"),
            Check("Name matches passport name", "name_in_text"),
            Check("Surname matches passport", "surname_in_text"),
            Check("Sample name/details NOT used", "no_sample_terms"),
            Check("Issued within the last 90 days", "doc_age", "90", "recommended"),
            Check("Letterhead & logo", "manual"),
            Check("Officer signature", "manual"),
            Check("Official bank/branch stamp", "manual"),
        ]),
        DocType("medical", "Medical report (IGRA, HIV, HCV, HBsAg)", signals=[
            _kw("Medical words", "igra, hiv, hcv, hbsag, medical report, laboratory"),
            Rule("File name hints", "keyword", "medical, lab, report, health", scope="filename", match="prefix",
                 weight=0.4),
        ], min_signals=2, checks=[
            Check("IGRA result", "regex", r"igra|interferon|quantiferon|qft|t[- ]?spot"),
            Check("HIV result", "regex", r"\bhiv\b"),
            Check("HCV result", "regex", r"\bhcv\b|hepatitis\s*c"),
            Check("HBsAg result", "regex", r"hbs\s*ag|hepatitis\s*b"),
            Check("Name matches passport name", "name_in_text", level="recommended"),
            Check("Surname matches passport", "surname_in_text", level="recommended"),
            Check("Issued within the last 180 days", "doc_age", "180", "recommended"),
        ]),
    ]
    return [t.to_dict() for t in types]
