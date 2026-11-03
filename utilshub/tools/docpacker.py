"""DocPacker: recognise the documents of one person, check them, fix pages by hand and build ONE PDF package."""
from __future__ import annotations

import datetime as dt
import os
import re
import tkinter as tk
from dataclasses import dataclass, field, replace
from pathlib import Path
from tkinter import filedialog, ttk

from ..core import arabic, ocr
from ..core.dates import parse_any_date
from ..core.doctypes import CheckContext, DocType, default_doctypes, run_check, score_doctype
from ..core.fileops import resolve_target
from ..core.pdfops import COMPRESSION_LEVELS, AssembleOptions, Piece, assemble, fitz
from ..core.readers import DocInfo, ReadOptions, read_document, supported_extensions
from ..core.schema import Opt
from ..core.system import open_path
from ..core.templates import render, render_folder, render_line, tidy_filename
from ..core.textutil import format_page_ranges, parse_page_ranges, sanitize_filename
from ..ui.dnd import DropZone
from ..ui.thumbs import ThumbGrid, ThumbItem
from ..ui.viewer import PageViewer
from ..ui.widgets import DataTable, FolderEntry, Tooltip, confirm, error, info as info_box, modal
from .base import Command, Tool

UNASSIGNED, EXCLUDED = "", "__skip__"
SEVERITY = {"fail": 4, "warn": 3, "manual": 2, "info": 1, "ok": 0, "manual_ok": 0, "skip": 0}
SYMBOL = {"ok": "✔", "manual_ok": "✔", "fail": "✘", "warn": "!", "manual": "☐", "info": "i", "skip": "–"}


@dataclass
class Entry:
    path: str
    info: DocInfo | None = None
    dtype: str = UNASSIGNED
    score: float = 0.0
    auto: bool = True                                   # assigned by recognition (False = by hand)
    results: list[tuple[str, str, str]] = field(default_factory=list)       # label, status, detail
    manual_ok: set[str] = field(default_factory=set)
    rot: dict[int, int] = field(default_factory=dict)   # manual + auto rotation per source page
    pages: list[int] | None = None                      # kept pages (None = all)
    busy: bool = True
    key: str = ""                                       # table id; parts of a split file share `path` but not `key`
    full: DocInfo | None = None                         # the whole file's info while it is split into parts

    def __post_init__(self) -> None:
        self.key = self.key or self.path

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def is_part(self) -> bool:
        return self.full is not None or self.key != self.path

    def worst(self) -> str:
        if self.info is not None and self.info.error:
            return "fail"
        worst = "ok"
        for _label, status, _d in self.results:
            if SEVERITY.get(status, 0) > SEVERITY[worst]:
                worst = status
        return worst


def custom_keys(labels: list[str]) -> dict[str, str]:
    """label -> placeholder name for the user's custom fields ('Passport no.' -> passport_no)."""
    out: dict[str, str] = {}
    for i, label in enumerate(labels, 1):
        key = re.sub(r"[^a-z0-9]+", "_", label.encode("ascii", "ignore").decode().lower()).strip("_")
        if not key or key[0].isdigit() or key in out.values():
            key = f"field{i}"
        out[label] = key
    return out


def page_info(info: DocInfo, pages: list[int]) -> DocInfo:
    """`info` restricted to some (0-based) pages: what recognition and checks see for one part of a file."""
    pick = lambda seq: [seq[p] for p in pages if p < len(seq)]          # noqa: E731
    texts = pick(info.page_texts)
    return replace(info, text="\n".join(texts) if info.page_texts else info.text, pages=len(pages), page_texts=texts,
                   page_meta=pick(info.page_meta), page_rot=pick(info.page_rot), hash="")


def detect_groups(types: list[DocType], info: DocInfo, threshold: float = 0.8) -> list[tuple[str, list[int]]]:
    """Recognise every page on its own and join neighbours of one type: [(type id, pages)].

    A page nobody recognises continues the previous document.  [] when the file is only one document.
    """
    if info.error or len(info.page_texts) < 2:
        return []
    labels: list[str] = []
    for i in range(len(info.page_texts)):
        one = page_info(info, [i])
        best, best_score = UNASSIGNED, 0.0
        for t in types:
            if t.enabled:
                s = score_doctype(t, one, "", threshold)
                if s >= t.min_score and s > best_score:
                    best, best_score = t.id, s
        labels.append(best or (labels[-1] if labels else UNASSIGNED))
    groups: list[tuple[str, list[int]]] = []
    for i, label in enumerate(labels):
        if groups and groups[-1][0] == label:
            groups[-1][1].append(i)
        else:
            groups.append((label, [i]))
    return groups if len({g[0] for g in groups}) > 1 else []


def compress_at_least(path: str, level: int, target_bytes: int = 0) -> int:
    """Compress `path` in place, starting at `level` and going stronger until `target_bytes` is met. Returns the size."""
    size = os.path.getsize(path)
    tmp = path + ".tmp.pdf"
    for lvl in range(max(0, min(level, len(COMPRESSION_LEVELS) - 1)), len(COMPRESSION_LEVELS)):
        q, _maxpx, dpi = COMPRESSION_LEVELS[lvl]
        with fitz.open(path) as doc:
            if dpi and hasattr(doc, "rewrite_images"):
                try:
                    doc.rewrite_images(dpi_threshold=dpi + 40, dpi_target=dpi, quality=q or 90)
                except Exception:
                    pass
            doc.save(tmp, garbage=4, deflate=True, clean=True)
        new = os.path.getsize(tmp)
        if new <= size:
            os.replace(tmp, path)
            size = new
        else:
            os.remove(tmp)
        if target_bytes <= 0 or size <= target_bytes:
            break
    return size


