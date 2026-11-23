"""HubTool: one sidebar entry that holds several related tools as tabs.

A hub does not replace the tools inside it.  Every member keeps its own id, settings (`<member id>.<key>`),
journal entries, commands and tests; the hub only gives them one place to live and adds what they share:

  * a **shared files bar** - drop files once (or send them from Home), then push the same files to any tab
  * the **last tab is remembered** and tabs are built only when first opened
  * `Alt+Left / Alt+Right` switch tabs (Ctrl+Tab works too, Tk provides it)
  * every member's palette commands, shortcuts (only of the tab on screen), statistics, badge and automation
    are forwarded, and `app.show_tool("<member id>")` / `app.send_to("<member id>", files)` still work -
    they open the hub on that tab

Declare a hub by subclassing and listing the members:

    class ExtractHub(HubTool):
        id, title, icon = "extract", "Extract", "🔎"
        MEMBERS = (("screenshot", "Screenshot", "Screenshots"), ("passport", "PassportTool", "Passports"))

A member is `(module in utilshub.tools, class name, tab label)`.  A member that cannot be imported (missing optional
library ...) is left out and logged instead of breaking the whole hub.
"""
from __future__ import annotations

from dataclasses import replace
import importlib
import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable

from ..core.schema import Opt, Page
from .base import Command, Tool

_log = logging.getLogger("hub")
_CACHE: dict[type, list[tuple[type[Tool], str]]] = {}


