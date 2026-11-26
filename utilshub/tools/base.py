"""Base class every tool derives from.

A tool is a self-contained screen.  It gets the `app` (settings, theme, jobs, renderer, journal, toast ...),
declares its options/pages, builds its UI into a frame and may accept files dropped on it or sent from
another tool.  See tools/file_picker.py for a compact example.
"""
from __future__ import annotations

import logging
import re
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from typing import Callable

from ..core.schema import Opt, Page


def _existing_dir(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if "{" in text:
        text = re.sub(r"[\\/]\{[^}]*\}.*$", "", text).strip()
        if "{" in text:
            text = re.sub(r"\{[^}]*\}.*$", "", text).strip()
    if not text:
        return None
    p = Path(text)
    if p.is_dir():
        return str(p)
    if p.is_file() and p.parent.is_dir():
        return str(p.parent)
    return None


@dataclass
class Command:
    id: str
    title: str
    callback: Callable[[], None]
    group: str = ""
    keywords: str = ""
    shortcut: str = ""


class Tool:
    id = "tool"
    title = "Tool"
    icon = "🧰"
    description = ""
    order = 50
    stats_when_closed = False     # True: Home shows stats() even before the tool was opened (tool must load by itself)
    input_path_key: str = ""
    output_path_key: str = ""
    settings_group: str = "Documents & files"

    def __init__(self, app) -> None:
        self.app = app
        self.settings = app.settings
        self.frame: ttk.Frame | None = None
        self.built = False
        self.host: Tool | None = None         # set when this tool lives as a tab inside a HubTool
        self.log_ = logging.getLogger(f"tool.{self.id}")

    # ------------------------------------------------------------------ declarations
    @classmethod
    def options(cls) -> list[Opt]:
        return []

    @classmethod
    def pages(cls) -> list[Page]:
        """Settings pages of this tool (default: one page built from options())."""
        return [Page(cls.id, cls.title, cls.icon, order=cls.order + 100, description=cls.description,
                     group=cls.settings_group)]

    # ------------------------------------------------------------------ lifecycle
    def ensure_built(self, parent: tk.Misc) -> ttk.Frame:
        if not self.built:
            self.frame = ttk.Frame(parent, padding=(8, 6))
            self.build(self.frame)
            self.built = True
        return self.frame

    def build(self, parent: ttk.Frame) -> None:
        raise NotImplementedError

    def on_show(self) -> None: ...

    def on_hide(self) -> None: ...

    def on_close(self) -> None: ...

    def accept_files(self, paths: list[str]) -> None:
        """Called for files dropped on the window / sent from another tool."""

    def commands(self) -> list[Command]:
        return []

    def keys(self) -> dict[str, Callable[[], None]]:
        """Keyboard shortcuts active while this tool is shown, e.g. {"<Control-Return>": self.compare}."""
        return {}

    # ------------------------------------------------------------------ output file
    @classmethod
    def append_opts(cls, auto: bool = False) -> list[Opt]:
        """Option for 'create / add to a CSV file of my choice'.  `auto`: tool adds to it by itself when it is set."""
        return [Opt(f"{cls.id}.append_file", "", "Add results to this CSV file", "append", section="Output file",
                    filetypes=[("CSV (opens in Excel)", "*.csv")],
                    help="Pick a new name to create the file, or an existing .csv to add rows to it."
                         + (" Used automatically; leave empty to switch off." if auto else
                            " Used by the 'Add to file' button; empty = ask each time."))]

    def append_rows(self, records: list[dict], ask: bool = False, quiet: bool = False, timestamp: bool = True) -> bool:
        """Add `records` to the user's CSV (`<id>.append_file`).  `ask`: choose the file when none is set."""
        from tkinter import filedialog
        from ..core.appendfile import AppendError, append_records
        from ..ui.widgets import error
        path = (self.get("append_file") or "").strip()
        if not path:
            if not ask:
                return False
            path = filedialog.asksaveasfilename(title="Add to CSV file (new or existing)", defaultextension=".csv",
                                                initialdir=self.output_dir(),
                                                filetypes=[("CSV (opens in Excel)", "*.csv")], confirmoverwrite=False)
            if not path:
                return False
            self.put("append_file", path)
        from ..core.appendfile import flush_pending, pending_path
        try:
            moved = flush_pending(path)                     # rows parked earlier while the file was locked go first
            if moved:
                self.log(f"Added {moved} waiting row(s) to {path}", "ok")
            n = append_records(path, records, timestamp=timestamp)
        except AppendError as exc:
            if quiet:                                       # unattended: never lose the row and never open a window per file
                try:
                    append_records(pending_path(path), records, timestamp=timestamp)
                    self.log(f"{exc} The row was kept in {pending_path(path).name} and will be added later.", "warn")
                    self._retry_pending(path)
                    return True
                except AppendError:
                    pass
            self.log(str(exc), "error")
            error(self.app.root, str(exc))
            return False
        if not quiet:
            self.app.notify(f"Added {n} row(s) to {Path(path).name}", "ok")
        self.log(f"Added {n} row(s) to {path}", "ok")
        return True

    def _retry_pending(self, path: str, delay_ms: int = 30_000) -> None:
        """Try again in a while to move the parked rows into the real file (e.g. once Excel is closed)."""
        if getattr(self, "_retry_set", None) is None:
            self._retry_set = set()
        if path in self._retry_set:
            return
        self._retry_set.add(path)

        def attempt() -> None:
            from ..core.appendfile import AppendError as _Err, flush_pending
            self._retry_set.discard(path)
            try:
                n = flush_pending(path)
                if n:
                    self.log(f"Added {n} waiting row(s) to {path}", "ok")
            except _Err:
                self._retry_pending(path, min(delay_ms * 2, 600_000))
        self.app.root.after(delay_ms, attempt)

    # ------------------------------------------------------------------ automation
    def can_automate(self) -> bool:
        return False

    def auto_running(self) -> bool:
        return False

    def auto_incomplete(self) -> bool:
        """True while it is running only in part (e.g. one of several triggers stopped or paused): 'Start all' finishes the job."""
        return False

    def start_auto(self) -> None: ...

    def stop_auto(self) -> None: ...

    def auto_summary(self) -> str:
        return ""

    # ------------------------------------------------------------------ hub support
    def members(self) -> list["Tool"]:
        """The real tools behind this entry (a HubTool returns its tabs, any other tool returns itself)."""
        return [self]

    def is_current(self) -> bool:
        """True while this tool is on screen - also when it is the selected tab of a hub."""
        cur = self.app.current
        return cur is self or (self.host is not None and cur is self.host and getattr(self.host, "active", None) is self)

    def focus(self) -> None:
        """Bring this tool to the front (for a tab: open the hub and select the tab)."""
        self.app.show_tool(self.id)

    # ------------------------------------------------------------------ helpers
    def log(self, message: str, level: str = "info") -> None:
        self.app.log(message, level, tool=self.id)

    def get(self, key: str):
        return self.settings.get(f"{self.id}.{key}")

    def put(self, key: str, value) -> None:
        self.settings.set(f"{self.id}.{key}", value)

    def input_dir(self) -> str | None:
        return _existing_dir(self.get(self.input_path_key)) if self.input_path_key else None

    def output_dir(self) -> str | None:
        return _existing_dir(self.get(self.output_path_key)) if self.output_path_key else None

    def sync_var(self, key: str, var: tk.Variable) -> None:
        """Keep a tool widget variable in sync when `<id>.<key>` changes in Settings."""
        if not hasattr(self, "_synced_vars"):
            self._synced_vars: dict[str, tk.Variable] = {}
            self.settings.subscribe(f"{self.id}.", self._on_synced_setting)
        self._synced_vars[key] = var

    def _on_synced_setting(self, full_key: str, value) -> None:
        if not self.built or self.frame is None or not self.frame.winfo_exists():
            return
        prefix = f"{self.id}."
        if not full_key.startswith(prefix):
            return
        var = self._synced_vars.get(full_key[len(prefix):])
        if var is None:
            return
        val = "" if value is None else value
        try:
            cur = var.get()
            if isinstance(val, str) and str(cur).strip() == val.strip():
                return
            if cur != val:
                var.set(val)
        except tk.TclError:
            pass

    def stats(self) -> dict[str, int | str]:
        """Shown on the Home dashboard."""
        return {}

    def badge(self) -> str:
        """Short text (a count) shown behind the name on the sidebar button; empty = nothing."""
        return ""
