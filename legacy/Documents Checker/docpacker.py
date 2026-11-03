#!/usr/bin/env python3
"""
DocPacker - local document checker, organizer & merger.
Drop files -> OCR + requirement checks -> auto-arrange -> one combined PDF
with a cover page (name, title, phone, joining date) and a checklist.
Everything runs on your machine. Requirements are fully editable.
"""
import base64
import calendar
import datetime as dt
import difflib
import hashlib
import html
import io
import json
import os
import queue
import subprocess
import sys
import re
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:
    HAS_DND = False
try:
    import pymupdf as fitz  # PyMuPDF (new name)
except Exception:
    try:
        import fitz  # older PyMuPDF versions
    except Exception:
        fitz = None
try:
    from PIL import Image, ImageOps, ExifTags
except Exception:
    Image = None
try:
    import pytesseract
except Exception:
    pytesseract = None
try:
    import numpy as np
except Exception:
    np = None
try:
    import cv2
except Exception:
    cv2 = None

APP = "DocPacker"
CFG_PATH = Path(__file__).with_name("docpacker_config.json")
HIST_PATH = Path(__file__).with_name("docpacker_history.log")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
EXTS = {".pdf": "pdf", ".jpg": "image", ".jpeg": "image", ".png": "image",
        ".tif": "image", ".tiff": "image", ".bmp": "image", ".webp": "image"}
PART_RE = re.compile(r"^(.*)#pages=(\d+)-(\d+)$")   # key of a page range inside a PDF
ICON = {"ok": "✔", "fail": "✖", "warn": "⚠", "manual": "☐", "manual_ok": "☑", "skip": "○"}


# ----------------------------------------------------------------- config
def R(rid, name, **kw):
    d = dict(id=rid, name=name, enabled=True, required=True, kinds=["pdf", "image"],
             any=[], any_min=1, all=[], regex=[], hints=[], min_score=0.35,
             low_text_bonus=False, checks=[])
    d.update(kw)
    return d


def C(label, type_="regex", arg="", level="fail", compact=False):
    return dict(label=label, type=type_, arg=arg, level=level, compact=compact)


DEFAULT_REQS = [
    R("passport", "Passport copy (scanner, full page)",
      any=["passport", "passeport", "date of birth", "nationality", "surname", "given names"],
      any_min=2, regex=[r"P<[A-Z]{3}", r"[A-Z0-9<]{20,}<<"], hints=["passport", "pp"],
      checks=[C("Surname is not empty", "surname_filled"),
              C("Surname present in passport", "passport_surname"),
              C("Surname matches cover form", "surname_matches"),
              C("Scanned, not a phone photo", "no_exif_camera"),
              C("High enough resolution", "min_res", "1000", "warn"),
              C("MRZ (machine-readable zone) visible", "regex", r"<<", "warn"),
              C("MRZ check digits valid", "mrz_valid", level="warn"),
              C("Passport valid 6+ months after joining", "passport_expiry", "6")]),
    R("photo", "Half-length photo", kinds=["image"], min_score=0.3, low_text_bonus=True,
      hints=["photo", "half", "portrait", "selfie", "pic", "img"],
      checks=[C("Portrait orientation", "portrait", level="warn"),
              C("Face detected", "face", level="warn"),
              C("Min. resolution", "min_res", "600", "warn")]),
    R("bank", "Bank account letter",
      any=["iban", "swift", "account number", "bank", "confidential", "account number and iban confirmation"],
      any_min=3, hints=["bank", "iban", "account"],
      checks=[C("Subject: Account number and IBAN confirmation", "regex", r"account\s*number\s*and\s*iban\s*confirmation"),
              C("IBAN present", "regex", r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact=True),
              C("Swift / BIC present", "regex", r"swift|bic"),
              C("Account number present", "regex", r"account\s*(no|number|#)"),
              C("Date present", "regex", r"\d{1,2}[-/. ]\w{3,9}[-/. ]\d{2,4}|\w{3,9}\s+\d{1,2},?\s+\d{4}", "warn"),
              C("Confidentiality statement", "regex", r"confidential"),
              C("Name matches passport name", "name_in_text"),
              C("Surname matches passport", "surname_in_text"),
              C("Sample name/details NOT used", "no_sample_terms"),
              C("Issued within the last 90 days", "doc_age", "90", "warn"),
              C("Letterhead & logo", "manual"),
              C("Officer signature", "manual"),
              C("Official bank/branch stamp", "manual")]),
    R("medical", "Medical report (IGRA, HIV, HCV, HBsAg)",
      any=["igra", "hiv", "hcv", "hbsag", "medical report", "laboratory"], any_min=2,
      hints=["medical", "lab", "report", "health"],
      checks=[C("IGRA result", "regex", r"igra|interferon|quantiferon|qft|t[- ]?spot"),
              C("HIV result", "regex", r"\bhiv\b"),
              C("HCV result", "regex", r"\bhcv\b|hepatitis\s*c"),
              C("HBsAg result", "regex", r"hbs\s*ag|hepatitis\s*b"),
              C("Name matches passport name", "name_in_text", level="warn"),
              C("Surname matches passport", "surname_in_text", level="warn"),
              C("Issued within the last 180 days", "doc_age", "180", "warn")]),
]

# Every optional feature has its own on/off switch (Settings > Features).
FEATURES = [
    ("feat_preview", "Page preview and page picker (see pages, reassign them by hand)", True),
    ("feat_autorotate", "Auto-rotate sideways / upside-down scans (needs Tesseract)", True),
    ("feat_deskew", "Straighten slightly tilted scans (needs numpy)", True),
    ("feat_datecheck", "Date checks: passport expiry and document age", True),
    ("feat_mrz", "Validate the passport MRZ check digits", True),
    ("feat_autofill", "Auto-fill name, passport no., birth date and expiry from the passport", True),
    ("feat_compress", "Compress the output PDF to the target size (see General tab)", False),
    ("feat_profiles", "Saved applicants (profiles)", True),
    ("feat_batch", "Batch mode (one sub-folder per person)", True),
    ("feat_arabic", "Arabic / right-to-left text on the cover page", True),
    ("feat_dupes", "Duplicate and overlap detection", True),
    ("feat_undo", "Undo (Ctrl+Z) for assignments and list changes", True),
    ("feat_log", "History log file (docpacker_history.log)", True),
    ("feat_validate", "Check settings and Tesseract setup", True),
]

DEFAULT_CFG = {
    "settings": {
        "watch_folder": "", "output_folder": "", "tesseract_path": "", "ocr_lang": "eng",
        "ocr_dpi": 200, "max_ocr_pages": 8, "watch_interval": 3, "auto_scan_on_new": False,
        "cover_page": True, "cover_title": "Application Package", "cover_checklist": True,
        "bookmarks": True, "append_unmatched": False, "page_size": "A4", "page_margin": 24,
        "image_quality": 90, "output_name": "{name}_{date}", "fuzzy_name": True,
        "sample_terms": "", "write_report": True, "split_pdf": True, "replace_on_add": True, "clear_after_combine": True,
    },
    "profile": {"name": "", "surname": "", "title": "", "phone": "", "day": "10", "month": "Oct", "year": "2030",
                "passport_no": "", "dob": "", "passport_expiry": ""},
    "people": {},
    "reqs": DEFAULT_REQS,
}
DEFAULT_CFG["settings"].update({k: d for k, _, d in FEATURES})
DEFAULT_CFG["settings"].update({"target_size_mb": 5, "arabic_font": ""})
PROFILE_KEYS = list(DEFAULT_CFG["profile"])
CFG_VERSION = 3


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if CFG_PATH.exists():
        try:
            data = json.loads(CFG_PATH.read_text("utf-8"))
            cfg["settings"].update(data.get("settings", {}))
            cfg["profile"].update(data.get("profile", {}))
            if data.get("reqs"):
                cfg["reqs"] = data["reqs"]
                # v2 migration: old config files get the new "surname not empty" check once
                ver = int(data.get("version", 1))
                def add_check(rid, ctype, chk, pos=None):
                    for r in cfg["reqs"]:
                        if r.get("id") == rid and not any(c.get("type") == ctype for c in r.get("checks", [])):
                            lst = r.setdefault("checks", [])
                            lst.insert(len(lst) if pos is None else pos, chk)
                if ver < 2:
                    add_check("passport", "surname_filled", C("Surname is not empty", "surname_filled"), 0)
                if ver < 3:   # new optional checks (each also has its own switch in Settings > Features)
                    add_check("passport", "mrz_valid", C("MRZ check digits valid", "mrz_valid", level="warn"))
                    add_check("passport", "passport_expiry", C("Passport valid 6+ months after joining", "passport_expiry", "6"))
                    add_check("bank", "doc_age", C("Issued within the last 90 days", "doc_age", "90", "warn"),
                              next((i for i, c in enumerate(next((r for r in cfg["reqs"] if r.get("id") == "bank"), {}).get("checks", []))
                                    if c.get("type") == "manual"), None))
                    add_check("medical", "doc_age", C("Issued within the last 180 days", "doc_age", "180", "warn"))
            if isinstance(data.get("people"), dict):
                cfg["people"] = data["people"]
        except Exception:
            pass
    cfg["version"] = CFG_VERSION
    return cfg


def open_default(path):
    """Open a file with the application the operating system uses for it."""
    path = str(path)
    if hasattr(os, "startfile"):
        os.startfile(path)                                  # Windows
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def open_rgb(path):
    """Open an image, apply EXIF rotation, release the file handle, return an RGB copy."""
    src = io.BytesIO(path) if isinstance(path, (bytes, bytearray)) else path
    with Image.open(src) as im:
        img = ImageOps.exif_transpose(im)
        img.load()
        return img.convert("RGB")


# ------------------------------------------------------------- extraction
def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def ocr_image(img, s):
    if pytesseract is None:
        raise RuntimeError("pytesseract not installed")
    if s["tesseract_path"]:
        pytesseract.pytesseract.tesseract_cmd = s["tesseract_path"]
    img = img.copy()
    img.thumbnail((3200, 3200))
    return pytesseract.image_to_string(ImageOps.autocontrast(img.convert("L")), lang=s["ocr_lang"])


# ------------------------------------------------------- feature helpers
_MONTH_WORDS = {}
for _i, _m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september",
                         "october", "november", "december"], 1):
    _MONTH_WORDS[_m] = _i
    _MONTH_WORDS[_m[:3]] = _i
_MONTH_WORDS["sept"] = 9


def _mkdate(y, m, d):
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def find_dates(text, allow2=False):
    """All dates found in free text: 10-Oct-2026, 10 October 2026, October 10, 2026, 2026-10-10, 10/10/2026 (day first)."""
    out = []
    yr = r"(\d{4}|\d{2})" if allow2 else r"(\d{4})"
    fix = lambda y: (2000 + y if y < 70 else 1900 + y) if y < 100 else y
    for m in re.finditer(r"\b(\d{1,2})[\s\-/.]*([A-Za-z]{3,9})\.?[\s\-/.,]*" + yr + r"\b", text):
        mon = _MONTH_WORDS.get(m.group(2).lower())
        if mon:
            d = _mkdate(fix(int(m.group(3))), mon, int(m.group(1)))
            if d: out.append(d)
    for m in re.finditer(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text):
        mon = _MONTH_WORDS.get(m.group(1).lower())
        if mon:
            d = _mkdate(int(m.group(3)), mon, int(m.group(2)))
            if d: out.append(d)
    for m in re.finditer(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", text):
        d = _mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d: out.append(d)
    for m in re.finditer(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d = _mkdate(y, b, a) if b <= 12 else _mkdate(y, a, b)       # day first, unless that is impossible
        if d: out.append(d)
    return out


def add_months(d, n):
    y, mi = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return dt.date(y, mi, min(d.day, calendar.monthrange(y, mi)[1]))


def fmt_date(d):
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}" if d else ""


def joining_date(prof):
    try:
        return dt.date(int(prof["year"]), MONTHS.index(prof["month"]) + 1, int(prof["day"]))
    except Exception:
        return None


_DIGITFIX = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}


def _cd(s):
    """ICAO 9303 check digit (weights 7-3-1), or None if s has illegal characters."""
    t = 0
    for i, c in enumerate(s):
        v = 0 if c == "<" else int(c) if c.isdigit() else ord(c) - 55 if "A" <= c <= "Z" else None
        if v is None:
            return None
        t += v * (7, 3, 1)[i % 3]
    return t % 10


