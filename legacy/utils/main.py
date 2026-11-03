#!/usr/bin/env python3
"""
OmniDocs Studio — Unified Document, HR & Data Automation Suite.

A single, cohesive desktop environment merging all 7 workspace utilities:
1. CV Sorter & Recruiter Studio (watch folder, OCR, metadata extraction, candidate registry)
2. DocPacker & Compliance Verifier (onboarding compliance, MRZ, OpenCV face detection, cover page)
3. Merge Desk & Page Studio (PDF, DOCX, and images merger with manual page rotation & reordering)
4. File Batch Picker (fast multi-mode file retrieval, extension whitelist, dry-run preview)
5. Sheet Name Matcher (bilingual Arabic/Latin fuzzy reconciliation, 3-sheet export)
6. Sheet Sync Studio (tabular synchronization, blank preservation, schema expansion, audit diff)
7. Screenshot Extractor (Win+Shift+S clipboard listener, Lanczos upscale, OCR & tab row export)
8. Rules & Regex Studio (rule catalog, toggleable necessity levels, live test sandbox)
9. Studio Preferences & Theme Settings (Dark/Light themes, custom accents, window & preview options)
"""
from __future__ import annotations

import logging
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("omnidocs")

# Root path configuration
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from tkinterdnd2 import TkinterDnD
    HAS_DND = True
    BaseTk = TkinterDnD.Tk
except Exception:
    HAS_DND = False
    BaseTk = tk.Tk

from omnidocs.core.config import AppConfig
from omnidocs.core.ocr import setup_tesseract
from omnidocs.tools.cvsorter_tool import CVSorterView
from omnidocs.tools.docpacker_tool import DocPackerView
from omnidocs.tools.matcher_tool import MatcherView
from omnidocs.tools.merger_tool import MergerView
from omnidocs.tools.picker_tool import FilePickerView
from omnidocs.tools.rules_tool import RulesStudioView
from omnidocs.tools.screenshot_tool import ScreenshotExtractView
from omnidocs.tools.settings_tool import SettingsView
from omnidocs.tools.sync_tool import SheetSyncView
from omnidocs.ui.theme import ThemeManager


