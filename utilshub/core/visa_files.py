"""Find the visa (or ticket) page inside a big file, extract structured visa fields, and store just that page (no Tk).

    texts, error = read_page_texts("big_scan.pdf", cfg, ocr_pages=40)       # text of every page (OCR where needed)
    scores = rank_pages(texts, "visa", passport_no="X1234567", name="Ali Khan")
    best = likely_pages(scores)                                              # [PageScore(page=16, score=14.0, ...)]
    dates = suggest_dates(texts[16], "visa")                                 # {"issue": date, "expiry": date, "all": [...]}
    info = extract_visa_page_info(texts[0], page=0)                          # ExtractedVisa(full_name=..., passport_no=..., ...)
    extract_pages("big_scan.pdf", [16], "Visa - Ali Khan.pdf")               # one clean PDF page, nothing else
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from .dates import find_dates

# (keyword, points).  Latin words must match as whole words; Arabic ones anywhere.
KEYWORDS: dict[str, list[tuple[str, int]]] = {
    "visa": [("visa", 4), ("e-visa", 4), ("evisa", 4), ("electronic visa", 5), ("entry visa", 4),
             ("entry permit", 3), ("work permit", 3), ("residence permit", 2),
             ("duration of stay", 3), ("entry validity", 3), ("number of entries", 3), ("no. of entries", 3),
             ("single entry", 3), ("multiple entry", 3), ("valid until", 1), ("date of issue", 1),
             ("place of issue", 1), ("issuing authority", 2), ("sponsor", 1), ("requester", 1),
             ("employment", 1), ("immigration", 1), ("تأشيرة", 5), ("تاشيرة", 5), ("فيزا", 4),
             ("سمة دخول", 5), ("سمة", 3), ("إقامة", 1)],
    "ticket": [("e-ticket", 5), ("eticket", 5), ("itinerary", 4), ("booking reference", 4), ("ticket number", 4),
               ("pnr", 4), ("boarding", 2), ("flight", 2), ("departure", 2), ("airlines", 2), ("airways", 2),
               ("passenger", 2), ("arrival", 1), ("seat", 1), ("baggage", 1), ("تذكرة", 4), ("رحلة", 2)],
    "passport": [("passport", 4), ("p<", 5), ("date of birth", 1), ("nationality", 1), ("جواز", 4)],
}
NEGATIVE: dict[str, list[tuple[str, int]]] = {
    "visa": [("itinerary", 3), ("boarding", 3), ("e-ticket", 3), ("curriculum vitae", 3), ("resume", 2)],
    "ticket": [("visa", 1)],
    "passport": [("itinerary", 2), ("boarding", 2)],
}
_DOB = re.compile(r"date\s*of\s*birth|birth\s*date|تاريخ\s*الميلاد", re.I)
_ISSUE = re.compile(r"date of issue|issue date|issued on|date issued|issued|issue|تاريخ الإصدار|تاريخ الاصدار|تاريخ إصدار|تاريخ اصدار", re.I)
_EXPIRY = re.compile(r"valid until|valid to|valid till|valid thru|expiry|expires?|expiration|enter before|last entry|"
                     r"must enter|تاريخ الانتهاء|ينتهي", re.I)
_VALIDITY_DAYS = re.compile(r"entry\s*validity|صلاحية\s*الدخول", re.I)
_DEPART = re.compile(r"depart|departure|flight date|travel date|outbound|take.?off|date of travel|date", re.I)

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_BIDI_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_TATWEEL_RE = re.compile(r"\u0640+")


class VisaFileError(Exception):
    """A user-facing problem with a file."""


@dataclass
class PageScore:
    page: int                                   # 0-based
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class ExtractedVisa:
    """Structured information parsed from a single visa page (tailored for Iraq Electronic Visa + generic visas)."""
    page: int = 0                               # 0-based page index
    full_name: str = ""
    nationality: str = ""
    date_of_birth: str = ""                     # ISO YYYY-MM-DD
    passport_no: str = ""
    passport_type: str = ""
    visa_no: str = ""                           # Entry Visa No.
    application_no: str = ""                    # Application No.
    visa_type: str = ""
    issuing_authority: str = ""
    visa_issue: str = ""                        # Date of Issue (ISO YYYY-MM-DD)
    entry_validity: int = 0                     # Entry Validity in days (e.g. 90)
    duration_of_stay: int = 0                   # Duration of stay in days (e.g. 365)
    visa_deadline: str = ""                     # Explicit fixed last entry date if printed (ISO)
    requester: str = ""
    address: str = ""
    email: str = ""
    mobile: str = ""
    is_visa: bool = False
    score: float = 0.0
    raw_text: str = ""


def _clean_text(text: str) -> str:
    s = _BIDI_RE.sub("", text or "")
    s = _TATWEEL_RE.sub("", s)
    return s.translate(_ARABIC_DIGITS)


def _norm(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u0600-\u06FF]+", "", _clean_text(text)).upper()


def _has(word: str, text: str) -> bool:
    if re.search(r"[\u0600-\u06FF]", word):
        return word in text
    return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text) is not None


def score_page(text: str, kind: str = "visa", passport_no: str = "", name: str = "") -> PageScore:
    cleaned = _clean_text(text)
    low = cleaned.lower()
    res = PageScore(0)
    if len(low.strip()) < 15:
        return res
    for word, pts in KEYWORDS.get(kind, []):
        if _has(word, low):
            res.score += pts
            res.reasons.append(word)
    for word, pts in NEGATIVE.get(kind, []):
        if _has(word, low):
            res.score -= pts
    flat = _norm(cleaned)
    pn = _norm(passport_no)
    if len(pn) >= 5 and pn in flat:
        res.score += 6
        res.reasons.insert(0, "passport number")
    tokens = [t for t in re.split(r"\s+", name.strip()) if len(t) >= 3]
    if tokens:
        found = [t for t in tokens if _norm(t) in flat]
        if len(found) == len(tokens):
            res.score += 4
            res.reasons.insert(0, "name")
        elif found:
            res.score += 2
            res.reasons.insert(0, "part of the name")
    res.score = max(0.0, res.score)
    return res


def rank_pages(texts: list[str], kind: str = "visa", passport_no: str = "", name: str = "") -> list[PageScore]:
    out = []
    for i, text in enumerate(texts):
        s = score_page(text, kind, passport_no, name)
        s.page = i
        out.append(s)
    return out


def likely_pages(scores: list[PageScore], min_score: float = 4.0, limit: int = 12) -> list[PageScore]:
    """The pages that look like the one we are after, best first."""
    return sorted((s for s in scores if s.score >= min_score), key=lambda s: (-s.score, s.page))[:limit]


# --------------------------------------------------------------------------- dates written on the page
def suggest_dates(text: str, kind: str = "visa", today: dt.date | None = None) -> dict:
    """Dates read from a page: {"issue", "expiry"} for a visa, {"flight"} for a ticket, and always {"all"}."""
    today = today or dt.date.today()
    cleaned = _clean_text(text)
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    found: dict = {"issue": None, "expiry": None, "flight": None, "all": []}
    seen: list[dt.date] = []
    validity_days = 0
    for i, line in enumerate(lines):
        if kind == "visa" and _VALIDITY_DAYS.search(line):
            nums = re.findall(r"\b(\d{1,3})\b", line)
            if not nums and i + 1 < len(lines):
                nums = re.findall(r"\b(\d{1,3})\b", lines[i + 1])
            if nums:
                val = int(nums[0])
                if 1 <= val <= 720:
                    validity_days = val
        dates = find_dates(line, allow2=True)
        if not dates:
            continue
        if _DOB.search(line) or (i > 0 and _DOB.search(lines[i - 1]) and not _ISSUE.search(line) and not _EXPIRY.search(line)):
            continue
        seen.extend(dates)
        context = [line, lines[i - 1] if i else ""]
        for ctx in context:                                    # label on the same line, else on the line above
            is_issue, is_expiry = bool(_ISSUE.search(ctx)), bool(_EXPIRY.search(ctx))
            if is_expiry and found["expiry"] is None:
                found["expiry"] = dates[-1]
                break
            if is_issue and not is_expiry and found["issue"] is None:
                found["issue"] = dates[0]
                break
            if kind == "ticket" and _DEPART.search(ctx) and found["flight"] is None and ctx is line:
                found["flight"] = dates[0]
                break
    found["all"] = sorted(set(seen))
    if kind == "ticket" and found["flight"] is None:
        future = [d for d in found["all"] if d >= today]
        found["flight"] = future[0] if future else None
    if kind == "visa" and found["issue"] and found["expiry"] and found["expiry"] < found["issue"]:
        found["issue"], found["expiry"] = found["expiry"], found["issue"]
    if kind == "visa" and validity_days:
        found["entry_validity"] = validity_days
    return found


# --------------------------------------------------------------------------- structured visa page extractor
# Field specs: (attr, label_regex, kind)
# Labels on Iraq Electronic Visa appear in Arabic / Kurdish / English with optional colons and slashes.
_FIELD_SPECS: list[tuple[str, re.Pattern[str], str]] = [
    ("full_name", re.compile(r"(?:الاسم\s*الكامل|ناوى\s*ت[ه‌ه]واو|full\s*name)\s*:?", re.I), "name"),
    ("nationality", re.compile(r"(?:الجنسية|ڕ[ه‌ه]گ[ه‌ه]زنام[ه‌ه]|nationality)\s*:?", re.I), "bilingual"),
    ("date_of_birth", re.compile(r"(?:تاريخ\s*الميلاد|ڕۆژی\s*ل[ه‌ه]\s*دایک\s*بوون|date\s*of\s*birth)\s*:?", re.I), "date"),
    ("passport_no", re.compile(r"(?:رقم\s*الجواز|ژمار[ه‌ه]ی\s*پاسپۆرت|passport\s*no\.?|passport\s*number)\s*:?", re.I), "passport"),
    ("passport_type", re.compile(r"(?:نوع\s*الجواز|جۆری\s*پاسپۆرت|passport\s*type)\s*:?", re.I), "text"),
    ("visa_no", re.compile(r"(?:رقم\s*سمة\s*الدخول|ژمار[ه‌ه]ی\s*ڤیز[ه‌ه]ی\s*چوون[ه‌ه]\s*ژوور[ه‌ه]و[ه‌ه]|entry\s*visa\s*no\.?|visa\s*no\.?)\s*:?", re.I), "code"),
    ("application_no", re.compile(r"(?:رقم\s*الطلب|ژمار[ه‌ه]ی\s*داواكردن|application\s*no\.?)\s*:?", re.I), "code"),
    ("visa_type", re.compile(r"(?:نوع\s*السمة|جۆری\s*ڤیز[ه‌ه]|visa\s*type)\s*:?", re.I), "bilingual"),
    ("issuing_authority", re.compile(r"(?:جهة\s*منح\s*السمة|شوێنی\s*د[ه‌ه]رچوون|issuing\s*authority)\s*:?", re.I), "bilingual"),
    ("visa_issue", re.compile(r"(?:تاريخ\s*[إا]صدار\s*سمة\s*الدخول|تاريخ\s*[إا]صدار|ب[ه‌ه]رواری\s*د[ه‌ه]رچوونی\s*ڤیز[ه‌ه]|date\s*of\s*issue|issue\s*date)\s*:?", re.I), "date"),
    ("entry_validity", re.compile(r"(?:صلاحية\s*الدخول|مۆڵ[ه‌ه]ت\s*بۆ\s*د[ه‌ه]ستگ[ه‌ه]یشتن|entry\s*validity)\s*:?", re.I), "int"),
    ("duration_of_stay", re.compile(r"(?:مدة\s*الإقامة|مدة\s*الاقامة|ماو[ه‌ه]ی\s*مان[ه‌ه]و[ه‌ه]|duration\s*of\s*stay)\s*:?", re.I), "int"),
    ("requester", re.compile(r"(?:جهة\s*الطلب|لای[ه‌ه]نی\s*داواکار|requester)\s*:?", re.I), "text"),
    ("address", re.compile(r"(?:العنوان|ناونیشان|address)\s*:?", re.I), "text"),
    ("email", re.compile(r"(?:البريد\s*الإلكتروني|البريد\s*الالكتروني|ئیم[ه‌ه]یل|e-?mail)\s*:?", re.I), "email"),
    ("mobile", re.compile(r"(?:رقم\s*الهاتف|ژمار[ه‌ه]ی\s*مۆبایل|mobile|phone)\s*:?", re.I), "phone"),
]

_SECTION_HEADERS = re.compile(
    r"visa\s*information|information\s*of\s*electronic\s*visa|requester\s*information|"
    r"معلومات\s*الزائر|معلومات\s*سمة\s*الدخول|معلومات\s*جهة\s*الطلب|"
    r"republic\s*of\s*iraq|ministry\s*of\s*interior|iraq\s*electronic\s*visa|"
    r"جمهورية\s*العراق|وزارة\s*الداخلية|سمة\s*دخول\s*العراق|"
    r"welcome\.|notice\s*:|تنبيه\s*:|أهلا\s*وسهلا",
    re.I,
)

# Combined regex that strips the entire trilingual label chunk from a line
_FULL_LABEL_CHUNK = re.compile(
    r"(?:"
    r"الاسم\s*الكامل|ناوى\s*ت[ه‌ه]واو|full\s*name|"
    r"الجنسية|ڕ[ه‌ه]گ[ه‌ه]زنام[ه‌ه]|nationality|"
    r"تاريخ\s*الميلاد|ڕۆژی\s*ل[ه‌ه]\s*دایک\s*بوون|date\s*of\s*birth|"
    r"رقم\s*الجواز|ژمار[ه‌ه]ی\s*پاسپۆرت|passport\s*no\.?|passport\s*number|"
    r"نوع\s*الجواز|جۆری\s*پاسپۆرت|passport\s*type|"
    r"رقم\s*سمة\s*الدخول|ژمار[ه‌ه]ی\s*ڤیز[ه‌ه]ی\s*چوون[ه‌ه]\s*ژوور[ه‌ه]و[ه‌ه]|entry\s*visa\s*no\.?|visa\s*no\.?|"
    r"رقم\s*الطلب|ژمار[ه‌ه]ی\s*داواكردن|application\s*no\.?|"
    r"نوع\s*السمة|جۆری\s*ڤیز[ه‌ه]|visa\s*type|"
    r"جهة\s*منح\s*السمة|شوێنی\s*د[ه‌ه]رچوون|issuing\s*authority|"
    r"تاريخ\s*[إا]صدار\s*سمة\s*الدخول|تاريخ\s*[إا]صدار|ب[ه‌ه]رواری\s*د[ه‌ه]رچوونی\s*ڤیز[ه‌ه]|date\s*of\s*issue|issue\s*date|"
    r"صلاحية\s*الدخول|مۆڵ[ه‌ه]ت\s*بۆ\s*د[ه‌ه]ستگ[ه‌ه]یشتن|entry\s*validity|"
    r"مدة\s*الإقامة|مدة\s*الاقامة|ماو[ه‌ه]ی\s*مان[ه‌ه]و[ه‌ه]|duration\s*of\s*stay|"
    r"جهة\s*الطلب|لای[ه‌ه]نی\s*داواکار|requester|"
    r"العنوان|ناونیشان|address|"
    r"البريد\s*الإلكتروني|البريد\s*الالكتروني|ئیم[ه‌ه]یل|e-?mail|"
    r"رقم\s*الهاتف|ژمار[ه‌ه]ی\s*مۆبایل|mobile|phone"
    r")\s*[:/]?",
    re.I,
)


def _is_label_only_line(line: str) -> bool:
    rem = _FULL_LABEL_CHUNK.sub("", line)
    rem = re.sub(r"[\s/:._\-|]+", "", rem)
    return not rem


def _strip_labels_from_line(line: str) -> str:
    """Remove any Arabic/Kurdish/English field labels from a line, leaving only the value."""
    s = _FULL_LABEL_CHUNK.sub(" ", line)
    # Remove orphan slashes/colons left between trilingual labels
    s = re.sub(r"(?:^|\s)[/:|]+(?:\s|$)", " ", s)
    s = re.sub(r"^[\s/:|]+|[\s/:|]+$", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def _prefer_latin(val: str) -> str:
    """On bilingual values like 'Indian   هندي' or 'Multiple One Year   متعددة سنة واحدة', keep the clean English part."""
    val = re.sub(r"\s+", " ", val or "").strip(" :/-|")
    if not val:
        return ""
    latin_parts = re.findall(r"[A-Za-z][A-Za-z0-9 .,'()\-&/]*", val)
    has_arabic = bool(re.search(r"[\u0600-\u06FF]", val))
    if latin_parts and has_arabic:
        best = " ".join(p.strip() for p in latin_parts if p.strip()).strip(" :/-|")
        if len(best) >= 2:
            return best
    return val


def _parse_field_value(raw: str, kind: str):
    val = re.sub(r"\s+", " ", raw or "").strip(" :|")
    if not val:
        return 0 if kind == "int" else ""
    if kind == "date":
        m = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", val)
        if m:
            try:
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError:
                pass
        found = find_dates(val, allow2=True)
        return found[0].isoformat() if found else ""
    if kind == "int":
        m = re.search(r"\b(\d{1,4})\b", val)
        return int(m.group(1)) if m else 0
    if kind == "passport":
        # Passport numbers are alphanumeric, 5-15 chars, usually containing at least one digit
        tokens = re.findall(r"\b[A-Za-z0-9]{5,15}\b", val)
        with_digits = [t for t in tokens if any(c.isdigit() for c in t)]
        return (with_digits[0] if with_digits else (tokens[0] if tokens else val)).upper()
    if kind == "code":
        m = re.search(r"\b[A-Za-z0-9][A-Za-z0-9\-]{3,24}\b", val)
        return m.group(0) if m else val
    if kind == "email":
        m = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", val)
        return m.group(0) if m else ""
    if kind == "phone":
        m = re.search(r"[+\d][\d\s()\-]{5,20}\d", val)
        return m.group(0).strip() if m else ""
    if kind == "bilingual":
        return _prefer_latin(val)
    if kind == "name":
        cleaned = _prefer_latin(val)
        return re.sub(r"\s+", " ", cleaned).strip()
    return val


def extract_visa_page_info(text: str, page: int = 0) -> ExtractedVisa:
    """Parse structured visa details from one page's text (Iraq Electronic Visa + generic visas)."""
    cleaned = _clean_text(text)
    sc = score_page(cleaned, "visa")
    info = ExtractedVisa(page=page, score=sc.score, is_visa=sc.score >= 4.0, raw_text=text or "")
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if not lines:
        return info

    # Pass 1: label + value on the same line, or value on the immediately following/preceding non-label line
    label_line_indices: list[tuple[int, str, str]] = []
    for idx, line in enumerate(lines):
        if _SECTION_HEADERS.search(line):
            continue
        for attr, rx, kind in _FIELD_SPECS:
            if not rx.search(line):
                continue
            label_line_indices.append((idx, attr, kind))
            if getattr(info, attr):
                continue
            remainder = _strip_labels_from_line(line)
            parsed = _parse_field_value(remainder, kind) if remainder else (0 if kind == "int" else "")
            if parsed:
                setattr(info, attr, parsed)
                continue
            # Check next line if it is not itself a label or section header
            if idx + 1 < len(lines) and not _SECTION_HEADERS.search(lines[idx + 1]) and not any(
                r2.search(lines[idx + 1]) for _, r2, _ in _FIELD_SPECS
            ):
                parsed = _parse_field_value(lines[idx + 1], kind)
                if parsed:
                    setattr(info, attr, parsed)
                    continue
            # Check previous line (RTL column-order fallback when value precedes label)
            if idx > 0 and not _SECTION_HEADERS.search(lines[idx - 1]) and not any(
                r2.search(lines[idx - 1]) for _, r2, _ in _FIELD_SPECS
            ):
                parsed = _parse_field_value(lines[idx - 1], kind)
                if parsed:
                    setattr(info, attr, parsed)

    # Pass 2: column-split fallback (when PDF text layer emits a block of labels followed/preceded by a block of values)
    # Match well-formed Iraq e-Visa patterns anywhere on the page if still missing
    if not info.application_no:
        m = re.search(r"\b(\d{6}-\d{6,12})\b", cleaned)
        if m:
            info.application_no = m.group(1)

    # Fallback dates from suggest_dates (for generic visas with Valid until / Date of issue)
    sd = suggest_dates(cleaned, "visa")
    if not info.visa_issue and sd.get("issue"):
        info.visa_issue = sd["issue"].isoformat()
    if not info.visa_deadline and sd.get("expiry"):
        info.visa_deadline = sd["expiry"].isoformat()
    if not info.entry_validity and sd.get("entry_validity"):
        info.entry_validity = int(sd["entry_validity"])

    # If we extracted key Iraq e-Visa identifiers, mark as visa even if keywords were sparse
    if not info.is_visa and (info.visa_no or info.application_no) and (info.passport_no or info.full_name):
        info.is_visa = True
        info.score = max(info.score, 6.0)

    return info


