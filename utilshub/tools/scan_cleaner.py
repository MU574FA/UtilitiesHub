"""Scan Cleaner: turn upright, straighten, trim borders, even out the lighting, drop blank pages.

Automatic first: add files (or drop them) and the tool *analyses* every page and tells you what it found.
"Clean all" writes the fixed copies.  Anything it got wrong can be corrected by hand: double-click a file to open
the page editor (turn, tilt slider, drag-to-crop, black & white ...).  The originals are never touched; the fixed
copies are new files, and the whole batch can be undone in one click.  The engine is core/scanclean.py.
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, ttk

from ..core import scanclean as sc
from ..core.fileops import batch_note, new_batch, resolve_target
from ..core.imaging import IMAGE_EXTS
from ..core.ocr import OcrConfig, find_tesseract
from ..core.schema import Opt
from ..core.system import open_path, reveal
from ..core.textutil import human_size
from ..ui.dnd import DropZone
from ..ui.widgets import DataTable, FolderEntry, Tooltip, confirm, error, info
from .base import Command, Tool

PRESET_CHOICES = [(k, v[0]) for k, v in sc.PRESETS.items()] + [("custom", "Custom")]
ON_ADD = [("nothing", "Do nothing"), ("analyse", "Analyse them (show what is wrong)"),
          ("clean", "Analyse and clean right away")]
DENOISE = [(0, "Off"), (1, "Light"), (2, "Strong (slow)")]
PRESET_KEYS = {"rotate", "deskew", "crop", "enhance", "denoise", "sharpen", "drop_blank"}
FILE_EXTS = sorted(IMAGE_EXTS | {".pdf"})


@dataclass
class FileEntry:
    path: str
    pages: int = 0
    size: int = 0
    analyses: list | None = None
    edits: dict = field(default_factory=dict)       # page index -> PageEdit
    status: str = ""
    out: str = ""


def summarise(analyses: list | None) -> str:
    """'turned ×2 · tilted ×3 · 1 blank' - what was found over all pages of a file."""
    if analyses is None:
        return ""
    n = lambda pred: sum(1 for a in analyses if pred(a))
    parts = []
    for label, count in (("turned", n(lambda a: a.rot)), ("tilted", n(lambda a: abs(a.skew) >= 0.5)),
                         ("dark borders", n(lambda a: a.dark_edges)), ("blank", n(lambda a: a.blank)),
                         ("has text", n(lambda a: a.native))):
        if count:
            parts.append(f"{label} ×{count}" if len(analyses) > 1 else label)
    return " · ".join(parts) if parts else "looks fine"


class ScanCleaner(Tool):
    id = "scan_cleaner"
    title = "Scan Cleaner"
    icon = "🧼"
    order = 41
    description = "Straighten, turn upright, trim and clean scans. Automatic first, then fix any page by hand."
    accepts = tuple(FILE_EXTS)

    @classmethod
    def options(cls) -> list[Opt]:
        p = "scan_cleaner."
        return [
            Opt(p + "preset", "document", "Preset", "choice", choices=PRESET_CHOICES, section="Quick start",
                help="Fills in the options below. Changing any of them switches this to 'Custom'."),
            Opt(p + "on_add", "analyse", "When I add files", "choice", choices=ON_ADD, section="Quick start"),
            Opt(p + "recursive", True, "Include sub-folders when I add a folder", "bool", section="Quick start"),
            Opt(p + "rotate", True, "Turn sideways / upside-down pages upright", "bool", section="What to fix",
                help="Needs Tesseract (Settings > OCR). Without it you can still turn pages by hand."),
            Opt(p + "deskew", True, "Straighten tilted pages", "bool", section="What to fix"),
            Opt(p + "crop", "auto", "Borders", "choice", choices=sc.CROP_MODES, section="What to fix"),
            Opt(p + "crop_margin", 0.015, "Paper kept around the content (share of the page)", "float", min=0.0, max=0.1,
                step=0.005, section="What to fix"),
            Opt(p + "enhance", "clean", "Look", "choice", choices=sc.ENHANCE_MODES, section="What to fix"),
            Opt(p + "denoise", 0, "Remove speckles", "choice", choices=DENOISE, section="What to fix"),
            Opt(p + "sharpen", False, "Sharpen", "bool", section="What to fix"),
            Opt(p + "brightness", 0, "Brightness", "int", min=-100, max=100, section="What to fix", advanced=True),
            Opt(p + "contrast", 0, "Contrast", "int", min=-100, max=100, section="What to fix", advanced=True),
            Opt(p + "drop_blank", True, "Leave out blank pages", "bool", section="Blank pages",
                help="Only when the file has more than one page. Check the list: a page with only a page number "
                     "or a small stamp can count as blank."),
            Opt(p + "blank_threshold", 0.001, "A page counts as blank below this share of ink", "float", min=0.0,
                max=0.05, step=0.0005, section="Blank pages", advanced=True),
            Opt(p + "keep_text_pages", True, "PDF pages that already have text are copied unchanged", "bool",
                section="PDF and quality"),
            Opt(p + "searchable", False, "Make the finished PDF searchable (OCR, slow)", "bool", section="PDF and quality"),
            Opt(p + "dpi", 200, "Resolution for PDF pages (dpi)", "int", min=72, max=600, section="PDF and quality"),
            Opt(p + "jpeg_quality", 90, "JPEG quality", "int", min=40, max=100, section="PDF and quality"),
            Opt(p + "output", "beside", "Save the clean copies", "choice", section="Output",
                choices=[("beside", "Next to the original"), ("folder", "In this folder")]),
            Opt(p + "folder", "", "Folder for the clean copies", "folder", section="Output"),
            Opt(p + "suffix", "_clean", "Added to the file name", "str", section="Output",
                help="The original is never replaced; an empty suffix is changed to _clean when needed."),
            Opt(p + "policy", "number", "If a file with that name exists", "choice", section="Output",
                choices=[("number", "Add a number"), ("overwrite", "Replace it"), ("skip", "Skip")]),
            Opt(p + "open_when_done", False, "Open the first clean copy when finished", "bool", section="Output"),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self._rows: dict[str, FileEntry] = {}          # table iid -> entry
        self._iid: dict[str, str] = {}                 # path -> iid
        self._applying = False
        self._warned_tesseract = False
        self.editor = None

    # ------------------------------------------------------------------ UI
    def build(self, parent: ttk.Frame) -> None:
        self.vars: dict[str, tk.Variable] = {}
        self.combos: dict[str, tuple[tk.StringVar, list]] = {}

        def var(key, kind=tk.StringVar):
            v = kind(value=self.get(key))
            v.trace_add("write", lambda *_a, k=key, vv=v: self._store(k, vv))
            self.vars[key] = v
            return v

        form = ttk.Frame(parent)
        form.pack(fill="x")
        r = ttk.Frame(form)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text="Preset").pack(side="left")
        self.preset_cb = self._combo(r, "preset", PRESET_CHOICES, 34, self._preset_chosen)
        self.preset_cb.pack(side="left", padx=6)
        ttk.Label(r, text="   When I add files").pack(side="left")
        self._combo(r, "on_add", ON_ADD, 32).pack(side="left", padx=6)
        r = ttk.Frame(form)
        r.pack(fill="x", pady=2)
        for text, key in (("Turn upright", "rotate"), ("Straighten", "deskew"), ("Leave out blank pages", "drop_blank"),
                          ("Sharpen", "sharpen")):
            ttk.Checkbutton(r, text=text, variable=var(key, tk.BooleanVar)).pack(side="left", padx=(0, 10))
        r = ttk.Frame(form)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text="Borders").pack(side="left")
        self._combo(r, "crop", sc.CROP_MODES, 26).pack(side="left", padx=(4, 12))
        ttk.Label(r, text="Look").pack(side="left")
        self._combo(r, "enhance", sc.ENHANCE_MODES, 30).pack(side="left", padx=(4, 12))
        ttk.Label(r, text="Speckles").pack(side="left")
        self._combo(r, "denoise", DENOISE, 12).pack(side="left", padx=4)
        r = ttk.Frame(form)
        r.pack(fill="x", pady=2)
        r.columnconfigure(3, weight=1)
        ttk.Label(r, text="Save").pack(side="left")
        self._combo(r, "output", [("beside", "Next to the original"), ("folder", "In this folder")], 20).pack(side="left", padx=4)
        FolderEntry(r, var("folder"), "dir", open_button=True).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(r, text="name +").pack(side="left")
        ttk.Entry(r, width=9, textvariable=var("suffix")).pack(side="left", padx=4)

        act = ttk.Frame(parent)
        act.pack(fill="x", pady=6)
        ttk.Button(act, text="Add files…", command=self.add_files).pack(side="left")
        ttk.Button(act, text="Add folder…", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(act, text="🔍 Analyse", command=self.analyse).pack(side="left", padx=(10, 0))
        ttk.Button(act, text="✨ Clean all", style="Accent.TButton", command=self.clean_all).pack(side="left", padx=4)
        ttk.Button(act, text="✏ Adjust…", command=self.adjust_selected).pack(side="left")
        ttk.Button(act, text="↩ Undo last batch", command=self.undo_batch).pack(side="left", padx=(10, 0))
        ttk.Button(act, text="Clear", command=self.clear).pack(side="right")
        ttk.Button(act, text="Remove", command=self.remove_selected).pack(side="right", padx=4)
        self.summary = ttk.Label(parent, style="Muted.TLabel", text="Add scans: images or PDFs.")
        self.summary.pack(anchor="w")

        self.table = DataTable(parent, [("file", "File", 240), ("pages", "Pages", 50, "e"), ("size", "Size", 70, "e"),
                                        ("found", "What was found", 260), ("status", "Status", 220),
                                        ("folder", "Folder", 260)],
                               searchable=True, on_double=lambda iid: self.adjust(iid), stretch="folder")
        self.table.pack(fill="both", expand=True, pady=(4, 6))
        self.table.tag_colors(ok="ok", fail="err", edited="info")
        m = self.table.menu
        m.add_separator()
        m.add_command(label="Adjust by hand…", command=self.adjust_selected)
        m.add_command(label="Reset my changes", command=self.reset_selected)
        m.add_command(label="Open original", command=lambda: self._each(lambda e: open_path(e.path)))
        m.add_command(label="Open clean copy", command=lambda: self._each(lambda e: e.out and open_path(e.out)))
        m.add_command(label="Show in folder", command=lambda: self._each(lambda e: reveal(e.out or e.path)))
        DropZone(parent, "Drop scans or folders here", self.accept_files,
                 filetypes=[("Scans", " ".join("*" + e for e in FILE_EXTS))], theme=self.app.theme, height=56).pack(fill="x")

    def _store(self, key: str, v: tk.Variable) -> None:
        try:
            self._set(key, v.get(), from_widget=True)
        except tk.TclError:
            pass

    def _set(self, key: str, value, from_widget: bool = False) -> None:
        self.put(key, value)
        if key in PRESET_KEYS and not self._applying and self.get("preset") != "custom":
            self.put("preset", "custom")
            self._sync_combo("preset")

    def _combo(self, parent, key: str, pairs: list, width: int, extra=None) -> ttk.Combobox:
        labels = [l for _v, l in pairs]
        cur = self.get(key)
        sv = tk.StringVar(value=next((l for v, l in pairs if v == cur), labels[0]))
        cb = ttk.Combobox(parent, textvariable=sv, values=labels, state="readonly", width=width)

        def chosen(_e=None):
            value = next(v for v, l in pairs if l == sv.get())
            self._set(key, value)
            if extra:
                extra(value)
        cb.bind("<<ComboboxSelected>>", chosen)
        self.combos[key] = (sv, pairs)
        return cb

    def _sync_combo(self, key: str) -> None:
        sv, pairs = self.combos[key]
        cur = self.get(key)
        sv.set(next((l for v, l in pairs if v == cur), pairs[0][1]))

    def _preset_chosen(self, preset_id: str) -> None:
        if preset_id not in sc.PRESETS:
            return
        self._applying = True
        try:
            for k, v in sc.PRESETS[preset_id][1].items():
                self.put(k, v)
                if k in self.vars:
                    self.vars[k].set(v)
                if k in self.combos:
                    self._sync_combo(k)
            self.put("preset", preset_id)
        finally:
            self._applying = False
        self.log(f"Preset: {sc.PRESETS[preset_id][0]}")

    # ------------------------------------------------------------------ settings -> engine
    def spec(self) -> sc.CleanSpec:
        return sc.CleanSpec.from_settings(self.settings, "scan_cleaner.")

    def _ocr_cfg(self, spec: sc.CleanSpec):
        """An OcrConfig for sideways detection, or None (switched off / Tesseract missing)."""
        if not spec.rotate:
            return None
        cfg = OcrConfig.from_settings(self.settings)
        if not find_tesseract(cfg.cmd):
            if not self._warned_tesseract:
                self._warned_tesseract = True
                self.log("Tesseract was not found, so sideways pages cannot be detected. You can still turn pages by "
                         "hand (Adjust…). Install it under Settings > OCR.", "warn")
            return None
        cfg.enabled = True
        return cfg

    # ------------------------------------------------------------------ the list
    def entries(self) -> list[FileEntry]:
        """The files in list order (the editor steps through these)."""
        return [self._rows[i] for i in self.table.tree.get_children() if i in self._rows]

    def _targets(self) -> list[FileEntry]:
        sel = [self._rows[i] for i in self.table.selection() if i in self._rows]
        return sel or list(self._rows.values())

    def _each(self, fn) -> None:
        for iid in self.table.selection():
            if iid in self._rows:
                fn(self._rows[iid])

    def _values(self, e: FileEntry) -> dict:
        p = Path(e.path)
        found = summarise(e.analyses)
        if e.edits:
            found = (found + " · " if found else "") + f"edited ×{len(e.edits)}"
        return {"file": p.name, "pages": e.pages or "", "size": human_size(e.size), "found": found,
                "status": e.status, "folder": str(p.parent)}

    def _tags(self, e: FileEntry) -> tuple:
        if e.status.startswith("✔"):
            return ("ok",)
        if e.status.startswith("✖"):
            return ("fail",)
        return ("edited",) if e.edits else ()

    def entry_changed(self, e: FileEntry) -> None:
        iid = self._iid.get(e.path)
        if iid and iid in self._rows and self.table.tree.exists(iid):
            self.table.update_row(iid, self._values(e), tags=self._tags(e))

    def _update_summary(self) -> None:
        n = len(self._rows)
        pages = sum(e.pages for e in self._rows.values())
        self.summary.configure(text=f"{n} file(s), {pages} page(s)" if n else "Add scans: images or PDFs.")

    # ------------------------------------------------------------------ adding files
    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Scans to clean", filetypes=[("Scans", " ".join("*" + e for e in FILE_EXTS)),
                                                                            ("All files", "*.*")])
        if paths:
            self.accept_files(list(paths))

    def add_folder(self) -> None:
        path = filedialog.askdirectory(title="Folder with scans")
        if path:
            self.accept_files([path])

    def accept_files(self, paths: list[str]) -> None:
        rec = bool(self.get("recursive"))
        found: list[str] = []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                it = p.rglob("*") if rec else p.glob("*")
                found += [str(f) for f in sorted(it) if f.is_file() and f.suffix.lower() in FILE_EXTS and f.name != ""]
            elif p.suffix.lower() in FILE_EXTS:
                found.append(str(p))
        found = [f for f in dict.fromkeys(found) if f not in self._iid]
        if not found:
            self.log("Nothing new to add (images and PDFs only).", "warn")
            return
        if any(f.lower().endswith(".pdf") for f in found) and sc.fitz is None:
            self.log("PDF files need PyMuPDF (pip install pymupdf); they were skipped.", "warn")
            found = [f for f in found if not f.lower().endswith(".pdf")]

        def work(job):
            out = []
            for n, f in enumerate(found, 1):
                if job.cancelled:
                    break
                job.progress(n, len(found), Path(f).name)
                try:
                    out.append((f, sc.page_total(f), Path(f).stat().st_size, ""))
                except Exception as exc:
                    out.append((f, 0, 0, str(exc)))
            return out

        self.app.jobs.submit(work, "Reading scans", self.id, on_done=self._added,
                             on_error=lambda m: error(self.frame, m), serial_key=self.id + ".read")

    def _added(self, items: list) -> None:
        new = []
        for path, pages, size, err in items:
            e = FileEntry(path, pages, size, status=f"✖ {err}" if err else "")
            iid = self.table.add(self._values(e), tags=self._tags(e))
            self._rows[iid], self._iid[path] = e, iid
            if not err:
                new.append(e)
        self._update_summary()
        mode = self.get("on_add")
        if new and mode == "analyse":
            self.analyse(new)
        elif new and mode == "clean":
            self.clean_all(new, ask=False)

    def remove_selected(self) -> None:
        for iid in list(self.table.selection()):
            e = self._rows.pop(iid, None)
            if e:
                self._iid.pop(e.path, None)
            self.table.tree.delete(iid)
        self._update_summary()

    def clear(self) -> None:
        self.table.clear()
        self._rows.clear()
        self._iid.clear()
        self._update_summary()

    def reset_selected(self) -> None:
        self._each(lambda e: (e.edits.clear(), self.entry_changed(e)))

    # ------------------------------------------------------------------ analysis
    def analyse(self, entries: list[FileEntry] | None = None) -> None:
        entries = entries or self._targets()
        entries = [e for e in entries if not e.status.startswith("✖")]
        if not entries:
            info(self.frame, "Add some scans first.")
            return
        spec, cfg = self.spec(), None
        cfg = self._ocr_cfg(spec)

        def work(job):
            for n, e in enumerate(entries, 1):
                if job.cancelled:
                    break
                job.progress(n - 1, len(entries), Path(e.path).name)
                try:
                    res, err = sc.analyse_file(e.path, spec, cfg, lambda: job.cancelled), ""
                except Exception as exc:
                    res, err = None, str(exc)
                self.app.bridge.call(self._analysed, e, res, err)
            return len(entries)

        self.summary.configure(text="Analysing…")
        self.app.jobs.submit(work, "Analysing scans", self.id, on_done=lambda n: self._update_summary(),
                             on_error=lambda m: error(self.frame, m), serial_key=self.id)

    def _analysed(self, e: FileEntry, res, err: str) -> None:
        if err:
            e.status = f"✖ {err}"
        else:
            e.analyses = res
            e.status = "analysed"
        self.entry_changed(e)

    def ensure_analysis(self, e: FileEntry, then) -> None:
        """For the editor: analyse one file (if not done yet), then call `then()`."""
        if e.analyses is not None:
            then()
            return
        spec = self.spec()
        cfg = self._ocr_cfg(spec)

        def work(job):
            return sc.analyse_file(e.path, spec, cfg, lambda: job.cancelled)

        def done(res):
            e.analyses, e.status = res, e.status or "analysed"
            self.entry_changed(e)
            then()

        self.app.jobs.submit(work, "Analysing scan", self.id, on_done=done,
                             on_error=lambda m: error(self.frame, m), serial_key=self.id + ".one")

    # ------------------------------------------------------------------ manual
    def adjust_selected(self) -> None:
        sel = [i for i in self.table.selection() if i in self._rows]
        if not sel:
            if len(self._rows) == 1:
                sel = list(self._rows)
            else:
                info(self.frame, "Select a file in the list first.")
                return
        self.adjust(sel[0])

    def adjust(self, iid: str) -> None:
        e = self._rows.get(iid)
        if e is None or e.status.startswith("✖"):
            return
        if self.editor is not None and self.editor.winfo_exists():
            self.editor.load_entry(e, 0)
            self.editor.lift()
            return
        from ..ui.cleaneditor import CleanEditor
        self.editor = CleanEditor(self, e)

    # ------------------------------------------------------------------ cleaning
    def _out_path(self, e: FileEntry) -> Path | None:
        src = Path(e.path)
        folder = Path(self.get("folder")) if self.get("output") == "folder" and self.get("folder") else src.parent
        suffix = self.get("suffix") or ""
        stem = src.stem + suffix
        ext = sc.output_suffix(src, e.pages if src.suffix.lower() not in (".pdf",) else 1)
        target = resolve_target(folder, stem, ext, self.get("policy") or "number")
        if target is not None and target.resolve() == src.resolve():
            target = resolve_target(folder, src.stem + "_clean", ext, "number")
        return target

    def clean_all(self, entries: list[FileEntry] | None = None, ask: bool = True) -> None:
        entries = entries if isinstance(entries, list) else None
        entries = [e for e in (entries or self._targets()) if not e.status.startswith("✖")]
        if not entries:
            info(self.frame, "Add some scans first.")
            return
        if ask and len(entries) > 20 and not confirm(self.frame, f"Clean {len(entries)} files? The originals stay as they are."):
            return
        spec = self.spec()
        cfg = self._ocr_cfg(spec) if (spec.rotate or spec.searchable) else None
        if spec.searchable and cfg is None:
            cfg = OcrConfig.from_settings(self.settings)
            if not find_tesseract(cfg.cmd):
                self.log("Searchable PDFs need Tesseract; continuing without.", "warn")
                spec.searchable, cfg = False, None
        batch, journal = new_batch(), self.app.journal

        def work(job):
            done = []
            for n, e in enumerate(entries, 1):
                if job.cancelled:
                    break
                job.progress(n - 1, len(entries), Path(e.path).name)
                try:
                    out = self._out_path(e)
                    if out is None:
                        self.app.bridge.call(self._cleaned, e, None, "skipped (a file with that name exists)")
                        continue
                    res = sc.clean_file(e.path, out, spec, e.analyses, e.edits, cfg, lambda: job.cancelled,
                                        lambda d, t, m: job.progress(n - 1, len(entries), m))
                    journal.record(self.id, "copy", e.path, out, batch_note(batch, "cleaned"))
                    done.append(out)
                    self.app.bridge.call(self._cleaned, e, res, "")
                except Exception as exc:
                    self.app.bridge.call(self._cleaned, e, None, str(exc))
            return done

        def finished(done):
            self.log(f"Cleaned {len(done)} of {len(entries)} file(s). Undo: ↩ Undo last batch.", "ok" if done else "warn")
            self._update_summary()
            if done and self.get("open_when_done"):
                open_path(done[0])

        self.summary.configure(text="Cleaning…")
        self.app.jobs.submit(work, "Cleaning scans", self.id, on_done=finished,
                             on_error=lambda m: error(self.frame, m), serial_key=self.id)

    def _cleaned(self, e: FileEntry, res, err: str) -> None:
        if err:
            e.status = f"✖ {err}" if not err.startswith("skipped") else err
        else:
            e.out = res.out
            saved = res.bytes_in - res.bytes_out
            bits = [f"{res.pages_in}→{res.pages_out} pages"] if res.pages_in != res.pages_out else []
            bits.append(f"{human_size(res.bytes_out)}" + (f" ({'-' if saved > 0 else '+'}{human_size(abs(saved))})" if saved else ""))
            e.status = "✔ " + Path(res.out).name + " · " + " · ".join(bits + res.notes)
        self.entry_changed(e)

    def undo_batch(self) -> None:
        j = self.app.journal
        batch = j.last_batch(self.id)
        if not batch:
            info(self.frame, "Nothing to undo.")
            return
        n, problems = j.undo_batch(batch, self.id)
        self.log(f"Removed {n} clean copy/copies", "ok" if n else "warn")
        if problems:
            error(self.frame, "\n".join(problems[:8]))
        for e in self._rows.values():
            if e.status.startswith("✔") and e.out and not Path(e.out).exists():
                e.out, e.status = "", "undone"
                self.entry_changed(e)

    # ------------------------------------------------------------------ app hooks
    def keys(self) -> dict:
        return {"<F5>": self.analyse, "<Control-Return>": self.clean_all, "<Control-z>": self.undo_batch}

    def commands(self) -> list[Command]:
        return [Command("scan.analyse", "Scan Cleaner: analyse", self.analyse, "Scan Cleaner"),
                Command("scan.clean", "Scan Cleaner: clean all", self.clean_all, "Scan Cleaner", "straighten deskew crop"),
                Command("scan.undo", "Scan Cleaner: undo last batch", self.undo_batch, "Scan Cleaner")]

    def stats(self) -> dict:
        return {"Files": len(self._rows)}
