"""PDF Desk: merge PDFs / images / Word files page by page, rotate, reorder, delete, split, compress, number, stamp, OCR."""
from __future__ import annotations

import os
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from ..core import ocr
from ..core.pdfops import COMPRESSION_LEVELS, AssembleOptions, Piece, assemble, extract_pages, make_searchable, split_pdf
from ..core.readers import supported_extensions
from ..core.schema import Opt
from ..core.system import open_path, reveal
from ..core.textutil import parse_page_ranges, sanitize_filename
from ..ui.dnd import DropZone
from ..ui.thumbs import ThumbGrid, ThumbItem
from ..ui.viewer import PageViewer
from ..ui.widgets import ask_text, confirm, error, info as info_box
from .base import Command, Tool
from .docpacker import compress_at_least

PICK_EXTS = [".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".docx", ".doc", ".pptx", ".ppt", ".rtf", ".odt"]


class PdfDesk(Tool):
    id = "pdf_desk"
    title = "PDF Desk"
    icon = "📄"
    order = 40
    description = "Merge PDFs, images and Word files; rotate, reorder and delete pages; split, compress, number, watermark, OCR."

    @classmethod
    def options(cls) -> list[Opt]:
        return [
            Opt("pdf_desk.output_folder", "", "Save results to (empty = ask each time)", "folder", section="Output"),
            Opt("pdf_desk.open_after", True, "Open the result when finished", "bool", section="Output"),
            Opt("pdf_desk.page_size", "a4", "Page size for images", "choice", section="Merging",
                choices=[("a4", "A4"), ("letter", "Letter"), ("fit", "Same as the image")]),
            Opt("pdf_desk.bookmarks", True, "Add one bookmark per source file", "bool", section="Merging"),
            Opt("pdf_desk.page_numbers", "", "Page numbers, e.g. Page {n} of {total} (empty = none)", "str", section="Merging"),
            Opt("pdf_desk.watermark", "", "Watermark text (empty = none)", "str", section="Merging"),
            Opt("pdf_desk.image_quality", 90, "Image quality (JPEG %)", "percent", min=30, max=100, section="Merging"),
            Opt("pdf_desk.compression", 0, "Compression level (0 = none, 4 = strongest)", "int", min=0, max=4, section="Compression"),
            Opt("pdf_desk.max_mb", 0.0, "Target size in MB (0 = no limit)", "float", min=0, max=500, section="Compression"),
            Opt("pdf_desk.searchable", False, "Make the result searchable with OCR", "bool", section="OCR",
                help="Adds an invisible text layer to scanned pages (needs Tesseract)."),
            Opt("pdf_desk.sort_natural", True, "Sort added folders by natural file name (2 before 10)", "bool", section="Adding"),
            *cls.append_opts(auto=True),
        ]

    # ------------------------------------------------------------------ build
    def build(self, parent: ttk.Frame) -> None:
        self.sources: dict[str, int] = {}               # path -> page count
        self.last_output = ""
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        ttk.Button(bar, text="Add files…", command=self.pick_files).pack(side="left")
        ttk.Button(bar, text="Add folder…", command=self.pick_folder).pack(side="left", padx=4)
        ttk.Button(bar, text="Sort by name", command=self.sort_by_name).pack(side="left")
        ttk.Button(bar, text="Reverse", command=self.reverse).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove file of selection", command=self.remove_files_of_selection).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left", padx=4)
        self.count = ttk.Label(bar, style="Muted.TLabel")
        self.count.pack(side="right")

        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=8)
        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        self.grid = ThumbGrid(left, self.app)
        self.grid.pack(fill="both", expand=True)
        self.grid.on_change = self._changed
        self.grid.on_select = self._selected
        self.grid.on_open = self._open_item
        self.drop = DropZone(left, "Drop PDFs, images or Word files here", self.add_paths, theme=self.app.theme, height=50)
        self.drop.pack(fill="x", pady=(6, 0))

        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        nb = ttk.Notebook(right)
        nb.pack(fill="x")
        self._build_output_tab(nb)
        self._build_tools_tab(nb)
        self.viewer = PageViewer(right, self.app)
        self.viewer.pack(fill="both", expand=True, pady=(8, 0))
        self.viewer.on_rotate = self._viewer_rotated
        self._changed()

    def _build_output_tab(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Merge & save")
        self.vars: dict[str, tk.Variable] = {}
        for row, (key, label, kind) in enumerate((("page_size", "Page size for images", "page"),
                                                   ("page_numbers", "Page numbers", "str"),
                                                   ("watermark", "Watermark", "str"),
                                                   ("compression", "Compression (0-4)", "int"),
                                                   ("max_mb", "Target size (MB, 0 = none)", "float"))):
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=2)
            if kind == "page":
                var = tk.StringVar(value=self.get(key))
                w = ttk.Combobox(f, textvariable=var, values=["a4", "letter", "fit"], state="readonly", width=10)
            elif kind == "int":
                var = tk.StringVar(value=str(self.get(key)))
                w = ttk.Spinbox(f, textvariable=var, from_=0, to=len(COMPRESSION_LEVELS) - 1, width=6)
            elif kind == "float":
                var = tk.StringVar(value=str(self.get(key)))
                w = ttk.Spinbox(f, textvariable=var, from_=0, to=500, increment=0.5, width=6)
            else:
                var = tk.StringVar(value=str(self.get(key)))
                w = ttk.Entry(f, textvariable=var, width=24)
            w.grid(row=row, column=1, sticky="w", padx=8)
            var.trace_add("write", lambda *_, k=key, v=var, t=kind: self._store(k, v.get(), t))
            self.vars[key] = var
        row = 5
        for key, label in (("bookmarks", "Bookmarks (one per file)"), ("searchable", "Make searchable (OCR)"),
                           ("open_after", "Open result when done")):
            var = tk.BooleanVar(value=bool(self.get(key)))
            ttk.Checkbutton(f, text=label, variable=var,
                            command=lambda k=key, v=var: self.put(k, v.get())).grid(row=row, column=0, columnspan=2, sticky="w")
            row += 1
        ttk.Button(f, text="Merge & save…", style="Accent.TButton", command=self.merge_save).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _store(self, key: str, raw: str, kind: str) -> None:
        try:
            self.put(key, int(float(raw)) if kind == "int" else float(raw) if kind == "float" else raw)
        except ValueError:
            pass

    def _build_tools_tab(self, nb: ttk.Notebook) -> None:
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Split & extract")
        ttk.Label(f, text="Pages are counted in the grid order, as shown (1 = first thumbnail).", style="Muted.TLabel",
                  wraplength=330).pack(anchor="w")
        ttk.Button(f, text="Extract selected pages to a new PDF…", command=self.extract_selected).pack(fill="x", pady=(6, 2))
        self.split_var = tk.StringVar(value="1-3, 4-6")
        ttk.Label(f, text="Split into files - one range per file, e.g. 1-3, 4-6, 7-").pack(anchor="w", pady=(8, 0))
        ttk.Entry(f, textvariable=self.split_var).pack(fill="x")
        ttk.Button(f, text="Split by ranges…", command=self.split_ranges).pack(fill="x", pady=2)
        self.every_var = tk.StringVar(value="1")
        r = ttk.Frame(f)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text="or every").pack(side="left")
        ttk.Spinbox(r, textvariable=self.every_var, from_=1, to=1000, width=5).pack(side="left", padx=4)
        ttk.Label(r, text="page(s)").pack(side="left")
        ttk.Button(r, text="Split…", command=self.split_every).pack(side="right")
        ttk.Separator(f).pack(fill="x", pady=8)
        ttk.Button(f, text="Rotate all pages ⟳", command=lambda: self._rotate_all(90)).pack(fill="x")
        ttk.Button(f, text="Keep only the selected pages", command=self.keep_selected).pack(fill="x", pady=2)
        ttk.Button(f, text="Reveal last result", command=lambda: self.last_output and reveal(self.last_output)).pack(fill="x")

    # ------------------------------------------------------------------ adding files
    def accept_files(self, paths: list[str]) -> None:
        self.add_paths(paths)

    def pick_files(self) -> None:
        exts = " ".join(f"*{e}" for e in PICK_EXTS)
        paths = filedialog.askopenfilenames(title="Add files", filetypes=[("PDF, images, Word", exts), ("All", "*.*")])
        if paths:
            self.add_paths(list(paths))

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="Add all files of a folder")
        if folder:
            self.add_paths([folder])

    @staticmethod
    def _natural(path: str):
        import re
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", Path(path).name)]

    def add_paths(self, paths: list[str]) -> None:
        ok = set(PICK_EXTS)
        files: list[str] = []
        for p in map(Path, paths):
            if p.is_dir():
                found = [str(f) for f in p.iterdir() if f.is_file() and f.suffix.lower() in ok]
                files.extend(sorted(found, key=self._natural) if self.get("sort_natural") else sorted(found))
            elif p.is_file() and p.suffix.lower() in ok:
                files.append(str(p))
        if not files:
            self.log("Nothing to add: no PDF, image or Word files found.", "warn")
            return

        def work(job):
            out = []
            for n, f in enumerate(files):
                job.progress(n, len(files), Path(f).name)
                try:
                    out.append((f, max(1, self.app.renderer.page_count(f))))
                except Exception as exc:
                    out.append((f, exc))
            return out

        self.app.jobs.submit(work, f"Adding {len(files)} file(s)", tool=self.id, on_done=self._added,
                             on_error=lambda m: error(self.frame, m), serial_key="pdf_desk.add")

    def _added(self, results) -> None:
        items = list(self.grid.items)
        for f, count in results:
            if isinstance(count, Exception):
                self.log(f"{Path(f).name}: {count}", "warn")
                continue
            self.sources[f] = count
            items.extend(ThumbItem(f, p, label=f"{Path(f).stem[:18]} p.{p + 1}" if count > 1 else Path(f).stem[:24])
                         for p in range(count))
        self.grid.set_items(items)
        self._changed()

    # ------------------------------------------------------------------ grid state
    def _changed(self) -> None:
        n = len(self.grid.items)
        files = len({i.path for i in self.grid.items})
        self.count.configure(text=f"{n} page(s) from {files} file(s)")

    def _selected(self, items: list[ThumbItem]) -> None:
        if items and (self.viewer.path is None or str(self.viewer.path) != items[0].path):
            rots = {i.page: i.rotation for i in self.grid.items if i.path == items[0].path and i.rotation}
            self.viewer.show(items[0].path, rots)
        if items:
            self.viewer.after(300, lambda p=items[0].page: self.viewer.goto(p))

    def _open_item(self, item: ThumbItem) -> None:
        open_path(item.path)

    def _viewer_rotated(self, page: int, total: int) -> None:
        if self.viewer.path is None:
            return
        for it in self.grid.items:
            if it.path == str(self.viewer.path) and it.page == page:
                it.rotation = total
        self.grid.redraw()

    def _rotate_all(self, deg: int) -> None:
        for it in self.grid.items:
            it.rotation = (it.rotation + deg) % 360
        self.grid.redraw()

    def sort_by_name(self) -> None:
        self.grid.items.sort(key=lambda i: (self._natural(i.path), i.page))
        self.grid.redraw()

    def reverse(self) -> None:
        self.grid.items.reverse()
        self.grid.redraw()

    def keep_selected(self) -> None:
        sel = self.grid.selected()
        if sel:
            self.grid.set_items(sel)
            for it in sel:
                it.selected = False
            self.grid.redraw()
            self._changed()

    def remove_files_of_selection(self) -> None:
        paths = {i.path for i in self.grid.selected()}
        if paths:
            self.grid.set_items([i for i in self.grid.items if i.path not in paths])
            self._changed()

    def clear(self) -> None:
        if self.grid.items and not confirm(self.frame, "Remove all pages from the list?"):
            return
        self.grid.set_items([])
        self.sources.clear()
        self.viewer.clear()
        self._changed()

    # ------------------------------------------------------------------ pieces
    def pieces(self, items: list[ThumbItem] | None = None) -> list[Piece]:
        """Consecutive pages of the same file become one Piece (keeps bookmarks tidy)."""
        out: list[Piece] = []
        for it in items if items is not None else self.grid.items:
            if out and out[-1].path == it.path and out[-1].pages is not None:
                piece = out[-1]
            else:
                piece = Piece(it.path, pages=[], title=Path(it.path).stem)
                out.append(piece)
            piece.pages.append(it.page)
            if it.rotation:
                piece.page_rotate[it.page] = it.rotation
        for piece in out:                       # whole untouched file -> let assemble use all pages
            if piece.pages == list(range(self.sources.get(piece.path, -1))):
                piece.pages = None
        return out

    def _options(self) -> AssembleOptions:
        return AssembleOptions(page_size=self.get("page_size"), bookmarks=self.get("bookmarks"),
                               page_numbers=self.get("page_numbers"), watermark=self.get("watermark"),
                               quality=self.get("image_quality"), libreoffice_cmd=self.settings.get("ocr.libreoffice_cmd"),
                               metadata={"creator": "Utils Hub"})

    def _ask_save(self, initial: str) -> str:
        folder = self.get("output_folder")
        if folder:
            base = Path(folder) / initial
            n = 2
            while base.exists():
                base = Path(folder) / f"{Path(initial).stem} ({n}).pdf"
                n += 1
            return str(base)
        return filedialog.asksaveasfilename(title="Save PDF as", defaultextension=".pdf", initialfile=initial,
                                            filetypes=[("PDF", "*.pdf")], parent=self.frame)

    def _finish(self, path: str, size: int = 0, pages: int = 0, extra: str = "") -> None:
        self.last_output = path
        size = size or (os.path.getsize(path) if os.path.exists(path) else 0)
        msg = f"Saved {Path(path).name} ({pages or '?'} pages, {size / 1024 / 1024:.2f} MB){extra}"
        self.log(msg, "ok")
        self.app.notify(msg, "ok")
        self.append_rows([{"output_file": path, "pages": pages or "", "size_mb": round(size / 1024 / 1024, 2),
                           "note": extra.strip(" ()-")}], quiet=True)
        if self.get("open_after"):
            open_path(path)

    # ------------------------------------------------------------------ actions
    def merge_save(self) -> None:
        items = list(self.grid.items)
        if not items:
            info_box(self.frame, "Add some files first.")
            return
        first = Path(items[0].path).stem
        out = self._ask_save(sanitize_filename(f"{first} - merged") + ".pdf")
        if not out:
            return
        pieces, opts = self.pieces(), self._options()
        level, target = self.get("compression"), int(self.get("max_mb") * 1024 * 1024)
        searchable = self.get("searchable")
        cfg = ocr.OcrConfig.from_settings(self.settings)

        def work(job):
            res = assemble(pieces, out, opts, progress=lambda n, t, name: job.progress(n, t, name))
            if level or target:
                job.progress(0, 0, "Compressing…")
                res.size = compress_at_least(res.path, level, target)
            if searchable:
                job.progress(0, 0, "Running OCR…")
                tmp = res.path + ".ocr.pdf"
                make_searchable(res.path, tmp, cfg)
                os.replace(tmp, res.path)
                res.size = os.path.getsize(res.path)
            return res

        def done(res) -> None:
            self._finish(res.path, res.size, res.pages, f" - skipped: {'; '.join(res.skipped)}" if res.skipped else "")

        self.app.jobs.submit(work, "Merging", tool=self.id, on_done=done,
                             on_error=lambda m: error(self.frame, f"Could not merge:\n{m}"), serial_key="pdf_desk.build")

    def _temp_assembled(self, job) -> str:
        fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="pdfdesk_")
        os.close(fd)
        assemble(self.pieces(), tmp, AssembleOptions(page_size=self.get("page_size"), bookmarks=False,
                                                    libreoffice_cmd=self.settings.get("ocr.libreoffice_cmd")),
                 progress=lambda n, t, name: job.progress(n, t, name))
        return tmp

    def extract_selected(self) -> None:
        sel = self.grid.selected()
        if not sel:
            info_box(self.frame, "Select the pages to extract first.")
            return
        out = self._ask_save(sanitize_filename(f"{Path(sel[0].path).stem} - extract") + ".pdf")
        if not out:
            return
        pieces, opts = self.pieces(sel), self._options()
        opts.bookmarks, opts.page_numbers, opts.watermark = False, "", ""

        def work(job):
            return assemble(pieces, out, opts)

        self.app.jobs.submit(work, "Extracting pages", tool=self.id,
                             on_done=lambda r: self._finish(r.path, r.size, r.pages),
                             on_error=lambda m: error(self.frame, m), serial_key="pdf_desk.build")

    def _split(self, groups_fn) -> None:
        if not self.grid.items:
            info_box(self.frame, "Add some files first.")
            return
        folder = self.get("output_folder") or filedialog.askdirectory(title="Save the parts to", parent=self.frame)
        if not folder:
            return
        stem = sanitize_filename(Path(self.grid.items[0].path).stem)

        def work(job):
            tmp = self._temp_assembled(job)
            try:
                import pymupdf as fitz
            except ImportError:
                import fitz
            with fitz.open(tmp) as d:
                total = len(d)
            groups = groups_fn(total)
            return split_pdf(tmp, groups, folder, stem), tmp

        def done(res) -> None:
            files, tmp = res
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.log(f"Split into {len(files)} file(s) in {folder}", "ok")
            self.app.notify(f"Split into {len(files)} file(s)", "ok")
            if self.get("open_after"):
                open_path(folder)

        self.app.jobs.submit(work, "Splitting", tool=self.id, on_done=done,
                             on_error=lambda m: error(self.frame, f"Could not split:\n{m}"), serial_key="pdf_desk.build")

    def split_ranges(self) -> None:
        text = self.split_var.get()
        parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
        if not parts:
            return
        try:
            for p in parts:
                parse_page_ranges(p, 10 ** 6)
        except ValueError as exc:
            error(self.frame, str(exc))
            return
        self._split(lambda total: [g for g in (parse_page_ranges(p, total) for p in parts) if g])

    def split_every(self) -> None:
        try:
            n = max(1, int(self.every_var.get()))
        except ValueError:
            return
        self._split(lambda total: [list(range(i, min(i + n, total))) for i in range(0, total, n)])

    # ------------------------------------------------------------------ app integration
    def keys(self) -> dict:
        return {"<Control-o>": self.pick_files, "<Control-s>": self.merge_save}

    def commands(self) -> list[Command]:
        return [Command("pdf.add", "PDF Desk: add files", self.pick_files, self.title, "merge combine"),
                Command("pdf.merge", "PDF Desk: merge & save", self.merge_save, self.title, "combine pdf"),
                Command("pdf.rotate", "PDF Desk: rotate all pages", lambda: self._rotate_all(90), self.title)]

    def stats(self) -> dict:
        return {"Pages loaded": len(self.grid.items) if self.built else 0}
