"""History: journal of file operations with undo, and the session log."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..ui.widgets import LogView, confirm, error, info
from .base import Tool


class HistoryTool(Tool):
    id = "history"
    title = "History & Undo"
    icon = "↶"
    description = "Every file move/copy/rename, with undo, and the activity log."
    order = 95

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
        ttk.Button(bar, text="Undo last", style="Accent.TButton", command=self.undo_last).pack(side="right")
        ttk.Button(bar, text="Undo selected", command=self.undo_selected).pack(side="right", padx=6)
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
        for e in reversed(j.entries(None if flt == "all" else flt)):
            self.tree.insert("", "end", iid=e.id, values=(e.time.replace("T", " "), e.tool, e.action, e.src, e.dest,
                                                          "undone" if e.undone else ""))

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
