"""Find a candidate's row in a spreadsheet by name (no Tk).

    hits = find_candidate("people.xlsx", "Ahmed Ali", sheet="", name_column="", header_row=1)
    hits[0].score, hits[0].fields   # -> 0.97, [("Full name", "Ahmed Ali"), ("Phone", "+964..."), ...]

The sheet is re-read automatically when the file changes, so you can keep editing it in Excel.
Names are compared tolerant of word order, typos and Arabic spelling variants.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .fuzzy import name_score, prep_name
from .sheets import SheetError, Table, cell_text, guess_name_column, load_book

_cache: dict[tuple, tuple[float, Table]] = {}


@dataclass
class Hit:
    score: float
    row: int                                    # Excel row number
    fields: list[tuple[str, str]]               # (column heading, value) - empty cells left out


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


def find_candidate(path: str, name: str, sheet: str = "", name_column: str = "", header_row: int = 1,
                   threshold: float = 0.75, limit: int = 5) -> list[Hit]:
    """Best matching rows (best first).  Raises SheetError when the sheet cannot be used."""
    table = _table(path, sheet.strip(), header_row)
    if not table.headers:
        raise SheetError("The sheet is empty.")
    wanted = name_column.strip()
    if wanted:
        low = [h.casefold() for h in table.headers]
        if wanted.casefold() not in low:
            raise SheetError(f"The sheet has no column called '{wanted}'. Columns: {', '.join(table.headers)}")
        col = low.index(wanted.casefold())
    else:
        col = guess_name_column(table.headers)
    target = prep_name(name)
    if not target.s:
        return []
    hits: list[Hit] = []
    for row, excel_row in zip(table.rows, table.excel_rows):
        score = name_score(target, prep_name(cell_text(row[col])))
        if score >= threshold:
            fields = [(h, cell_text(v).strip()) for h, v in zip(table.headers, row) if cell_text(v).strip()]
            hits.append(Hit(score, excel_row, fields))
    hits.sort(key=lambda h: -h.score)
    return hits[:limit]
