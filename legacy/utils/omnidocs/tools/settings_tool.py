"""Preferences & Configuration Hub: Window, Theme, Preview, Automation, and OCR settings."""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from ..core.config import AppConfig
from ..core.ocr import setup_tesseract


class SettingsView(ttk.Frame):
    """Unified application settings view."""
    def __init__(self, parent: Any, config: AppConfig, on_theme_change: Callable[[], None] | None = None, **kwargs):
        super().__init__(parent, padding=12, **kwargs)
        self.config = config
        self.on_theme_change = on_theme_change

        self._build_ui()

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # Title
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="Studio Preferences & Customization", style="Header.TLabel").pack(side="left")
        ttk.Label(top, text="Customize window settings, themes, preview resolution, automation policies, and OCR paths",
                  style="Muted.TLabel").pack(side="left", padx=12, pady=(2, 0))

        # Main splitter (Notebook of categories)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        # --------------------------------------------- Tab 1: Theme & Visuals
        t_theme = ttk.Frame(nb, padding=12)
        nb.add(t_theme, text="🎨 Theme & Appearance")

        ttk.Label(t_theme, text="Color Theme:").grid(row=0, column=0, sticky="w", **pad)
        self.v_theme = tk.StringVar(value=self.config.theme.theme)
        cb_theme = ttk.Combobox(t_theme, textvariable=self.v_theme, values=["dark", "light"], state="readonly", width=14)
        cb_theme.grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(t_theme, text="Accent Color:").grid(row=1, column=0, sticky="w", **pad)
        self.v_accent = tk.StringVar(value=self.config.theme.accent)
        cb_accent = ttk.Combobox(t_theme, textvariable=self.v_accent, values=["emerald", "blue", "purple", "amber", "crimson"], state="readonly", width=14)
        cb_accent.grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(t_theme, text="UI Font Scaling:").grid(row=2, column=0, sticky="w", **pad)
        self.v_font = tk.StringVar(value=self.config.theme.font_scale)
        cb_font = ttk.Combobox(t_theme, textvariable=self.v_font, values=["small", "medium", "large", "xl"], state="readonly", width=14)
        cb_font.grid(row=2, column=1, sticky="w", **pad)

        ttk.Button(t_theme, text="Apply Theme Changes Live", style="Primary.TButton",
                   command=self._apply_theme_live).grid(row=3, column=1, sticky="w", pady=12)

        # --------------------------------------------- Tab 2: Window & Previews
        t_win = ttk.Frame(nb, padding=12)
        nb.add(t_win, text="🪟 Window & Previews")

        self.v_maximized = tk.BooleanVar(value=self.config.window.maximized)
        ttk.Checkbutton(t_win, text="Launch window maximized", variable=self.v_maximized).grid(row=0, column=0, columnspan=2, sticky="w", **pad)

        self.v_topmost = tk.BooleanVar(value=self.config.window.always_on_top)
        ttk.Checkbutton(t_win, text="Always on top of other windows", variable=self.v_topmost).grid(row=1, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(t_win, text="Thumbnail Size:").grid(row=2, column=0, sticky="w", **pad)
        self.v_thumb = tk.IntVar(value=self.config.preview.thumb_size)
        cb_thumb = ttk.Combobox(t_win, textvariable=self.v_thumb, values=[80, 120, 160, 220], state="readonly", width=10)
        cb_thumb.grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(t_win, text="Preview Raster DPI:").grid(row=3, column=0, sticky="w", **pad)
        self.v_dpi = tk.IntVar(value=self.config.preview.preview_dpi)
        cb_dpi = ttk.Combobox(t_win, textvariable=self.v_dpi, values=[100, 150, 200, 300], state="readonly", width=10)
        cb_dpi.grid(row=3, column=1, sticky="w", **pad)

        # --------------------------------------------- Tab 3: Automation Policies
        t_auto = ttk.Frame(nb, padding=12)
        nb.add(t_auto, text="⚡ Automation & QoL")

        self.v_cv_auto = tk.BooleanVar(value=self.config.automation.auto_process_cvs)
        ttk.Checkbutton(t_auto, text="Auto-process CV folder without review prompt if candidate is new", variable=self.v_cv_auto).pack(anchor="w", **pad)

        self.v_deskew = tk.BooleanVar(value=self.config.automation.auto_deskew)
        ttk.Checkbutton(t_auto, text="Auto-deskew tilted documents using OpenCV Hough Transform", variable=self.v_deskew).pack(anchor="w", **pad)

        self.v_autorotate = tk.BooleanVar(value=self.config.automation.auto_rotate)
        ttk.Checkbutton(t_auto, text="Auto-rotate upside-down / sideways pages (Tesseract OSD)", variable=self.v_autorotate).pack(anchor="w", **pad)

        self.v_autocopy = tk.BooleanVar(value=self.config.automation.auto_copy_extracted)
        ttk.Checkbutton(t_auto, text="Auto-copy screenshot extracted row to clipboard", variable=self.v_autocopy).pack(anchor="w", **pad)

        self.v_open_merged = tk.BooleanVar(value=self.config.automation.auto_open_merged)
        ttk.Checkbutton(t_auto, text="Auto-open merged PDF documents when compilation finishes", variable=self.v_open_merged).pack(anchor="w", **pad)

        self.v_auto_reg = tk.BooleanVar(value=self.config.automation.auto_add_candidates_to_registry)
        ttk.Checkbutton(t_auto, text="Auto-grow candidate registry by appending saved names", variable=self.v_auto_reg).pack(anchor="w", **pad)

        # --------------------------------------------- Tab 4: OCR Engine & Paths
        t_ocr = ttk.Frame(nb, padding=12)
        nb.add(t_ocr, text="🔍 Tesseract OCR & Paths")

        ttk.Label(t_ocr, text="Tesseract Executable Path:").grid(row=0, column=0, sticky="w", **pad)
        self.v_tess_path = tk.StringVar(value=self.config.general.tesseract_path)
        ttk.Entry(t_ocr, textvariable=self.v_tess_path, width=40).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(t_ocr, text="Browse...", command=self._browse_tess).grid(row=0, column=2, **pad)

        ttk.Label(t_ocr, text="OCR Language Model(s):").grid(row=1, column=0, sticky="w", **pad)
        self.v_ocr_lang = tk.StringVar(value=self.config.general.ocr_lang)
        ttk.Entry(t_ocr, textvariable=self.v_ocr_lang, width=14).grid(row=1, column=1, sticky="w", **pad)

        ttk.Button(t_ocr, text="Test Tesseract Installation", command=self._test_tesseract).grid(row=2, column=1, sticky="w", pady=10)

        # Bottom Bar: Save & Reset
        bottom_bar = ttk.Frame(self, style="Surface2.TFrame", padding=8)
        bottom_bar.pack(fill="x", pady=(10, 0))

        ttk.Button(bottom_bar, text="💾 Save All Preferences", style="Primary.TButton",
                   command=self._save_all_settings).pack(side="left", padx=4)

        ttk.Button(bottom_bar, text="⟲ Reset to Defaults", command=self._reset_defaults).pack(side="left", padx=4)

    def _browse_tess(self) -> None:
        p = filedialog.askopenfilename(title="Select tesseract.exe", filetypes=[("Executable", "*.exe"), ("All", "*.*")])
        if p:
            self.v_tess_path.set(p)

    def _test_tesseract(self) -> None:
        ok = setup_tesseract(self.v_tess_path.get().strip())
        if ok:
            messagebox.showinfo("Tesseract OCR OK", "Tesseract executable was located and initialized successfully!")
        else:
            messagebox.showwarning("Tesseract Not Found", "Could not execute Tesseract at the specified path.\nPlease verify the file exists.")

    def _apply_theme_live(self) -> None:
        self.config.theme.theme = self.v_theme.get()
        self.config.theme.accent = self.v_accent.get()
        self.config.theme.font_scale = self.v_font.get()
        if self.on_theme_change:
            self.on_theme_change()

    def _save_all_settings(self) -> None:
        # Save theme
        self.config.theme.theme = self.v_theme.get()
        self.config.theme.accent = self.v_accent.get()
        self.config.theme.font_scale = self.v_font.get()

        # Save window & preview
        self.config.window.maximized = self.v_maximized.get()
        self.config.window.always_on_top = self.v_topmost.get()
        self.config.preview.thumb_size = self.v_thumb.get()
        self.config.preview.preview_dpi = self.v_dpi.get()

        # Save automation
        self.config.automation.auto_process_cvs = self.v_cv_auto.get()
        self.config.automation.auto_deskew = self.v_deskew.get()
        self.config.automation.auto_rotate = self.v_autorotate.get()
        self.config.automation.auto_copy_extracted = self.v_autocopy.get()
        self.config.automation.auto_open_merged = self.v_open_merged.get()
        self.config.automation.auto_add_candidates_to_registry = self.v_auto_reg.get()

        # Save OCR
        self.config.general.tesseract_path = self.v_tess_path.get().strip()
        self.config.general.ocr_lang = self.v_ocr_lang.get().strip()

        self.config.save()
        if self.on_theme_change:
            self.on_theme_change()

        messagebox.showinfo("Saved", "All settings successfully saved to omnidocs_config.json!")

    def _reset_defaults(self) -> None:
        if messagebox.askyesno("Reset", "Reset all preferences to default values?"):
            self.config = AppConfig()
            self.config.save()
            if self.on_theme_change:
                self.on_theme_change()
            messagebox.showinfo("Reset", "Settings reset to defaults.")