def draw_cover(page, vals: dict[str, str], cfg: dict, rows: list[tuple[str, str]], starts: dict[int, int],
               missing: list[str]) -> None:
    """Draw the cover page the user designed in the settings (title, free lines, checklist, missing documents).

    Arabic text is shaped and right-aligned; everything else starts at the left margin.
    """
    right, y = page.rect.width - 50, 70
    try:
        title = render(cfg.get("title") or "", vals).strip()
    except ValueError:
        title = vals.get("name", "")
    if title:
        arabic.draw_text(page, 50, y, title, 18, bold=True, right=right)
        y += 24
    for tpl in cfg.get("lines") or []:
        try:
            line = render_line(str(tpl), vals)
        except ValueError:
            continue
        if line:
            arabic.draw_text(page, 50, y, line, 11, color=(0.3, 0.3, 0.3), right=right)
            y += 18
    y += 16
    if cfg.get("checklist", True):
        for i, (tname, worst) in enumerate(rows):
            mark = {"fail": "[X]", "warn": "[!]", "manual": "[ ]"}.get(worst, "[v]")
            page.insert_text((50, y), mark, fontsize=11, fontname="helv")
            arabic.draw_text(page, 80, y, tname[:70], 11)
            if cfg.get("refs", True) and i in starts:
                page.insert_text((470, y), f"p. {starts[i]}", fontsize=11, fontname="helv")
            y += 18
            if y > 780:
                break
    if cfg.get("missing", True):
        for tname in missing:
            page.insert_text((50, y + 6), "[X]", fontsize=11, fontname="hebo", color=(0.75, 0, 0))
            arabic.draw_text(page, 80, y + 6, f"MISSING: {tname}"[:80], 11, bold=True, color=(0.75, 0, 0))
            y += 18


