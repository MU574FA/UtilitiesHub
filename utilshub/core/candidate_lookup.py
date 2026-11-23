"""Find a candidate's row in a spreadsheet by name or passport (no Tk).

    hits = find_candidate("people.xlsx", "Ahmed Ali", sheet="", name_column="", header_row=1)
    hits[0].score, hits[0].fields   # -> 0.97, [("Full name", "Ahmed Ali"), ("Phone", "+964..."), ...]

    hits = match_candidate("people.xlsx", name="RAJESH KUMAR SHARMA", passport_no="V7790175")
    hits[0].matched_by              # -> "passport" | "name" | "partial_name" | "dob+name"

The sheet is re-read automatically when the file changes, so you can keep editing it in Excel.
Names are compared tolerant of word order, typos, passport vs. short names, and Arabic spelling variants.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from .dates import find_dates
from .fuzzy import lev_ratio, name_score, prep_name
from .sheets import SheetError, Table, cell_text, guess_name_column, load_book

_cache: dict[tuple, tuple[float, Table]] = {}

_PASSPORT_HDR = re.compile(r"passport\s*(?:no|num|number|#)?|pp\s*no|رقم\s*الجواز|جواز", re.I)
_DOB_HDR = re.compile(r"\bdob\b|date\s*of\s*birth|birth\s*date|birthday|تاريخ\s*الميلاد|الميلاد", re.I)
_PHONE_HDR = re.compile(r"phone|mobile|whatsapp|whats\s*app|tel|cell|contact|هاتف|موبايل|جوال|واتساب", re.I)
_EMAIL_HDR = re.compile(r"e-?mail|البريد|ايميل|إيميل", re.I)
_POS_HDR = re.compile(r"position|role|job|title|profession|trade|occupation|الوظيفة|المهنة", re.I)
_GROUP_HDR = re.compile(r"group|batch|project|agency|department|dept|company|المجموعة|المشروع|القسم", re.I)
_NAT_HDR = re.compile(r"national|country|citizenship|الجنسية", re.I)


@dataclass
class Hit:
    score: float
    row: int                                    # Excel row number
    fields: list[tuple[str, str]]               # (column heading, value) - empty cells left out
    matched_by: str = "name"                    # passport | name | partial_name | dob+name
    candidate_name: str = ""                    # value from the sheet's name column

    def field_map(self) -> dict[str, str]:
        """Extract common candidate details (name, passport_no, phone, email, position, group, nationality)."""
        out: dict[str, str] = {}
        if self.candidate_name:
            out["name"] = self.candidate_name
        for heading, val in self.fields:
            h = heading.strip()
            v = val.strip()
            if not v:
                continue
            if "passport_no" not in out and _PASSPORT_HDR.search(h) and not re.search(r"expir|issue|type|valid", h, re.I):
                out["passport_no"] = v
            elif "phone" not in out and _PHONE_HDR.search(h):
                out["phone"] = v
            elif "email" not in out and _EMAIL_HDR.search(h):
                out["email"] = v
            elif "position" not in out and _POS_HDR.search(h):
                out["position"] = v
            elif "group" not in out and _GROUP_HDR.search(h):
                out["group"] = v
            elif "nationality" not in out and _NAT_HDR.search(h):
                out["nationality"] = v
        return out


def _norm_passport(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", value or "").upper()


def _norm_date(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    found = find_dates(s, allow2=True)
    return found[0].isoformat() if found else ""


def _table(path: str, sheet: str, header_row: int) -> Table:
    p = Path(path)
    if not p.is_file():
        raise SheetError(f"The sheet was not found: {path}")
    key = (str(p), sheet, header_row)
    mtime = p.stat().st_mtime
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    book = load_book(p)
    if sheet and sheet not in book.sheets:
        raise SheetError(f"The sheet has no tab called '{sheet}'. Tabs: {', '.join(book.names)}")
    table = book.table(sheet or None, max(0, header_row - 1))
    _cache[key] = (mtime, table)
    return table


def _resolve_name_col(table: Table, name_column: str) -> int:
    wanted = name_column.strip()
    if wanted:
        low = [h.casefold() for h in table.headers]
        if wanted.casefold() not in low:
            raise SheetError(f"The sheet has no column called '{wanted}'. Columns: {', '.join(table.headers)}")
        return low.index(wanted.casefold())
    return guess_name_column(table.headers)


def _passport_name_score(target, cand) -> tuple[float, str]:
    """Compare a passport full name against a candidate list name (which may omit middle/family names)."""
    base = name_score(target, cand)
    if not target.tokens or not cand.tokens:
        return base, "name"
    small, large = (target.tokens, cand.tokens) if len(target.tokens) <= len(cand.tokens) else (cand.tokens, target.tokens)
    matched_tokens = 0
    for tok in small:
        if len(tok) < 2:
            continue
        best = max((lev_ratio(tok, other) for other in large), default=0.0)
        if best >= 0.84:
            matched_tokens += 1
    sig_small = sum(1 for tok in small if len(tok) >= 2)
    if sig_small >= 2 and matched_tokens == sig_small and len(large) > len(small):
        # Every meaningful word of the shorter candidate-list name appears in the full passport name
        boosted = max(base, 0.90)
        return boosted, ("name" if base >= 0.85 else "partial_name")
    if sig_small >= 2 and matched_tokens >= 2:
        boosted = max(base, 0.72 + 0.12 * (matched_tokens / sig_small))
        return min(0.95, boosted), ("name" if base >= 0.75 else "partial_name")
    return base, "name"


def find_candidate(path: str, name: str, sheet: str = "", name_column: str = "", header_row: int = 1,
                   threshold: float = 0.75, limit: int = 5) -> list[Hit]:
    """Best matching rows (best first).  Raises SheetError when the sheet cannot be used."""
    table = _table(path, sheet.strip(), header_row)
    if not table.headers:
        raise SheetError("The sheet is empty.")
    col = _resolve_name_col(table, name_column)
    target = prep_name(name)
    if not target.s:
        return []
    hits: list[Hit] = []
    for row, excel_row in zip(table.rows, table.excel_rows):
        cand_name = cell_text(row[col]).strip()
        score = name_score(target, prep_name(cand_name))
        if score >= threshold:
            fields = [(h, cell_text(v).strip()) for h, v in zip(table.headers, row) if cell_text(v).strip()]
            hits.append(Hit(score, excel_row, fields, "name", cand_name))
    hits.sort(key=lambda h: -h.score)
    return hits[:limit]


def match_candidate(path: str, name: str = "", passport_no: str = "", dob: str = "",
                    sheet: str = "", name_column: str = "", header_row: int = 1,
                    threshold: float = 0.55, limit: int = 5) -> list[Hit]:
    """Match a visa/passport record against the candidate info sheet.

    Checks passport number first (exact match = 1.0), then compares names tolerant of passport-name vs.
    short-name differences, and uses date of birth (if present in the sheet) to strengthen uncertain matches.
    """
    table = _table(path, sheet.strip(), header_row)
    if not table.headers:
        raise SheetError("The sheet is empty.")
    name_col = _resolve_name_col(table, name_column)
    pp_cols = [i for i, h in enumerate(table.headers)
               if _PASSPORT_HDR.search(h) and not re.search(r"expir|issue|type|valid", h, re.I)]
    dob_cols = [i for i, h in enumerate(table.headers) if _DOB_HDR.search(h)]

    target_name = prep_name(name)
    target_pp = _norm_passport(passport_no)
    target_dob = _norm_date(dob)
    if not target_name.s and not target_pp:
        return []

    hits: list[Hit] = []
    for row, excel_row in zip(table.rows, table.excel_rows):
        cand_name = cell_text(row[name_col]).strip() if name_col < len(row) else ""
        score = 0.0
        matched_by = "name"

        # 1. Exact passport match wins immediately (handles completely different names in the sheet)
        if len(target_pp) >= 5 and pp_cols:
            for ci in pp_cols:
                if ci < len(row) and _norm_passport(cell_text(row[ci])) == target_pp:
                    score = 1.0
                    matched_by = "passport"
                    break

        # 2. Fuzzy / passport-vs-short-name match
        if score < 1.0 and target_name.s and cand_name:
            cand_prep = prep_name(cand_name)
            score, matched_by = _passport_name_score(target_name, cand_prep)

            # 3. Date of birth corroboration when available in both visa and sheet
            if target_dob and dob_cols and score >= 0.35:
                for ci in dob_cols:
                    if ci < len(row) and _norm_date(cell_text(row[ci])) == target_dob:
                        score = min(0.98, max(score + 0.20, 0.85))
                        matched_by = "dob+name"
                        break

        if score >= threshold:
            fields = [(h, cell_text(v).strip()) for h, v in zip(table.headers, row) if cell_text(v).strip()]
            hits.append(Hit(score, excel_row, fields, matched_by, cand_name))

    hits.sort(key=lambda h: (-h.score, h.row))
    return hits[:limit]


def match_status(hits: list[Hit]) -> str:
    """'match' (high confidence) | 'possible' (needs manual check) | 'none'."""
    if not hits:
        return "none"
    top = hits[0]
    if top.matched_by == "passport" or top.score >= 0.78:
        return "match"
    return "possible"
