"""Merge Desk & Page Studio: PDF, DOCX, and Image merger with manual page rotation & reordering."""
from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from ..core.config import AppConfig
from ..core.pdf_ops import (
    build_merged_pdf,
    get_document_page_count,
    is_docx,
    is_image,
    is_pdf,
)
from ..ui.widgets import DropTarget, PageViewer


class MergerView(ttk.Frame):
    """Unified Merge Desk and Page Studio."""
    def __init__(self, parent: Any, config: AppConfig, **kwargs):
        super().__init__(parent, padding=12, **kwargs)
        self.config = config
        self.file_items: list[dict[str, Any]] = []

        self._build_ui()

    def _build_ui(self) -> None:
        # Title
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="Merge Desk & Page Studio", style="Header.TLabel").pack(side="left")
        ttk.Label(top, text="Combine PDFs, Word docs & Images into publication-ready PDFs with manual rotation",
                  style="Muted.TLabel").pack(side="left", padx=12, pady=(2, 0))

        # Main splitter (Left: File queue & options, Right: Page Studio & Rotator)
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # ------------------------------------------------ Left: File Queue
        left = ttk.Frame(paned, padding=8)
        paned.add(left, weight=3)

        # Dropzone
        drop = DropTarget(left, text="Drop PDFs, DOCX or Images here",
                          subtext="Files will appear in merge order",
                          on_drop=self._add_files)
        drop.pack(fill="x", pady=(0, 8))

        # Reordering toolbar
        tb = ttk.Frame(left)
        tb.pack(fill="x", pady=(0, 4))
        ttk.Button(tb, text="▲ Move Up", command=self._move_up).pack(side="left", padx=2)
        ttk.Button(tb, text="▼ Move Down", command=self._move_down).pack(side="left", padx=2)
        ttk.Button(tb, text="✕ Remove", command=self._remove_selected).pack(side="left", padx=2)
        ttk.Button(tb, text="Clear All", command=self._clear_all).pack(side="left", padx=2)

        # File List Treeview
        cols = ("pos", "name", "kind", "pages", "size")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse", height=10)
        self.tree.heading("pos", text="#")
        self.tree.heading("name", text="File Name")
        self.tree.heading("kind", text="Format")
        self.tree.heading("pages", text="Pages")
        self.tree.heading("size", text="Size")

        self.tree.column("pos", width=40)
        self.tree.column("name", width=200)
        self.tree.column("kind", width=70)
        self.tree.column("pages", width=60)
        self.tree.column("size", width=80)

        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(fill="both", expand=True, pady=4)
        sb.pack(in_=self.tree, side="right", fill="y")

        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_file_selected())

        # Options Box
        opt_box = ttk.LabelFrame(left, text="Merge Settings", padding=8)
        opt_box.pack(fill="x", pady=6)

        self.v_img_mode = tk.StringVar(value=self.config.merger.get("img_mode", "a4"))
        ttk.Label(opt_box, text="Image Placement:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Radiobutton(opt_box, text="Fit on A4 Page (Centered)", variable=self.v_img_mode, value="a4").grid(row=0, column=1, sticky="w", padx=4)
        ttk.Radiobutton(opt_box, text="Match Photo Canvas", variable=self.v_img_mode, value="own").grid(row=0, column=2, sticky="w", padx=4)

        self.v_auto_open = tk.BooleanVar(value=self.config.automation.auto_open_merged)
        ttk.Checkbutton(opt_box, text="Open PDF automatically upon completion", variable=self.v_auto_open).grid(row=1, column=0, columnspan=3, sticky="w", padx=4, pady=4)

        # Merge Execution Button & Progress
        act_box = ttk.Frame(left)
        act_box.pack(fill="x", pady=(6, 0))

        self.btn_merge = ttk.Button(act_box, text="✨ Merge to PDF...", style="Primary.TButton", command=self._start_merge)
        self.btn_merge.pack(side="left", padx=4)

        self.prog = ttk.Progressbar(act_box, orient="horizontal", mode="determinate")
        self.prog.pack(side="left", fill="x", expand=True, padx=8)

        self.lbl_status = ttk.Label(act_box, text="0 files queued", style="Muted.TLabel")
        self.lbl_status.pack(side="right")

        # ------------------------------------------------ Right: Interactive Page Studio
        right = ttk.LabelFrame(paned, text="Interactive Page Studio & Manual Rotation", padding=8)
        paned.add(right, weight=3)

        self.page_viewer = PageViewer(right, on_change=self._on_page_rotated)
        self.page_viewer.pack(fill="both", expand=True)

    def _add_files(self, paths: list[str]) -> None:
        for p_str in paths:
            p = Path(p_str)
            if not p.is_file():
                continue
            kind = "pdf" if is_pdf(p) else "image" if is_image(p) else "docx" if is_docx(p) else "other"
            if kind == "other":
                continue

            pages = get_document_page_count(p)
            size_kb = p.stat().st_size / 1024
            size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.2f} MB"

            self.file_items.append({
                "path": str(p),
                "name": p.name,
                "kind": kind,
                "pages": pages,
                "size_str": size_str,
                "rotation": 0,
            })
        self._refresh_table()

    def _refresh_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        total_pages = 0
        for idx, item in enumerate(self.file_items):
            total_pages += item["pages"]
            self.tree.insert("", "end", iid=str(idx), values=(
                idx + 1, item["name"], item["kind"].upper(), item["pages"], item["size_str"]
            ))
        self.lbl_status.configure(text=f"{len(self.file_items)} files · {total_pages} total pages")

    def _on_file_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.file_items):
            item = self.file_items[idx]
            self.page_viewer.load_document(item["path"])

    def _on_page_rotated(self) -> None:
        # Sync rotation adjustments back to file_item
        sel = self.tree.selection()
        if sel and self.page_viewer.pages:
            idx = int(sel[0])
            if 0 <= idx < len(self.file_items):
                # If single page image, sync overall rotation
                self.file_items[idx]["rotation"] = self.page_viewer.pages[0]["rotation"]

    def _move_up(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx > 0:
            self.file_items[idx - 1], self.file_items[idx] = self.file_items[idx], self.file_items[idx - 1]
            self._refresh_table()
            self.tree.selection_set(str(idx - 1))

    def _move_down(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx < len(self.file_items) - 1:
            self.file_items[idx + 1], self.file_items[idx] = self.file_items[idx], self.file_items[idx + 1]
            self._refresh_table()
            self.tree.selection_set(str(idx + 1))

    def _remove_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        del self.file_items[idx]
        self._refresh_table()
        self.page_viewer.pages = []
        self.page_viewer.render_current()

    def _clear_all(self) -> None:
        self.file_items.clear()
        self._refresh_table()
        self.page_viewer.pages = []
        self.page_viewer.render_current()

    def _start_merge(self) -> None:
        if not self.file_items:
            messagebox.showinfo("Empty Queue", "Please add files to merge.")
            return

        out_file = filedialog.asksaveasfilename(
            title="Save Merged PDF As",
            defaultextension=".pdf",
            filetypes=[("PDF Documents", "*.pdf")]
        )
        if not out_file:
            return

        self.btn_merge.configure(state="disabled")
        self.prog["value"] = 10

        def worker():
            try:
                img_mode = self.v_img_mode.get()
                ok = build_merged_pdf(self.file_items, out_file, img_mode=img_mode)
                self.after(0, lambda: self._on_merge_done(ok, out_file))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Merge Failed", str(exc)))
                self.after(0, lambda: self.btn_merge.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_merge_done(self, success: bool, out_file: str) -> None:
        self.btn_merge.configure(state="normal")
        self.prog["value"] = 100
        if success:
            messagebox.showinfo("Success", f"PDF successfully merged and saved:\n{out_file}")
            if self.v_auto_open.get():
                try:
                    os.startfile(out_file)
                except Exception:
                    pass
        else:
            messagebox.showerror("Error", "Could not assemble output PDF.")

