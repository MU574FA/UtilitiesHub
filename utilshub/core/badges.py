"""Badge Extractor core logic (no Tk): Arabic OCR, 3-line badge parsing, English transliteration,
customizable filename rendering, and multi-match Excel similarity lookup.

Designed for employee/worker ID badges where:
  * Line 1 = Arabic full name
  * Line 2 = Job title / department
  * Line 3 = Badge number
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps

from . import llm, ocr
from .fuzzy import lev_ratio, name_score, prep_name
from .sheets import SheetError, Table, cell_text, guess_name_column, load_book
from .templates import base_values, render, tidy_filename
from .textutil import arabic_fold, has_arabic, sanitize_filename

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
CV_EXTENSIONS = {".pdf", ".docx", ".doc", ".rtf", ".odt", ".txt", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
_CV_NOISE = re.compile(r"\b(?:cv|resume|résumé|curriculum|vitae|updated|final|new|copy|scan|doc|document)\b", re.I)
DEFAULT_FILENAME_TEMPLATE = "{ar_name}-{en_name}-{badge_no}-{job_title}"

FILENAME_PRESETS: list[tuple[str, str]] = [
    ("Arabic - English - Badge - Job", "{ar_name}-{en_name}-{badge_no}-{job_title}"),
    ("Arabic - English - Job - Badge", "{ar_name}-{en_name}-{job_title}-{badge_no}"),
    ("Badge - English - Arabic", "{badge_no}-{en_name}-{ar_name}"),
    ("English - Badge - Job", "{en_name}-{badge_no}-{job_title}"),
    ("Arabic - Badge", "{ar_name}-{badge_no}"),
]

SEARCH_BY_CHOICES: list[tuple[str, str]] = [
    ("en_name", "English transliteration"),
    ("ar_name", "Arabic name"),
    ("badge_no", "Badge number"),
    ("auto", "Auto (badge no + English + Arabic)"),
]

CROP_MODE_CHOICES: list[tuple[str, str]] = [
    ("auto", "Auto (right text area, fallback full)"),
    ("right", "Right side only (skip left photo)"),
    ("full", "Full badge image"),
]

_AR_TO_EN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
_BADGE_NO_HDR = re.compile(
    r"badge|emp(?:loyee)?\s*(?:id|no|num|#)|staff\s*(?:id|no)|id\s*(?:no|num|#)|card\s*(?:no|num|#)|"
    r"رقم\s*(?:الباج|البطاقة|الوظيفي|الموظف|الهوية)|باج",
    re.I,
)
_NOISE_LINE = re.compile(r"^[\W_]+$")

# Common Middle-Eastern, Iraqi, Arabic, and South-Asian name spellings for cleaner transliteration
_COMMON_NAMES: dict[str, str] = {
    "محمد": "Mohammed", "احمد": "Ahmed", "علي": "Ali", "حسن": "Hassan", "حسين": "Hussein",
    "حيدر": "Haider", "عباس": "Abbas", "كاظم": "Kadhim", "جعفر": "Jaafar", "مهدي": "Mahdi",
    "رضا": "Ridha", "صادق": "Sadiq", "باقر": "Baqir", "مرتضي": "Murtadha", "مصطفي": "Mustafa",
    "عبدالله": "Abdullah", "عبد": "Abdul", "الله": "Allah", "رحمن": "Rahman", "عزيز": "Aziz",
    "رزاق": "Razzaq", "ستار": "Sattar", "جبار": "Jabbar", "قادر": "Qadir", "فتاح": "Fattah",
    "مجيد": "Majeed", "زهراء": "Zahraa", "امير": "Amir",
    "ابراهيم": "Ibrahim", "اسماعيل": "Ismail", "يوسف": "Youssef", "يعقوب": "Yaqub", "داود": "Dawood",
    "سليمان": "Sulaiman", "موسي": "Musa", "عيسي": "Issa", "يحيي": "Yahya", "زكريا": "Zakaria",
    "عمر": "Omar", "عثمان": "Othman", "خالد": "Khaled", "سعد": "Saad", "سعيد": "Saeed",
    "طارق": "Tariq", "وليد": "Waleed", "فيصل": "Faisal", "ماجد": "Majid", "ناصر": "Nasser",
    "منصور": "Mansour", "سمير": "Samir", "نبيل": "Nabil", "فؤاد": "Fuad", "زياد": "Ziad",
    "بلال": "Bilal", "انس": "Anas", "حمزه": "Hamza", "امين": "Amin", "عادل": "Adel",
    "جمال": "Jamal", "كمال": "Kamal", "جلال": "Jalal", "جاسم": "Jasim", "قاسم": "Qasim",
    "كريم": "Karim", "رحيم": "Rahim", "سلمان": "Salman", "سلام": "Salam", "صلاح": "Salah",
    "فلاح": "Falah", "نجاح": "Najah", "صباح": "Sabah", "رائد": "Raed", "مازن": "Mazen",
    "باسم": "Basim", "حسام": "Hussam", "وسام": "Wissam", "مهند": "Muhannad", "عمار": "Ammar",
    "ياسر": "Yasser", "ياسين": "Yassin", "طه": "Taha", "علاء": "Alaa", "بهاء": "Bahaa",
    "ضياء": "Dhiyaa", "صفاء": "Safaa", "شفاء": "Shifaa", "اسعد": "Asaad", "اكرم": "Akram",
    "احسان": "Ihsan", "اسلام": "Islam", "ايمن": "Ayman", "اياد": "Iyad", "عماد": "Imad",
    "فراس": "Firas", "ليث": "Laith", "غيث": "Ghaith", "نور": "Noor", "زينة": "Zainab",
    "زينب": "Zainab", "فاطمه": "Fatima", "مريم": "Maryam", "ساره": "Sara", "نوره": "Noura",
    "هدي": "Huda", "ندي": "Nada", "رشا": "Rasha", "منى": "Muna", "مني": "Muna",
    "خان": "Khan", "فيروج": "Firoj", "فيروز": "Firoz", "راجيش": "Rajesh", "كومار": "Kumar",
    "سينغ": "Singh", "احمدي": "Ahmadi", "بشير": "Bashir", "رشيد": "Rashid", "شريف": "Sharif",
    "لطيف": "Latif", "منير": "Munir", "نذير": "Nadhir", "قدير": "Qadir", "نصير": "Nasir",
    "شاكر": "Shakir", "ذاكر": "Dhakir", "طاهر": "Tahir", "ظاهر": "Dhahir", "ماهر": "Maher",
    "زاهر": "Zaher", "فارس": "Faris", "حارس": "Haris", "بدر": "Badr", "نصر": "Nasr",
    "حامد": "Hamid", "محمود": "Mahmoud", "ممدوح": "Mamdouh", "مسعود": "Masoud", "سعود": "Saud",
    "راشد": "Rashid", "مرشد": "Murshid", "سالم": "Salem", "غانم": "Ghanem", "حاتم": "Hatem",
    "هاشم": "Hashim", "كاظم": "Kadhim", "ناظم": "Nadhim", "هادي": "Hadi", "فادي": "Fadi",
    "شادي": "Shadi", "رامي": "Rami", "سامي": "Sami", "هاني": "Hani", "مؤيد": "Muayad",
    "ثامر": "Thamer", "عامر": "Amer", "تامر": "Tamer", "سامر": "Samer", "يوسف": "Youssef",
    "فرهاد": "Farhad", "شهيد": "Shahid", "ساجد": "Sajid", "عابد": "Abid", "واجد": "Wajid",
    "زاهد": "Zahid", "مجاهد": "Mujahid", "خليل": "Khalil", "جميل": "Jamil", "عقيل": "Aqeel",
    "اسماعيل": "Ismail", "جبريل": "Jibreel", "ميكائيل": "Mikail", "عمران": "Imran",
    "عدنان": "Adnan", "قحطان": "Qahtan", "غسان": "Ghassan", "حسان": "Hassan", "عثمان": "Othman",
    "رمضان": "Ramadhan", "شعبان": "Shaaban", "رجب": "Rajab", "صفر": "Safar", "محرم": "Muharram",
    "جواد": "Jawad", "فؤاد": "Fuad", "ايهاب": "Ihab", "شهاب": "Shihab", "وهاب": "Wahab",
    "عمال": "Workers", "عامل": "Worker", "التنظيف": "Cleaning", "تنظيف": "Cleaning",
    "المجزر": "Slaughterhouse", "مجزر": "Slaughterhouse", "مشرف": "Supervisor",
    "مهندس": "Engineer", "فني": "Technician", "سائق": "Driver", "حارس": "Guard",
    "اداري": "Admin", "محاسب": "Accountant", "مترجم": "Interpreter", "مدير": "Manager",
    "انتاج": "Production", "الانتاج": "Production", "صيانه": "Maintenance", "الصيانه": "Maintenance",
    "جوده": "Quality", "الجوده": "Quality", "مخازن": "Warehouse", "المخازن": "Warehouse",
}

_CHAR_MAP: dict[str, str] = {
    "ا": "a", "أ": "a", "إ": "i", "آ": "aa", "ٱ": "a",
    "ب": "b", "پ": "p", "ت": "t", "ث": "th",
    "ج": "j", "چ": "ch", "ح": "h", "خ": "kh",
    "د": "d", "ذ": "dh", "ر": "r", "ز": "z", "ژ": "zh",
    "س": "s", "ش": "sh", "ص": "s", "ض": "d",
    "ط": "t", "ظ": "dh", "ع": "a", "غ": "gh",
    "ف": "f", "ڤ": "v", "ق": "q", "ك": "k", "ک": "k", "گ": "g",
    "ل": "l", "م": "m", "ن": "n", "ه": "h", "ة": "a",
    "و": "w", "ؤ": "o", "ي": "y", "ی": "y", "ى": "a", "ئ": "i",
    "ء": "", "لا": "la",
}

_TABLE_CACHE: dict[tuple[str, str, int], tuple[float, Table]] = {}


# --------------------------------------------------------------------------- data models
@dataclass
class BadgeData:
    ar_name: str = ""
    en_name: str = ""
    job_title: str = ""
    en_job_title: str = ""
    badge_no: str = ""
    raw_text: str = ""
    lines: list[str] = field(default_factory=list)
    ocr_source: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class CandidateMatch:
    score: float                                 # 0.0 .. 1.0
    row: int                                     # Excel row number (1-based)
    matched_value: str                           # Value from the searched column
    matched_by: str                              # "en_name" | "ar_name" | "badge_no"
    fields: list[tuple[str, str]]                # (column heading, value)
    name_value: str = ""                         # Value from the sheet's name column

    @property
    def percent(self) -> int:
        return int(round(self.score * 100))

    def summary(self, max_fields: int = 3) -> str:
        """Compact preview of key columns other than the matched value."""
        parts: list[str] = []
        mv = self.matched_value.casefold()
        for k, v in self.fields:
            if v.casefold() == mv:
                continue
            parts.append(f"{k}: {v}")
            if len(parts) >= max_fields:
                break
        return " • ".join(parts)


# --------------------------------------------------------------------------- transliteration
def _transliterate_word(word: str) -> str:
    """Transliterate a single Arabic word to readable Latin title-case."""
    if not word:
        return ""
    if not has_arabic(word):
        return word.translate(_AR_TO_EN_DIGITS)

    # Preserve leading/trailing punctuation such as parentheses or hyphens
    m = re.match(r"^([^\w\u0600-\u06FF]*)(.*?)([^\w\u0600-\u06FF]*)$", word)
    if not m:
        return word
    lead, core, trail = m.groups()
    if not core or not has_arabic(core):
        return word.translate(_AR_TO_EN_DIGITS)

    # Handle hyphenated tokens like "ص-المجزر"
    if "-" in core:
        return lead + "-".join(_transliterate_word(p) for p in core.split("-")) + trail

    cleaned = _DIACRITICS.sub("", core).translate(_AR_TO_EN_DIGITS)
    folded = arabic_fold(cleaned)

    # Exact dictionary hit (fast, natural spelling)
    if folded in _COMMON_NAMES:
        return lead + _COMMON_NAMES[folded] + trail

    # "ال..." (Al-) prefix handling
    prefix = ""
    body = cleaned
    folded_body = folded
    if len(folded) > 3 and folded.startswith("ال"):
        rest = folded[2:]
        if rest in _COMMON_NAMES:
            return lead + "Al-" + _COMMON_NAMES[rest] + trail
        prefix = "Al-"
        body = cleaned[2:]
        folded_body = rest

    # Character-by-character phonetic transliteration
    out: list[str] = []
    n = len(body)
    i = 0
    while i < n:
        ch = body[i]
        # Handle "لا" ligature
        if ch == "ل" and i + 1 < n and body[i + 1] in ("ا", "أ", "إ", "آ"):
            out.append("la")
            i += 2
            continue
        # Context-aware "و" and "ي"
        if ch == "و":
            if i == 0:
                out.append("w")
            elif i == n - 1:
                out.append("o")
            else:
                out.append("oo" if body[i - 1] not in ("ا", "و", "ي") and i + 1 < n and body[i + 1] not in ("ا", "و", "ي") else "o")
            i += 1
            continue
        if ch in ("ي", "ی"):
            if i == 0:
                out.append("y")
            elif i == n - 1:
                out.append("i")
            else:
                out.append("i")
            i += 1
            continue
        if ch == "ا" and i == 0:
            out.append("a")
            i += 1
            continue
        mapped = _CHAR_MAP.get(ch, ch)
        out.append(mapped)
        i += 1

    res = "".join(out)
    # Collapse triple letters and tidy
    res = re.sub(r"([a-z])\1\1+", r"\1\1", res)
    if res:
        res = res[0].upper() + res[1:]
    return lead + prefix + res + trail


def transliterate_arabic(text: str) -> str:
    """Offline Arabic -> English transliteration suitable for names and badge job titles."""
    raw = (text or "").strip()
    if not raw:
        return ""
    tokens = raw.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        # Combine "عبد الله" -> "Abdullah", "عبد الرحمن" -> "Abdulrahman"
        folded = arabic_fold(_DIACRITICS.sub("", tokens[i]))
        if folded == "عبد" and i + 1 < len(tokens):
            nxt = arabic_fold(_DIACRITICS.sub("", tokens[i + 1]))
            if nxt == "الله":
                out.append("Abdullah")
                i += 2
                continue
            if nxt.startswith("ال") and nxt[2:] in _COMMON_NAMES:
                out.append("Abdul" + _COMMON_NAMES[nxt[2:]].lower())
                i += 2
                continue
        out.append(_transliterate_word(tokens[i]))
        i += 1
    return " ".join(p for p in out if p)


def ai_transliterate(text: str, cfg: llm.LlmConfig | None) -> str:
    """Optional AI-assisted transliteration using the configured UtilitiesHub LLM."""
    if not text.strip() or cfg is None or not cfg.enabled:
        return transliterate_arabic(text)
    prompt = (
        "Transliterate the following Arabic personal name into standard English Latin spelling. "
        "Return ONLY the English name with no quotes, explanation, or extra text.\n\n"
        f"Arabic name: {text.strip()}"
    )
    ans = llm.ask_text(cfg, prompt, system="You transliterate Arabic names to clean English spelling.",
                       task="extraction").strip()
    ans = ans.splitlines()[0].strip(" \"'`.-:")
    return ans or transliterate_arabic(text)


# --------------------------------------------------------------------------- badge OCR & parsing
def _clean_line(line: str) -> str:
    s = " ".join((line or "").split()).strip()
    if not s or _NOISE_LINE.match(s):
        return ""
    # Strip tiny isolated non-Arabic/non-digit garbage at edges
    s = s.strip("|~`^_=+\\/")
    return s.strip()


def _is_badge_number_line(line: str) -> bool:
    """True when a line looks like a badge / employee ID number (e.g. '64847', 'ID 64847')."""
    converted = line.translate(_AR_TO_EN_DIGITS)
    digits = re.sub(r"\D", "", converted)
    if len(digits) < 2:
        return False
    letters = re.findall(r"[A-Za-z\u0600-\u06FF]", converted)
    # Mostly digits (at most a tiny prefix like ID / No)
    return len(letters) <= 3 and len(digits) >= len(letters)


def _extract_digits(line: str) -> str:
    converted = (line or "").translate(_AR_TO_EN_DIGITS)
    m = re.findall(r"\d{2,}", converted)
    if m:
        return max(m, key=len)
    return re.sub(r"\D", "", converted)


def parse_badge_text(raw_text: str) -> BadgeData:
    """Parse OCR text from a 3-line badge into (ar_name, job_title, badge_no) + transliterations.

    Expected layout:
      Line 1: Arabic Name
      Line 2: Job Title
      Line 3: Badge Number
    Tolerates extra blank lines, stray OCR noise, and Arabic-Indic numerals.
    """
    raw_lines = [_clean_line(ln) for ln in (raw_text or "").splitlines()]
    # Filter out single-character punctuation/OCR specks unless they are meaningful
    lines = [ln for ln in raw_lines if ln and (len(ln) >= 2 or ln.isdigit())]

    ar_name = ""
    job_title = ""
    badge_no = ""

    if not lines:
        return BadgeData(raw_text=raw_text or "", lines=[])

    # 1. Look for badge number line (preferring the last numeric line, matching Line 3)
    num_idx: int | None = None
    for idx in range(len(lines) - 1, -1, -1):
        if _is_badge_number_line(lines[idx]):
            num_idx = idx
            badge_no = _extract_digits(lines[idx])
            break

    text_lines = [ln for idx, ln in enumerate(lines) if idx != num_idx]

    # 2. Map remaining lines: Line 1 -> Arabic name, Line 2 -> Job title
    if len(text_lines) >= 2:
        ar_name = text_lines[0]
        # If there are extra lines (e.g. wrapped job title), join them cleanly into job_title
        job_title = " ".join(text_lines[1:])
    elif len(text_lines) == 1:
        ar_name = text_lines[0]

    # 3. Fallback: if no standalone numeric line was found, check if a 3rd line had embedded digits
    if not badge_no and len(lines) >= 3:
        maybe = _extract_digits(lines[-1])
        if len(maybe) >= 3:
            badge_no = maybe

    en_name = transliterate_arabic(ar_name)
    en_job_title = transliterate_arabic(job_title)
    return BadgeData(
        ar_name=ar_name,
        en_name=en_name,
        job_title=job_title,
        en_job_title=en_job_title,
        badge_no=badge_no,
        raw_text=raw_text or "",
        lines=lines,
    )


def _prep_badge_image(image: Image.Image, crop_mode: str = "full", rotation: int = 0) -> Image.Image:
    im = ImageOps.exif_transpose(image)
    if rotation % 360:
        im = im.rotate(-rotation, expand=True, fillcolor=(255, 255, 255))
    if crop_mode == "right":
        # Skip the left portrait photo (~34% of badge width) so photo texture doesn't confuse OCR
        w, h = im.size
        im = im.crop((int(w * 0.34), 0, w, h))
    gray = im.convert("L")
    if min(gray.size) < 1000:
        scale = 2
        gray = gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.LANCZOS)
    elif max(gray.size) > 3200:
        gray.thumbnail((3200, 3200))
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = gray.filter(ImageFilter.SHARPEN)
    return gray


def _score_parsed(data: BadgeData) -> tuple[int, int, int]:
    """Rank two OCR passes (higher is better): prefers having Arabic text + badge no + >=2 lines."""
    score = 0
    if data.ar_name and has_arabic(data.ar_name):
        score += 3
    elif data.ar_name:
        score += 1
    if data.job_title:
        score += 2
    if data.badge_no and len(data.badge_no) >= 3:
        score += 3
    return (score, len(data.lines), len(data.ar_name) + len(data.job_title))


def _resolve_ocr_langs(cfg: ocr.OcrConfig, wanted_langs: str) -> tuple[str, list[str]]:
    """Return usable Tesseract language string + any warnings (e.g. if 'ara' pack is missing)."""
    warnings: list[str] = []
    wanted = (wanted_langs or "ara+eng").strip() or "ara+eng"
    have = set(ocr.installed_languages(cfg))
    if not have:
        return wanted, warnings
    parts = [p.strip() for p in wanted.split("+") if p.strip()]
    usable = [p for p in parts if p in have]
    missing = [p for p in parts if p not in have]
    if missing:
        warnings.append(
            f"Tesseract language pack(s) missing: {', '.join(missing)}. "
            f"Install Arabic ('ara') tessdata for Arabic badge OCR (installed: {', '.join(sorted(have))})."
        )
    return ("+".join(usable) if usable else wanted), warnings


def extract_badge(path: str | Path, cfg: ocr.OcrConfig, languages: str = "ara+eng",
                  crop_mode: str = "auto", rotation: int = 0) -> BadgeData:
    """Read a badge image, run OCR (Arabic + English), and parse name, job title, and badge number."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Badge image not found: {p}")

    with Image.open(p) as raw_im:
        raw_im.load()
        base_im = raw_im.copy()

    # Check if AI vision OCR is active and preferred
    badge_cfg = replace(cfg, languages=languages or "ara+eng")
    warnings: list[str] = []

    if badge_cfg.ai_active and badge_cfg.ai_mode in ("ai", "ai_rules"):
        try:
            im_for_ai = ImageOps.exif_transpose(base_im)
            if rotation % 360:
                im_for_ai = im_for_ai.rotate(-rotation, expand=True, fillcolor=(255, 255, 255))
            txt = ocr.ai_ocr_image(im_for_ai, badge_cfg)
            parsed = parse_badge_text(txt)
            parsed.ocr_source = "ai"
            if parsed.ar_name or parsed.badge_no:
                return parsed
        except Exception as exc:
            log.warning("Badge AI OCR failed, falling back to Tesseract: %s", exc)
            warnings.append(f"AI OCR unavailable ({exc}); used Tesseract.")

    usable_langs, lang_warns = _resolve_ocr_langs(badge_cfg, languages)
    warnings.extend(lang_warns)
    tess_cfg = replace(badge_cfg, languages=usable_langs)

    def run_pass(mode: str) -> BadgeData:
        prep = _prep_badge_image(base_im, crop_mode=mode, rotation=rotation)
        txt = ocr._tesseract_image(prep, tess_cfg, config="--psm 6")
        parsed = parse_badge_text(txt)
        parsed.ocr_source = f"tesseract:{usable_langs}:{mode}"
        return parsed

    if crop_mode == "full":
        result = run_pass("full")
    elif crop_mode == "right":
        result = run_pass("right")
    else:
        # Auto mode: try right text area first (avoids portrait noise), compare with full if incomplete
        right_res = run_pass("right")
        if _score_parsed(right_res)[0] >= 7:
            result = right_res
        else:
            full_res = run_pass("full")
            result = right_res if _score_parsed(right_res) >= _score_parsed(full_res) else full_res

    # Secondary digits pass if badge_no wasn't captured clearly
    if not result.badge_no:
        try:
            prep = _prep_badge_image(base_im, crop_mode="right" if crop_mode != "full" else "full", rotation=rotation)
            w, h = prep.size
            bottom = prep.crop((0, int(h * 0.55), w, h))
            dig_cfg = replace(tess_cfg, languages="eng")
            dig_txt = ocr._tesseract_image(bottom, dig_cfg, config="--psm 6 -c tessedit_char_whitelist=0123456789")
            found = _extract_digits(dig_txt)
            if len(found) >= 3:
                result.badge_no = found
        except Exception:
            pass

    result.warnings.extend(warnings)
    return result


