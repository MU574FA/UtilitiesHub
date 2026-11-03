"""Drag & drop of files onto widgets (uses tkinterdnd2 when installed; silently does nothing otherwise)."""
from __future__ import annotations

import re
import tkinter as tk
from typing import Callable

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:                                   # not installed / no tkdnd for this platform
    DND_FILES, TkinterDnD, HAS_DND = None, None, False


def create_root() -> tk.Tk:
    if HAS_DND:
        try:
            return TkinterDnD.Tk()
        except Exception:
            pass
    return tk.Tk()


def parse_drop(root: tk.Misc, data: str) -> list[str]:
    try:
        return list(root.tk.splitlist(data))
    except tk.TclError:
        return re.findall(r"\{([^}]*)\}|(\S+)", data)


def make_drop_target(widget: tk.Misc, callback: Callable[[list[str]], None]) -> bool:
    """Call callback(paths) when files are dropped on `widget`. Returns False if DnD is unavailable."""
    if not HAS_DND or not hasattr(widget, "drop_target_register"):
        return False
    try:
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", lambda e: callback([p for p in parse_drop(widget, e.data) if p]))
        return True
    except Exception:
        return False


class DropZone(tk.Frame):
    """A big 'drop files here / click to choose' area."""

    def __init__(self, parent: tk.Misc, text: str, callback: Callable[[list[str]], None],
                 filetypes: list | None = None, folders: bool = True, theme=None, height: int = 70) -> None:
        super().__init__(parent, height=height, highlightthickness=2, cursor="hand2")
        self.callback, self.filetypes, self.theme = callback, filetypes, theme
        self.label = tk.Label(self, text=text + ("\n(or click to choose)" if HAS_DND else "\nClick to choose"),
                              justify="center")
        self.label.pack(expand=True, fill="both", padx=12, pady=10)
        for w in (self, self.label):
            w.bind("<Button-1>", self._choose)
            make_drop_target(w, callback)
        self.pack_propagate(False)
        self.paint()
        if theme:
            theme.on_change(self.paint)

    def paint(self) -> None:
        p = self.theme.p if self.theme else {"surface": "#fff", "muted": "#666", "line": "#bbb"}
        self.configure(background=p["surface"], highlightbackground=p["line"], highlightcolor=p["line"])
        self.label.configure(background=p["surface"], foreground=p["muted"])

    def _choose(self, _e=None) -> None:
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(filetypes=self.filetypes or [("All files", "*.*")],
                                            parent=self.winfo_toplevel())
        if paths:
            self.callback(list(paths))