class HubTool(Tool):
    MEMBERS: tuple[tuple[str, str, str], ...] = ()

    # ------------------------------------------------------------------ declarations
    @classmethod
    def member_classes(cls) -> list[tuple[type[Tool], str]]:
        """[(tool class, tab label)] for every member that could be imported."""
        if cls in _CACHE:
            return _CACHE[cls]
        out: list[tuple[type[Tool], str]] = []
        for module, name, label in cls.MEMBERS:
            try:
                mod = importlib.import_module(f"{__package__}.{module}")
                out.append((getattr(mod, name), label))
            except Exception:                                 # a missing library must not take the other tabs down
                _log.exception("%s: tab %s.%s is not available", cls.id, module, name)
        _CACHE[cls] = out
        return out

    @classmethod
    def options(cls) -> list[Opt]:
        first = cls.member_classes()[0][0].id if cls.member_classes() else cls.id
        opts: list[Opt] = [
            Opt(f"{cls.id}.last_tab", "", "Last open tab", "str"),
            Opt(f"{cls.id}.show_filebar", True, "Show the shared files bar above the tabs", "bool", section="Tabs",
                help="Add files once, then send them to any tab of this tool with one click.", page=first),
        ]
        for member, _ in cls.member_classes():
            opts += member.options()
        return opts

    @classmethod
    def pages(cls) -> list[Page]:
        pages: list[Page] = []
        base = 200 + cls.order * 10
        n = 0
        for member, _label in cls.member_classes():
            for p in member.pages():
                group = cls.title if (not p.group or p.group == Tool.settings_group) else p.group
                pages.append(replace(p, group=group, order=base + n))
                n += 1
        return pages

    # ------------------------------------------------------------------ lifecycle
    def __init__(self, app) -> None:
        super().__init__(app)
        self.subs: list[Tool] = []
        self.labels: dict[str, str] = {}
        for klass, label in self.member_classes():
            try:
                sub = klass(app)
            except Exception:
                _log.exception("Could not create tab %s", klass.id)
                continue
            sub.host = self
            self.subs.append(sub)
            self.labels[sub.id] = label
        self.active: Tool | None = None
        self.shared: list[str] = []
        self.nb = None
        self.holders: dict[str, ttk.Frame] = {}
        self.failed: set[str] = set()
        self._shown = False
        self._files_lbl = None
        self._send_buttons: list = []

    def members(self) -> list[Tool]:
        return list(self.subs)

    def member(self, sub_id: str) -> Tool | None:
        return next((s for s in self.subs if s.id == sub_id), None)

    def build(self, parent: ttk.Frame) -> None:
        for s in self.subs:                                    # the shell may rebuild the window: start every tab fresh
            s.frame, s.built = None, False
        self.holders.clear()
        self.failed.clear()
        self._send_buttons = []
        if self.get("show_filebar") is not False:
            self._build_filebar(parent)
        self.nb = ttk.Notebook(parent)
        self.nb.pack(fill="both", expand=True)
        for s in self.subs:
            holder = ttk.Frame(self.nb)
            self.holders[s.id] = holder
            self.nb.add(holder, text=f"{s.icon} {self.labels[s.id]}")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        if self.subs:
            wanted = self.get("last_tab")
            start = self.member(wanted) if wanted else None
            self.select((start or self.subs[0]).id)

    def _build_filebar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 6))
        self._files_lbl = ttk.Label(bar, text="", style="Muted.TLabel")
        self._files_lbl.pack(side="left")
        ttk.Button(bar, text="Clear", style="Tiny.TButton", command=self.clear_files).pack(side="right")
        for s in reversed(self.subs):
            b = ttk.Button(bar, text=self.labels[s.id], style="Tiny.TButton",
                           command=lambda i=s.id: self.deliver(i, self.shared))
            b.pack(side="right", padx=2)
            self._send_buttons.append(b)
        ttk.Label(bar, text="Send these files to:", style="Muted.TLabel").pack(side="right", padx=(0, 6))
        self._refresh_filebar()

    def _refresh_filebar(self) -> None:
        if self._files_lbl is None:
            return
        n = len(self.shared)
        if n:
            first = self.shared[0].replace("\\", "/").rsplit("/", 1)[-1]
            text = f"{n} file(s) ready: {first}" + (f" and {n - 1} more" if n > 1 else "")
        else:
            text = "Add files in a tab - they stay available for every tab."
        self._files_lbl.configure(text=text)
        for b in self._send_buttons:
            b.configure(state="normal" if n else "disabled")

    # ------------------------------------------------------------------ tabs
    def select(self, sub_id: str) -> None:
        """Open the tab of member `sub_id`."""
        sub = self.member(sub_id)
        if sub is None or self.nb is None:
            return
        try:
            self.nb.select(self.subs.index(sub))
        except tk.TclError:
            pass
        self._activate(sub)

    def next_tab(self, step: int = 1) -> None:
        if self.active is not None and self.subs:
            i = (self.subs.index(self.active) + step) % len(self.subs)
            self.select(self.subs[i].id)

    def _tab_changed(self) -> None:
        try:
            idx = self.nb.index(self.nb.select())
        except (tk.TclError, ValueError):
            return
        if 0 <= idx < len(self.subs):
            self._activate(self.subs[idx])

    def _activate(self, sub: Tool) -> None:
        if sub is self.active and sub.built:
            return
        prev = self.active
        if prev is not None and prev is not sub and prev.built and self._shown:
            self._safe(prev.on_hide, "on_hide")
        self.active = sub
        if not sub.built and sub.id not in self.failed:
            holder = self.holders[sub.id]
            try:
                sub.ensure_built(holder).pack(fill="both", expand=True)
            except Exception as exc:
                _log.exception("Building tab %s failed", sub.id)
                self.failed.add(sub.id)
                sub.built = False
                self.log(f"{self.labels[sub.id]} could not be opened: {exc}", "error")
                ttk.Label(holder, text=f"This tab could not be opened:\n{exc}", style="Muted.TLabel").pack(padx=20, pady=20)
        self.put("last_tab", sub.id)
        if self._shown and sub.built:
            self._safe(sub.on_show, "on_show")

    def _safe(self, fn: Callable[[], None], what: str) -> None:
        try:
            fn()
        except Exception:
            _log.exception("%s: %s", self.id, what)

    def on_show(self) -> None:
        self._shown = True
        if self.active is not None and self.active.built:
            self._safe(self.active.on_show, "on_show")
        self._refresh_filebar()

    def on_hide(self) -> None:
        self._shown = False
        if self.active is not None and self.active.built:
            self._safe(self.active.on_hide, "on_hide")

    def on_close(self) -> None:
        for s in self.subs:
            self._safe(s.on_close, "on_close")

    # ------------------------------------------------------------------ files
    def accept_files(self, paths: list[str]) -> None:
        """Files dropped on the window: remember them for every tab and hand them to the open one."""
        self.shared = list(paths)
        self._refresh_filebar()
        if self.active is not None and self.active.built:
            self.active.accept_files(list(paths))

    def deliver(self, sub_id: str, paths: list[str]) -> None:
        """Open a tab and give it files (used by the files bar and by `app.send_to`)."""
        if not paths:
            return
        self.shared = list(paths)
        self._refresh_filebar()
        self.select(sub_id)
        sub = self.member(sub_id)
        if sub is not None and sub.built:
            sub.accept_files(list(paths))

    def clear_files(self) -> None:
        self.shared = []
        self._refresh_filebar()

    # ------------------------------------------------------------------ forwarded to the members
    def commands(self) -> list[Command]:
        cmds: list[Command] = []
        for s in self.subs:
            cmds.append(Command(f"{self.id}.tab.{s.id}", f"{self.title}: open {self.labels[s.id]}",
                                lambda i=s.id: self.app.show_tool(i), self.title, f"{s.title} {s.description}"))
            for c in s.commands():
                cmds.append(Command(c.id, c.title, c.callback, c.group or self.labels[s.id], c.keywords, c.shortcut))
        return cmds

    def keys(self) -> dict[str, Callable[[], None]]:
        """Before a tab is open: every shortcut (so the shell can bind them).  Afterwards: only the open tab's."""
        out: dict[str, Callable[[], None]] = {}
        pool = [self.active] if self.active is not None else self.subs
        for s in pool:
            out.update(s.keys())
        out["<Alt-Right>"] = lambda: self.next_tab(1)
        out["<Alt-Left>"] = lambda: self.next_tab(-1)
        return out

    def stats(self) -> dict[str, int | str]:
        out: dict[str, int | str] = {}
        for s in self.subs:
            if s.built or s.stats_when_closed:
                for k, v in s.stats().items():
                    out[f"{self.labels[s.id]}: {k}" if len(self.subs) > 1 else k] = v
        return out

    def badge(self) -> str:
        parts = [b for b in (s.badge() for s in self.subs) if b]
        return ", ".join(parts)
