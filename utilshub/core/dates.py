"""Date helpers: find dates in free text, month arithmetic, formatting."""
from __future__ import annotations

import calendar
import datetime as dt
import re

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_WORDS: dict[str, int] = {}
for _i, _m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                         "september", "october", "november", "december"], 1):
    _MONTH_WORDS[_m] = _i
    _MONTH_WORDS[_m[:3]] = _i
_MONTH_WORDS["sept"] = 9


def mkdate(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str, allow2: bool = False) -> list[dt.date]:
    """Dates in free text: 10-Oct-2026, 10 October 2026, October 10, 2026, 2026-10-10, 10/10/2026 (day first)."""
    out: list[dt.date] = []
    yr = r"(\d{4}|\d{2})" if allow2 else r"(\d{4})"

    def fix(y: int) -> int:
        return (2000 + y if y < 70 else 1900 + y) if y < 100 else y

    for m in re.finditer(r"\b(\d{1,2})[\s\-/.]*([A-Za-z]{3,9})\.?[\s\-/.,]*" + yr + r"\b", text):
        mon = _MONTH_WORDS.get(m.group(2).lower())
        if mon and (d := mkdate(fix(int(m.group(3))), mon, int(m.group(1)))):
            out.append(d)
    for m in re.finditer(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text):
        mon = _MONTH_WORDS.get(m.group(1).lower())
        if mon and (d := mkdate(int(m.group(3)), mon, int(m.group(2)))):
            out.append(d)
    for m in re.finditer(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", text):
        if d := mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3))):
            out.append(d)
    for m in re.finditer(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d = mkdate(y, b, a) if b <= 12 else mkdate(y, a, b)      # day first, unless that is impossible
        if d:
            out.append(d)
    return out


def add_months(d: dt.date, n: int) -> dt.date:
    y, mi = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return dt.date(y, mi, min(d.day, calendar.monthrange(y, mi)[1]))


def fmt_date(d: dt.date | None, fmt: str = "") -> str:
    if not d:
        return ""
    return d.strftime(fmt) if fmt else f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def parse_any_date(text: str) -> dt.date | None:
    found = find_dates(text or "", allow2=True)
    return found[0] if found else None
