"""Reusable Modern UI Widgets: DropTarget, MetricPill, PageViewer, DiffTable, and StatusBadge."""
from __future__ import annotations

import io
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from PIL import Image, ImageTk

try:
    from tkinterdnd2 import DND_FILES
    HAS_DND = True
except Exception:
    HAS_DND = False

from ..core.pdf_ops import render_page_thumbnail


class DropTarget(ttk.Frame):
    """Modern Drag-and-Drop Area supporting files or folders with browse fallback."""
    def __init__(self, parent: Any, text: str = "Drop files here or click to browse",
                 subtext: str = "Supports PDF, DOCX, Images, Spreadsheets",
                 on_drop: Callable[[list[str]], None] | None = None,
                 is_folder: bool = False, file_types: list[tuple[str, str]] | None = None,
                 **kwargs):
        super().__init__(parent, style="Card.TFrame", padding=14, **kwargs)
        self.on_drop_cb = on_drop
        self.is_folder = is_folder
        self.file_types = file_types or [("All Supported", "*.pdf *.docx *.jpg *.png *.xlsx *.csv"), ("All Files", "*.*")]

        self.lbl_main = ttk.Label(self, text=text, style="Accent.TLabel", cursor="hand2")
        self.lbl_main.pack(pady=(4, 2))
        
        self.lbl_sub = ttk.Label(self, text=subtext, style="Muted.TLabel", cursor="hand2")
        self.lbl_sub.pack(pady=(0, 4))

        # Click to browse
        for w in (self, self.lbl_main, self.lbl_sub):
            w.bind("<Button-1>", lambda e: self._browse())

        # Native DND2 registration
        if HAS_DND:
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._on_dnd_drop)
                self.dnd_bind("<<DragEnter>>", lambda e: self.configure(relief="solid"))
                self.dnd_bind("<<DragLeave>>", lambda e: self.configure(relief="flat"))
            except Exception:
                pass

    def _browse(self) -> None:
        if self.is_folder:
            folder = filedialog.askdirectory(title="Select Folder")
            if folder and self.on_drop_cb:
                self.on_drop_cb([folder])
        else:
            files = filedialog.askopenfilenames(title="Select Files", filetypes=self.file_types)
            if files and self.on_drop_cb:
                self.on_drop_cb(list(files))

    def _on_dnd_drop(self, event: Any) -> None:
        data = event.data
        if not data:
            return
        # Parse curly braced or space separated paths
        files = []
        if "{" in data:
            import re
            files = re.findall(r"\{([^}]+)\}", data)
        else:
            files = data.split()
        if files and self.on_drop_cb:
            self.on_drop_cb([f.strip() for f in files if f.strip()])


class MetricPill(ttk.Frame):
    """Large KPI / metric display card."""
    def __init__(self, parent: Any, title: str, initial_value: str = "0", **kwargs):
        super().__init__(parent, style="Card.TFrame", padding=(12, 8), **kwargs)
        self.var_val = tk.StringVar(value=initial_value)
        
        self.lbl_val = ttk.Label(self, textvariable=self.var_val, font=("Segoe UI", 18, "bold"))
        self.lbl_val.pack(anchor="w")
        
        self.lbl_title = ttk.Label(self, text=title, style="Muted.TLabel")
        self.lbl_title.pack(anchor="w")

    def set(self, val: Any) -> None:
        self.var_val.set(str(val))


class StatusBadge(tk.Label):
    """Pill badge showing status with colors."""
    def __init__(self, parent: Any, text: str = "OK", kind: str = "ok", **kwargs):
        super().__init__(parent, text=text, font=("Segoe UI", 9, "bold"), padx=8, pady=2, **kwargs)
        self.set_status(text, kind)

    def set_status(self, text: str, kind: str = "ok") -> None:
        colors = {
            "ok": ("#1b3a29", "#6fd6a0"),
            "warn": ("#3d3114", "#f0c168"),
            "danger": ("#42201f", "#f19a9a"),
            "info": ("#1c3d5a", "#8fc2f5"),
            "neutral": ("#232c27", "#9aa89f"),
        }
        bg, fg = colors.get(kind, colors["neutral"])
        self.configure(text=text, bg=bg, fg=fg)