def parse_mrz(text):
    """Parse a passport MRZ (TD3, two lines of 44). Returns a dict or None. Validates all ICAO check digits."""
    flat = re.sub(r"\s+", "", text.upper().replace("«", "<"))
    cands = list(re.finditer(r"P<[A-Z]{3}[A-Z<]{39}", flat)) or list(re.finditer(r"P[A-Z<][A-Z]{3}[A-Z<]{39}", flat))
    if not cands:
        return None
    # the printed word "PASSPORT" can look like a line start: prefer the candidate that has a second MRZ line after it
    m = next((c for c in cands if re.search(r"[A-Z0-9<]{44}", flat[c.end():c.end() + 80])), cands[0])
    l1, rest = flat[m.start():m.start() + 44], flat[m.start() + 44:]
    parts = l1[5:].split("<<")
    surname = parts[0].replace("<", " ").strip()
    given = re.sub(r"\s+", " ", " ".join(parts[1:]).replace("<", " ")).strip()
    surname = re.sub(r"\s+", " ", surname)
    res = dict(surname=surname, given=given, full=(given + " " + surname).strip().title(), country=l1[2:5],
               number="", dob=None, expiry=None, sex="", checks=[], ok=False, has_line2=False)
    m2 = re.search(r"[A-Z0-9<]{44}", rest[:80])
    if not m2:
        return res
    l2 = m2.group(0)
    num = lambda s: "".join(_DIGITFIX.get(c, c) for c in s)
    dob, exp = num(l2[13:19]), num(l2[21:27])
    if not (dob.isdigit() and exp.isdigit()):
        return res
    res.update(has_line2=True, number=l2[0:9].replace("<", ""), sex=l2[20])
    yy = int(dob[:2])
    res["dob"] = _mkdate((2000 if yy <= dt.date.today().year % 100 else 1900) + yy, int(dob[2:4]), int(dob[4:6]))
    res["expiry"] = _mkdate(2000 + int(exp[:2]), int(exp[2:4]), int(exp[4:6]))
    chk = lambda body, digit: (lambda c: c is not None and num(digit).isdigit() and c == int(num(digit)))(_cd(body))
    pers_ok = (set(l2[28:42]) == {"<"} and l2[42] in "<0") or chk(l2[28:42], l2[42])
    res["checks"] = [("passport number", chk(l2[0:9], l2[9])), ("birth date", chk(dob, l2[19])),
                     ("expiry date", chk(exp, l2[27])), ("personal number", pers_ok),
                     ("composite", chk(l2[0:10] + l2[13:20] + l2[21:43], l2[43]))]
    res["ok"] = all(ok for _, ok in res["checks"])
    return res


def passport_expiry_date(text):
    mz = parse_mrz(text)
    if mz and mz["expiry"]:
        return mz["expiry"]
    lines = [l for l in text.splitlines() if l.strip()]
    for i, l in enumerate(lines):
        if re.search(r"expir|date of exp|valid until", l, re.I):
            for chunk in (l, lines[i + 1] if i + 1 < len(lines) else ""):
                ds = find_dates(chunk, allow2=True)
                if ds:
                    return max(ds)
    return None


def detect_rotation(img, s):
    """Clockwise degrees (90/180/270) that make a text page upright, else 0.
    Decided by reading: Tesseract's own orientation guess is unreliable on pages with little text, so we count
    the words it reads with high confidence in each orientation. Tesseract also reads text that is turned a quarter
    clockwise almost as well as upright text, so when two neighbouring orientations tie, the upright one is the one
    whose next quarter-turn is also readable. Photos without text score ~0 everywhere and are left alone."""
    if pytesseract is None:
        return 0
    try:
        if s.get("tesseract_path"):
            pytesseract.pytesseract.tesseract_cmd = s["tesseract_path"]
        small = img.copy()
        small.thumbnail((1400, 1400))
        res = {}

        def count(r):
            if r not in res:
                im = small if r == 0 else small.rotate(-r, expand=True, fillcolor=(255, 255, 255))
                d = pytesseract.image_to_data(ImageOps.autocontrast(im.convert("L")), lang=s.get("ocr_lang", "eng"),
                                              output_type=pytesseract.Output.DICT)
                res[r] = sum(1 for t, c in zip(d["text"], d["conf"]) if len(t.strip()) >= 3 and float(c) >= 60)
            return res[r]
        if count(0) >= 40 and 0.7 * count(0) <= count(90) <= count(0) / 0.7:   # both readable alike = upright (2 passes only)
            return 0
        for r in (90, 180, 270):
            count(r)
        mx = max(res.values())
        if mx < 8:
            return 0
        ready = {r for r, v in res.items() if v >= 0.7 * mx}
        if len(ready) == 1:
            return next(iter(ready))
        if len(ready) == 2:
            for x in ready:
                if (x + 90) % 360 in ready:
                    return x
        return 0
    except Exception:
        return 0


def looks_like_passport(text):
    return bool(re.search(r"passport|nationality|date of birth|surname|given names", text, re.I)) or text.count("<") >= 5


