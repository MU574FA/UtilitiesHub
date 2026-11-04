"""Tool manager: drag tools into the order you like, hide the ones you never use, bring them back.

Used on the Settings page "Home & tools".  All logic lives in core.toolorder and App.set_tool_order /
App.set_tool_hidden; this widget only shows the list and calls them, and it re-draws itself when the layout is
changed from somewhere else (Home card menu, command palette ...).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..core.toolorder import PINNED


class ToolManager(ttk.Frame):
    def __init__(self, parent: tk.Misc, app) -> None:
        super().__init__(parent)
        self.app = app
        self._drag: str | None = None
        ttk.Label(self, text="Tools in the sidebar and on Home", style="H3.TLabel").pack(anchor="w")
        ttk.Label(self, wraplength=560, justify="left", style="Muted.TLabel",
                  text="Drag a row (or use ▲ ▼) to change the order. Double-click a row, press Space or use the button "
                       "to hide or show a tool. A hidden tool keeps its settings and stays reachable from the Tools "
                       "menu and the command palette (Ctrl+K).").pack(anchor="w", pady=(0, 6))

        body = ttk.Frame(self)
        body.pack(fill="x")
        n = len(app.tools)
        self.tree = ttk.Treeview(body, columns=("shown", "tool", "about"), show="headings", selectmode="browse",
                                 height=max(6, min(16, n)))
        for key, head, width, anchor in (("shown", "Shown", 60, "center"), ("tool", "Tool", 190, "w"),
                                         ("about", "What it does", 380, "w")):
            self.tree.heading(key, text=head)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "about"))
        ys = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="x", expand=True)
        self.tree.tag_configure("hidden", foreground=app.theme.p.get("muted", "#888888"))

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="▲ Up", command=lambda: self._move(-1)).pack(side="left")
        ttk.Button(bar, text="▼ Down", command=lambda: self._move(1)).pack(side="left", padx=4)
        self.toggle_btn = ttk.Button(bar, text="Hide / show", command=self._toggle_selected)
        self.toggle_btn.pack(side="left", padx=(10, 4))
        ttk.Button(bar, text="Show all", command=self.app.show_all_tools).pack(side="left", padx=4)
        ttk.Button(bar, text="Reset order", command=self._reset_order).pack(side="left", padx=4)
        self.note = ttk.Label(self, style="Muted.TLabel")
        self.note.pack(anchor="w")

        self.tree.bind("<ButtonPress-1>", self._press, add="+")
        self.tree.bind("<B1-Motion>", self._motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._release, add="+")
        self.tree.bind("<Double-1>", lambda e: self._toggle_selected())
        self.tree.bind("<space>", lambda e: (self._toggle_selected(), "break")[1])
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_button())

        unsubscribe = app.settings.subscribe("window.", self._external)
        self.bind("<Destroy>", lambda e: unsubscribe() if e.widget is self else None)
        self.refresh()

    # ------------------------------------------------------------------ content
    def _selected(self) -> str:
        sel = self.tree.selection()
        return sel[0] if sel else ""

    def refresh(self, select: str = "") -> None:
        select = select or self._selected()
        hidden = set(self.app.settings.get("window.hidden_tools") or [])
        self.tree.delete(*self.tree.get_children())
        for t in self.app.sorted_tools():
            mark = "🔒" if t.id == PINNED else ("—" if t.id in hidden else "✔")
            self.tree.insert("", "end", iid=t.id, values=(mark, f"{t.icon}  {t.title}", t.description),
                             tags=("hidden",) if t.id in hidden else ())
        if select and self.tree.exists(select):
            self.tree.selection_set(select)
            self.tree.see(select)
        n = len(hidden & set(self.app.tools))
        self.note.configure(text=f"{n} hidden" if n else "All tools are shown.")
        self._update_button()

    def _update_button(self) -> None:
        sel = self._selected()
        hidden = set(self.app.settings.get("window.hidden_tools") or [])
        if not sel or sel == PINNED:
            self.toggle_btn.configure(text="Hide / show", state="disabled")
        else:
            self.toggle_btn.configure(text="Show" if sel in hidden else "Hide", state="normal")

    def _external(self, key: str, _value) -> None:
        if key in ("window.tool_order", "window.hidden_tools") and self.winfo_exists():
            self.after_idle(self._safe_refresh)

    def _safe_refresh(self) -> None:
        if self.winfo_exists():
            self.refresh()

    # ------------------------------------------------------------------ actions
    def _move(self, delta: int) -> None:
        sel = self._selected()
        if sel and sel != PINNED:
            self.app.move_tool(sel, delta)
            self.refresh(sel)

    def _toggle_selected(self) -> None:
        sel = self._selected()
        if not sel or sel == PINNED:
            return
        hidden = set(self.app.settings.get("window.hidden_tools") or [])
        self.app.set_tool_hidden(sel, sel not in hidden)
        self.refresh(sel)

    def _reset_order(self) -> None:
        self.app.set_tool_order([])
        self.refresh()

    # ------------------------------------------------------------------ drag to reorder
    def _press(self, e) -> None:
        iid = self.tree.identify_row(e.y)
        self._drag = iid if iid and iid != PINNED else None

    def _motion(self, e) -> None:
        if not self._drag:
            return
        target = self.tree.identify_row(e.y)
        if not target or target == self._drag:
            return
        self.tree.move(self._drag, "", max(1, self.tree.index(target)))       # never above the pinned Home row

    def _release(self, _e) -> None:
        if not self._drag:
            return
        moved, self._drag = self._drag, None
        ids = list(self.tree.get_children())
        if ids != self.app.tool_order():
            self.app.set_tool_order(ids)
        self.refresh(moved)