# --------------------------------------------------------------------------- filename rendering
def badge_template_values(ar_name: str = "", en_name: str = "", job_title: str = "", badge_no: str = "",
                          en_job_title: str = "", matched_name: str = "",
                          original: Path | str | None = None) -> dict[str, str]:
    """Build placeholder dictionary for customizable badge filenames."""
    fields = {
        "ar_name": [ar_name.strip()] if ar_name.strip() else [],
        "en_name": [en_name.strip()] if en_name.strip() else [],
        "job_title": [job_title.strip()] if job_title.strip() else [],
        "en_job_title": [en_job_title.strip() or transliterate_arabic(job_title)] if (en_job_title.strip() or job_title.strip()) else [],
        "badge_no": [badge_no.strip()] if badge_no.strip() else [],
        "badge": [badge_no.strip()] if badge_no.strip() else [],
        "matched_name": [matched_name.strip()] if matched_name.strip() else [],
    }
    return base_values(fields, original=original)


def render_badge_stem(template: str, ar_name: str = "", en_name: str = "", job_title: str = "",
                      badge_no: str = "", en_job_title: str = "", matched_name: str = "",
                      original: Path | str | None = None) -> str:
    """Render and sanitize the output filename stem (without extension)."""
    tpl = (template or "").strip() or DEFAULT_FILENAME_TEMPLATE
    vals = badge_template_values(ar_name, en_name, job_title, badge_no, en_job_title, matched_name, original)
    try:
        raw = render(tpl, vals)
    except ValueError:
        raw = f"{ar_name}-{en_name}-{badge_no}-{job_title}"
    # Clean empty segments caused by missing fields (e.g. "A--B" -> "A-B")
    cleaned = re.sub(r"\s*-\s*(?:-\s*)+", "-", raw)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    stem = sanitize_filename(cleaned.strip(" -_.,"))
    if not stem:
        fallback = Path(original).stem if original else "badge"
        stem = sanitize_filename(fallback) or "badge"
    return stem


