"""Badge Extractor: watch a folder for ID badges, OCR Arabic name/job title/badge number,
transliterate to English, check multiple similar candidates in an Excel sheet, and rename after review.
"""
from __future__ import annotations

import itertools
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable

from ..core import badges as B
from ..core import llm, ocr
from ..core.fileops import predicted_name, transfer
from ..core.schema import Opt
from ..core.sheets import SheetError
from ..core.system import open_path, reveal
from ..core.textutil import parse_ext_list
from ..core.watcher import FolderWatcher
from ..ui.viewer import PageViewer
from ..ui.widgets import DataTable, FolderEntry, Tooltip, confirm, copy_to_clipboard, error, info, modal, style_text
from .base import Command, Tool

_ids = itertools.count(1)

CSV_HEADERS = [
    "Arabic Name", "English Name", "Job Title", "Badge Number",
    "Matched Sheet Name", "Match %", "Excel Row", "Saved File", "Original File",
]


@dataclass
class BadgeItem:
    id: str
    path: Path
    status: str = "reading"                 # reading | pending | saved | skipped | error
    ar_name: str = ""
    en_name: str = ""
    job_title: str = ""
    badge_no: str = ""
    raw_text: str = ""
    ocr_source: str = ""
    warnings: list[str] = field(default_factory=list)
    rotation: int = 0
    matches: list[B.CandidateMatch] = field(default_factory=list)
    selected_match: B.CandidateMatch | None = None
    match_error: str = ""
    saved_path: Path | None = None
    error_msg: str = ""
    en_edited_manually: bool = False


