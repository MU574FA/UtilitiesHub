"""Settings GUI: general, OCR, extraction rules and custom regex/keyword fields."""
from __future__ import annotations

import copy
import re
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .. import ocr
from ..clipboard import render_clipboard
from ..config import (BUILTIN_FIELDS, RESERVED_KEYS, ClipboardFormat, CustomField, LinkTemplate,
                      Settings, normalise_ext, save_settings, slugify, split_list)
from ..extractor import extract_custom, field_definitions
from ..fileops import render_filename
from ..links import link_definitions, render_link
from ..registry import NameRegistry
from .common import center, open_path


class CustomFieldDialog(tk.Toplevel):
    """Add/edit one custom field, with a live tester."""

    def __init__(self, parent: tk.Misc, spec: CustomField | None, on_ok: Callable[[CustomField], None],
                 threshold: Callable[[], float] = lambda: 0.8):
        super().__init__(parent)
        self.on_ok, self.threshold = on_ok, threshold
        self.title("Custom field")
        self.transient(parent)
        self.grab_set()
        spec = spec or CustomField("", "regex", "")

        self.name = tk.StringVar(value=spec.name)
        self.kind = tk.StringVar(value=spec.kind)
        self.ignore_case = tk.BooleanVar(value=spec.ignore_case)
        self.multiple = tk.BooleanVar(value=spec.multiple)
        self.fuzzy = tk.BooleanVar(value=spec.fuzzy)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Field name").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.name).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(frame, text="Type").grid(row=1, column=0, sticky="w", pady=4)
        kind = ttk.Combobox(frame, textvariable=self.kind, values=["regex", "keywords"],
                            state="readonly", width=12)
        kind.grid(row=1, column=1, sticky="w", padx=6)
        kind.bind("<<ComboboxSelected>>", lambda _e: self._update_hint())

        self.hint = ttk.Label(frame, style="Hint.TLabel", wraplength=480)
        self.hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.pattern = tk.Text(frame, height=4, width=60, wrap="word")
        self.pattern.grid(row=3, column=0, columnspan=2, sticky="ew", pady=4)
        self.pattern.insert("1.0", spec.pattern)

        ttk.Checkbutton(frame, text="Ignore case", variable=self.ignore_case).grid(row=4, column=0, sticky="w")
        ttk.Checkbutton(frame, text="Keep all matches (otherwise only the first)",
                        variable=self.multiple).grid(row=4, column=1, sticky="w")
        ttk.Checkbutton(frame, text="Similarity match: tolerate typos / OCR errors (keywords only, "
                                    "uses the threshold from Settings > Extraction)",
                        variable=self.fuzzy).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        tester = ttk.LabelFrame(frame, text="Test it: paste some CV text")
        tester.grid(row=6, column=0, columnspan=2, sticky="ew", pady=8)
        self.sample = tk.Text(tester, height=5, width=60, wrap="word")
        self.sample.pack(fill="x", padx=6, pady=4)
        row = ttk.Frame(tester)
        row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(row, text="Test", command=self._test).pack(side="left")
        self.result = ttk.Label(row, text="", wraplength=420)
        self.result.pack(side="left", padx=8)

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="OK", style="Accent.TButton", command=self._ok).pack(side="right")

        self._update_hint()
        center(self, 600, 640)

    def _update_hint(self) -> None:
        self.hint.config(text=(
            "One regular expression. If it has a (group), group 1 is kept; otherwise the whole match."
            if self.kind.get() == "regex" else
            "Keywords separated by commas or new lines. Every keyword found in the CV is returned."))

    def _spec(self) -> CustomField:
        return CustomField(self.name.get().strip(), self.kind.get(),
                           self.pattern.get("1.0", "end").strip(),
                           self.ignore_case.get(), self.multiple.get(), self.fuzzy.get())

    def _test(self) -> None:
        try:
            values = extract_custom(self._spec(), self.sample.get("1.0", "end"), self.threshold())
        except re.error as exc:
            self.result.config(text=f"Invalid regex: {exc}", foreground="#b00020")
            return
        self.result.config(text=("Found: " + " | ".join(values)) if values else "No match",
                           foreground="#0a5" if values else "#b36b00")

    def _ok(self) -> None:
        spec = self._spec()
        if not spec.name or not spec.pattern:
            messagebox.showwarning("Custom field", "Name and pattern are required.", parent=self)
            return
        try:
            extract_custom(spec, "")
        except re.error as exc:
            messagebox.showerror("Custom field", f"Invalid regex: {exc}", parent=self)
            return
        self.on_ok(spec)
        self.destroy()