# --------------------------------------------------------------------------- Excel multi-match similarity lookup
def _load_table(path: str, sheet: str = "", header_row: int = 1) -> Table:
    p = Path(path)
    if not p.is_file():
        raise SheetError(f"The spreadsheet was not found: {path}")
    key = (str(p), sheet.strip(), max(1, int(header_row or 1)))
    mtime = p.stat().st_mtime
    cached = _TABLE_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    book = load_book(p)
    if sheet.strip() and sheet.strip() not in book.sheets:
        raise SheetError(f"The spreadsheet has no tab called '{sheet}'. Tabs: {', '.join(book.names)}")
    table = book.table(sheet.strip() or None, max(0, key[2] - 1))
    _TABLE_CACHE[key] = (mtime, table)
    return table


def _resolve_column(table: Table, col_name: str, fallback_to_badge: bool = False) -> int:
    wanted = (col_name or "").strip()
    if wanted:
        low = [h.casefold() for h in table.headers]
        if wanted.casefold() not in low:
            raise SheetError(f"The spreadsheet has no column called '{wanted}'. Columns: {', '.join(table.headers)}")
        return low.index(wanted.casefold())
    if fallback_to_badge:
        for i, h in enumerate(table.headers):
            if _BADGE_NO_HDR.search(h):
                return i
    return guess_name_column(table.headers)


