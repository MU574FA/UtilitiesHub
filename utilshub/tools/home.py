"""Home: dashboard with tool cards, drop zone and recent activity."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..ui.dnd import DropZone
from ..ui.widgets import LogView
from .base import Tool


class HomeTool(Tool):
    id = "home"
    title = "Home"
    icon = "🏠"
    description = "Overview, quick drop and recent activity."
    order = 0

    @classmethod
    def pages(cls):
        return []

    def build(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Utils Hub", style="H2.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Everything in one place. Pick a tool, or drop files below.", style="Muted.TLabel").pack(anchor="w")
        self.cards = ttk.Frame(parent)
        self.cards.pack(fill="x", pady=10)
        self.target = tk.StringVar()
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Send dropped files to:").pack(side="left")
        self.combo = ttk.Combobox(row, textvariable=self.target, state="readonly", width=28)
        self.combo.pack(side="left", padx=6)
        DropZone(parent, "Drop files here", self._dropped, theme=self.app.theme).pack(fill="x", pady=4)
        ttk.Label(parent, text="Recent activity", style="H3.TLabel").pack(anchor="w", pady=(10, 2))
        self.logview = LogView(parent, height=10)
        self.logview.pack(fill="both", expand=True)
        for stamp, level, tool, msg in list(self.app.logs):
            self.logview.log(f"{tool + ': ' if tool else ''}{msg}", level)
        self.app.on_log(lambda s, lv, t, m: self.frame.winfo_exists() and self.logview.log(f"{t + ': ' if t else ''}{m}", lv))
        self.refresh()

    def _targets(self):
        return [t for t in self.app.visible_tools() if t.id not in ("home", "autopilot", "history")]

    def refresh(self) -> None:
        for w in self.cards.winfo_children():
            w.destroy()
        tools = self._targets()
        cols = 3
        for i, t in enumerate(tools):
            c = ttk.Frame(self.cards, padding=10, relief="groove")
            c.grid(row=i // cols, column=i % cols, sticky="nsew", padx=4, pady=4)
            self.cards.columnconfigure(i % cols, weight=1)
            ttk.Label(c, text=f"{t.icon}  {t.title}", style="H3.TLabel").pack(anchor="w")
            ttk.Label(c, text=t.description, style="Muted.TLabel", wraplength=260, justify="left").pack(anchor="w")
            st = t.stats() if t.built else {}
            if st:
                ttk.Label(c, text="  ".join(f"{k}: {v}" for k, v in st.items())).pack(anchor="w", pady=(4, 0))
            ttk.Button(c, text="Open", command=lambda i=t.id: self.app.show_tool(i)).pack(anchor="w", pady=(6, 0))
        names = [f"{t.icon} {t.title}" for t in tools]
        self.combo.configure(values=names)
        if names and self.target.get() not in names:
            self.target.set(names[0])

    def on_show(self) -> None:
        self.refresh()

    def _dropped(self, paths: list[str]) -> None:
        for t in self._targets():
            if f"{t.icon} {t.title}" == self.target.get():
                self.app.send_to(t.id, paths)
                return

    def accept_files(self, paths: list[str]) -> None:
        self._dropped(paths)
