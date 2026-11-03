"""CV Sorter: watch a folder, read every CV, extract the fields, let you review (or auto-confirm) and file it.

Also exports the reusable `FieldForm` (editable extracted values + click-to-assign from the text) used by the
Screenshot tool, and `expand_paths`.
"""
from __future__ import annotations

import itertools
import re
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable

from ..core import ocr
from ..core.fields import ExtractContext, FieldEngine, FieldSpec, default_cv_fields, join_values, parse_specs
from ..core.fileops import transfer
from ..core.history import HistoryLog
from ..core.pdfops import rotate_image_file, rotate_pdf_pages
from ..core.readers import ReadOptions, read_document, supported_extensions
from ..core.registry import NameRegistry
from ..core.schema import Opt
from ..core.settings import data_dir
from ..core.system import open_path, reveal
from ..core.templates import base_values, render_clipboard, render_filename, render_folder, render_link
from ..core.textutil import parse_ext_list
from ..core.watcher import FolderWatcher
from ..ui.dnd import DropZone
from ..ui.viewer import PageViewer
from ..ui.widgets import DataTable, FolderEntry, ScrollFrame, Tooltip, confirm, copy_to_clipboard, error, info, style_text
from .base import Command, Tool

DEFAULT_EXTS = "pdf, docx, doc, txt, jpg, jpeg, png"
_ids = itertools.count(1)


def default_cv_csv_columns() -> list[dict]:
    return [
        {"header": "Candidate Name", "value": "{name}", "enabled": True},
        {"header": "Phone", "value": "{phone}", "enabled": True},
        {"header": "Email", "value": "{email}", "enabled": True},
        {"header": "Job Title", "value": "{job_title}", "enabled": True},
        {"header": "Company", "value": "{company}", "enabled": True},
        {"header": "Original File", "value": "{original_file}", "enabled": True},
        {"header": "Saved File", "value": "{saved_as}", "enabled": True},
        {"header": "Folder", "value": "{folder}", "enabled": True},
    ]


def expand_paths(paths, extensions) -> list[Path]:
    """Files as given + files inside dropped folders (recursive), filtered by extension."""
    exts = {e.lower() for e in extensions}
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in exts and not f.name.startswith(("~$", ".")))
        elif p.is_file():
            out.append(p)
    return out


# --------------------------------------------------------------------------- reusable form
class FieldForm(ttk.Frame):
    """One editable row per field. `candidates` fill the drop-down; `assign(key, text)` sets a value."""

    def __init__(self, parent: tk.Misc, app, specs: list[FieldSpec], values: dict[str, list[str]],
                 on_change: Callable[[], None] | None = None, hide_unsearched: bool = False) -> None:
        super().__init__(parent)
        self.app, self.specs, self.on_change = app, [s for s in specs if s.enabled], on_change
        self.vars: dict[str, tk.StringVar] = {}
        self.boxes: dict[str, ttk.Combobox] = {}
        self.labels: dict[str, ttk.Label] = {}
        self.columnconfigure(1, weight=1)
        for r, s in enumerate(self.specs):
            mark = " *" if s.level == "required" else ""
            lbl = ttk.Label(self, text=s.label + mark)
            lbl.grid(row=r, column=0, sticky="w", padx=(0, 8), pady=3)
            var = tk.StringVar(value=join_values(values.get(s.key, []), s))
            box = ttk.Combobox(self, textvariable=var, values=values.get(s.key, []))
            box.grid(row=r, column=1, sticky="ew", pady=3)
            var.trace_add("write", lambda *_: self._changed())
            self.vars[s.key], self.boxes[s.key], self.labels[s.key] = var, box, lbl
            if s.note:
                Tooltip(lbl, s.note)
        self._paint()

    def _changed(self) -> None:
        self._paint()
        if self.on_change:
            self.on_change()

    def _paint(self) -> None:
        p = self.app.theme.p
        for s in self.specs:
            empty = not self.vars[s.key].get().strip()
            bad = empty and s.level in ("required", "recommended") and s.kind != "manual"
            self.labels[s.key].configure(foreground=(p["err"] if s.level == "required" else p["warn"]) if bad else p["fg"])

    def get(self) -> dict[str, str]:
        return {k: v.get().strip() for k, v in self.vars.items()}

    def fields(self) -> dict[str, list[str]]:
        out = {}
        for s in self.specs:
            v = self.vars[s.key].get().strip()
            sep = s.join.strip()
            multi = s.multiple and s.kind in ("keywords", "regex", "label", "builtin") and sep and sep in v
            out[s.key] = ([x.strip() for x in v.split(sep) if x.strip()] if multi else [v]) if v else []
        return out

    def assign(self, key: str, text: str, append: bool = False) -> None:
        var = self.vars.get(key)
        if var is None:
            return
        text = " ".join(text.split())
        spec = next(s for s in self.specs if s.key == key)
        var.set((var.get() + spec.join + text) if append and var.get().strip() else text)

    def labels_by_key(self) -> dict[str, str]:
        return {s.key: s.label for s in self.specs}


