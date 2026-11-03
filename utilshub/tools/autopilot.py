"""Autopilot: one switchboard for every tool that can run by itself."""
from __future__ import annotations

from tkinter import ttk

from ..core.schema import Opt
from .base import Tool


class AutopilotTool(Tool):
    id = "autopilot"
    title = "Autopilot"
    icon = "🤖"
    description = "Start or stop the automatic watchers of all tools."
    order = 90

    @classmethod
    def pages(cls):
        return []

    def build(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Autopilot", style="H2.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Tools that can work without you. Global switches live in Settings > Automation.",
                  style="Muted.TLabel").pack(anchor="w")
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=8)
        ttk.Button(bar, text="Start all", style="Accent.TButton", command=self._all(True)).pack(side="left")
        ttk.Button(bar, text="Stop all", command=self._all(False)).pack(side="left", padx=6)
        ttk.Button(bar, text="Automation settings…", command=lambda: self.app.open_settings("auto")).pack(side="right")
        self.rows = ttk.Frame(parent)
        self.rows.pack(fill="both", expand=True)
        self.refresh()

    def _all(self, on: bool):
        def go():
            (self.app.start_all_auto if on else self.app.stop_all_auto)()
            self.refresh()
        return go

    def refresh(self) -> None:
        for w in self.rows.winfo_children():
            w.destroy()
        autos = [t for t in self.app.tools.values() if t.can_automate()]
        if not autos:
            ttk.Label(self.rows, text="No tool offers automation yet.", style="Muted.TLabel").pack(anchor="w")
        for t in autos:
            r = ttk.Frame(self.rows, padding=6)
            r.pack(fill="x")
            running = t.auto_running()
            ttk.Label(r, text=f"{t.icon} {t.title}", width=22).pack(side="left")
            ttk.Label(r, text="● running" if running else "○ stopped",
                      foreground=self.app.theme.p["ok" if running else "muted"], width=12).pack(side="left")
            ttk.Label(r, text=t.auto_summary(), style="Muted.TLabel").pack(side="left", padx=8)
            ttk.Button(r, text="Stop" if running else "Start", command=lambda t=t, run=running: self._toggle(t, run)).pack(side="right")
        self.frame.after(3000, lambda: self.frame.winfo_exists() and self.app.current is self and self.refresh())

    def _toggle(self, tool, running: bool) -> None:
        try:
            (tool.stop_auto if running else tool.start_auto)()
        except Exception as exc:
            self.app.log(f"{tool.title}: {exc}", "error")
        self.refresh()

    def on_show(self) -> None:
        self.refresh()