def extract_visas_from_texts(texts: list[str]) -> list[ExtractedVisa]:
    """Parse structured visa info for every page."""
    return [extract_visa_page_info(t, i) for i, t in enumerate(texts)]


# --------------------------------------------------------------------------- reading and cutting
def _pymupdf_row_grouped_texts(path: Path) -> list[str] | None:
    """Read PDF pages using PyMuPDF grouping words by Y-coordinate so 2-column RTL forms stay on one line per row."""
    if path.suffix.lower() != ".pdf":
        return None
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz  # type: ignore
        except ImportError:
            return None
    if not hasattr(fitz, "open"):
        return None
    try:
        doc = fitz.open(str(path))
    except Exception:
        return None
    texts: list[str] = []
    try:
        for page in doc:
            words = page.get_text("words") or []
            if not words:
                texts.append("")
                continue
            # Each word: (x0, y0, x1, y1, text, block_no, line_no, word_no)
            words.sort(key=lambda w: (round(((w[1] + w[3]) / 2.0) / 5.0), w[0]))
            rows: list[list[tuple]] = []
            for w in words:
                yc = (w[1] + w[3]) / 2.0
                if rows and abs(yc - rows[-1][0][0]) <= 6.0:
                    rows[-1].append((yc, w[0], w[4]))
                else:
                    rows.append([(yc, w[0], w[4])])
            lines = []
            for row in rows:
                row.sort(key=lambda item: item[1])
                lines.append(" ".join(item[2] for item in row))
            texts.append("\n".join(lines))
    finally:
        doc.close()
    return texts