class OmniDocsApp(BaseTk):
    """Single master desktop application for OmniDocs Studio."""
    def __init__(self):
        super().__init__()
        self.title("OmniDocs Studio — Document, HR & Data Automation Suite")
        
        # Load unified configuration
        self.config = AppConfig.load()
        
        # Initialize Tesseract
        setup_tesseract(self.config.general.tesseract_path)

        # Restore window geometry
        win = self.config.window
        w = max(900, win.width)
        h = max(650, win.height)
        if win.x is not None and win.y is not None:
            self.geometry(f"{w}x{h}+{win.x}+{win.y}")
        else:
            self.geometry(f"{w}x{h}")
        self.minsize(960, 680)

        if win.always_on_top:
            self.attributes("-topmost", True)
        if win.maximized:
            try:
                self.state("zoomed")
            except Exception:
                pass

        # Apply Modern Theme
        self.theme_mgr = ThemeManager(
            theme=self.config.theme.theme,
            accent=self.config.theme.accent,
            font_scale=self.config.theme.font_scale,
        )
        self.theme_mgr.apply(self)

        # Views storage
        self.views: dict[str, ttk.Frame] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self.current_view_key = "cvsorter"

        self._build_layout()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Show initial view
        self.show_view("cvsorter")

    def _build_layout(self) -> None:
        c = self.theme_mgr.colors

        # Main horizontal container
        self.main_box = ttk.Frame(self)
        self.main_box.pack(fill="both", expand=True)

        # ------------------------------------------------ Left Sidebar Navigation
        self.sidebar = tk.Frame(self.main_box, bg=c["surface"], width=230)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        # Brand header in sidebar
        brand_frame = tk.Frame(self.sidebar, bg=c["surface"], padx=14, pady=16)
        brand_frame.pack(fill="x")

        lbl_logo = tk.Label(brand_frame, text="⚡ OmniDocs", font=("Segoe UI", 16, "bold"),
                            bg=c["surface"], fg=c["accent"])
        lbl_logo.pack(anchor="w")

        lbl_sub = tk.Label(brand_frame, text="Document & Data Studio", font=("Segoe UI", 9),
                           bg=c["surface"], fg=c["muted"])
        lbl_sub.pack(anchor="w")

        tk.Frame(self.sidebar, bg=c["line"], height=1).pack(fill="x", padx=10, pady=(0, 8))

        # Navigation menu items
        nav_items = [
            ("cvsorter", "📄  CV Sorter & Organizer", "cvsorter"),
            ("docpacker", "📦  DocPacker Compliance", "docpacker"),
            ("merger", "📑  Merge Desk & Studio", "merger"),
            ("picker", "🔍  File Batch Picker", "picker"),
            ("matcher", "📊  Sheet Name Matcher", "matcher"),
            ("sync", "🔄  Sheet Sync Studio", "sync"),
            ("screenshot", "📸  Screenshot Extractor", "screenshot"),
            ("rules", "⚙️  Rules & Regex Studio", "rules"),
            ("settings", "🎛️  Studio Preferences", "settings"),
        ]

        self.nav_frame = tk.Frame(self.sidebar, bg=c["surface"])
        self.nav_frame.pack(fill="both", expand=True, padx=6)

        for key, label, _ in nav_items:
            btn = tk.Button(
                self.nav_frame,
                text=label,
                anchor="w",
                font=("Segoe UI", 10, "normal"),
                relief="flat",
                bd=0,
                padx=12,
                pady=9,
                cursor="hand2",
                command=lambda k=key: self.show_view(k)
            )
            btn.pack(fill="x", pady=2)
            self.nav_buttons[key] = btn

        # Bottom info card in sidebar
        bottom_side = tk.Frame(self.sidebar, bg=c["surface_2"], padx=10, pady=8)
        bottom_side.pack(side="bottom", fill="x", padx=8, pady=8)

        tk.Label(bottom_side, text="Automations Active", font=("Segoe UI", 9, "bold"),
                 bg=c["surface_2"], fg=c["ok"]).pack(anchor="w")
        tk.Label(bottom_side, text="OCR · Deskew · Watcher", font=("Segoe UI", 8),
                 bg=c["surface_2"], fg=c["muted"]).pack(anchor="w")

        # ------------------------------------------------ Center / Right Content Area
        self.content_container = ttk.Frame(self.main_box, style="Card.TFrame")
        self.content_container.pack(side="left", fill="both", expand=True)

        # ------------------------------------------------ Global Status Bar
        self.status_bar = tk.Frame(self, bg=c["surface"], height=24, padx=8)
        self.status_bar.pack(side="bottom", fill="x")

        self.lbl_status_global = tk.Label(self.status_bar, text="Ready · Python 3 · PyMuPDF & Tesseract Ready",
                                          font=("Segoe UI", 8), bg=c["surface"], fg=c["muted"])
        self.lbl_status_global.pack(side="left")

        # Quick Theme Switcher Pill in status bar
        self.btn_quick_theme = tk.Button(
            self.status_bar,
            text="☀ Toggle Light/Dark",
            font=("Segoe UI", 8),
            relief="flat",
            bd=0,
            bg=c["surface"],
            fg=c["accent"],
            cursor="hand2",
            command=self._quick_toggle_theme
        )
        self.btn_quick_theme.pack(side="right", padx=6)

    def show_view(self, key: str) -> None:
        """Switches active view and highlights sidebar navigation."""
        c = self.theme_mgr.colors

        # Instantiate view on-demand (lazy loading for instant responsiveness)
        if key not in self.views:
            if key == "cvsorter":
                self.views[key] = CVSorterView(self.content_container, self.config)
            elif key == "docpacker":
                self.views[key] = DocPackerView(self.content_container, self.config)
            elif key == "merger":
                self.views[key] = MergerView(self.content_container, self.config)
            elif key == "picker":
                self.views[key] = FilePickerView(self.content_container, self.config)
            elif key == "matcher":
                self.views[key] = MatcherView(self.content_container, self.config)
            elif key == "sync":
                self.views[key] = SheetSyncView(self.content_container, self.config)
            elif key == "screenshot":
                self.views[key] = ScreenshotExtractView(self.content_container, self.config)
            elif key == "rules":
                self.views[key] = RulesStudioView(self.content_container, self.config)
            elif key == "settings":
                self.views[key] = SettingsView(self.content_container, self.config, on_theme_change=self._on_theme_updated)

        # Hide current
        for v in self.views.values():
            v.pack_forget()

        # Show target
        self.views[key].pack(fill="both", expand=True)
        self.current_view_key = key

        # Update button styles
        for k, btn in self.nav_buttons.items():
            if k == key:
                btn.configure(bg=c["accent"], fg=c["accent_fg"], font=("Segoe UI", 10, "bold"))
            else:
                btn.configure(bg=c["surface"], fg=c["fg"], font=("Segoe UI", 10, "normal"))

        self.lbl_status_global.configure(text=f"Active Workspace: {btn.cget('text').strip()}")

    def _quick_toggle_theme(self) -> None:
        new_t = "light" if self.config.theme.theme == "dark" else "dark"
        self.config.theme.theme = new_t
        self.config.save()
        self._on_theme_updated()

    def _on_theme_updated(self) -> None:
        """Dynamically re-applies theme live without app restart."""
        self.theme_mgr = ThemeManager(
            theme=self.config.theme.theme,
            accent=self.config.theme.accent,
            font_scale=self.config.theme.font_scale,
        )
        self.theme_mgr.apply(self)
        c = self.theme_mgr.colors

        self.sidebar.configure(bg=c["surface"])
        self.nav_frame.configure(bg=c["surface"])
        self.status_bar.configure(bg=c["surface"])
        self.lbl_status_global.configure(bg=c["surface"], fg=c["muted"])
        self.btn_quick_theme.configure(bg=c["surface"], fg=c["accent"])

        for k, btn in self.nav_buttons.items():
            if k == self.current_view_key:
                btn.configure(bg=c["accent"], fg=c["accent_fg"])
            else:
                btn.configure(bg=c["surface"], fg=c["fg"])

    def _bind_shortcuts(self) -> None:
        keys = ["cvsorter", "docpacker", "merger", "picker", "matcher", "sync", "screenshot", "rules", "settings"]
        for idx, k in enumerate(keys, 1):
            self.bind_all(f"<Control-Key-{idx}>", lambda e, target=k: self.show_view(target))

        self.bind_all("<F11>", lambda e: self.attributes("-fullscreen", not self.attributes("-fullscreen")))

    def _on_close(self) -> None:
        # Save window settings
        try:
            self.config.window.width = self.winfo_width()
            self.config.window.height = self.winfo_height()
            self.config.window.x = self.winfo_x()
            self.config.window.y = self.winfo_y()
            try:
                self.config.window.maximized = (self.state() == "zoomed")
            except Exception:
                pass
            self.config.save()
        except Exception:
            pass

        # Stop watchers / pollers if any
        if "cvsorter" in self.views and hasattr(self.views["cvsorter"], "watcher"):
            w = self.views["cvsorter"].watcher
            if w:
                w.stop()

        if "screenshot" in self.views and hasattr(self.views["screenshot"], "polling"):
            self.views["screenshot"].polling = False

        self.destroy()


def main():
    app = OmniDocsApp()
    app.mainloop()


if __name__ == "__main__":
    main()

