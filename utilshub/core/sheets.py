"""Reading and writing spreadsheets (.xlsx / .csv) as plain Python tables.

    book = load_book("people.xlsx")             # all sheets as rows
    table = book.table("Sheet1", header_row=0)  # unique headers, blank rows dropped, Excel row numbers kept
    write_xlsx("out.xlsx", {"Matches": [[...], ...]})
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .textutil import slugify


class SheetError(Exception):
    """Unreadable spreadsheet (message is user-friendly)."""


@dataclass
class Table:
    headers: list[str]
    rows: list[list[Any]]                       # data rows only
    excel_rows: list[int]                       # 1-based sheet row number of each data row
    header_row: int = 0                         # 0-based row index of the header in the sheet
    sheet: str = ""

    def col(self, name: str) -> int:
        return self.headers.index(name)

    def column(self, name: str) -> list[Any]:
        i = self.col(name)
        return [r[i] if i < len(r) else "" for r in self.rows]

    def records(self) -> list[dict[str, Any]]:
        return [{h: (r[i] if i < len(r) else "") for i, h in enumerate(self.headers)} for r in self.rows]


@dataclass
class Book:
    path: str
    fmt: str                                    # xlsx | csv
    sheets: dict[str, list[list[Any]]] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return list(self.sheets)

    def table(self, sheet: str | None = None, header_row: int = 0) -> Table:
        sheet = sheet or (self.names[0] if self.names else "")
        aoa = self.sheets.get(sheet, [])
        head_raw = aoa[header_row] if header_row < len(aoa) else []
        width = max([len(head_raw)] + [len(r) for r in aoa[header_row + 1:header_row + 200]] or [0])
        headers, seen = [], {}
        for i in range(width):
            h = cell_text(head_raw[i]).strip() if i < len(head_raw) else ""
            h = h or f"Column {i + 1}"
            if h in seen:
                seen[h] += 1
                h = f"{h} ({seen[h]})"
            else:
                seen[h] = 1
            headers.append(h)
        rows, excel_rows = [], []
        for n, r in enumerate(aoa[header_row + 1:], start=header_row + 2):
            if any(cell_text(c).strip() for c in r):
                rows.append(list(r) + [""] * (width - len(r)))
                excel_rows.append(n)
        return Table(headers, rows, excel_rows, header_row, sheet)


def cell_text(value: Any) -> str:
    """Display / comparison text of a cell value."""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M") if (value.hour or value.minute) else value.strftime("%Y-%m-%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_csv(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1256", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeError:
            continue
    else:
        text = raw.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def load_book(path: str | Path) -> Book:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".csv", ".txt", ".tsv"):
        try:
            return Book(str(path), "csv", {path.stem: _read_csv(path)})
        except OSError as exc:
            raise SheetError(f"Could not read {path.name}: {exc}") from exc
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
            sheets = {ws.title: [list(row) for row in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
            wb.close()
        except Exception as exc:
            raise SheetError(f"Could not read {path.name}: {exc}") from exc
        return Book(str(path), "xlsx", sheets)
    if ext == ".xls":
        raise SheetError("Old .xls files are not supported. Open it in Excel and save it as .xlsx.")
    raise SheetError(f"Unsupported spreadsheet type: {ext or '(none)'}")


def write_xlsx(path: str | Path, sheets: dict[str, list[list[Any]]], header: bool = True,
               widths: bool = True) -> str:
    """Write several sheets (first row = header, styled).  Returns the path."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=(name or "Sheet")[:31])
        for row in rows:
            ws.append([_excel_safe(c) for c in row])
        if header and rows:
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", fgColor="E8EEF4")
                cell.alignment = Alignment(vertical="center")
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
        if widths:
            for col in ws.columns:
                longest = max((len(str(c.value)) for c in col[:300] if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(60, max(9, longest + 2))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return str(path)


def _excel_safe(value: Any) -> Any:
    if isinstance(value, str):
        value = "".join(ch for ch in value if ch == "\t" or ch == "\n" or ord(ch) >= 32)
        if value[:1] in ("=", "+", "@") and len(value) > 1:
            return "'" + value                               # never let text become a formula
    return value


def write_csv(path: str | Path, rows: list[list[Any]], delimiter: str = ",") -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        csv.writer(fh, delimiter=delimiter).writerows([[cell_text(c) for c in r] for r in rows])
    return str(path)


def guess_name_column(headers: list[str]) -> int:
    import re
    rx = re.compile(r"name|full|اسم|الاسم|الإسم|nom|nombre", re.I)
    for i, h in enumerate(headers):
        if rx.search(h):
            return i
    return 0


def column_key(headers: list[str]) -> str:
    return slugify(headers[0]) if headers else ""
