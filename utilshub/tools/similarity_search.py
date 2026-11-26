"""Similarity Search: smart cached OCR across files/folders, with multi-mode similarity search."""
from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable

from PIL import Image, ImageGrab

from ..core import ocr
from ..core.fileops import batch_note, new_batch, transfer
from ..core.ocr_search import (
    CHANGE_POLICIES,
    FILE_TYPE_GROUPS,
    SEARCH_MODES,
    CachedDoc,
    ChangedFile,
    CorpusIndex,
    OcrTextStore,
    SearchHit,
    SearchOptions,
    classify_files,
    collect_search_files,
    find_highlight_spans,
    format_doc_pages,
    index_files,
    make_pasted_doc,
    norm_path,
    ocr_config_signature,
    search_by_documents,
    search_documents,
)
from ..core.readers import ReadOptions, supported_extensions
from ..core.schema import Opt
from ..core.sheets import write_xlsx
from ..core.system import open_path, reveal
from ..core.textutil import human_size, parse_ext_list
from ..ui.autocomplete import attach as autocomplete
from ..ui.viewer import PageViewer
from ..ui.widgets import DataTable, Tooltip, confirm, copy_to_clipboard, error, info, modal, style_text
from .base import Command, Tool

_IMAGE_CLIP_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif")
_FILE_TYPE_CHOICES = [
    ("all", "All supported"),
    ("pdf", "PDF"),
    ("images", "Images"),
    ("office", "Office"),
    ("text", "Plain text"),
]


class ChangedFilesDialog:
    """Ask the user which modified/reconfigured cached files should be re-OCR'd."""

    def __init__(self, parent: tk.Misc, changed: list[ChangedFile], tool: "SimilaritySearch") -> None:
        self.tool = tool
        self.changed = changed
        self.chosen: list[str] = []
        self.cancelled = True
        self.win, body = modal(parent, f"Changed files detected ({len(changed)})", 760, 440)
        self.win.minsize(600, 340)

        ttk.Label(
            body,
            text=(
                f"{len(changed)} cached file(s) changed since they were last OCR'd. "
                "Tick the files to re-OCR and update in the cache (unticked files keep their cached text):"
            ),
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        self.vars: dict[str, tk.BooleanVar] = {}
        box = ttk.Frame(body)
        box.pack(fill="both", expand=True)

        canvas = tk.Canvas(box, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_id, width=e.width))

        for cf in changed:
            v = tk.BooleanVar(value=True)
            self.vars[cf.path] = v
            row = ttk.Frame(inner, padding=(2, 3))
            row.pack(fill="x")
            why = "Content changed" if cf.reason == "modified" else "OCR settings changed"
            old_t = datetime.fromtimestamp(cf.old_mtime).strftime("%Y-%m-%d %H:%M") if cf.old_mtime else "?"
            new_t = datetime.fromtimestamp(cf.new_mtime).strftime("%Y-%m-%d %H:%M") if cf.new_mtime else "?"
            detail = f"{Path(cf.path).name}  —  {why}  ({human_size(cf.old_size)} → {human_size(cf.new_size)}, {old_t} → {new_t})"
            cb = ttk.Checkbutton(row, text=detail, variable=v)
            cb.pack(side="left", anchor="w")
            Tooltip(cb, cf.path)

        sel_bar = ttk.Frame(body)
        sel_bar.pack(fill="x", pady=(6, 4))
        ttk.Button(sel_bar, text="Select all", style="Tiny.TButton", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(sel_bar, text="Select none", style="Tiny.TButton", command=lambda: self._set_all(False)).pack(side="left", padx=6)
        self.always_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(sel_bar, text="Always re-OCR changed files automatically", variable=self.always_var).pack(side="right")

        btn_bar = ttk.Frame(body)
        btn_bar.pack(fill="x", pady=(8, 0))
        ttk.Button(btn_bar, text="Re-OCR selected", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(btn_bar, text="Keep all cached", command=self._keep_all).pack(side="right", padx=6)
        ttk.Button(btn_bar, text="Cancel", command=self._cancel).pack(side="right")
        self.win.bind("<Escape>", lambda _e: self._cancel())
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)
        self.win.grab_set()
        self.win.wait_window()

    def _set_all(self, value: bool) -> None:
        for v in self.vars.values():
            v.set(value)

    def _ok(self) -> None:
        self.chosen = [p for p, v in self.vars.items() if v.get()]
        self.cancelled = False
        if self.always_var.get():
            self.tool.put("on_file_changed", "auto")
        self.win.destroy()

    def _keep_all(self) -> None:
        self.chosen = []
        self.cancelled = False
        self.win.destroy()

    def _cancel(self) -> None:
        self.chosen = []
        self.cancelled = True
        self.win.destroy()


class CacheManagerDialog:
    """Inspect, search, prune, export, or clear the persistent OCR cache."""

    def __init__(self, parent: tk.Misc, tool: "SimilaritySearch") -> None:
        self.tool = tool
        self.win, body = modal(parent, "OCR Cache Manager", 820, 520)
        self.win.minsize(640, 380)

        self.stats_lbl = ttk.Label(body, style="Muted.TLabel")
        self.stats_lbl.pack(anchor="w", pady=(0, 6))

        self.table = DataTable(
            body,
            [
                ("file", "File", 210),
                ("kind", "Type", 65),
                ("pages", "Pages", 55, "e"),
                ("chars", "Chars", 75, "e"),
                ("how", "Read by", 85),
                ("status", "Status", 80),
                ("folder", "Folder", 240),
            ],
            height=14,
            searchable=True,
            stretch="folder",
        )
        self.table.pack(fill="both", expand=True)
        self.table.tag_colors(missing="err", ok="ok")

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="Remove selected", command=self.remove_selected).pack(side="left")
        ttk.Button(bar, text="Prune missing", command=self.prune_missing).pack(side="left", padx=6)
        ttk.Button(bar, text="Export TXT…", command=self.export_txt).pack(side="left")
        ttk.Button(bar, text="Export JSON…", command=self.export_json).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear all", command=self.clear_all).pack(side="left")
        ttk.Button(bar, text="Close", command=self.win.destroy).pack(side="right")

        self.refresh()

    def refresh(self) -> None:
        self.table.clear()
        for row in self.tool.store.list_summaries():
            status = "OK" if row["exists"] else "Missing"
            tag = () if row["exists"] else ("missing",)
            self.table.add(
                {
                    "file": row["name"],
                    "kind": row["kind"],
                    "pages": row["pages"],
                    "chars": row["char_count"],
                    "how": "OCR" if row["used_ocr"] else "text layer",
                    "status": status,
                    "folder": row["folder"],
                },
                iid=row["norm_path"],
                tags=tag,
            )
        st = self.tool.store.stats()
        self.stats_lbl.configure(
            text=(
                f"{st['docs']} cached document(s) • {st['pages']} page(s) • {st['chars']:,} character(s) • "
                f"DB size {human_size(st['db_bytes'])} • {st['db_path']}"
            )
        )
        self.tool._refresh_cache_btn()

    def remove_selected(self) -> None:
        sel = self.table.selection()
        if not sel:
            return
        self.tool.store.delete(sel)
        for np in sel:
            self.tool.active_docs.pop(np, None)
        self.tool._invalidate_index()
        self.refresh()

    def prune_missing(self) -> None:
        removed = self.tool.store.prune_missing()
        self.tool._drop_missing_active()
        self.refresh()
        self.tool.app.notify(f"Pruned {removed} missing file(s) from cache")

    def export_txt(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Export cached OCR text",
            defaultextension=".txt",
            initialdir=self.tool.output_dir(),
            initialfile="ocr-cache.txt",
            filetypes=[("Text", "*.txt")],
            parent=self.win,
        )
        if path:
            n = self.tool.store.export_to_txt(path)
            self.tool.app.notify(f"Exported {n} document(s) to {Path(path).name}")

    def export_json(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Export cached OCR JSON",
            defaultextension=".json",
            initialdir=self.tool.output_dir(),
            initialfile="ocr-cache.json",
            filetypes=[("JSON", "*.json")],
            parent=self.win,
        )
        if path:
            n = self.tool.store.export_to_json(path)
            self.tool.app.notify(f"Exported {n} document(s) to {Path(path).name}")

    def clear_all(self) -> None:
        if confirm(self.win, "Clear all cached OCR text from the database?"):
            self.tool.store.clear()
            self.tool.active_docs.clear()
            self.tool._invalidate_index()
            self.refresh()