class PageViewer(ttk.Frame):
    """
    Interactive Document & Page Studio.
    Provides visual thumbnails, zoom inspection, manual page rotation (90° CW, 90° CCW, 180°),
    reordering, deletion, and slot assignment.
    """
    def __init__(self, parent: Any, on_change: Callable[[], None] | None = None, **kwargs):
        super().__init__(parent, style="Card.TFrame", padding=10, **kwargs)
        self.on_change = on_change
        self.pages: list[dict[str, Any]] = []  # items: {doc_path, page_idx, rot, thumb_img}
        self.selected_idx = 0

        # Toolbar
        bar = ttk.Frame(self, style="Surface2.TFrame", padding=6)
        bar.pack(fill="x", pady=(0, 8))

        ttk.Button(bar, text="⟲ 90° CCW", command=lambda: self.rotate_selected(-90)).pack(side="left", padx=2)
        ttk.Button(bar, text="⟳ 90° CW", command=lambda: self.rotate_selected(90)).pack(side="left", padx=2)
        ttk.Button(bar, text="↷ 180°", command=lambda: self.rotate_selected(180)).pack(side="left", padx=2)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        
        ttk.Button(bar, text="▲ Move Up", command=self.move_up).pack(side="left", padx=2)
        ttk.Button(bar, text="▼ Move Down", command=self.move_down).pack(side="left", padx=2)
        ttk.Button(bar, text="✕ Delete", style="Danger.TButton", command=self.delete_selected).pack(side="left", padx=4)
        
        # Page info
        self.lbl_info = ttk.Label(bar, text="No document loaded", style="Muted.TLabel")
        self.lbl_info.pack(side="right", padx=6)

        # Center Preview & Canvas
        preview_box = ttk.Frame(self)
        preview_box.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(preview_box, bg="#101412", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # Thumbnail navigation strip
        strip_frame = ttk.Frame(self, style="Surface2.TFrame", padding=4)
        strip_frame.pack(fill="x", pady=(8, 0))

        ttk.Button(strip_frame, text="◀ Previous", command=self.prev_page).pack(side="left")
        self.lbl_nav = ttk.Label(strip_frame, text="Page 0 of 0", style="Muted.TLabel")
        self.lbl_nav.pack(side="left", expand=True)
        ttk.Button(strip_frame, text="Next ▶", command=self.next_page).pack(side="right")

    def load_document(self, path: Path | str) -> None:
        """Loads pages of a PDF or image into the visual studio."""
        from ..core.pdf_ops import get_document_page_count, is_pdf
        p = Path(path)
        count = get_document_page_count(p)
        self.pages = []
        for i in range(count):
            self.pages.append({
                "path": str(p),
                "page_idx": i,
                "rotation": 0,
            })
        self.selected_idx = 0
        self.render_current()

    def render_current(self) -> None:
        self.canvas.delete("all")
        if not self.pages or self.selected_idx >= len(self.pages):
            self.lbl_info.configure(text="0 pages")
            self.lbl_nav.configure(text="Page 0 of 0")
            return

        item = self.pages[self.selected_idx]
        self.lbl_info.configure(text=f"{Path(item['path']).name} (Rot: {item['rotation']}°)")
        self.lbl_nav.configure(text=f"Page {self.selected_idx + 1} of {len(self.pages)}")

        # Render thumbnail
        img = render_page_thumbnail(item["path"], item["page_idx"], dpi=140)
        if img:
            if item["rotation"]:
                img = img.rotate(360 - item["rotation"], expand=True)

            # Fit to canvas
            cw = max(200, self.canvas.winfo_width())
            ch = max(200, self.canvas.winfo_height())
            scale = min((cw - 20) / img.width, (ch - 20) / img.height)
            dw = max(50, int(img.width * scale))
            dh = max(50, int(img.height * scale))
            img_resized = img.resize((dw, dh), Image.Resampling.LANCZOS)
            
            self._tk_img = ImageTk.PhotoImage(img_resized)
            self.canvas.create_image(cw // 2, ch // 2, image=self._tk_img, anchor="center")

    def rotate_selected(self, deg: int) -> None:
        if self.pages and 0 <= self.selected_idx < len(self.pages):
            self.pages[self.selected_idx]["rotation"] = (self.pages[self.selected_idx]["rotation"] + deg) % 360
            self.render_current()
            if self.on_change:
                self.on_change()

    def move_up(self) -> None:
        if self.selected_idx > 0:
            idx = self.selected_idx
            self.pages[idx - 1], self.pages[idx] = self.pages[idx], self.pages[idx - 1]
            self.selected_idx -= 1
            self.render_current()
            if self.on_change:
                self.on_change()

    def move_down(self) -> None:
        if self.selected_idx < len(self.pages) - 1:
            idx = self.selected_idx
            self.pages[idx + 1], self.pages[idx] = self.pages[idx], self.pages[idx + 1]
            self.selected_idx += 1
            self.render_current()
            if self.on_change:
                self.on_change()

    def delete_selected(self) -> None:
        if self.pages and 0 <= self.selected_idx < len(self.pages):
            del self.pages[self.selected_idx]
            if self.selected_idx >= len(self.pages):
                self.selected_idx = max(0, len(self.pages) - 1)
            self.render_current()
            if self.on_change:
                self.on_change()

    def prev_page(self) -> None:
        if self.selected_idx > 0:
            self.selected_idx -= 1
            self.render_current()

    def next_page(self) -> None:
        if self.selected_idx < len(self.pages) - 1:
            self.selected_idx += 1
            self.render_current()


class DiffTable(ttk.Frame):
    """Displays cell modification diffs with distinct visual markers."""
    def __init__(self, parent: Any, **kwargs):
        super().__init__(parent, **kwargs)
        cols = ("key", "column", "change", "old", "new")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=8)
        
        self.tree.heading("key", text="Unique Key")
        self.tree.heading("column", text="Column Name")
        self.tree.heading("change", text="Change Type")
        self.tree.heading("old", text="Old Value")
        self.tree.heading("new", text="New Value")

        self.tree.column("key", width=120)
        self.tree.column("column", width=130)
        self.tree.column("change", width=110)
        self.tree.column("old", width=180)
        self.tree.column("new", width=180)

        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)

        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def populate(self, change_log: list[dict[str, Any]]) -> None:
        self.tree.delete(*self.tree.get_children())
        for entry in change_log[:400]:  # Cap rendering to 400 items for smoothness
            self.tree.insert("", "end", values=(
                entry.get("key", ""),
                entry.get("column", ""),
                entry.get("change", ""),
                entry.get("old_val", ""),
                entry.get("new_val", ""),
            ))

