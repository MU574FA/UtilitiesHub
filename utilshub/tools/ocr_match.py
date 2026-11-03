"""OCR & Match: read text from scans / PDFs / photos, then find things in it with a regex or by similarity."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from ..core import ocr
from ..core.readers import DocInfo, ReadOptions, read_document, supported_extensions
from ..core.schema import Opt
from ..core.textmatch import TextHit, find_text, split_patterns
from ..ui.dnd import DropZone
from ..ui.widgets import DataTable, copy_to_clipboard, error, info, style_text
from .base import Command, Tool


class OcrMatch(Tool):
    id = "ocr_match"
    title = "OCR & Match"
    icon = "🔎"
    order = 45
    description = "Read text from scans, PDFs and photos (OCR), then find words or patterns: regex or similarity."

    @classmethod
    def options(cls) -> list[Opt]:
        p = cls.id + "."
        return [
            Opt(p + "mode", "regex", "Find by", "choice", section="Matching",
                choices=[("regex", "Regular expression"), ("similar", "Similarity (typos, word order, Arabic spelling)")]),
            Opt(p + "threshold", 80, "Similarity counts from (%)", "percent", min=40, max=100, section="Matching"),
            Opt(p + "ignore_case", True, "Ignore upper / lower case (regex)", "bool", section="Matching"),
            Opt(p + "patterns", "", "Last patterns / names", "text", advanced=True),
            *cls.append_opts(),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.docs: dict[str, DocInfo] = {}

    # ------------------------------------------------------------------ UI
    def build(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        ttk.Button(bar, text="Add files…  (Ctrl+O)", command=self.pick_files).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left", padx=6)
        ttk.Button(bar, text="Copy text", command=self.copy_text).pack(side="right")
        ttk.Button(bar, text="Save text…", command=self.save_text).pack(side="right", padx=6)
        self.status = ttk.Label(parent, style="Muted.TLabel", text="Add scans, PDFs or photos (or drop them anywhere).")
        self.status.pack(anchor="w", pady=(4, 0))

        paned = ttk.PanedWindow(parent, orient="vertical")
        paned.pack(fill="both", expand=True, pady=6)
        top = ttk.Frame(paned)
        paned.add(top, weight=3)
        self.files = DataTable(top, [("file", "File", 260), ("pages", "Pages", 55, "e"), ("chars", "Characters", 80, "e"),
                                     ("how", "Read by", 90), ("note", "Note", 240)], height=4, stretch="note",
                               on_select=lambda ids: self._show_text())
        self.files.pack(fill="x")
        self.files.tag_colors(err="err")
        tf = ttk.Frame(top)
        tf.pack(fill="both", expand=True, pady=(6, 0))
        self.text = tk.Text(tf, wrap="word", height=10)
        sb = ttk.Scrollbar(tf, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        style_text(self.frame, self.text)

        low = ttk.Frame(paned)
        paned.add(low, weight=3)
        box = ttk.LabelFrame(low, text="Find in the text", padding=8)
        box.pack(fill="x")
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.mode = tk.StringVar(value=self.get("mode"))
        self.th = tk.IntVar(value=self.get("threshold"))
        self.icase = tk.BooleanVar(value=self.get("ignore_case"))
        self.all_files = tk.BooleanVar(value=True)
        for label, v in (("Regex", "regex"), ("Similarity", "similar")):
            ttk.Radiobutton(row, text=label, value=v, variable=self.mode,
                            command=lambda: self.put("mode", self.mode.get())).pack(side="left", padx=(0, 8))
        ttk.Label(row, text="Similarity %").pack(side="left", padx=(8, 2))
        ttk.Spinbox(row, from_=40, to=100, width=4, textvariable=self.th).pack(side="left")
        ttk.Checkbutton(row, text="Ignore case", variable=self.icase).pack(side="left", padx=10)
        ttk.Checkbutton(row, text="Search all files", variable=self.all_files).pack(side="left")
        ttk.Button(row, text="Find  (F5)", style="Accent.TButton", command=self.find).pack(side="right")
        ttk.Label(box, style="Muted.TLabel",
                  text="One pattern per line. Regex example: \\b\\d{9}\\b   •   Similarity: type the names / words to look for.").pack(
            anchor="w", pady=(4, 2))
        self.patterns = tk.Text(box, height=3, wrap="none", undo=True)
        self.patterns.pack(fill="x")
        style_text(self.frame, self.patterns)
        self.patterns.insert("1.0", self.get("patterns"))

        rbar = ttk.Frame(low)
        rbar.pack(fill="x", pady=(8, 2))
        self.summary = ttk.Label(rbar, style="Muted.TLabel")
        self.summary.pack(side="left")
        ttk.Button(rbar, text="Add to file…", command=self.add_to_file).pack(side="right")
        ttk.Button(rbar, text="Copy results", command=lambda: self.results.copy_selected(all_rows=True)).pack(side="right", padx=6)
        self.results = DataTable(low, [("file", "File", 200), ("pattern", "Looked for", 170), ("found", "Found", 220),
                                       ("score", "Score", 60, "e"), ("page", "Page", 50, "e"), ("line", "Line", 50, "e"),
                                       ("context", "Line text", 340)], height=8, stretch="context", searchable=True)
        self.results.pack(fill="both", expand=True)
        DropZone(parent, "Drop scans, PDFs or photos here", self.accept_files, theme=self.app.theme, height=44).pack(fill="x")
        for iid in self.docs:
            self._add_row(iid)

    # ------------------------------------------------------------------ reading
    def accept_files(self, paths: list[str]) -> None:
        self.add_paths(paths)

    def pick_files(self) -> None:
        exts = " ".join(f"*{e}" for e in supported_extensions())
        paths = filedialog.askopenfilenames(title="Add files", filetypes=[("Documents & images", exts), ("All", "*.*")])
        if paths:
            self.add_paths(list(paths))

    def add_paths(self, paths: list[str]) -> None:
        supported = set(supported_extensions())
        files: list[str] = []
        for p in map(Path, paths):
            found = [f for f in sorted(p.rglob("*")) if f.is_file() and f.suffix.lower() in supported] if p.is_dir() else [p]
            files.extend(str(f) for f in found if f.is_file())
        files = [f for f in dict.fromkeys(files) if f not in self.docs]
        if not files:
            return
        cfg = ocr.OcrConfig.from_settings(self.settings)
        opts = ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=False, deskew=cfg.deskew)

        def work(job):
            out = []
            for n, f in enumerate(files):
                if job.cancelled:
                    break
                job.progress(n, len(files), Path(f).name)
                out.append((f, read_document(f, cfg, opts)))
            return out

        def done(results):
            for path, doc in results or []:
                self.docs[path] = doc
                if self.built:
                    self._add_row(path)
            self.status.configure(text=f"{len(self.docs)} file(s) read.")
            if self.files.tree.get_children() and not self.files.selection():
                first = self.files.tree.get_children()[0]
                self.files.tree.selection_set(first)

        self.app.jobs.submit(work, f"Reading {len(files)} file(s)", tool=self.id, on_done=done,
                             on_error=lambda m: error(self.frame, f"Reading failed: {m}"), serial_key="ocr_match.read")

    def _add_row(self, path: str) -> None:
        d = self.docs[path]
        self.files.add({"file": Path(path).name, "pages": d.pages, "chars": len(d.text.strip()),
                        "how": "OCR" if d.used_ocr else "text layer", "note": d.error or ("" if d.text.strip() else "no text found")},
                       iid=path, tags=("err",) if d.error else ())

    def clear(self) -> None:
        self.docs.clear()
        self.files.clear()
        self.results.clear()
        self.text.delete("1.0", "end")
        self.summary.configure(text="")

    # ------------------------------------------------------------------ text
    def _selected_doc(self) -> DocInfo | None:
        ids = self.files.selection()
        return self.docs.get(ids[0]) if ids else None

    def _show_text(self) -> None:
        d = self._selected_doc()
        self.text.delete("1.0", "end")
        if d is None:
            return
        pages = d.page_texts or [d.text]
        self.text.insert("1.0", "\n".join((f"──── page {i} ────\n" if len(pages) > 1 else "") + t.strip() + "\n"
                                          for i, t in enumerate(pages, 1)) or "(no text could be read)")

    def copy_text(self) -> None:
        copy_to_clipboard(self.frame, self.text.get("1.0", "end-1c"))
        self.app.notify("Text copied")

    def save_text(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")])
        if path:
            Path(path).write_text(self.text.get("1.0", "end-1c"), encoding="utf-8")

    # ------------------------------------------------------------------ matching
    def find(self) -> None:
        if not self.docs:
            info(self.frame, "Add some files first.")
            return
        raw = self.patterns.get("1.0", "end-1c")
        pats = split_patterns(raw)
        if not pats:
            info(self.frame, "Type what to look for (one pattern per line).")
            return
        self.put("patterns", raw)
        sel = self._selected_doc()
        docs = list(self.docs.values()) if self.all_files.get() or sel is None else [sel]
        mode, thr, icase = self.mode.get(), self.th.get() / 100, self.icase.get()

        def work(job):
            out: list[tuple[str, TextHit]] = []
            for n, d in enumerate(docs):
                job.progress(n, len(docs), Path(d.path).name)
                for h in find_text(d.page_texts or [d.text], pats, mode, thr, icase):
                    out.append((Path(d.path).name, h))
            return out

        def done(hits):
            self.results.clear()
            for name, h in hits:
                self.results.add({"file": name, "pattern": h.pattern, "found": h.text,
                                  "score": f"{h.score * 100:.0f}%" if mode == "similar" else "", "page": h.page,
                                  "line": h.line, "context": h.context.strip()})
            self.summary.configure(text=f"{len(hits)} match(es) in {len(docs)} file(s)")

        self.app.jobs.submit(work, "Searching", tool=self.id, on_done=done,
                             on_error=lambda m: error(self.frame, str(m)), serial_key="ocr_match.find")

    def add_to_file(self) -> None:
        rows = self.results.to_rows(all_rows=True)
        if len(rows) < 2:
            info(self.frame, "Find something first.")
            return
        headers = ["File", "Looked for", "Found", "Score", "Page", "Line", "Line text"]
        self.append_rows([dict(zip(headers, r)) for r in rows[1:]], ask=True)

    # ------------------------------------------------------------------ app integration
    def keys(self) -> dict:
        return {"<F5>": self.find, "<Control-o>": self.pick_files}

    def commands(self) -> list[Command]:
        return [Command("ocr.add", "OCR & Match: add files…", self.pick_files, self.title, "scan text read"),
                Command("ocr.find", "OCR & Match: find in the text", self.find, self.title, "regex similarity search")]

    def stats(self) -> dict:
        return {"Files read": len(self.docs)}