class ClipboardFormatDialog(tk.Toplevel):
    """Create/edit a clipboard format: pick fields in the order you want, or type a template."""

    SEPARATORS = {"Tab (cells in Excel / Sheets)": r"\t", "Comma": ", ", "Semicolon": "; ",
                  "Space": " ", "New line": r"\n", "Pipe  |": " | ", "Dash  -": " - "}
    SAMPLE = {"name": "John Smith", "job_title": "Data Analyst", "phone": "+1 555 010 1234",
              "email": "john@example.com", "company": "Acme"}

    def __init__(self, parent: tk.Misc, fmt: ClipboardFormat | None,
                 keys: list[tuple[str, str]], on_ok: Callable[[ClipboardFormat], None]):
        super().__init__(parent)
        self.title("Clipboard format")
        self.transient(parent)
        self.grab_set()
        self.keys, self.labels, self.on_ok = keys, dict(keys), on_ok
        self.order: list[str] = []
        fmt = fmt or ClipboardFormat("", "")
        self.name = tk.StringVar(value=fmt.name)
        self.template = tk.StringVar(value=fmt.template)
        self.sep = tk.StringVar(value="Tab (cells in Excel / Sheets)")
        self.preview = tk.StringVar()

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Format name").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.name).grid(row=0, column=1, sticky="ew", padx=6)

        builder = ttk.LabelFrame(frame, text="Build it: pick fields in the order you want to paste them")
        builder.grid(row=1, column=0, columnspan=2, sticky="ew", pady=8)
        self.avail = tk.Listbox(builder, height=8, width=24, exportselection=False)
        for _key, label in keys:
            self.avail.insert("end", label)
        self.avail.grid(row=0, column=0, rowspan=4, padx=8, pady=6)
        ttk.Button(builder, text="Add →", command=self._add).grid(row=0, column=1, padx=6, sticky="s")
        self.chosen = tk.Listbox(builder, height=8, width=24, exportselection=False)
        self.chosen.grid(row=0, column=2, rowspan=4, padx=8, pady=6)
        side = ttk.Frame(builder)
        side.grid(row=0, column=3, rowspan=4, sticky="n", pady=6)
        for text, command in (("↑ Up", lambda: self._move(-1)), ("↓ Down", lambda: self._move(1)),
                              ("Remove", self._remove)):
            ttk.Button(side, text=text, command=command).pack(fill="x", pady=2)
        bottom = ttk.Frame(builder)
        bottom.grid(row=4, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))
        ttk.Label(bottom, text="Separator").pack(side="left")
        ttk.Combobox(bottom, textvariable=self.sep, values=list(self.SEPARATORS),
                     state="readonly", width=28).pack(side="left", padx=6)
        ttk.Button(bottom, text="Build template", command=self._build).pack(side="left")

        ttk.Label(frame, text="Template").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.template).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Label(frame, style="Hint.TLabel", wraplength=520, justify="left",
                  text=r"You can edit it by hand. {key} = first value, {key_all} = every value, "
                       r"\t = tab, \n = new line, plus {filename} {date} {original}. "
                       r"Empty fields keep their column.").grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="Preview").grid(row=4, column=0, sticky="nw", pady=(8, 0))
        ttk.Label(frame, textvariable=self.preview, wraplength=440, foreground="#0a7a3d").grid(
            row=4, column=1, sticky="w", padx=6, pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="OK", style="Accent.TButton", command=self._ok).pack(side="right")

        self.template.trace_add("write", self._update_preview)
        self._update_preview()
        center(self, 640, 600)

    def _selected(self) -> int | None:
        selection = self.chosen.curselection()
        return selection[0] if selection else None

    def _refresh(self, select: int | None = None) -> None:
        self.chosen.delete(0, "end")
        for key in self.order:
            self.chosen.insert("end", self.labels[key])
        if select is not None and 0 <= select < len(self.order):
            self.chosen.selection_set(select)

    def _add(self) -> None:
        selection = self.avail.curselection()
        if selection:
            self.order.append(self.keys[selection[0]][0])
            self._refresh(len(self.order) - 1)

    def _remove(self) -> None:
        index = self._selected()
        if index is not None:
            del self.order[index]
            self._refresh(min(index, len(self.order) - 1))

    def _move(self, delta: int) -> None:
        index = self._selected()
        if index is None or not 0 <= index + delta < len(self.order):
            return
        self.order[index], self.order[index + delta] = self.order[index + delta], self.order[index]
        self._refresh(index + delta)

    def _build(self) -> None:
        separator = self.SEPARATORS[self.sep.get()]
        self.template.set(separator.join("{%s}" % key for key in self.order))

    def _update_preview(self, *_args) -> None:
        sample = {k: [self.SAMPLE.get(k, f"<{label}>")] for k, label in self.keys
                  if k not in ("filename", "date", "original")}
        try:
            text = render_clipboard(self.template.get(), sample, Path("cv.pdf"), "John Smith.pdf")
        except ValueError:
            self.preview.set("⚠ invalid template")
            return
        self.preview.set(text.replace("\t", "  ⇥  ").replace("\n", "  ↵  "))

    def _ok(self) -> None:
        name, template = self.name.get().strip(), self.template.get()
        if not name or not template.strip():
            messagebox.showwarning("Clipboard format", "Name and template are required.", parent=self)
            return
        try:
            render_clipboard(template, {}, Path("x.pdf"))
        except ValueError as exc:
            messagebox.showerror("Clipboard format", str(exc), parent=self)
            return
        self.on_ok(ClipboardFormat(name, template))
        self.destroy()