def assign_menu(widget: tk.Misc, text_widget: tk.Text, form_getter: Callable[[], FieldForm]) -> None:
    """Right-click on selected text -> 'Assign selection to <field>' (and 'Add to <field>')."""
    def popup(e):
        try:
            sel = text_widget.get("sel.first", "sel.last")
        except tk.TclError:
            return
        form = form_getter()
        if not sel.strip() or form is None:
            return
        m = tk.Menu(text_widget, tearoff=False)
        for s in form.specs:
            m.add_command(label=f"Set as {s.label}", command=lambda k=s.key: form.assign(k, sel))
        m.add_separator()
        for s in form.specs:
            m.add_command(label=f"Add to {s.label}", command=lambda k=s.key: form.assign(k, sel, True))
        m.add_separator()
        m.add_command(label="Copy", command=lambda: copy_to_clipboard(text_widget, sel))
        m.tk_popup(e.x_root, e.y_root)
    text_widget.bind("<Button-3>", popup)
    text_widget.bind("<Button-2>", popup)


# --------------------------------------------------------------------------- data
@dataclass
class Item:
    path: Path
    text: str = ""
    candidates: dict[str, list[str]] = field(default_factory=dict)
    status: str = "review"            # review | saved | skipped | error
    problems: list[str] = field(default_factory=list)
    registry: str = ""
    saved_as: str = ""
    rotations: dict[int, int] = field(default_factory=dict)
    fix: dict | None = None
    error: str = ""
    iid: str = ""
    auto: bool = False


