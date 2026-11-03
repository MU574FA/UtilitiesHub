"""Small Tk helpers shared by all windows."""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


def setup_theme(root: tk.Tk) -> None:
    style = ttk.Style(root)
    for theme in ("vista", "aqua", "clam"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    style.configure("Accent.TButton", font=("TkDefaultFont", 10, "bold"))
    style.configure("Hint.TLabel", foreground="#666666")


def center(window: tk.Misc, width: int, height: int) -> None:
    window.update_idletasks()
    x = (window.winfo_screenwidth() - width) // 2
    y = (window.winfo_screenheight() - height) // 3
    window.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")


def copy_to_clipboard(widget: tk.Misc, text: str) -> None:
    """Put text on the system clipboard (via the root window so it outlives pop-ups)."""
    root = widget.nametowidget(".")
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()  # flush so other applications can read it right away


def open_path(path: str | Path) -> None:
    """Open a file or folder with the OS default application."""
    path = str(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as exc:
        messagebox.showerror("Cannot open", f"{path}\n\n{exc}")
