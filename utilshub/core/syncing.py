"""Update an old spreadsheet from a newer one (matched rows updated, new rows added).

    plan = build_plan(old_table, new_table, SyncOptions(key="ID", columns=["Phone", "Email"]))
    for ch in plan.changes: ch.accepted = ...          # the UI lets the user accept / reject each change
    apply_plan(plan, old_book, "old.xlsx", "updated.xlsx")   # keeps the old workbook's formatting

Changes are cell-level (`update`), whole rows (`add`) or informational (`missing`: in old, not in new).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sheets import Book, Table, cell_text, write_csv


@dataclass
class SyncOptions:
    key: str = ""
    ignore_case: bool = True
    columns: list[str] = field(default_factory=list)       # columns to update (shared, non-key)
    add_new: bool = True
    skip_blank: bool = True                                # keep the old value when the new cell is empty
    add_columns: bool = False                              # add columns that exist only in the new file
    status_column: bool = True
    status_name: str = "Sync status"


@dataclass
class Change:
    kind: str                         # update | add | missing
    key: str
    column: str = ""
    old: Any = ""
    new: Any = ""
    old_row: int = -1                 # index into old.rows (update/missing)
    new_row: int = -1                 # index into new.rows (update/add)
    accepted: bool = True


@dataclass
class SyncPlan:
    old: Table
    new: Table
    options: SyncOptions
    extra_columns: list[str]
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def stats(self) -> dict[str, int]:
        acc = [c for c in self.changes if c.accepted]
        updated_rows = {c.old_row for c in acc if c.kind == "update"}
        return {"updated_rows": len(updated_rows), "cells": sum(1 for c in acc if c.kind == "update"),
                "added": sum(1 for c in acc if c.kind == "add"),
                "missing": sum(1 for c in self.changes if c.kind == "missing"),
                "total_changes": len([c for c in self.changes if c.kind != "missing"])}


def norm_key(value: Any, ignore_case: bool) -> str:
    text = cell_text(value).strip()
    return " ".join(text.lower().split()) if ignore_case else text


def shared_columns(old: Table, new: Table) -> list[str]:
    return [h for h in old.headers if h in new.headers]


def key_warnings(table: Table, key: str, ignore_case: bool, label: str) -> list[str]:
    seen, dup, blank = set(), 0, 0
    for v in table.column(key):
        k = norm_key(v, ignore_case)
        if not k:
            blank += 1
        elif k in seen:
            dup += 1
        seen.add(k)
    out = []
    if dup:
        out.append(f"{dup} duplicate value{'s' if dup > 1 else ''} in the {label} file (first match is used).")
    if blank:
        out.append(f"{blank} row{'s' if blank > 1 else ''} in the {label} file have an empty value and are skipped.")
    return out


def build_plan(old: Table, new: Table, opts: SyncOptions) -> SyncPlan:
    extra = [h for h in new.headers if h not in old.headers] if opts.add_columns else []
    plan = SyncPlan(old, new, opts, extra)
    plan.warnings += key_warnings(old, opts.key, opts.ignore_case, "old") + \
        key_warnings(new, opts.key, opts.ignore_case, "new")
    ki_old, ki_new = old.col(opts.key), new.col(opts.key)
    new_index: dict[str, int] = {}
    for i, row in enumerate(new.rows):
        k = norm_key(row[ki_new], opts.ignore_case)
        if k and k not in new_index:
            new_index[k] = i
    used: set[str] = set()
    for oi, row in enumerate(old.rows):
        k = norm_key(row[ki_old], opts.ignore_case)
        ni = new_index.get(k) if k else None
        if ni is None:
            plan.changes.append(Change("missing", cell_text(row[ki_old]), old_row=oi))
            continue
        used.add(k)
        for col in list(opts.columns) + extra:
            if col not in new.headers:
                continue
            nv = new.rows[ni][new.col(col)]
            ov = row[old.col(col)] if col in old.headers and old.col(col) < len(row) else ""
            if nv is None or (opts.skip_blank and cell_text(nv).strip() == ""):
                continue
            if cell_text(nv).strip() != cell_text(ov).strip():
                plan.changes.append(Change("update", cell_text(row[ki_old]), col, ov, nv, oi, ni))
    if opts.add_new:
        for i, row in enumerate(new.rows):
            k = norm_key(row[ki_new], opts.ignore_case)
            if k and k not in used and new_index.get(k) == i:
                plan.changes.append(Change("add", cell_text(row[ki_new]), new_row=i))
    return plan


def _status_by_old_row(plan: SyncPlan) -> dict[int, str]:
    status: dict[int, str] = {}
    for c in plan.changes:
        if c.kind == "update" and c.accepted:
            status[c.old_row] = "Updated"
        elif c.kind == "missing":
            status.setdefault(c.old_row, "Not in new file")
    return status


def result_rows(plan: SyncPlan) -> tuple[list[str], list[list[Any]]]:
    """The updated table as plain rows (accepted changes only)."""
    o = plan.old
    headers = o.headers + plan.extra_columns
    rows = [list(r) + [""] * len(plan.extra_columns) for r in o.rows]
    for c in plan.changes:
        if c.kind != "update" or not c.accepted:
            continue
        j = headers.index(c.column)
        rows[c.old_row][j] = c.new
    for c in plan.changes:
        if c.kind == "add" and c.accepted:
            nrow = plan.new.rows[c.new_row]
            rows.append([nrow[plan.new.col(h)] if h in plan.new.headers else "" for h in headers])
    if plan.options.status_column:
        status = _status_by_old_row(plan)
        headers = headers + [plan.options.status_name]
        for i in range(len(o.rows)):
            rows[i].append(status.get(i, "Unchanged"))
        for i in range(len(o.rows), len(rows)):
            rows[i].append("Added")
    return headers, rows


def apply_plan(plan: SyncPlan, old_book: Book, out_path: str | Path) -> str:
    """Write the result.  For .xlsx the OLD workbook is copied and edited in place, so formatting, widths,
    other sheets and formulas survive; for .csv a new file is written."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    headers, rows = result_rows(plan)
    if old_book.fmt == "csv":
        return write_csv(out_path, [headers] + rows)
    import openpyxl
    from openpyxl.styles import Font
    shutil.copy2(old_book.path, out_path)
    wb = openpyxl.load_workbook(out_path)
    ws = wb[plan.old.sheet] if plan.old.sheet in wb.sheetnames else wb.worksheets[0]
    hdr_row = plan.old.header_row + 1
    base_cols = len(plan.old.headers)
    new_cols = headers[base_cols:]
    for k, name in enumerate(new_cols):
        cell = ws.cell(row=hdr_row, column=base_cols + k + 1, value=name)
        cell.font = Font(bold=True)
    for r_idx, excel_row in enumerate(plan.old.excel_rows):
        for j in range(base_cols):
            ws.cell(row=excel_row, column=j + 1).value = rows[r_idx][j] if rows[r_idx][j] != "" or \
                ws.cell(row=excel_row, column=j + 1).value is not None else None
        for k in range(len(new_cols)):
            ws.cell(row=excel_row, column=base_cols + k + 1, value=rows[r_idx][base_cols + k])
    next_row = (plan.old.excel_rows[-1] if plan.old.excel_rows else hdr_row) + 1
    for extra in rows[len(plan.old.rows):]:
        for j, v in enumerate(extra):
            ws.cell(row=next_row, column=j + 1, value=v if v != "" else None)
        next_row += 1
    wb.save(out_path)
    return str(out_path)


def change_log_rows(plan: SyncPlan) -> list[list[Any]]:
    rows: list[list[Any]] = [["Key", "Column", "Change", "Old value", "New value", "Applied"]]
    for c in plan.changes:
        if c.kind == "update":
            rows.append([c.key, c.column, "Updated", cell_text(c.old), cell_text(c.new), "yes" if c.accepted else "no"])
        elif c.kind == "add":
            rows.append([c.key, "(all columns)", "Added", "", "", "yes" if c.accepted else "no"])
        else:
            rows.append([c.key, "", "Not in new file", "", "", "kept"])
    if len(rows) == 1:
        rows.append(["", "", "No changes", "", "", ""])
    return rows