# --------------------------------------------------------------------------- review dialog
class ReviewDialog(tk.Toplevel):
    def __init__(self, tool: "CvSorter", item: Item) -> None:
        super().__init__(tool.app.root)
        self.tool, self.app = tool, tool.app
        self.configure(background=self.app.theme.p["bg"])
        self.geometry("1240x780")
        self.minsize(900, 560)
        self.transient(self.app.root)
        self.item: Item | None = None
        self.form: FieldForm | None = None
        pos = tool.get("preview_position") if self.app.settings.get_bool("preview.enabled", True) else "none"
        self.paned = ttk.PanedWindow(self, orient="vertical" if pos == "bottom" else "horizontal")
        self.paned.pack(fill="both", expand=True)
        self.left = ttk.Frame(self.paned, padding=10)
        self.paned.add(self.left, weight=1)
        self.viewer = None
        if pos != "none":
            right = ttk.Notebook(self.paned)
            self.paned.add(right, weight=2)
            self.viewer = PageViewer(right, self.app, max_pages=self.app.settings.get_int("preview.max_pages", 8))
            right.add(self.viewer, text="Preview")
            self.viewer.on_rotate = lambda p, d: self._rotated()
            if self.app.settings.get_bool("preview.show_text_tab", True):
                tf = ttk.Frame(right)
                right.add(tf, text="Extracted text  (select → right-click to assign)")
                self.text = tk.Text(tf, wrap="word")
                sb = ttk.Scrollbar(tf, command=self.text.yview)
                self.text.configure(yscrollcommand=sb.set)
                sb.pack(side="right", fill="y")
                self.text.pack(fill="both", expand=True)
                style_text(self, self.text)
                assign_menu(self, self.text, lambda: self.form)
        else:
            self.text = None
        if not hasattr(self, "text"):
            self.text = None
        self.title_lbl = ttk.Label(self.left, style="H2.TLabel", wraplength=420)
        self.title_lbl.pack(anchor="w")
        self.sub_lbl = ttk.Label(self.left, style="Muted.TLabel", wraplength=420)
        self.sub_lbl.pack(anchor="w", pady=(0, 8))
        self.form_host = ttk.Frame(self.left)
        self.form_host.pack(fill="x")
        self.reg_lbl = ttk.Label(self.left, wraplength=420, justify="left")
        self.reg_lbl.pack(anchor="w", pady=(8, 0), fill="x")
        self.name_lbl = ttk.Label(self.left, style="Muted.TLabel", wraplength=420)
        self.name_lbl.pack(anchor="w", pady=(4, 0))
        self.warn_lbl = ttk.Label(self.left, wraplength=420, justify="left")
        self.warn_lbl.pack(anchor="w", pady=(4, 0))
        bar = ttk.Frame(self.left)
        bar.pack(side="bottom", fill="x", pady=(10, 0))
        row1 = ttk.Frame(bar)
        row1.pack(fill="x")
        ttk.Button(row1, text="Save & next  (Enter)", style="Accent.TButton", command=lambda: self.save(True)).pack(side="left")
        ttk.Button(row1, text="Save  (Ctrl+S)", command=lambda: self.save(False)).pack(side="left", padx=4)
        ttk.Button(row1, text="Skip  (Esc)", command=self.skip).pack(side="left")
        ttk.Button(row1, text="Close  (Ctrl+W)", command=self.destroy).pack(side="right")
        row2 = ttk.Frame(bar)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Button(row2, text="◀", width=3, command=lambda: self.step(-1)).pack(side="left")
        ttk.Button(row2, text="▶", width=3, command=lambda: self.step(1)).pack(side="left", padx=2)
        ttk.Button(row2, text="Open file", command=lambda: open_path(self.item.path)).pack(side="left", padx=4)
        mb = ttk.Menubutton(row2, text="Copy ▾")
        self.copy_menu = tk.Menu(mb, tearoff=False)
        mb["menu"] = self.copy_menu
        mb.pack(side="left")
        ttk.Label(bar, style="Muted.TLabel", text="Alt+←/→ previous / next without saving").pack(anchor="w", pady=(6, 0))
        self.bind("<Return>", lambda e: self._key(e, lambda: self.save(True)))
        self.bind("<KP_Enter>", lambda e: self._key(e, lambda: self.save(True)))
        self.bind("<Control-Return>", lambda e: self._key(e, lambda: self.save(True), force=True))
        self.bind("<Control-s>", lambda e: self._key(e, lambda: self.save(False), force=True))
        self.bind("<Escape>", lambda e: self._key(e, self.skip, force=True))
        self.bind("<Control-w>", lambda e: self.destroy())
        self.bind("<Alt-Left>", lambda e: self.step(-1))
        self.bind("<Alt-Right>", lambda e: self.step(1))
        self.show(item)

    def _key(self, event, action, force: bool = False) -> str | None:
        """Enter / Esc act on the CV, except inside the multi-line text where Enter must stay a new line."""
        if isinstance(event.widget, tk.Text) and not force:
            return None
        if not self.item or self.item.status != "review":
            return None
        action()
        return "break"

    # -- navigation
    def step(self, d: int) -> None:
        pend = [i for i in self.tool.items.values() if i.status == "review"]
        if not pend:
            self.destroy()
            return
        cur = pend.index(self.item) if self.item in pend else -1
        self.show(pend[(cur + d) % len(pend)] if cur >= 0 or d > 0 else pend[0])

    def show(self, item: Item) -> None:
        self.item = item
        for w in self.form_host.winfo_children():
            w.destroy()
        self.form = FieldForm(self.form_host, self.app, self.tool.specs(), item.candidates, self._refresh)
        self.form.pack(fill="x")
        self.title_lbl.configure(text=item.path.name)
        pend = sum(1 for i in self.tool.items.values() if i.status == "review")
        self.sub_lbl.configure(text=f"{pend} waiting for review • {item.path.parent}")
        if self.viewer:
            self.viewer.show(item.path, item.rotations, item.fix)
        if self.text is not None:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", item.text or "(no text could be read)")
        self.app.last_text = item.text
        self.copy_menu.delete(0, "end")
        for rec in self.tool.get("clipboard") or []:
            self.copy_menu.add_command(label=rec["name"], command=lambda r=rec: self.copy(r))
        for rec in self.tool.get("links") or []:
            self.copy_menu.add_command(label=f"Link: {rec['name']}", command=lambda r=rec: self.copy(r, link=True))
        self._refresh()

    def copy(self, rec: dict, link: bool = False) -> None:
        fields = self.form.fields()
        stem = self.tool.stem_for(self.item, fields)
        text = (render_link(rec["template"], fields, self.item.path, stem + self.item.path.suffix) if link
                else render_clipboard(rec["template"], fields, self.item.path, stem))
        copy_to_clipboard(self, text)
        self.app.notify(f"Copied: {rec['name']}")

    def _rotated(self) -> None:
        if self.item and self.viewer:
            self.item.rotations = dict(self.viewer.rotations)

    def _refresh(self) -> None:
        if not (self.item and self.form):
            return
        p = self.app.theme.p
        fields = self.form.fields()
        name = (fields.get("name") or [""])[0]
        chk = self.tool.registry().check(name) if name else None
        if chk and chk.status in ("exact", "similar"):
            best = chk.best
            self.reg_lbl.configure(foreground=p["err"] if chk.status == "exact" else p["warn"],
                                   text=("⚠ Already in the list: " if chk.status == "exact" else "⚠ Looks similar to: ")
                                   + ", ".join(f"{m.name} ({m.score:.0%})" for m in chk.matches[:3]))
        elif chk and chk.status == "new":
            self.reg_lbl.configure(foreground=p["ok"], text="✓ New name (not in the list)")
        else:
            self.reg_lbl.configure(text="")
        self.name_lbl.configure(text=f"Will be saved as: {self.tool.stem_for(self.item, fields)}{self.item.path.suffix.lower()}")
        miss = [s.label for s in self.tool.specs() if s.level == "required" and s.searched and not fields.get(s.key)]
        self.warn_lbl.configure(foreground=p["err"], text=("Missing required: " + ", ".join(miss)) if miss else "")

    def save(self, nxt: bool) -> None:
        if not self.item:
            return
        fields = self.form.fields()
        miss = [s.label for s in self.tool.specs() if s.level == "required" and s.searched and not fields.get(s.key)]
        if miss and not confirm(self, "Required fields are empty: " + ", ".join(miss) + "\nSave anyway?"):
            return
        self._rotated()
        done = self.item
        if self.tool.save(done, fields):
            self.after_action(nxt)

    def skip(self) -> None:
        self.tool.skip(self.item)
        self.after_action(True)

    def after_action(self, nxt: bool) -> None:
        pend = [i for i in self.tool.items.values() if i.status == "review"]
        if nxt and pend:
            self.show(pend[0])
        elif not pend:
            self.destroy()
        else:
            self.show(self.item)


