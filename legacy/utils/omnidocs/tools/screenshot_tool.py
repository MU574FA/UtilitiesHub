"""Screenshot Extractor: Background clipboard screen snip monitor, OCR, and spreadsheet row generator."""
from __future__ import annotations

import hashlib
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from PIL import Image, ImageTk

from ..core.clipboard import (
    compute_image_hash,
    copy_to_clipboard,
    format_spreadsheet_row,
    get_clipboard_image,
)
from ..core.config import AppConfig
from ..core.ocr import (
    ocr_image,
    preprocess_image,
    repair_email_whitespace,
    setup_tesseract,
)
from ..core.rules import get_default_rules


class ScreenshotExtractView(ttk.Frame):
    """Real-time screenshot extractor view."""
    def __init__(self, parent: Any, config: AppConfig, **kwargs):
        super().__init__(parent, padding=12, **kwargs)
        self.config = config
        self.rules = get_default_rules()

        self.last_img_hash = ""
        self.last_image: Image.Image | None = None
        self.polling = False

        self.field_vars: dict[str, tk.BooleanVar] = {}
        self.extracted_data: dict[str, str] = {}

        self._build_ui()

    def _build_ui(self) -> None:
        # Title
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="Screenshot Extractor", style="Header.TLabel").pack(side="left")
        ttk.Label(top, text="Monitors clipboard for Win+Shift+S screen snips, applies OCR & generates spreadsheet rows",
                  style="Muted.TLabel").pack(side="left", padx=12, pady=(2, 0))

        # Main splitter (Left: Control Panel & Form, Right: Screenshot Preview & Raw OCR)
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # --------------------------------------------- Left: Control Panel & Form
        left = ttk.Frame(paned, padding=8)
        paned.add(left, weight=3)

        # Monitor toolbar
        tb = ttk.Frame(left, style="Surface2.TFrame", padding=8)
        tb.pack(fill="x", pady=(0, 8))

        self.v_monitoring = tk.BooleanVar(value=True)
        self.btn_toggle_mon = ttk.Button(tb, text="⏸ Pause Listener", command=self._toggle_monitor)
        self.btn_toggle_mon.pack(side="left", padx=4)

        ttk.Button(tb, text="⟲ Re-scan Last Screenshot", command=self._rescan_last).pack(side="left", padx=4)

        self.v_auto_copy = tk.BooleanVar(value=self.config.screenshot.get("auto_copy", True))
        ttk.Checkbutton(tb, text="Auto-copy row to clipboard", variable=self.v_auto_copy).pack(side="left", padx=12)

        self.lbl_status = ttk.Label(tb, text="Listening for Win+Shift+S snips...", style="Accent.TLabel")
        self.lbl_status.pack(side="right", padx=6)

        # Active Fields Selection
        f_box = ttk.LabelFrame(left, text="Active Fields to Extract", padding=8)
        f_box.pack(fill="x", pady=4)

        chips_grid = ttk.Frame(f_box)
        chips_grid.pack(fill="x")

        # Pick top extractors
        extractors = [r for r in self.rules if r.category == "Extractor" or r.id in ("phone", "email", "name", "job_title", "iban")]
        for idx, r in enumerate(extractors):
            var = tk.BooleanVar(value=True)
            self.field_vars[r.id] = var
            row = idx // 4
            col = idx % 4
            cb = ttk.Checkbutton(chips_grid, text=r.name, variable=var)
            cb.grid(row=row, column=col, sticky="w", padx=6, pady=2)

        # Editable Values Form
        form_box = ttk.LabelFrame(left, text="Extracted Fields (Editable)", padding=8)
        form_box.pack(fill="both", expand=True, pady=6)

        self.form_entries: dict[str, ttk.Entry] = {}
        for idx, r in enumerate(extractors):
            row_frame = ttk.Frame(form_box)
            row_frame.pack(fill="x", pady=2)
            ttk.Label(row_frame, text=f"{r.name}:", width=16, anchor="w").pack(side="left")
            ent = ttk.Entry(row_frame, font=("Consolas", 10))
            ent.pack(side="left", fill="x", expand=True, padx=4)
            self.form_entries[r.id] = ent

        # Copy Row Button
        act_bar = ttk.Frame(left)
        act_bar.pack(fill="x", pady=4)
        ttk.Button(act_bar, text="📋 Copy Tab-Separated Row (for Excel)", style="Primary.TButton",
                   command=self._copy_current_row).pack(side="left", padx=4)

        # --------------------------------------------- Right: Visual Preview
        right = ttk.LabelFrame(paned, text="Captured Screenshot & OCR Text", padding=8)
        paned.add(right, weight=3)

        self.lbl_thumb = ttk.Label(right, text="No screenshot in memory\n(Press Win+Shift+S to capture)",
                                   anchor="center", justify="center")
        self.lbl_thumb.pack(fill="x", expand=False, pady=10)

        ttk.Label(right, text="Raw Extracted OCR Text:", style="Muted.TLabel").pack(anchor="w", pady=(8, 2))
        self.txt_ocr = tk.Text(right, height=10, wrap="word", font=("Consolas", 9), state="disabled", bg="#141816", fg="#e6ebe7")
        sb_ocr = ttk.Scrollbar(right, command=self.txt_ocr.yview)
        self.txt_ocr.configure(yscrollcommand=sb_ocr.set)
        self.txt_ocr.pack(side="left", fill="both", expand=True)
        sb_ocr.pack(side="right", fill="y")

        # Start clipboard polling loop
        self.polling = True
        self.after(500, self._poll_clipboard)

    def _toggle_monitor(self) -> None:
        self.polling = not self.polling
        if self.polling:
            self.btn_toggle_mon.configure(text="⏸ Pause Listener")
            self.lbl_status.configure(text="Listening for Win+Shift+S snips...")
            self._poll_clipboard()
        else:
            self.btn_toggle_mon.configure(text="▶ Resume Listener")
            self.lbl_status.configure(text="Listener paused")

    def _poll_clipboard(self) -> None:
        if not self.polling:
            return

        try:
            img = get_clipboard_image()
            if img is not None:
                h = compute_image_hash(img)
                if h and h != self.last_img_hash:
                    self.last_img_hash = h
                    self.last_image = img
                    self._process_image(img)
        except Exception:
            pass

        if self.polling:
            self.after(500, self._poll_clipboard)

    def _process_image(self, img: Image.Image) -> None:
        setup_tesseract(self.config.general.tesseract_path)
        
        # 1. Image preprocessing
        proc = preprocess_image(img, auto_upscale=True, contrast_boost=True)
        
        # 2. OCR
        raw_text = ocr_image(proc, lang=self.config.general.ocr_lang)
        raw_text = repair_email_whitespace(raw_text)

        # Update preview
        self._update_preview(img, raw_text)

        # 3. Extract active fields
        values = {}
        for r in self.rules:
            if not self.field_vars.get(r.id, tk.BooleanVar(value=False)).get():
                continue
            passed, vals, _ = r.evaluate(raw_text)
            values[r.id] = vals[0] if vals else ""

        # Update form entries
        for rid, val in values.items():
            if rid in self.form_entries:
                self.form_entries[rid].delete(0, "end")
                self.form_entries[rid].insert(0, val)

        # Auto-copy if enabled
        if self.v_auto_copy.get():
            self._copy_current_row()

    def _update_preview(self, img: Image.Image, raw_text: str) -> None:
        # Resize thumbnail to fit
        scale = min(400 / img.width, 240 / img.height, 1.0)
        tw = max(50, int(img.width * scale))
        th = max(50, int(img.height * scale))
        resized = img.resize((tw, th), Image.Resampling.LANCZOS)
        self._photo_ref = ImageTk.PhotoImage(resized)
        self.lbl_thumb.configure(image=self._photo_ref, text="")

        self.txt_ocr.configure(state="normal")
        self.txt_ocr.delete("1.0", "end")
        self.txt_ocr.insert("1.0", raw_text)
        self.txt_ocr.configure(state="disabled")

    def _rescan_last(self) -> None:
        if self.last_image:
            self._process_image(self.last_image)
        else:
            messagebox.showinfo("No Image", "No screenshot currently loaded.")

    def _copy_current_row(self) -> None:
        row_vals = []
        for rid, ent in self.form_entries.items():
            if self.field_vars.get(rid, tk.BooleanVar()).get():
                row_vals.append(ent.get().strip())

        tab_text = format_spreadsheet_row(row_vals)
        if tab_text:
            copy_to_clipboard(tab_text)
            self.lbl_status.configure(text="Copied row to clipboard! Click Excel cell & paste.")