class LinkTemplateDialog(tk.Toplevel):
    """Create/edit a link template (folder URL + file name) with a live preview."""

    SAMPLE = {"name": ["John Smith"], "company": ["Acme"], "job_title": ["Data Analyst"]}

    def __init__(self, parent: tk.Misc, tpl: LinkTemplate | None, on_ok: Callable[[LinkTemplate], None]):
        super().__init__(parent)
        self.title("Link template")
        self.transient(parent)
        self.grab_set()
        self.on_ok = on_ok
        tpl = tpl or LinkTemplate("", "")
        self.name = tk.StringVar(value=tpl.name)
        self.template = tk.StringVar(value=tpl.template)
        self.sample = tk.StringVar(value="John Smith.pdf")
        self.preview = tk.StringVar()

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Link name").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.name).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(frame, text="Template").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.template, width=70).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Label(frame, style="Hint.TLabel", wraplength=560, justify="left",
                  text="Put your folder link first and the file name at the end, e.g.\n"
                       "https://contoso-my.sharepoint.com/personal/hr/Documents/Archive/{filename}\n"
                       "Values are URL-encoded ('John Smith.pdf' becomes 'John%20Smith.pdf'). Placeholders: "
                       "{filename} {stem} {ext} {name} {company} {date} {original} and any field; "
                       "add _raw (e.g. {name_raw}) for no encoding. The link name becomes a field you can "
                       "use in clipboard formats, e.g. {archive_link}.").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Label(frame, text="Sample file name").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.sample).grid(row=3, column=1, sticky="ew", padx=6)
        ttk.Label(frame, text="Preview").grid(row=4, column=0, sticky="nw", pady=4)
        ttk.Label(frame, textvariable=self.preview, wraplength=460, foreground="#0a7a3d").grid(
            row=4, column=1, sticky="w", padx=6)
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="OK", style="Accent.TButton", command=self._ok).pack(side="right")
        for var in (self.template, self.sample):
            var.trace_add("write", self._update)
        self._update()
        center(self, 680, 420)

    def _update(self, *_args) -> None:
        sample = self.sample.get() or "file.pdf"
        try:
            self.preview.set(render_link(self.template.get(), self.SAMPLE, Path(sample), sample))
        except ValueError:
            self.preview.set("⚠ invalid template")

    def _ok(self) -> None:
        name, template = self.name.get().strip(), self.template.get().strip()
        if not name or not template:
            messagebox.showwarning("Link template", "Name and template are required.", parent=self)
            return
        try:
            render_link(template, {}, Path("x.pdf"), "x.pdf")
        except ValueError as exc:
            messagebox.showerror("Link template", str(exc), parent=self)
            return
        self.on_ok(LinkTemplate(name, template))
        self.destroy()