class BadgeExtractor(Tool):
    id = "badge_extractor"
    title = "Badge Extractor"
    icon = "🪪"
    order = 48
    input_path_key = "input_folder"
    output_path_key = "output_folder"
    description = (
        "Watch a folder for ID badges: extract Arabic name, job title, and badge number, "
        "transliterate to English, check multiple similar Excel matches, and rename after review."
    )

    @classmethod
    def options(cls) -> list[Opt]:
        return [
            Opt("badge_extractor.input_folder", "", "Input / Watch folder", "folder", section="Folders",
                help="Folder watched for dropped badge images (and default folder for Add badges…)."),
            Opt("badge_extractor.output_folder", "", "Output folder", "folder", section="Folders",
                help="Destination folder where renamed badge images are saved."),
            Opt("badge_extractor.action", "move", "When saving a badge", "choice",
                choices=[("move", "Move to output folder"), ("copy", "Copy to output folder")],
                section="Folders"),
            Opt("badge_extractor.duplicate_policy", "number", "If the output file already exists", "choice",
                choices=[("number", "Keep both — add (2), (3)"), ("overwrite", "Replace (keep backup for Undo)"),
                         ("skip", "Skip")],
                section="Folders"),
            Opt("badge_extractor.watch_extensions", "jpg, jpeg, png, bmp, tif, tiff, webp",
                "Image extensions to watch", "str", section="Watching"),
            Opt("badge_extractor.quiet_seconds", 1.5, "Wait seconds for file copy to finish", "float",
                min=0.0, max=30.0, section="Watching"),
            Opt("badge_extractor.process_existing", False, "Load existing files when watching starts", "bool",
                section="Watching"),
            Opt("badge_extractor.filename_template", B.DEFAULT_FILENAME_TEMPLATE,
                "Filename template (without extension)", "str", section="Naming",
                help="Placeholders: {ar_name}, {en_name}, {badge_no}, {job_title}, {en_job_title}, "
                     "{matched_name}, {date}, {original}. Supports filters like {en_name|upper}."),
            Opt("badge_extractor.ocr_languages", "ara+eng", "OCR languages (Tesseract)", "str", section="OCR",
                help="Default 'ara+eng' reads Arabic name/job title plus English digits."),
            Opt("badge_extractor.crop_mode", "auto", "Badge text region", "choice",
                choices=B.CROP_MODE_CHOICES, section="OCR",
                help="'Auto' tries the right text area first (skipping the left photo) and falls back to full image."),
            Opt("badge_extractor.info_sheet", "", "Names / Candidates sheet (.xlsx / .csv)", "file",
                section="Candidate sheet",
                filetypes=[("Spreadsheets", "*.xlsx *.xlsm *.csv"), ("All", "*.*")],
                help="Excel/CSV sheet checked by similarity for each badge (and by the Candidate Info button)."),
            Opt("badge_extractor.info_tab", "", "Sheet tab name (empty = first tab)", "str",
                section="Candidate sheet"),
            Opt("badge_extractor.info_search_column", "", "Column heading to search (empty = auto-detect)", "str",
                section="Candidate sheet"),
            Opt("badge_extractor.info_header_row", 1, "Row that holds the column headings", "int",
                min=1, max=50, section="Candidate sheet"),
            Opt("badge_extractor.search_by", "en_name", "Default value to search by", "choice",
                choices=B.SEARCH_BY_CHOICES, section="Candidate sheet"),
            Opt("badge_extractor.similarity_threshold", 55, "Minimum similarity % to show", "int",
                min=30, max=100, section="Candidate sheet",
                help="Lower values surface more similar names with different percentages for manual review."),
            Opt("badge_extractor.max_matches", 10, "Maximum similar candidates to list", "int",
                min=1, max=50, section="Candidate sheet"),
            Opt("badge_extractor.use_docpacker_sheet_fallback", True,
                "Fall back to DocPacker's candidate sheet when none is set above", "bool",
                section="Candidate sheet"),
            Opt("badge_extractor.auto_log_csv", False, "Automatically append each saved badge to CSV log", "bool",
                section="Output file"),
            *cls.append_opts(auto=False),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.items: dict[str, BadgeItem] = {}
        self.current_id: str | None = None
        self.watcher: FolderWatcher | None = None
        self._seen_paths: set[str] = set()
        self._saved_badges: dict[str, str] = {}     # badge_no -> saved filename (duplicate warning)
        self._loading_form = False
        self._form_vars: dict[str, tk.StringVar] = {}
        self._match_map: dict[str, B.CandidateMatch] = {}

    # ------------------------------------------------------------------ automation hooks
    def can_automate(self) -> bool:
        return bool((self.get("input_folder") or "").strip())

    def auto_running(self) -> bool:
        return self.watcher is not None and self.watcher.running

    def start_auto(self) -> None:
        self.start_watching()

    def stop_auto(self) -> None:
        self.stop_watching()

    def auto_summary(self) -> str:
        if not self.auto_running():
            return ""
        folder = Path(str(self.get("input_folder") or "")).name or "folder"
        pending = sum(1 for it in self.items.values() if it.status == "pending")
        return f"watching {folder} ({pending} pending)" if pending else f"watching {folder}"

    def on_close(self) -> None:
        self.stop_watching()

    # ------------------------------------------------------------------ UI
    def build(self, parent: ttk.Frame) -> None:
        self.in_var = tk.StringVar(value=str(self.get("input_folder") or ""))
        self.out_var = tk.StringVar(value=str(self.get("output_folder") or ""))
        self.sync_var("input_folder", self.in_var)
        self.sync_var("output_folder", self.out_var)
        self.in_var.trace_add("write", lambda *_: (self.put("input_folder", self.in_var.get().strip()), self._refresh_status()))
        self.out_var.trace_add("write", lambda *_: (self.put("output_folder", self.out_var.get().strip()), self._refresh_status()))

        # 1. Clean 1-row primary action bar
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 4))
        self.watch_btn = ttk.Button(bar, text="▶ Start watching", style="Accent.TButton", command=self.toggle_watching)
        self.watch_btn.pack(side="left")

        scan_btn = ttk.Button(bar, text="📂 Scan", command=self.scan_input_folder)
        scan_btn.pack(side="left", padx=(6, 0))
        Tooltip(scan_btn, "Scan Watch / Input folder for badge images")

        add_btn = ttk.Button(bar, text="➕ Add…", command=self.pick_files)
        add_btn.pack(side="left", padx=(4, 0))
        Tooltip(add_btn, "Add badge images… (Ctrl+O)")

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8, pady=2)

        self._folders_open = not (bool(self.in_var.get().strip()) and bool(self.out_var.get().strip()))
        self.folders_btn = ttk.Button(
            bar,
            text="📁 Folders ▴" if self._folders_open else "📁 Folders ▾",
            command=self._toggle_folders,
        )
        self.folders_btn.pack(side="left")
        Tooltip(self.folders_btn, "Show or hide Watch / Input and Output folders")

        sheet_btn = ttk.Button(bar, text="📋 Sheet…", command=self.pick_sheet)
        sheet_btn.pack(side="left", padx=(4, 0))
        Tooltip(sheet_btn, "Choose Excel / CSV candidate sheet")

        gear_btn = ttk.Button(bar, text="⚙", width=3, command=lambda: self.app.open_settings(self.id))
        gear_btn.pack(side="right")
        Tooltip(gear_btn, "Badge Extractor settings")

        ttk.Button(bar, text="📊 Export CSV", command=self.export_csv).pack(side="right", padx=(0, 4))
        self.undo_btn = ttk.Button(bar, text="↩ Undo", command=self.undo_last)
        self.undo_btn.pack(side="right", padx=(0, 4))
        Tooltip(self.undo_btn, "Undo last saved badge (Ctrl+Z)")

        # 2. Collapsible Folders drawer (open if folders are unset, collapsed otherwise)
        self._folders_frame = ttk.LabelFrame(parent, text="Folders", padding=(8, 4))
        self._folders_frame.columnconfigure(1, weight=1)
        self._folders_frame.columnconfigure(3, weight=1)

        ttk.Label(self._folders_frame, text="Watch / Input:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        in_fe = FolderEntry(self._folders_frame, self.in_var, "dir", width=22, open_button=True,
                            title="Badge watch / input folder")
        in_fe.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        Tooltip(in_fe.entry, "Drop badge images into this folder to extract them automatically.")

        ttk.Label(self._folders_frame, text="Output folder:").grid(row=0, column=2, sticky="w", padx=(0, 6))
        out_fe = FolderEntry(self._folders_frame, self.out_var, "dir", width=22, open_button=True,
                             title="Badge output folder")
        out_fe.grid(row=0, column=3, sticky="ew")
        Tooltip(out_fe.entry, "Renamed badge files are saved here after your review.")

        if self._folders_open:
            self._folders_frame.pack(fill="x", pady=(0, 4))

        # 3. Compact status bar
        self._status_bar = ttk.Frame(parent)
        self._status_bar.pack(fill="x", pady=(0, 6))
        self.watch_badge = ttk.Label(self._status_bar, text="Idle", style="Badge.TLabel")
        self.watch_badge.pack(side="left")
        self.status_lbl = ttk.Label(self._status_bar, style="Muted.TLabel", text="")
        self.status_lbl.pack(side="left", padx=(8, 0))
        self.folders_summary_lbl = ttk.Label(self._status_bar, style="Muted.TLabel", text="")
        self.folders_summary_lbl.pack(side="right")
        Tooltip(self.folders_summary_lbl, self._folders_tooltip)

        # 4. Main Workspace: Left (Queue + Preview) | Right (Review & Edit + Matches/Raw Tabs + Pinned Save Bar)
        self._h_panes = ttk.PanedWindow(parent, orient="horizontal")
        self._h_panes.pack(fill="both", expand=True)

        # ---- Left Column: Vertical split between Queue (top) and Badge Preview (bottom)
        self._left_panes = ttk.PanedWindow(self._h_panes, orient="vertical")
        self._h_panes.add(self._left_panes, weight=4)

        queue_frame = ttk.Frame(self._left_panes)
        self._left_panes.add(queue_frame, weight=2)
        self.table = DataTable(
            queue_frame,
            [
                ("status", "Status", 76),
                ("en_name", "English Name", 115),
                ("ar_name", "Arabic Name", 110),
                ("badge_no", "Badge #", 62),
                ("matches", "Match", 90),
                ("file", "File", 110),
            ],
            height=6,
            stretch="en_name",
            searchable=True,
            on_select=lambda ids: self._on_queue_select(ids),
        )
        self.table.pack(fill="both", expand=True)
        self.table.tag_colors(pending="info", multi="warn", match="ok", saved="ok", skipped="muted", err="err")

        qbar = ttk.Frame(queue_frame)
        qbar.pack(fill="x", pady=(4, 0))
        ttk.Button(qbar, text="Skip", style="Tiny.TButton", command=self.skip_current).pack(side="left")
        ttk.Button(qbar, text="Remove", style="Tiny.TButton", command=self.remove_selected).pack(side="left", padx=4)
        ttk.Button(qbar, text="Clear done", style="Tiny.TButton", command=self.clear_done).pack(side="left")
        ttk.Button(qbar, text="Clear all", style="Tiny.TButton", command=self.clear_all).pack(side="right")

        prev_box = ttk.LabelFrame(self._left_panes, text="Badge Preview", padding=(6, 4))
        self._left_panes.add(prev_box, weight=3)

        pbar = ttk.Frame(prev_box)
        pbar.pack(fill="x", pady=(0, 4))
        reocr_btn = ttk.Button(pbar, text="🔄 Re-OCR", style="Tiny.TButton", command=self.reocr_current)
        reocr_btn.pack(side="left")
        Tooltip(reocr_btn, "Re-run OCR on this badge (F5)")
        rot_l = ttk.Button(pbar, text="⟲", width=3, style="Tiny.TButton", command=lambda: self.rotate_and_reocr(-90))
        rot_l.pack(side="left", padx=(4, 2))
        Tooltip(rot_l, "Rotate 90° left & re-OCR")
        rot_r = ttk.Button(pbar, text="⟳", width=3, style="Tiny.TButton", command=lambda: self.rotate_and_reocr(90))
        rot_r.pack(side="left")
        Tooltip(rot_r, "Rotate 90° right & re-OCR")

        ttk.Label(pbar, text="Region:", style="Muted.TLabel").pack(side="left", padx=(8, 3))
        self.crop_var = tk.StringVar(value=str(self.get("crop_mode") or "auto"))
        self.sync_var("crop_mode", self.crop_var)
        crop_cb = ttk.Combobox(pbar, textvariable=self.crop_var, values=[k for k, _ in B.CROP_MODE_CHOICES],
                               state="readonly", width=6)
        crop_cb.pack(side="left")
        crop_cb.bind("<<ComboboxSelected>>", lambda _e: self.put("crop_mode", self.crop_var.get()))

        open_img_btn = ttk.Button(pbar, text="Open", style="Tiny.TButton", command=self._open_current_badge)
        open_img_btn.pack(side="right")
        Tooltip(open_img_btn, "Open badge image in external viewer")
        ttk.Button(pbar, text="+", width=2, style="Tiny.TButton",
                   command=lambda: self.viewer.zoom_by(1.2)).pack(side="right", padx=(2, 4))
        ttk.Button(pbar, text="Fit", style="Tiny.TButton",
                   command=lambda: self.viewer.fit("page")).pack(side="right", padx=2)
        ttk.Button(pbar, text="−", width=2, style="Tiny.TButton",
                   command=lambda: self.viewer.zoom_by(1 / 1.2)).pack(side="right")

        self.ocr_info_lbl = ttk.Label(prev_box, text="Add or select a badge to preview", style="Muted.TLabel")
        self.ocr_info_lbl.pack(side="bottom", anchor="w", pady=(3, 0))

        self.viewer = PageViewer(prev_box, self.app, show_toolbar=False, max_pages=1)
        self.viewer.mode = "page"
        self.viewer.pack(fill="both", expand=True)
        self.viewer.clear("Add or select a badge to preview")

        # ---- Right Column: Review & Edit + Tabbed Inspector + Pinned Save Bar
        right = ttk.Frame(self._h_panes)
        self._h_panes.add(right, weight=5)

        # Pack bottom action bar FIRST so it always stays pinned and visible
        sbar = ttk.Frame(right)
        sbar.pack(side="bottom", fill="x", pady=(4, 0))
        ttk.Label(sbar, text="Save:", style="Muted.TLabel").pack(side="left", padx=(0, 4))
        self.action_var = tk.StringVar(value=str(self.get("action") or "move"))
        self.sync_var("action", self.action_var)
        act_cb = ttk.Combobox(sbar, textvariable=self.action_var, values=["move", "copy"], state="readonly", width=6)
        act_cb.pack(side="left")
        act_cb.bind("<<ComboboxSelected>>", lambda _e: self.put("action", self.action_var.get()))
        Tooltip(act_cb, "Move removes the original from the watch folder; Copy keeps it.")

        ttk.Button(sbar, text="Open Output", command=self.open_output_folder).pack(side="left", padx=(6, 0))
        self.save_btn = ttk.Button(sbar, text="✔ Save & Next  (Ctrl+Enter)", style="Accent.TButton",
                                   command=self.save_current)
        self.save_btn.pack(side="right")
        ttk.Button(sbar, text="Skip", command=self.skip_current).pack(side="right", padx=(0, 6))
        ttk.Separator(right, orient="horizontal").pack(side="bottom", fill="x", pady=(6, 2))

        # Review & Edit Card (spacious, cleanly aligned, uncrowded)
        form_box = ttk.LabelFrame(right, text="Review & Edit", padding=(10, 8))
        form_box.pack(fill="x", pady=(0, 6))
        form_box.columnconfigure(1, weight=1)

        self.ar_var = tk.StringVar()
        self.en_var = tk.StringVar()
        self.job_var = tk.StringVar()
        self.badge_var = tk.StringVar()
        self.tpl_var = tk.StringVar(value=str(self.get("filename_template") or B.DEFAULT_FILENAME_TEMPLATE))
        self.sync_var("filename_template", self.tpl_var)
        self._form_vars = {
            "ar_name": self.ar_var,
            "en_name": self.en_var,
            "job_title": self.job_var,
            "badge_no": self.badge_var,
        }

        # Row 0: Arabic Name
        ttk.Label(form_box, text="Arabic Name:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ar_row = ttk.Frame(form_box)
        ar_row.grid(row=0, column=1, sticky="ew", pady=3)
        ar_row.columnconfigure(0, weight=1)
        self.ar_entry = ttk.Entry(ar_row, textvariable=self.ar_var, justify="right")
        self.ar_entry.grid(row=0, column=0, sticky="ew")
        swap_btn = ttk.Button(ar_row, text="⇅ Swap", style="Tiny.TButton", command=self.swap_name_job)
        swap_btn.grid(row=0, column=1, padx=(4, 0))
        Tooltip(swap_btn, "Swap Arabic Name (Line 1) and Job Title (Line 2)")

        # Row 1: English Name (Transliteration)
        ttk.Label(form_box, text="English Name:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        en_row = ttk.Frame(form_box)
        en_row.grid(row=1, column=1, sticky="ew", pady=3)
        en_row.columnconfigure(0, weight=1)
        self.en_entry = ttk.Entry(en_row, textvariable=self.en_var)
        self.en_entry.grid(row=0, column=0, sticky="ew")
        auto_btn = ttk.Button(en_row, text="↻ Auto", style="Tiny.TButton", command=self.retransliterate)
        auto_btn.grid(row=0, column=1, padx=(4, 0))
        Tooltip(auto_btn, "Re-transliterate from Arabic Name")
        ai_btn = ttk.Button(en_row, text="✨ AI", style="Tiny.TButton", command=self.ai_retransliterate)
        ai_btn.grid(row=0, column=2, padx=(4, 0))
        Tooltip(ai_btn, "Transliterate using AI")

        # Row 2: Job Title + Badge # paired on one row
        ttk.Label(form_box, text="Job Title:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        jb_row = ttk.Frame(form_box)
        jb_row.grid(row=2, column=1, sticky="ew", pady=3)
        jb_row.columnconfigure(0, weight=3)
        jb_row.columnconfigure(2, weight=1)
        self.job_entry = ttk.Entry(jb_row, textvariable=self.job_var, justify="right")
        self.job_entry.grid(row=0, column=0, sticky="ew")
        ttk.Label(jb_row, text="Badge #:").grid(row=0, column=1, sticky="w", padx=(10, 6))
        self.badge_entry = ttk.Entry(jb_row, textvariable=self.badge_var, width=10)
        self.badge_entry.grid(row=0, column=2, sticky="ew")

        # Row 3: Auto-hiding duplicate badge warning
        self.dup_lbl = ttk.Label(form_box, text="", style="Warn.TLabel")
        self.dup_lbl.grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.dup_lbl.grid_remove()

        # Row 4: Divider
        ttk.Separator(form_box, orient="horizontal").grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 6))

        # Row 5: Filename Template + compact "+ Field ▾" menu
        ttk.Label(form_box, text="Filename:").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=2)
        tpl_row = ttk.Frame(form_box)
        tpl_row.grid(row=5, column=1, sticky="ew", pady=2)
        tpl_row.columnconfigure(0, weight=1)
        self.tpl_combo = ttk.Combobox(tpl_row, textvariable=self.tpl_var,
                                      values=[tpl for _, tpl in B.FILENAME_PRESETS])
        self.tpl_combo.grid(row=0, column=0, sticky="ew")
        Tooltip(self.tpl_combo, "Customize the output filename using {ar_name}, {en_name}, {badge_no}, {job_title}, {en_job_title}, {matched_name}, {date}.")
        self._tok_btn = ttk.Button(tpl_row, text="+ Field ▾", style="Tiny.TButton", command=self._show_token_menu)
        self._tok_btn.grid(row=0, column=1, padx=(4, 0))

        # Row 6: Live Output Filename Preview
        ttk.Label(form_box, text="Output:", style="Muted.TLabel").grid(row=6, column=0, sticky="nw", padx=(0, 8), pady=(3, 0))
        self.preview_lbl = ttk.Label(form_box, text="—", style="H3.TLabel", wraplength=420, justify="left")
        self.preview_lbl.grid(row=6, column=1, sticky="ew", pady=(3, 0))
        form_box.bind("<Configure>", lambda e: self.preview_lbl.configure(wraplength=max(220, e.width - 110)))

        # Bind live field updates
        self.ar_var.trace_add("write", lambda *_: self._on_ar_changed())
        self.en_var.trace_add("write", lambda *_: self._on_field_changed("en_name"))
        self.job_var.trace_add("write", lambda *_: self._on_field_changed("job_title"))
        self.badge_var.trace_add("write", lambda *_: self._on_field_changed("badge_no"))
        self.tpl_var.trace_add("write", lambda *_: self._on_template_changed())

        # Tabbed Inspector: Excel Matches | Raw OCR
        self.detail_nb = ttk.Notebook(right)
        self.detail_nb.pack(fill="both", expand=True)

        self._match_tab = ttk.Frame(self.detail_nb, padding=(8, 6))
        self.detail_nb.add(self._match_tab, text="📋 Excel Matches")

        mbar = ttk.Frame(self._match_tab)
        mbar.pack(fill="x", pady=(0, 4))
        ttk.Label(mbar, text="By:").pack(side="left", padx=(0, 4))
        self.search_by_var = tk.StringVar(value=str(self.get("search_by") or "en_name"))
        self.sync_var("search_by", self.search_by_var)
        sb_cb = ttk.Combobox(mbar, textvariable=self.search_by_var, values=[k for k, _ in B.SEARCH_BY_CHOICES],
                             state="readonly", width=9)
        sb_cb.pack(side="left")
        sb_cb.bind("<<ComboboxSelected>>", lambda _e: (self.put("search_by", self.search_by_var.get()), self.check_matches_current()))

        ttk.Button(mbar, text="🔍 Check", style="Tiny.TButton", command=self.check_matches_current).pack(side="left", padx=(6, 0))
        ttk.Button(mbar, text="👤 Details (Ctrl+K)", style="Tiny.TButton", command=self.show_candidate_info).pack(side="left", padx=(4, 0))
        ttk.Button(mbar, text="✔ Use Selected", style="Tiny.TButton", command=self.apply_selected_match).pack(side="right")

        self.match_lbl = ttk.Label(self._match_tab, text="Choose an Excel sheet to check candidate similarity.", style="Muted.TLabel")
        self.match_lbl.pack(anchor="w", pady=(0, 4))

        mtree_host = ttk.Frame(self._match_tab)
        mtree_host.pack(fill="both", expand=True)
        self.match_tree = ttk.Treeview(
            mtree_host,
            columns=("pct", "name", "row", "by", "details"),
            show="headings",
            height=4,
            selectmode="browse",
        )
        m_ys = ttk.Scrollbar(mtree_host, orient="vertical", command=self.match_tree.yview)
        self.match_tree.configure(yscrollcommand=m_ys.set)
        m_ys.pack(side="right", fill="y")
        self.match_tree.pack(fill="both", expand=True)
        for col, heading, width in (
            ("pct", "%", 52),
            ("name", "Candidate Name", 155),
            ("row", "Row", 42),
            ("by", "By", 70),
            ("details", "Details", 175),
        ):
            self.match_tree.heading(col, text=heading)
            self.match_tree.column(col, width=width, stretch=(col == "details"))
        self.match_tree.bind("<Double-1>", lambda _e: self.show_candidate_info())

        self._raw_tab = ttk.Frame(self.detail_nb, padding=(8, 6))
        self.detail_nb.add(self._raw_tab, text="📝 Raw OCR")
        ttk.Label(self._raw_tab, text="Select any text and right-click to assign it to a field.", style="Muted.TLabel").pack(anchor="w", pady=(0, 4))
        raw_host = ttk.Frame(self._raw_tab)
        raw_host.pack(fill="both", expand=True)
        self.raw_text = tk.Text(raw_host, height=5, wrap="word")
        r_ys = ttk.Scrollbar(raw_host, orient="vertical", command=self.raw_text.yview)
        self.raw_text.configure(yscrollcommand=r_ys.set)
        r_ys.pack(side="right", fill="y")
        self.raw_text.pack(fill="both", expand=True)
        style_text(self.frame, self.raw_text)
        self._bind_raw_assign_menu()

        parent.after(80, self._init_sashes)

        # Populate any items added before UI was built
        for it in self.items.values():
            self._upsert_queue_row(it)
        self._refresh_status()

    def _init_sashes(self) -> None:
        if not self.built:
            return
        try:
            if self._h_panes.winfo_exists():
                w = self._h_panes.winfo_width()
                if w > 200:
                    self._h_panes.sashpos(0, int(w * 0.44))
            if self._left_panes.winfo_exists():
                h = self._left_panes.winfo_height()
                if h > 200:
                    self._left_panes.sashpos(0, int(h * 0.38))
        except Exception:
            pass

    def _toggle_folders(self) -> None:
        self._folders_open = not self._folders_open
        if self._folders_open:
            self._folders_frame.pack(fill="x", pady=(0, 4), before=self._status_bar)
            self.folders_btn.configure(text="📁 Folders ▴")
        else:
            self._folders_frame.pack_forget()
            self.folders_btn.configure(text="📁 Folders ▾")

    def _folders_tooltip(self) -> str:
        in_p = str(self.get("input_folder") or "").strip() or "(not set)"
        out_p = str(self.get("output_folder") or "").strip() or "(not set)"
        return f"Watch / Input: {in_p}\nOutput: {out_p}"

    def _open_current_badge(self) -> None:
        it = self._current()
        if it is not None and it.path.is_file():
            open_path(it.path)

    def _show_token_menu(self) -> None:
        m = tk.Menu(self._tok_btn, tearoff=False)
        for tok, desc in (
            ("{ar_name}", "Arabic Name"),
            ("{en_name}", "English Name"),
            ("{badge_no}", "Badge Number"),
            ("{job_title}", "Job Title (Arabic)"),
            ("{en_job_title}", "Job Title (English)"),
            ("{matched_name}", "Matched Excel Name"),
            ("{date}", "Today's Date"),
        ):
            m.add_command(label=f"{tok}  —  {desc}", command=lambda t=tok: self._insert_token(t))
        try:
            x = self._tok_btn.winfo_rootx()
            y = self._tok_btn.winfo_rooty() + self._tok_btn.winfo_height()
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def _set_tab_label(self, tab: ttk.Frame, text: str) -> None:
        try:
            self.detail_nb.tab(tab, text=text)
        except (tk.TclError, TypeError):
            pass

    def _bind_raw_assign_menu(self) -> None:
        def popup(event) -> None:
            try:
                sel = self.raw_text.get("sel.first", "sel.last").strip()
            except tk.TclError:
                return
            if not sel:
                return
            m = tk.Menu(self.raw_text, tearoff=False)
            m.add_command(label="Assign to Arabic Name", command=lambda: self.ar_var.set(sel))
            m.add_command(label="Assign to English Name", command=lambda: self.en_var.set(sel))
            m.add_command(label="Assign to Job Title", command=lambda: self.job_var.set(sel))
            m.add_command(label="Assign to Badge Number", command=lambda: self.badge_var.set(B._extract_digits(sel) or sel))
            try:
                m.tk_popup(event.x_root, event.y_root)
            finally:
                m.grab_release()

        self.raw_text.bind("<Button-3>", popup)

    # ------------------------------------------------------------------ watching & adding files
    def toggle_watching(self) -> None:
        if self.auto_running():
            self.stop_watching()
        else:
            self.start_watching()

    def start_watching(self) -> None:
        folder_str = str(self.get("input_folder") or "").strip()
        if not folder_str:
            if self.built:
                info(self.frame, "Choose a Watch / Input folder first.")
            return
        folder = Path(folder_str)
        exts = parse_ext_list(str(self.get("watch_extensions") or "jpg, jpeg, png, bmp, tif, tiff, webp"))
        quiet = max(0.0, float(self.get("quiet_seconds") or 1.5))
        proc_existing = bool(self.get("process_existing"))
        self.stop_watching()

        def on_file(path: Path) -> None:
            self.app.bridge.call(lambda p=path: self._on_watched_file(p))

        self.watcher = FolderWatcher(
            folder=folder,
            extensions=exts or B.IMAGE_EXTENSIONS,
            quiet_seconds=quiet,
            on_file=on_file,
            process_existing=proc_existing,
            recursive=False,
        )
        self.watcher.start()
        self.log(f"Watching badge folder: {folder}", "ok")
        self._refresh_status()

    def stop_watching(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
            self.log("Stopped watching badge folder.", "info")
        self._refresh_status()

    def _on_watched_file(self, path: Path) -> None:
        if str(path) in self._seen_paths:
            return
        self.app.notify(f"New badge detected: {path.name}", "info")
        self.add_paths([str(path)])

    def scan_input_folder(self) -> None:
        folder_str = str(self.get("input_folder") or "").strip()
        if not folder_str or not Path(folder_str).is_dir():
            info(self.frame, "Choose an existing Input / Watch folder first.")
            return
        exts = parse_ext_list(str(self.get("watch_extensions") or "jpg, jpeg, png, bmp, tif, tiff, webp")) or B.IMAGE_EXTENSIONS
        files = [str(p) for p in sorted(Path(folder_str).iterdir())
                 if p.is_file() and p.suffix.lower() in exts and not p.name.startswith(("~$", "."))]
        if not files:
            info(self.frame, f"No badge images found in {Path(folder_str).name}.")
            return
        self.add_paths(files)

    def pick_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(B.IMAGE_EXTENSIONS))
        paths = filedialog.askopenfilenames(
            title="Add badge images",
            initialdir=self.input_dir(),
            filetypes=[("Badge images", exts), ("All files", "*.*")],
        )
        if paths:
            self.add_paths(list(paths))

    def accept_files(self, paths: list[str]) -> None:
        self.add_paths(paths)

    def add_paths(self, paths: list[str]) -> None:
        exts = parse_ext_list(str(self.get("watch_extensions") or "jpg, jpeg, png, bmp, tif, tiff, webp")) or B.IMAGE_EXTENSIONS
        files: list[Path] = []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                files.extend(f for f in sorted(p.rglob("*"))
                             if f.is_file() and f.suffix.lower() in exts and not f.name.startswith(("~$", ".")))
            elif p.is_file() and p.suffix.lower() in exts:
                files.append(p)
        new_items: list[BadgeItem] = []
        for f in files:
            key = str(f.resolve())
            if key in self._seen_paths:
                continue
            self._seen_paths.add(key)
            item = BadgeItem(id=f"badge-{next(_ids)}", path=f, status="reading")
            self.items[item.id] = item
            new_items.append(item)
            if self.built:
                self._upsert_queue_row(item)

        if not new_items:
            return
        self._refresh_status()
        self._extract_items(new_items)

    # ------------------------------------------------------------------ OCR + Excel matching jobs
    def _effective_sheet(self) -> tuple[str, str, str, int]:
        """Return (sheet_path, tab, search_column, header_row), falling back to DocPacker if enabled."""
        path = str(self.get("info_sheet") or "").strip()
        if path:
            return (
                path,
                str(self.get("info_tab") or "").strip(),
                str(self.get("info_search_column") or "").strip(),
                int(self.get("info_header_row") or 1),
            )
        if bool(self.get("use_docpacker_sheet_fallback")):
            dp_path = str(self.settings.get("docpacker.info_sheet", "") or "").strip()
            if dp_path:
                return (
                    dp_path,
                    str(self.settings.get("docpacker.info_tab", "") or "").strip(),
                    str(self.settings.get("docpacker.info_name_column", "") or "").strip(),
                    int(self.settings.get_int("docpacker.info_header_row", 1) or 1),
                )
        return ("", "", "", 1)

    def _extract_items(self, items: list[BadgeItem]) -> None:
        cfg = ocr.OcrConfig.from_settings(self.settings)
        langs = str(self.get("ocr_languages") or "ara+eng").strip() or "ara+eng"
        crop_mode = str(self.get("crop_mode") or "auto")
        sheet_path, sheet_tab, search_col, header_row = self._effective_sheet()
        search_by = str(self.get("search_by") or "en_name")
        thr = max(0.30, min(1.0, int(self.get("similarity_threshold") or 55) / 100.0))
        max_m = max(1, int(self.get("max_matches") or 10))

        def work(job):
            out = []
            for idx, it in enumerate(items):
                if job.cancelled:
                    break
                job.progress(idx, len(items), it.path.name)
                try:
                    data = B.extract_badge(it.path, cfg, languages=langs, crop_mode=crop_mode, rotation=it.rotation)
                    matches: list[B.CandidateMatch] = []
                    m_err = ""
                    if sheet_path and Path(sheet_path).is_file():
                        try:
                            matches = B.search_badge_candidates(
                                sheet_path,
                                en_name=data.en_name,
                                ar_name=data.ar_name,
                                badge_no=data.badge_no,
                                search_by=search_by,
                                sheet=sheet_tab,
                                search_column=search_col,
                                header_row=header_row,
                                threshold=thr,
                                limit=max_m,
                            )
                        except Exception as exc:
                            m_err = str(exc)
                    out.append((it.id, data, matches, m_err, ""))
                except Exception as exc:
                    out.append((it.id, None, [], "", str(exc)))
            return out

        def done(results) -> None:
            first_ready: str | None = None
            for iid, data, matches, m_err, err in results or []:
                it = self.items.get(iid)
                if it is None:
                    continue
                if err:
                    it.status = "error"
                    it.error_msg = err
                    self.log(f"Badge OCR failed for {it.path.name}: {err}", "error")
                elif data is not None:
                    it.ar_name = data.ar_name
                    it.en_name = data.en_name
                    it.job_title = data.job_title
                    it.badge_no = data.badge_no
                    it.raw_text = data.raw_text
                    it.ocr_source = data.ocr_source
                    it.warnings = list(data.warnings)
                    it.matches = matches
                    it.selected_match = matches[0] if matches else None
                    it.match_error = m_err
                    it.en_edited_manually = False
                    it.status = "pending"
                    if first_ready is None:
                        first_ready = it.id
                    for w in data.warnings:
                        self.log(w, "warn")
                if self.built:
                    self._upsert_queue_row(it)
            if self.built:
                if self.current_id is None and first_ready:
                    self._select_item(first_ready)
                elif self.current_id and self.current_id in self.items:
                    self._load_item_into_form(self.items[self.current_id])
                self._refresh_status()

        self.app.jobs.submit(
            work,
            f"Extracting {len(items)} badge(s)",
            tool=self.id,
            on_done=done,
            on_error=lambda m: self.built and error(self.frame, f"Badge extraction failed: {m}"),
            serial_key="badge_extractor.ocr",
        )

    def reocr_current(self) -> None:
        it = self._current()
        if it is None:
            return
        it.status = "reading"
        if self.built:
            self._upsert_queue_row(it)
        self._extract_items([it])

    def rotate_and_reocr(self, deg: int) -> None:
        it = self._current()
        if it is None:
            return
        it.rotation = (it.rotation + deg) % 360
        self.reocr_current()

    # ------------------------------------------------------------------ queue & form sync
    def _current(self) -> BadgeItem | None:
        return self.items.get(self.current_id) if self.current_id else None

    def _queue_tag(self, it: BadgeItem) -> str:
        if it.status == "error":
            return "err"
        if it.status == "saved":
            return "saved"
        if it.status == "skipped":
            return "skipped"
        if B.has_ambiguous_matches(it.matches):
            return "multi"
        if it.matches and it.matches[0].score >= 0.85:
            return "match"
        return "pending"

    def _match_summary(self, it: BadgeItem) -> str:
        if it.match_error:
            return "Sheet error"
        if not it.matches:
            return "—"
        top = it.matches[0]
        if len(it.matches) == 1:
            return f"{top.percent}% ({top.name_value or top.matched_value})"
        return f"⚠ {len(it.matches)} matches ({top.percent}%)"

    def _upsert_queue_row(self, it: BadgeItem) -> None:
        if not self.built:
            return
        status_label = {
            "reading": "Reading…",
            "pending": "⚠ Check" if B.has_ambiguous_matches(it.matches) else "Review",
            "saved": "✔ Saved",
            "skipped": "Skipped",
            "error": "✘ Error",
        }.get(it.status, it.status)
        row = {
            "status": status_label,
            "file": it.path.name,
            "ar_name": it.ar_name,
            "en_name": it.en_name,
            "badge_no": it.badge_no,
            "matches": self._match_summary(it),
        }
        tag = self._queue_tag(it)
        if it.id in self.table._rows:
            self.table.update_row(it.id, row, tags=(tag,))
        else:
            self.table.add(row, iid=it.id, tags=(tag,))

    def _on_queue_select(self, ids: list[str]) -> None:
        if not ids:
            return
        iid = ids[0]
        if iid in self.items:
            self.current_id = iid
            self._load_item_into_form(self.items[iid])

    def _select_item(self, iid: str) -> None:
        if iid not in self.items:
            return
        self.current_id = iid
        if self.built:
            try:
                self.table.tree.selection_set(iid)
                self.table.tree.see(iid)
            except tk.TclError:
                pass
            self._load_item_into_form(self.items[iid])

    def _load_item_into_form(self, it: BadgeItem) -> None:
        if not self.built:
            return
        self._loading_form = True
        try:
            self.ar_var.set(it.ar_name)
            self.en_var.set(it.en_name)
            self.job_var.set(it.job_title)
            self.badge_var.set(it.badge_no)
            self.raw_text.delete("1.0", "end")
            self.raw_text.insert("1.0", it.raw_text or it.error_msg or "")
            info_parts = [it.path.name]
            for p in (it.ocr_source, f"rot {it.rotation}°" if it.rotation else ""):
                if p:
                    info_parts.append(p)
            if it.warnings:
                info_parts.append(it.warnings[0])
            self.ocr_info_lbl.configure(text=" • ".join(info_parts))
            if it.path.is_file():
                rot = {0: it.rotation} if it.rotation else None
                try:
                    self.viewer.show(it.path, rotations=rot)
                except Exception:
                    pass
            self._populate_matches_tree(it)
            self._update_filename_preview()
            self._update_duplicate_warning()
        finally:
            self._loading_form = False

    def _on_ar_changed(self) -> None:
        if self._loading_form:
            return
        it = self._current()
        if it is None:
            return
        it.ar_name = self.ar_var.get().strip()
        if not it.en_edited_manually:
            self._loading_form = True
            try:
                it.en_name = B.transliterate_arabic(it.ar_name)
                self.en_var.set(it.en_name)
            finally:
                self._loading_form = False
        self._upsert_queue_row(it)
        self._update_filename_preview()

    def _on_field_changed(self, key: str) -> None:
        if self._loading_form:
            return
        it = self._current()
        if it is None:
            return
        val = self._form_vars[key].get().strip()
        setattr(it, key, val)
        if key == "en_name":
            it.en_edited_manually = True
        self._upsert_queue_row(it)
        self._update_filename_preview()
        if key == "badge_no":
            self._update_duplicate_warning()

    def _on_template_changed(self) -> None:
        tpl = self.tpl_var.get().strip()
        if tpl:
            self.put("filename_template", tpl)
        self._update_filename_preview()

    def _insert_token(self, token: str) -> None:
        cur = self.tpl_var.get()
        sep = "-" if cur and not cur.endswith(("-", "_", " ")) else ""
        self.tpl_var.set(cur + sep + token)

    def swap_name_job(self) -> None:
        it = self._current()
        if it is None:
            return
        a, j = self.ar_var.get().strip(), self.job_var.get().strip()
        it.en_edited_manually = False
        self.ar_var.set(j)
        self.job_var.set(a)

    def retransliterate(self) -> None:
        it = self._current()
        ar = self.ar_var.get().strip()
        if not ar:
            return
        en = B.transliterate_arabic(ar)
        if it is not None:
            it.en_edited_manually = False
            it.en_name = en
        self._loading_form = True
        try:
            self.en_var.set(en)
        finally:
            self._loading_form = False
        if it is not None:
            self._upsert_queue_row(it)
        self._update_filename_preview()

    def ai_retransliterate(self) -> None:
        ar = self.ar_var.get().strip()
        if not ar:
            return
        cfg = llm.LlmConfig.from_settings(self.settings)
        if not cfg.enabled:
            info(self.frame, "AI is disabled. Enable it in Settings > AI, or use the offline ↻ Transliterate button.")
            return
        it = self._current()

        def work(_job):
            return B.ai_transliterate(ar, cfg)

        def done(en: str) -> None:
            if not en:
                return
            if it is not None:
                it.en_name = en
                it.en_edited_manually = True
            self.en_var.set(en)
            if it is not None:
                self._upsert_queue_row(it)
            self._update_filename_preview()

        self.app.jobs.submit(work, "AI transliterating name", tool=self.id, on_done=done,
                             on_error=lambda m: error(self.frame, f"AI transliteration failed: {m}"))

    def _update_filename_preview(self) -> None:
        if not self.built:
            return
        it = self._current()
        if it is None:
            self.preview_lbl.configure(text="—")
            return
        matched_name = it.selected_match.name_value if it.selected_match else ""
        stem = B.render_badge_stem(
            self.tpl_var.get(),
            ar_name=self.ar_var.get(),
            en_name=self.en_var.get(),
            job_title=self.job_var.get(),
            badge_no=self.badge_var.get(),
            matched_name=matched_name,
            original=it.path,
        )
        suffix = it.path.suffix.lower() or ".jpg"
        out_dir = Path(str(self.get("output_folder") or "").strip()) if (self.get("output_folder") or "").strip() else None
        if out_dir and out_dir.is_dir():
            fname = predicted_name(out_dir, stem, suffix, str(self.get("duplicate_policy") or "number"))
        else:
            fname = f"{stem}{suffix}"
        self.preview_lbl.configure(text=fname)

    def _update_duplicate_warning(self) -> None:
        if not self.built:
            return
        bn = self.badge_var.get().strip()
        if bn and bn in self._saved_badges:
            self.dup_lbl.configure(text=f"⚠ Badge #{bn} already saved ({self._saved_badges[bn]})")
            self.dup_lbl.grid()
        else:
            self.dup_lbl.configure(text="")
            self.dup_lbl.grid_remove()

    # ------------------------------------------------------------------ Candidate Similarity Check
    def pick_sheet(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose names / candidates spreadsheet",
            initialdir=self.input_dir(),
            filetypes=[("Spreadsheets", "*.xlsx *.xlsm *.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        self.put("info_sheet", path)
        self.log(f"Badge candidate sheet set to: {path}", "info")
        self._refresh_status()
        if self._current() is not None:
            self.check_matches_current()

    def check_matches_current(self) -> None:
        it = self._current()
        if it is None:
            return
        sheet_path, sheet_tab, search_col, header_row = self._effective_sheet()
        if not sheet_path:
            if self.built:
                self.match_lbl.configure(text="No Excel sheet selected. Click 'Choose Excel sheet…' above.")
            return
        search_by = self.search_by_var.get() if self.built else str(self.get("search_by") or "en_name")
        thr = max(0.30, min(1.0, int(self.get("similarity_threshold") or 55) / 100.0))
        max_m = max(1, int(self.get("max_matches") or 10))
        try:
            matches = B.search_badge_candidates(
                sheet_path,
                en_name=it.en_name,
                ar_name=it.ar_name,
                badge_no=it.badge_no,
                search_by=search_by,
                sheet=sheet_tab,
                search_column=search_col,
                header_row=header_row,
                threshold=thr,
                limit=max_m,
            )
            it.matches = matches
            it.selected_match = matches[0] if matches else None
            it.match_error = ""
        except SheetError as exc:
            it.matches = []
            it.selected_match = None
            it.match_error = str(exc)
        except Exception as exc:
            it.matches = []
            it.selected_match = None
            it.match_error = f"Could not read sheet: {exc}"

        if self.built:
            self._populate_matches_tree(it)
            self._upsert_queue_row(it)
            self._update_filename_preview()
            self._refresh_status()

    def _populate_matches_tree(self, it: BadgeItem) -> None:
        self.match_tree.delete(*self.match_tree.get_children())
        self._match_map.clear()
        sheet_path, _, _, _ = self._effective_sheet()
        if it.match_error:
            self.match_lbl.configure(text=f"Sheet error: {it.match_error}", style="Warn.TLabel")
            self._set_tab_label(self._match_tab, "📋 Excel Matches")
            return
        if not sheet_path:
            self.match_lbl.configure(text="No Excel sheet selected. Click 'Sheet…' above to compare names.", style="Muted.TLabel")
            self._set_tab_label(self._match_tab, "📋 Excel Matches")
            return
        if not it.matches:
            self.match_lbl.configure(text=f"No similar rows found in {Path(sheet_path).name}.", style="Muted.TLabel")
            self._set_tab_label(self._match_tab, "📋 Excel Matches (0)")
            return
        if B.has_ambiguous_matches(it.matches):
            self.match_lbl.configure(
                text=f"⚠ {len(it.matches)} similar names in {Path(sheet_path).name} — verify which row is right:",
                style="Warn.TLabel",
            )
            self._set_tab_label(self._match_tab, f"⚠ Excel Matches ({len(it.matches)})")
        else:
            self.match_lbl.configure(
                text=f"{len(it.matches)} match(es) in {Path(sheet_path).name} (top: {it.matches[0].percent}%):",
                style="Muted.TLabel",
            )
            self._set_tab_label(self._match_tab, f"📋 Excel Matches ({len(it.matches)})")
        for idx, m in enumerate(it.matches):
            mid = f"m-{idx}"
            self._match_map[mid] = m
            self.match_tree.insert(
                "", "end", iid=mid,
                values=(f"{m.percent}%", m.name_value or m.matched_value, m.row, m.matched_by, m.summary()),
            )
        first = self.match_tree.get_children()
        if first:
            self.match_tree.selection_set(first[0])

    def _selected_inline_match(self) -> B.CandidateMatch | None:
        if not self.built:
            return None
        sel = self.match_tree.selection()
        if sel and sel[0] in self._match_map:
            return self._match_map[sel[0]]
        it = self._current()
        return it.matches[0] if (it and it.matches) else None

    def apply_selected_match(self, match: B.CandidateMatch | None = None) -> None:
        """Apply the chosen Excel candidate's verified name to the badge fields."""
        it = self._current()
        m = match or self._selected_inline_match()
        if it is None or m is None:
            return
        it.selected_match = m
        chosen = (m.name_value or m.matched_value).strip()
        if chosen:
            if B.has_arabic(chosen):
                it.ar_name = chosen
                if self.built:
                    self.ar_var.set(chosen)
            else:
                it.en_name = chosen
                it.en_edited_manually = True
                if self.built:
                    self.en_var.set(chosen)
        if self.built:
            self._upsert_queue_row(it)
            self._update_filename_preview()
        self.app.notify(f"Applied candidate name: {chosen} ({m.percent}%)", "ok")

    def show_candidate_info(self) -> None:
        """DocPacker-style Candidate Info popup, enhanced to compare multiple similar matches with percentages."""
        it = self._current()
        sheet_path, sheet_tab, search_col, header_row = self._effective_sheet()
        if not sheet_path:
            if confirm(self.frame, "No candidates spreadsheet is chosen yet.\n\nChoose an Excel/CSV sheet now?"):
                self.pick_sheet()
            return
        en = self.en_var.get().strip() if self.built else (it.en_name if it else "")
        ar = self.ar_var.get().strip() if self.built else (it.ar_name if it else "")
        bn = self.badge_var.get().strip() if self.built else (it.badge_no if it else "")
        if not (en or ar or bn):
            info(self.frame, "Extract or type a name or badge number first.")
            return
        search_by = self.search_by_var.get() if self.built else str(self.get("search_by") or "en_name")
        thr = max(0.30, min(1.0, int(self.get("similarity_threshold") or 55) / 100.0))
        max_m = max(1, int(self.get("max_matches") or 10))
        try:
            hits = B.search_badge_candidates(
                sheet_path,
                en_name=en,
                ar_name=ar,
                badge_no=bn,
                search_by=search_by,
                sheet=sheet_tab,
                search_column=search_col,
                header_row=header_row,
                threshold=thr,
                limit=max_m,
            )
        except SheetError as exc:
            error(self.frame, str(exc))
            return
        except Exception as exc:
            error(self.frame, f"Could not read the spreadsheet: {exc}")
            return

        if it is not None:
            it.matches = hits
            if self.built:
                self._populate_matches_tree(it)
                self._upsert_queue_row(it)

        if not hits:
            query_lbl = en or ar or bn
            info(self.frame, f"No similar rows for “{query_lbl}” were found in {Path(sheet_path).name} (threshold {int(thr * 100)}%).")
            return

        title_name = en or ar or bn
        top, body = modal(self.frame, f"Candidate Info Check — {title_name}", 560, 0)
        top.transient(self.frame.winfo_toplevel())
        top.bind("<Escape>", lambda _e: top.destroy())

        head = tk.StringVar()
        ttk.Label(body, textvariable=head, style="Muted.TLabel", wraplength=520, justify="left").pack(anchor="w")

        # Multi-match selector list (shows all similar names with their percentages!)
        active_idx = [0]
        if len(hits) > 1:
            ttk.Label(body, text=f"{len(hits)} similar candidates found — select one to inspect:",
                      style="H3.TLabel").pack(anchor="w", pady=(6, 2))
            cand_tree = ttk.Treeview(body, columns=("pct", "name", "row", "by"), show="headings",
                                     height=min(6, len(hits)), selectmode="browse")
            for col, label, w in (("pct", "Match %", 70), ("name", "Candidate", 250), ("row", "Row", 55), ("by", "Matched By", 95)):
                cand_tree.heading(col, text=label)
                cand_tree.column(col, width=w, stretch=(col == "name"))
            for i, h in enumerate(hits):
                cand_tree.insert("", "end", iid=str(i), values=(f"{h.percent}%", h.name_value or h.matched_value, h.row, h.matched_by))
            cand_tree.pack(fill="x", pady=(0, 6))
        else:
            cand_tree = None

        tree = ttk.Treeview(body, columns=("field", "value"), show="headings",
                            height=min(13, max(len(h.fields) for h in hits)), selectmode="browse")
        tree.heading("field", text="Field")
        tree.heading("value", text="Value")
        tree.column("field", width=165, stretch=False)
        tree.column("value", width=350)
        tree.pack(fill="both", expand=True, pady=(0, 6))

        def show_hit(i: int) -> None:
            active_idx[0] = i
            h = hits[i]
            head.set(f"{Path(sheet_path).name}  •  Row {h.row}  •  Similarity {h.percent}%  (matched by {h.matched_by})")
            tree.delete(*tree.get_children())
            for k, v in h.fields:
                tree.insert("", "end", values=(k, v))

        if cand_tree is not None:
            def on_cand_sel(_e) -> None:
                sel = cand_tree.selection()
                if sel:
                    show_hit(int(sel[0]))
            cand_tree.bind("<<TreeviewSelect>>", on_cand_sel)
            cand_tree.selection_set("0")

        def copy_row() -> None:
            sel = tree.selection()
            rows = [tree.item(s, "values") for s in sel] or [tree.item(c, "values") for c in tree.get_children()]
            copy_to_clipboard(top, "\n".join(f"{k}: {v}" for k, v in rows))
            self.app.notify("Copied candidate details.", "ok")

        def use_this() -> None:
            self.apply_selected_match(hits[active_idx[0]])
            top.destroy()

        bar = ttk.Frame(body)
        bar.pack(fill="x")
        ttk.Button(bar, text="✔ Use this candidate's name", style="Accent.TButton", command=use_this).pack(side="left")
        ttk.Button(bar, text="Copy", command=copy_row).pack(side="left", padx=6)
        ttk.Button(bar, text="Close", command=top.destroy).pack(side="right")
        show_hit(0)

    # ------------------------------------------------------------------ saving, undo, queue management
    def save_current(self) -> None:
        it = self._current()
        if it is None:
            if self.built:
                info(self.frame, "Select a badge in the queue first.")
            return
        if it.status == "reading":
            if self.built:
                info(self.frame, "Wait a moment — OCR is still reading this badge.")
            return
        out_str = str(self.get("output_folder") or "").strip()
        if not out_str:
            out_str = filedialog.askdirectory(title="Choose output folder for renamed badges", initialdir=self.input_dir())
            if not out_str:
                return
            self.put("output_folder", out_str)
            if self.built:
                self.out_var.set(out_str)

        out_dir = Path(out_str)
        if not it.path.is_file():
            if self.built:
                error(self.frame, f"Source file no longer exists: {it.path}")
            return

        if self.built:
            it.ar_name = self.ar_var.get().strip()
            it.en_name = self.en_var.get().strip()
            it.job_title = self.job_var.get().strip()
            it.badge_no = self.badge_var.get().strip()
            tpl = self.tpl_var.get().strip()
        else:
            tpl = str(self.get("filename_template") or B.DEFAULT_FILENAME_TEMPLATE)

        matched_name = it.selected_match.name_value if it.selected_match else ""
        stem = B.render_badge_stem(
            tpl,
            ar_name=it.ar_name,
            en_name=it.en_name,
            job_title=it.job_title,
            badge_no=it.badge_no,
            matched_name=matched_name,
            original=it.path,
        )
        action = str(self.get("action") or "move")
        policy = str(self.get("duplicate_policy") or "number")
        try:
            dest = transfer(it.path, out_dir, stem=stem, action=action, policy=policy,
                            journal=self.app.journal, tool=self.id, note=f"badge {it.badge_no}".strip())
        except OSError as exc:
            if self.built:
                error(self.frame, f"Could not save badge: {exc}")
            self.log(f"Save failed for {it.path.name}: {exc}", "error")
            return

        if dest is None:
            it.status = "skipped"
            self.log(f"Skipped existing badge: {stem}{it.path.suffix.lower()}", "warn")
        else:
            it.status = "saved"
            it.saved_path = dest
            if it.badge_no:
                self._saved_badges[it.badge_no] = dest.name
            self.app.notify(f"Saved: {dest.name}", "ok")
            self.log(f"{action.title()}d {it.path.name} -> {dest}", "ok")
            if bool(self.get("auto_log_csv")) and (self.get("append_file") or "").strip():
                self.append_rows([self._item_record(it)], ask=False, quiet=True)

        if self.built:
            self._upsert_queue_row(it)
            self._refresh_status()
            self._select_next_pending()

    def _select_next_pending(self) -> None:
        nxt = next((it.id for it in self.items.values() if it.status == "pending"), None)
        if nxt:
            self._select_item(nxt)

    def skip_current(self) -> None:
        it = self._current()
        if it is None:
            return
        it.status = "skipped"
        if self.built:
            self._upsert_queue_row(it)
            self._refresh_status()
            self._select_next_pending()

    def remove_selected(self) -> None:
        if not self.built:
            return
        sel = list(self.table.selection())
        for iid in sel:
            it = self.items.pop(iid, None)
            if it is not None:
                self._seen_paths.discard(str(it.path.resolve()))
            if self.current_id == iid:
                self.current_id = None
        self.table.remove(sel)
        self._refresh_status()
        self._select_next_pending()

    def clear_done(self) -> None:
        done_ids = [k for k, v in self.items.items() if v.status in ("saved", "skipped")]
        for iid in done_ids:
            it = self.items.pop(iid)
            self._seen_paths.discard(str(it.path.resolve()))
            if self.current_id == iid:
                self.current_id = None
        if self.built:
            self.table.remove(done_ids)
        self._refresh_status()

    def clear_all(self) -> None:
        self.items.clear()
        self._seen_paths.clear()
        self.current_id = None
        if self.built:
            self.table.clear()
            self.match_tree.delete(*self.match_tree.get_children())
            self._match_map.clear()
            self._set_tab_label(self._match_tab, "📋 Excel Matches")
            self.match_lbl.configure(text="Choose an Excel sheet to check candidate similarity.", style="Muted.TLabel")
            self.raw_text.delete("1.0", "end")
            self.viewer.clear("Add or select a badge to preview")
            self.ocr_info_lbl.configure(text="Add or select a badge to preview")
            self.preview_lbl.configure(text="—")
            self.dup_lbl.configure(text="")
            self.dup_lbl.grid_remove()
            for v in self._form_vars.values():
                v.set("")
            self._refresh_status()

    def undo_last(self) -> None:
        entry = self.app.journal.last_undoable(self.id)
        if entry is None:
            if self.built:
                info(self.frame, "Nothing to undo for Badge Extractor.")
            return
        try:
            msg = self.app.journal.undo(entry)
        except OSError as exc:
            if self.built:
                error(self.frame, str(exc))
            return
        # Restore item status if still in queue
        for it in self.items.values():
            if it.saved_path and str(it.saved_path) == entry.dest:
                it.status = "pending"
                it.saved_path = None
                if self.built:
                    self._upsert_queue_row(it)
                break
        self.app.notify(msg, "ok")
        self.log(f"Undo: {msg}", "info")
        self._refresh_status()

    def open_output_folder(self) -> None:
        out_str = str(self.get("output_folder") or "").strip()
        if not out_str:
            info(self.frame, "No output folder is set yet.")
            return
        p = Path(out_str)
        p.mkdir(parents=True, exist_ok=True)
        open_path(p)

    def _item_record(self, it: BadgeItem) -> dict[str, str]:
        m = it.selected_match or (it.matches[0] if it.matches else None)
        return {
            "Arabic Name": it.ar_name,
            "English Name": it.en_name,
            "Job Title": it.job_title,
            "Badge Number": it.badge_no,
            "Matched Sheet Name": (m.name_value or m.matched_value) if m else "",
            "Match %": f"{m.percent}%" if m else "",
            "Excel Row": str(m.row) if m else "",
            "Saved File": it.saved_path.name if it.saved_path else "",
            "Original File": it.path.name,
        }

    def export_csv(self) -> None:
        recs = [self._item_record(it) for it in self.items.values() if it.status in ("saved", "pending")]
        if not recs:
            info(self.frame, "No extracted badges to export yet.")
            return
        self.append_rows(recs, ask=True)

    def _refresh_status(self) -> None:
        if not self.built:
            return
        running = self.auto_running()
        self.watch_btn.configure(text="⏹ Stop watching" if running else "▶ Start watching")
        in_str = str(self.get("input_folder") or "").strip()
        out_str = str(self.get("output_folder") or "").strip()
        in_name = Path(in_str).name or in_str
        out_name = Path(out_str).name or out_str
        self.watch_badge.configure(text=f"● Watching {in_name}" if running else "Idle")

        pending = sum(1 for it in self.items.values() if it.status == "pending")
        saved = sum(1 for it in self.items.values() if it.status == "saved")
        multi = sum(1 for it in self.items.values() if it.status == "pending" and B.has_ambiguous_matches(it.matches))
        sheet_path, _, _, _ = self._effective_sheet()
        sheet_lbl = Path(sheet_path).name if sheet_path else "none"
        parts = [f"{pending} to review", f"{saved} saved"]
        if multi:
            parts.append(f"⚠ {multi} ambiguous")
        parts.append(f"Sheet: {sheet_lbl}")
        self.status_lbl.configure(text="  •  ".join(parts))

        if in_name and out_name:
            self.folders_summary_lbl.configure(text=f"📁 {in_name} → {out_name}")
        elif in_name or out_name:
            self.folders_summary_lbl.configure(text=f"📁 {in_name or 'no input'} → {out_name or 'no output'}")
        else:
            self.folders_summary_lbl.configure(text="Folders not set")

    # ------------------------------------------------------------------ shortcuts, commands, stats
    def keys(self) -> dict[str, Callable[[], None]]:
        return {
            "<Control-Return>": self.save_current,
            "<Control-o>": self.pick_files,
            "<F5>": self.reocr_current,
            "<Control-k>": self.show_candidate_info,
            "<Control-z>": self.undo_last,
        }

    def commands(self) -> list[Command]:
        return [
            Command("badge_extractor.add", "Badge Extractor: add badge images…", self.pick_files,
                    self.title, "badge ocr arabic id", "Ctrl+O"),
            Command("badge_extractor.watch", "Badge Extractor: start / stop watching folder", self.toggle_watching,
                    self.title, "watch folder auto"),
            Command("badge_extractor.candidate", "Badge Extractor: candidate info check", self.show_candidate_info,
                    self.title, "excel similarity candidate", "Ctrl+K"),
            Command("badge_extractor.save", "Badge Extractor: save & next badge", self.save_current,
                    self.title, "rename save", "Ctrl+Enter"),
        ]

    def stats(self) -> dict[str, int | str]:
        return {
            "Pending": sum(1 for it in self.items.values() if it.status == "pending"),
            "Saved": sum(1 for it in self.items.values() if it.status == "saved"),
        }

    def badge(self) -> str:
        pending = sum(1 for it in self.items.values() if it.status == "pending")
        return str(pending) if pending else ""
