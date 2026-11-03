"""Modern Theme, Color Palettes & TTK Style Engine for OmniDocs Studio."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

# Color Palettes
PALETTES = {
    "dark": {
        "bg": "#131815",
        "surface": "#1b221e",
        "surface_2": "#232c27",
        "line": "#2f3a34",
        "fg": "#e6ebe7",
        "muted": "#9aa89f",
        "text_field": "#171e19",
        "select_bg": "#1f6f5c",
        "select_fg": "#ffffff",
        "ok": "#6fd6a0",
        "ok_bg": "#1b3a29",
        "warn": "#f0c168",
        "warn_bg": "#3d3114",
        "danger": "#f19a9a",
        "danger_bg": "#42201f",
    },
    "light": {
        "bg": "#f5f6f4",
        "surface": "#ffffff",
        "surface_2": "#eceeea",
        "line": "#d9ddd6",
        "fg": "#1d2420",
        "muted": "#5d675f",
        "text_field": "#ffffff",
        "select_bg": "#1f6f5c",
        "select_fg": "#ffffff",
        "ok": "#1f7a4d",
        "ok_bg": "#dcf0e3",
        "warn": "#8a5a00",
        "warn_bg": "#fbebc8",
        "danger": "#a33a3a",
        "danger_bg": "#f8dcdc",
    }
}

ACCENTS = {
    "emerald": {"accent": "#1f6f5c", "accent_fg": "#ffffff", "accent_soft": "#2d4a3e"},
    "blue": {"accent": "#1a5fb4", "accent_fg": "#ffffff", "accent_soft": "#1c3d5a"},
    "purple": {"accent": "#7137c8", "accent_fg": "#ffffff", "accent_soft": "#3b235f"},
    "amber": {"accent": "#b56905", "accent_fg": "#ffffff", "accent_soft": "#4d3412"},
    "crimson": {"accent": "#c01c28", "accent_fg": "#ffffff", "accent_soft": "#4d161a"},
}

FONT_SIZES = {
    "small": {"body": 9, "header": 12, "title": 14, "kpi": 18},
    "medium": {"body": 10, "header": 13, "title": 16, "kpi": 22},
    "large": {"body": 11, "header": 14, "title": 18, "kpi": 24},
    "xl": {"body": 12, "header": 16, "title": 20, "kpi": 28},
}


class ThemeManager:
    """Applies and updates modern ttk styles and widget colors across the app."""
    def __init__(self, theme: str = "dark", accent: str = "emerald", font_scale: str = "medium"):
        self.theme = theme if theme in PALETTES else "dark"
        self.accent_name = accent if accent in ACCENTS else "emerald"
        self.font_scale = font_scale if font_scale in FONT_SIZES else "medium"

    @property
    def colors(self) -> dict[str, str]:
        c = dict(PALETTES[self.theme])
        c.update(ACCENTS[self.accent_name])
        return c

    @property
    def fonts(self) -> dict[str, int]:
        return FONT_SIZES[self.font_scale]

    def apply(self, root: tk.Tk) -> None:
        """Configures ttk style database and root background."""
        c = self.colors
        f = self.fonts
        body_font = ("Segoe UI", f["body"])
        bold_font = ("Segoe UI", f["body"], "bold")
        header_font = ("Segoe UI", f["header"], "bold")

        root.configure(bg=c["bg"])
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # General defaults
        style.configure(".", background=c["bg"], foreground=c["fg"], font=body_font, borderwidth=0)
        
        # Frames
        style.configure("TFrame", background=c["bg"])
        style.configure("Card.TFrame", background=c["surface"], relief="flat")
        style.configure("Surface2.TFrame", background=c["surface_2"])
        
        # LabelFrames
        style.configure("TLabelframe", background=c["surface"], foreground=c["fg"], bordercolor=c["line"])
        style.configure("TLabelframe.Label", background=c["surface"], foreground=c["muted"], font=bold_font)
        
        # Labels
        style.configure("TLabel", background=c["bg"], foreground=c["fg"], font=body_font)
        style.configure("Card.TLabel", background=c["surface"], foreground=c["fg"])
        style.configure("Muted.TLabel", foreground=c["muted"])
        style.configure("Header.TLabel", font=header_font, foreground=c["fg"])
        style.configure("Accent.TLabel", foreground=c["accent"], font=bold_font)

        # Buttons
        style.configure("TButton", background=c["surface_2"], foreground=c["fg"], bordercolor=c["line"],
                        relief="flat", padding=(10, 6), font=bold_font)
        style.map("TButton",
                  background=[("active", c["line"]), ("pressed", c["surface"])],
                  foreground=[("disabled", c["muted"])])

        # Primary Button (Accent filled)
        style.configure("Primary.TButton", background=c["accent"], foreground=c["accent_fg"],
                        bordercolor=c["accent"], relief="flat", padding=(12, 7), font=bold_font)
        style.map("Primary.TButton",
                  background=[("active", c["accent_soft"]), ("pressed", c["accent"])],
                  foreground=[("disabled", c["muted"])])

        # Danger Button
        style.configure("Danger.TButton", background=c["danger_bg"], foreground=c["danger"],
                        bordercolor=c["danger"], relief="flat", padding=(10, 6), font=bold_font)

        # Tabs (Notebook)
        style.configure("TNotebook", background=c["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=c["surface"], foreground=c["muted"],
                        padding=(14, 8), font=bold_font, borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", c["accent"]), ("active", c["surface_2"])],
                  foreground=[("selected", c["accent_fg"]), ("active", c["fg"])])

        # Treeview (Data tables)
        style.configure("Treeview", background=c["surface"], foreground=c["fg"], fieldbackground=c["surface"],
                        bordercolor=c["line"], rowheight=28, font=body_font)
        style.configure("Treeview.Heading", background=c["surface_2"], foreground=c["fg"],
                        font=bold_font, relief="flat", bordercolor=c["line"])
        style.map("Treeview",
                  background=[("selected", c["select_bg"])],
                  foreground=[("selected", c["select_fg"])])

        # Entry & Combobox
        style.configure("TEntry", fieldbackground=c["text_field"], foreground=c["fg"], bordercolor=c["line"], padding=4)
        style.configure("TCombobox", fieldbackground=c["text_field"], foreground=c["fg"], bordercolor=c["line"])
        style.map("TCombobox", fieldbackground=[("readonly", c["text_field"])], selectbackground=[("readonly", c["accent"])])

        # Checkbutton & Radiobutton
        style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"], font=body_font)
        style.map("TCheckbutton", background=[("active", c["bg"])])
        style.configure("TRadiobutton", background=c["bg"], foreground=c["fg"], font=body_font)
        style.map("TRadiobutton", background=[("active", c["bg"])])

        # Progressbar
        style.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor=c["surface_2"], borderwidth=0)

