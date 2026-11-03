"""Spreadsheet Ingestion, OpenXML Engine, Sheet Sync & Fuzzy Matcher Exporter."""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

try:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except Exception:
    HAS_OPENPYXL = False

log = logging.getLogger(__name__)


def read_tabular_file(path: Path | str, sheet_name_or_idx: Any = 0,
                      header_row: int = 0) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """
    Reads an XLSX, XLS, or CSV file.
    Returns (headers, list of row dicts, list of all sheet names).
    """
    p = Path(path)
    sheet_names = []
    headers = []
    rows = []

    if p.suffix.lower() == ".csv":
        sheet_names = [p.name]
        with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
            sample = f.read(4096)
            f.seek(0)
            delimiter = ","
            try:
                sniffer = csv.Sniffer()
                dialect = sniffer.sniff(sample)
                delimiter = dialect.delimiter
            except Exception:
                for cand in [",", ";", "\t"]:
                    if cand in sample:
                        delimiter = cand
                        break

            reader = csv.reader(f, delimiter=delimiter)
            all_lines = list(reader)

        if not all_lines or header_row >= len(all_lines):
            return [], [], sheet_names

        raw_headers = all_lines[header_row]
        # Deduplicate headers
        seen = {}
        for h in raw_headers:
            name = str(h).strip() or "Column"
            if name in seen:
                seen[name] += 1
                headers.append(f"{name} ({seen[name]})")
            else:
                seen[name] = 1
                headers.append(name)

        for line in all_lines[header_row + 1:]:
            row_dict = {}
            for col_idx, h_name in enumerate(headers):
                row_dict[h_name] = line[col_idx] if col_idx < len(line) else ""
            rows.append(row_dict)
        return headers, rows, sheet_names

    # Excel file
    if not HAS_OPENPYXL:
        log.error("openpyxl required for Excel files.")
        return [], [], []

    try:
        wb = openpyxl.load_workbook(str(p), data_only=True)
        sheet_names = wb.sheetnames
        ws = None
        if isinstance(sheet_name_or_idx, int):
            idx = min(max(0, sheet_name_or_idx), len(sheet_names) - 1)
            ws = wb[sheet_names[idx]]
        elif str(sheet_name_or_idx) in sheet_names:
            ws = wb[str(sheet_name_or_idx)]
        else:
            ws = wb.active

        all_rows = list(ws.iter_rows(values_only=True))
        if not all_rows or header_row >= len(all_rows):
            return [], [], sheet_names

        raw_headers = all_rows[header_row]
        seen = {}
        for h in raw_headers:
            name = str(h).strip() if h is not None else ""
            if not name:
                name = "Column"
            if name in seen:
                seen[name] += 1
                headers.append(f"{name} ({seen[name]})")
            else:
                seen[name] = 1
                headers.append(name)

        for row_vals in all_rows[header_row + 1:]:
            row_dict = {}
            for col_idx, h_name in enumerate(headers):
                v = row_vals[col_idx] if col_idx < len(row_vals) else ""
                row_dict[h_name] = "" if v is None else str(v).strip()
            # Skip completely empty rows
            if any(row_dict.values()):
                rows.append(row_dict)
        return headers, rows, sheet_names
    except Exception as exc:
        log.error("Failed to read Excel file %s: %s", path, exc)
        return [], [], []


def normalize_key_value(val: Any, ignore_case_and_spaces: bool = True) -> str:
    """Normalizes unique key for matching."""
    if val is None:
        return ""
    s = str(val).strip()
    if ignore_case_and_spaces:
        s = " ".join(s.lower().split())
    return s


