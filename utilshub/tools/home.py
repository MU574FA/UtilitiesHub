"""Home: dashboard with tool cards, drop zone and recent activity.

Customisable (Settings > Home & tools, or right-click a card / sidebar button): the order of the tools, which
ones are hidden, how many cards per row and which parts of Home are shown.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..core.schema import Opt, Page
from ..ui.dnd import DropZone
from ..ui.widgets import LogView
from .base import Tool

_NOT_CARDS = ("home", "autopilot", "history")


def _tool_manager_page(parent, app) -> None:
    from ..ui.toolmanager import ToolManager
    ToolManager(parent, app).pack(fill="x", pady=(12, 0))


class HomeTool(Tool):
    id = "home"
    title = "Home"
    icon = "🏠"
    description = "Overview, quick drop and recent activity."
    order = 0

    @classmethod
    def options(cls) -> list[Opt]:
        return [
            Opt("home.columns", 3, "Tool cards per row", "int", min=1, max=5, section="Home screen"),
            Opt("home.show_descriptions", True, "Show what each tool does on its card", "bool", section="Home screen"),
            Opt("home.show_stats", True, "Show small statistics on the cards", "bool", section="Home screen"),
            Opt("home.show_hidden_strip", True, "Show a row with the hidden tools (one click to bring one back)", "bool",
                section="Home screen"),
            Opt("home.show_dropzone", True, "Show the drop zone", "bool", section="Home screen"),
            Opt("home.show_activity", True, "Show recent activity", "bool", section="Home screen"),
        ]

    @classmethod
    def pages(cls) -> list[Page]:
        return [Page("home", "Home & tools", "🏠", 7, "Rearrange, hide and show tools; choose what Home displays.",
                     builder=_tool_manager_page)]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.parent: ttk.Frame | None = None
        self.combo = None
        self.logview = None
        self._hooked = False

    # ------------------------------------------------------------------ build
    def build(self, parent: ttk.Frame) -> None:
        self.parent = parent
        if not self._hooked:                               # listeners are added once, however often we re-draw
            self._hooked = True
            self.app.on_log(self._on_log)
            self.settings.subscribe("home.", lambda k, v: self._later(self._layout))
            self.settings.subscribe("window.", self._window_changed)
        self._layout()

    def _layout(self) -> None:
        """(Re)draw the whole Home screen."""
        p = self.parent
        if p is None or not p.winfo_exists():
            return
        for w in p.winfo_children():
            w.destroy()
        head = ttk.Frame(p)
        head.pack(fill="x")
        left = ttk.Frame(head)
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="Utils Hub", style="H2.TLabel").pack(anchor="w")
        ttk.Label(left, text="Everything in one place. Pick a tool, or drop files below.", style="Muted.TLabel").pack(anchor="w")
        ttk.Button(head, text="⚙  Customize tools", command=lambda: self.app.open_settings("home")).pack(side="right")

        self.cards = ttk.Frame(p)
        self.cards.pack(fill="x", pady=10)
        self.hidden_bar = ttk.Frame(p)
        self.hidden_bar.pack(fill="x")

        self.target = tk.StringVar()
        self.combo = None
        if self.settings.get_bool("home.show_dropzone", True):
            row = ttk.Frame(p)
            row.pack(fill="x", pady=(8, 4))
            ttk.Label(row, text="Send dropped files to:").pack(side="left")
            self.combo = ttk.Combobox(row, textvariable=self.target, state="readonly", width=28)
            self.combo.pack(side="left", padx=6)
            DropZone(p, "Drop files here", self._dropped, theme=self.app.theme).pack(fill="x", pady=4)

        self.logview = None
        if self.settings.get_bool("home.show_activity", True):
            ttk.Label(p, text="Recent activity", style="H3.TLabel").pack(anchor="w", pady=(10, 2))
            self.logview = LogView(p, height=10)
            self.logview.pack(fill="both", expand=True)
            for _stamp, level, tool, msg in list(self.app.logs):
                self.logview.log(f"{tool + ': ' if tool else ''}{msg}", level)
        self.refresh()

    # ------------------------------------------------------------------ live updates
    def _later(self, fn) -> None:
        if self.built and self.parent is not None and self.parent.winfo_exists():
            self.app.root.after_idle(fn)

    def _window_changed(self, key: str, _value) -> None:
        if key in ("window.tool_order", "window.hidden_tools"):
            self._later(self.refresh)

    def _on_log(self, _stamp, level, tool, msg) -> None:
        lv = self.logview
        if lv is not None and lv.winfo_exists():
            lv.log(f"{tool + ': ' if tool else ''}{msg}", level)

    # ------------------------------------------------------------------ cards
    def _targets(self):
        return [t for t in self.app.visible_tools() if t.id not in _NOT_CARDS]

    def refresh(self) -> None:
        if self.parent is None or not self.cards.winfo_exists():
            return
        for w in self.cards.winfo_children():
            w.destroy()
        tools = self._targets()
        cols = max(1, min(5, self.settings.get_int("home.columns", 3)))
        show_desc = self.settings.get_bool("home.show_descriptions", True)
        show_stats = self.settings.get_bool("home.show_stats", True)
        for c in range(cols):
            self.cards.columnconfigure(c, weight=1, uniform="cards")
        for i, t in enumerate(tools):
            card = ttk.Frame(self.cards, padding=10, relief="groove")
            card.grid(row=i // cols, column=i % cols, sticky="nsew", padx=4, pady=4)
            ttk.Label(card, text=f"{t.icon}  {t.title}", style="H3.TLabel").pack(anchor="w")
            if show_desc:
                ttk.Label(card, text=t.description, style="Muted.TLabel", wraplength=max(160, 760 // cols - 40),
                          justify="left").pack(anchor="w")
            st = t.stats() if (show_stats and t.built) else {}
            if st:
                ttk.Label(card, text="  ".join(f"{k}: {v}" for k, v in st.items())).pack(anchor="w", pady=(4, 0))
            ttk.Button(card, text="Open", command=lambda i=t.id: self.app.show_tool(i)).pack(anchor="w", pady=(6, 0))
            self._bind_menu(card, t.id)
        self._draw_hidden_bar()
        if self.combo is not None:
            names = [f"{t.icon} {t.title}" for t in tools]
            self.combo.configure(values=names)
            if names and self.target.get() not in names:
                self.target.set(names[0])

    def _bind_menu(self, widget: tk.Misc, tool_id: str) -> None:
        for seq in ("<Button-3>", "<Button-2>"):
            widget.bind(seq, lambda e, i=tool_id: self._card_menu(e, i))
        for child in widget.winfo_children():
            self._bind_menu(child, tool_id)

    def _card_menu(self, event, tool_id: str) -> None:
        m = tk.Menu(self.cards, tearoff=False)
        m.add_command(label="Open", command=lambda: self.app.show_tool(tool_id))
        m.add_separator()
        m.add_command(label="Move earlier", command=lambda: self.app.move_tool(tool_id, -1))
        m.add_command(label="Move later", command=lambda: self.app.move_tool(tool_id, 1))
        m.add_command(label="Hide this tool", command=lambda: self.app.set_tool_hidden(tool_id, True))
        m.add_separator()
        m.add_command(label="Customize tools…", command=lambda: self.app.open_settings("home"))
        m.tk_popup(event.x_root, event.y_root)

    def _draw_hidden_bar(self) -> None:
        for w in self.hidden_bar.winfo_children():
            w.destroy()
        hidden = self.app.hidden_tools() if self.settings.get_bool("home.show_hidden_strip", True) else []
        if not hidden:
            return
        ttk.Label(self.hidden_bar, text="Hidden:", style="Muted.TLabel").pack(side="left", padx=(4, 6))
        for t in hidden:
            ttk.Button(self.hidden_bar, text=f"＋ {t.title}", style="Tiny.TButton",
                       command=lambda i=t.id: self.app.set_tool_hidden(i, False)).pack(side="left", padx=2)

    def on_show(self) -> None:
        self.refresh()

    # ------------------------------------------------------------------ dropped files
    def _dropped(self, paths: list[str]) -> None:
        for t in self._targets():
            if f"{t.icon} {t.title}" == self.target.get():
                self.app.send_to(t.id, paths)
                return

    def accept_files(self, paths: list[str]) -> None:
        self._dropped(paths)