def read_page_texts(path: str | Path, cfg, ocr_pages: int = 40) -> tuple[list[str], str]:
    """Text of every page.  Pages without a text layer are read with OCR for the first `ocr_pages` pages.
    Returns (texts, error message or '')."""
    from .readers import ReadOptions, read_document
    p = Path(path)
    grouped = _pymupdf_row_grouped_texts(p)
    if grouped and any(len(t.strip()) >= 40 for t in grouped):
        # If every page has text, return row-grouped text directly; otherwise fill scanned pages with OCR
        if all(len(t.strip()) >= 40 for t in grouped) or not cfg.enabled:
            return grouped, ""
        cfg.max_pages = max(0, ocr_pages)
        info = read_document(p, cfg, ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=False, deskew=False))
        ocr_texts = list(info.page_texts) or ([info.text] if info.text else [])
        merged = [
            g if len(g.strip()) >= 40 else (ocr_texts[i] if i < len(ocr_texts) else g)
            for i, g in enumerate(grouped)
        ]
        return merged, info.error
    cfg.max_pages = max(0, ocr_pages)
    info = read_document(p, cfg, ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=False, deskew=False))
    texts = list(info.page_texts) or ([info.text] if info.text else [])
    return texts, info.error


def extract_pages(src: str | Path, pages: list[int], dest: str | Path, rotations: dict[int, int] | None = None,
                  libreoffice_cmd: str = "") -> str:
    """Write the chosen pages (0-based) of `src` as a new PDF.  `rotations`: extra clockwise degrees per page."""
    from . import pdfops
    src = Path(src)
    if not src.exists():
        raise VisaFileError(f"{src.name} was not found.")
    if not pages and src.suffix.lower() == ".pdf":
        raise VisaFileError("Choose at least one page.")
    piece = pdfops.Piece(str(src), pages=sorted(set(pages)) if pages else None, page_rotate=dict(rotations or {}))
    opts = pdfops.AssembleOptions(page_size="a4", bookmarks=False, libreoffice_cmd=libreoffice_cmd)
    try:
        res = pdfops.assemble([piece], dest, opts)
    except Exception as exc:                                   # PdfError, OSError, OfficeError ...
        raise VisaFileError(f"Could not cut the page out of {src.name}: {exc}") from exc
    if res.skipped or res.pages < 1:
        raise VisaFileError(f"Could not cut the page out of {src.name}: " + ("; ".join(res.skipped) or "no pages"))
    return res.path