def _name_similarity_score(query: str, candidate: str) -> float:
    """Compare two names (handles word order, partial names, typos, and Arabic variants)."""
    q = prep_name(query)
    c = prep_name(candidate)
    if not q.s or not c.s:
        return 0.0
    base = name_score(q, c)
    if base >= 0.99 or not q.tokens or not c.tokens:
        return base
    # Reward partial-name containment (e.g. badge has 2-3 words, Excel has 4 words)
    small, large = (q.tokens, c.tokens) if len(q.tokens) <= len(c.tokens) else (c.tokens, q.tokens)
    sig_small = [t for t in small if len(t) >= 2]
    if len(sig_small) >= 2:
        matched = sum(1 for t in sig_small if max((lev_ratio(t, o) for o in large), default=0.0) >= 0.82)
        if matched == len(sig_small) and len(large) > len(small):
            return max(base, 0.90)
        if matched >= 2:
            return min(0.95, max(base, 0.70 + 0.14 * (matched / len(sig_small))))
    return base


def _badge_no_score(query_no: str, cell_val: str) -> float:
    q = re.sub(r"\D", "", (query_no or "").translate(_AR_TO_EN_DIGITS))
    c = re.sub(r"\D", "", (cell_val or "").translate(_AR_TO_EN_DIGITS))
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if len(q) >= 4 and (q in c or c in q):
        return 0.85
    return lev_ratio(q, c) if len(q) >= 4 else 0.0


