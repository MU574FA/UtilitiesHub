"""History: journal of file operations with undo, and the session log."""
from __future__ import annotations

import csv
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from ..core.schema import Opt
from ..core.sheets import formula_safe
from ..core.system import open_path
from ..ui.widgets import LogView, confirm, error, info
from .base import Tool


class HistoryTool(Tool):
    id = "history"
    title = "History & Undo"
    icon = "↶"
    description = "Every file move/copy/rename, with undo, and the activity log."
    order = 95
    output_path_key = "output_folder"
    stats_when_closed = True          # the journal is loaded by the app, so the count is always known

    @classmethod
    def options(cls) -> list[Opt]:
        return [Opt("history.output_folder", "", "Output folder (export list)", "folder", section="Folders",
                    help="Default folder when exporting the file operations list.")]

    @classmethod
    def pages(cls):
        return []

    def build(self, parent: ttk.Frame) -> None:
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)
        jf = ttk.Frame(nb, padding=6)
        nb.add(jf, text="File operations")
        bar = ttk.Frame(jf)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Tool").pack(side="left")
        self.filter = tk.StringVar(value="all")
        self.fcombo = ttk.Combobox(bar, textvariable=self.filter, state="readonly", width=16)
        self.fcombo.pack(side="left", padx=6)
        self.fcombo.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Label(bar, text="Search").pack(side="left", padx=(12, 0))
        self.search = tk.StringVar()
        ttk.Entry(bar, textvariable=self.search, width=22).pack(side="left", padx=6)
        self.search.trace_add("write", lambda *_: self.refresh())
        ttk.Button(bar, text="Undo last", style="Accent.TButton", command=self.undo_last).pack(side="right")
        ttk.Button(bar, text="Undo last batch", command=self.undo_last_batch).pack(side="right", padx=6)
        ttk.Button(bar, text="Undo selected", command=self.undo_selected).pack(side="right")
        tools = ttk.Frame(jf)
        tools.pack(fill="x", pady=(0, 6))
        ttk.Button(tools, text="Show in folder", style="Tiny.TButton", command=self.show_selected).pack(side="left")
        ttk.Button(tools, text="Export list…", style="Tiny.TButton", command=self.export).pack(side="left", padx=6)
        self.count = ttk.Label(tools, text="", style="Muted.TLabel")
        self.count.pack(side="right")
        cols = ("time", "tool", "action", "src", "dest", "state")
        self.tree = ttk.Treeview(jf, columns=cols, show="headings", selectmode="extended")
        for c, w in zip(cols, (140, 90, 70, 300, 300, 80)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(jf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        lf = ttk.Frame(nb, padding=6)
        nb.add(lf, text="Activity log")
        self.logview = LogView(lf, height=20)
        self.logview.pack(fill="both", expand=True)
        for s, lv, t, m in list(self.app.logs):
            self.logview.log(f"{t + ': ' if t else ''}{m}", lv)
        self.app.on_log(lambda s, lv, t, m: self.frame.winfo_exists() and self.logview.log(f"{t + ': ' if t else ''}{m}", lv))
        self.refresh()

    def refresh(self) -> None:
        j = self.app.journal
        tools = sorted({e.tool for e in j.entries()})
        self.fcombo.configure(values=["all"] + tools)
        flt = self.filter.get()
        self.tree.delete(*self.tree.get_children())
        needle = self.search.get().strip().lower()
        shown = 0
        for e in reversed(j.entries(None if flt == "all" else flt)):
            if needle and needle not in f"{e.tool} {e.action} {e.src} {e.dest}".lower():
                continue
            shown += 1
            self.tree.insert("", "end", iid=e.id, values=(e.time.replace("T", " "), e.tool, e.action, e.src, e.dest,
                                                          "undone" if e.undone else ""))
        self.count.configure(text=f"{shown} entr{'y' if shown == 1 else 'ies'}")

    def stats(self) -> dict:
        return {"Can be undone": sum(1 for e in self.app.journal.entries() if not e.undone and e.action in ("move", "copy", "rename"))}

    def on_show(self) -> None:
        self.refresh()

    def _undo(self, entries) -> None:
        done, failed = 0, []
        for e in entries:
            try:
                self.app.journal.undo(e)
                done += 1
            except OSError as exc:
                failed.append(f"{e.dest}: {exc}")
        self.refresh()
        self.app.log(f"Undid {done} operation(s)", "ok" if done else "warn", tool=self.id)
        if failed:
            error(self.frame, "Could not undo:\n" + "\n".join(failed[:8]))

    def undo_selected(self) -> None:
        ids = set(self.tree.selection())
        entries = [e for e in self.app.journal.entries() if e.id in ids][::-1]
        if entries and confirm(self.frame, f"Undo {len(entries)} operation(s)?"):
            self._undo(entries)

    def undo_last(self) -> None:
        flt = self.filter.get()
        e = self.app.journal.last_undoable(None if flt == "all" else flt)
        if e is None:
            info(self.frame, "Nothing to undo.")
        elif confirm(self.frame, f"Undo: {e.action} {e.dest}?"):
            self._undo([e])

    def undo_last_batch(self) -> None:
        """One click undoes everything one run did (all files moved/renamed by the same Move/Clean/Workflow run)."""
        flt = self.filter.get()
        tool = None if flt == "all" else flt
        batch = self.app.journal.last_batch(tool)
        if not batch:
            return self.undo_last()                      # the newest entry is not part of a batch: undo just that one
        if confirm(self.frame, "Undo everything the last run did?"):
            done, problems = self.app.journal.undo_batch(batch, tool)
            self.refresh()
            self.app.log(f"Undid {done} operation(s) of the last run", "ok" if done else "warn", tool=self.id)
            if problems:
                error(self.frame, "Could not undo:\n" + "\n".join(problems[:8]))

    def show_selected(self) -> None:
        ids = self.tree.selection()
        entry = next((e for e in self.app.journal.entries() if ids and e.id == ids[0]), None)
        if entry is None:
            info(self.frame, "Select an entry first.")
            return
        target = Path(entry.dest or entry.src)
        open_path(target if target.is_dir() else target.parent)

    def export(self) -> None:
        path = filedialog.asksaveasfilename(title="Export the list", defaultextension=".csv",
                                            initialdir=self.output_dir(),
                                            filetypes=[("CSV (opens in Excel)", "*.csv")], parent=self.frame.winfo_toplevel())
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["time", "tool", "action", "from", "to", "state"])
                for iid in self.tree.get_children():
                    w.writerow([formula_safe(str(v)) for v in self.tree.item(iid, "values")])
        except OSError as exc:
            error(self.frame, f"Could not write the file: {exc}")
            return
        self.app.notify(f"Exported {len(self.tree.get_children())} entries", "ok")
