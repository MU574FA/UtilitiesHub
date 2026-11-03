"""Sheet Name Matcher: Bilingual Arabic/English Fuzzy Spreadsheet Reconciliation."""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from ..core.arabic import prepare_name_tokens
from ..core.config import AppConfig
from ..core.excel_ops import export_matcher_workbook, read_tabular_file
from ..core.fuzzy import rank_candidates
from ..ui.widgets import DropTarget, MetricPill


class MatcherView(ttk.Frame):
    """Bilingual Sheet Name Matcher and Candidate Reconciler."""
    def __init__(self, parent: Any, config: AppConfig, **kwargs):
        super().__init__(parent, padding=12, **kwargs)
        self.config = config

        self.data_a: dict[str, Any] = {"headers": [], "rows": [], "sheets": [], "path": ""}
        self.data_b: dict[str, Any] = {"headers": [], "rows": [], "sheets": [], "path": ""}

        self.selected_cols_a: list[str] = []
        self.selected_cols_b: list[str] = []

        self.matches_result: list[dict[str, Any]] = []
        self.all_candidates_result: list[dict[str, Any]] = []
        self.unmatched_b_result: list[dict[str, Any]] = []

        self._build_ui()

    def _build_ui(self) -> None:
        # Title
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="Sheet Name Matcher", style="Header.TLabel").pack(side="left")
        ttk.Label(top, text="Fuzzy reconciliation between two spreadsheets with specialized Arabic orthographic normalization",
                  style="Muted.TLabel").pack(side="left", padx=12, pady=(2, 0))

        # Sources Frame (Side-by-side cards for Sheet A and Sheet B)
        sources_box = ttk.Frame(self)
        sources_box.pack(fill="x", pady=(0, 8))
        sources_box.columnconfigure(0, weight=1)
        sources_box.columnconfigure(1, weight=1)

        # --------------------------------------------- Sheet A Card
        card_a = ttk.LabelFrame(sources_box, text="Source Sheet A (Master / Primary)", padding=8)
        card_a.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        self.drop_a = DropTarget(card_a, text="Drop Sheet A (.xlsx, .csv)", on_drop=lambda f: self._load_file("A", f[0]))
        self.drop_a.pack(fill="x", pady=(0, 4))

        meta_a = ttk.Frame(card_a)
        meta_a.pack(fill="x", pady=2)
        ttk.Label(meta_a, text="Sheet:").pack(side="left")
        self.cb_sheet_a = ttk.Combobox(meta_a, state="readonly", width=14)
        self.cb_sheet_a.pack(side="left", padx=4)
        self.cb_sheet_a.bind("<<ComboboxSelected>>", lambda e: self._on_sheet_changed("A"))

        ttk.Label(meta_a, text="Header:").pack(side="left", padx=(8, 2))
        self.cb_hdr_a = ttk.Combobox(meta_a, values=[f"Row {i}" for i in range(1, 6)], state="readonly", width=7)
        self.cb_hdr_a.current(0)
        self.cb_hdr_a.pack(side="left")
        self.cb_hdr_a.bind("<<ComboboxSelected>>", lambda e: self._on_sheet_changed("A"))

        ttk.Label(card_a, text="Select Name Column(s):", style="Muted.TLabel").pack(anchor="w", pady=(4, 2))
        self.chips_a = ttk.Frame(card_a)
        self.chips_a.pack(fill="x", pady=2)

        # --------------------------------------------- Sheet B Card
        card_b = ttk.LabelFrame(sources_box, text="Source Sheet B (Candidate / New)", padding=8)
        card_b.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self.drop_b = DropTarget(card_b, text="Drop Sheet B (.xlsx, .csv)", on_drop=lambda f: self._load_file("B", f[0]))
        self.drop_b.pack(fill="x", pady=(0, 4))

        meta_b = ttk.Frame(card_b)
        meta_b.pack(fill="x", pady=2)
        ttk.Label(meta_b, text="Sheet:").pack(side="left")
        self.cb_sheet_b = ttk.Combobox(meta_b, state="readonly", width=14)
        self.cb_sheet_b.pack(side="left", padx=4)
        self.cb_sheet_b.bind("<<ComboboxSelected>>", lambda e: self._on_sheet_changed("B"))

        ttk.Label(meta_b, text="Header:").pack(side="left", padx=(8, 2))
        self.cb_hdr_b = ttk.Combobox(meta_b, values=[f"Row {i}" for i in range(1, 6)], state="readonly", width=7)
        self.cb_hdr_b.current(0)
        self.cb_hdr_b.pack(side="left")
        self.cb_hdr_b.bind("<<ComboboxSelected>>", lambda e: self._on_sheet_changed("B"))

        ttk.Label(card_b, text="Select Name Column(s):", style="Muted.TLabel").pack(anchor="w", pady=(4, 2))
        self.chips_b = ttk.Frame(card_b)
        self.chips_b.pack(fill="x", pady=2)

        # Settings & Controls Strip
        ctrl_strip = ttk.Frame(self, style="Surface2.TFrame", padding=8)
        ctrl_strip.pack(fill="x", pady=6)

        # Match threshold slider
        ttk.Label(ctrl_strip, text="Match Threshold:").pack(side="left", padx=4)
        self.v_thr = tk.IntVar(value=self.config.matcher.get("threshold", 80))
        scale = ttk.Scale(ctrl_strip, from_=50, to=100, variable=self.v_thr, orient="horizontal", length=120)
        scale.pack(side="left", padx=4)
        self.lbl_thr_val = ttk.Label(ctrl_strip, text=f"{self.v_thr.get()}%", font=("Segoe UI", 9, "bold"))
        self.lbl_thr_val.pack(side="left", padx=4)
        scale.configure(command=lambda v: self.lbl_thr_val.configure(text=f"{int(float(v))}%"))

        # Top N candidates
        ttk.Label(ctrl_strip, text="Top Candidates:").pack(side="left", padx=(12, 4))
        self.v_top_n = tk.IntVar(value=self.config.matcher.get("top_n", 3))
        ttk.Combobox(ctrl_strip, textvariable=self.v_top_n, values=[1, 2, 3, 5], state="readonly", width=4).pack(side="left")

        # Run Button
        self.btn_run = ttk.Button(ctrl_strip, text="⚡ Reconcile Names", style="Primary.TButton", command=self._start_reconciliation)
        self.btn_run.pack(side="left", padx=16)

        self.btn_export = ttk.Button(ctrl_strip, text="📥 Download Results (.xlsx)", command=self._export_results)
        self.btn_export.pack(side="left", padx=4)
        self.btn_export.configure(state="disabled")

        # Metric Pills
        pills_box = ttk.Frame(self)
        pills_box.pack(fill="x", pady=4)
        self.pill_total = MetricPill(pills_box, "Total Rows (A)")
        self.pill_total.pack(side="left", padx=4, expand=True, fill="x")
        self.pill_strong = MetricPill(pills_box, "Strong (≥90%)")
        self.pill_strong.pack(side="left", padx=4, expand=True, fill="x")
        self.pill_prob = MetricPill(pills_box, "Probable (≥Thr)")
        self.pill_prob.pack(side="left", padx=4, expand=True, fill="x")
        self.pill_weak = MetricPill(pills_box, "Weak / Unmatched")
        self.pill_weak.pack(side="left", padx=4, expand=True, fill="x")

        # Filter and Search Bar
        filter_bar = ttk.Frame(self)
        filter_bar.pack(fill="x", pady=(6, 2))

        ttk.Label(filter_bar, text="Filter:").pack(side="left", padx=(4, 2))
        self.v_pill_filter = tk.StringVar(value="All")
        for f_label in ["All", "Strong", "Probable", "Weak"]:
            ttk.Radiobutton(filter_bar, text=f_label, variable=self.v_pill_filter, value=f_label,
                            command=self._apply_ledger_filter).pack(side="left", padx=2)

        ttk.Label(filter_bar, text="Search:").pack(side="left", padx=(16, 2))
        self.v_search = tk.StringVar()
        entry_search = ttk.Entry(filter_bar, textvariable=self.v_search, width=22)
        entry_search.pack(side="left", padx=4)
        entry_search.bind("<KeyRelease>", lambda e: self._apply_ledger_filter())

        # Progress bar
        self.prog = ttk.Progressbar(self, orient="horizontal", mode="determinate")
        self.prog.pack(fill="x", pady=2)

        # Ledger Table
        cols = ("row_a", "name_a", "best_b", "sim", "status")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=9)
        self.tree.heading("row_a", text="Row (A)")
        self.tree.heading("name_a", text="Name in Sheet A")
        self.tree.heading("best_b", text="Best Match in Sheet B")
        self.tree.heading("sim", text="Similarity")
        self.tree.heading("status", text="Classification")

        self.tree.column("row_a", width=60)
        self.tree.column("name_a", width=220)
        self.tree.column("best_b", width=240)
        self.tree.column("sim", width=100)
        self.tree.column("status", width=110)

        sb_ledger = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb_ledger.set)
        self.tree.pack(fill="both", expand=True)
        sb_ledger.pack(in_=self.tree, side="right", fill="y")

    def _load_file(self, target: str, path: str) -> None:
        p = Path(path)
        hdr_row = 0
        headers, rows, sheets = read_tabular_file(p, 0, hdr_row)
        if not headers:
            messagebox.showerror("Error", f"Could not read spreadsheet {p.name}")
            return

        storage = self.data_a if target == "A" else self.data_b
        storage["path"] = str(p)
        storage["headers"] = headers
        storage["rows"] = rows
        storage["sheets"] = sheets

        cb_sheet = self.cb_sheet_a if target == "A" else self.cb_sheet_b
        cb_sheet["values"] = sheets
        if sheets:
            cb_sheet.current(0)

        # Build column selection chips
        self._build_column_chips(target, headers)

    def _on_sheet_changed(self, target: str) -> None:
        storage = self.data_a if target == "A" else self.data_b
        if not storage["path"]:
            return
        cb_sheet = self.cb_sheet_a if target == "A" else self.cb_sheet_b
        cb_hdr = self.cb_hdr_a if target == "A" else self.cb_hdr_b
        s_idx = cb_sheet.current()
        h_idx = cb_hdr.current()
        headers, rows, _ = read_tabular_file(storage["path"], s_idx, h_idx)
        storage["headers"] = headers
        storage["rows"] = rows
        self._build_column_chips(target, headers)

    def _build_column_chips(self, target: str, headers: list[str]) -> None:
        frame = self.chips_a if target == "A" else self.chips_b
        for w in frame.winfo_children():
            w.destroy()

        import re
        sel_list = []
        for h in headers:
            var = tk.BooleanVar()
            # Autodetect name column
            if re.search(r"name|full|اسم|الاسم|الإسم", h, re.I):
                var.set(True)
                sel_list.append(h)
            
            cb = ttk.Checkbutton(frame, text=h, variable=var,
                                 command=lambda col=h, v=var, t=target: self._on_chip_toggled(t, col, v.get()))
            cb.pack(side="left", padx=2, pady=1)

        if target == "A":
            self.selected_cols_a = sel_list or (headers[:1] if headers else [])
        else:
            self.selected_cols_b = sel_list or (headers[:1] if headers else [])

    def _on_chip_toggled(self, target: str, col: str, is_checked: bool) -> None:
        sel = self.selected_cols_a if target == "A" else self.selected_cols_b
        if is_checked and col not in sel:
            sel.append(col)
        elif not is_checked and col in sel:
            sel.remove(col)

    def _start_reconciliation(self) -> None:
        if not self.data_a["rows"] or not self.data_b["rows"]:
            messagebox.showinfo("Sources Required", "Please load both Sheet A and Sheet B before running reconciliation.")
            return

        self.btn_run.configure(state="disabled")
        self.prog["value"] = 5

        def worker():
            try:
                # 1. Prepare candidate objects
                prep_a = []
                for idx, r in enumerate(self.data_a["rows"]):
                    raw_val = " ".join(str(r.get(c, "")).strip() for c in self.selected_cols_a if str(r.get(c, "")).strip())
                    p = prepare_name_tokens(raw_val)
                    p["original_row"] = r
                    p["row_idx"] = idx + 2
                    prep_a.append(p)

                prep_b = []
                for idx, r in enumerate(self.data_b["rows"]):
                    raw_val = " ".join(str(r.get(c, "")).strip() for c in self.selected_cols_b if str(r.get(c, "")).strip())
                    p = prepare_name_tokens(raw_val)
                    p["original_row"] = r
                    p["row_idx"] = idx + 2
                    prep_b.append(p)

                matches = []
                all_cands = []
                matched_b_indices = set()

                thr = self.v_thr.get() / 100.0
                top_n = self.v_top_n.get()

                # Pairwise matching
                total_a = len(prep_a)
                for i, item_a in enumerate(prep_a):
                    if i % 25 == 0:
                        prog_pct = int(10 + (i / total_a) * 80)
                        self.after(0, lambda v=prog_pct: self.prog.configure(value=v))

                    ranked = rank_candidates(item_a, prep_b, top_n=top_n, min_score=0.40)
                    best_cand = ranked[0] if ranked else None
                    
                    if best_cand:
                        score = best_cand["score"]
                        b_cand_item = best_cand["candidate"]
                        b_idx = best_cand["index"]
                        
                        if score >= 0.90:
                            status = "Strong"
                            matched_b_indices.add(b_idx)
                        elif score >= thr:
                            status = "Probable"
                            matched_b_indices.add(b_idx)
                        else:
                            status = "Weak"

                        matches.append({
                            "row_a": item_a["original_row"],
                            "row_num_a": item_a["row_idx"],
                            "name_a": item_a["raw"],
                            "best_b_name": b_cand_item["raw"],
                            "row_b": b_cand_item["original_row"],
                            "similarity": score,
                            "status": status,
                        })

                        # Collect top N
                        for rank_idx, c_info in enumerate(ranked, 1):
                            all_cands.append({
                                "a_name": item_a["raw"],
                                "a_row": item_a["row_idx"],
                                "rank": rank_idx,
                                "b_name": c_info["candidate"]["raw"],
                                "b_row": c_info["candidate"]["row_idx"],
                                "score": c_info["score"],
                            })
                    else:
                        matches.append({
                            "row_a": item_a["original_row"],
                            "row_num_a": item_a["row_idx"],
                            "name_a": item_a["raw"],
                            "best_b_name": "(No candidate)",
                            "row_b": {},
                            "similarity": 0.0,
                            "status": "Weak",
                        })

                # Unmatched in B
                unmatched_b = [item_b["original_row"] for idx, item_b in enumerate(prep_b) if idx not in matched_b_indices]

                self.after(0, lambda: self._on_match_done(matches, all_cands, unmatched_b))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Matching Failed", str(exc)))
                self.after(0, lambda: self.btn_run.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_match_done(self, matches: list, all_cands: list, unmatched_b: list) -> None:
        self.btn_run.configure(state="normal")
        self.btn_export.configure(state="normal")
        self.prog["value"] = 100

        self.matches_result = matches
        self.all_candidates_result = all_cands
        self.unmatched_b_result = unmatched_b

        # Update metric pills
        c_strong = sum(1 for m in matches if m["status"] == "Strong")
        c_prob = sum(1 for m in matches if m["status"] == "Probable")
        c_weak = sum(1 for m in matches if m["status"] == "Weak")

        self.pill_total.set(len(matches))
        self.pill_strong.set(c_strong)
        self.pill_prob.set(c_prob)
        self.pill_weak.set(c_weak)

        self._apply_ledger_filter()

    def _apply_ledger_filter(self) -> None:
        self.tree.delete(*self.tree.get_children())
        p_filt = self.v_pill_filter.get()
        q = self.v_search.get().lower().strip()

        for m in self.matches_result:
            if p_filt != "All" and m["status"] != p_filt:
                continue
            if q and q not in m["name_a"].lower() and q not in m["best_b_name"].lower():
                continue

            sim_txt = f"{round(m['similarity'] * 100, 1)}%" if m['similarity'] > 0 else "-"
            self.tree.insert("", "end", values=(
                m["row_num_a"],
                m["name_a"],
                m["best_b_name"],
                sim_txt,
                m["status"],
            ))

    def _export_results(self) -> None:
        if not self.matches_result:
            return
        out_path = filedialog.asksaveasfilename(
            title="Save Match Results",
            defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx")]
        )
        if not out_path:
            return

        ok = export_matcher_workbook(
            out_path,
            self.data_a["headers"],
            self.data_b["headers"],
            self.matches_result,
            self.all_candidates_result,
            self.unmatched_b_result,
        )
        if ok:
            messagebox.showinfo("Export Successful", f"Match results saved with 3 sheets to:\n{out_path}")
        else:
            messagebox.showerror("Export Failed", "Could not export workbook.")