class SimilaritySearch(Tool):
    id = "similarity_search"
    title = "Similarity Search"
    icon = "🧭"
    order = 47
    input_path_key = "input_folder"
    output_path_key = "output_folder"
    description = "OCR and cache files or folders with smart change detection, then search by similarity using text, files, or folders."

    @classmethod
    def options(cls) -> list[Opt]:
        p = cls.id + "."
        return [
            Opt(p + "input_folder", "", "Input folder", "folder", section="Folders",
                help="Default folder when adding files/folders; also used if the 'Search in' list is empty."),
            Opt(p + "output_folder", "", "Output folder", "folder", section="Folders",
                help="Default folder when exporting results, saving OCR text, or copying matched files."),
            Opt(p + "sources", [], "Folders and files to search", "list", section="Sources", advanced=True),
            Opt(p + "recursive", True, "Include sub-folders", "bool", section="Sources"),
            Opt(p + "auto_index", True, "Index / OCR automatically when adding files or folders", "bool", section="Sources"),
            Opt(p + "include_all_cached", False, "Include all previously cached files in search", "bool", section="Sources"),
            Opt(p + "extensions", "", "Only these extensions (e.g. pdf, png; empty = all)", "str", section="Sources"),
            Opt(p + "file_type", "all", "File type filter", "choice", choices=_FILE_TYPE_CHOICES, section="Sources"),
            Opt(p + "on_file_changed", "ask", "When a cached file changes", "choice", choices=CHANGE_POLICIES,
                section="Smart OCR Cache",
                help="Ask once per batch before re-OCRing modified files, re-OCR automatically, or keep the cached text."),
            Opt(p + "reocr_on_cfg_change", False, "Also ask to re-OCR when OCR language/DPI settings change", "bool",
                section="Smart OCR Cache"),
            Opt(p + "mode", "smart", "Search mode", "choice", choices=SEARCH_MODES, section="Search"),
            Opt(p + "threshold", 60, "Minimum similarity (%)", "percent", min=20, max=100, section="Search"),
            Opt(p + "ocr_tolerant", True, "OCR-tolerant matching (rn/m, 0/O, 1/l, hyphenated breaks, Arabic/accents)", "bool",
                section="Search"),
            Opt(p + "ignore_case", True, "Ignore case (Exact / Regex)", "bool", section="Search"),
            Opt(p + "match_all_lines", False, "Require all query lines to match (AND)", "bool", section="Search"),
            Opt(p + "live_search", True, "Search automatically while typing", "bool", section="Search"),
            Opt(p + "top_k", 200, "Maximum results to show", "int", min=10, max=5000, section="Search"),
            Opt(p + "preview", True, "Show OCR text & document preview pane", "bool", section="Result"),
            Opt(p + "recent_queries", [], "Recent queries", "list", section="Search", advanced=True),
            *cls.append_opts(),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.store = OcrTextStore()
        self.pasted_docs: dict[str, CachedDoc] = {}
        self.active_docs: dict[str, CachedDoc] = {}
        self._corpus_index: CorpusIndex | None = None
        self.query_files: list[str] = []
        self.hits: dict[str, SearchHit] = {}
        self._hit_spans: list[tuple[int, int, int]] = []
        self._hit_idx: int = 0
        self._live_after = None
        self._paste_seq = 0
        self._reader_fn: Callable | None = None
        self._changed_prompt: Callable[[list[ChangedFile]], tuple[list[str], bool] | None] | None = None

    # ------------------------------------------------------------------ UI
    def build(self, parent: ttk.Frame) -> None:
        self.vars: dict[str, tk.Variable] = {}

        def var(key: str, kind=tk.StringVar):
            v = kind(value=self.get(key))
            v.trace_add("write", lambda *_a, k=key, vv=v: self._store_var(k, vv))
            self.vars[key] = v
            return v

        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=(0, 0, 6, 0))
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=4)

        # --- Left: sources & smart OCR cache
        hdr = ttk.Frame(left)
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Search in", style="H3.TLabel").pack(side="left")
        self.cache_btn = ttk.Button(hdr, text="💾 0", style="Tiny.TButton", command=self.open_cache_manager)
        self.cache_btn.pack(side="right")
        Tooltip(self.cache_btn, "Open OCR Cache Manager")

        sbox = ttk.Frame(left)
        sbox.pack(fill="both", expand=True, pady=3)
        self.src_list = tk.Listbox(sbox, width=22, height=7, selectmode="extended", activestyle="none")
        ssb = ttk.Scrollbar(sbox, command=self.src_list.yview)
        self.src_list.configure(yscrollcommand=ssb.set)
        ssb.pack(side="right", fill="y")
        self.src_list.pack(side="left", fill="both", expand=True)
        style_text(left, self.src_list)

        sbar1 = ttk.Frame(left)
        sbar1.pack(fill="x")
        b_af = ttk.Button(sbar1, text="Folder", style="Tiny.TButton", command=self.add_folder)
        b_af.pack(side="left")
        Tooltip(b_af, "Add folder to index")
        b_afi = ttk.Button(sbar1, text="Files", style="Tiny.TButton", command=self.add_files)
        b_afi.pack(side="left", padx=2)
        Tooltip(b_afi, "Add files to index")
        b_pt = ttk.Button(sbar1, text="Paste", style="Tiny.TButton", command=self.add_pasted_text)
        b_pt.pack(side="left")
        Tooltip(b_pt, "Add pasted text snippet to corpus")

        sbar2 = ttk.Frame(left)
        sbar2.pack(fill="x", pady=(3, 0))
        ttk.Button(sbar2, text="Remove", style="Tiny.TButton", command=self.remove_sources).pack(side="left")
        ttk.Button(sbar2, text="Clear", style="Tiny.TButton", command=self.clear_sources).pack(side="left", padx=3)

        self.src_lbl = ttk.Label(left, style="Muted.TLabel", wraplength=200, justify="left", text="")
        self.src_lbl.pack(anchor="w", pady=(3, 2))

        for txt, key in (
            ("Sub-folders", "recursive"),
            ("Auto-index", "auto_index"),
            ("All cached files", "include_all_cached"),
        ):
            ttk.Checkbutton(left, text=txt, variable=var(key, tk.BooleanVar)).pack(anchor="w")

        frow = ttk.Frame(left)
        frow.pack(fill="x", pady=(3, 2))
        ttk.Label(frow, text="Type").pack(side="left")
        ft_labels = dict(_FILE_TYPE_CHOICES)
        self.ft_label_var = tk.StringVar(value=ft_labels.get(self.get("file_type"), "All supported"))
        ft_combo = ttk.Combobox(frow, textvariable=self.ft_label_var, values=[l for _k, l in _FILE_TYPE_CHOICES],
                                state="readonly", width=13)
        ft_combo.pack(side="left", padx=4, fill="x", expand=True)
        ft_combo.bind("<<ComboboxSelected>>", self._on_file_type_selected)

        erow = ttk.Frame(left)
        erow.pack(fill="x", pady=2)
        ttk.Label(erow, text="Exts").pack(side="left")
        self.ext_entry = ttk.Entry(erow, width=14, textvariable=var("extensions"))
        self.ext_entry.pack(side="left", padx=4, fill="x", expand=True)
        autocomplete(self.ext_entry, "similarity_search.extensions")

        ibar = ttk.Frame(left)
        ibar.pack(fill="x", pady=(4, 0))
        idx_b = ttk.Button(ibar, text="⚡ Index / Check", style="Accent.TButton", command=self.index_sources)
        idx_b.pack(fill="x")
        Tooltip(idx_b, "Index sources & check for modified cached files")
        reocr_b = ttk.Button(ibar, text="↻ Re-OCR all", command=self.force_reocr_all)
        reocr_b.pack(fill="x", pady=(3, 0))
        Tooltip(reocr_b, "Force re-OCR on all active sources")

        # --- Right: query + results + inspector
        qbox = ttk.LabelFrame(right, text="Search by text, file, folder, or clipboard image", padding=(6, 4))
        qbox.pack(fill="x")

        r1 = ttk.Frame(qbox)
        r1.pack(fill="x")
        ttk.Label(r1, text="Mode").pack(side="left")
        mode_map = dict(SEARCH_MODES)
        self.mode_label_var = tk.StringVar(value=mode_map.get(self.get("mode"), SEARCH_MODES[0][1]))
        mode_combo = ttk.Combobox(r1, textvariable=self.mode_label_var, values=[l for _k, l in SEARCH_MODES],
                                  state="readonly", width=18)
        mode_combo.pack(side="left", padx=(4, 6))
        mode_combo.bind("<<ComboboxSelected>>", self._on_mode_selected)

        ttk.Label(r1, text="Min %").pack(side="left")
        ttk.Spinbox(r1, from_=20, to=100, width=4, textvariable=var("threshold", tk.IntVar)).pack(side="left", padx=(2, 4))
        chk_all = ttk.Checkbutton(r1, text="AND", variable=var("match_all_lines", tk.BooleanVar))
        chk_all.pack(side="left")
        Tooltip(chk_all, "Require all query lines to match (AND)")
        chk_tol = ttk.Checkbutton(r1, text="OCR-tol", variable=var("ocr_tolerant", tk.BooleanVar))
        chk_tol.pack(side="left", padx=3)
        Tooltip(chk_tol, "OCR-tolerant matching (rn/m, 0/O, 1/l, Arabic/accents)")
        ttk.Checkbutton(r1, text="Live", variable=var("live_search", tk.BooleanVar)).pack(side="left")

        self.recent_btn = ttk.Menubutton(r1, text="Recent ▾", style="Tiny.TMenubutton")
        self.recent_menu = tk.Menu(self.recent_btn, tearoff=False)
        self.recent_btn["menu"] = self.recent_menu
        self.recent_btn.pack(side="right", padx=(4, 0))
        srch_b = ttk.Button(r1, text="🔍 Search", style="Accent.TButton", command=self.run_search)
        srch_b.pack(side="right")
        Tooltip(srch_b, "Run search (F5)")

        self.query_text = tk.Text(qbox, height=2, wrap="word", undo=True)
        self.query_text.pack(fill="x", pady=(4, 3))
        style_text(right, self.query_text)
        autocomplete(self.query_text, "similarity_search.query")
        self.query_text.bind("<Control-Return>", lambda _e: (self.run_search(), "break")[1])
        self.query_text.bind("<KeyRelease>", lambda _e: self._schedule_live_search())

        r3 = ttk.Frame(qbox)
        r3.pack(fill="x")
        ttk.Button(r3, text="📄 File…", style="Tiny.TButton", command=self.pick_query_file).pack(side="left")
        ttk.Button(r3, text="📁 Folder…", style="Tiny.TButton", command=self.pick_query_folder).pack(side="left", padx=3)
        ttk.Button(r3, text="📋 Clipboard", style="Tiny.TButton", command=self.query_from_clipboard_image).pack(side="left")
        ttk.Button(r3, text="Clear", style="Tiny.TButton", command=self.clear_query).pack(side="left", padx=3)

        self.query_src_lbl = ttk.Label(r3, text="", style="Info.TLabel")
        self.query_src_lbl.pack(side="left", padx=4)

        self.exclude_var = tk.StringVar(value="")
        self.exclude_entry = ttk.Entry(r3, width=15, textvariable=self.exclude_var)
        self.exclude_entry.pack(side="right")
        autocomplete(self.exclude_entry, "similarity_search.exclude")
        ttk.Label(r3, text="Exclude:", style="Muted.TLabel").pack(side="right", padx=(6, 4))
        self.exclude_var.trace_add("write", lambda *_a: self._schedule_live_search())

        # Action bar + dedicated summary line so buttons never squeeze
        abar = ttk.Frame(right)
        abar.pack(fill="x", pady=(4, 2))
        ttk.Button(abar, text="Copy files…", style="Tiny.TButton", command=self.copy_matching_files).pack(side="left")
        ttk.Button(abar, text="↩ Undo copy", style="Tiny.TButton", command=self.undo_copy).pack(side="left", padx=3)
        ttk.Button(abar, text="Excel…", style="Tiny.TButton", command=self.export_excel).pack(side="left")
        ttk.Button(abar, text="Add to file…", style="Tiny.TButton", command=self.add_to_file).pack(side="left", padx=3)
        ttk.Button(abar, text="Copy results", style="Tiny.TButton",
                   command=lambda: self.results.copy_selected(all_rows=True)).pack(side="left")

        pv = var("preview", tk.BooleanVar)
        ttk.Checkbutton(abar, text="Preview", variable=pv, command=self._apply_preview_setting).pack(side="right")
        self.summary = ttk.Label(right, style="Muted.TLabel", text="Add files/folders to index and search.")
        self.summary.pack(anchor="w", pady=(0, 2))

        # Results + Inspector PanedWindow
        self.vpane = ttk.PanedWindow(right, orient="vertical")
        self.vpane.pack(fill="both", expand=True)

        top_res = ttk.Frame(self.vpane)
        self.vpane.add(top_res, weight=3)
        self.results = DataTable(
            top_res,
            [
                ("score", "Score", 52, "e"),
                ("band", "Match", 64),
                ("file", "File", 130),
                ("page", "Pg", 40, "e"),
                ("hits", "Hits", 40, "e"),
                ("snippet", "Matched snippet", 170),
                ("source", "Source", 80),
                ("query", "Query", 90),
                ("folder", "Folder", 130),
            ],
            height=6,
            searchable=True,
            stretch="snippet",
            on_select=self._on_result_selected,
            on_double=lambda iid: self._open_hit(iid),
        )
        self.results.pack(fill="both", expand=True)
        self.results.tag_colors(exact="ok", high="info", medium="warn", low="muted")

        m = self.results.menu
        m.add_separator()
        m.add_command(label="Open file", command=lambda: self._with_selected_hit(lambda h: not h.is_virtual and open_path(h.path)))
        m.add_command(label="Show in folder", command=lambda: self._with_selected_hit(lambda h: not h.is_virtual and reveal(h.path)))
        m.add_command(label="Find more like this", command=self.find_more_like_selected)
        m.add_separator()
        m.add_command(label="Copy file path(s)", command=self.copy_selected_paths)
        m.add_command(label="Copy matched snippet", command=self.copy_selected_snippet)
        m.add_command(label="Copy full OCR text", command=self.copy_doc_text)
        m.add_command(label="Copy matching files to folder…", command=self.copy_matching_files)
        m.add_command(label="Force re-OCR this file", command=self.force_reocr_selected)
        send_menu = tk.Menu(m, tearoff=False)
        for tid, label in (
            ("ocr_match", "Find text"),
            ("passport", "Passports"),
            ("pdf_desk", "PDF Desk"),
            ("docpacker", "DocPacker"),
            ("scan_cleaner", "Scan Cleaner"),
        ):
            send_menu.add_command(label=label, command=lambda t=tid: self._send_selected_to(t))
        m.add_cascade(label="Send to", menu=send_menu)

        # Bottom inspector
        self.preview_box = ttk.Frame(self.vpane)
        self.inspect_nb = ttk.Notebook(self.preview_box)
        self.inspect_nb.pack(fill="both", expand=True)

        text_tab = ttk.Frame(self.inspect_nb)
        self.inspect_nb.add(text_tab, text="OCR Text & Highlights")
        tbar = ttk.Frame(text_tab)
        tbar.pack(fill="x", pady=(2, 4))
        ttk.Button(tbar, text="◀ Prev", style="Tiny.TButton", command=lambda: self._step_hit(-1)).pack(side="left")
        ttk.Button(tbar, text="Next ▶", style="Tiny.TButton", command=lambda: self._step_hit(1)).pack(side="left", padx=4)
        self.hit_pos_lbl = ttk.Label(tbar, text="", style="Muted.TLabel")
        self.hit_pos_lbl.pack(side="left", padx=6)
        ttk.Button(tbar, text="Save text…", style="Tiny.TButton", command=self.save_doc_text).pack(side="right")
        ttk.Button(tbar, text="Copy text", style="Tiny.TButton", command=self.copy_doc_text).pack(side="right", padx=4)
        ttk.Button(tbar, text="Copy snippet", style="Tiny.TButton", command=self.copy_selected_snippet).pack(side="right")

        tf = ttk.Frame(text_tab)
        tf.pack(fill="both", expand=True)
        self.doc_text = tk.Text(tf, wrap="word", height=6)
        tsb = ttk.Scrollbar(tf, command=self.doc_text.yview)
        self.doc_text.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self.doc_text.pack(fill="both", expand=True)
        style_text(right, self.doc_text)
        p = self.app.theme.p
        self.doc_text.tag_configure("hit", background=p.get("accent_soft", "#dbeafe"))
        self.doc_text.tag_configure("hit_cur", background=p.get("warn", "#fbbf24"), foreground="#000000")

        prev_tab = ttk.Frame(self.inspect_nb)
        self.inspect_nb.add(prev_tab, text="Document Preview")
        self.viewer = PageViewer(prev_tab, self.app, show_toolbar=True, max_pages=10)
        self.viewer.pack(fill="both", expand=True)

        self._apply_preview_setting()
        self._show_sources()
        self._refresh_recent_menu()
        self._refresh_cache_btn()

    def _store_var(self, key: str, v: tk.Variable) -> None:
        try:
            self.put(key, v.get())
        except tk.TclError:
            pass

    def _on_file_type_selected(self, _e=None) -> None:
        lbl = self.ft_label_var.get()
        for k, l in _FILE_TYPE_CHOICES:
            if l == lbl:
                self.put("file_type", k)
                break
        if self.active_docs:
            self._schedule_live_search()

    def _on_mode_selected(self, _e=None) -> None:
        lbl = self.mode_label_var.get()
        for k, l in SEARCH_MODES:
            if l == lbl:
                self.put("mode", k)
                break
        if self.active_docs:
            self._schedule_live_search()

    def _apply_preview_setting(self) -> None:
        if not hasattr(self, "vpane"):
            return
        shown = str(self.preview_box) in [str(p) for p in self.vpane.panes()]
        want = bool(self.get("preview"))
        if want and not shown:
            self.vpane.add(self.preview_box, weight=2)
        elif not want and shown:
            self.vpane.forget(self.preview_box)

    def _refresh_cache_btn(self) -> None:
        if hasattr(self, "cache_btn"):
            n = self.store.stats()["docs"]
            self.cache_btn.configure(text=f"💾 {n}")

    def _invalidate_index(self) -> None:
        self._corpus_index = None

    def _drop_missing_active(self) -> None:
        for np in list(self.active_docs):
            doc = self.active_docs[np]
            if not doc.is_virtual and not Path(doc.path).is_file():
                del self.active_docs[np]
        self._invalidate_index()

    # ------------------------------------------------------------------ sources
    def sources(self) -> list[str]:
        items = [s for s in (self.get("sources") or []) if Path(s).exists()]
        if not items:
            inp = self.input_dir()
            if inp:
                return [inp]
        return items

    def _show_sources(self) -> None:
        if not self.built:
            return
        self.src_list.delete(0, "end")
        for s in self.get("sources") or []:
            self.src_list.insert("end", s)
        for vkey, doc in self.pasted_docs.items():
            self.src_list.insert("end", f"[Text] {doc.name}")
        n_real = len(self.get("sources") or [])
        n_txt = len(self.pasted_docs)
        parts = []
        if n_real:
            parts.append(f"{n_real} path(s)")
        if n_txt:
            parts.append(f"{n_txt} text snippet(s)")
        self.src_lbl.configure(text=", ".join(parts) + " configured." if parts else "Add folders, files, or pasted text.")

    def _add_sources(self, paths: list[str]) -> None:
        cur = list(self.get("sources") or [])
        added = False
        for p in paths:
            if p and p not in cur:
                cur.append(p)
                added = True
        if added:
            self.put("sources", cur)
            self._show_sources()

    def add_folder(self) -> None:
        path = filedialog.askdirectory(title="Add folder to index", initialdir=self.input_dir())
        if path:
            self.accept_files([path])

    def add_files(self) -> None:
        exts = " ".join(f"*{e}" for e in supported_extensions())
        paths = filedialog.askopenfilenames(
            title="Add files to index",
            initialdir=self.input_dir(),
            filetypes=[("Supported documents & images", exts), ("All", "*.*")],
        )
        if paths:
            self.accept_files(list(paths))

    def add_pasted_text(self) -> None:
        self._paste_seq += 1
        win, body = modal(self.frame, "Add pasted text to corpus", 560, 380)
        ttk.Label(body, text="Title").pack(anchor="w")
        title_var = tk.StringVar(value=f"Pasted text {self._paste_seq}")
        title_entry = ttk.Entry(body, textvariable=title_var)
        title_entry.pack(fill="x", pady=(2, 8))
        autocomplete(title_entry, "similarity_search.pasted_title")
        ttk.Label(body, text="Text").pack(anchor="w")
        txt = tk.Text(body, height=10, wrap="word")
        txt.pack(fill="both", expand=True, pady=(2, 8))
        style_text(win, txt)
        autocomplete(txt, "similarity_search.pasted_text")
        txt.focus_set()

        def ok() -> None:
            content = txt.get("1.0", "end-1c")
            if not content.strip():
                win.destroy()
                return
            doc = make_pasted_doc(title_var.get(), content, key=f"pasted://{self._paste_seq}")
            self.pasted_docs[doc.norm_path] = doc
            self.active_docs[doc.norm_path] = doc
            self._invalidate_index()
            self._show_sources()
            win.destroy()
            if self._has_query():
                self.run_search()

        bar = ttk.Frame(body)
        bar.pack(fill="x")
        ttk.Button(bar, text="Add", style="Accent.TButton", command=ok).pack(side="right")
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        win.bind("<Escape>", lambda _e: win.destroy())
        win.grab_set()
        win.wait_window()

    def add_pasted_snippet(self, title: str, text: str) -> CachedDoc:
        """Programmatic helper to add a pasted snippet directly to the search corpus."""
        self._paste_seq += 1
        doc = make_pasted_doc(title, text, key=f"pasted://{self._paste_seq}")
        self.pasted_docs[doc.norm_path] = doc
        self.active_docs[doc.norm_path] = doc
        self._invalidate_index()
        self._show_sources()
        return doc

    def remove_sources(self) -> None:
        cur = list(self.get("sources") or [])
        pasted_keys = list(self.pasted_docs)
        for idx in sorted(self.src_list.curselection(), reverse=True):
            if idx < len(cur):
                del cur[idx]
            else:
                pi = idx - len(cur)
                if 0 <= pi < len(pasted_keys):
                    k = pasted_keys[pi]
                    self.pasted_docs.pop(k, None)
                    self.active_docs.pop(k, None)
        self.put("sources", cur)
        if not self.get("include_all_cached"):
            keep_real = {norm_path(p) for p in self._collect_target_files()} if self.sources() else set()
            self.active_docs = {
                k: d for k, d in self.active_docs.items()
                if d.is_virtual or k in keep_real
            }
        self._invalidate_index()
        self._show_sources()

    def clear_sources(self) -> None:
        self.put("sources", [])
        self.pasted_docs.clear()
        self.active_docs.clear()
        self.hits.clear()
        self._invalidate_index()
        if self.built:
            self.results.clear()
            self.doc_text.delete("1.0", "end")
            self.summary.configure(text="Cleared sources (cached OCR text remains stored).")
            self._show_sources()

    def accept_files(self, paths: list[str]) -> None:
        if not paths:
            return
        self._add_sources(list(paths))
        if self.get("auto_index"):
            self.index_sources()

    # ------------------------------------------------------------------ smart indexing & change prompt
    def _collect_target_files(self) -> list[Path]:
        srcs = self.sources()
        exts = parse_ext_list(self.get("extensions"))
        rec = bool(self.get("recursive"))
        ftype = self.get("file_type") or "all"
        return collect_search_files(srcs, recursive=rec, extensions=exts, file_type=ftype)

    def _ask_changed_files(self, changed: list[ChangedFile]) -> tuple[list[str], bool]:
        if self._changed_prompt is not None:
            res = self._changed_prompt(changed)
            if res is None:
                return [], True
            return res
        dlg = ChangedFilesDialog(self.frame, changed, self)
        return dlg.chosen, dlg.cancelled

    def index_sources(self, then_search: bool = False, force: bool = False) -> None:
        files = self._collect_target_files()
        include_cached = bool(self.get("include_all_cached"))
        if not files and not include_cached and not self.pasted_docs:
            info(self.frame, "Add a folder, file, or pasted text first.")
            return

        cfg = ocr.OcrConfig.from_settings(self.settings)
        ocr_sig = ocr_config_signature(cfg)
        check_sig = bool(self.get("reocr_on_cfg_change"))

        if force:
            plan_cached: list[CachedDoc] = []
            to_ocr = [str(f) for f in files]
            reused_count = 0
        else:
            plan = classify_files(files, self.store, ocr_sig=ocr_sig, check_ocr_sig=check_sig)
            plan_cached = list(plan.cached)
            reused_count = plan.reused_count
            to_ocr = list(plan.new_files)

            if plan.changed:
                policy = self.get("on_file_changed") or "ask"
                if policy == "auto":
                    to_ocr.extend(cf.path for cf in plan.changed)
                elif policy == "keep":
                    for cf in plan.changed:
                        cf.cached_doc.origin = "cached"
                        plan_cached.append(cf.cached_doc)
                else:
                    chosen, cancelled = self._ask_changed_files(plan.changed)
                    if cancelled:
                        return
                    chosen_set = {norm_path(p) for p in chosen}
                    for cf in plan.changed:
                        if cf.norm_path in chosen_set:
                            to_ocr.append(cf.path)
                        else:
                            cf.cached_doc.origin = "cached"
                            plan_cached.append(cf.cached_doc)

        def finish(fresh_docs: list[CachedDoc]) -> None:
            merged: dict[str, CachedDoc] = {}
            if include_cached:
                for d in self.store.all_docs(existing_only=True):
                    merged[d.norm_path] = d
            for d in plan_cached:
                merged[d.norm_path] = d
            for d in fresh_docs:
                merged[d.norm_path] = d
            for k, d in self.pasted_docs.items():
                merged[k] = d
            self.active_docs = merged
            self._invalidate_index()
            self._refresh_cache_btn()

            err_count = sum(1 for d in fresh_docs if d.error)
            msg = (
                f"{len(self.active_docs)} doc(s) ready "
                f"({len(plan_cached)} cached{f', {reused_count} reused' if reused_count else ''}, "
                f"{len(fresh_docs)} OCR'd{f', {err_count} error(s)' if err_count else ''})."
            )
            if self.built:
                self.summary.configure(text=msg)
            self.log(msg, "warn" if err_count else "ok")
            if then_search and self._has_query():
                self.run_search()

        if not to_ocr:
            finish([])
            return

        opts = ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=False, deskew=cfg.deskew)
        reader_fn = self._reader_fn

        def work(job):
            return index_files(
                to_ocr,
                self.store,
                cfg,
                opts,
                cancelled=lambda: job.cancelled,
                progress=job.progress,
                reader_fn=reader_fn,
            )

        if self.built:
            self.summary.configure(text=f"Reading / OCRing {len(to_ocr)} file(s)…")
        self.app.jobs.submit(
            work,
            f"OCR & indexing {len(to_ocr)} file(s)",
            tool=self.id,
            on_done=finish,
            on_error=lambda m: error(self.frame, f"Indexing failed: {m}"),
            serial_key="similarity_search.index",
        )

    def force_reocr_all(self) -> None:
        self.index_sources(force=True)

    def force_reocr_selected(self) -> None:
        hit = self._selected_hit()
        if hit is None or hit.is_virtual:
            return
        cfg = ocr.OcrConfig.from_settings(self.settings)
        opts = ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=False, deskew=cfg.deskew)
        reader_fn = self._reader_fn

        def work(job):
            return index_files([hit.path], self.store, cfg, opts, cancelled=lambda: job.cancelled,
                               progress=job.progress, reader_fn=reader_fn)

        def done(docs: list[CachedDoc]) -> None:
            for d in docs:
                self.active_docs[d.norm_path] = d
            self._invalidate_index()
            self._refresh_cache_btn()
            if self._has_query():
                self.run_search()

        self.app.jobs.submit(
            work,
            f"Re-OCRing {hit.name}",
            tool=self.id,
            on_done=done,
            on_error=lambda m: error(self.frame, str(m)),
            serial_key="similarity_search.index",
        )

    # ------------------------------------------------------------------ query sources
    def _has_query(self) -> bool:
        if self.query_files:
            return True
        return self.built and bool(self.query_text.get("1.0", "end-1c").strip())

    def clear_query(self) -> None:
        self.query_files.clear()
        if self.built:
            self.query_src_lbl.configure(text="")
            self.query_text.delete("1.0", "end")
            self.exclude_var.set("")

    def pick_query_file(self) -> None:
        exts = " ".join(f"*{e}" for e in supported_extensions())
        path = filedialog.askopenfilename(
            title="Pick a query file to search with",
            initialdir=self.input_dir(),
            filetypes=[("Supported documents & images", exts), ("All", "*.*")],
        )
        if path:
            self.search_with_paths([path])

    def pick_query_folder(self) -> None:
        folder = filedialog.askdirectory(title="Pick a query folder of files", initialdir=self.input_dir())
        if folder:
            self.search_with_paths([folder])

    def search_with_paths(self, paths: list[str]) -> None:
        """Use file(s) or folder(s) as query input (cached smartly via OcrTextStore)."""
        files = collect_search_files(paths, recursive=True)
        if not files:
            info(self.frame, "No supported files found in that selection.")
            return
        self.query_files = [str(f) for f in files]
        if self.built:
            label = files[0].name if len(files) == 1 else f"{len(files)} query files"
            self.query_src_lbl.configure(text=f"Querying with: {label}")
        self.run_search()

    def query_from_clipboard_image(self) -> None:
        img = self._grab_clipboard_image()
        if img is None:
            self.app.notify("No image found on clipboard", "warn")
            return
        cfg = ocr.OcrConfig.from_settings(self.settings)

        def work(_job):
            return ocr.ocr_image(img, cfg)

        def done(text: str) -> None:
            self.query_files.clear()
            if self.built:
                self.query_src_lbl.configure(text="")
                self.query_text.delete("1.0", "end")
                self.query_text.insert("1.0", (text or "").strip())
            if (text or "").strip():
                self.run_search()
            else:
                self.app.notify("No text could be read from the clipboard image", "warn")

        self.app.jobs.submit(work, "Reading clipboard image", tool=self.id, on_done=done,
                             on_error=lambda m: error(self.frame, f"Clipboard OCR failed: {m}"),
                             serial_key="similarity_search.clip")

    @staticmethod
    def _grab_clipboard_image() -> Image.Image | None:
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            return None
        if isinstance(data, Image.Image):
            return data
        if isinstance(data, list) and data and str(data[0]).lower().endswith(_IMAGE_CLIP_EXTS):
            try:
                return Image.open(data[0])
            except Exception:
                return None
        return None

    # ------------------------------------------------------------------ searching
    def _schedule_live_search(self) -> None:
        if not self.built or not self.get("live_search") or self.query_files:
            return
        if self._live_after:
            self.app.root.after_cancel(self._live_after)
        self._live_after = self.app.root.after(260, self._run_live_if_ready)

    def _run_live_if_ready(self) -> None:
        self._live_after = None
        if self.active_docs and self.query_text.get("1.0", "end-1c").strip():
            self.run_search(live=True)

    def _search_options(self) -> SearchOptions:
        raw_ex = [t.strip() for t in self.exclude_var.get().replace(";", ",").split(",") if t.strip()] if self.built else []
        return SearchOptions(
            mode=self.get("mode") or "smart",
            threshold=max(0.1, min(1.0, (int(self.get("threshold") or 60)) / 100.0)),
            ocr_tolerant=bool(self.get("ocr_tolerant")),
            ignore_case=bool(self.get("ignore_case")),
            match_all_lines=bool(self.get("match_all_lines")),
            top_k=int(self.get("top_k") or 200),
            file_type=self.get("file_type") or "all",
            exclude_terms=raw_ex,
        )

    def _ensure_corpus_loaded(self) -> bool:
        if self.active_docs:
            return True
        if self.get("include_all_cached"):
            self.active_docs = {d.norm_path: d for d in self.store.all_docs(existing_only=True)}
        for k, d in self.pasted_docs.items():
            self.active_docs[k] = d
        if self.active_docs:
            self._invalidate_index()
            return True
        return False

    def _remember_query(self, raw: str) -> None:
        q = " ".join(raw.split())[:140]
        if not q:
            return
        cur = [x for x in (self.get("recent_queries") or []) if x != q]
        cur.insert(0, q)
        self.put("recent_queries", cur[:15])
        self._refresh_recent_menu()

    def _refresh_recent_menu(self) -> None:
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.delete(0, "end")
        items = list(self.get("recent_queries") or [])
        if not items:
            self.recent_menu.add_command(label="(no recent searches)", state="disabled")
            return
        for q in items:
            self.recent_menu.add_command(label=q[:70], command=lambda s=q: self._use_recent(s))

    def _use_recent(self, q: str) -> None:
        self.query_files.clear()
        self.query_src_lbl.configure(text="")
        self.query_text.delete("1.0", "end")
        self.query_text.insert("1.0", q)
        self.run_search()

    def run_search(self, live: bool = False) -> None:
        if not self._ensure_corpus_loaded():
            if self.sources():
                self.index_sources(then_search=True)
            elif not live:
                info(self.frame, "Add files, folders, or pasted text to search first.")
            return

        if not self.query_files:
            raw_q = self.query_text.get("1.0", "end-1c")
            if not raw_q.strip():
                if not live:
                    info(self.frame, "Type a query, or pick a file / folder / clipboard image.")
                return
            if not live:
                self._remember_query(raw_q)

        opts = self._search_options()
        corpus = list(self.active_docs.values())
        if self._corpus_index is None:
            self._corpus_index = CorpusIndex(corpus, opts.ocr_tolerant)
        idx = self._corpus_index

        if not self.query_files:
            def work(_job):
                return search_documents(corpus, raw_q, opts, index=idx)

            self.app.jobs.submit(
                work,
                "Searching",
                tool=self.id,
                on_done=self._show_hits,
                on_error=lambda m: error(self.frame, str(m)),
                serial_key="similarity_search.find",
            )
            return

        # Querying with file(s) or folder(s): classify & OCR any uncached query files cleanly first
        cfg = ocr.OcrConfig.from_settings(self.settings)
        ocr_sig = ocr_config_signature(cfg)
        qplan = classify_files(self.query_files, self.store, ocr_sig=ocr_sig, check_ocr_sig=bool(self.get("reocr_on_cfg_change")))
        q_cached = list(qplan.cached)
        q_to_ocr = list(qplan.new_files)

        if qplan.changed:
            policy = self.get("on_file_changed") or "ask"
            if policy == "auto":
                q_to_ocr.extend(cf.path for cf in qplan.changed)
            elif policy == "keep":
                q_cached.extend(cf.cached_doc for cf in qplan.changed)
            else:
                chosen, cancelled = self._ask_changed_files(qplan.changed)
                if cancelled:
                    return
                cset = {norm_path(p) for p in chosen}
                for cf in qplan.changed:
                    if cf.norm_path in cset:
                        q_to_ocr.append(cf.path)
                    else:
                        q_cached.append(cf.cached_doc)

        read_opts = ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=False, deskew=cfg.deskew)
        reader_fn = self._reader_fn

        def work_files(job):
            fresh = (
                index_files(
                    q_to_ocr,
                    self.store,
                    cfg,
                    read_opts,
                    cancelled=lambda: job.cancelled,
                    progress=job.progress,
                    reader_fn=reader_fn,
                )
                if q_to_ocr
                else []
            )
            qdocs = q_cached + fresh
            return search_by_documents(corpus, qdocs, opts, exclude_self=True)

        self.app.jobs.submit(
            work_files,
            f"Searching with {len(self.query_files)} file(s)",
            tool=self.id,
            on_done=lambda hits: (self._refresh_cache_btn(), self._show_hits(hits)),
            on_error=lambda m: error(self.frame, str(m)),
            serial_key="similarity_search.find",
        )

    def _show_hits(self, hits: list[SearchHit] | None) -> None:
        if not self.built:
            return
        self.results.clear()
        self.hits.clear()
        rows = hits or []
        for h in rows:
            tag = "exact" if h.score >= 0.97 else ("high" if h.score >= 0.85 else ("medium" if h.score >= 0.70 else "low"))
            iid = self.results.add(
                {
                    "score": f"{h.score * 100:.0f}%",
                    "band": h.band,
                    "file": h.name,
                    "page": h.page,
                    "hits": h.hit_count,
                    "snippet": h.snippet,
                    "source": h.source_kind,
                    "query": h.query,
                    "folder": h.folder,
                },
                tags=(tag,),
            )
            self.hits[iid] = h

        n_files = len({h.path for h in rows})
        self.summary.configure(text=f"{len(rows)} match(es) across {n_files} file(s) (corpus: {len(self.active_docs)} doc(s)).")
        ids = self.results.all_ids()
        if ids:
            self.results.tree.selection_set(ids[0])
            self._on_result_selected([ids[0]])
        else:
            self.doc_text.delete("1.0", "end")
            self.hit_pos_lbl.configure(text="")
            self.viewer.clear()

    # ------------------------------------------------------------------ inspector & highlights
    def _selected_hit(self) -> SearchHit | None:
        if not self.built:
            return None
        sel = self.results.selection()
        return self.hits.get(sel[0]) if sel else None

    def _with_selected_hit(self, fn: Callable[[SearchHit], None]) -> None:
        h = self._selected_hit()
        if h is not None:
            fn(h)

    def _on_result_selected(self, iids: list[str]) -> None:
        if not iids:
            return
        hit = self.hits.get(iids[0])
        if hit is None:
            return
        doc = self.active_docs.get(hit.path if hit.is_virtual else norm_path(hit.path))
        if doc is None:
            doc = self.store.get_by_path(hit.path)
        self.doc_text.delete("1.0", "end")
        self._hit_spans = []
        self._hit_idx = 0

        if doc is not None:
            display_txt, page_starts = format_doc_pages(doc)
            self.doc_text.insert("1.0", display_txt)
            terms = list(hit.highlight_terms or ([hit.matched_text] if hit.matched_text else []))
            self._hit_spans = find_highlight_spans(display_txt, terms, ignore_case=bool(self.get("ignore_case")))
            for lno, c0, c1 in self._hit_spans:
                self.doc_text.tag_add("hit", f"{lno}.{c0}", f"{lno}.{c1}")
            if self._hit_spans:
                # Jump to the first span on or after the matched page if available
                target_line = dict(page_starts).get(hit.page, 1)
                idx = next((i for i, sp in enumerate(self._hit_spans) if sp[0] >= target_line), 0)
                self._focus_span(idx)
            else:
                target_line = dict(page_starts).get(hit.page, 1)
                self.doc_text.see(f"{target_line}.0")
                self.hit_pos_lbl.configure(text=f"Page {hit.page}")
        else:
            self.doc_text.insert("1.0", hit.snippet)
            self.hit_pos_lbl.configure(text="")

        if self.get("preview") and not hit.is_virtual and Path(hit.path).is_file():
            self.viewer.show(hit.path)
            if hit.page > 1:
                self.app.root.after(200, lambda p=hit.page - 1: self.viewer.goto(p))
        else:
            self.viewer.clear("No file preview for pasted text" if hit.is_virtual else "")

    def _focus_span(self, idx: int) -> None:
        if not self._hit_spans:
            self.hit_pos_lbl.configure(text="0 hits")
            return
        idx %= len(self._hit_spans)
        self._hit_idx = idx
        self.doc_text.tag_remove("hit_cur", "1.0", "end")
        lno, c0, c1 = self._hit_spans[idx]
        self.doc_text.tag_add("hit_cur", f"{lno}.{c0}", f"{lno}.{c1}")
        self.doc_text.see(f"{lno}.{c0}")
        self.hit_pos_lbl.configure(text=f"{idx + 1} / {len(self._hit_spans)}")

    def _step_hit(self, delta: int) -> None:
        if not self._hit_spans:
            return
        self._focus_span(self._hit_idx + delta)

    def _open_hit(self, iid: str) -> None:
        h = self.hits.get(iid)
        if h is not None and not h.is_virtual and Path(h.path).is_file():
            open_path(h.path)

    # ------------------------------------------------------------------ QoL actions & exports
    def find_more_like_selected(self) -> None:
        h = self._selected_hit()
        if h is None:
            return
        if h.is_virtual:
            doc = self.pasted_docs.get(h.path)
            if doc is not None:
                self.query_files.clear()
                self.query_src_lbl.configure(text="")
                self.query_text.delete("1.0", "end")
                self.query_text.insert("1.0", doc.text[:4000])
                self.run_search()
            return
        self.search_with_paths([h.path])

    def copy_selected_paths(self) -> None:
        sel = self.results.selection() or self.results.all_ids()
        paths = [self.hits[i].path for i in sel if i in self.hits and not self.hits[i].is_virtual]
        if paths:
            copy_to_clipboard(self.frame, "\n".join(dict.fromkeys(paths)))
            self.app.notify(f"Copied {len(paths)} path(s)")

    def copy_selected_snippet(self) -> None:
        h = self._selected_hit()
        if h is not None:
            copy_to_clipboard(self.frame, h.snippet)
            self.app.notify("Snippet copied")

    def copy_doc_text(self) -> None:
        if self.built:
            copy_to_clipboard(self.frame, self.doc_text.get("1.0", "end-1c"))
            self.app.notify("Text copied")

    def save_doc_text(self) -> None:
        if not self.built:
            return
        h = self._selected_hit()
        init_name = f"{Path(h.name).stem}_ocr.txt" if h else "ocr-text.txt"
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialdir=self.output_dir(),
            initialfile=init_name,
            filetypes=[("Text", "*.txt")],
        )
        if path:
            Path(path).write_text(self.doc_text.get("1.0", "end-1c"), encoding="utf-8")

    def _result_records(self) -> list[dict]:
        ids = self.results.all_ids()
        out: list[dict] = []
        for iid in ids:
            h = self.hits.get(iid)
            if h is None:
                continue
            out.append(
                {
                    "Score %": f"{h.score * 100:.0f}",
                    "Match": h.band,
                    "File": h.name,
                    "Page": h.page,
                    "Hits": h.hit_count,
                    "Snippet": h.snippet,
                    "Source": h.source_kind,
                    "Query": h.query,
                    "Why": h.why,
                    "Path": h.path,
                }
            )
        return out

    def export_excel(self) -> None:
        recs = self._result_records()
        if not recs:
            info(self.frame, "Search first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            initialdir=self.output_dir(),
            initialfile="similarity-results.xlsx",
            filetypes=[("Excel", "*.xlsx")],
        )
        if not path:
            return
        heads = list(recs[0])
        write_xlsx(path, {"Results": [heads] + [[r[h] for h in heads] for r in recs]})
        self.log(f"Exported {len(recs)} result(s) to {path}", "ok")
        self.app.notify(f"Saved {Path(path).name}", "ok")

    def add_to_file(self) -> None:
        recs = self._result_records()
        if not recs:
            info(self.frame, "Search first.")
            return
        self.append_rows(recs, ask=True)

    def copy_matching_files(self) -> None:
        ids = self.results.selection() or self.results.all_ids()
        paths = list(
            dict.fromkeys(
                self.hits[i].path
                for i in ids
                if i in self.hits and not self.hits[i].is_virtual and Path(self.hits[i].path).is_file()
            )
        )
        if not paths:
            info(self.frame, "No matching files on disk to copy.")
            return
        dest = filedialog.askdirectory(title="Copy matching files to…", initialdir=self.output_dir())
        if not dest:
            return
        dest_dir = Path(dest)
        batch, journal = new_batch(), self.app.journal

        def work(job):
            copied, failed = [], []
            for n, p in enumerate(paths, 1):
                if job.cancelled:
                    break
                job.progress(n, len(paths), Path(p).name)
                try:
                    transfer(Path(p), dest_dir, None, "copy", "number", journal, self.id,
                             note=batch_note(batch, "similarity search match"))
                    copied.append(p)
                except OSError as exc:
                    failed.append((p, str(exc)))
            return copied, failed

        def done(res) -> None:
            copied, failed = res
            self.log(f"Copied {len(copied)} file(s) to {dest_dir}" + (f", {len(failed)} failed" if failed else ""),
                     "warn" if failed else "ok")
            self.app.notify(f"Copied {len(copied)} file(s) (undo available)", "ok")

        self.app.jobs.submit(work, f"Copying {len(paths)} file(s)", tool=self.id, on_done=done,
                             on_error=lambda m: error(self.frame, m))

    def undo_copy(self) -> None:
        j = self.app.journal
        batch = j.last_batch(self.id)
        if batch:
            n, problems = j.undo_batch(batch, self.id)
            self.log(f"Removed {n} copied file(s)" + (f" ({len(problems)} error(s))" if problems else ""), "ok" if n else "warn")
            if problems:
                error(self.frame, "\n".join(problems[:8]))
            return
        e = j.last_undoable(self.id)
        if not e:
            info(self.frame, "Nothing to undo.")
            return
        self.log(j.undo(e), "ok")

    def _send_selected_to(self, target_id: str) -> None:
        ids = self.results.selection() or self.results.all_ids()
        paths = list(
            dict.fromkeys(
                self.hits[i].path
                for i in ids
                if i in self.hits and not self.hits[i].is_virtual and Path(self.hits[i].path).is_file()
            )
        )
        if paths and hasattr(self.app, "send_to"):
            self.app.send_to(target_id, paths)

    def open_cache_manager(self) -> None:
        CacheManagerDialog(self.frame, self)

    def _focus_query(self) -> None:
        if self.built:
            self.query_text.focus_set()

    # ------------------------------------------------------------------ lifecycle & app integration
    def on_show(self) -> None:
        self._refresh_cache_btn()

    def on_close(self) -> None:
        if self._live_after:
            try:
                self.app.root.after_cancel(self._live_after)
            except Exception:
                pass
            self._live_after = None
        self.store.close()

    def keys(self) -> dict:
        return {
            "<F5>": self.run_search,
            "<Control-o>": self.add_files,
            "<Control-Shift-O>": self.add_folder,
            "<Control-f>": self._focus_query,
            "<Control-l>": self.index_sources,
        }

    def commands(self) -> list[Command]:
        return [
            Command("simsearch.find", "Similarity Search: run search", self.run_search, self.title, "ocr similarity search"),
            Command("simsearch.add_folder", "Similarity Search: add folder…", self.add_folder, self.title, "folder index"),
            Command("simsearch.add_files", "Similarity Search: add files…", self.add_files, self.title, "files index"),
            Command("simsearch.query_file", "Similarity Search: search by file…", self.pick_query_file, self.title, "query file"),
            Command("simsearch.query_folder", "Similarity Search: search by folder…", self.pick_query_folder, self.title, "query folder"),
            Command("simsearch.index", "Similarity Search: index / check changes", self.index_sources, self.title, "ocr cache refresh"),
            Command("simsearch.cache", "Similarity Search: open OCR Cache Manager", self.open_cache_manager, self.title, "cache sqlite"),
        ]

    def stats(self) -> dict:
        return {"Sources": len(self.sources()) + len(self.pasted_docs), "Cached docs": self.store.stats()["docs"]}