class SettingsWindow(tk.Toplevel):
    def __init__(self, parent: tk.Misc, settings: Settings, on_saved: Callable[[Settings], None]):
        super().__init__(parent)
        self.title("Settings")
        self.transient(parent)
        self.grab_set()
        self.settings, self.on_saved = settings, on_saved
        self.vars: dict[str, tk.Variable] = {}
        self.texts: dict[str, tk.Text] = {}
        self.builtin_vars: dict[str, tk.BooleanVar] = {}
        self.custom_fields = [CustomField(**asdict(cf)) for cf in settings.custom_fields]
        self.formats = [ClipboardFormat(**asdict(f)) for f in settings.clipboard_formats]
        self.links = [LinkTemplate(**asdict(t)) for t in settings.link_templates]

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        for title, builder in (("General", self._tab_general),
                               ("OCR & Converters", self._tab_ocr),
                               ("Extraction", self._tab_extraction),
                               ("Custom fields", self._tab_custom),
                               ("Names list", self._tab_names),
                               ("Links", self._tab_links),
                               ("Clipboard", self._tab_clipboard)):
            frame = ttk.Frame(notebook, padding=12)
            frame.columnconfigure(1, weight=1)
            builder(frame)
            notebook.add(frame, text=title)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(side="right")
        center(self, 900, 760)

    # ------------------------------------------------------------------ widget helpers
    @staticmethod
    def _next_row(frame: ttk.Frame) -> int:
        return frame.grid_size()[1]

    def _entry(self, frame, label, attr, browse: str | None = None) -> None:
        row = self._next_row(frame)
        var = tk.StringVar(value=str(getattr(self.settings, attr)))
        self.vars[attr] = var
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=var).grid(row=row, column=1, sticky="ew", padx=6)
        if browse:
            ttk.Button(frame, text="Browse…", command=lambda: self._browse(var, browse)).grid(row=row, column=2)

    def _check(self, frame, text, attr) -> None:
        var = tk.BooleanVar(value=getattr(self.settings, attr))
        self.vars[attr] = var
        ttk.Checkbutton(frame, text=text, variable=var).grid(
            row=self._next_row(frame), column=0, columnspan=3, sticky="w", pady=2)

    def _combo(self, frame, label, attr, values) -> None:
        row = self._next_row(frame)
        var = tk.StringVar(value=getattr(self.settings, attr))
        self.vars[attr] = var
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(frame, textvariable=var, values=values, state="readonly", width=14).grid(
            row=row, column=1, sticky="w", padx=6)

    def _spin(self, frame, label, attr, low, high, step) -> None:
        row = self._next_row(frame)
        var = tk.StringVar(value=str(getattr(self.settings, attr)))
        self.vars[attr] = var
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Spinbox(frame, from_=low, to=high, increment=step, textvariable=var, width=8).grid(
            row=row, column=1, sticky="w", padx=6)

    def _text(self, frame, label, attr, values: list[str], height=3, sep=", ") -> None:
        row = self._next_row(frame)
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="nw", pady=4)
        widget = tk.Text(frame, height=height, wrap="word")
        widget.grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=4)
        widget.insert("1.0", sep.join(values))
        self.texts[attr] = widget

    def _hint(self, frame, text) -> None:
        ttk.Label(frame, text=text, style="Hint.TLabel", wraplength=640, justify="left").grid(
            row=self._next_row(frame), column=0, columnspan=3, sticky="w", pady=(0, 6))

    @staticmethod
    def _browse(var: tk.StringVar, kind: str) -> None:
        if kind == "dir":
            chosen = filedialog.askdirectory()
        elif kind == "names":   # may be a file that does not exist yet
            chosen = filedialog.asksaveasfilename(
                confirmoverwrite=False, defaultextension=".txt",
                filetypes=[("Names list", "*.txt *.csv"), ("All files", "*.*")])
        else:
            chosen = filedialog.askopenfilename()
        if chosen:
            var.set(chosen)

    # ------------------------------------------------------------------ tabs
    def _tab_general(self, f) -> None:
        self._entry(f, "Watch folder (inbox)", "watch_folder", "dir")
        self._entry(f, "Output folder", "output_folder", "dir")
        self._entry(f, "File name template", "filename_template")
        placeholders = " ".join("{%s}" % d.key for d in field_definitions(self.settings))
        self._hint(f, f"Placeholders: {placeholders} {{date}} {{original}}   "
                      "e.g. {name} - {job_title}. The extension is added automatically.")
        self._combo(f, "If the name already exists", "duplicate_policy", ["number", "overwrite", "skip"])
        self._combo(f, "Action", "file_action", ["move", "copy"])
        self._spin(f, "Wait until file is stable (s)", "stable_seconds", 0.5, 30, 0.5)
        self._text(f, "File types", "extensions", self.settings.extensions, height=2)
        self._check(f, "Start watching when the app starts", "autostart_watching")
        self._check(f, "Process files already in the inbox when watching starts", "process_existing")
        self._check(f, "Auto-confirm: skip the verification pop-up (not recommended)", "auto_confirm")
        self._check(f, "Keep the pop-up on top of other windows", "popup_topmost")
        self._check(f, "Write a cv_history.csv log in the output folder", "keep_history")
        self._check(f, "Show the original file next to the extracted data in the pop-up", "show_preview")
        self._spin(f, "Preview: maximum pages to render", "preview_pages", 1, 30, 1)
        self._entry(f, "Duplicates folder", "duplicates_folder", "dir")
        self._hint(f, "Where 'Send to Duplicates' puts files. Empty = a 'Duplicates' folder inside the output folder.")
        self._check(f, "Rename duplicates with the file-name template too (otherwise keep the original name)",
                    "duplicates_rename")

    def _tab_ocr(self, f) -> None:
        self._check(f, "Enable OCR (images, scanned PDFs, pictures inside DOCX/PPTX)", "ocr_enabled")
        self._entry(f, "Tesseract executable", "tesseract_cmd", "file")
        self._hint(f, "Leave empty if 'tesseract' is on your PATH.")
        self._entry(f, "OCR languages", "ocr_languages")
        self._hint(f, "Tesseract codes joined with '+', e.g. eng+ara+fra (language packs must be installed).")
        self._spin(f, "Render DPI for PDFs", "ocr_dpi", 100, 600, 50)
        self._spin(f, "OCR a PDF page if it has fewer characters than", "ocr_min_chars", 0, 2000, 10)
        self._entry(f, "LibreOffice executable", "libreoffice_cmd", "file")
        self._hint(f, "Only needed for old .doc / .ppt / .rtf files. Empty = auto-detect.")
        ttk.Button(f, text="Test Tesseract", command=self._test_tesseract).grid(
            row=self._next_row(f), column=0, sticky="w", pady=8)

    def _tab_extraction(self, f) -> None:
        box = ttk.LabelFrame(f, text="Built-in fields to extract")
        box.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        for key, label in BUILTIN_FIELDS.items():
            var = tk.BooleanVar(value=key in self.settings.builtin_fields)
            self.builtin_vars[key] = var
            ttk.Checkbutton(box, text=label, variable=var).pack(side="left", padx=8, pady=4)
        self._text(f, "Job-title keywords", "job_title_keywords", self.settings.job_title_keywords, 3)
        self._text(f, "Company suffixes", "company_suffixes", self.settings.company_suffixes, 3)
        self._text(f, "Words that are NOT names", "name_stopwords", self.settings.name_stopwords, 3)
        self._hint(f, "Comma or new-line separated. Tune these to improve detection for your CVs.")
        self._spin(f, "Similarity threshold (%)", "fuzzy_threshold", 50, 100, 1)
        self._hint(f, "For keyword fields and the company list. Higher = stricter; ~80 tolerates one wrong "
                      "letter in a 6-letter word. Words under 5 letters and the first letter must match exactly.")
        self._check(f, "Also tolerate typos in job-title keywords (may cause false positives)", "fuzzy_job_titles")
        self._text(f, "Known companies (one per line)", "known_companies",
                   self.settings.known_companies, 3, sep="\n")
        self._hint(f, "Your own company list, matched by similarity: 'Gogle Inc' → 'Google'. "
                      "Matches are put first in the Company field.")
        self._check(f, "Replace a detected company with the closest known company", "company_snap")

    def _tab_custom(self, f) -> None:
        f.rowconfigure(1, weight=1)
        self._hint(f, "Extra fields found with your own regex or keyword list. Each becomes a column in the "
                      "pop-up and a {placeholder} (its name in lower_snake_case) for the file name.")
        self.tree = ttk.Treeview(f, columns=("name", "kind", "pattern"), show="headings", height=10)
        for col, text, width in (("name", "Name", 150), ("kind", "Type", 80), ("pattern", "Pattern", 380)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.grid(row=1, column=0, columnspan=3, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _e: self._edit_field())
        bar = ttk.Frame(f)
        bar.grid(row=2, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(bar, text="Add…", command=self._add_field).pack(side="left")
        ttk.Button(bar, text="Edit…", command=self._edit_field).pack(side="left", padx=6)
        ttk.Button(bar, text="Delete", command=self._delete_field).pack(side="left")
        self._refresh_tree()

    # ------------------------------------------------------------------ custom field list
    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, cf in enumerate(self.custom_fields):
            kind = cf.kind + (" ≈" if cf.kind == "keywords" and cf.fuzzy else "")
            self.tree.insert("", "end", iid=str(i), values=(cf.name, kind, cf.pattern.replace("\n", " ")))

    def _selected(self) -> int | None:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _add_field(self) -> None:
        CustomFieldDialog(self, None, lambda spec: (self.custom_fields.append(spec), self._refresh_tree()),
                          self._threshold)

    def _edit_field(self) -> None:
        index = self._selected()
        if index is not None:
            def done(spec: CustomField) -> None:
                self.custom_fields[index] = spec
                self._refresh_tree()
            CustomFieldDialog(self, self.custom_fields[index], done, self._threshold)

    def _delete_field(self) -> None:
        index = self._selected()
        if index is not None:
            del self.custom_fields[index]
            self._refresh_tree()

    # ------------------------------------------------------------------ names list tab
    def _tab_names(self, f) -> None:
        self._entry(f, "Names list file", "names_list_path", "names")
        self._hint(f, "A .txt file (one name per line) or .csv (first column, or the column headed 'Name'). "
                      "Leave empty to turn the check off. The file is re-read whenever you edit it, and is "
                      "created when the first name is added.")
        self._spin(f, "Similarity threshold (%)", "name_match_threshold", 60, 100, 1)
        self._hint(f, "85 catches one-letter typos (Jon / John). Lower it to catch more variants (more false "
                      "alarms). Word order and accents are ignored. The pop-up re-checks every time you edit "
                      "the name and shows the full listed name it is close to.")
        self._check(f, "Add accepted names to the list automatically (not when sent to Duplicates)",
                    "names_auto_add")
        ttk.Button(f, text="Open list file", command=self._open_names).grid(
            row=self._next_row(f), column=0, sticky="w", pady=8)

    def _open_names(self) -> None:
        path = self.vars["names_list_path"].get().strip()
        if not path:
            messagebox.showinfo("Names list", "Choose a file first.", parent=self)
            return
        try:
            NameRegistry(path).ensure_file()
        except OSError as exc:
            messagebox.showerror("Names list", str(exc), parent=self)
            return
        open_path(path)

    # ------------------------------------------------------------------ links tab
    def _tab_links(self, f) -> None:
        f.rowconfigure(1, weight=1)
        self._hint(f, "Build a URL from the final file name, e.g. your OneDrive / SharePoint archive folder + "
                      "the file name. Every link appears in the pop-up with a Copy button and can be used in "
                      "clipboard formats as {link_name}.")
        self.link_tree = ttk.Treeview(f, columns=("name", "template"), show="headings", height=10)
        self.link_tree.heading("name", text="Name")
        self.link_tree.heading("template", text="Template")
        self.link_tree.column("name", width=160, anchor="w")
        self.link_tree.column("template", width=480, anchor="w")
        self.link_tree.grid(row=1, column=0, columnspan=3, sticky="nsew")
        self.link_tree.bind("<Double-1>", lambda _e: self._edit_link())
        bar = ttk.Frame(f)
        bar.grid(row=2, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(bar, text="Add…", command=self._add_link).pack(side="left")
        ttk.Button(bar, text="Edit…", command=self._edit_link).pack(side="left", padx=6)
        ttk.Button(bar, text="Delete", command=self._delete_link).pack(side="left")
        self._refresh_links()

    def _refresh_links(self) -> None:
        self.link_tree.delete(*self.link_tree.get_children())
        for i, tpl in enumerate(self.links):
            self.link_tree.insert("", "end", iid=str(i), values=(tpl.name, tpl.template))

    def _add_link(self) -> None:
        LinkTemplateDialog(self, None, lambda tpl: (self.links.append(tpl), self._refresh_links()))

    def _edit_link(self) -> None:
        selection = self.link_tree.selection()
        if selection:
            index = int(selection[0])

            def done(tpl: LinkTemplate) -> None:
                self.links[index] = tpl
                self._refresh_links()
            LinkTemplateDialog(self, self.links[index], done)

    def _delete_link(self) -> None:
        selection = self.link_tree.selection()
        if selection:
            del self.links[int(selection[0])]
            self._refresh_links()

    # ------------------------------------------------------------------ clipboard formats
    def _tab_clipboard(self, f) -> None:
        self._check(f, "Copy to the clipboard automatically after 'Save & Move'", "clipboard_auto")
        row = self._next_row(f)
        self.vars["clipboard_default"] = tk.StringVar(value=self.settings.clipboard_default)
        ttk.Label(f, text="Default format").grid(row=row, column=0, sticky="w", pady=4)
        self.default_combo = ttk.Combobox(f, textvariable=self.vars["clipboard_default"],
                                          state="readonly", width=32)
        self.default_combo.grid(row=row, column=1, sticky="w", padx=6)
        self._hint(f, "A format chooses which values are copied and in what order. Tab-separated formats "
                      "paste into separate cells in Excel / Google Sheets. In the verification pop-up you "
                      "can pick any format and copy it manually, and Tools → 'Copy last record as' re-copies "
                      "the last saved CV.")
        row = self._next_row(f)
        f.rowconfigure(row, weight=1)
        self.fmt_tree = ttk.Treeview(f, columns=("name", "template"), show="headings", height=8)
        self.fmt_tree.heading("name", text="Name")
        self.fmt_tree.heading("template", text="Template")
        self.fmt_tree.column("name", width=160, anchor="w")
        self.fmt_tree.column("template", width=440, anchor="w")
        self.fmt_tree.grid(row=row, column=0, columnspan=3, sticky="nsew")
        self.fmt_tree.bind("<Double-1>", lambda _e: self._edit_format())
        bar = ttk.Frame(f)
        bar.grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(bar, text="Add…", command=self._add_format).pack(side="left")
        ttk.Button(bar, text="Edit…", command=self._edit_format).pack(side="left", padx=6)
        ttk.Button(bar, text="Delete", command=self._delete_format).pack(side="left")
        self._refresh_formats()

    def _refresh_formats(self) -> None:
        self.fmt_tree.delete(*self.fmt_tree.get_children())
        for i, fmt in enumerate(self.formats):
            self.fmt_tree.insert("", "end", iid=str(i), values=(fmt.name, fmt.template))
        names = [fmt.name for fmt in self.formats]
        self.default_combo.configure(values=names)
        if self.vars["clipboard_default"].get() not in names:
            self.vars["clipboard_default"].set(names[0] if names else "")

    def _format_keys(self) -> list[tuple[str, str]]:
        """Fields available to a format: the ones currently active, plus a few extras."""
        tmp = copy.copy(self.settings)
        tmp.builtin_fields = [k for k, v in self.builtin_vars.items() if v.get()]
        tmp.custom_fields = self.custom_fields
        tmp.link_templates = self.links
        return [(d.key, d.label) for d in field_definitions(tmp)] + [
            (d.key, d.label) for d in link_definitions(tmp)] + [
            ("filename", "Saved file name"), ("date", "Date"), ("original", "Original file name")]

    def _add_format(self) -> None:
        ClipboardFormatDialog(self, None, self._format_keys(),
                              lambda fmt: (self.formats.append(fmt), self._refresh_formats()))

    def _edit_format(self) -> None:
        selection = self.fmt_tree.selection()
        if selection:
            index = int(selection[0])

            def done(fmt: ClipboardFormat) -> None:
                if self.vars["clipboard_default"].get() == self.formats[index].name:
                    self.vars["clipboard_default"].set(fmt.name)  # keep the default when renaming
                self.formats[index] = fmt
                self._refresh_formats()
            ClipboardFormatDialog(self, self.formats[index], self._format_keys(), done)

    def _delete_format(self) -> None:
        selection = self.fmt_tree.selection()
        if selection:
            del self.formats[int(selection[0])]
            self._refresh_formats()

    # ------------------------------------------------------------------ actions
    def _threshold(self) -> float:
        try:
            return min(1.0, max(0.5, float(self.vars["fuzzy_threshold"].get()) / 100))
        except ValueError:
            return 0.8

    def _test_tesseract(self) -> None:
        try:
            current = self._collect()
            version = ocr.check_tesseract(current)
            messagebox.showinfo("Tesseract", f"Tesseract {version} found.", parent=self)
        except (ocr.OcrUnavailable, ValueError) as exc:
            messagebox.showerror("Tesseract", str(exc), parent=self)

    def _text_list(self, attr: str) -> list[str]:
        return split_list(self.texts[attr].get("1.0", "end"))

    def _collect(self) -> Settings:
        """Build a new Settings from the widgets (raises ValueError if invalid)."""
        new = copy.deepcopy(self.settings)
        for attr, var in self.vars.items():
            current, raw = getattr(new, attr), var.get()
            try:
                if isinstance(current, bool):
                    value = bool(raw)
                elif isinstance(current, int):
                    value = int(float(raw))
                elif isinstance(current, float):
                    value = float(raw)
                else:
                    value = str(raw).strip()
            except ValueError:
                raise ValueError(f"Invalid number for '{attr}': {raw!r}") from None
            setattr(new, attr, value)
        new.extensions = [normalise_ext(e) for e in self._text_list("extensions")]
        new.job_title_keywords = self._text_list("job_title_keywords")
        new.company_suffixes = self._text_list("company_suffixes")
        new.name_stopwords = [w.lower() for w in self._text_list("name_stopwords")]
        new.builtin_fields = [k for k, v in self.builtin_vars.items() if v.get()]
        new.custom_fields = copy.deepcopy(self.custom_fields)
        new.known_companies = [ln.strip() for ln in self.texts["known_companies"].get("1.0", "end").splitlines()
                               if ln.strip()]
        new.clipboard_formats = copy.deepcopy(self.formats)
        new.link_templates = copy.deepcopy(self.links)
        if new.clipboard_default not in [f.name for f in new.clipboard_formats]:
            new.clipboard_default = new.clipboard_formats[0].name if new.clipboard_formats else ""
        return new

    @staticmethod
    def _validate(s: Settings) -> None:
        if not s.watch_folder or not s.output_folder:
            raise ValueError("Watch folder and output folder are required.")
        if Path(s.watch_folder).resolve() == Path(s.output_folder).resolve():
            raise ValueError("Watch folder and output folder must be different.")
        if not s.extensions:
            raise ValueError("Select at least one file type.")
        keys = []
        for cf in s.custom_fields:
            if cf.key in RESERVED_KEYS or cf.key in keys:
                raise ValueError(f"Custom field name '{cf.name}' clashes with another field.")
            keys.append(cf.key)
            try:
                extract_custom(cf, "")
            except re.error as exc:
                raise ValueError(f"Custom field '{cf.name}' has an invalid regex: {exc}") from None
        if s.duplicates_folder.strip() and Path(s.duplicates_folder).resolve() == Path(s.watch_folder).resolve():
            raise ValueError("The duplicates folder must be different from the watch folder.")
        if not 60 <= s.name_match_threshold <= 100:
            raise ValueError("The names similarity threshold must be between 60 and 100.")
        link_keys: list[str] = []
        for tpl in s.link_templates:
            key = slugify(tpl.name)
            if not tpl.name.strip() or not tpl.template.strip():
                raise ValueError("Every link needs a name and a template.")
            if key in RESERVED_KEYS or key in keys or key in link_keys:
                raise ValueError(f"Link name '{tpl.name}' clashes with another field.")
            link_keys.append(key)
            render_link(tpl.template, {"name": ["Test"]}, Path("x.pdf"), "x.pdf")  # ValueError if malformed
        if not 50 <= s.fuzzy_threshold <= 100:
            raise ValueError("The similarity threshold must be between 50 and 100.")
        names = [f.name for f in s.clipboard_formats]
        if len(set(names)) != len(names):
            raise ValueError("Clipboard format names must be unique.")
        for fmt in s.clipboard_formats:
            render_clipboard(fmt.template, {}, Path("x.pdf"))  # raises ValueError if malformed
        render_filename(s.filename_template, {"name": ["Test Name"]}, Path("x.pdf"))

    def _save(self) -> None:
        try:
            new = self._collect()
            self._validate(new)
            save_settings(new)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Settings", str(exc), parent=self)
            return
        self.on_saved(new)
        self.destroy()
