"""Base class every tool derives from.

A tool is a self-contained screen.  It gets the `app` (settings, theme, jobs, renderer, journal, toast ...),
declares its options/pages, builds its UI into a frame and may accept files dropped on it or sent from
another tool.  See tools/file_picker.py for a compact example.
"""
from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from typing import Callable

from ..core.schema import Opt, Page


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
    accepts = ()                  # file extensions this tool can take via "Send to" (empty = any)

    def __init__(self, app) -> None:
        self.app = app
        self.settings = app.settings
        self.frame: ttk.Frame | None = None
        self.built = False
        self.log_ = logging.getLogger(f"tool.{self.id}")

    # ------------------------------------------------------------------ declarations
    @classmethod
    def options(cls) -> list[Opt]:
        return []

    @classmethod
    def pages(cls) -> list[Page]:
        """Settings pages of this tool (default: one page built from options())."""
        return [Page(cls.id, cls.title, cls.icon, order=cls.order + 100, description=cls.description)]

    # ------------------------------------------------------------------ lifecycle
    def ensure_built(self, parent: tk.Misc) -> ttk.Frame:
        if not self.built:
            self.frame = ttk.Frame(parent, padding=10)
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
                                                filetypes=[("CSV (opens in Excel)", "*.csv")], confirmoverwrite=False)
            if not path:
                return False
            self.put("append_file", path)
        try:
            n = append_records(path, records, timestamp=timestamp)
        except AppendError as exc:
            self.log(str(exc), "error")
            error(self.app.root, str(exc))
            return False
        if not quiet:
            self.app.notify(f"Added {n} row(s) to {Path(path).name}", "ok")
        self.log(f"Added {n} row(s) to {path}", "ok")
        return True

    # ------------------------------------------------------------------ automation
    def can_automate(self) -> bool:
        return False

    def auto_running(self) -> bool:
        return False

    def start_auto(self) -> None: ...

    def stop_auto(self) -> None: ...

    def auto_summary(self) -> str:
        return ""

    # ------------------------------------------------------------------ helpers
    def log(self, message: str, level: str = "info") -> None:
        self.app.log(message, level, tool=self.id)

    def get(self, key: str):
        return self.settings.get(f"{self.id}.{key}")

    def put(self, key: str, value) -> None:
        self.settings.set(f"{self.id}.{key}", value)

    def stats(self) -> dict[str, int | str]:
        """Shown on the Home dashboard."""
        return {}
