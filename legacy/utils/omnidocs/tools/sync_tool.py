"""Sheet Sync Studio: Reconcile and synchronize master Excel tables with cell-level audit trail."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from ..core.config import AppConfig
from ..core.excel_ops import export_sync_workbook, read_tabular_file, run_sheet_sync
from ..ui.widgets import DiffTable, DropTarget, MetricPill


class SheetSyncView(ttk.Frame):
    """Sheet Sync Studio for tabular data reconciliation."""
    def __init__(self, parent: Any, config: AppConfig, **kwargs):
        super().__init__(parent, padding=12, **kwargs)
        self.config = config

        self.old_headers: list[str] = []
        self.old_rows: list[dict[str, Any]] = []
        self.old_path: str = ""

        self.new_headers: list[str] = []
        self.new_rows: list[dict[str, Any]] = []
        self.new_path: str = ""

        self.updated_headers: list[str] = []
        self.updated_rows: list[dict[str, Any]] = []
        self.change_log: list[dict[str, Any]] = []

        self._build_ui()

    def _build_ui(self) -> None:
        # Title
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="Sheet Sync Studio", style="Header.TLabel").pack(side="left")
        ttk.Label(top, text="Update master datasets from new sheets, preserve blanks, expand schemas & audit changes",
                  style="Muted.TLabel").pack(side="left", padx=12, pady=(2, 0))

        # Sources box
        sources = ttk.Frame(self)
        sources.pack(fill="x", pady=(0, 8))
        sources.columnconfigure(0, weight=1)
        sources.columnconfigure(1, weight=1)

        # --------------------------------------------- Old (Master) File
        card_old = ttk.LabelFrame(sources, text="1. Master / Old File (To Be Updated)", padding=8)
        card_old.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        self.drop_old = DropTarget(card_old, text="Drop Master Spreadsheet (.xlsx, .csv)",
                                   on_drop=lambda f: self._load_file("old", f[0]))
        self.drop_old.pack(fill="x", pady=(0, 4))

        meta_old = ttk.Frame(card_old)
        meta_old.pack(fill="x", pady=2)
        ttk.Label(meta_old, text="Sheet:").pack(side="left")
        self.cb_sheet_old = ttk.Combobox(meta_old, state="readonly", width=14)
        self.cb_sheet_old.pack(side="left", padx=4)
        self.cb_sheet_old.bind("<<ComboboxSelected>>", lambda e: self._on_sheet_changed("old"))

        ttk.Label(meta_old, text="Header:").pack(side="left", padx=(8, 2))
        self.cb_hdr_old = ttk.Combobox(meta_old, values=[f"Row {i}" for i in range(1, 6)], state="readonly", width=7)
        self.cb_hdr_old.current(0)
        self.cb_hdr_old.pack(side="left")
        self.cb_hdr_old.bind("<<ComboboxSelected>>", lambda e: self._on_sheet_changed("old"))

        # --------------------------------------------- New (Update) File
        card_new = ttk.LabelFrame(sources, text="2. Newer / Update File (Source of Updates)", padding=8)
        card_new.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self.drop_new = DropTarget(card_new, text="Drop Updated Spreadsheet (.xlsx, .csv)",
                                   on_drop=lambda f: self._load_file("new", f[0]))
        self.drop_new.pack(fill="x", pady=(0, 4))

        meta_new = ttk.Frame(card_new)
        meta_new.pack(fill="x", pady=2)
        ttk.Label(meta_new, text="Sheet:").pack(side="left")
        self.cb_sheet_new = ttk.Combobox(meta_new, state="readonly", width=14)
        self.cb_sheet_new.pack(side="left", padx=4)
        self.cb_sheet_new.bind("<<ComboboxSelected>>", lambda e: self._on_sheet_changed("new"))

        ttk.Label(meta_new, text="Header:").pack(side="left", padx=(8, 2))
        self.cb_hdr_new = ttk.Combobox(meta_new, values=[f"Row {i}" for i in range(1, 6)], state="readonly", width=7)
        self.cb_hdr_new.current(0)
        self.cb_hdr_new.pack(side="left")
        self.cb_hdr_new.bind("<<ComboboxSelected>>", lambda e: self._on_sheet_changed("new"))

        # Alignment & Sync Rules Box
        rules_box = ttk.LabelFrame(self, text="3. Alignment & Synchronization Rules", padding=8)
        rules_box.pack(fill="x", pady=6)

        r_top = ttk.Frame(rules_box)
        r_top.pack(fill="x", pady=(0, 4))

        ttk.Label(r_top, text="Unique Key Column:").pack(side="left", padx=4)
        self.v_key_col = tk.StringVar()
        self.cb_key_col = ttk.Combobox(r_top, textvariable=self.v_key_col, state="readonly", width=18)
        self.cb_key_col.pack(side="left", padx=4)

        self.v_ignore_case = tk.BooleanVar(value=self.config.sync.get("ignore_case_and_spaces", True))
        ttk.Checkbutton(r_top, text="Ignore case & extra whitespace in key", variable=self.v_ignore_case).pack(side="left", padx=12)

        # Rule Toggles
        r_mid = ttk.Frame(rules_box)
        r_mid.pack(fill="x", pady=4)

        self.v_add_rows = tk.BooleanVar(value=self.config.sync.get("add_new_rows", True))
        ttk.Checkbutton(r_mid, text="Add brand new rows", variable=self.v_add_rows).pack(side="left", padx=6)

        self.v_skip_blank = tk.BooleanVar(value=self.config.sync.get("keep_old_on_blank", True))
        ttk.Checkbutton(r_mid, text="Preserve old values when new cells are blank", variable=self.v_skip_blank).pack(side="left", padx=6)

        self.v_add_cols = tk.BooleanVar(value=self.config.sync.get("add_new_columns", True))
        ttk.Checkbutton(r_mid, text="Expand schema (add new columns)", variable=self.v_add_cols).pack(side="left", padx=6)

        self.v_add_status = tk.BooleanVar(value=self.config.sync.get("include_status_column", True))
        ttk.Checkbutton(r_mid, text="Append 'Sync status' column", variable=self.v_add_status).pack(side="left", padx=6)

        # Action Buttons
        btn_bar = ttk.Frame(rules_box)
        btn_bar.pack(fill="x", pady=(6, 0))

        self.btn_run = ttk.Button(btn_bar, text="⚡ Execute Sync & Audit", style="Primary.TButton", command=self._execute_sync)
        self.btn_run.pack(side="left", padx=4)

        self.btn_dl_full = ttk.Button(btn_bar, text="💾 Download Updated Workbook (.xlsx)", command=lambda: self._export(False))
        self.btn_dl_full.pack(side="left", padx=4)
        self.btn_dl_full.configure(state="disabled")

        self.btn_dl_log = ttk.Button(btn_bar, text="📋 Download Change Log Only (.xlsx)", command=lambda: self._export(True))
        self.btn_dl_log.pack(side="left", padx=4)
        self.btn_dl_log.configure(state="disabled")

        # Metric Pills
        pills_box = ttk.Frame(self)
        pills_box.pack(fill="x", pady=4)
        self.pill_updated = MetricPill(pills_box, "Updated Rows")
        self.pill_updated.pack(side="left", padx=4, expand=True, fill="x")
        self.pill_added = MetricPill(pills_box, "New Rows Added")
        self.pill_added.pack(side="left", padx=4, expand=True, fill="x")
        self.pill_unchanged = MetricPill(pills_box, "Unchanged Rows")
        self.pill_unchanged.pack(side="left", padx=4, expand=True, fill="x")
        self.pill_cells = MetricPill(pills_box, "Total Cells Changed")
        self.pill_cells.pack(side="left", padx=4, expand=True, fill="x")

        # Change Log Diff Table
        ttk.Label(self, text="Live Audit Trail & Diff Table (Old vs New):", style="Muted.TLabel").pack(anchor="w", pady=(6, 2))
        self.diff_table = DiffTable(self)
        self.diff_table.pack(fill="both", expand=True)

    def _load_file(self, target: str, path: str) -> None:
        p = Path(path)
        headers, rows, sheets = read_tabular_file(p, 0, 0)
        if not headers:
            messagebox.showerror("Error", f"Failed to parse {p.name}")
            return

        if target == "old":
            self.old_path = str(p)
            self.old_headers = headers
            self.old_rows = rows
            self.cb_sheet_old["values"] = sheets
            if sheets:
                self.cb_sheet_old.current(0)
        else:
            self.new_path = str(p)
            self.new_headers = headers
            self.new_rows = rows
            self.cb_sheet_new["values"] = sheets
            if sheets:
                self.cb_sheet_new.current(0)

        # Update key column dropdown with common headers
        self._refresh_common_keys()

    def _on_sheet_changed(self, target: str) -> None:
        path = self.old_path if target == "old" else self.new_path
        if not path:
            return
        cb_s = self.cb_sheet_old if target == "old" else self.cb_sheet_new
        cb_h = self.cb_hdr_old if target == "old" else self.cb_hdr_new
        s_idx = cb_s.current()
        h_idx = cb_h.current()
        headers, rows, _ = read_tabular_file(path, s_idx, h_idx)
        if target == "old":
            self.old_headers = headers
            self.old_rows = rows
        else:
            self.new_headers = headers
            self.new_rows = rows
        self._refresh_common_keys()

    def _refresh_common_keys(self) -> None:
        if self.old_headers and self.new_headers:
            common = [h for h in self.old_headers if h in self.new_headers]
            self.cb_key_col["values"] = common
            if common and not self.v_key_col.get():
                # Pick first or one matching id
                best = next((c for c in common if "id" in c.lower() or "code" in c.lower() or "key" in c.lower()), common[0])
                self.v_key_col.set(best)

    def _execute_sync(self) -> None:
        if not self.old_rows or not self.new_rows:
            messagebox.showinfo("Files Required", "Please load both Master and Updated sheets.")
            return

        key_col = self.v_key_col.get()
        if not key_col:
            messagebox.showwarning("Key Required", "Please select a Unique Key column shared between both sheets.")
            return

        options = {
            "ignore_case_and_spaces": self.v_ignore_case.get(),
            "keep_old_on_blank": self.v_skip_blank.get(),
            "add_new_rows": self.v_add_rows.get(),
            "add_new_columns": self.v_add_cols.get(),
            "include_status_column": self.v_add_status.get(),
        }

        # Select all shared columns
        selected_cols = [h for h in self.old_headers if h in self.new_headers and h != key_col]

        final_headers, updated_rows, change_log, metrics = run_sheet_sync(
            self.old_headers, self.old_rows,
            self.new_headers, self.new_rows,
            key_col, selected_cols, options
        )

        self.updated_headers = final_headers
        self.updated_rows = updated_rows
        self.change_log = change_log

        # Update KPI pills
        self.pill_updated.set(metrics["updated_rows"])
        self.pill_added.set(metrics["new_rows_added"])
        self.pill_unchanged.set(metrics["unchanged_rows"])
        self.pill_cells.set(metrics["cells_changed"])

        # Populate Diff Table
        self.diff_table.populate(change_log)

        self.btn_dl_full.configure(state="normal")
        self.btn_dl_log.configure(state="normal")
        messagebox.showinfo("Sync Complete", f"Synchronized successfully!\nUpdated: {metrics['updated_rows']} rows\nAdded: {metrics['new_rows_added']} rows\nCells Modified: {metrics['cells_changed']}")

    def _export(self, log_only: bool) -> None:
        if not self.change_log and not self.updated_rows:
            return
        out_path = filedialog.asksaveasfilename(
            title="Save Synchronized Excel File",
            defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx")]
        )
        if not out_path:
            return

        ok = export_sync_workbook(out_path, self.updated_headers, self.updated_rows, self.change_log, log_only=log_only)
        if ok:
            messagebox.showinfo("Saved", f"Exported workbook to:\n{out_path}")
        else:
            messagebox.showerror("Export Failed", "Could not write Excel workbook.")

