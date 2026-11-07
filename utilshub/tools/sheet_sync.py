"""Sheet Sync: update an old spreadsheet from a new one by a key column, accepting or rejecting each change."""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, ttk

from ..core.schema import Opt
from ..core.sheets import SheetError, Table, cell_text, load_book, write_csv, write_xlsx
from ..core.syncing import SyncOptions, SyncPlan, apply_plan, build_plan, change_log_rows, shared_columns

from ..ui.widgets import DataTable, error, info
from .base import Command, Tool
from .name_matcher import SheetPicker

KIND = {"update": "Update", "add": "Add row", "missing": "Not in new file"}

class SheetSync(Tool):
    id = "sheet_sync"
    title = "Sheet Sync"
    icon = "🔄"
    order = 45
    description = "Update an old sheet from a new one by key; review every change before applying."

    @classmethod
    def options(cls) -> list[Opt]:
        p = "sheet_sync."
        return [
            Opt(p + "ignore_case", True, "Ignore case when matching keys", "bool"),
            Opt(p + "add_new", True, "Add rows that exist only in the new file", "bool"),
            Opt(p + "skip_blank", True, "Keep the old value when the new cell is empty", "bool"),
            Opt(p + "add_columns", False, "Add columns that exist only in the new file", "bool"),
            Opt(p + "keep_missing", True, "Keep rows that are not found in the new file", "bool"),
            Opt(p + "tag_missing", True, "Tag kept-missing rows in the status column", "bool"),
            Opt(p + "missing_label", "Not in new file", "Status label for missing rows", "str", advanced=True),
            Opt(p + "status_column", True, "Add a 'Sync status' column", "bool"),
            Opt(p + "status_name", "Sync status", "Status column title", "str", advanced=True),
            Opt(p + "old_path", "", "Last old file", "file", advanced=True),
            Opt(p + "new_path", "", "Last new file", "file", advanced=True),
            *cls.append_opts(),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.plan: SyncPlan | None = None

    def build(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)
        self.old = SheetPicker(top, "Old file (will be updated)", on_load=self._loaded, pick_column=False, path=self.get("old_path"))
        self.new = SheetPicker(top, "New file (the fresh data)", on_load=self._loaded, pick_column=False, path=self.get("new_path"))
        self.old.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.new.grid(row=0, column=1, sticky="ew")
        self.old.path.trace_add("write", lambda *_: self.put("old_path", self.old.path.get()))
        self.new.path.trace_add("write", lambda *_: self.put("new_path", self.new.path.get()))

        mid = ttk.Frame(parent)
        mid.pack(fill="x", pady=6)
        ttk.Label(mid, text="Key column").pack(side="left")
        self.key = tk.StringVar()
        self.key_cb = ttk.Combobox(mid, textvariable=self.key, state="readonly", width=26)
        self.key_cb.pack(side="left", padx=4)
        ttk.Label(mid, text="Columns to update").pack(side="left", padx=(12, 4))
        self.cols = tk.Listbox(mid, selectmode="multiple", height=4, exportselection=False, width=28)
        self.app.theme.style_listbox(self.cols)
        self.cols.pack(side="left")
        cb = ttk.Frame(mid)
        cb.pack(side="left", padx=8)
        ttk.Button(cb, text="All", style="Tiny.TButton", command=lambda: self.cols.selection_set(0, "end")).pack(fill="x")
        ttk.Button(cb, text="None", style="Tiny.TButton", command=lambda: self.cols.selection_clear(0, "end")).pack(fill="x", pady=2)
        self.flags: dict[str, tk.BooleanVar] = {}
        flagbox = ttk.Frame(mid)
        flagbox.pack(side="left", padx=8)
        for key, text in (("ignore_case", "Ignore case in keys"), ("add_new", "Add new rows"),
                          ("skip_blank", "Keep old value if new is empty"), ("add_columns", "Add new columns"),
                          ("keep_missing", "Keep rows missing from new file"),
                          ("tag_missing", "  ↳ Tag missing rows in status column"),
                          ("status_column", "Status column")):
            v = tk.BooleanVar(value=self.get(key))
            v.trace_add("write", lambda *_a, k=key, vv=v: self.put(k, vv.get()))
            self.flags[key] = v
            ttk.Checkbutton(flagbox, text=text, variable=v).pack(anchor="w")

        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="🔍 Preview changes", style="Accent.TButton", command=self.preview).pack(side="left")
        self.apply_btn = ttk.Button(bar, text="Save updated file…", command=self.apply, state="disabled")
        self.apply_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="Accept all", style="Tiny.TButton", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(bar, text="Reject all", style="Tiny.TButton", command=lambda: self.set_all(False)).pack(side="left", padx=2)
        ttk.Button(bar, text="Change log…", command=self.export_log).pack(side="right")
        ttk.Button(bar, text="Add log to file…", command=self.add_log_to_file).pack(side="right", padx=6)
        self.summary = ttk.Label(parent, style="Muted.TLabel", wraplength=900, justify="left")
        self.summary.pack(anchor="w")
        self.table = DataTable(parent, [("ok", "Apply", 50, "center"), ("kind", "Change", 110), ("key", "Key", 180),
                                        ("col", "Column", 140), ("old", "Old value", 200), ("new", "New value", 200)],
                               searchable=True, on_double=self.toggle, stretch="new")
        self.table.pack(fill="both", expand=True)
        self.table.tag_colors(update="info", add="ok", missing="muted", off="muted")
        ttk.Label(parent, text="Double-click a change to accept / reject it.", style="Muted.TLabel").pack(anchor="w")
        self._loaded(None)

    def _loaded(self, _t) -> None:
        o, n = (self.old.table(), self.new.table()) if hasattr(self, "new") and hasattr(self, "old") else (None, None)
        if not o or not n:
            if o and hasattr(self, "key_cb"):
                self.key_cb.configure(values=o.headers)
            return
        shared = shared_columns(o, n)
        self.key_cb.configure(values=shared)
        if self.key.get() not in shared and shared:
            self.key.set(shared[0])
        self.cols.delete(0, "end")
        for h in shared:
            self.cols.insert("end", h)
        self.cols.selection_set(0, "end")

    # ------------------------------------------------------------------ preview
    def options_from_ui(self) -> SyncOptions:
        shared = list(self.cols.get(0, "end"))
        chosen = [shared[i] for i in self.cols.curselection() if shared[i] != self.key.get()]
        f = {k: v.get() for k, v in self.flags.items()}
        return SyncOptions(key=self.key.get(), columns=chosen, status_name=self.get("status_name"),
                           missing_label=self.get("missing_label"), **f)
    def preview(self) -> None:
        o, n = self.old.table(), self.new.table()
        if not o or not n:
            info(self.frame, "Load the old and the new file first.")
            return
        opts = self.options_from_ui()
        if not opts.key or opts.key not in o.headers or opts.key not in n.headers:
            info(self.frame, "Pick a key column that exists in both files.")
            return
        self.plan = build_plan(o, n, opts)
        self.fill()
        self.apply_btn.configure(state="normal")
        self.log(f"Sync preview: {self.plan.stats()['total_changes']} changes", "ok")

    def fill(self) -> None:
        plan = self.plan
        self.table.clear()
        for i, c in enumerate(plan.changes):
            self.table.add({"ok": self._mark(c), "kind": KIND[c.kind], "key": c.key, "col": c.column,
                            "old": cell_text(c.old), "new": cell_text(c.new) if c.kind != "add" else "(whole row)"},
                           iid=f"c{i}", tags=(c.kind,) if c.accepted or c.kind == "missing" else ("off",))
        st = plan.stats()
        opts = plan.options
        missing_note = (f"{st['missing']} old rows not in the new file "
                        f"({'kept & tagged' if opts.keep_missing and opts.tag_missing else 'kept' if opts.keep_missing else 'removed'})")
        text = (f"{st['cells']} cells in {st['updated_rows']} rows will change • {st['added']} rows added • "
                + missing_note)
        if plan.warnings:
            text += "\n⚠ " + "  ⚠ ".join(plan.warnings[:4])
        self.summary.configure(text=text)

    @staticmethod
    def _mark(c) -> str:
        return "—" if c.kind == "missing" else ("✔" if c.accepted else "✘")

    def toggle(self, iid: str) -> None:
        c = self.plan.changes[int(iid[1:])]
        if c.kind != "missing":
            c.accepted = not c.accepted
            self.fill()

    def set_all(self, on: bool) -> None:
        if self.plan:
            for c in self.plan.changes:
                if c.kind != "missing":
                    c.accepted = on
            self.fill()

    # ------------------------------------------------------------------ output
    def apply(self) -> None:
        if not self.plan:
            return
        src = self.old.path.get().strip().strip('"')
        ext = ".csv" if src.lower().endswith((".csv", ".tsv", ".txt")) else ".xlsx"
        path = filedialog.asksaveasfilename(defaultextension=ext, initialfile=f"updated{ext}",
                                            filetypes=[("Spreadsheet", f"*{ext}")])
        if not path:
            return
        try:
            apply_plan(self.plan, self.old.book, path)
        except (OSError, SheetError, ValueError) as exc:
            error(self.frame, f"Could not write the file: {exc}")
            return
        self.app.notify(f"Saved {path}", "ok")
        self.log(f"Updated sheet saved to {path}", "ok")

    def export_log(self) -> None:
        if not self.plan:
            info(self.frame, "Preview first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", initialfile="sync-log.xlsx",
                                            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if path:
            rows = change_log_rows(self.plan)
            (write_csv(path, rows) if path.endswith(".csv") else write_xlsx(path, {"Changes": rows}))

    def add_log_to_file(self) -> None:
        if not self.plan:
            info(self.frame, "Preview first.")
            return
        rows = change_log_rows(self.plan)
        if len(rows) > 1:
            self.append_rows([dict(zip(rows[0], r)) for r in rows[1:]], ask=True)

    def accept_files(self, paths: list[str]) -> None:
        for p, picker in zip(paths[:2], (self.old, self.new)):
            picker.path.set(p)

    def keys(self) -> dict:
        return {"<F5>": self.preview, "<Control-s>": self.apply}

    def commands(self) -> list[Command]:
        return [Command("ss.preview", "Sheet Sync: preview changes", self.preview, "Sheet Sync")]