def run_sheet_sync(old_headers: list[str], old_rows: list[dict[str, Any]],
                   new_headers: list[str], new_rows: list[dict[str, Any]],
                   key_col: str, selected_cols: list[str],
                   options: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """
    Executes synchronization of master (Old) table from updated (New) table:
    - Unique key alignment
    - Preserves old values when new cells are empty (if keep_old_on_blank)
    - Adds new rows from new table (if add_new_rows)
    - Adds new columns from new table (if add_new_columns)
    - Injects 'Sync status' column
    - Generates full change log
    """
    ignore_case = options.get("ignore_case_and_spaces", True)
    keep_old_on_blank = options.get("keep_old_on_blank", True)
    add_new_rows = options.get("add_new_rows", True)
    add_new_columns = options.get("add_new_columns", True)
    include_status = options.get("include_status_column", True)

    # Extra columns
    extra_cols = [h for h in new_headers if h not in old_headers] if add_new_columns else []
    final_headers = list(old_headers) + extra_cols
    if include_status and "Sync status" not in final_headers:
        final_headers.append("Sync status")

    # Index new rows
    new_index = {}
    for r in new_rows:
        k = normalize_key_value(r.get(key_col, ""), ignore_case)
        if k and k not in new_index:
            new_index[k] = r

    updated_rows = []
    change_log = []
    matched_new_keys = set()
    
    count_updated = 0
    count_unchanged = 0
    count_cells_changed = 0

    cols_to_inspect = list(selected_cols) + extra_cols

    # Process old rows
    for old_row in old_rows:
        row_copy = dict(old_row)
        for c in extra_cols:
            row_copy[c] = ""

        k = normalize_key_value(old_row.get(key_col, ""), ignore_case)
        new_row = new_index.get(k)
        row_changed = False

        if new_row is not None:
            matched_new_keys.add(k)
            for c in cols_to_inspect:
                old_v = str(row_copy.get(c, "")).strip()
                new_v = str(new_row.get(c, "")).strip()

                if c not in new_row:
                    continue
                if keep_old_on_blank and new_v == "":
                    continue

                if old_v != new_v:
                    change_log.append({
                        "key": old_row.get(key_col, ""),
                        "column": c,
                        "change": "Updated",
                        "old_val": old_v,
                        "new_val": new_v,
                    })
                    row_copy[c] = new_v
                    row_changed = True
                    count_cells_changed += 1

            if row_changed:
                count_updated += 1
                status = "Updated"
            else:
                count_unchanged += 1
                status = "Unchanged"
        else:
            status = "Not in new file"

        if include_status:
            row_copy["Sync status"] = status
        updated_rows.append(row_copy)

    # Add brand-new rows from new file
    count_added = 0
    if add_new_rows:
        for new_row in new_rows:
            k = normalize_key_value(new_row.get(key_col, ""), ignore_case)
            if k and k not in matched_new_keys:
                added_row = {}
                for c in final_headers:
                    added_row[c] = str(new_row.get(c, "")).strip()
                if include_status:
                    added_row["Sync status"] = "Added"
                updated_rows.append(added_row)
                count_added += 1

                for c in final_headers:
                    if c in ("Sync status", key_col):
                        continue
                    v = str(new_row.get(c, "")).strip()
                    if v:
                        change_log.append({
                            "key": new_row.get(key_col, ""),
                            "column": c,
                            "change": "Added (new row)",
                            "old_val": "",
                            "new_val": v,
                        })
                        count_cells_changed += 1

    metrics = {
        "updated_rows": count_updated,
        "new_rows_added": count_added,
        "unchanged_rows": count_unchanged,
        "cells_changed": count_cells_changed,
    }
    return final_headers, updated_rows, change_log, metrics


def export_sync_workbook(output_path: Path | str, headers: list[str],
                         rows: list[dict[str, Any]], change_log: list[dict[str, Any]],
                         log_only: bool = False) -> bool:
    """Exports synchronized spreadsheet with frozen panes and change log."""
    if not HAS_OPENPYXL:
        return False
    try:
        wb = openpyxl.Workbook()
        header_fill = PatternFill(start_color="1F6F5C", end_color="1F6F5C", fill_type="solid")
        header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")

        if not log_only:
            ws_main = wb.active
            ws_main.title = "Updated"
            ws_main.freeze_panes = "A2"

            # Write header
            for col_idx, h in enumerate(headers, 1):
                cell = ws_main.cell(row=1, column=col_idx, value=h)
                cell.fill = header_fill
                cell.font = header_font

            # Write rows
            for row_idx, r in enumerate(rows, 2):
                for col_idx, h in enumerate(headers, 1):
                    ws_main.cell(row=row_idx, column=col_idx, value=r.get(h, ""))

            # Auto column widths
            for col_idx, col in enumerate(ws_main.columns, 1):
                max_len = max(len(str(cell.value or "")) for cell in col)
                col_letter = get_column_letter(col_idx)
                ws_main.column_dimensions[col_letter].width = min(50, max(10, max_len + 3))

            ws_log = wb.create_sheet(title="Change log")
        else:
            ws_log = wb.active
            ws_log.title = "Change log"

        # Log sheet
        ws_log.freeze_panes = "A2"
        log_headers = ["Unique Key", "Column Name", "Change Type", "Old Value", "New Value"]
        for col_idx, h in enumerate(log_headers, 1):
            cell = ws_log.cell(row=1, column=col_idx, value=h)
            cell.fill = header_fill
            cell.font = header_font

        for row_idx, entry in enumerate(change_log, 2):
            ws_log.cell(row=row_idx, column=1, value=entry["key"])
            ws_log.cell(row=row_idx, column=2, value=entry["column"])
            ws_log.cell(row=row_idx, column=3, value=entry["change"])
            ws_log.cell(row=row_idx, column=4, value=entry["old_val"])
            ws_log.cell(row=row_idx, column=5, value=entry["new_val"])

        for col_idx, col in enumerate(ws_log.columns, 1):
            max_len = max(len(str(cell.value or "")) for cell in col)
            col_letter = get_column_letter(col_idx)
            ws_log.column_dimensions[col_letter].width = min(50, max(12, max_len + 3))

        wb.save(str(output_path))
        return True
    except Exception as exc:
        log.error("Failed to export sync workbook: %s", exc)
        return False


def export_matcher_workbook(output_path: Path | str,
                             sheet_a_headers: list[str],
                             sheet_b_headers: list[str],
                             matches: list[dict[str, Any]],
                             all_candidates: list[dict[str, Any]],
                             unmatched_b: list[dict[str, Any]]) -> bool:
    """Exports 3-sheet fuzzy match workbook (Matches, All candidates, Unmatched in B)."""
    if not HAS_OPENPYXL:
        return False
    try:
        wb = openpyxl.Workbook()
        header_fill = PatternFill(start_color="1F6F5C", end_color="1F6F5C", fill_type="solid")
        header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")

        # 1. Matches Sheet
        ws1 = wb.active
        ws1.title = "Matches"
        ws1.freeze_panes = "A2"

        m_headers = list(sheet_a_headers) + ["Best Match (Sheet B)", "Similarity %", "Match Status"] + [f"B: {h}" for h in sheet_b_headers]
        for col_idx, h in enumerate(m_headers, 1):
            c = ws1.cell(row=1, column=col_idx, value=h)
            c.fill = header_fill
            c.font = header_font

        for row_idx, m in enumerate(matches, 2):
            c_idx = 1
            for h in sheet_a_headers:
                ws1.cell(row=row_idx, column=c_idx, value=m.get("row_a", {}).get(h, ""))
                c_idx += 1
            ws1.cell(row=row_idx, column=c_idx, value=m.get("best_b_name", ""))
            c_idx += 1
            ws1.cell(row=row_idx, column=c_idx, value=round(m.get("similarity", 0.0) * 100, 1))
            c_idx += 1
            ws1.cell(row=row_idx, column=c_idx, value=m.get("status", ""))
            c_idx += 1
            for h in sheet_b_headers:
                ws1.cell(row=row_idx, column=c_idx, value=m.get("row_b", {}).get(h, ""))
                c_idx += 1

        # 2. All candidates Sheet
        ws2 = wb.create_sheet(title="All candidates")
        ws2.freeze_panes = "A2"
        c_headers = ["Sheet A Name", "Sheet A Row", "Rank", "Candidate Name (Sheet B)", "Sheet B Row", "Similarity %"]
        for col_idx, h in enumerate(c_headers, 1):
            c = ws2.cell(row=1, column=col_idx, value=h)
            c.fill = header_fill
            c.font = header_font

        for row_idx, cand in enumerate(all_candidates, 2):
            ws2.cell(row=row_idx, column=1, value=cand.get("a_name", ""))
            ws2.cell(row=row_idx, column=2, value=cand.get("a_row", ""))
            ws2.cell(row=row_idx, column=3, value=cand.get("rank", 1))
            ws2.cell(row=row_idx, column=4, value=cand.get("b_name", ""))
            ws2.cell(row=row_idx, column=5, value=cand.get("b_row", ""))
            ws2.cell(row=row_idx, column=6, value=round(cand.get("score", 0.0) * 100, 1))

        # 3. Unmatched in B Sheet
        ws3 = wb.create_sheet(title="Unmatched in B")
        ws3.freeze_panes = "A2"
        for col_idx, h in enumerate(sheet_b_headers, 1):
            c = ws3.cell(row=1, column=col_idx, value=h)
            c.fill = header_fill
            c.font = header_font

        for row_idx, ub in enumerate(unmatched_b, 2):
            for col_idx, h in enumerate(sheet_b_headers, 1):
                ws3.cell(row=row_idx, column=col_idx, value=ub.get(h, ""))

        # Adjust column widths
        for ws in (ws1, ws2, ws3):
            for col_idx, col in enumerate(ws.columns, 1):
                max_len = max(len(str(cell.value or "")) for cell in col)
                col_letter = get_column_letter(col_idx)
                ws.column_dimensions[col_letter].width = min(50, max(10, max_len + 3))

        wb.save(str(output_path))
        return True
    except Exception as exc:
        log.error("Failed to export matcher workbook: %s", exc)
        return False