def ocr_mrz(img, s):
    """Second OCR pass with a restricted alphabet to read the passport MRZ (plain OCR turns the '<' filler into letters)."""
    if pytesseract is None:
        return ""
    try:
        if s.get("tesseract_path"):
            pytesseract.pytesseract.tesseract_cmd = s["tesseract_path"]
        im = img.copy()
        im.thumbnail((2400, 2400))
        txt = pytesseract.image_to_string(ImageOps.autocontrast(im.convert("L")), lang="eng",
                                          config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<")
        out = []
        for l in (re.sub(r"\s+", "", x) for x in txt.splitlines()):
            if len(l) >= 30:
                out.append(l.ljust(44, "<")[:44] if l.startswith("P<") and 40 <= len(l) <= 47 else l)
        return "\n".join(out)
    except Exception:
        return ""


def detect_skew(img):
    """Small tilt in degrees (PIL rotate() angle that straightens the text), 0 if none/unsure. Needs numpy."""
    if np is None:
        return 0.0
    try:
        g = img.convert("L")
        g.thumbnail((700, 700))
        a = np.asarray(g, dtype=np.float32)
        bw = Image.fromarray(((a < min(165.0, a.mean() - 20)) * 255).astype("uint8"))
        score_at = lambda ang: float(np.var(np.asarray(bw.rotate(ang, resample=Image.BILINEAR)).sum(axis=1)))
        base = score_at(0)
        if base <= 0:
            return 0.0
        best = max(((score_at(x), x) for x in np.arange(-8, 8.01, 0.5)))[1]
        best = max(((score_at(x), x) for x in np.arange(best - 0.5, best + 0.51, 0.1)))[1]
        return round(float(best), 1) if abs(best) >= 0.5 and score_at(best) > base * 1.05 else 0.0
    except Exception:
        return 0.0


def apply_fix(img, fix):
    """Apply the stored rotation / deskew correction to a PIL image."""
    if not fix:
        return img
    if fix.get("rot"):
        img = img.rotate(-fix["rot"], expand=True, fillcolor=(255, 255, 255))
    if fix.get("skew"):
        img = img.rotate(fix["skew"], expand=True, resample=Image.BICUBIC, fillcolor=(255, 255, 255))
    return img


def has_arabic(t):
    return any("\u0600" <= c <= "\u06ff" or "\u0750" <= c <= "\u077f" or "\ufb50" <= c <= "\ufdff" or "\ufe70" <= c <= "\ufeff"
               for c in t)


ARABIC_FONTS = [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\tahoma.ttf", r"C:\Windows\Fonts\segoeui.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf"]


def find_arabic_font(s):
    for p in [s.get("arabic_font", "")] + ARABIC_FONTS:
        if p and Path(p).is_file():
            return p
    return None


def shingles(text, k=3):
    w = norm(text).split()
    return {" ".join(w[i:i + k]) for i in range(max(0, len(w) - k + 1))}


def validate_settings(S):
    """Return [(level, message)] describing problems in the settings / environment. level: fail | warn."""
    out = []
    if pytesseract is None:
        out.append(("warn", "pytesseract is not installed - scanned files cannot be read (text PDFs still work)."))
    else:
        try:
            if S.get("tesseract_path"):
                if not Path(S["tesseract_path"]).is_file():
                    out.append(("fail", f"Tesseract path does not exist: {S['tesseract_path']}"))
                    raise RuntimeError
                pytesseract.pytesseract.tesseract_cmd = S["tesseract_path"]
            pytesseract.get_tesseract_version()
            have = set(pytesseract.get_languages(config=""))
            for lg in [x for x in str(S.get("ocr_lang", "eng")).split("+") if x]:
                if lg not in have:
                    out.append(("fail", f"OCR language '{lg}' is not installed in Tesseract (installed: {', '.join(sorted(have))})."))
        except RuntimeError:
            pass
        except Exception:
            out.append(("fail", "Tesseract OCR was not found. Install it (https://github.com/UB-Mannheim/tesseract/wiki) "
                                "or set 'Tesseract path' in Settings."))
    ints = {"ocr_dpi": (72, 600), "max_ocr_pages": (0, 500), "watch_interval": (1, 3600), "page_margin": (0, 200), "image_quality": (1, 100)}
    for k, (lo, hi) in ints.items():
        try:
            if not lo <= int(S[k]) <= hi:
                out.append(("fail", f"'{k}' must be between {lo} and {hi}."))
        except Exception:
            out.append(("fail", f"'{k}' must be a whole number."))
    if str(S.get("page_size", "")).lower() not in ("a4", "letter"):
        out.append(("fail", "Page size must be A4 or Letter."))
    try:
        str(S["output_name"]).format(name="x", date="x", title="x")
    except Exception:
        out.append(("fail", "Output file name may only use {name} {date} {title}."))
    for k, lbl in (("watch_folder", "Watch folder"), ("output_folder", "Output folder")):
        if S.get(k) and not Path(S[k]).is_dir():
            out.append(("fail", f"{lbl} does not exist: {S[k]}"))
    if S.get("feat_compress", False):
        try:
            if float(S.get("target_size_mb", 0)) <= 0:
                out.append(("fail", "Target size (MB) must be greater than 0 when compression is on."))
        except Exception:
            out.append(("fail", "Target size (MB) must be a number."))
    if S.get("feat_deskew", True) and np is None:
        out.append(("warn", "Deskew needs numpy (pip install numpy) - it will be skipped."))
    if S.get("feat_arabic", True) and not find_arabic_font(S):
        out.append(("warn", "No Arabic-capable font found - set 'Arabic font file' in Settings (e.g. C:\\Windows\\Fonts\\arial.ttf)."))
    return out


def page_image_meta(doc, page, text):
    """For photo-like pages (almost no text) grab the biggest embedded image."""
    meta = dict(w=0, h=0, data=None)
    if len(text.strip()) >= 80:
        return meta
    try:
        imgs = page.get_images(full=True)
        if imgs:
            best = max(imgs, key=lambda x: x[2] * x[3])
            ex = doc.extract_image(best[0])
            meta.update(w=ex["width"], h=ex["height"], data=ex["image"])
    except Exception:
        pass
    return meta


def extract(path, s):
    kind = EXTS.get(Path(path).suffix.lower(), "other")
    info = dict(kind=kind, text="", pages=1, w=0, h=0, exif_cam=False, error="", page_texts=[], page_meta=[],
                page_rot=[], fix={}, hash="")
    try:
        info["hash"] = hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except Exception:
        pass
    if kind == "pdf":
        doc = fitz.open(path)
        info["pages"] = len(doc)
        for i, page in enumerate(doc):
            t = page.get_text()
            prot = 0
            if len(t.strip()) < 30 and i < s["max_ocr_pages"]:
                try:
                    pix = page.get_pixmap(dpi=int(s["ocr_dpi"]))
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    if s.get("feat_autorotate", True):
                        prot = detect_rotation(img, s)             # sideways / upside-down page: read it upright
                        if prot:
                            img = img.rotate(-prot, expand=True, fillcolor=(255, 255, 255))
                    t += ocr_image(img, s)
                    if (s.get("feat_mrz", True) or s.get("feat_autofill", True)) and looks_like_passport(t):
                        mz = ocr_mrz(img, s)
                        if mz:
                            t = mz + "\n" + t
                except Exception as e:
                    info["error"] = f"OCR: {e}"
            info["page_rot"].append(prot)
            info["page_texts"].append(t)
            info["text"] += t + "\n"
            info["page_meta"].append(page_image_meta(doc, page, t))
        doc.close()
    elif kind == "image":
        try:
            with Image.open(path) as im0:
                ex = im0.getexif()
                info["exif_cam"] = any(ex.get(k) for k in (271, 272))  # Make / Model
        except Exception:
            pass
        img = open_rgb(path)
        fix = {}
        try:
            if s.get("feat_autorotate", True):      # Tesseract OSD fails on photos without text, so faces are left alone
                rot = detect_rotation(img, s)
                if rot:
                    img = apply_fix(img, {"rot": rot}); fix["rot"] = rot
            info["text"] = ocr_image(img, s)
            if (s.get("feat_mrz", True) or s.get("feat_autofill", True)) and looks_like_passport(info["text"]):
                mz = ocr_mrz(img, s)
                if mz:
                    info["text"] = mz + "\n" + info["text"]
            if s.get("feat_deskew", True) and len(info["text"].strip()) >= 80:   # only text documents, not photos
                sk = detect_skew(img)
                if sk:
                    fix["skew"] = sk
        except Exception as e:
            info["error"] = f"OCR: {e}"
        info["w"], info["h"] = img.size
        info["fix"] = fix
        info["page_texts"] = [info["text"]]
    return info


# ---------------------------------------------------------------- matching
def score(req, path, info):
    if req["kinds"] and info["kind"] not in req["kinds"]:
        return 0.0
    text, nt = info["text"], norm(info["text"])
    compact = nt.replace(" ", "")
    hits = sum(1 for k in req["any"] if norm(k) in nt or norm(k).replace(" ", "") in compact)
    for rx in req["regex"]:
        try:
            hits += 1 if re.search(rx, text) else 0
        except re.error:
            pass
    sc = 0.0
    if req["any"] or req["regex"]:
        sc += min(1.0, hits / max(1, req["any_min"])) * (0.6 if req["all"] else 1.0)
    if req["all"]:
        sc += 0.4 * sum(1 for k in req["all"] if norm(k) in nt) / len(req["all"])
    fn = norm(Path(path).stem)
    # hint must start at a word start, so "pp" no longer matches "WhatsApp"
    if any(re.search(r"(?<![a-z])" + re.escape(norm(h)), fn) for h in req["hints"] if norm(h)):
        sc += 0.4
    if req["low_text_bonus"] and info["kind"] == "image" and len(text.strip()) < 80:
        sc += 0.5
    return min(1.0, sc)


def extract_surname(text):
    """Return (surname, source). Empty surname means none could be found / passport shows none."""
    flat = re.sub(r"\s+", "", text.upper())
    m = re.search(r"P<([A-Z]{3})([A-Z<]{3,})", flat)           # MRZ line 1
    if m:
        rest = m.group(2)
        if rest.startswith("<<"):
            return "", "MRZ shows an EMPTY surname"
        sn = rest.split("<<")[0].replace("<", " ").strip()
        if len(sn) > 1:
            return sn, "MRZ"
    lines = [l.strip() for l in text.splitlines() if l.strip()]   # printed field
    for i, l in enumerate(lines):
        if re.search(r"surname|family name|apellidos?|\bnom\b", l, re.I):
            cand = re.sub(r"(?i)surname|family name|apellidos?|\bnom\b|[/:]", " ", l)
            words = re.findall(r"[A-Z][A-Z'\-]+", cand)
            if not words and i + 1 < len(lines):
                words = re.findall(r"[A-Z][A-Z'\-]+", lines[i + 1])
            if words:
                return " ".join(words), "printed field"
    return "", "no surname field detected"


def same_name(a, b, fuzzy=True):
    a, b = norm(a), norm(b)
    return bool(a and b) and (a == b or (fuzzy and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85))


def name_ok(name, text, fuzzy):
    toks = [t for t in norm(name).split() if len(t) > 1]
    if not toks:
        return None
    nt = norm(text)
    words = set(nt.split())
    for t in toks:
        if t in nt:
            continue
        if fuzzy and difflib.get_close_matches(t, words, n=1, cutoff=0.85):
            continue
        return False
    return True


def run_check(ch, info, prof, s):
    t, k = ch["type"], info["kind"]
    if t == "regex":
        txt = re.sub(r"\s+", "", info["text"]).upper() if ch.get("compact") else info["text"]
        try:
            ok = bool(re.search(ch["arg"], txt, re.I))
        except re.error as e:
            return "warn", f"bad regex: {e}"
        return ("ok", "") if ok else (ch["level"], "not found in text")
    if t == "surname_filled":
        want = prof["surname"].strip()
        if want:
            return "ok", want
        return ch["level"], "surname is empty (fill it in or check the passport MRZ)"

    if t == "mrz_valid":
        if not s.get("feat_mrz", True):
            return "skip", "switched off in Settings"
        mz = parse_mrz(info["text"])
        if not mz:
            return ch["level"], "MRZ not found"
        if not mz["has_line2"]:
            return ch["level"], "MRZ second line not readable"
        bad = [n for n, ok in mz["checks"] if not ok]
        return ("ok", "all check digits valid") if not bad else \
               (ch["level"], "check digit wrong: " + ", ".join(bad) + " (OCR misread or altered passport)")
    if t == "passport_expiry":
        if not s.get("feat_datecheck", True):
            return "skip", "switched off in Settings"
        exp, jd = passport_expiry_date(info["text"]), joining_date(prof)
        if not exp:
            return ch["level"], "could not read the expiry date"
        if not jd:
            return "skip", "joining date is invalid"
        need = add_months(jd, int(ch["arg"] or 6))
        return ("ok", f"expires {fmt_date(exp)}") if exp >= need else \
               (ch["level"], f"expires {fmt_date(exp)}; must be valid until {fmt_date(need)}")
    if t == "doc_age":
        if not s.get("feat_datecheck", True):
            return "skip", "switched off in Settings"
        today = dt.date.today()
        ds = [d for d in find_dates(info["text"]) if d <= today + dt.timedelta(days=2)]
        if not ds:
            return ch["level"], "no date found in the document"
        newest, limit = max(ds), int(ch["arg"] or 90)
        age = (today - newest).days
        return ("ok", f"dated {fmt_date(newest)} ({age} days ago)") if age <= limit else \
               (ch["level"], f"dated {fmt_date(newest)} - {age} days ago, limit {limit}")
    if t == "no_exif_camera":
        if k != "image":
            return "ok", "PDF"
        return ("fail", "camera EXIF found - looks like a phone photo") if info["exif_cam"] else ("ok", "no camera EXIF")
    if t == "min_res":
        if k != "image":
            return "skip", "n/a for PDF"
        m = min(info["w"], info["h"])
        return ("ok", f"{m}px") if m >= int(ch["arg"] or 0) else (ch["level"], f"{m}px < {ch['arg']}px")
    if t == "portrait":
        if k != "image":
            return "skip", "n/a"
        return ("ok", "") if info["h"] >= info["w"] else (ch["level"], f"{info['w']}x{info['h']} is landscape")
    if t == "face":
        if cv2 is None or np is None or k != "image":
            return "skip", "OpenCV not installed"
        try:
            g = cv2.cvtColor(np.array(open_rgb(info.get("img_bytes") or info["path"])), cv2.COLOR_RGB2GRAY)
            cc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            n = len(cc.detectMultiScale(g, 1.1, 5))
            return ("ok", f"{n} face(s)") if n == 1 else (ch["level"], f"{n} faces found")
        except Exception as e:
            return "skip", str(e)
    if t == "passport_surname":
        sn, src = extract_surname(info["text"])
        return ("ok", f"{sn} ({src})") if sn else (ch["level"], src)
    if t == "surname_matches":
        sn, src = extract_surname(info["text"])
        want = prof["surname"].strip()
        if not want:
            return ch["level"], "surname is empty on the cover form"
        if not sn:
            return "warn", "could not read surname from passport"
        if not same_name(sn, want, s["fuzzy_name"]):
            return ch["level"], f"passport says '{sn}', form says '{want}'"
        if prof["name"].strip() and norm(want) not in norm(prof["name"]):
            return "warn", "surname is not part of the full name field"
        return "ok", f"{sn} matches"
    if t == "surname_in_text":
        want = prof["surname"].strip()
        if not want:
            return ch["level"], "surname is empty on the cover form"
        r = name_ok(want, info["text"], s["fuzzy_name"])
        if r is None:
            return "skip", "surname too short to check"
        return ("ok", "") if r else (ch["level"], f"'{want}' not found in document")
    if t == "name_in_text":
        r = name_ok(prof["name"], info["text"], s["fuzzy_name"])
        if r is None:
            return "skip", "enter full name first"
        return ("ok", "") if r else (ch["level"], "name not found in document")
    if t == "no_sample_terms":
        terms = [x.strip() for x in s["sample_terms"].split(",") if x.strip()]
        if not terms:
            return "skip", "add sample terms in Settings"
        found = [x for x in terms if norm(x) in norm(info["text"])]
        return ("fail", f"sample data found: {', '.join(found)}") if found else ("ok", "")
    if t == "min_pages":
        return ("ok", "") if info["pages"] >= int(ch["arg"] or 1) else (ch["level"], f"{info['pages']} page(s)")
    return "skip", "unknown check"


# ------------------------------------------------------------------- app
class App:
    def __init__(self):
        self.root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
        self.root.title(f"{APP} - document checker & packer")
        self.root.geometry("1180x760")
        # show unexpected errors in a dialog instead of only printing a traceback in the console
        self.root.report_callback_exception = lambda exc, val, tb: (
            __import__("traceback").print_exception(exc, val, tb),
            messagebox.showerror(APP, f"{exc.__name__}: {val}"))
        self.cfg = load_cfg()
        self.S, self.P = self.cfg["settings"], self.cfg["profile"]
        self.files, self.info, self.manual = [], {}, {}
        self.manual_ok, self.results, self.known_in_folder = set(), {}, set()
        self.q, self.busy = queue.Queue(), False
        self.items, self.lookup, self.part_keys = {}, {}, set()
        self.undo_stack, self.warnings, self.setup_issues = [], [], []
        self.thumbs, self._arch, self._arch_font = [], None, None
        self.build_ui()
        self.apply_features()
        self.load_history()
        self.refresh_reqs()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.bind("<Control-z>", lambda e: self.undo())
        self.root.bind("<Control-Key>", self.layout_free_shortcuts)
        self.root.bind_class("TEntry", "<Button-3>", self.entry_menu)
        self.root.bind_class("Entry", "<Button-3>", self.entry_menu)
        self.root.after(100, self.pump)
        self.root.after(1000, self.watch_tick)
        if self.S["watch_folder"] and Path(self.S["watch_folder"]).is_dir():
            self.poll_folder(initial=True)
        missing = [n for n, m in (("PyMuPDF", fitz), ("Pillow", Image)) if m is None]
        if missing:
            messagebox.showerror(APP, "Missing: " + ", ".join(missing) + "\nRun: pip install -r requirements.txt")
        elif pytesseract is None:
            self.status("pytesseract missing - OCR disabled (text PDFs still work).")
        self.check_setup(startup=True)

    def layout_free_shortcuts(self, e):
        """Make Ctrl+V/C/X/A work on non-Latin keyboard layouts (Windows key codes)."""
        if e.keysym.lower() in ("v", "c", "x", "a", "z"):   # layout already works, Tk handles it
            return
        w = e.widget
        if not isinstance(w, (tk.Entry, ttk.Entry, tk.Text)):
            return
        ev = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>"}.get(e.keycode)
        if ev:
            w.event_generate(ev)
            return "break"
        if e.keycode == 65 and not isinstance(w, tk.Text):   # select all
            w.selection_range(0, "end"); w.icursor("end")
            return "break"
        if e.keycode == 90:
            self.undo()
            return "break"

    def entry_menu(self, e):
        """Right-click Cut / Copy / Paste menu for every entry box."""
        w = e.widget
        w.focus_set()
        m = tk.Menu(self.root, tearoff=0)
        for lbl, ev in (("Cut", "<<Cut>>"), ("Copy", "<<Copy>>"), ("Paste", "<<Paste>>")):
            m.add_command(label=lbl, command=lambda ev=ev: w.event_generate(ev))
        m.tk_popup(e.x_root, e.y_root)

    # ---- UI
    def build_ui(self):
        r = self.root
        top = ttk.LabelFrame(r, text="Cover page (first page of the final PDF)")
        top.pack(fill="x", padx=8, pady=6)
        self.v = {k: tk.StringVar(value=self.P[k]) for k in self.P}
        for i, (lbl, key, w) in enumerate([("Full name (as in passport)", "name", 26), ("Title", "title", 22), ("Phone", "phone", 16)]):
            ttk.Label(top, text=lbl).grid(row=0, column=i * 2, sticky="e", padx=(8, 2), pady=4)
            ttk.Entry(top, textvariable=self.v[key], width=w).grid(row=0, column=i * 2 + 1, padx=2)
        ttk.Label(top, text="Surname (auto-filled from passport)").grid(row=1, column=0, sticky="e", padx=(8, 2), pady=4)
        ttk.Entry(top, textvariable=self.v["surname"], width=26).grid(row=1, column=1, padx=2)
        for i, (lbl, key, w) in enumerate([("Passport no.", "passport_no", 14), ("Date of birth", "dob", 13), ("Passport expiry", "passport_expiry", 13)]):
            ttk.Label(top, text=lbl).grid(row=1, column=2 + i * 2, sticky="e", padx=(8, 2), pady=4)
            ttk.Entry(top, textvariable=self.v[key], width=w).grid(row=1, column=3 + i * 2, padx=2)
        self.prof_frame = ttk.Frame(top)
        self.prof_frame.grid(row=2, column=0, columnspan=11, sticky="w", padx=8, pady=(0, 4))
        ttk.Label(self.prof_frame, text="Saved applicants:").pack(side="left")
        self.people_cb = ttk.Combobox(self.prof_frame, width=28, state="readonly")
        self.people_cb.pack(side="left", padx=4)
        self.people_cb.bind("<<ComboboxSelected>>", self.load_profile)
        for txt, cmd in (("Save", self.save_profile), ("New applicant", self.new_applicant), ("Delete", self.delete_profile)):
            ttk.Button(self.prof_frame, text=txt, command=cmd, width=13 if len(txt) > 6 else 7).pack(side="left", padx=2)
        self.refresh_people()
        ttk.Label(top, text="Joining date").grid(row=0, column=6, padx=(12, 2))
        ttk.Spinbox(top, from_=1, to=31, width=3, textvariable=self.v["day"], format="%02.0f").grid(row=0, column=7)
        ttk.Combobox(top, values=MONTHS, width=5, textvariable=self.v["month"], state="readonly").grid(row=0, column=8, padx=2)
        ttk.Spinbox(top, from_=2024, to=2050, width=6, textvariable=self.v["year"]).grid(row=0, column=9)
        self.date_lbl = ttk.Label(top, text="", font=("TkDefaultFont", 10, "bold"))
        self.date_lbl.grid(row=0, column=10, padx=10)
        for k in self.v:
            self.v[k].trace_add("write", lambda *a: self.on_profile())
        self.on_profile()

        bar = ttk.Frame(r)
        bar.pack(fill="x", padx=8)
        self.btn = {}
        for i, (key, txt, cmd) in enumerate([
                ("watch", "📁 Watch folder…", self.pick_folder), ("add", "➕ Add files…", self.add_dialog),
                ("scan", "▶ Scan & Check", self.scan), ("rescan", "⟳ Re-scan all", lambda: self.scan(True)),
                ("combine", "📄 Combine → PDF", self.combine), ("batch", "🗂 Batch…", self.batch_run),
                ("report", "📝 Export report", self.export_report), ("clearf", "🧹 Clear fields", self.clear_fields),
                ("undo", "↶ Undo", self.undo),
                ("settings", "⚙ Settings", self.settings_dialog)]):
            self.btn[key] = ttk.Button(bar, text=txt, command=cmd)
            self.btn[key].grid(row=0, column=i, padx=2, pady=4)
        self.replace_var = tk.BooleanVar(value=bool(self.S.get("replace_on_add", True)))
        self.replace_var.trace_add("write", lambda *a: self.S.__setitem__("replace_on_add", self.replace_var.get()))
        ttk.Checkbutton(bar, text="New files replace the previous ones", variable=self.replace_var).grid(row=1, column=0, columnspan=4, sticky="w", padx=4)
        self.clear_after_var = tk.BooleanVar(value=bool(self.S.get("clear_after_combine", True)))
        self.clear_after_var.trace_add("write", lambda *a: self.S.__setitem__("clear_after_combine", self.clear_after_var.get()))
        ttk.Checkbutton(bar, text="Clear files & fields after saving the PDF", variable=self.clear_after_var).grid(row=1, column=4, columnspan=4, sticky="w", padx=4)
        self.drop = ttk.Label(r, relief="groove", anchor="center", padding=10,
                              text="⬇ Drop all your files / folders here" if HAS_DND else
                              "Drag & drop needs: pip install tkinterdnd2  (use ‘Add files’ or the watch folder meanwhile)")
        self.drop.pack(fill="x", padx=8, pady=4)
        if HAS_DND:
            for w in (self.drop, r):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", lambda e: self.add_paths(self.root.tk.splitlist(e.data), fresh=True))

        pw = ttk.PanedWindow(r, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=8, pady=4)
        left = ttk.Frame(pw)
        pw.add(left, weight=3)
        cols = ("st", "req", "file", "sc")
        self.tr = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c, t, w in zip(cols, ("Status", "Requirement (in output order)", "Matched file", "Match"), (90, 270, 220, 55)):
            self.tr.heading(c, text=t)
            self.tr.column(c, width=w, anchor="w")
        for tag, col in (("ok", "#1b7f3b"), ("issues", "#b45309"), ("missing", "#b91c1c"), ("off", "#888888"), ("confirm", "#1d4ed8")):
            self.tr.tag_configure(tag, foreground=col)
        self.tr.pack(fill="both", expand=True)
        self.tr.bind("<<TreeviewSelect>>", lambda e: self.show_detail())
        self.tr.bind("<Double-1>", self.open_selected_req)
        rb = ttk.Frame(left)
        rb.pack(fill="x")
        for i, (key, txt, cmd) in enumerate([
                ("assign", "Assign file…", self.assign_file), ("unassign", "Unassign", self.unassign),
                ("pages", "Pages…", self.pages_for_selected), ("up", "▲", lambda: self.move(-1)),
                ("down", "▼", lambda: self.move(1)), ("add", "Add req.", lambda: self.req_dialog()),
                ("edit", "Edit", self.edit_req), ("del", "Delete", self.del_req), ("toggle", "On/Off", self.toggle_req)]):
            self.btn["r_" + key] = ttk.Button(rb, text=txt, command=cmd, width=8 if len(txt) > 2 else 3)
            self.btn["r_" + key].grid(row=0, column=i, padx=1, pady=3)

        self.nb = nb = ttk.Notebook(pw)
        pw.add(nb, weight=4)
        t1 = ttk.Frame(nb); nb.add(t1, text="Checks (double-click ☐ to confirm manually)")
        self.ck = ttk.Treeview(t1, columns=("s", "c", "d"), show="headings")
        for c, t, w in (("s", "", 30), ("c", "Check", 270), ("d", "Detail", 230)):
            self.ck.heading(c, text=t); self.ck.column(c, width=w)
        self.ck.pack(fill="both", expand=True)
        self.ck.bind("<Double-1>", self.toggle_manual)
        t2 = ttk.Frame(nb); nb.add(t2, text="OCR text")
        self.txt = scrolledtext.ScrolledText(t2, wrap="word", height=10)
        self.txt.pack(fill="both", expand=True)
        t3 = ttk.Frame(nb); nb.add(t3, text="All files")
        self.fl = ttk.Treeview(t3, columns=("f", "k", "p", "u"), show="headings")
        for c, t, w in (("f", "File", 230), ("k", "Type", 50), ("p", "Pages", 50), ("u", "Used for", 200)):
            self.fl.heading(c, text=t); self.fl.column(c, width=w)
        self.fl.pack(fill="both", expand=True)
        self.fl.bind("<Double-1>", self.open_selected_file)
        fb = ttk.Frame(t3); fb.pack(pady=3)
        ttk.Button(fb, text="Remove selected from list", command=self.remove_files).pack(side="left", padx=4)
        self.btn["f_pages"] = ttk.Button(fb, text="Pages…", command=self.pages_for_file)
        self.btn["f_pages"].pack(side="left", padx=4)

        self.t_preview = ttk.Frame(nb); nb.add(self.t_preview, text="Preview")
        vbar = ttk.Scrollbar(self.t_preview, orient="vertical")
        vbar.pack(side="right", fill="y")
        self.prev_canvas = tk.Canvas(self.t_preview, height=300, highlightthickness=0, yscrollcommand=vbar.set)
        vbar.config(command=self.prev_canvas.yview)
        self.prev_canvas.pack(side="left", fill="both", expand=True)
        self.prev_inner = ttk.Frame(self.prev_canvas)
        self.prev_win = self.prev_canvas.create_window((0, 0), window=self.prev_inner, anchor="nw")
        self.prev_cells = []
        self.prev_canvas.bind("<Configure>", lambda e: self.reflow_preview())
        self.prev_inner.bind("<Configure>", lambda e: self.fit_preview())

        def wheel(e):                      # works wherever the pointer is inside the Preview tab
            if not str(e.widget).startswith(str(self.t_preview)):
                return
            up = getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4
            self.prev_canvas.yview_scroll(-3 if up else 3, "units")
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(ev, wheel, add="+")
        self.t_warn = ttk.Frame(nb); nb.add(self.t_warn, text="Warnings")
        self.warn_box = scrolledtext.ScrolledText(self.t_warn, wrap="word", height=10)
        self.warn_box.pack(fill="both", expand=True)
        self.t_hist = ttk.Frame(nb); nb.add(self.t_hist, text="History")
        self.hist_box = scrolledtext.ScrolledText(self.t_hist, wrap="word", height=10)
        self.hist_box.pack(fill="both", expand=True)

        bot = ttk.Frame(r)
        bot.pack(fill="x", padx=8, pady=4)
        self.pb = ttk.Progressbar(bot, length=200)
        self.pb.pack(side="left")
        self.st = ttk.Label(bot, text="Ready.")
        self.st.pack(side="left", padx=10)

    def status(self, t):
        self.st.config(text=t)

    def on_profile(self):
        for k in self.P:
            self.P[k] = self.v[k].get()
        d = self.joining()
        self.date_lbl.config(text=d or "invalid date", foreground="#1b7f3b" if d else "#b91c1c")

    def joining(self):
        try:
            d = dt.date(int(self.P["year"]), MONTHS.index(self.P["month"]) + 1, int(self.P["day"]))
            return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"
        except Exception:
            return ""

    # ---- optional features: switches
    def feat(self, key):
        return bool(self.S.get(key, True))

    def apply_features(self):
        """Show / hide every optional piece of the UI according to Settings > Features."""
        show = lambda w, on: w.grid() if on else w.grid_remove()
        show(self.prof_frame, self.feat("feat_profiles"))
        show(self.btn["batch"], self.feat("feat_batch"))
        show(self.btn["undo"], self.feat("feat_undo"))
        show(self.btn["r_pages"], self.feat("feat_preview"))
        self.btn["f_pages"].pack() if self.feat("feat_preview") else self.btn["f_pages"].pack_forget()
        tab = lambda f, on: self.nb.tab(f, state="normal" if on else "hidden")
        tab(self.t_preview, self.feat("feat_preview"))
        tab(self.t_warn, self.feat("feat_dupes") or self.feat("feat_validate"))
        tab(self.t_hist, self.feat("feat_log"))
        if not self.feat("feat_undo"):
            self.undo_stack.clear()

    # ---- optional features: undo and history
    def snapshot(self, why):
        if not self.feat("feat_undo"):
            return
        self.undo_stack.append(dict(why=why, files=list(self.files), info=dict(self.info), manual=dict(self.manual),
                                    manual_ok=set(self.manual_ok), reqs=json.loads(json.dumps(self.cfg["reqs"]))))
        del self.undo_stack[:-30]

    def undo(self):
        if not self.feat("feat_undo"):
            return
        if not self.undo_stack:
            self.status("Nothing to undo."); return
        s = self.undo_stack.pop()
        self.files, self.info, self.manual, self.manual_ok = s["files"], s["info"], s["manual"], s["manual_ok"]
        self.cfg["reqs"] = s["reqs"]
        self.log(f"Undo: {s['why']}")
        self.evaluate()
        self.status(f"Undid: {s['why']}")

    def log(self, msg):
        if not self.feat("feat_log"):
            return
        line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
        try:
            self.hist_box.insert("end", line + "\n"); self.hist_box.see("end")
            with open(HIST_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def load_history(self):
        try:
            if self.feat("feat_log") and HIST_PATH.exists():
                self.hist_box.insert("end", "\n".join(HIST_PATH.read_text("utf-8").splitlines()[-150:]) + "\n")
                self.hist_box.see("end")
        except Exception:
            pass

    # ---- optional features: setup check, warnings, duplicates
    def check_setup(self, startup=False, show=False):
        self.setup_issues = validate_settings(self.S) if self.feat("feat_validate") else []
        self.refresh_warnings()
        if show:
            msg = "\n".join(("✖ " if l == "fail" else "⚠ ") + m for l, m in self.setup_issues) or "Everything looks fine."
            messagebox.showinfo(APP, "Setup check:\n\n" + msg)
        elif startup and self.setup_issues:
            self.status(f"{len(self.setup_issues)} setup issue(s) - see the Warnings tab.")

    def find_warnings(self):
        W = []
        if not self.feat("feat_dupes"):
            return W
        names = {r["id"]: r["name"] for r in self.cfg["reqs"]}
        by_hash = {}
        for p in self.files:
            h = self.info.get(p, {}).get("hash")
            if h:
                by_hash.setdefault(h, []).append(p)
        for ps in by_hash.values():
            if len(ps) > 1:
                W.append(("warn", "Identical files: " + ", ".join(Path(x).name for x in ps)))
        spans = []
        for rid, res in self.results.items():
            f = res.get("file")
            if f:
                m = PART_RE.match(f)
                base = m.group(1) if m else f
                a, b = (int(m.group(2)), int(m.group(3))) if m else (1, (self.info.get(base) or {}).get("pages", 1) or 1)
                spans.append((rid, base, a, b))
        for i in range(len(spans)):
            for j in range(i + 1, len(spans)):
                A, B = spans[i], spans[j]
                if A[1] == B[1] and A[2] <= B[3] and B[2] <= A[3]:
                    W.append(("warn", f"The same pages are used for '{names.get(A[0], A[0])}' and '{names.get(B[0], B[0])}' ({Path(A[1]).name})"))
        items = [(k, v, shingles(v["text"])) for k, v in self.items.items() if len(norm(v["text"])) > 300]
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (ka, va, sa), (kb, vb, sb) = items[i], items[j]
                if self.base(ka) == self.base(kb) or (va.get("hash") and va.get("hash") == vb.get("hash")):
                    continue
                if sa and sb and len(sa & sb) / len(sa | sb) >= 0.8:
                    W.append(("warn", f"'{self.label(ka)}' and '{self.label(kb)}' have almost the same text (duplicate document?)"))
        used = {self.base(res["file"]) for res in self.results.values() if res.get("file")}
        for p in self.files:
            if p in self.info and p not in used:
                W.append(("info", f"Not used in the package: {Path(p).name}"))
        return W

    def refresh_warnings(self):
        lines = [("✖ " if l == "fail" else "⚠ ") + "Setup: " + m for l, m in self.setup_issues]
        lines += [("⚠ " if l == "warn" else "ℹ ") + m for l, m in self.warnings]
        self.warn_box.delete("1.0", "end")
        self.warn_box.insert("1.0", "\n".join(lines) or "No warnings.")
        n = sum(1 for l, _ in self.warnings if l == "warn") + len(self.setup_issues)
        self.nb.tab(self.t_warn, text=f"Warnings ({n})" if n else "Warnings")

    # ---- optional features: page preview and page picker
    def thumb(self, path, page, width):
        """tk.PhotoImage of one page (0-based) of a PDF, or of an image file, as it will appear in the output."""
        if EXTS.get(Path(path).suffix.lower()) == "pdf":
            doc = fitz.open(path)
            try:
                pg = doc[page]
                rots = (self.info.get(path) or {}).get("page_rot") or []
                if page < len(rots) and rots[page]:
                    pg.set_rotation((pg.rotation + rots[page]) % 360)
                z = width / max(1.0, pg.rect.width)
                data = pg.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("png")
            finally:
                doc.close()
        else:
            img = apply_fix(open_rgb(path), (self.info.get(path) or {}).get("fix"))
            img.thumbnail((width, width * 2))
            buf = io.BytesIO(); img.save(buf, "PNG"); data = buf.getvalue()
        return tk.PhotoImage(data=base64.b64encode(data))

    def fit_preview(self):
        w = max(1, self.prev_canvas.winfo_width())
        self.prev_canvas.configure(scrollregion=(0, 0, w, max(self.prev_inner.winfo_reqheight(), 1)))

    def reflow_preview(self):
        """Wrap the thumbnails to the width of the tab, so only up/down scrolling is ever needed."""
        w = max(1, self.prev_canvas.winfo_width())
        self.prev_canvas.itemconfigure(self.prev_win, width=w)
        cols = max(1, w // 230)
        for n, f in enumerate(self.prev_cells):
            f.grid(row=n // cols, column=n % cols, padx=4, pady=4, sticky="n")
        self.prev_inner.update_idletasks()
        self.fit_preview()

    def show_preview(self, res):
        for w in self.prev_inner.winfo_children():
            w.destroy()
        self.thumbs, self.prev_cells = [], []
        if not (self.feat("feat_preview") and res and res.get("file")):
            return
        key = res["file"]
        m = PART_RE.match(key)
        base = m.group(1) if m else key
        inf = self.info.get(base) or {}
        is_pdf = inf.get("kind") == "pdf"
        if is_pdf:
            a, b = (int(m.group(2)), int(m.group(3))) if m else (1, inf.get("pages", 1))
            pages = list(range(a - 1, min(b, a + 11)))
        else:
            pages = [0]
        for i in pages:
            try:
                im = self.thumb(base, i, 210)
            except Exception:
                continue
            self.thumbs.append(im)
            f = ttk.Frame(self.prev_inner)
            self.prev_cells.append(f)
            lb = ttk.Label(f, image=im, cursor="hand2"); lb.pack()
            cap = ttk.Label(f, text=(f"page {i + 1}" if is_pdf else Path(base).name) + "  (double-click to open)")
            cap.pack()
            for wdg in (f, lb, cap):
                wdg.bind("<Double-1>", lambda e, b=base: self.open_file(b))
        self.reflow_preview()
        self.prev_canvas.yview_moveto(0)

    def open_file(self, path):
        try:
            open_default(path)
        except Exception as e:
            messagebox.showerror(APP, f"Cannot open {Path(path).name}: {e}")

    def open_selected_req(self, e=None):
        if e is not None and self.tr.identify_row(e.y) == "":
            return
        r = self.cur(); res = self.results.get(r["id"]) if r else None
        if res and res.get("file"):
            self.open_file(self.base(res["file"]))

    def open_selected_file(self, e=None):
        sel = self.fl.selection()
        if sel and int(sel[0]) < len(self.files):
            self.open_file(self.files[int(sel[0])])

    def big_preview(self, path, page):
        try:
            im = self.thumb(path, page, 760)
        except Exception as e:
            messagebox.showerror(APP, f"Cannot preview: {e}"); return
        w = tk.Toplevel(self.root); w.title(f"{Path(path).name} - page {page + 1}")
        lb = ttk.Label(w, image=im); lb.image = im; lb.pack()

    def pages_for_selected(self):
        r = self.cur()
        res = self.results.get(r["id"]) if r else None
        path = self.base(res["file"]) if res and res.get("file") else None
        if not path:
            path = filedialog.askopenfilename(title="Choose the file to assign page by page", initialdir=self.S["watch_folder"] or None)
            if not path:
                return
            path = str(Path(path)); self.add_paths([path])
        self.pages_dialog(path)

    def pages_for_file(self):
        sel = self.fl.selection()
        if not sel:
            messagebox.showinfo(APP, "Select a file in the list first."); return
        self.pages_dialog(self.files[int(sel[0])])

    def pages_dialog(self, path):
        if not self.feat("feat_preview"):
            return
        inf = self.info.get(path)
        if not inf:
            messagebox.showinfo(APP, "Click ‘Scan & Check’ first so the file can be read."); return
        is_pdf = inf["kind"] == "pdf"
        n = inf["pages"] if is_pdf else 1
        reqs = [r for r in self.cfg["reqs"] if r["enabled"]]
        names, AUTO = {r["name"]: r["id"] for r in reqs}, "(auto)"
        cur = {}
        for rid, res in self.results.items():
            f = res.get("file")
            if f and self.base(f) == path:
                m = PART_RE.match(f)
                a, b = (int(m.group(2)), int(m.group(3))) if m else (1, n)
                for pg in range(a, b + 1):
                    cur[pg] = next((r["name"] for r in reqs if r["id"] == rid), AUTO)
        w = tk.Toplevel(self.root); w.title(f"Pages - {Path(path).name}"); w.geometry("940x680"); w.grab_set()
        ttk.Label(w, wraplength=900, text="Choose which requirement each page belongs to. (auto) lets DocPacker decide. "
                  "Pages you assign here override the automatic matching; use ‘Undo’ to go back.").pack(anchor="w", padx=8, pady=6)
        bar = ttk.Frame(w); bar.pack(side="bottom", fill="x", pady=6)
        box = ttk.Frame(w); box.pack(fill="both", expand=True, padx=8)
        cv = tk.Canvas(box, highlightthickness=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set); sb.pack(side="right", fill="y"); cv.pack(side="left", fill="both", expand=True)
        grid = ttk.Frame(cv); cv.create_window((0, 0), window=grid, anchor="nw")
        w.bind("<MouseWheel>", lambda e: cv.yview_scroll(int(-e.delta / 120), "units"))
        keep, vars_ = [], []
        for i in range(min(n, 60)):
            f = ttk.Frame(grid, relief="groove", padding=4); f.grid(row=i // 4, column=i % 4, padx=4, pady=4)
            try:
                im = self.thumb(path, i, 190); keep.append(im); ttk.Label(f, image=im).pack()
            except Exception:
                ttk.Label(f, text="(no preview)").pack()
            ttk.Label(f, text=f"Page {i + 1}" if is_pdf else Path(path).name).pack()
            v = tk.StringVar(value=cur.get(i + 1, AUTO)); vars_.append(v)
            ttk.Combobox(f, textvariable=v, values=[AUTO] + list(names), state="readonly", width=24).pack()
        w._keep = keep
        grid.update_idletasks(); cv.configure(scrollregion=cv.bbox("all"))

        def apply():
            self.snapshot("page assignment")
            sel, notes = {}, []
            for i, v in enumerate(vars_):
                if v.get() in names:
                    sel.setdefault(names[v.get()], []).append(i + 1)
            for rid in [k for k, val in self.manual.items() if self.base(val) == path]:
                del self.manual[rid]
            for rid, pgs in sel.items():
                a, b = min(pgs), max(pgs)
                if b - a + 1 != len(pgs):
                    notes.append(f"'{next(r['name'] for r in reqs if r['id'] == rid)}' got pages {pgs}: they are not next to each other, "
                                 f"so pages {a}-{b} were used.")
                self.manual[rid] = path if (not is_pdf or (a == 1 and b == n)) else f"{path}#pages={a}-{b}"
                self.log(f"Pages assigned: {Path(path).name} p.{a}-{b} -> {next(r['name'] for r in reqs if r['id'] == rid)}")
            w.destroy()
            if notes:
                messagebox.showinfo(APP, "\n".join(notes))
            self.evaluate()
        ttk.Button(bar, text="Apply", command=apply).pack(side="right", padx=8)
        ttk.Button(bar, text="Cancel", command=w.destroy).pack(side="right")

    # ---- optional features: saved applicants
    def refresh_people(self):
        self.people_cb["values"] = sorted(self.cfg.get("people", {}))

    def save_profile(self):
        label = simpledialog.askstring(APP, "Save this applicant as:", initialvalue=self.P["name"] or self.P["surname"], parent=self.root)
        if not label or not label.strip():
            return
        label = label.strip()
        self.cfg.setdefault("people", {})[label] = {k: self.P.get(k, "") for k in PROFILE_KEYS}
        self.refresh_people(); self.people_cb.set(label); self.save()
        self.log(f"Applicant saved: {label}"); self.status(f"Saved applicant '{label}'.")

    def load_profile(self, *_):
        d = self.cfg.get("people", {}).get(self.people_cb.get())
        if d:
            for k, val in d.items():
                if k in self.v:
                    self.v[k].set(val)
            self.log(f"Applicant loaded: {self.people_cb.get()}")
            self.evaluate()

    def new_applicant(self):
        if not messagebox.askyesno(APP, "Start a new applicant?\n\nThis clears the name, surname, passport details and the file list.\n"
                                        "Title, phone, joining date and your requirements are kept."):
            return
        self.snapshot("new applicant")
        for k in ("name", "surname", "passport_no", "dob", "passport_expiry"):
            self.v[k].set("")
        self.files, self.info, self.manual, self.manual_ok = [], {}, {}, set()
        self.people_cb.set("")
        self.log("New applicant started")
        self.evaluate()

    def delete_profile(self):
        label = self.people_cb.get()
        if label and label in self.cfg.get("people", {}) and messagebox.askyesno(APP, f"Delete saved applicant '{label}'?"):
            del self.cfg["people"][label]
            self.people_cb.set(""); self.refresh_people(); self.save(); self.log(f"Applicant deleted: {label}")

    # ---- files
    def add_dialog(self):
        ps = filedialog.askopenfilenames(title="Select files")
        if ps:
            self.add_paths(ps, fresh=True)

    def clear_fields(self):
        """Empty every applicant field on the cover form (joining date is kept)."""
        for k in ("name", "surname", "title", "phone", "passport_no", "dob", "passport_expiry"):
            self.v[k].set("")
        self.people_cb.set("")
        self.log("Fields cleared"); self.status("All fields cleared.")

    def reset_session(self):
        """Forget the previous applicant's files, results and passport-derived fields."""
        self.snapshot("new scan")
        self.files, self.info, self.manual, self.manual_ok = [], {}, {}, set()
        self.results, self.items, self.lookup, self.part_keys = {}, {}, {}, set()
        for k in ("name", "surname", "passport_no", "dob", "passport_expiry"):
            self.v[k].set("")
        self.people_cb.set("")
        self.log("Previous files and fields cleared for a new scan")
        self.evaluate()

    def add_paths(self, paths, fresh=False):
        if fresh and self.replace_var.get() and self.files and not self.busy:
            self.reset_session()
        n = 0
        for p in paths:
            p = Path(p)
            for f in (sorted(p.rglob("*")) if p.is_dir() else [p]):
                if f.suffix.lower() in EXTS and str(f) not in self.files:
                    self.files.append(str(f)); n += 1
        self.status(f"{n} file(s) added. Click ‘Scan & Check’." if n else "No new supported files.")
        if n:
            self.log(f"Added {n} file(s)")
        self.refresh_files()
        if n and self.S["auto_scan_on_new"]:
            self.scan()

    def remove_files(self):
        gone = {self.files[int(i)] for i in self.fl.selection() if int(i) < len(self.files)}
        if gone:
            self.snapshot("remove files")
            self.log("Removed from list: " + ", ".join(Path(g).name for g in gone))
        self.drop_files(gone)
        self.evaluate()

    def drop_files(self, gone):
        """Forget files completely (list, scan data, manual assignments)."""
        gone = set(gone)
        self.files = [f for f in self.files if f not in gone]
        for p in gone:
            self.info.pop(p, None)
        self.manual = {k: v for k, v in self.manual.items() if self.base(v) not in gone}

    # ---- multi-document PDFs: split into page ranges
    @staticmethod
    def base(key):
        m = PART_RE.match(key)
        return m.group(1) if m else key

    @staticmethod
    def label(key):
        m = PART_RE.match(key)
        if not m:
            return Path(key).name
        a, b = m.group(2), m.group(3)
        return f"{Path(m.group(1)).name} (p.{a})" if a == b else f"{Path(m.group(1)).name} (p.{a}-{b})"

    def make_part(self, base, s, e):
        P = self.info[base]
        text = "\n".join(P["page_texts"][s - 1:e])
        meta = P["page_meta"][s - 1:e] if P.get("page_meta") else []
        d = dict(kind="pdf", text=text, pages=e - s + 1, w=0, h=0, exif_cam=False,
                 error=P.get("error", ""), path=base)
        if s == e and meta and meta[0]["data"] and len(text.strip()) < 80:
            d.update(kind="image", w=meta[0]["w"], h=meta[0]["h"], img_bytes=meta[0]["data"])
        return d

    def segment(self, path, inf, reqs):
        """Return [(first_page, last_page), ...] if the PDF holds several different documents, else None."""
        n, texts = inf.get("pages", 0), inf.get("page_texts") or []
        if not self.S.get("split_pdf", True) or inf["kind"] != "pdf" or n < 2 or len(texts) != n:
            return None
        labels = []
        for i in range(n):
            pi = self.make_part(path, i + 1, i + 1)
            best, bsc = None, 0.0
            for r in reqs:
                sc = score(r, "", pi)
                if sc >= r["min_score"] and sc > bsc:
                    best, bsc = r["id"], sc
            labels.append(best)
        if len({l for l in labels if l}) < 2:
            return None
        segs = []
        for i, l in enumerate(labels, 1):          # unreadable pages continue the previous document
            if segs and (l is None or l == segs[-1][0]):
                segs[-1][2] = i
            else:
                segs.append([l, i, i])
        return [(a, b) for _, a, b in segs]

    def sync_parts(self):
        """Candidates for matching: whole files, or page ranges of multi-document PDFs."""
        self.items, self.lookup, self.part_keys = {}, {}, set()
        reqs = [r for r in self.cfg["reqs"] if r["enabled"]]
        for p in self.files:
            inf = self.info.get(p)
            if not inf:
                continue
            self.lookup[p] = inf
            segs = self.segment(p, inf, reqs)
            if segs:
                for a, b in segs:
                    k = f"{p}#pages={a}-{b}"
                    self.items[k] = self.lookup[k] = self.make_part(p, a, b)
                    self.part_keys.add(k)
            else:
                self.items[p] = inf
        for v in self.manual.values():             # manually chosen page ranges
            m = PART_RE.match(v)
            if m and v not in self.lookup and m.group(1) in self.info:
                a, b = int(m.group(2)), int(m.group(3))
                P = self.info[m.group(1)]
                if 1 <= a <= b <= P.get("pages", 0) and len(P.get("page_texts") or []) == P["pages"]:
                    self.lookup[v] = self.make_part(m.group(1), a, b)
                    self.part_keys.add(v)

    def prune_missing(self):
        """Remove files that no longer exist on disk; return their paths."""
        gone = [p for p in self.files if not Path(p).exists()]
        if gone:
            self.drop_files(gone)
        return gone

    def pick_folder(self):
        d = filedialog.askdirectory(title="Folder to watch")
        if d:
            self.S["watch_folder"] = d
            self.known_in_folder = set()
            self.poll_folder(initial=True)

    def poll_folder(self, initial=False):
        d = Path(self.S["watch_folder"])
        if not d.is_dir():
            return
        if not self.busy and self.prune_missing():
            self.evaluate()
        cur = {str(f) for f in d.iterdir() if f.suffix.lower() in EXTS}
        new = cur - self.known_in_folder
        self.known_in_folder = cur
        if new:
            self.add_paths(sorted(new))
            if not initial:
                self.status(f"{len(new)} new file(s) detected in watch folder." +
                            ("" if self.S["auto_scan_on_new"] else " Click ‘Scan & Check’."))

    def watch_tick(self):
        if self.S["watch_folder"]:
            try:
                self.poll_folder()
            except Exception:
                pass
        self.root.after(max(1, int(self.S["watch_interval"])) * 1000, self.watch_tick)

    # ---- scan
    def scan(self, force=False):
        if self.busy or fitz is None:
            return
        if force:
            self.info.clear()
        todo = [p for p in self.files if p not in self.info]
        if not todo:
            self.evaluate(); return
        self.busy = True
        self.pb.config(maximum=len(todo), value=0)

        def work():
            for i, p in enumerate(todo):
                self.q.put(("msg", f"OCR {i + 1}/{len(todo)}: {Path(p).name}"))
                try:
                    inf = extract(p, self.S)
                except Exception as e:
                    inf = dict(kind="other", text="", pages=0, w=0, h=0, exif_cam=False, error=str(e))
                inf["path"] = p
                self.info[p] = inf
                self.q.put(("pb", i + 1))
            self.q.put(("done", None))
        threading.Thread(target=work, daemon=True).start()

    def pump(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "msg": self.status(v)
                elif k == "pb": self.pb.config(value=v)
                elif k == "done":
                    self.busy = False; self.evaluate(); self.status("Scan complete.")
                    self.log("Scan complete: " + "; ".join(f"{r['name']}: {self.results[r['id']]['status']}"
                                                          for r in self.cfg["reqs"] if r["id"] in self.results))
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    # ---- evaluate
    def evaluate(self):
        self.sync_parts()
        reqs = [r for r in self.cfg["reqs"] if r["enabled"]]
        blocked = {self.base(v) for v in self.manual.values() if not PART_RE.match(v)}
        man_ranges = {}
        for v in self.manual.values():                  # pages the user picked by hand are not matched automatically again
            mm = PART_RE.match(v)
            if mm:
                man_ranges.setdefault(mm.group(1), []).append((int(mm.group(2)), int(mm.group(3))))

        def overlaps_manual(key):
            mm = PART_RE.match(key)
            base = mm.group(1) if mm else key
            if base not in man_ranges:
                return False
            a, b = (int(mm.group(2)), int(mm.group(3))) if mm else (1, (self.info.get(base) or {}).get("pages", 1) or 1)
            return any(a <= y and x <= b for x, y in man_ranges[base])
        pairs = []
        for r in reqs:
            for p, inf in self.items.items():
                if self.base(p) in blocked or overlaps_manual(p):
                    continue
                sc = score(r, "" if p in self.part_keys else p, inf)
                if sc >= r["min_score"]:
                    pairs.append((sc, r["id"], p))
        assign, used = {}, set()
        for rid, p in self.manual.items():
            if p in self.lookup and any(r["id"] == rid for r in reqs):
                assign[rid] = (p, 1.0); used.add(p)
        for sc, rid, p in sorted(pairs, key=lambda x: -x[0]):
            if rid not in assign and p not in used:
                assign[rid] = (p, sc); used.add(p)
        if "passport" in assign and not self.P["surname"].strip():
            sn, _ = extract_surname(self.lookup[assign["passport"][0]]["text"])
            if sn:
                self.v["surname"].set(sn.title())
        if "passport" in assign and self.feat("feat_autofill"):
            mz = parse_mrz(self.lookup[assign["passport"][0]]["text"])
            if mz:
                for k, val in (("name", mz["full"]), ("passport_no", mz["number"]),
                               ("dob", fmt_date(mz["dob"])), ("passport_expiry", fmt_date(mz["expiry"]))):
                    if val and not self.P.get(k, "").strip():
                        self.v[k].set(val)
        self.results = {}
        for r in reqs:
            if r["id"] not in assign:
                self.results[r["id"]] = dict(file=None, score=0, checks=[], status="MISSING" if r["required"] else "—")
                continue
            p, sc = assign[r["id"]]
            checks = []
            for ch in r["checks"]:
                if ch["type"] == "manual":
                    ok = (r["id"], ch["label"]) in self.manual_ok
                    checks.append(("manual_ok" if ok else "manual", ch["label"], "confirmed" if ok else "double-click to confirm"))
                else:
                    st, d = run_check(ch, self.lookup[p], self.P, self.S)
                    checks.append((st, ch["label"], d))
            if self.lookup[p].get("error"):
                checks.append(("warn", "Reading", self.lookup[p]["error"]))
            stt = "ISSUES" if any(c[0] == "fail" for c in checks) else \
                  "CONFIRM" if any(c[0] == "manual" for c in checks) else \
                  "OK*" if any(c[0] == "warn" for c in checks) else "OK"
            self.results[r["id"]] = dict(file=p, score=sc, checks=checks, status=stt)
        self.warnings = self.find_warnings()
        self.refresh_warnings()
        self.refresh_reqs(); self.refresh_files(); self.show_detail()

    def refresh_reqs(self):
        sel = self.tr.selection()
        self.tr.delete(*self.tr.get_children())
        tagmap = {"OK": "ok", "OK*": "ok", "ISSUES": "issues", "MISSING": "missing", "CONFIRM": "confirm", "—": "off"}
        for r in self.cfg["reqs"]:
            res = self.results.get(r["id"])
            if not r["enabled"]:
                vals, tag = ("OFF", r["name"], "", ""), "off"
            elif res:
                vals = (res["status"], r["name"] + ("" if r["required"] else " (optional)"),
                        self.label(res["file"]) if res["file"] else "", f"{res['score']:.0%}" if res["file"] else "")
                tag = tagmap[res["status"]]
            else:
                vals, tag = ("…", r["name"], "", ""), "off"
            self.tr.insert("", "end", iid=r["id"], values=vals, tags=(tag,))
        if sel and self.tr.exists(sel[0]):
            self.tr.selection_set(sel[0])

    def refresh_files(self):
        self.fl.delete(*self.fl.get_children())
        names = {r["id"]: r["name"] for r in self.cfg["reqs"]}
        used = {}
        for rid, v in self.results.items():
            if v["file"]:
                used.setdefault(self.base(v["file"]), []).append(names.get(rid, rid))
        for n, p in enumerate(self.files):
            i = self.info.get(p, {})
            use = ", ".join(used[p]) if p in used else ("— unmatched —" if i else "not scanned")
            self.fl.insert("", "end", iid=str(n), values=(Path(p).name, i.get("kind", "?"), i.get("pages", ""), use))

    def cur(self):
        s = self.tr.selection()
        return next((r for r in self.cfg["reqs"] if s and r["id"] == s[0]), None)

    def show_detail(self):
        self.ck.delete(*self.ck.get_children()); self.txt.delete("1.0", "end")
        r = self.cur(); res = self.results.get(r["id"]) if r else None
        self.show_preview(res)
        if not res or not res["file"]:
            return
        for i, (st, lbl, d) in enumerate(res["checks"]):
            self.ck.insert("", "end", iid=str(i), values=(ICON[st], lbl, d))
        self.txt.insert("1.0", self.lookup[res["file"]]["text"] or "(no text found)")

    def toggle_manual(self, e):
        r, i = self.cur(), self.ck.identify_row(e.y)
        if not r or not i:
            return
        st, lbl, _ = self.results[r["id"]]["checks"][int(i)]
        if st in ("manual", "manual_ok"):
            self.snapshot("manual check")
            self.manual_ok ^= {(r["id"], lbl)}
            self.log(f"Manual check {'confirmed' if (r['id'], lbl) in self.manual_ok else 'unconfirmed'}: {r['name']} / {lbl}")
            self.evaluate(); self.tr.selection_set(r["id"])

    # ---- requirement management
    def assign_file(self):
        r = self.cur()
        if not r: return
        p = filedialog.askopenfilename(title=f"File for: {r['name']}", initialdir=self.S["watch_folder"] or None)
        if p:
            p = str(Path(p))                       # same spelling as the file list (Windows uses backslashes)
            key = p
            if p.lower().endswith(".pdf"):
                pr = simpledialog.askstring(APP, "Use only some pages of this PDF? e.g. 2  or  2-3\n"
                                                 "Leave blank to use the whole file.", parent=self.root)
                m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", pr or "")
                if m and 1 <= int(m.group(1)) <= int(m.group(2) or m.group(1)):
                    key = f"{p}#pages={int(m.group(1))}-{int(m.group(2) or m.group(1))}"
            self.snapshot("assign file")
            self.add_paths([p]); self.manual[r["id"]] = key
            self.log(f"Assigned {self.label(key)} -> {r['name']}")
            if p in self.info: self.evaluate()
            else: self.scan()

    def unassign(self):
        r = self.cur()
        if r:
            self.snapshot("unassign")
            self.manual.pop(r["id"], None); self.log(f"Unassigned: {r['name']}"); self.evaluate()

    def move(self, d):
        r = self.cur()
        if not r: return
        L = self.cfg["reqs"]; i = L.index(r); j = i + d
        if 0 <= j < len(L):
            self.snapshot("move requirement")
            L[i], L[j] = L[j], L[i]; self.refresh_reqs(); self.tr.selection_set(r["id"])

    def toggle_req(self):
        r = self.cur()
        if r:
            self.snapshot("requirement on/off")
            r["enabled"] = not r["enabled"]; self.evaluate()

    def del_req(self):
        r = self.cur()
        if r and messagebox.askyesno(APP, f"Delete requirement '{r['name']}'?"):
            self.snapshot("delete requirement")
            self.cfg["reqs"].remove(r); self.log(f"Requirement deleted: {r['name']}"); self.evaluate()

    def edit_req(self):
        r = self.cur()
        if r: self.req_dialog(r)

    def req_dialog(self, req=None):
        new = req is None
        r = req or R(f"custom_{int(time.time())}", "New requirement")
        w = tk.Toplevel(self.root); w.title("Requirement"); w.grab_set()
        lst = lambda a, sep: sep.join(a)
        rows = [("Name", "name", r["name"]), ("Keywords - any (comma-separated)", "any", lst(r["any"], ", ")),
                ("Min. keywords needed", "any_min", str(r["any_min"])), ("Keywords - ALL required (comma)", "all", lst(r["all"], ", ")),
                ("Regex patterns - any (separate with ;)", "regex", lst(r["regex"], "; ")),
                ("Filename hints (comma)", "hints", lst(r["hints"], ", ")), ("Min. match score 0-1", "min_score", str(r["min_score"]))]
        vs = {}
        for i, (l, k, val) in enumerate(rows):
            ttk.Label(w, text=l).grid(row=i, column=0, sticky="e", padx=6, pady=3)
            vs[k] = tk.StringVar(value=val); ttk.Entry(w, textvariable=vs[k], width=55).grid(row=i, column=1, padx=6)
        fl = {k: tk.BooleanVar(value=v) for k, v in (("required", r["required"]), ("pdf", "pdf" in r["kinds"]),
                                                       ("image", "image" in r["kinds"]), ("low_text_bonus", r["low_text_bonus"]))}
        f = ttk.Frame(w); f.grid(row=len(rows), column=0, columnspan=2)
        for k, t in (("required", "Required"), ("pdf", "Accept PDF"), ("image", "Accept image"), ("low_text_bonus", "Photo-like (image with little text)")):
            ttk.Checkbutton(f, text=t, variable=fl[k]).pack(side="left", padx=5)
        ttk.Label(w, text="Checks - one per line:  label | regex     (prefix label with ~ for warning only)\n"
                          "built-ins:  label | @surname_filled  @surname_matches  @surname_in_text  @passport_surname  @name_in_text  @portrait  @face\n"
                          "                     @no_exif_camera  @min_res:1000  @min_pages:2  @no_sample_terms  @manual\n"
                          "                     @mrz_valid  @passport_expiry:6 (months after joining)  @doc_age:90 (max days old)",
                  justify="left").grid(row=len(rows) + 1, column=0, columnspan=2, padx=6, pady=(8, 0), sticky="w")
        box = tk.Text(w, width=90, height=11); box.grid(row=len(rows) + 2, column=0, columnspan=2, padx=6)
        lines = []
        for c in r["checks"]:
            body = c["arg"] if c["type"] == "regex" else "@" + c["type"] + (":" + c["arg"] if c["arg"] else "")
            lines.append(("~" if c["level"] == "warn" else "") + f"{c['label']} | {body}")
        box.insert("1.0", "\n".join(lines))

        def ok():
            self.snapshot("edit requirement")
            try:
                r.update(name=vs["name"].get().strip() or "Unnamed", any=[x.strip() for x in vs["any"].get().split(",") if x.strip()],
                         all=[x.strip() for x in vs["all"].get().split(",") if x.strip()],
                         regex=[x.strip() for x in vs["regex"].get().split(";") if x.strip()],
                         hints=[x.strip() for x in vs["hints"].get().split(",") if x.strip()],
                         any_min=int(vs["any_min"].get() or 1), min_score=float(vs["min_score"].get() or .35),
                         required=fl["required"].get(), low_text_bonus=fl["low_text_bonus"].get(),
                         kinds=[k for k in ("pdf", "image") if fl[k].get()])
                chk = []
                for ln in box.get("1.0", "end").splitlines():
                    if "|" not in ln: continue
                    lab, body = (x.strip() for x in ln.split("|", 1))
                    lvl = "warn" if lab.startswith("~") else "fail"; lab = lab.lstrip("~").strip()
                    if body.startswith("@"):
                        t, _, a = body[1:].partition(":"); chk.append(C(lab, t, a, lvl))
                    else:
                        chk.append(C(lab, "regex", body, lvl, compact=bool(re.fullmatch(r"\[A-Z\]\{2\}.*", body))))
                r["checks"] = chk
            except ValueError as e:
                messagebox.showerror(APP, f"Invalid value: {e}"); return
            if new: self.cfg["reqs"].append(r)
            w.destroy(); self.evaluate()
        ttk.Button(w, text="Save", command=ok).grid(row=len(rows) + 3, column=1, sticky="e", padx=6, pady=8)

    # ---- settings
    def settings_dialog(self):
        w = tk.Toplevel(self.root); w.title("Settings"); w.grab_set()
        nb = ttk.Notebook(w); nb.pack(fill="both", expand=True, padx=6, pady=6)
        gen = ttk.Frame(nb); nb.add(gen, text="General")
        fea = ttk.Frame(nb); nb.add(fea, text="Features (on / off)")
        items = [("Tesseract path (blank = auto)", "tesseract_path", "file"), ("OCR language(s), e.g. eng+ara", "ocr_lang", "s"),
                 ("OCR DPI for PDFs", "ocr_dpi", "s"), ("Max OCR pages per PDF", "max_ocr_pages", "s"),
                 ("Watch-folder poll (seconds)", "watch_interval", "s"), ("Output folder (blank = watch folder)", "output_folder", "dir"),
                 ("Output file name ({name} {date} {title})", "output_name", "s"), ("Cover page title", "cover_title", "s"),
                 ("Page size (A4 / Letter)", "page_size", "s"), ("Image page margin (pt)", "page_margin", "s"),
                 ("JPEG quality for images", "image_quality", "s"),
                 ("Target size when compressing (MB)", "target_size_mb", "s"),
                 ("Arabic font file (blank = auto)", "arabic_font", "file"),
                 ("Sample terms to reject (comma: sample name, account…)", "sample_terms", "s")]
        vs = {}
        for i, (l, k, kind) in enumerate(items):
            ttk.Label(gen, text=l).grid(row=i, column=0, sticky="e", padx=6, pady=2)
            vs[k] = tk.StringVar(value=str(self.S.get(k, ""))); ttk.Entry(gen, textvariable=vs[k], width=46).grid(row=i, column=1)
            if kind != "s":
                ttk.Button(gen, text="…", width=3, command=lambda k=k, kind=kind: vs[k].set(
                    (filedialog.askopenfilename() if kind == "file" else filedialog.askdirectory()) or vs[k].get())).grid(row=i, column=2)
        keys = ("cover_page", "cover_checklist", "bookmarks", "append_unmatched", "auto_scan_on_new", "fuzzy_name", "write_report", "split_pdf")
        bs = {k: tk.BooleanVar(value=self.S.get(k, True)) for k in keys}
        labels = {"cover_page": "Add cover page", "cover_checklist": "Checklist on cover", "bookmarks": "PDF bookmarks",
                  "append_unmatched": "Append unmatched files at end", "auto_scan_on_new": "Auto-scan new files in watch folder",
                  "fuzzy_name": "Fuzzy name matching (OCR typos)", "write_report": "Write .txt report next to PDF",
                  "split_pdf": "Split multi-document PDFs by page"}
        for j, k in enumerate(bs):
            ttk.Checkbutton(gen, text=labels[k], variable=bs[k]).grid(row=len(items) + j // 2, column=j % 2, sticky="w", padx=8)
        ttk.Label(fea, text="Every optional feature can be switched on or off here. Changes to rotation / deskew apply to\n"
                            "files scanned after saving (use ‘Re-scan all’ for files already in the list).",
                  justify="left").pack(anchor="w", padx=10, pady=(8, 4))
        fv = {k: tk.BooleanVar(value=self.feat(k)) for k, _, _ in FEATURES}
        for k, lbl, _ in FEATURES:
            ttk.Checkbutton(fea, text=lbl, variable=fv[k]).pack(anchor="w", padx=14, pady=2)

        def collect():
            new, errs = dict(self.S), []
            for k, v in vs.items():
                old, val = self.S.get(k, ""), v.get().strip()
                if k == "target_size_mb":
                    try: new[k] = float(val)
                    except ValueError: errs.append("Target size must be a number.")
                elif isinstance(old, int) and not isinstance(old, bool):
                    try: new[k] = int(val)
                    except ValueError: errs.append(f"'{k}' must be a whole number.")
                else:
                    new[k] = val
            for k, v in list(bs.items()) + list(fv.items()):
                new[k] = v.get()
            return new, errs

        def problems():
            new, errs = collect()
            issues = [("fail", e) for e in errs]
            if new.get("feat_validate", True):
                issues += validate_settings(new)
            return new, issues

        def check():
            _, issues = problems()
            messagebox.showinfo(APP, "Setup check:\n\n" + ("\n".join(("✖ " if l == "fail" else "⚠ ") + m for l, m in issues)
                                                           or "Everything looks fine."), parent=w)

        def save():
            new, issues = problems()
            fails = [m for l, m in issues if l == "fail"]
            if fails and not messagebox.askyesno(APP, "Problems found:\n- " + "\n- ".join(fails) + "\n\nSave anyway?", parent=w):
                return
            rescan = any(new.get(k) != self.S.get(k) for k in ("feat_autorotate", "feat_deskew"))
            self.S.update(new)
            w.destroy(); self.save(); self.apply_features(); self.check_setup(); self.log("Settings saved")
            self.evaluate()
            if rescan:
                self.status("Rotation / deskew settings changed - click ‘Re-scan all’ to apply them to files already listed.")
        row = ttk.Frame(w); row.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Button(row, text="Save", command=save).pack(side="right")
        ttk.Button(row, text="Check setup", command=check).pack(side="right", padx=6)

    # ---- output
    def report_text(self):
        L = [f"{APP} report - {dt.datetime.now():%Y-%m-%d %H:%M}", f"Name: {self.P['name']}", f"Surname: {self.P['surname']}", f"Title: {self.P['title']}",
             f"Phone: {self.P['phone']}", f"Joining date: {self.joining()}",
             f"Passport no.: {self.P.get('passport_no', '')}   Date of birth: {self.P.get('dob', '')}   Passport expiry: {self.P.get('passport_expiry', '')}", ""]
        for r in self.cfg["reqs"]:
            if not r["enabled"]: continue
            res = self.results.get(r["id"], {})
            L.append(f"[{res.get('status', '?')}] {r['name']} -> {self.label(res['file']) if res.get('file') else 'NOT FOUND'}")
            for st, lbl, d in res.get("checks", []):
                L.append(f"     {ICON[st]} {lbl} {('- ' + d) if d else ''}")
        if self.warnings:
            L += ["", "Warnings:"] + [f"  - {m}" for _, m in self.warnings]
        return "\n".join(L)

    def export_report(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="report.txt")
        if p: Path(p).write_text(self.report_text(), "utf-8"); self.status(f"Saved {p}")

    def out_name(self):
        safe = lambda s: re.sub(r"[^\w\-]+", "_", s).strip("_")
        try:
            return self.S["output_name"].format(name=safe(self.P["name"]) or "package", date=safe(self.joining()), title=safe(self.P["title"]))
        except (KeyError, IndexError, ValueError):
            return f"{safe(self.P['name']) or 'package'}_{safe(self.joining())}"

    # (image quality, longest image side in px, PDF image dpi): level 0 = your own settings, later levels shrink more
    LEVELS = [(None, None, None), (80, 2400, 170), (65, 1800, 130), (50, 1400, 100), (40, 1100, 80)]

    def build_pdf(self, p, lvl=(None, None, None)):
        """Write the combined PDF for the current results to path p (no dialogs)."""
        q, maxpx, dpi = lvl
        size = fitz.paper_size("letter" if self.S["page_size"].lower() == "letter" else "a4")
        m = int(self.S["page_margin"])
        quality = int(q or self.S["image_quality"])
        self._arch = None
        out = fitz.open(); toc, rows, placed = [], [], {}
        if self.S["cover_page"]:
            out.new_page(width=size[0], height=size[1])

        def rot_pages(first_idx, base, a0, b0):          # scanned pages that were sideways get turned upright
            rots = (self.info.get(base) or {}).get("page_rot") or []
            for k in range(a0, b0 + 1):
                if k < len(rots) and rots[k]:
                    pg = out[first_idx + k - a0]
                    pg.set_rotation((pg.rotation + rots[k]) % 360)

        def append(path):
            if path in placed:                           # same pages already in the output: just point to them
                return placed[path]
            start = len(out) + 1
            pm = PART_RE.match(path)
            if pm:
                d = fitz.open(pm.group(1)); a0, b0 = int(pm.group(2)) - 1, int(pm.group(3)) - 1
                out.insert_pdf(d, from_page=a0, to_page=b0); d.close()
                rot_pages(start - 1, pm.group(1), a0, b0)
            elif EXTS[Path(path).suffix.lower()] == "pdf":
                d = fitz.open(path); n = len(d)
                out.insert_pdf(d); d.close()
                rot_pages(start - 1, path, 0, n - 1)
            else:
                img = apply_fix(open_rgb(path), (self.info.get(path) or {}).get("fix"))
                if maxpx:
                    img.thumbnail((maxpx, maxpx))
                buf = io.BytesIO(); img.save(buf, "JPEG", quality=quality)
                pg = out.new_page(width=size[0], height=size[1])
                pg.insert_image(fitz.Rect(m, m, size[0] - m, size[1] - m), stream=buf.getvalue(), keep_proportion=True)
            placed[path] = start
            return start
        for r in self.cfg["reqs"]:
            res = self.results.get(r["id"])
            if r["enabled"] and res and res["file"]:
                pg = append(res["file"]); toc.append([1, r["name"], pg]); rows.append((r["name"], res["status"], pg))
            elif r["enabled"]:
                rows.append((r["name"], "MISSING", None))
        if self.S["append_unmatched"]:
            used = {v["file"] for v in self.results.values() if v.get("file")}
            used_whole = {u for u in used if not PART_RE.match(u)}
            for f in self.items:
                if f not in used and self.base(f) not in used_whole:
                    pg = append(f); toc.append([1, "Extra: " + self.label(f), pg]); rows.append(("Extra: " + self.label(f), "EXTRA", pg))
        if self.S["cover_page"]:
            self.draw_cover(out[0], size, rows)
            toc.insert(0, [1, "Cover page", 1])
        if self.S["bookmarks"] and toc:
            out.set_toc(toc)
        out.set_metadata({"title": f"{self.P['name']} - {self.S['cover_title']}", "author": self.P["name"]})
        if dpi and hasattr(out, "rewrite_images"):       # also shrink images inside PDFs that were added as they are
            try:
                out.rewrite_images(dpi_threshold=dpi + 40, dpi_target=dpi, quality=quality)
            except Exception:
                pass
        out.save(p, garbage=4 if dpi else 3, deflate=True)
        out.close()

    def write_pdf(self, p):
        """Build the PDF; with compression on, retry with stronger shrinking until it fits the target size.
        Returns (size in bytes, compression level used, target met)."""
        target = 0.0
        if self.feat("feat_compress"):
            try:
                target = float(self.S.get("target_size_mb") or 0) * 1_000_000
            except ValueError:
                target = 0.0
        levels = self.LEVELS if target > 0 else self.LEVELS[:1]
        size, i = 0, 0
        for i, lvl in enumerate(levels):
            self.build_pdf(p, lvl)
            size = os.path.getsize(p)
            if target <= 0 or size <= target:
                break
        return size, i, (target <= 0 or size <= target)

    def combine(self):
        if fitz is None: return
        if not self.joining():
            messagebox.showerror(APP, "Joining date is invalid."); return
        gone = self.prune_missing()
        if gone:
            self.evaluate()
            messagebox.showwarning(APP, "These files no longer exist and were removed from the list:\n- "
                                   + "\n- ".join(Path(p).name for p in gone)
                                   + "\n\nCheck the list, then click Combine again.")
            return
        if not self.results:
            messagebox.showinfo(APP, "Scan first."); return
        bad = [r["name"] for r in self.cfg["reqs"] if r["enabled"] and self.results.get(r["id"], {}).get("status") in ("MISSING", "ISSUES", "CONFIRM")
               and (r["required"] or self.results[r["id"]]["file"])]
        if not self.P["surname"].strip():
            bad.insert(0, "Surname is empty on the cover form")
        bad += ["⚠ " + m for l, m in self.warnings if l == "warn"]
        if bad and not messagebox.askyesno(APP, "These items are not fully OK:\n- " + "\n- ".join(bad) + "\n\nCombine anyway?"):
            return
        out_dir = self.S["output_folder"] or self.S["watch_folder"] or str(Path.home())
        p = filedialog.asksaveasfilename(defaultextension=".pdf", initialdir=out_dir, initialfile=self.out_name() + ".pdf")
        if not p: return
        size, lvl, met = self.write_pdf(p)
        if self.S["write_report"]:
            Path(p).with_suffix(".report.txt").write_text(self.report_text(), "utf-8")
        self.save()
        note = f"{size / 1e6:.1f} MB" + (f", compressed (level {lvl})" if lvl else "")
        self.status(f"Saved {p} ({note})")
        self.log(f"Combined -> {p} ({note}); " + "; ".join(f"{r['name']}: {self.results[r['id']]['status']}"
                                                           for r in self.cfg["reqs"] if r["id"] in self.results))
        if not met:
            messagebox.showwarning(APP, f"The PDF is {size / 1e6:.1f} MB, still above the {self.S.get('target_size_mb')} MB target even at "
                                        "the strongest compression. Consider fewer pages or a higher target.")
        if self.clear_after_var.get():                   # ready for the next applicant
            self.reset_session(); self.clear_fields()
            self.status(f"Saved {p} ({note}). Files and fields cleared - ready for the next applicant (Ctrl+Z restores the files).")
        if messagebox.askyesno(APP, "Saved. Open the output folder?"):
            try: os.startfile(str(Path(p).parent))  # Windows
            except AttributeError:
                os.system(f'open "{Path(p).parent}"' if os.uname().sysname == "Darwin" else f'xdg-open "{Path(p).parent}"')

    # ---- batch mode: one sub-folder per person
    def batch_run(self):
        if fitz is None or self.busy or not self.feat("feat_batch"):
            return
        if not self.joining():
            messagebox.showerror(APP, "Joining date is invalid."); return
        parent = filedialog.askdirectory(title="Folder that contains one sub-folder per person")
        if not parent:
            return
        people = [d for d in sorted(Path(parent).iterdir()) if d.is_dir() and any(f.suffix.lower() in EXTS for f in d.rglob("*"))]
        if not people:
            messagebox.showinfo(APP, "No sub-folders with supported files were found."); return
        if not messagebox.askyesno(APP, f"Make one combined PDF for each of the {len(people)} sub-folders?\n\n"
                                        "Title, phone and joining date come from the form above. The name comes from the passport "
                                        "(or the folder name). Items that need a manual tick (signature, stamp) will show as CONFIRM "
                                        "in the summary.\n\nThe window stays usable but may feel slow while scanning."):
            return
        out_dir = Path(self.S["output_folder"] or parent)
        out_dir.mkdir(parents=True, exist_ok=True)
        keep = dict(files=self.files, info=self.info, manual=self.manual, manual_ok=self.manual_ok, undo=self.undo_stack,
                    P={k: self.P.get(k, "") for k in PROFILE_KEYS})
        self.busy = True
        summary = []
        try:
            for n, d in enumerate(people, 1):
                self.files, self.info, self.manual, self.manual_ok, self.undo_stack = [], {}, {}, set(), []
                for k in ("name", "surname", "passport_no", "dob", "passport_expiry"):
                    self.v[k].set("")
                self.files = [str(f) for f in sorted(d.rglob("*")) if f.suffix.lower() in EXTS]
                for j, f in enumerate(self.files, 1):
                    self.status(f"Batch {n}/{len(people)} - {d.name}: OCR {j}/{len(self.files)}"); self.root.update()
                    try:
                        inf = extract(f, self.S)
                    except Exception as e:
                        inf = dict(kind="other", text="", pages=0, w=0, h=0, exif_cam=False, error=str(e), page_texts=[])
                    inf["path"] = f; self.info[f] = inf
                self.evaluate()
                if not self.P["name"].strip():
                    self.v["name"].set(d.name); self.evaluate()
                target = out_dir / (self.out_name() + ".pdf")
                k = 2
                while target.exists():
                    target = out_dir / f"{self.out_name()}_{k}.pdf"; k += 1
                size, lvl, met = self.write_pdf(str(target))
                if self.S["write_report"]:
                    target.with_suffix(".report.txt").write_text(self.report_text(), "utf-8")
                states = [f"{r['name']}: {self.results[r['id']]['status']}" for r in self.cfg["reqs"] if r["enabled"] and r["id"] in self.results]
                warn = sum(1 for l, _ in self.warnings if l == "warn")
                summary.append(f"{d.name}  ->  {target.name} ({size / 1e6:.1f} MB{'' if met else ', ABOVE TARGET'})"
                               f"{'  [' + str(warn) + ' warning(s)]' if warn else ''}\n      " + "; ".join(states))
                self.log(f"Batch: {d.name} -> {target}")
        except Exception as e:
            summary.append(f"STOPPED: {e}")
            raise
        finally:
            self.files, self.info, self.manual, self.manual_ok, self.undo_stack = (keep["files"], keep["info"], keep["manual"],
                                                                                    keep["manual_ok"], keep["undo"])
            for k, val in keep["P"].items():
                self.v[k].set(val)
            self.busy = False
            self.evaluate()
            text = f"{APP} batch summary - {dt.datetime.now():%Y-%m-%d %H:%M}\n\n" + "\n".join(summary)
            try:
                (out_dir / "batch_summary.txt").write_text(text, "utf-8")
            except Exception:
                pass
            self.status(f"Batch finished: {len(summary)} of {len(people)} done. Summary: {out_dir / 'batch_summary.txt'}")
        messagebox.showinfo(APP, text[:1800] + ("\n…" if len(text) > 1800 else "") + f"\n\nSaved in: {out_dir}")

    def draw_text(self, pg, x, y, text, fs, bold=False, maxw=None):
        """Write text on the cover; Arabic / right-to-left text is shaped properly when a suitable font is found."""
        text = str(text)
        if self.feat("feat_arabic") and has_arabic(text):
            fp = find_arabic_font(self.S)
            if fp:
                try:
                    if self._arch is None or self._arch_font != fp:
                        self._arch = fitz.Archive(); self._arch.add(Path(fp).read_bytes(), "arfont.ttf"); self._arch_font = fp
                    css = "@font-face {font-family: arfont; src: url(arfont.ttf);} body {font-family: arfont;}"
                    box = fitz.Rect(x, y - fs, x + (maxw or pg.rect.width - x - 40), y + fs * 1.8)
                    pg.insert_htmlbox(box, f'<div style="font-size:{fs}pt;font-weight:{"bold" if bold else "normal"};text-align:left">'
                                           f'{html.escape(text)}</div>', css=css, archive=self._arch)
                    return
                except Exception:
                    pass
        pg.insert_text((x, y), text, fontsize=fs, fontname="hebo" if bold else "helv")

    def draw_cover(self, pg, size, rows):
        x, y = 50, 80
        self.draw_text(pg, x, y, self.S["cover_title"], 24, True); y += 40
        for lbl, val in (("Full name", self.P["name"]), ("Surname", self.P["surname"]), ("Title", self.P["title"]), ("Phone", self.P["phone"]),
                         ("Joining date", self.joining())):
            pg.insert_text((x, y), f"{lbl}:", fontsize=12, fontname="hebo")
            self.draw_text(pg, x + 110, y, val, 12); y += 22
        if self.S["cover_checklist"]:
            y += 20
            pg.insert_text((x, y), "Contents & checklist", fontsize=14, fontname="hebo"); y += 8
            pg.draw_line((x, y), (size[0] - x, y)); y += 20
            tags = {"OK": "[OK]", "OK*": "[OK]", "ISSUES": "[CHECK]", "CONFIRM": "[CONFIRM]", "MISSING": "[MISSING]", "EXTRA": "[EXTRA]"}
            for n, st, page in rows:
                if y > size[1] - 50: break
                pg.insert_text((x, y), tags.get(st, st), fontsize=10, fontname="hebo")
                self.draw_text(pg, x + 80, y, n[:62], 10, maxw=size[0] - x - 190)
                if page: pg.insert_text((size[0] - 100, y), f"page {page}", fontsize=10, fontname="helv")
                y += 18
        pg.insert_text((x, size[1] - 30), f"Generated {dt.date.today():%d-%b-%Y} by {APP}", fontsize=8, fontname="helv")

    def save(self):
        try: CFG_PATH.write_text(json.dumps(self.cfg, indent=2, ensure_ascii=False), "utf-8")
        except Exception as e: self.status(f"Could not save config: {e}")

    def quit(self):
        self.save(); self.root.destroy()


if __name__ == "__main__":
    App().root.mainloop()