class DocPacker(Tool):
    id = "docpacker"
    title = "Doc Packer"
    icon = "📦"
    order = 20
    description = "Recognise a person's documents, check them, fix pages by hand and build one PDF package."
    accepts = ()

    @classmethod
    def options(cls) -> list[Opt]:
        return [
            Opt("docpacker.doctypes", default_doctypes(), "Document types", "record", editor="doctypes",
                section="Documents & checks",
                help="Each type has signals (how it is recognised - toggle, set necessity and weight) and checks."),
            Opt("docpacker.sample_terms", [], "Sample names / details that must NOT appear", "list",
                section="Documents & checks", help="Used by the 'no sample terms' check, e.g. names in template letters."),
            Opt("docpacker.fuzzy_names", True, "Tolerate typos / OCR errors when comparing names", "bool",
                section="Documents & checks"),
            Opt("docpacker.date_checks", True, "Run date based checks (age of letters, passport validity)", "bool",
                section="Documents & checks"),
            Opt("docpacker.recursive", True, "Look inside sub-folders when adding a folder", "bool", section="Adding files"),
            Opt("docpacker.include_unassigned", False, "Also put unrecognised files at the end of the package", "bool",
                section="Package"),
            Opt("docpacker.output_folder", "", "Save packages to (empty = next to the first file)", "folder", section="Package"),
            Opt("docpacker.output_subfolder", "", "Sub-folder for each package", "str", section="Package",
                help="Created inside the folder above. Any field, e.g. {surname} or {year}/{name}. Empty = none."),
            Opt("docpacker.output_name", "{name} - package", "Package file name", "str", section="Package",
                help="{name} {surname} {joining} {date} and your custom fields, e.g. {name} - {passport_no}"),
            Opt("docpacker.parts_folder", "", "Also save every document as its own PDF in folder", "str", section="Package",
                help="Relative to the folder above, e.g. {name}/documents. Empty = only the one package."),
            Opt("docpacker.parts_name", "{doctype}", "Name of those separate files", "str", section="Package",
                help="{doctype} {file} {n} plus the fields above"),
            Opt("docpacker.page_size", "a4", "Page size for images", "choice", section="Package",
                choices=[("a4", "A4"), ("letter", "Letter"), ("fit", "Same as the image")]),
            Opt("docpacker.bookmarks", True, "Add bookmarks (one per document)", "bool", section="Package"),
            Opt("docpacker.page_numbers", "Page {n} of {total}", "Page numbers (empty = none)", "str", section="Package",
                help="Arabic works too, e.g. صفحة {n} من {total}"),
            Opt("docpacker.watermark", "", "Watermark text (empty = none)", "str", section="Package"),
            Opt("docpacker.compression", 0, "Compression level (0 = none, 4 = strongest)", "int", min=0, max=4,
                section="Package"),
            Opt("docpacker.max_mb", 0.0, "Target size in MB (0 = no limit; compresses harder until it fits)", "float",
                min=0, max=500, section="Package"),
            Opt("docpacker.open_after", True, "Open the package when finished", "bool", section="Package"),
            Opt("docpacker.write_report", True, "Write a .txt report next to the package", "bool", section="Package"),
            Opt("docpacker.custom_fields", [], "Custom fields (one label per line)", "list", section="Custom fields",
                help="Extra boxes shown above the file list, e.g. Passport no. / Employer / Project. Each gets a "
                     "placeholder (shown in the box's tooltip) for the cover page and the file / folder names."),
            Opt("docpacker.auto_split", False, "Try to split a multi-page file into separate documents", "bool",
                section="One file, several documents",
                help="Off: you assign a file by hand (right-click → Split into documents). On: every page is "
                     "recognised on its own and neighbouring pages of one type become one document."),
            Opt("docpacker.cover", True, "Add a cover page", "bool", section="Cover page"),
            Opt("docpacker.cover_title", "Document package - {name}", "Title", "str", section="Cover page",
                help="{name} {surname} {joining} {date} {count} (documents) and your custom fields. Arabic is fine."),
            Opt("docpacker.cover_lines", ["{date}"], "Lines under the title (one per line)", "list", section="Cover page",
                help="e.g. Passport no: {passport_no} • A line whose fields are all empty is left out."),
            Opt("docpacker.cover_checklist", True, "Show the checklist of documents", "bool", section="Cover page"),
            Opt("docpacker.cover_page_refs", True, "Show the page number of each document", "bool", section="Cover page"),
            Opt("docpacker.cover_missing", True, "Show missing required documents", "bool", section="Cover page"),
            *cls.append_opts(auto=True),
        ]

    # ------------------------------------------------------------------ build
    def build(self, parent: ttk.Frame) -> None:
        self.entries: dict[str, Entry] = {}
        self._types: list[DocType] = []
        self._load_types()
        self.settings.subscribe("docpacker.doctypes", lambda k, v: self._types_changed())
        self.name_var, self.surname_var, self.join_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.status_var = tk.StringVar(value="Add the person's files (drop them anywhere in the window).")
        self.last_output = ""

        top = ttk.Frame(parent)
        top.pack(fill="x")
        for label, var, w in (("Full name", self.name_var, 26), ("Surname", self.surname_var, 16),
                              ("Joining date (YYYY-MM-DD)", self.join_var, 14)):
            ttk.Label(top, text=label).pack(side="left", padx=(0, 4))
            e = ttk.Entry(top, textvariable=var, width=w)
            e.pack(side="left", padx=(0, 12))
            var.trace_add("write", lambda *_: self._ctx_changed())
        ttk.Button(top, text="Add files…", command=self.pick_files).pack(side="right", padx=2)
        ttk.Button(top, text="Add folder…", command=self.pick_folder).pack(side="right", padx=2)
        ttk.Button(top, text="Clear", command=self.clear).pack(side="right", padx=2)
        ttk.Button(top, text="Re-check", command=self.recheck).pack(side="right", padx=2)

        self.custom_vars: dict[str, tk.StringVar] = {}
        self.custom_host = ttk.Frame(parent)
        self.custom_host.pack(fill="x", pady=(6, 0))
        self._build_custom()
        self.settings.subscribe("docpacker.custom_fields", lambda k, v: self._build_custom())

        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=8)
        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        self.table = DataTable(left, [("file", "File", 220), ("type", "Document", 190), ("score", "Match", 55, "e"),
                                      ("status", "Checks", 90), ("pages", "Pages", 50, "e")], height=8,
                               on_select=self._selected, on_double=lambda i: self.edit_pages(i))
        self.table.tag_colors(fail="err", warn="warn", ok="ok", manual="muted", busy="muted")
        self.table.pack(fill="both", expand=True)
        self.table.tree.bind("<Button-3>", self._menu, add="+")
        self.table.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.drop = DropZone(left, "Drop files or folders here", self.add_paths, theme=self.app.theme, height=54)
        self.drop.pack(fill="x", pady=(6, 0))
        chk = ttk.LabelFrame(left, text="Checks of the selected file (double-click a ☐ to confirm it yourself)", padding=4)
        chk.pack(fill="both", expand=True, pady=(6, 0))
        self.checks = ttk.Treeview(chk, columns=("s", "label", "detail"), show="headings", height=6)
        for k, h, w in (("s", "", 30), ("label", "Check", 250), ("detail", "Details", 260)):
            self.checks.heading(k, text=h)
            self.checks.column(k, width=w, stretch=(k == "detail"))
        self.checks.pack(fill="both", expand=True)
        self.checks.bind("<Double-1>", self._toggle_manual)
        for tag, col in (("fail", "err"), ("warn", "warn"), ("ok", "ok")):
            self.checks.tag_configure(tag, foreground=self.app.theme.p[col])

        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        ttk.Button(bar, text="Pages…", command=self.edit_pages, style="Tiny.TButton").pack(side="left")
        ttk.Button(bar, text="Split into documents…", style="Tiny.TButton",
                   command=lambda: self.table.selection() and self.split_dialog(self.table.selection()[0])).pack(side="left", padx=4)
        ttk.Label(bar, text="Rotate pages in the viewer; the package uses your rotation.", style="Muted.TLabel").pack(
            side="left", padx=8)
        self.viewer = PageViewer(right, self.app)
        self.viewer.pack(fill="both", expand=True)
        self.viewer.on_rotate = self._rotated
        self.app.theme.on_change(self._repaint_checks)

        self.summary = ttk.Label(parent, textvariable=self.status_var, wraplength=1000, justify="left")
        self.summary.pack(fill="x")
        bot = ttk.Frame(parent)
        bot.pack(fill="x", pady=(6, 0))
        ttk.Button(bot, text="Build package  (Ctrl+B)", style="Accent.TButton", command=self.build_package).pack(side="right")
        ttk.Button(bot, text="Report…", command=self.show_report).pack(side="right", padx=6)
        self.open_btn = ttk.Button(bot, text="Open last package", command=lambda: self.last_output and open_path(self.last_output))
        self.open_btn.pack(side="right")

    def _repaint_checks(self) -> None:
        if self.checks.winfo_exists():
            for tag, col in (("fail", "err"), ("warn", "warn"), ("ok", "ok")):
                self.checks.tag_configure(tag, foreground=self.app.theme.p[col])

    # ------------------------------------------------------------------ custom fields
    def _custom_labels(self) -> list[str]:
        return [str(x).strip() for x in self.get("custom_fields") or [] if str(x).strip()]

    def _build_custom(self) -> None:
        """One entry per custom field from the settings; typed values survive a rebuild."""
        if not hasattr(self, "custom_host") or not self.custom_host.winfo_exists():
            return
        old = {k: v.get() for k, v in self.custom_vars.items()}
        for w in self.custom_host.winfo_children():
            w.destroy()
        self.custom_vars = {}
        keys = custom_keys(self._custom_labels())
        for label, key in keys.items():
            ttk.Label(self.custom_host, text=label).pack(side="left", padx=(0, 4))
            var = tk.StringVar(value=old.get(label, ""))
            e = ttk.Entry(self.custom_host, textvariable=var, width=20)
            e.pack(side="left", padx=(0, 12))
            Tooltip(e, f"Placeholder: {{{key}}}")
            self.custom_vars[label] = var
        if keys:
            ttk.Button(self.custom_host, text="Fields…", style="Tiny.TButton",
                       command=lambda: self.app.open_settings(self.id)).pack(side="right")

    def values(self) -> dict[str, str]:
        """Placeholder values for the cover, the file name and the folders."""
        name = self.name_var.get().strip()
        out = {"name": name, "surname": self.surname_var.get().strip() or (name.split()[-1] if name else ""),
               "joining": self.join_var.get().strip(), "date": dt.date.today().isoformat()}
        for label, key in custom_keys(self._custom_labels()).items():
            var = self.custom_vars.get(label)
            out[key] = var.get().strip() if var else ""
        return out

    # ------------------------------------------------------------------ types & context
    def _load_types(self) -> None:
        out = []
        for d in self.get("doctypes") or []:
            try:
                out.append(DocType.from_dict(d))
            except Exception as exc:
                self.log(f"Skipped a broken document type: {exc}", "warn")
        self._types = out

    def _types_changed(self) -> None:
        self._load_types()
        if self.built:
            self.recheck(reassign=True)

    def _type(self, tid: str) -> DocType | None:
        return next((t for t in self._types if t.id == tid), None)

    def _context(self) -> CheckContext:
        name = self.name_var.get().strip()
        surname = self.surname_var.get().strip() or (name.split()[-1] if name else "")
        joining = parse_any_date(self.join_var.get()) if self.join_var.get().strip() else None
        return CheckContext(name=name, surname=surname, joining=joining, fuzzy_name=self.get("fuzzy_names"),
                            sample_terms=list(self.get("sample_terms") or []), datecheck=self.get("date_checks"),
                            mrz=self.settings.get_bool("ocr.mrz", True))

    def _ctx_changed(self) -> None:
        if getattr(self, "_after", None):
            self.frame.after_cancel(self._after)
        self._after = self.frame.after(500, lambda: self.recheck())

    # ------------------------------------------------------------------ adding files
    def accept_files(self, paths: list[str]) -> None:
        self.add_paths(paths)

    def pick_files(self) -> None:
        exts = " ".join(f"*{e}" for e in supported_extensions())
        paths = filedialog.askopenfilenames(title="Add documents", filetypes=[("Documents & images", exts), ("All", "*.*")])
        if paths:
            self.add_paths(list(paths))

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="Folder with the person's documents")
        if folder:
            self.add_paths([folder])

    def add_paths(self, paths: list[str]) -> None:
        supported = set(supported_extensions())
        files: list[str] = []
        for p in map(Path, paths):
            if p.is_dir():
                it = p.rglob("*") if self.get("recursive") else p.glob("*")
                files.extend(str(f) for f in sorted(it) if f.is_file() and f.suffix.lower() in supported)
            elif p.is_file():
                files.append(str(p))
        files = [f for f in dict.fromkeys(files) if f not in self.entries]
        if not files:
            return
        if not self.name_var.get().strip():                     # a folder named after the person is a good guess
            parent = Path(files[0]).parent.name
            if parent and parent.lower() not in ("documents", "downloads", "desktop", "scans"):
                self.name_var.set(parent)
        for f in files:
            self.entries[f] = Entry(f)
            self.table.add({"file": Path(f).name, "type": "reading…", "score": "", "status": "", "pages": ""}, iid=f,
                           tags=("busy",))
        cfg = ocr.OcrConfig.from_settings(self.settings)
        opts = ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=cfg.mrz, deskew=cfg.deskew, page_images=True)

        def work(job):
            out = []
            for n, f in enumerate(files):
                if job.cancelled:
                    break
                job.progress(n, len(files), Path(f).name)
                out.append((f, read_document(f, cfg, opts)))
            return out

        self.app.jobs.submit(work, f"Reading {len(files)} file(s)", tool=self.id, on_done=self._read_done,
                             on_error=lambda m: self.log(f"Reading failed: {m}", "error"), serial_key="docpacker.read")

    def _read_done(self, results) -> None:
        for path, info in results or []:
            e = self.entries.get(path)
            if e is None:
                continue
            e.info, e.busy = info, False
            if info.kind == "pdf" and info.page_rot:
                e.rot = {i: r % 360 for i, r in enumerate(info.page_rot) if r % 360}
            self._recognise(e)
        self._dedupe_notice()
        if self.get("auto_split"):
            thr = self.settings.get_int("match.fuzzy_threshold", 80) / 100
            for path, _info in results or []:
                e = self.entries.get(path)
                groups = detect_groups(self._types, e.full or e.info, thr) if e and e.info and e.auto else []
                if groups:
                    self.split_entry(e, [(None, pages) for _tid, pages in groups])
                    self.log(f"{e.name}: split into {len(groups)} documents", "info")
        self.recheck()

    def _dedupe_notice(self) -> None:
        seen: dict[str, str] = {}
        for e in self.entries.values():
            h = e.info.hash if e.info else ""
            if h and h in seen:
                self.log(f"{e.name} is an exact copy of {seen[h]}", "warn")
            elif h:
                seen[h] = e.name

    # ------------------------------------------------------------------ recognition & checks
    def _recognise(self, e: Entry) -> None:
        if e.info is None or e.info.error or not e.auto:
            return
        thr = self.settings.get_int("match.fuzzy_threshold", 80) / 100
        best, best_score = UNASSIGNED, 0.0
        taken = {x.dtype for x in self.entries.values() if x is not e and x.dtype not in (UNASSIGNED, EXCLUDED)}
        for t in self._types:
            if not t.enabled:
                continue
            s = score_doctype(t, e.info, e.name, thr)
            if s >= t.min_score and s > best_score + (0 if t.id not in taken else -0.05):
                best, best_score = t.id, s
        e.dtype, e.score = best, best_score

    def _evaluate(self, e: Entry, ctx: CheckContext) -> None:
        e.results = []
        t = self._type(e.dtype)
        if e.info is None or t is None:
            return
        for ch in t.checks:
            if not ch.enabled:
                continue
            status, detail = run_check(ch, e.info, ctx)
            if status == "manual" and ch.label in e.manual_ok:
                status = "manual_ok"
            e.results.append((ch.label, status, detail))

    def recheck(self, reassign: bool = False) -> None:
        if not self.built:
            return
        ctx = self._context()
        for e in self.entries.values():
            if e.busy:
                continue
            if reassign and e.auto:
                self._recognise(e)
            self._evaluate(e, ctx)
            self._refresh_row(e)
        self._refresh_summary()
        self._show_checks()

    def _refresh_row(self, e: Entry) -> None:
        t = self._type(e.dtype)
        if e.busy:
            return
        if e.info is not None and e.info.error:
            typ, status, tag = "⚠ unreadable", e.info.error[:60], "fail"
        elif e.dtype == EXCLUDED:
            typ, status, tag = "(excluded)", "", "busy"
        elif t is None:
            typ, status, tag = "? not recognised", "", "warn"
        else:
            worst = e.worst()
            typ = t.name + ("" if e.auto else "  ✎")
            status = {"ok": "✔ all good", "manual_ok": "✔ all good", "info": "✔ ok"}.get(worst, "")
            if not status:
                n = sum(1 for _l, s, _d in e.results if s == worst)
                status = {"fail": f"✘ {n} failed", "warn": f"! {n} warning(s)", "manual": f"☐ {n} to confirm"}.get(worst, worst)
            tag = {"fail": "fail", "warn": "warn", "manual": "manual"}.get(worst, "ok")
        pages = (len(e.pages) if e.pages is not None else (e.info.pages if e.info else 0)) or ""
        label = e.name + (f"  [p. {format_page_ranges(e.pages)}]" if e.is_part and e.pages else "")
        self.table.update_row(e.key, {"file": label, "type": typ, "score": f"{e.score:.0%}" if e.score and t else "",
                                      "status": status, "pages": pages}, tags=(tag,))

    def _missing(self) -> list[DocType]:
        have = {e.dtype for e in self.entries.values() if not e.busy}
        return [t for t in self._types if t.enabled and t.required and t.id not in have]

    def _problems(self) -> int:
        return sum(1 for e in self.entries.values() if e.dtype not in (UNASSIGNED, EXCLUDED) and e.worst() in ("fail",))

    def _refresh_summary(self) -> None:
        if not self.entries:
            self.status_var.set("Add the person's files (drop them anywhere in the window).")
            return
        miss = self._missing()
        warn = sum(1 for e in self.entries.values() if e.worst() in ("warn", "manual"))
        unk = sum(1 for e in self.entries.values() if e.dtype == UNASSIGNED and not e.busy)
        parts = [f"{len(self.entries)} file(s)"]
        parts.append("MISSING: " + ", ".join(t.name for t in miss) if miss else "all required documents present")
        if self._problems():
            parts.append(f"{self._problems()} file(s) with failed checks")
        if warn:
            parts.append(f"{warn} with warnings / things to confirm")
        if unk:
            parts.append(f"{unk} not recognised (right-click → assign)")
        self.status_var.set("  •  ".join(parts))

    # ------------------------------------------------------------------ selection, viewer, checks
    def _selected(self, ids: list[str]) -> None:
        self._show_checks()
        e = self.entries.get(ids[0]) if ids else None
        if e is None:
            return
        fix = e.info.fix if e.info and e.info.kind == "image" else None
        self.viewer.show(e.path, e.rot, fix)

    def _rotated(self, page: int, total: int) -> None:
        ids = self.table.selection()
        e = self.entries.get(ids[0]) if ids else None
        if e is not None:
            if total:
                e.rot[page] = total
            else:
                e.rot.pop(page, None)

    def _show_checks(self) -> None:
        self.checks.delete(*self.checks.get_children())
        ids = self.table.selection()
        e = self.entries.get(ids[0]) if ids else None
        if e is None:
            return
        if e.info is not None and e.info.error:
            self.checks.insert("", "end", values=("✘", "Could not read the file", e.info.error), tags=("fail",))
        for i, (label, status, detail) in enumerate(e.results):
            tag = {"fail": "fail", "warn": "warn", "ok": "ok", "manual_ok": "ok"}.get(status, "")
            self.checks.insert("", "end", iid=str(i), values=(SYMBOL.get(status, "?"), label, detail), tags=(tag,))

    def _toggle_manual(self, _e) -> None:
        sel, ids = self.checks.selection(), self.table.selection()
        if not sel or not ids or not sel[0].isdigit():
            return
        e = self.entries[ids[0]]
        label, status, _d = e.results[int(sel[0])]
        if status not in ("manual", "manual_ok"):
            return
        e.manual_ok ^= {label}
        self._evaluate(e, self._context())
        self._refresh_row(e)
        self._show_checks()
        self._refresh_summary()

    # ------------------------------------------------------------------ manual editing
    def _menu(self, ev) -> None:
        iid = self.table.tree.identify_row(ev.y)
        if iid and iid not in self.table.selection():
            self.table.tree.selection_set(iid)
        ids = self.table.selection()
        if not ids:
            return
        m = tk.Menu(self.table.tree, tearoff=False)
        sub = tk.Menu(m, tearoff=False)
        for t in self._types:
            sub.add_command(label=t.name, command=lambda tid=t.id: self.assign(ids, tid))
        sub.add_separator()
        sub.add_command(label="Exclude from the package", command=lambda: self.assign(ids, EXCLUDED))
        sub.add_command(label="Recognise automatically again", command=lambda: self.assign(ids, UNASSIGNED))
        m.add_cascade(label="Assign to…", menu=sub)
        m.add_command(label="Split into documents… (one file, several documents)", command=lambda: self.split_dialog(ids[0]))
        if self.entries[ids[0]].is_part:
            m.add_command(label="Undo split (back to one document)", command=lambda: self.unsplit(self.entries[ids[0]].path))
        m.add_command(label="Pages (rotate / reorder / delete)…", command=lambda: self.edit_pages(ids[0]))
        m.add_command(label="Open file", command=lambda: open_path(self.entries[ids[0]].path))
        m.add_separator()
        m.add_command(label="Remove from the list", command=self.remove_selected)
        m.tk_popup(ev.x_root, ev.y_root)

    def assign(self, ids: list[str], tid: str) -> None:
        for i in ids:
            e = self.entries.get(i)
            if e is None:
                continue
            if tid == UNASSIGNED:
                e.auto, e.dtype = True, UNASSIGNED
                self._recognise(e)
            else:
                e.auto, e.dtype, e.score = False, tid, 1.0 if tid != EXCLUDED else 0.0
            e.manual_ok.clear()
        self.recheck()

    def remove_selected(self) -> None:
        ids = self.table.selection()
        for i in ids:
            self.entries.pop(i, None)
        self.table.remove(ids)
        self.viewer.clear()
        self.recheck()

    def clear(self) -> None:
        if self.entries and not confirm(self.frame, "Remove all files from the list?"):
            return
        self.entries.clear()
        self.table.clear()
        self.viewer.clear()
        self.checks.delete(*self.checks.get_children())
        self.name_var.set("")
        self.surname_var.set("")
        self._refresh_summary()

    def edit_pages(self, iid: str | None = None) -> None:
        ids = self.table.selection()
        iid = iid or (ids[0] if ids else "")
        e = self.entries.get(iid)
        if e is None or e.busy:
            return
        try:
            total = self.app.renderer.page_count(e.path)
        except Exception as exc:
            error(self.frame, f"Cannot open the pages: {exc}")
            return
        top, body = modal(self.frame, f"Pages of {e.name}", 860, 600)
        ttk.Label(body, text="Click to select, drag to reorder, ⟳ rotate, 🗑 remove a page from the package.",
                  style="Muted.TLabel").pack(anchor="w")
        grid = ThumbGrid(body, self.app)
        grid.pack(fill="both", expand=True, pady=6)
        kept = e.pages if e.pages is not None else list(range(total))
        fix = e.info.fix if e.info and e.info.kind == "image" else None
        grid.set_items([ThumbItem(e.path, p, e.rot.get(p, 0), fix, label=f"page {p + 1}") for p in kept])

        def apply() -> None:
            order = [it.page for it in grid.items]
            e.pages = None if order == list(range(total)) else order
            e.rot = {it.page: it.rotation for it in grid.items if it.rotation}
            top.destroy()
            self._refresh_row(e)
            if self.table.selection() and self.table.selection()[0] == e.key:
                self._selected([e.key])

        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Button(row, text="Reset", command=lambda: grid.set_items(
            [ThumbItem(e.path, p, 0, fix, label=f"page {p + 1}") for p in range(total)])).pack(side="left")
        ttk.Button(row, text="Apply", style="Accent.TButton", command=apply).pack(side="right")
        ttk.Button(row, text="Cancel", command=top.destroy).pack(side="right", padx=6)

    # ------------------------------------------------------------------ one file, several documents
    def _drop_parts(self, path: str) -> None:
        keys = [k for k, x in self.entries.items() if x.path == path and k != path]
        for k in keys:
            del self.entries[k]
        self.table.remove(keys)

    def split_entry(self, e: Entry, groups: list[tuple[str | None, list[int]]]) -> None:
        """Make `e` (a whole file) one entry per group: (type id | None = recognise it, 0-based pages)."""
        base = e.full or e.info
        if base is None:
            return
        self._drop_parts(e.path)
        e.full = base
        for n, (tid, pages) in enumerate(groups):
            if n == 0:
                part = e
            else:
                part = Entry(e.path, key=f"{e.path}::{n + 1}", busy=False, rot=dict(e.rot))
                self.entries[part.key] = part
                self.table.add({"file": e.name, "type": "", "score": "", "status": "", "pages": ""}, iid=part.key)
            part.info, part.pages, part.manual_ok = page_info(base, pages), list(pages), set()
            if tid:
                part.auto, part.dtype, part.score = False, tid, 1.0 if tid != EXCLUDED else 0.0
            else:
                part.auto, part.dtype, part.score = True, UNASSIGNED, 0.0
                self._recognise(part)

    def unsplit(self, path: str) -> None:
        root = self.entries.get(path)
        if root is None or root.full is None:
            return
        self._drop_parts(path)
        root.info, root.full, root.pages, root.manual_ok = root.full, None, None, set()
        root.auto, root.dtype, root.score = True, UNASSIGNED, 0.0
        self._recognise(root)
        self.recheck()

    def split_dialog(self, iid: str) -> None:
        sel = self.entries.get(iid)
        root = self.entries.get(sel.path) if sel else None
        if root is None or root.busy or root.info is None or root.info.error:
            info_box(self.frame, "Select a file that was read successfully.")
            return
        base = root.full or root.info
        total = base.pages or len(base.page_texts) or 1
        thr = self.settings.get_int("match.fuzzy_threshold", 80) / 100
        AUTO, SKIP = "(recognise automatically)", "(exclude from the package)"
        by_name = {t.name: t.id for t in self._types}
        names = list(by_name) + [AUTO, SKIP]

        def label_of(tid: str | None) -> str:
            t = self._type(tid or "")
            return t.name if t else SKIP if tid == EXCLUDED else AUTO

        top, body = modal(self.frame, f"One file, several documents - {root.name}", 660, 460)
        ttk.Label(body, wraplength=620, style="Muted.TLabel",
                  text=f"This file has {total} page(s). Add one row per document: its type and its pages (e.g. 1-2, 5). "
                       "Pages left out of every row are not put in the package.").pack(anchor="w")
        host = ttk.Frame(body)
        host.pack(fill="both", expand=True, pady=8)
        rows: list[tuple[tk.StringVar, tk.StringVar, ttk.Frame]] = []

        def add_row(type_label: str = AUTO, pages: str = "") -> None:
            fr = ttk.Frame(host)
            fr.pack(fill="x", pady=2)
            tv, pv = tk.StringVar(value=type_label), tk.StringVar(value=pages)
            ttk.Combobox(fr, textvariable=tv, values=names, state="readonly", width=34).pack(side="left")
            ttk.Label(fr, text="pages").pack(side="left", padx=(10, 4))
            ttk.Entry(fr, textvariable=pv, width=16).pack(side="left")
            item = (tv, pv, fr)
            ttk.Button(fr, text="✕", width=3, style="Tiny.TButton",
                       command=lambda: (rows.remove(item), fr.destroy())).pack(side="left", padx=6)
            rows.append(item)

        def fill(groups: list[tuple[str | None, list[int]]]) -> None:
            for _tv, _pv, fr in rows:
                fr.destroy()
            rows.clear()
            for tid, pages in groups:
                add_row(label_of(tid), format_page_ranges(pages))

        def current() -> list[tuple[str | None, list[int]]]:
            parts = [x for x in self.entries.values() if x.path == root.path and x.pages is not None]
            return [(x.dtype, x.pages or []) for x in parts] if root.full is not None else [(root.dtype, list(range(total)))]

        def suggest() -> None:
            groups = detect_groups(self._types, base, thr)
            if groups:
                fill(groups)
            else:
                info_box(top, "The pages all look like one document. Add the rows yourself.")

        def free_pages() -> str:
            used: set[int] = set()
            for _tv, pv, _fr in rows:
                try:
                    used |= set(parse_page_ranges(pv.get(), total))
                except ValueError:
                    pass
            return format_page_ranges([p for p in range(total) if p not in used])

        def apply() -> None:
            groups: list[tuple[str | None, list[int]]] = []
            for tv, pv, _fr in rows:
                text = pv.get().strip()
                if not text:
                    continue
                try:
                    pages = parse_page_ranges(text, total)
                except ValueError as exc:
                    error(top, str(exc))
                    return
                if not pages:
                    error(top, f"'{text}' has no page in this file ({total} pages).")
                    return
                lab = tv.get()
                groups.append((EXCLUDED if lab == SKIP else by_name.get(lab), pages))
            if not groups:
                error(top, "Give at least one document its pages.")
                return
            top.destroy()
            if len(groups) == 1 and sorted(groups[0][1]) == list(range(total)):      # not a split after all
                self.unsplit(root.path)
                self.assign([root.key], groups[0][0] or UNASSIGNED)
            else:
                self.split_entry(root, groups)
                self.recheck()

        fill(current())
        bar = ttk.Frame(body)
        bar.pack(fill="x")
        ttk.Button(bar, text="Add document", command=lambda: add_row(AUTO, free_pages())).pack(side="left")
        ttk.Button(bar, text="Suggest from page contents", command=suggest).pack(side="left", padx=6)
        ttk.Button(bar, text="Apply", style="Accent.TButton", command=apply).pack(side="right")
        ttk.Button(bar, text="Cancel", command=top.destroy).pack(side="right", padx=6)

    # ------------------------------------------------------------------ package
    def ordered_pieces(self) -> list[tuple[Entry, Piece]]:
        rank = {t.id: i for i, t in enumerate(self._types)}
        use = []
        for e in self.entries.values():
            if e.busy or (e.info is not None and e.info.error) or e.dtype == EXCLUDED:
                continue
            if e.dtype == UNASSIGNED and not self.get("include_unassigned"):
                continue
            use.append(e)
        use.sort(key=lambda e: rank.get(e.dtype, 10 ** 6))
        out = []
        for e in use:
            t = self._type(e.dtype)
            fix = e.info.fix if e.info and e.info.kind == "image" else None
            out.append((e, Piece(e.path, pages=e.pages, page_rotate=dict(e.rot), fix=fix or None,
                                 title=t.name if t else e.name)))
        return out

    def _package_name(self, vals: dict[str, str] | None = None) -> str:
        vals = dict(vals or self.values())
        vals["name"] = vals["name"] or "package"
        try:
            stem = render(self.get("output_name") or "{name} - package", vals)
        except ValueError:
            stem = vals["name"]
        return tidy_filename(stem) or "package"

    def build_package(self) -> None:
        pairs = self.ordered_pieces()
        if not pairs:
            info_box(self.frame, "There is nothing to put in the package yet.")
            return
        issues = []
        if self._missing():
            issues.append("missing: " + ", ".join(t.name for t in self._missing()))
        if self._problems():
            issues.append(f"{self._problems()} file(s) failed their checks")
        if issues and not confirm(self.frame, "The package has problems (" + "; ".join(issues) + ").\n\nBuild it anyway?"):
            return
        vals = self.values()
        base = Path(self.get("output_folder") or Path(pairs[0][0].path).parent)
        folder = base / render_folder(self.get("output_subfolder") or "", {**vals, "name": vals["name"] or "package"})
        out = folder / f"{self._package_name(vals)}.pdf"
        if out.exists():
            n = 2
            while (folder / f"{out.stem} ({n}).pdf").exists():
                n += 1
            out = folder / f"{out.stem} ({n}).pdf"
        opts = AssembleOptions(page_size=self.get("page_size"), bookmarks=self.get("bookmarks"),
                               page_numbers=self.get("page_numbers"), watermark=self.get("watermark"),
                               cover_pages=1 if self.get("cover") else 0,
                               metadata={"title": self._package_name(vals), "author": vals["name"],
                                         "creator": "Utils Hub"},
                               libreoffice_cmd=self.settings.get("ocr.libreoffice_cmd"))
        pieces = [p for _e, p in pairs]
        names = [e for e, _p in pairs]
        level, target = self.get("compression"), int(self.get("max_mb") * 1024 * 1024)
        report = self.report_text()
        missing = [t.name for t in self._missing()]
        rows = [(self._type(e.dtype).name if self._type(e.dtype) else e.name, e.worst()) for e in names]
        write_report = self.get("write_report")
        cover_vals = {**vals, "name": vals["name"] or "unnamed", "count": str(len(rows))}
        cover_cfg = dict(title=self.get("cover_title"), lines=list(self.get("cover_lines") or []),
                         checklist=self.get("cover_checklist"), refs=self.get("cover_page_refs"),
                         missing=self.get("cover_missing"))
        parts_tpl, parts_name = self.get("parts_folder") or "", self.get("parts_name") or "{doctype}"
        part_jobs = [(i, rows[i][0], Path(e.path).stem, p) for i, (e, p) in enumerate(pairs)] if parts_tpl else []
        record = {**vals, "documents": "; ".join(r[0] for r in rows), "missing": "; ".join(missing),
                  "failed_files": self._problems()}

        def cover(doc, starts) -> None:
            if not opts.cover_pages or not len(doc):
                return
            draw_cover(doc[0], cover_vals, cover_cfg, rows, starts, missing)

        def work(job):
            folder.mkdir(parents=True, exist_ok=True)
            res = assemble(pieces, out, opts, before_save=cover,
                           progress=lambda n, t, name: job.progress(n, t, name))
            if level or target:
                res.size = compress_at_least(res.path, level, target)
            if write_report:
                Path(str(out)[:-4] + "_report.txt").write_text(report, encoding="utf-8")
            if part_jobs:
                pdir = base / render_folder(parts_tpl, {**vals, "name": vals["name"] or "package"})
                single = AssembleOptions(page_size=opts.page_size, bookmarks=False, libreoffice_cmd=opts.libreoffice_cmd)
                for i, doctype, stem, piece in part_jobs:
                    fname = tidy_filename(render(parts_name, {**vals, "doctype": doctype, "file": stem, "n": str(i + 1)}))
                    dest = resolve_target(pdir, fname or doctype, ".pdf", "number")
                    pdir.mkdir(parents=True, exist_ok=True)
                    assemble([piece], dest, single)
            return res

        self.app.jobs.submit(work, "Building package", tool=self.id, on_done=lambda res: self._built(res, record),
                             on_error=lambda m: error(self.frame, f"Could not build the package:\n{m}"),
                             serial_key="docpacker.build")

    def _built(self, res, record: dict | None = None) -> None:
        self.last_output = res.path
        msg = f"Package saved: {Path(res.path).name} ({res.pages} pages, {res.size / 1024 / 1024:.1f} MB)"
        if res.skipped:
            msg += f" - skipped: {'; '.join(res.skipped)}"
        self.log(msg, "warn" if res.skipped else "ok")
        self.app.notify(msg, "warn" if res.skipped else "ok")
        if record is not None:
            self.append_rows([{**record, "package": res.path, "pages": res.pages}], quiet=True)
        if self.get("open_after"):
            open_path(res.path)

    # ------------------------------------------------------------------ report
    def report_text(self) -> str:
        lines = [f"Package report - {self.name_var.get().strip() or 'unnamed'}  ({dt.date.today().isoformat()})", ""]
        for label in self._custom_labels():
            var = self.custom_vars.get(label)
            if var is not None and var.get().strip():
                lines.append(f"{label}: {var.get().strip()}")
        for t in self._missing():
            lines.append(f"MISSING  {t.name}")
        for e in self.entries.values():
            t = self._type(e.dtype)
            lines.append("")
            where = f" [p. {format_page_ranges(e.pages)}]" if e.is_part and e.pages else ""
            lines.append(f"{e.name}{where}  ->  {t.name if t else '(not recognised)' if e.dtype == UNASSIGNED else '(excluded)'}")
            if e.info is not None and e.info.error:
                lines.append(f"   ERROR: {e.info.error}")
            for label, status, detail in e.results:
                lines.append(f"   [{SYMBOL.get(status, '?')}] {label}" + (f" - {detail}" if detail else ""))
        return "\n".join(lines)

    def show_report(self) -> None:
        top, body = modal(self.frame, "Package report", 720, 560)
        txt = tk.Text(body, wrap="word")
        self.app.theme.style_text(txt)
        txt.insert("1.0", self.report_text())
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        ttk.Button(body, text="Copy", command=lambda: (top.clipboard_clear(), top.clipboard_append(self.report_text()))).pack(
            anchor="e", pady=6)

    # ------------------------------------------------------------------ app integration
    def commands(self) -> list[Command]:
        return [Command("docpacker.add", "Doc Packer: add files", self.pick_files, self.title, "documents package"),
                Command("docpacker.build", "Doc Packer: build package", self.build_package, self.title, "pdf merge"),
                Command("docpacker.recheck", "Doc Packer: re-check all files", self.recheck, self.title)]

    def keys(self) -> dict:
        return {"<Control-b>": self.build_package, "<Control-o>": self.pick_files, "<F5>": self.recheck}

    def stats(self) -> dict:
        return {"Files in package": len(self.entries) if self.built else 0}
