"""Command palette (Ctrl+K): type to filter every command of the app and of the open tool."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from .widgets import Tooltip, attach_window_controls


class CommandPalette(tk.Toplevel):
    _open: "CommandPalette | None" = None

    @classmethod
    def show(cls, app) -> None:
        if cls._open is not None and cls._open.winfo_exists():
            cls._open.destroy()
        cls._open = cls(app)

    def __init__(self, app) -> None:
        super().__init__(app.root)
        self.app = app
        self.overrideredirect(True)
        self.configure(background=app.theme.p["accent"], padx=2, pady=2)
        box = ttk.Frame(self, padding=8)
        box.pack(fill="both", expand=True)
        top_row = ttk.Frame(box)
        top_row.pack(fill="x", pady=(0, 6))
        ttk.Label(top_row, text="Commands (Ctrl+K)", style="Bold.TLabel").pack(side="left")
        attach_window_controls(self, bar=top_row)
        self.var = tk.StringVar()
        self.entry = ttk.Entry(box, textvariable=self.var, font=(None, 12))
        self.entry.pack(fill="x")
        self.lb = tk.Listbox(box, height=12, activestyle="none", borderwidth=0, highlightthickness=0, font=(None, 11))
        app.theme.style_listbox(self.lb)
        self.lb.pack(fill="both", expand=True, pady=(6, 0))
        self.cmds = app.all_commands()
        self.shown: list = []
        self.var.trace_add("write", lambda *_: self.refresh())
        for k, fn in (("<Return>", self.run), ("<Escape>", lambda e: self.destroy()),
                      ("<Down>", lambda e: self.move(1)), ("<Up>", lambda e: self.move(-1))):
            self.entry.bind(k, fn)
        self.lb.bind("<Double-1>", self.run)
        self.bind("<FocusOut>", lambda e: self.after(150, self._maybe_close))
        w = 560
        self.geometry(f"{w}x330+{app.root.winfo_rootx() + (app.root.winfo_width() - w) // 2}+{app.root.winfo_rooty() + 80}")
        self.refresh()
        self.entry.focus_force()

    def _maybe_close(self) -> None:
        if self.winfo_exists() and self.focus_get() is None:
            self.destroy()

    def refresh(self) -> None:
        q = self.var.get().lower().split()
        self.shown = [c for c in self.cmds if all(t in f"{c.group} {c.title} {c.keywords}".lower() for t in q)]
        self.lb.delete(0, "end")
        for c in self.shown:
            self.lb.insert("end", f"{c.group + '  ›  ' if c.group else ''}{c.title}" + (f"      {c.shortcut}" if c.shortcut else ""))
        if self.shown:
            self.lb.selection_set(0)

    def move(self, step: int) -> str:
        cur = (self.lb.curselection() or (0,))[0] + step
        cur = max(0, min(len(self.shown) - 1, cur))
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(cur)
        self.lb.see(cur)
        return "break"

    def run(self, _e=None) -> None:
        sel = self.lb.curselection()
        if not sel:
            return
        cmd = self.shown[sel[0]]
        self.destroy()
        self.app.root.after(30, cmd.callback)