def search_badge_candidates(
    path: str,
    *,
    en_name: str = "",
    ar_name: str = "",
    badge_no: str = "",
    search_by: str = "en_name",
    sheet: str = "",
    search_column: str = "",
    header_row: int = 1,
    threshold: float = 0.55,
    limit: int = 10,
) -> list[CandidateMatch]:
    """Find ALL similar candidate rows in an Excel/CSV sheet, ranked by similarity percentage.

    Accounts for multiple similar names with different percentages so the user can review and pick.
    """
    table = _load_table(path, sheet, header_row)
    if not table.headers:
        raise SheetError("The spreadsheet is empty.")

    mode = (search_by or "en_name").strip().lower()
    name_col = _resolve_column(table, search_column if mode != "badge_no" else "", fallback_to_badge=False)
    badge_col = _resolve_column(table, search_column if mode == "badge_no" else "", fallback_to_badge=True)
    has_badge_col = bool(search_column.strip()) or any(_BADGE_NO_HDR.search(h) for h in table.headers)

    matches: list[CandidateMatch] = []
    for row, excel_row in zip(table.rows, table.excel_rows):
        name_val = cell_text(row[name_col]).strip() if name_col < len(row) else ""
        badge_val = cell_text(row[badge_col]).strip() if (has_badge_col and badge_col < len(row)) else ""

        best_score = 0.0
        matched_val = name_val
        matched_by = mode

        if mode == "en_name":
            if en_name.strip() and name_val:
                best_score = _name_similarity_score(en_name, name_val)
                matched_val = name_val
                matched_by = "en_name"
        elif mode == "ar_name":
            if ar_name.strip() and name_val:
                best_score = _name_similarity_score(ar_name, name_val)
                matched_val = name_val
                matched_by = "ar_name"
        elif mode == "badge_no":
            target_val = badge_val if has_badge_col else name_val
            if badge_no.strip() and target_val:
                best_score = _badge_no_score(badge_no, target_val)
                matched_val = target_val
                matched_by = "badge_no"
        else:  # auto: check badge_no exact/near, en_name, and ar_name — take highest
            if badge_no.strip() and has_badge_col and badge_val:
                s_id = _badge_no_score(badge_no, badge_val)
                if s_id > best_score:
                    best_score, matched_val, matched_by = s_id, badge_val, "badge_no"
            if en_name.strip() and name_val:
                s_en = _name_similarity_score(en_name, name_val)
                if s_en > best_score:
                    best_score, matched_val, matched_by = s_en, name_val, "en_name"
            if ar_name.strip() and name_val:
                s_ar = _name_similarity_score(ar_name, name_val)
                if s_ar > best_score:
                    best_score, matched_val, matched_by = s_ar, name_val, "ar_name"

        if best_score >= threshold:
            fields = [(h, cell_text(v).strip()) for h, v in zip(table.headers, row) if cell_text(v).strip()]
            matches.append(CandidateMatch(
                score=best_score,
                row=excel_row,
                matched_value=matched_val or name_val,
                matched_by=matched_by,
                fields=fields,
                name_value=name_val,
            ))

    matches.sort(key=lambda m: (-m.score, m.row))
    return matches[:max(1, limit)]