# --------------------------------------------------------------------------- the tool
class CvSorter(Tool):
    id = "cv_sorter"
    title = "CV Sorter"
    icon = "📄"
    order = 10
    description = "Watch a folder of CVs, extract name / contact / job fields, review and file them automatically."
    accepts = ()

    @classmethod
    def options(cls) -> list[Opt]:
        p = cls.id + "."
        return [
            Opt(p + "watch_folder", "", "Folder to watch / process", "folder", section="Folders"),
            Opt(p + "dest_folder", "", "Save sorted CVs to", "str", section="Folders",
                help="Base path, optionally with {placeholders}: e.g. C:/Archive/{interviewer}/{name}  "
                     "— empty = a 'Sorted' folder inside the watched folder"),
            Opt(p + "extensions", DEFAULT_EXTS, "File types", "str", section="Folders", help="e.g. pdf, docx, jpg"),
            Opt(p + "recursive", False, "Include sub-folders", "bool", section="Folders"),
            Opt(p + "watch", False, "Watch the folder automatically", "bool", section="Automation",
                help="New files are read as soon as they finish copying"),
            Opt(p + "process_existing", False, "Also process files already in the folder when watching starts", "bool",
                section="Automation"),
            Opt(p + "auto_mode", "clean", "Auto-confirm", "choice", section="Automation",
                choices=[("off", "Never - I review every CV"), ("clean", "Only when all required fields are found and the name is new"),
                         ("always", "Always (save whatever was found)")]),
            Opt(p + "popup", True, "Open the review window when a CV needs attention", "bool", section="Automation"),
            Opt(p + "action", "move", "Filing", "choice", choices=[("move", "Move the file"), ("copy", "Copy (keep the original)")],
                section="Saving"),
            Opt(p + "conflict", "number", "If the name already exists", "choice", section="Saving",
                choices=[("number", "Add a number: name (2)"), ("overwrite", "Overwrite"), ("skip", "Skip")]),
            Opt(p + "name_template", "{name}", "New file name", "str", section="Saving",
                help="Placeholders: {name} {email} {phone} {job_title} {company} {date} {original} ... plus filters {name|upper}"),
            Opt(p + "skip_folder", "", "Move skipped CVs to (optional)", "folder", section="Saving"),
            Opt(p + "folder_template", "", "Sub-folder inside the destination", "str", section="Saving",
                help="Any field, e.g. {job_title} or {company}/{year}. Folders are created as needed; empty = none."),
            Opt(p + "copy_format", "", "After saving, copy to the clipboard using format", "str", section="Saving",
                help="Name of one of the clipboard formats below (empty = off)"),
            Opt(p + "preview_position", "right", "Preview position", "choice", section="Review window",
                choices=[("right", "Beside the form"), ("bottom", "Below the form"), ("none", "No preview")]),
            Opt(p + "fields", default_cv_fields(), "Fields to extract", "record", editor="fields", section="Fields",
                help="Toggle, reorder, set how necessary each field is, add your own keyword / regex fields."),
            Opt(p + "clipboard", [{"name": "Spreadsheet row", "template": r"{name}\t{email}\t{phone}\t{job_title}\t{company}"}],
                "Clipboard formats", "record", editor="clipboard", section="Copy formats"),
            Opt(p + "links", [], "Link templates", "record", editor="links", section="Copy formats"),
            Opt(p + "append_file", "", "Add results to this CSV file", "append", section="Output file",
                filetypes=[("CSV (opens in Excel)", "*.csv")],
                help="Pick a new name to create the file, or an existing .csv to add rows to it. Saved automatically when filing."),
            Opt(p + "csv_include_timestamp", False, "Include automatic timestamp column", "bool", section="Output file",
                help="Add an automatic 'timestamp' column as the very first column in the CSV file."),
            Opt(p + "csv_columns", default_cv_csv_columns(), "CSV columns & arrangement", "record", editor="csv_columns", section="Output file",
                help="Customize which columns get saved to the CSV file and their exact arrangement/order. Use ▲ and ▼ to reorder; uncheck to exclude."),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.items: dict[str, Item] = {}
        self._seen: set[str] = set()
        self.watcher: FolderWatcher | None = None
        self.dialog: ReviewDialog | None = None
        self.history = HistoryLog(data_dir() / "cv_history.csv")
        self._registry: NameRegistry | None = None
        self._reg_key: tuple = ()
        self.counts = {"saved": 0, "skipped": 0, "auto": 0}
        app.settings.subscribe(self.id + ".watch", lambda k, v: self._watch_setting(v))

    # ------------------------------------------------------------------ helpers
    def specs(self) -> list[FieldSpec]:
        return parse_specs(self.get("fields") or [])

    def extensions(self) -> list[str]:
        exts = parse_ext_list(self.get("extensions")) or parse_ext_list(DEFAULT_EXTS)
        return [e for e in exts if e in supported_extensions()] or exts

    def registry(self) -> NameRegistry:
        s = self.settings
        key = (s.get("match.names_list_path"), s.get_int("match.name_match_threshold", 85))
        if self._registry is None or key != self._reg_key:
            self._registry, self._reg_key = NameRegistry(key[0], key[1] / 100), key
        return self._registry

    def dest(self, fields: dict[str, list[str]] | None = None) -> Path | None:
        d = self.get("dest_folder")
        if d:
            # Resolve any {placeholders} in the path using the current CV's fields.
            # Placeholders that contain path separators (/) are allowed so that
            # e.g.  C:/Archive/{interviewer}/{name}  creates nested sub-folders.
            if re.search(r"\{[A-Za-z_]\w*", d) and fields is not None:
                vals = base_values(fields)
                from ..core.templates import render as _render, FILTERS
                # Render; sanitize each segment but preserve the separators.
                def _safe_seg(m: re.Match) -> str:
                    key = m.group(1)
                    filters = [f.strip() for f in m.group(2).split("|") if f.strip()]
                    text = str(vals.get(key, ""))
                    for fn_name in filters:
                        fn = FILTERS.get(fn_name)
                        if fn:
                            text = fn(text)
                    from ..core.textutil import sanitize_filename
                    return sanitize_filename(text) if text else ""
                import re as _re
                _PH = _re.compile(r"\{\s*([A-Za-z_][\w]*)\s*((?:\|\s*[A-Za-z0-9_]+\s*)*)\}")
                resolved = _PH.sub(_safe_seg, d)
                # Collapse any double separators left by empty placeholders
                resolved = re.sub(r"[/\\]{2,}", "/", resolved).rstrip("/\\")
                return Path(resolved) if resolved else None
            return Path(d)
        w = self.get("watch_folder")
        return Path(w) / "Sorted" if w else None

    def stem_for(self, item: Item, fields: dict[str, list[str]]) -> str:
        return render_filename(self.get("name_template") or "{name}", fields, item.path, fallback=item.path.stem)

    # ------------------------------------------------------------------ pipeline
    def submit(self, path: Path | str, force: bool = False) -> None:
        path = Path(path)
        key = str(path.resolve()) if path.exists() else str(path)
        if not force and key in self._seen:
            return
        self._seen.add(key)
        s = self.settings
        cfg = ocr.OcrConfig.from_settings(s)
        opts = ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=False, deskew=cfg.deskew)
        specs = self.specs()
        ctx = ExtractContext.from_settings(s)

        def work(job):
            info_ = read_document(path, cfg, opts)
            engine = FieldEngine(specs, ctx)
            values = engine.extract(info_.text, path) if info_.text.strip() else {}
            return info_, engine, values

        def done(res):
            info_, engine, values = res
            it = Item(path, info_.text, values, fix=info_.fix or None, error=info_.error)
            self._ready(it, [s_.label for s_ in engine.missing(values)])

        def fail(msg):
            it = Item(path, status="error", error=str(msg))
            self._register(it)
            self.log(f"{path.name}: {msg}", "error")

        self.app.jobs.submit(work, f"Reading {path.name}", self.id, done, fail, serial_key=self.id)

    def _register(self, it: Item) -> None:
        it.iid = str(next(_ids))
        self.items[it.iid] = it
        if self.built:
            self._add_row(it)

    def _problems(self, it: Item, missing: list[str]) -> None:
        it.problems = []
        if it.error:
            it.problems.append(it.error)
        if not it.text.strip():
            it.problems.append("no text found")
        if missing:
            it.problems.append("missing: " + ", ".join(missing))
        name = (it.candidates.get("name") or [""])[0]
        if name and self.registry().enabled:
            chk = self.registry().check(name)
            it.registry = chk.status
            if chk.status in ("exact", "similar"):
                it.problems.append(f"name {'already listed' if chk.status == 'exact' else 'similar to ' + chk.matches[0].name}")

    def _ready(self, it: Item, missing: list[str]) -> None:
        self._problems(it, missing)
        self._register(it)
        mode = self.get("auto_mode")
        if mode == "always" and it.text.strip() or mode == "clean" and not it.problems:
            fields = {k: v[:1] for k, v in it.candidates.items() if v}
            it.auto = True
            if self.save(it, fields):
                self.counts["auto"] += 1
                return
        self.log(f"{it.path.name}: needs review ({'; '.join(it.problems) or 'review mode'})", "warn")
        self.app.notify(f"{it.path.name} needs your review", "warn")
        self._refresh_status()
        if self.get("popup") and (self.dialog is None or not self.dialog.winfo_exists()):
            self.open_review(it)

    def save(self, it: Item, fields: dict[str, list[str]]) -> bool:
        dest = self.dest(fields)
        if dest is None:
            error(self.app.root, "Choose a folder to watch or a destination folder first.")
            return False
        try:
            if it.rotations:
                if it.path.suffix.lower() == ".pdf":
                    rotate_pdf_pages(it.path, it.rotations, it.path)
                elif it.rotations.get(0):
                    rotate_image_file(it.path, it.rotations[0])
            stem = self.stem_for(it, fields)
            sub = render_folder(self.get("folder_template") or "", base_values(fields, it.path))
            target = transfer(it.path, dest / sub, stem, self.get("action"), self.get("conflict"), self.app.journal, self.id,
                              note=(fields.get("name") or [""])[0])
        except Exception as exc:
            self.log(f"{it.path.name}: could not save - {exc}", "error")
            error(self.app.root, f"Could not save {it.path.name}:\n{exc}")
            return False
        if target is None:
            it.status, it.saved_as = "skipped", ""
            self.log(f"{it.path.name}: skipped (name already exists)", "warn")
        else:
            it.status, it.saved_as = "saved", target.name
            row = {k: ", ".join(v) for k, v in fields.items()}
            row["auto"] = "yes" if it.auto else ""
            self.history.append(row, "saved", it.path.name, target.name)
            csv_record = self._build_csv_record(it, target, sub, fields, row)
            use_ts = self.get("csv_include_timestamp") if self.get("csv_columns") else True
            self.append_rows([csv_record], quiet=True, timestamp=bool(use_ts))
            name = (fields.get("name") or [""])[0]
            if name and self.get_registry_auto():
                self.registry().add(name)
            fmt = self.get("copy_format")
            rec = next((r for r in self.get("clipboard") or [] if r["name"] == fmt), None) if fmt else None
            if rec and not it.auto:
                copy_to_clipboard(self.app.root, render_clipboard(rec["template"], fields, it.path, stem))
            self.counts["saved"] += 1
            self.log(f"{it.path.name} → {target.name}", "ok")
        self._update_row(it)
        self._refresh_status()
        return True

    def _build_csv_record(self, it: Item, target: Path, sub: Path, fields: dict[str, list[str]], default_row: dict) -> dict:
        columns_spec = self.get("csv_columns")
        if not columns_spec:
            return {"original_file": it.path.name, "saved_as": target.name, "folder": str(sub), **default_row}

        vals = base_values(fields, it.path, filename=target.name)
        vals["original_file"] = it.path.name
        vals["saved_as"] = target.name
        vals["folder"] = str(sub)
        vals["auto"] = "yes" if it.auto else ""
        vals["status"] = it.status
        vals["timestamp"] = datetime.now().isoformat(timespec="seconds")

        from ..core.templates import render
        record = {}
        for col in columns_spec:
            if not col.get("enabled", True):
                continue
            header = str(col.get("header", "")).strip()
            if not header:
                continue
            tmpl = str(col.get("value", "")).strip()
            if not ("{" in tmpl and "}" in tmpl) and tmpl in vals:
                val = vals[tmpl]
            else:
                try:
                    val = render(tmpl, vals)
                except Exception:
                    val = vals.get(tmpl, "")
            record[header] = val

        return record if record else {"original_file": it.path.name, "saved_as": target.name, "folder": str(sub), **default_row}

    def open_csv(self) -> None:
        path = (self.get("append_file") or "").strip()
        if not path:
            self.app.notify("No CSV file configured. Choose one in ⚙ Settings → Output file.", "warn")
            return
        p = Path(path)
        if not p.exists():
            self.app.notify(f"CSV file does not exist yet ({p.name}). It will be created when a CV is saved.", "warn")
            return
        open_path(p)

    def get_registry_auto(self) -> bool:
        return self.settings.get_bool("match.names_auto_add", True) and self.registry().enabled

    def skip(self, it: Item) -> None:
        it.status = "skipped"
        d = self.get("skip_folder")
        if d:
            try:
                transfer(it.path, Path(d), None, "move", "number", self.app.journal, self.id, "skipped")
            except Exception as exc:
                self.log(f"{it.path.name}: {exc}", "error")
        self.counts["skipped"] += 1
        self._update_row(it)
        self._refresh_status()

    def undo_last(self) -> None:
        entry = self.app.journal.last_undoable(self.id)
        if not entry:
            self.app.notify("Nothing to undo", "warn")
            return
        ok, why = self.app.journal.can_undo(entry)
        if not ok:
            error(self.app.root, why)
            return
        msg = self.app.journal.undo(entry)
        for it in self.items.values():
            if it.saved_as and it.saved_as == Path(entry.dest).name:
                it.status, it.saved_as = "review", ""
                it.path = Path(entry.src)
                self._seen.discard(str(it.path))
                self._update_row(it)
        self.log(f"Undone: {msg}", "info")
        self._refresh_status()

    # ------------------------------------------------------------------ input
    def accept_files(self, paths: list[str]) -> None:
        files = expand_paths(paths, self.extensions())
        if not files:
            self.app.notify("No supported files", "warn")
            return
        if not self.get("dest_folder") and not self.get("watch_folder"):
            first = files[0].parent
            self.put("watch_folder", str(first))
        for f in files:
            self.submit(f)

    def process_folder(self) -> None:
        folder = self.get("watch_folder")
        if not folder or not Path(folder).is_dir():
            error(self.app.root, "Choose the folder first.")
            return
        files = expand_paths([folder], self.extensions()) if self.get("recursive") else \
            [f for f in sorted(Path(folder).iterdir()) if f.is_file() and f.suffix.lower() in self.extensions()
             and not f.name.startswith(("~$", "."))]
        dest = self.dest()
        files = [f for f in files if dest is None or dest not in f.parents]
        for f in files:
            self.submit(f)
        self.log(f"Queued {len(files)} file(s)", "info")

    # ------------------------------------------------------------------ automation
    def can_automate(self) -> bool:
        return bool(self.get("watch") and self.get("watch_folder") and Path(self.get("watch_folder")).is_dir())

    def auto_running(self) -> bool:
        return self.watcher is not None

    def start_auto(self) -> None:
        if self.watcher or not self.get("watch_folder"):
            return
        folder = Path(self.get("watch_folder"))
        if not folder.is_dir():
            self.log("Watch folder does not exist", "error")
            return
        dest = self.dest()

        def on_file(p: Path) -> None:
            if dest is not None and dest in p.parents:
                return
            self.app.bridge.call(self.submit, p)

        self.watcher = FolderWatcher(folder, self.extensions(), self.settings.get_float("auto.stable_seconds", 1.5), on_file,
                                     self.get("process_existing"), self.get("recursive"))
        self.watcher.start()
        self.log(f"Watching {folder}", "info")
        self._sync_watch_var()

    def stop_auto(self) -> None:
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
            self.log("Stopped watching", "info")
        self._sync_watch_var()

    def _watch_setting(self, value) -> None:
        if value and not self.watcher:
            self.start_auto()
        elif not value and self.watcher:
            self.stop_auto()

    def auto_summary(self) -> str:
        return f"{self.counts['saved']} saved, {sum(1 for i in self.items.values() if i.status == 'review')} to review"

    def stats(self) -> dict:
        return {"Saved": self.counts["saved"], "To review": sum(1 for i in self.items.values() if i.status == "review"),
                "Auto-confirmed": self.counts["auto"]}

    def on_close(self) -> None:
        self.stop_auto()

    def commands(self) -> list[Command]:
        return [Command("cv.process", "CV Sorter: process the folder now", self.process_folder, self.title, "run scan"),
                Command("cv.review", "CV Sorter: review next CV", self.review_next, self.title),
                Command("cv.undo", "CV Sorter: undo last filing", self.undo_last, self.title),
                Command("cv.add", "CV Sorter: add files…", self.add_files, self.title),
                Command("cv.watch", "CV Sorter: start / stop watching", self.toggle_watch, self.title),
                Command("cv.open_csv", "CV Sorter: open results CSV file", self.open_csv, self.title)]

    def keys(self) -> dict:
        return {"<Control-o>": self.add_files, "<F5>": self.process_folder, "<Control-r>": self.review_next}

    # ------------------------------------------------------------------ UI
    def build(self, parent: ttk.Frame) -> None:
        self.vars: dict[str, tk.Variable] = {}
        top = ttk.Frame(parent)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)

        # Row 0 — watch / source folder (plain folder entry)
        ttk.Label(top, text="Watch / source folder").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        watch_var = tk.StringVar(value=self.get("watch_folder"))
        watch_var.trace_add("write", lambda *_: self.put("watch_folder", watch_var.get().strip()))
        FolderEntry(top, watch_var, "dir", width=60, open_button=True).grid(row=0, column=1, sticky="ew", pady=2)
        self.vars["watch_folder"] = watch_var

        # Row 1 — destination folder with optional {placeholder} template support
        ttk.Label(top, text="Save sorted CVs to").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=2)
        dest_var = tk.StringVar(value=self.get("dest_folder"))
        dest_var.trace_add("write", lambda *_: self.put("dest_folder", dest_var.get().strip()))
        dest_frame = ttk.Frame(top)
        dest_frame.columnconfigure(0, weight=1)
        dest_entry = ttk.Entry(dest_frame, textvariable=dest_var, width=52)
        dest_entry.grid(row=0, column=0, sticky="ew")

        def _browse_dest():
            cur = dest_var.get().strip()
            # Strip any trailing template segments ({…}) to find an existing base path
            base = re.sub(r"[\\/]\{[^}]*\}.*$", "", cur).strip(" \\/")
            init = base if base and Path(base).exists() else None
            chosen = filedialog.askdirectory(title="Choose base destination folder", initialdir=init,
                                             parent=parent.winfo_toplevel())
            if chosen:
                # Keep any template suffix the user already typed
                suffix = re.sub(r"^[^{]*", "", cur[len(base):]) if base and cur.startswith(base) else ""
                dest_var.set(chosen + (suffix or ""))

        def _open_dest():
            raw = dest_var.get().strip()
            # Open the resolved path (strip placeholders for a best-effort base)
            path = re.sub(r"\{[^}]*\}", "", raw).rstrip("/\\ ")
            if path:
                open_path(path)

        ttk.Button(dest_frame, text="Browse…", command=_browse_dest, style="Tiny.TButton").grid(row=0, column=1, padx=(4, 0))
        ttk.Button(dest_frame, text="Open", command=_open_dest, style="Tiny.TButton").grid(row=0, column=2, padx=(4, 0))
        dest_frame.grid(row=1, column=1, sticky="ew", pady=2)
        Tooltip(dest_entry, "Supports {placeholders}  e.g.  C:/Archive/{interviewer}/{name}\n"
                            "Available: {name} {email} {phone} {job_title} {company} {date} {year} {month} {interviewer} …\n"
                            "Filters: {name|upper}  {name|slug}  {name|first}  etc.\n"
                            "Empty = a 'Sorted' folder inside the watched folder")
        self.vars["dest_folder"] = dest_var
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=8)
        self.watch_var = tk.BooleanVar(value=self.auto_running())
        ttk.Checkbutton(bar, text="Watch automatically", variable=self.watch_var,
                        command=lambda: self.put("watch", self.watch_var.get())).pack(side="left")
        for text, cmd, tip_ in (("Process folder now", self.process_folder, "Read every file in the source folder"),
                                ("Add files…", self.add_files, "Pick files to sort"),
                                ("Review next ▶", self.review_next, "Open the next CV that needs review"),
                                ("Accept all clean", self.accept_clean, "Save every waiting CV that has no problems"),
                                ("Undo last", self.undo_last, "Move the last filed CV back")):
            b = ttk.Button(bar, text=text, command=cmd, style="Accent.TButton" if text.startswith("Review") else "TButton")
            b.pack(side="left", padx=(8, 0))
            Tooltip(b, tip_)
        ttk.Button(bar, text="⚙ Settings", command=lambda: self.app.open_settings(self.id)).pack(side="right")
        b_csv = ttk.Button(bar, text="📊 Open CSV", command=self.open_csv)
        b_csv.pack(side="right", padx=(0, 4))
        Tooltip(b_csv, "Open the configured output CSV file in Excel / default app")
        self.summary = ttk.Label(bar, style="Muted.TLabel")
        self.summary.pack(side="right", padx=10)
        DropZone(parent, "Drop CVs or folders here", self.accept_files, theme=self.app.theme, height=58).pack(fill="x")
        self.table = DataTable(parent, [("file", "File", 260, "w"), ("name", "Name", 180, "w"), ("status", "Status", 90, "w"),
                                        ("notes", "Notes", 360, "w")], height=12, searchable=True,
                               on_double=lambda iid: self.open_review(self.items[iid]), stretch="notes")
        self.table.pack(fill="both", expand=True, pady=(8, 0))
        self.table.tree.bind("<Return>", lambda e: self._each_selected(lambda i: self.open_review(i), 1))
        ttk.Label(parent, style="Muted.TLabel",
                  text="Shortcuts: Enter = review selected • Ctrl+R = review next • Ctrl+O = add files • F5 = process folder\n"
                       "In the review window: Enter = save & next • Esc = skip • Ctrl+S = save • Alt+←/→ = previous / next • Ctrl+W = close"
                  ).pack(anchor="w", pady=(4, 0))
        p = self.app.theme.p
        for tag, col in (("saved", p["ok"]), ("review", p["warn"]), ("error", p["err"]), ("skipped", p["muted"])):
            self.table.tree.tag_configure(tag, foreground=col)
        menu = tk.Menu(self.table.tree, tearoff=False)
        menu.add_command(label="Review…", command=lambda: self._each_selected(lambda i: self.open_review(i), 1))
        menu.add_command(label="Show in folder", command=lambda: self._each_selected(lambda i: reveal(i.path), 1))
        menu.add_command(label="Re-read this file", command=lambda: self._each_selected(self._reread))
        menu.add_command(label="Skip", command=lambda: self._each_selected(self.skip))
        menu.add_command(label="Remove from list", command=self._remove_selected)
        self.table.tree.bind("<Button-3>", lambda e: (self.table.tree.identify_row(e.y) and
                                                      self.table.tree.selection_set(self.table.tree.identify_row(e.y)),
                                                      menu.tk_popup(e.x_root, e.y_root)))
        for it in self.items.values():
            self._add_row(it)
        self._refresh_status()

    def _each_selected(self, fn, limit: int = 0) -> None:
        for iid in self.table.selection()[: limit or None]:
            if iid in self.items:
                fn(self.items[iid])

    def _remove_selected(self) -> None:
        sel = self.table.selection()
        for iid in sel:
            self.items.pop(iid, None)
        self.table.remove(sel)
        self._refresh_status()

    def _reread(self, it: Item) -> None:
        self.items.pop(it.iid, None)
        if self.built:
            self.table.remove([it.iid])
        self.submit(it.path if it.path.exists() else it.path, force=True)

    def _row(self, it: Item) -> dict:
        name = (it.candidates.get("name") or [""])[0]
        notes = f"→ {it.saved_as}" if it.saved_as else "; ".join(it.problems)
        return {"file": it.path.name, "name": name, "status": ("auto-saved" if it.auto and it.status == "saved" else it.status),
                "notes": notes}

    def _add_row(self, it: Item) -> None:
        self.table.add(self._row(it), iid=it.iid, tags=(it.status,))

    def _update_row(self, it: Item) -> None:
        if self.built and it.iid in self.items:
            self.table.update_row(it.iid, self._row(it), (it.status,))

    def _refresh_status(self) -> None:
        if self.built:
            pend = sum(1 for i in self.items.values() if i.status == "review")
            self.summary.configure(text=f"{pend} to review • {self.counts['saved']} saved • {self.counts['skipped']} skipped")

    def _sync_watch_var(self) -> None:
        if self.built:
            self.watch_var.set(self.auto_running())

    def toggle_watch(self) -> None:
        self.put("watch", not self.auto_running())

    def add_files(self) -> None:
        exts = " ".join("*" + e for e in self.extensions())
        files = filedialog.askopenfilenames(title="Choose CVs", filetypes=[("CVs", exts), ("All files", "*.*")])
        if files:
            self.accept_files(list(files))

    def review_next(self) -> None:
        pend = [i for i in self.items.values() if i.status == "review"]
        if not pend:
            self.app.notify("Nothing waiting for review")
            return
        self.open_review(pend[0])

    def open_review(self, it: Item) -> None:
        if it.status != "review":
            if it.status == "error":
                info(self.app.root, it.error or "This file could not be read.")
            return
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.show(it)
            self.dialog.lift()
            return
        self.dialog = ReviewDialog(self, it)

    def accept_clean(self) -> None:
        pend = [i for i in self.items.values() if i.status == "review" and not i.problems and i.text.strip()]
        if not pend:
            self.app.notify("No clean CVs waiting", "warn")
            return
        if not confirm(self.app.root, f"Save {len(pend)} CV(s) with the detected values?"):
            return
        for it in pend:
            it.auto = True
            self.save(it, {k: v[:1] for k, v in it.candidates.items() if v})
