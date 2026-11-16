"""Find the visa (or ticket) page inside a big file and store just that page (no Tk).

    texts, error = read_page_texts("big_scan.pdf", cfg, ocr_pages=40)       # text of every page (OCR where needed)
    scores = rank_pages(texts, "visa", passport_no="X1234567", name="Ali Khan")
    best = likely_pages(scores)                                              # [PageScore(page=16, score=14.0, ...)]
    dates = suggest_dates(texts[16], "visa")                                 # {"issue": date, "expiry": date, "all": [...]}
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
    "visa": [("visa", 4), ("e-visa", 4), ("evisa", 4), ("entry permit", 3), ("work permit", 3), ("residence permit", 2),
             ("duration of stay", 3), ("number of entries", 3), ("no. of entries", 3), ("single entry", 3),
             ("multiple entry", 3), ("valid until", 1), ("date of issue", 1), ("place of issue", 1), ("sponsor", 1),
             ("employment", 1), ("immigration", 1), ("تأشيرة", 5), ("تاشيرة", 5), ("فيزا", 4), ("سمة", 3), ("إقامة", 1)],
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
_ISSUE = re.compile(r"date of issue|issue date|issued on|date issued|issued|issue|تاريخ الإصدار|تاريخ الاصدار", re.I)
_EXPIRY = re.compile(r"valid until|valid to|valid till|valid thru|expiry|expires?|expiration|enter before|last entry|"
                     r"must enter|تاريخ الانتهاء|ينتهي", re.I)
_DEPART = re.compile(r"depart|departure|flight date|travel date|outbound|take.?off|date of travel|date", re.I)


class VisaFileError(Exception):
    """A user-facing problem with a file."""


@dataclass
class PageScore:
    page: int                                   # 0-based
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u0600-\u06FF]+", "", text or "").upper()


def _has(word: str, text: str) -> bool:
    if re.search(r"[\u0600-\u06FF]", word):
        return word in text
    return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text) is not None


def score_page(text: str, kind: str = "visa", passport_no: str = "", name: str = "") -> PageScore:
    low = (text or "").lower()
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
    flat = _norm(text)
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
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    found: dict = {"issue": None, "expiry": None, "flight": None, "all": []}
    seen: list[dt.date] = []
    for i, line in enumerate(lines):
        dates = find_dates(line, allow2=True)
        if not dates:
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
    return found


# --------------------------------------------------------------------------- reading and cutting
def read_page_texts(path: str | Path, cfg, ocr_pages: int = 40) -> tuple[list[str], str]:
    """Text of every page.  Pages without a text layer are read with OCR for the first `ocr_pages` pages.
    Returns (texts, error message or '')."""
    from .readers import ReadOptions, read_document
    cfg.max_pages = max(0, ocr_pages)
    info = read_document(path, cfg, ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=False, deskew=False))
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