def has_ambiguous_matches(matches: list[CandidateMatch]) -> bool:
    """True when multiple similar candidates exist and should be manually verified by the user."""
    if len(matches) < 2:
        return False
    # Ambiguous if top match isn't an exact 100% badge_no match and there's a runner-up within 15%
    if matches[0].matched_by == "badge_no" and matches[0].score >= 0.99:
        return False
    return (matches[0].score - matches[1].score) <= 0.15 or len(matches) >= 2


def list_sheet_columns(path: str, sheet: str = "", header_row: int = 1) -> tuple[list[str], list[str]]:
    """Return (sheet_names, column_headers) for populating dropdowns in UI/Settings."""
    p = Path(path)
    if not p.is_file():
        return [], []
    book = load_book(p)
    table = book.table(sheet.strip() or None, max(0, max(1, int(header_row or 1)) - 1))
    return list(book.names), list(table.headers)


def _clean_cv_label(raw: str) -> str:
    spaced = re.sub(r"[_\-.]+", " ", raw or "")
    cleaned = _CV_NOISE.sub(" ", spaced)
    return " ".join(cleaned.split()).strip()


def find_candidate_cvs(
    folder: str | Path,
    *,
    matched_name: str = "",
    en_name: str = "",
    ar_name: str = "",
    badge_no: str = "",
    threshold: float = 0.55,
    limit: int = 10,
) -> list[tuple[float, Path]]:
    """Search `folder` recursively for CVs matching the candidate name or badge number."""
    root = Path(folder)
    if not root.is_dir():
        return []

    queries = list(dict.fromkeys(q.strip() for q in (matched_name, en_name, ar_name) if q and q.strip()))
    bn_digits = re.sub(r"\D", "", (badge_no or "").translate(_AR_TO_EN_DIGITS))
    results: list[tuple[float, Path]] = []

    for p in root.rglob("*"):
        if not p.is_file() or p.name.startswith(("~$", ".")) or p.suffix.lower() not in CV_EXTENSIONS:
            continue
        stem_clean = _clean_cv_label(p.stem)
        parent_clean = _clean_cv_label(p.parent.name) if p.parent != root else ""
        cand_texts = [s for s in (stem_clean, parent_clean, f"{parent_clean} {stem_clean}".strip()) if s]

        best = 0.0
        for q in queries:
            qp = prep_name(q)
            q_tokens = [t for t in qp.tokens if len(t) >= 2]
            for cand in cand_texts:
                s = _name_similarity_score(q, cand)
                if s < 0.92 and len(q_tokens) >= 2:
                    cp_tokens = prep_name(cand).tokens
                    if cp_tokens and all(max((lev_ratio(t, c) for c in cp_tokens), default=0.0) >= 0.85 for t in q_tokens):
                        s = max(s, 0.92)
                if s > best:
                    best = s

        if len(bn_digits) >= 3:
            raw_id_text = f"{p.stem} {p.parent.name if p.parent != root else ''}".translate(_AR_TO_EN_DIGITS)
            digit_groups = re.findall(r"\d+", raw_id_text)
            if bn_digits in digit_groups:
                best = max(best, 0.98)
            elif bn_digits in "".join(digit_groups):
                best = max(best, 0.92)

        if best >= threshold:
            results.append((best, p))

    results.sort(key=lambda item: (-item[0], item[1].name.casefold()))
    return results[:max(1, limit)]
