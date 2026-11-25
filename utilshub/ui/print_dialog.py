"""Full-featured Print Center dialog: queue editor, layout/N-up/duplex/page-range settings, live WYSIWYG preview & Win32 spooler."""
from __future__ import annotations

import shutil
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable

from ..core import printing as P
from ..core.system import open_path, reveal
from .viewer import PageViewer
from .widgets import ScrollFrame, Tooltip, confirm, error, info, modal


def load_print_settings(app, prefix: str = "file_picker.print_") -> P.PrintSettings:
    """Load saved print preferences from `app.settings`."""
    g = app.settings.get
    gb = app.settings.get_bool
    gi = app.settings.get_int
    base_margin = max(0.0, float(g(prefix + "margin_mm", 6.0) or 6.0))

    def _m(key: str) -> float:
        raw = g(prefix + key, None)
        return base_margin if raw is None or raw == "" else max(0.0, float(raw))

    return P.PrintSettings(
        printer=str(g(prefix + "printer", "") or ""),
        copies=max(1, gi(prefix + "copies", 1)),
        collate=gb(prefix + "collate", True),
        duplex=str(g(prefix + "duplex", "simplex") or "simplex"),
        color_mode=str(g(prefix + "color_mode", "color") or "color"),
        orientation=str(g(prefix + "orientation", "auto") or "auto"),
        paper_size=str(g(prefix + "paper_size", "default") or "default"),
        scale_mode=str(g(prefix + "scale_mode", "fit") or "fit"),
        margin_mm=base_margin,
        margin_left_mm=_m("margin_left_mm"),
        margin_top_mm=_m("margin_top_mm"),
        margin_right_mm=_m("margin_right_mm"),
        margin_bottom_mm=_m("margin_bottom_mm"),
        nup=int(g(prefix + "nup", 1) or 1),
        nup_border=gb(prefix + "nup_border", True),
        page_scope=str(g(prefix + "page_scope", "all") or "all"),
        page_range=str(g(prefix + "page_range", "") or ""),
        page_subset=str(g(prefix + "page_subset", "all") or "all"),
        reverse_order=gb(prefix + "reverse_order", False),
        separator=str(g(prefix + "separator", "none") or "none"),
        header_text=str(g(prefix + "header_text", "") or ""),
        footer_text=str(g(prefix + "footer_text", "") or ""),
        job_mode=str(g(prefix + "job_mode", "combined") or "combined"),
        method=str(g(prefix + "method", "direct") or "direct"),
        delay_seconds=max(0.0, float(g(prefix + "delay_seconds", 0.0) or 0.0)),
        wait_for_job=gb(prefix + "wait_for_job", False),
        dpi=max(72, gi(prefix + "dpi", 200)),
        libreoffice_cmd=str(g("ocr.libreoffice_cmd", "") or ""),
    )


def save_print_settings(app, s: P.PrintSettings, prefix: str = "file_picker.print_") -> None:
    """Persist print preferences into `app.settings`."""
    p = app.settings.set
    for k in (
        "printer", "copies", "collate", "duplex", "color_mode", "orientation",
        "paper_size", "scale_mode", "margin_mm", "margin_left_mm", "margin_top_mm",
        "margin_right_mm", "margin_bottom_mm", "nup", "nup_border",
        "page_scope", "page_range", "page_subset", "reverse_order",
        "separator", "header_text", "footer_text", "job_mode", "method",
        "delay_seconds", "wait_for_job", "dpi",
    ):
        p(prefix + k, getattr(s, k))


class PrintDialog(tk.Toplevel):
    """Interactive Print Center with live WYSIWYG sheet preview and spooler management."""

    def __init__(self, parent: tk.Misc, app, items: list[P.PrintItem], tool=None,
                 settings_prefix: str = "file_picker.print_",
                 on_printed: Callable[[P.PrintSummary], None] | None = None,
                 on_visibility_change: Callable[[str], None] | None = None) -> None:
        super().__init__(parent.winfo_toplevel())
        self.app = app
        self.tool = tool
        self.settings_prefix = settings_prefix
        self.on_printed = on_printed
        self.on_visibility_change = on_visibility_change
        self.items: list[P.PrintItem] = list(items)
        self._printers: list[P.PrinterInfo] = []
        self._printer_map: dict[str, P.PrinterInfo] = {}
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="uh_print_"))
        self._preview_token = 0
        self._preview_after = None
        self._printing = False
        self._cancel_ev = threading.Event()
        self._closed = False
        self._failed_indices: list[int] = []
        self._item_status: dict[int, str] = {}
        self._excluded_sheets: set[int] = set()
        self._preview_labels: list[str] = []
        self._preview_groups: list[tuple[str, str]] = []
        self._batch_sheets: dict[str, list[int]] = {}
        self._batch_titles: dict[str, tuple[int, str]] = {}
        self._pv_list_open = True
        self._syncing_pv = False

        self.configure(background=app.theme.p["bg"])
        self.title(f"Print Center — {len(self.items)} file(s)")
        self.geometry("1120x720")
        self.minsize(920, 580)
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._init_vars(load_print_settings(app, settings_prefix))
        self._build_ui()
        self._center_over(parent.winfo_toplevel())

        self.bind("<Escape>", lambda _e: self._on_cancel_or_close())
        self.bind("<Control-Return>", lambda _e: self.start_print())
        self.bind("<Control-h>", lambda _e: self.hide_dialog())
        self.bind("<F5>", lambda _e: self.refresh_preview())

        self._refresh_printers()
        self._populate_queue()
        self._update_summary()
        self._count_pages_async()
        self.after(100, self.refresh_preview)

    def _center_over(self, root: tk.Misc) -> None:
        self.update_idletasks()
        x = root.winfo_rootx() + max(20, (root.winfo_width() - 1120) // 2)
        y = root.winfo_rooty() + max(20, (root.winfo_height() - 720) // 3)
        self.geometry(f"1120x720+{x}+{y}")

    # ------------------------------------------------------------------ variables
    def _init_vars(self, s: P.PrintSettings) -> None:
        self.printer_disp_var = tk.StringVar()
        self.copies_var = tk.IntVar(value=s.copies)
        self.collate_var = tk.BooleanVar(value=s.collate)
        self.duplex_var = tk.StringVar(value=dict(P.DUPLEX_MODES).get(s.duplex, P.DUPLEX_MODES[0][1]))
        self.color_var = tk.StringVar(value=dict(P.COLOR_MODES).get(s.color_mode, P.COLOR_MODES[0][1]))
        self.dpi_var = tk.IntVar(value=s.dpi)
        self.paper_var = tk.StringVar(value=dict(P.PAPER_SIZES).get(s.paper_size, P.PAPER_SIZES[0][1]))
        self.orient_var = tk.StringVar(value=dict(P.ORIENTATIONS).get(s.orientation, P.ORIENTATIONS[0][1]))
        self.scale_var = tk.StringVar(value=dict(P.SCALE_MODES).get(s.scale_mode, P.SCALE_MODES[0][1]))
        ml, mt, mr, mb = s.margins_mm()
        self.margin_left_var = tk.DoubleVar(value=ml)
        self.margin_top_var = tk.DoubleVar(value=mt)
        self.margin_right_var = tk.DoubleVar(value=mr)
        self.margin_bottom_var = tk.DoubleVar(value=mb)
        self.nup_var = tk.IntVar(value=s.nup if s.nup in (1, 2, 4) else 1)
        self.nup_border_var = tk.BooleanVar(value=s.nup_border)
        self.scope_var = tk.StringVar(value=dict(P.PAGE_SCOPES).get(s.page_scope, P.PAGE_SCOPES[0][1]))
        self.range_var = tk.StringVar(value=s.page_range)
        self.subset_var = tk.StringVar(value=dict(P.PAGE_SUBSETS).get(s.page_subset, P.PAGE_SUBSETS[0][1]))
        self.reverse_var = tk.BooleanVar(value=s.reverse_order)
        self.sep_var = tk.StringVar(value=dict(P.SEPARATOR_MODES).get(s.separator, P.SEPARATOR_MODES[0][1]))
        self.header_var = tk.StringVar(value=s.header_text)
        self.footer_var = tk.StringVar(value=s.footer_text)
        self.job_mode_var = tk.StringVar(value=dict(P.JOB_MODES).get(s.job_mode, P.JOB_MODES[0][1]))
        self.method_var = tk.StringVar(value=dict(P.PRINT_METHODS).get(s.method, P.PRINT_METHODS[0][1]))
        self.delay_var = tk.DoubleVar(value=s.delay_seconds)
        self.wait_job_var = tk.BooleanVar(value=s.wait_for_job)
        self._saved_printer = s.printer
        self._libreoffice_cmd = s.libreoffice_cmd

    @staticmethod
    def _safe_int(var: tk.Variable, default: int) -> int:
        try:
            return int(var.get())
        except (tk.TclError, ValueError, TypeError):
            return default

    @staticmethod
    def _safe_float(var: tk.Variable, default: float) -> float:
        try:
            return float(var.get())
        except (tk.TclError, ValueError, TypeError):
            return default

    @staticmethod
    def _key_for(choices: list[tuple[str, str]], label: str, default: str) -> str:
        for k, v in choices:
            if v == label:
                return k
        return default

    def current_settings(self) -> P.PrintSettings:
        pinfo = self._printer_map.get(self.printer_disp_var.get())
        pname = pinfo.name if pinfo else self._saved_printer
        return P.PrintSettings(
            printer=pname,
            copies=max(1, self._safe_int(self.copies_var, 1)),
            collate=bool(self.collate_var.get()),
            duplex=self._key_for(P.DUPLEX_MODES, self.duplex_var.get(), "simplex"),
            color_mode=self._key_for(P.COLOR_MODES, self.color_var.get(), "color"),
            orientation=self._key_for(P.ORIENTATIONS, self.orient_var.get(), "auto"),
            paper_size=self._key_for(P.PAPER_SIZES, self.paper_var.get(), "default"),
            scale_mode=self._key_for(P.SCALE_MODES, self.scale_var.get(), "fit"),
            margin_mm=max(0.0, self._safe_float(self.margin_left_var, 6.0)),
            margin_left_mm=max(0.0, self._safe_float(self.margin_left_var, 6.0)),
            margin_top_mm=max(0.0, self._safe_float(self.margin_top_var, 6.0)),
            margin_right_mm=max(0.0, self._safe_float(self.margin_right_var, 6.0)),
            margin_bottom_mm=max(0.0, self._safe_float(self.margin_bottom_var, 6.0)),
            nup=self._safe_int(self.nup_var, 1),
            nup_border=bool(self.nup_border_var.get()),
            page_scope=self._key_for(P.PAGE_SCOPES, self.scope_var.get(), "all"),
            page_range=self.range_var.get().strip(),
            page_subset=self._key_for(P.PAGE_SUBSETS, self.subset_var.get(), "all"),
            reverse_order=bool(self.reverse_var.get()),
            separator=self._key_for(P.SEPARATOR_MODES, self.sep_var.get(), "none"),
            header_text=self.header_var.get().strip(),
            footer_text=self.footer_var.get().strip(),
            job_mode=self._key_for(P.JOB_MODES, self.job_mode_var.get(), "combined"),
            method=self._key_for(P.PRINT_METHODS, self.method_var.get(), "direct"),
            delay_seconds=max(0.0, self._safe_float(self.delay_var, 0.0)),
            wait_for_job=bool(self.wait_job_var.get()),
            dpi=max(72, self._safe_int(self.dpi_var, 200)),
            libreoffice_cmd=self._libreoffice_cmd,
        )

    # ------------------------------------------------------------------ UI construction
    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # 1. Top Printer Bar
        pbar = ttk.Frame(outer)
        pbar.pack(fill="x", pady=(0, 8))
        ttk.Label(pbar, text="Printer:", style="H3.TLabel").pack(side="left", padx=(0, 6))
        self.printer_cb = ttk.Combobox(pbar, textvariable=self.printer_disp_var, state="readonly", width=40)
        self.printer_cb.pack(side="left")
        self.printer_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_printer_changed())

        self.printer_status_lbl = ttk.Label(pbar, text="Ready", style="Badge.TLabel")
        self.printer_status_lbl.pack(side="left", padx=(8, 0))

        hide_btn = ttk.Button(pbar, text="Hide", style="Tiny.TButton", command=self.hide_dialog)
        hide_btn.pack(side="right")
        Tooltip(hide_btn, "Hide Print Center without losing your queue, page ticks, or settings (Ctrl+H)")
        ttk.Button(pbar, text="Test Page", style="Tiny.TButton", command=self._send_test_page).pack(side="right", padx=(0, 4))
        ttk.Button(pbar, text="Properties…", style="Tiny.TButton",
                   command=lambda: P.open_printer_properties(self.current_settings().printer)).pack(side="right", padx=(0, 4))
        ttk.Button(pbar, text="Queue…", style="Tiny.TButton",
                   command=lambda: P.open_printer_queue(self.current_settings().printer)).pack(side="right", padx=(0, 4))
        ttk.Button(pbar, text="↻ Refresh", style="Tiny.TButton", command=self._refresh_printers).pack(side="right", padx=(0, 4))

        # 2. Pinned Bottom Action Bar (packed before PanedWindow so it never disappears)
        bot = ttk.Frame(outer)
        bot.pack(side="bottom", fill="x", pady=(8, 0))
        self.summary_lbl = ttk.Label(bot, text="", style="Muted.TLabel")
        self.summary_lbl.pack(side="left")

        self.progress = ttk.Progressbar(bot, mode="determinate", length=150)

        self.print_btn = ttk.Button(bot, text="🖨 Print Now  (Ctrl+Enter)", style="Accent.TButton", command=self.start_print)
        self.print_btn.pack(side="right")
        self.close_btn = ttk.Button(bot, text="Close", command=self._on_cancel_or_close)
        self.close_btn.pack(side="right", padx=(0, 6))
        self.retry_btn = ttk.Button(bot, text="↻ Retry Failed", command=self.retry_failed)
        self.save_pdf_btn = ttk.Button(bot, text="💾 Save Print PDF…", command=self.save_print_pdf)
        self.save_pdf_btn.pack(side="right", padx=(0, 6))

        ttk.Separator(outer, orient="horizontal").pack(side="bottom", fill="x", pady=(6, 0))

        # 3. Main Horizontal Split: Left Tabs | Right Live Preview
        self.panes = ttk.PanedWindow(outer, orient="horizontal")
        self.panes.pack(fill="both", expand=True)

        left = ttk.Frame(self.panes)
        self.panes.add(left, weight=5)
        self.nb = ttk.Notebook(left)
        self.nb.pack(fill="both", expand=True)

        self._queue_tab = ttk.Frame(self.nb, padding=8)
        self._settings_tab = ttk.Frame(self.nb, padding=4)
        self._extras_tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(self._queue_tab, text=f"📄 Queue ({len(self.items)})")
        self.nb.add(self._settings_tab, text="⚙ Layout & Pages")
        self.nb.add(self._extras_tab, text="✨ Covers & Spooler")

        self._build_queue_tab()
        self._build_settings_tab()
        self._build_extras_tab()

        # Right: Live WYSIWYG Preview
        right = ttk.LabelFrame(self.panes, text="Live Print Preview", padding=6)
        self.panes.add(right, weight=4)

        pv_bar = ttk.Frame(right)
        pv_bar.pack(fill="x", pady=(0, 4))
        ttk.Button(pv_bar, text="↻ Refresh", style="Tiny.TButton", command=self.refresh_preview).pack(side="left")
        ttk.Separator(pv_bar, orient="vertical").pack(side="left", fill="y", padx=6, pady=1)
        ttk.Button(pv_bar, text="◀", width=3, style="Tiny.TButton",
                   command=lambda: self.viewer.goto(self.viewer.current_page() - 1)).pack(side="left")
        self.pv_page_lbl = ttk.Label(pv_bar, text="– / –", width=9, anchor="center")
        self.pv_page_lbl.pack(side="left")
        ttk.Button(pv_bar, text="▶", width=3, style="Tiny.TButton",
                   command=lambda: self.viewer.goto(self.viewer.current_page() + 1)).pack(side="left")

        ttk.Button(pv_bar, text="+", width=2, style="Tiny.TButton",
                   command=lambda: self.viewer.zoom_by(1.2)).pack(side="right")
        ttk.Button(pv_bar, text="Fit", style="Tiny.TButton",
                   command=lambda: self.viewer.fit("page")).pack(side="right", padx=2)
        ttk.Button(pv_bar, text="−", width=2, style="Tiny.TButton",
                   command=lambda: self.viewer.zoom_by(1 / 1.2)).pack(side="right")

        self.preview_info_lbl = ttk.Label(right, text="Preparing preview…", style="Muted.TLabel")
        self.preview_info_lbl.pack(side="bottom", anchor="w", pady=(4, 0))

        # Page tick / exclusion strip
        sel_bar = ttk.Frame(right)
        sel_bar.pack(fill="x", pady=(0, 4))
        self.pv_list_btn = ttk.Button(sel_bar, text="◀ Pages", style="Tiny.TButton", command=self._toggle_pv_list)
        self.pv_list_btn.pack(side="left", padx=(0, 6))
        Tooltip(self.pv_list_btn, "Show or hide the sheet checklist")
        self.pv_include_var = tk.BooleanVar(value=True)
        self.pv_include_cb = ttk.Checkbutton(
            sel_bar, text="Include sheet 1", variable=self.pv_include_var,
            command=self._toggle_current_preview_sheet,
        )
        self.pv_include_cb.pack(side="left")
        self.pv_excluded_lbl = ttk.Label(sel_bar, text="", style="Err.TLabel")
        self.pv_excluded_lbl.pack(side="left", padx=(6, 0))

        ttk.Button(sel_bar, text="Invert", style="Tiny.TButton", command=self._invert_preview_sheets).pack(side="right")
        ttk.Button(sel_bar, text="None", style="Tiny.TButton",
                   command=lambda: self._set_all_preview_sheets(False)).pack(side="right", padx=2)
        ttk.Button(sel_bar, text="All", style="Tiny.TButton",
                   command=lambda: self._set_all_preview_sheets(True)).pack(side="right")

        # Current batch indicator bar
        batch_bar = ttk.Frame(right)
        batch_bar.pack(fill="x", pady=(0, 4))
        self.pv_batch_badge = ttk.Label(batch_bar, text="", style="Badge.TLabel")
        self.pv_batch_badge.pack(side="left")
        self.pv_kept_lbl = ttk.Label(batch_bar, text="", style="Muted.TLabel")
        self.pv_kept_lbl.pack(side="right")

        # Body: left batch/sheet checklist + right PageViewer
        self._pv_body = ttk.Frame(right)
        self._pv_body.pack(fill="both", expand=True)
        self._pv_body.rowconfigure(0, weight=1)
        self._pv_body.columnconfigure(0, weight=0, minsize=175)
        self._pv_body.columnconfigure(1, weight=1)

        self._pv_list_frame = ttk.Frame(self._pv_body)
        self._pv_list_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.pv_pages_tree = ttk.Treeview(
            self._pv_list_frame,
            columns=("sel", "pg", "src"),
            show="headings",
            selectmode="browse",
        )
        pv_ys = ttk.Scrollbar(self._pv_list_frame, orient="vertical", command=self.pv_pages_tree.yview)
        self.pv_pages_tree.configure(yscrollcommand=pv_ys.set)
        pv_ys.pack(side="right", fill="y")
        self.pv_pages_tree.pack(fill="both", expand=True)
        for col, head, w, anc in (
            ("sel", "✔", 26, "center"),
            ("pg", "#", 28, "e"),
            ("src", "Batch / Sheet", 112, "w"),
        ):
            self.pv_pages_tree.heading(col, text=head)
            self.pv_pages_tree.column(col, width=w, anchor=anc, stretch=(col == "src"))
        self.pv_pages_tree.tag_configure("group", foreground=self.app.theme.accent)
        self.pv_pages_tree.tag_configure("off", foreground=self.app.theme.p["muted"])
        self.pv_pages_tree.bind("<Button-1>", self._on_pv_tree_click)
        self.pv_pages_tree.bind("<<TreeviewSelect>>", self._on_pv_tree_select)
        self.pv_pages_tree.bind("<Double-1>", self._on_pv_tree_double)
        self.pv_pages_tree.bind("<space>", self._on_pv_tree_space)

        self.viewer = PageViewer(self._pv_body, self.app, show_toolbar=False)
        self.viewer.mode = "page"
        self.viewer.grid(row=0, column=1, sticky="nsew")
        self.viewer.canvas.bind("<Configure>", lambda _e: self._sync_preview_page_lbl(), add="+")
        self.viewer.canvas.bind("<space>", self._on_viewer_space, add="+")
        self.after(250, self._poll_preview_page_lbl)

    def _build_queue_tab(self) -> None:
        qbar = ttk.Frame(self._queue_tab)
        qbar.pack(fill="x", pady=(0, 6))
        ttk.Button(qbar, text="✔ All", style="Tiny.TButton", command=lambda: self._toggle_all(True)).pack(side="left")
        ttk.Button(qbar, text="☐ None", style="Tiny.TButton", command=lambda: self._toggle_all(False)).pack(side="left", padx=(4, 0))
        ttk.Separator(qbar, orient="vertical").pack(side="left", fill="y", padx=6, pady=1)
        ttk.Button(qbar, text="↑", width=3, style="Tiny.TButton", command=lambda: self._move_selected(-1)).pack(side="left")
        ttk.Button(qbar, text="↓", width=3, style="Tiny.TButton", command=lambda: self._move_selected(1)).pack(side="left", padx=(2, 0))
        ttk.Button(qbar, text="✎ Copies / Range…", style="Tiny.TButton", command=self._edit_item_overrides).pack(side="left", padx=(6, 0))

        self.sort_var = tk.StringVar(value=P.SORT_ORDERS[0][1])
        sort_cb = ttk.Combobox(qbar, textvariable=self.sort_var, values=[v for _, v in P.SORT_ORDERS],
                               state="readonly", width=18)
        sort_cb.pack(side="right")
        sort_cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_sort())
        ttk.Label(qbar, text="Order:", style="Muted.TLabel").pack(side="right", padx=(0, 4))

        host = ttk.Frame(self._queue_tab)
        host.pack(fill="both", expand=True)
        cols = ("sel", "name", "file", "pages", "copies", "range", "status")
        self.q_tree = ttk.Treeview(host, columns=cols, show="headings", selectmode="extended")
        ys = ttk.Scrollbar(host, orient="vertical", command=self.q_tree.yview)
        self.q_tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.q_tree.pack(fill="both", expand=True)
        for col, head, w, anc in (
            ("sel", "✔", 28, "center"),
            ("name", "Name", 115, "w"),
            ("file", "File", 170, "w"),
            ("pages", "Pages", 48, "e"),
            ("copies", "Copies", 50, "e"),
            ("range", "Range", 66, "w"),
            ("status", "Status", 78, "w"),
        ):
            self.q_tree.heading(col, text=head)
            self.q_tree.column(col, width=w, anchor=anc, stretch=(col == "file"))
        p = self.app.theme.p
        self.q_tree.tag_configure("off", foreground=p["muted"])
        self.q_tree.tag_configure("ok", foreground=p["ok"])
        self.q_tree.tag_configure("err", foreground=p["err"])
        self.q_tree.bind("<Double-1>", self._on_q_double)

        menu = tk.Menu(self.q_tree, tearoff=False)
        menu.add_command(label="Toggle ✔", command=self._toggle_selected)
        menu.add_command(label="Set Copies / Page Range…", command=self._edit_item_overrides)
        menu.add_separator()
        menu.add_command(label="Open file", command=lambda: self._each_selected(lambda it: open_path(it.path)))
        menu.add_command(label="Show in folder", command=lambda: self._each_selected(lambda it: reveal(it.path)))
        menu.add_separator()
        menu.add_command(label="Remove from print list", command=self._remove_selected)
        self.q_tree.bind("<Button-3>", lambda e: self._popup_q(e, menu))

    def _build_settings_tab(self) -> None:
        sf = ScrollFrame(self._settings_tab)
        sf.pack(fill="both", expand=True)
        body = sf.body
        body.columnconfigure(0, weight=1)

        # Copies & Quality
        g1 = ttk.LabelFrame(body, text="Copies & Quality", padding=8)
        g1.pack(fill="x", padx=4, pady=(4, 6))
        g1.columnconfigure(1, weight=1)
        g1.columnconfigure(3, weight=1)

        ttk.Label(g1, text="Copies:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        sp_c = ttk.Spinbox(g1, from_=1, to=99, width=6, textvariable=self.copies_var, command=self._on_setting_changed)
        sp_c.grid(row=0, column=1, sticky="w", pady=3)
        ttk.Checkbutton(g1, text="Collate (1,2,3  1,2,3)", variable=self.collate_var,
                        command=self._on_setting_changed).grid(row=0, column=2, columnspan=2, sticky="w", padx=(10, 0), pady=3)

        ttk.Label(g1, text="Two-sided:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        dup_cb = ttk.Combobox(g1, textvariable=self.duplex_var, values=[v for _, v in P.DUPLEX_MODES], state="readonly")
        dup_cb.grid(row=1, column=1, columnspan=3, sticky="ew", pady=3)
        dup_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_setting_changed())

        ttk.Label(g1, text="Color:").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
        col_cb = ttk.Combobox(g1, textvariable=self.color_var, values=[v for _, v in P.COLOR_MODES], state="readonly", width=22)
        col_cb.grid(row=2, column=1, sticky="ew", pady=3)
        col_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_setting_changed(preview=True))

        ttk.Label(g1, text="DPI:").grid(row=2, column=2, sticky="w", padx=(12, 6), pady=3)
        dpi_cb = ttk.Combobox(g1, textvariable=self.dpi_var, values=[150, 200, 300], state="readonly", width=8)
        dpi_cb.grid(row=2, column=3, sticky="w", pady=3)
        dpi_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_setting_changed())

        # Paper & Layout
        g2 = ttk.LabelFrame(body, text="Paper & Layout", padding=8)
        g2.pack(fill="x", padx=4, pady=(0, 6))
        g2.columnconfigure(1, weight=1)
        g2.columnconfigure(3, weight=1)

        ttk.Label(g2, text="Paper size:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        pap_cb = ttk.Combobox(g2, textvariable=self.paper_var, values=[v for _, v in P.PAPER_SIZES], state="readonly")
        pap_cb.grid(row=0, column=1, sticky="ew", pady=3)
        pap_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_setting_changed(preview=True))

        ttk.Label(g2, text="Orientation:").grid(row=0, column=2, sticky="w", padx=(12, 6), pady=3)
        ori_cb = ttk.Combobox(g2, textvariable=self.orient_var, values=[v for _, v in P.ORIENTATIONS], state="readonly")
        ori_cb.grid(row=0, column=3, sticky="ew", pady=3)
        ori_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_setting_changed(preview=True))

        ttk.Label(g2, text="Scaling:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        scl_cb = ttk.Combobox(g2, textvariable=self.scale_var, values=[v for _, v in P.SCALE_MODES], state="readonly")
        scl_cb.grid(row=1, column=1, columnspan=3, sticky="ew", pady=3)
        scl_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_setting_changed(preview=True))

        ttk.Label(g2, text="Margins / Shift (mm):").grid(row=2, column=0, sticky="nw", padx=(0, 6), pady=4)
        m_box = ttk.Frame(g2)
        m_box.grid(row=2, column=1, columnspan=3, sticky="ew", pady=3)
        m_specs = (
            ("Left:", self.margin_left_var, 0, 0),
            ("Top:", self.margin_top_var, 0, 2),
            ("Right:", self.margin_right_var, 1, 0),
            ("Bottom:", self.margin_bottom_var, 1, 2),
        )
        for lbl, mvar, r_i, c_i in m_specs:
            ttk.Label(m_box, text=lbl, style="Muted.TLabel").grid(row=r_i, column=c_i, sticky="w", padx=(0, 4), pady=2)
            sp = ttk.Spinbox(m_box, from_=0, to=100, increment=1, width=6, textvariable=mvar,
                             command=lambda: self._on_setting_changed(preview=True))
            sp.grid(row=r_i, column=c_i + 1, sticky="w", padx=(0, 12), pady=2)
            sp.bind("<Return>", lambda _e: self._on_setting_changed(preview=True))
            sp.bind("<FocusOut>", lambda _e: self._on_setting_changed(preview=True))
            Tooltip(sp, "Increase a side's margin (mm) to shift printed content away from a faded/unprinted printer edge.")
        rst_m = ttk.Button(m_box, text="Reset 6mm", style="Tiny.TButton", command=self._reset_margins)
        rst_m.grid(row=1, column=4, sticky="w", pady=2)

        ttk.Label(g2, text="Pages / sheet:").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=3)
        nup_row = ttk.Frame(g2)
        nup_row.grid(row=3, column=1, columnspan=3, sticky="w", pady=3)
        for n in (1, 2, 4):
            ttk.Radiobutton(nup_row, text=f"{n}-up", value=n, variable=self.nup_var,
                            command=lambda: self._on_setting_changed(preview=True)).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(nup_row, text="Draw cell borders", variable=self.nup_border_var,
                        command=lambda: self._on_setting_changed(preview=True)).pack(side="left", padx=(6, 0))

        # Page Selection
        g3 = ttk.LabelFrame(body, text="Page Selection", padding=8)
        g3.pack(fill="x", padx=4, pady=(0, 6))
        g3.columnconfigure(1, weight=1)
        g3.columnconfigure(3, weight=1)

        ttk.Label(g3, text="Pages:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        scp_cb = ttk.Combobox(g3, textvariable=self.scope_var, values=[v for _, v in P.PAGE_SCOPES], state="readonly")
        scp_cb.grid(row=0, column=1, sticky="ew", pady=3)
        scp_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_scope_changed())

        ttk.Label(g3, text="Range:").grid(row=0, column=2, sticky="w", padx=(12, 6), pady=3)
        self.range_entry = ttk.Entry(g3, textvariable=self.range_var)
        self.range_entry.grid(row=0, column=3, sticky="ew", pady=3)
        Tooltip(self.range_entry, "Examples: 1-3, 5, 7-")
        self.range_entry.bind("<FocusOut>", lambda _e: self._on_setting_changed(preview=True))

        ttk.Label(g3, text="Subset:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        sub_cb = ttk.Combobox(g3, textvariable=self.subset_var, values=[v for _, v in P.PAGE_SUBSETS], state="readonly")
        sub_cb.grid(row=1, column=1, sticky="ew", pady=3)
        sub_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_setting_changed(preview=True))

        ttk.Checkbutton(g3, text="Reverse page order", variable=self.reverse_var,
                        command=lambda: self._on_setting_changed(preview=True)).grid(row=1, column=2, columnspan=2, sticky="w", padx=(12, 0), pady=3)

        self._on_scope_changed(initial=True)
        for v in (self.copies_var, self.margin_left_var, self.margin_top_var, self.margin_right_var, self.margin_bottom_var):
            v.trace_add("write", lambda *_a: self._update_summary())
        sf.bind_children()

    def _build_extras_tab(self) -> None:
        g1 = ttk.LabelFrame(self._extras_tab, text="Cover Sheets & Headers / Footers", padding=8)
        g1.pack(fill="x", pady=(0, 6))
        g1.columnconfigure(1, weight=1)

        ttk.Label(g1, text="Separator:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        sep_cb = ttk.Combobox(g1, textvariable=self.sep_var, values=[v for _, v in P.SEPARATOR_MODES], state="readonly")
        sep_cb.grid(row=0, column=1, sticky="ew", pady=3)
        sep_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_setting_changed(preview=True))

        ttk.Label(g1, text="Header:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        he = ttk.Entry(g1, textvariable=self.header_var)
        he.grid(row=1, column=1, sticky="ew", pady=3)
        he.bind("<FocusOut>", lambda _e: self._on_setting_changed(preview=True))

        ttk.Label(g1, text="Footer:").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
        f_row = ttk.Frame(g1)
        f_row.grid(row=2, column=1, sticky="ew", pady=3)
        f_row.columnconfigure(0, weight=1)
        fe = ttk.Entry(f_row, textvariable=self.footer_var)
        fe.grid(row=0, column=0, sticky="ew")
        fe.bind("<FocusOut>", lambda _e: self._on_setting_changed(preview=True))
        ttk.Button(f_row, text="Preset", style="Tiny.TButton", command=self._apply_footer_preset).grid(row=0, column=1, padx=(4, 0))
        Tooltip(fe, "Placeholders: {name}, {file}, {page}, {total}, {date}")

        g2 = ttk.LabelFrame(self._extras_tab, text="Batching & Print Engine", padding=8)
        g2.pack(fill="x", pady=(0, 6))
        g2.columnconfigure(1, weight=1)

        ttk.Label(g2, text="Job batching:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        jm_cb = ttk.Combobox(g2, textvariable=self.job_mode_var, values=[v for _, v in P.JOB_MODES], state="readonly")
        jm_cb.grid(row=0, column=1, sticky="ew", pady=3)

        ttk.Label(g2, text="Engine:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        m_cb = ttk.Combobox(g2, textvariable=self.method_var, values=[v for _, v in P.PRINT_METHODS], state="readonly")
        m_cb.grid(row=1, column=1, sticky="ew", pady=3)

        ttk.Label(g2, text="Delay (sec):").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
        d_row = ttk.Frame(g2)
        d_row.grid(row=2, column=1, sticky="ew", pady=3)
        sp_d = ttk.Spinbox(d_row, from_=0, to=120, increment=0.5, width=7, textvariable=self.delay_var,
                           command=self._on_setting_changed)
        sp_d.pack(side="left")
        Tooltip(sp_d, "Pause (in seconds) between each file/batch (or copy). Works in all batching modes.")
        wait_cb = ttk.Checkbutton(d_row, text="Wait for printer before delay", variable=self.wait_job_var,
                                  command=self._on_setting_changed)
        wait_cb.pack(side="left", padx=(10, 0))
        Tooltip(wait_cb, "Wait until the previous job leaves the Windows print spooler before starting the delay countdown.")
        self.delay_var.trace_add("write", lambda *_a: self._update_summary())

        g3 = ttk.LabelFrame(self._extras_tab, text="Active Printer Spooler Queue", padding=8)
        g3.pack(fill="both", expand=True)
        sbar = ttk.Frame(g3)
        sbar.pack(fill="x", pady=(0, 4))
        ttk.Button(sbar, text="↻ Refresh", style="Tiny.TButton", command=self._refresh_spooler).pack(side="left")
        ttk.Button(sbar, text="Pause", style="Tiny.TButton", command=lambda: self._pause_resume(True)).pack(side="left", padx=4)
        ttk.Button(sbar, text="Resume", style="Tiny.TButton", command=lambda: self._pause_resume(False)).pack(side="left")
        ttk.Button(sbar, text="Cancel Job", style="Tiny.TButton", command=self._cancel_spool_job).pack(side="right")

        self.spool_tree = ttk.Treeview(g3, columns=("id", "doc", "pages", "status"), show="headings", height=5, selectmode="browse")
        for col, head, w, anc in (("id", "ID", 52, "e"), ("doc", "Document", 200, "w"),
                                  ("pages", "Pages", 55, "e"), ("status", "Status", 95, "w")):
            self.spool_tree.heading(col, text=head)
            self.spool_tree.column(col, width=w, anchor=anc, stretch=(col == "doc"))
        self.spool_tree.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ printers & spooler
    def _refresh_printers(self) -> None:
        self._printers = P.list_printers()
        self._printer_map.clear()
        vals: list[str] = []
        selected = ""
        wanted = self._saved_printer
        for p in self._printers:
            disp = f"★ {p.name}" if p.is_default else p.name
            self._printer_map[disp] = p
            vals.append(disp)
            if wanted and p.name.casefold() == wanted.casefold():
                selected = disp
            elif not selected and p.is_default:
                selected = disp
        if not selected and vals:
            selected = vals[0]
        self.printer_cb.configure(values=vals)
        if selected:
            self.printer_disp_var.set(selected)
        self._on_printer_changed()

    def _on_printer_changed(self) -> None:
        pinfo = self._printer_map.get(self.printer_disp_var.get())
        if pinfo is None:
            self.printer_status_lbl.configure(text="No printer", style="Err.TLabel")
            return
        self._saved_printer = pinfo.name
        st = pinfo.status or "Ready"
        low = st.lower()
        style = "Err.TLabel" if any(k in low for k in ("error", "offline", "jam", "out")) else (
            "Warn.TLabel" if any(k in low for k in ("paused", "low", "waiting")) else "Badge.TLabel"
        )
        suffix = f" ({pinfo.jobs} queued)" if pinfo.jobs else ""
        self.printer_status_lbl.configure(text=f"{st}{suffix}", style=style)
        self._refresh_spooler()

    def _refresh_spooler(self) -> None:
        if not hasattr(self, "spool_tree"):
            return
        self.spool_tree.delete(*self.spool_tree.get_children())
        pname = self.current_settings().printer
        if not pname:
            return
        for j in P.list_print_jobs(pname):
            self.spool_tree.insert("", "end", iid=str(j.job_id), values=(j.job_id, j.document, j.pages or "—", j.status))

    def _pause_resume(self, pause: bool) -> None:
        pname = self.current_settings().printer
        if not pname:
            return
        try:
            P.pause_printer(pname, pause)
            self._refresh_printers()
        except P.PrintError as exc:
            error(self, str(exc))

    def _cancel_spool_job(self) -> None:
        sel = self.spool_tree.selection()
        pname = self.current_settings().printer
        if not sel or not pname:
            return
        try:
            P.cancel_print_job(pname, int(sel[0]))
            self._refresh_spooler()
        except Exception as exc:
            error(self, str(exc))

    def _send_test_page(self) -> None:
        pname = self.current_settings().printer
        if not pname:
            info(self, "Select a printer first.")
            return
        if not confirm(self, f"Send a 1-page test page to:\n{pname}?"):
            return

        def work():
            try:
                P.print_test_page(pname)
                self.app.bridge.call(lambda: self.winfo_exists() and self.app.notify(f"Test page sent to {pname}.", "ok"))
            except Exception as exc:
                self.app.bridge.call(lambda m=str(exc): self.winfo_exists() and error(self, f"Test page failed: {m}"))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ queue interactions
    def _populate_queue(self) -> None:
        self.q_tree.delete(*self.q_tree.get_children())
        for idx, it in enumerate(self.items):
            pg = str(it.page_count) if it.page_count is not None else "…"
            cp = str(it.copies) if it.copies > 0 else "auto"
            rg = it.page_range or "auto"
            st = self._item_status.get(idx, "Ready" if it.enabled else "Skipped")
            tags = ("err",) if "Failed" in st else ("ok",) if "Sent" in st else (() if it.enabled else ("off",))
            self.q_tree.insert("", "end", iid=str(idx),
                               values=("✔" if it.enabled else "—", it.label, it.path.name, pg, cp, rg, st),
                               tags=tags)
        self._set_tab_label(self._queue_tab, f"📄 Queue ({sum(1 for i in self.items if i.enabled)}/{len(self.items)})")

    def _set_tab_label(self, tab: ttk.Frame, text: str) -> None:
        try:
            self.nb.tab(tab, text=text)
        except (tk.TclError, TypeError):
            pass

    def _count_pages_async(self) -> None:
        items_snapshot = list(enumerate(self.items))
        lo_cmd = self._libreoffice_cmd

        def work():
            counts = []
            for idx, it in items_snapshot:
                if self._closed:
                    return
                counts.append((idx, P.count_file_pages(it.path, lo_cmd)))
            self.app.bridge.call(self._apply_page_counts, counts)

        threading.Thread(target=work, daemon=True).start()

    def _apply_page_counts(self, counts: list[tuple[int, int | None]]) -> None:
        if self._closed or not self.winfo_exists():
            return
        for idx, cnt in counts:
            if 0 <= idx < len(self.items):
                self.items[idx].page_count = cnt
        self._populate_queue()
        self._update_summary()

    def _on_q_double(self, event) -> None:
        iid = self.q_tree.identify_row(event.y)
        if iid and iid.isdigit():
            idx = int(iid)
            self.items[idx].enabled = not self.items[idx].enabled
            self._populate_queue()
            self._update_summary()
            self._schedule_preview()

    def _toggle_selected(self) -> None:
        for iid in self.q_tree.selection():
            if iid.isdigit():
                idx = int(iid)
                self.items[idx].enabled = not self.items[idx].enabled
        self._populate_queue()
        self._update_summary()
        self._schedule_preview()

    def _toggle_all(self, on: bool) -> None:
        for it in self.items:
            it.enabled = on
        self._populate_queue()
        self._update_summary()
        self._schedule_preview()

    def _move_selected(self, delta: int) -> None:
        sel = [int(i) for i in self.q_tree.selection() if i.isdigit()]
        if len(sel) != 1:
            return
        i = sel[0]
        j = i + delta
        if not (0 <= j < len(self.items)):
            return
        self.items[i], self.items[j] = self.items[j], self.items[i]
        self._item_status.clear()
        self._populate_queue()
        self.q_tree.selection_set(str(j))
        self._schedule_preview()

    def _apply_sort(self) -> None:
        order = self._key_for(P.SORT_ORDERS, self.sort_var.get(), "keep")
        if order != "keep":
            self.items = P.sort_print_items(self.items, order)
            self._item_status.clear()
            self._populate_queue()
            self._schedule_preview()

    def _each_selected(self, fn: Callable[[P.PrintItem], None]) -> None:
        for iid in self.q_tree.selection():
            if iid.isdigit():
                fn(self.items[int(iid)])

    def _remove_selected(self) -> None:
        sel = sorted((int(i) for i in self.q_tree.selection() if i.isdigit()), reverse=True)
        for idx in sel:
            self.items.pop(idx)
        self._item_status.clear()
        self._populate_queue()
        self._update_summary()
        self._schedule_preview()

    def _popup_q(self, event, menu: tk.Menu) -> None:
        iid = self.q_tree.identify_row(event.y)
        if iid and iid not in self.q_tree.selection():
            self.q_tree.selection_set(iid)
        menu.tk_popup(event.x_root, event.y_root)

    def _edit_item_overrides(self) -> None:
        sel = [int(i) for i in self.q_tree.selection() if i.isdigit()]
        if not sel:
            info(self, "Select one or more files in the queue first.")
            return
        first = self.items[sel[0]]
        top, body = modal(self, f"Item Print Options ({len(sel)} file(s))", 380, 0)
        top.transient(self)
        c_var = tk.IntVar(value=first.copies)
        r_var = tk.StringVar(value=first.page_range)
        ttk.Label(body, text="Copies override (0 = use global setting):").pack(anchor="w")
        ttk.Spinbox(body, from_=0, to=99, textvariable=c_var, width=8).pack(anchor="w", pady=(2, 8))
        ttk.Label(body, text="Page range override (empty = use global; e.g. 1-2, 4):").pack(anchor="w")
        ttk.Entry(body, textvariable=r_var, width=32).pack(fill="x", pady=(2, 10))

        def ok(_e=None):
            for idx in sel:
                self.items[idx].copies = max(0, int(c_var.get() or 0))
                self.items[idx].page_range = r_var.get().strip()
            top.destroy()
            self._populate_queue()
            self._update_summary()
            self._schedule_preview()

        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Button(row, text="Apply", style="Accent.TButton", command=ok).pack(side="right")
        ttk.Button(row, text="Cancel", command=top.destroy).pack(side="right", padx=6)
        top.bind("<Return>", ok)
        top.bind("<Escape>", lambda _e: top.destroy())

    # ------------------------------------------------------------------ settings & preview
    def _on_scope_changed(self, initial: bool = False) -> None:
        scope = self._key_for(P.PAGE_SCOPES, self.scope_var.get(), "all")
        self.range_entry.configure(state="normal" if scope == "range" else "disabled")
        if not initial:
            self._on_setting_changed(preview=True)

    def _apply_footer_preset(self) -> None:
        self.footer_var.set("{name}  •  {file}  •  Page {page} of {total}")
        self._on_setting_changed(preview=True)

    def _reset_margins(self) -> None:
        for v in (self.margin_left_var, self.margin_top_var, self.margin_right_var, self.margin_bottom_var):
            v.set(6.0)
        self._on_setting_changed(preview=True)

    def _on_setting_changed(self, preview: bool = False) -> None:
        self._update_summary()
        if preview:
            self._schedule_preview()

    def _update_summary(self) -> None:
        s = self.current_settings()
        enabled = sum(1 for it in self.items if it.enabled)
        pages, sheets = P.estimate_sheets(self.items, s)
        excl = len(self._excluded_sheets)
        if self._preview_labels and excl:
            kept = max(0, len(self._preview_labels) - excl)
            copies = max(1, int(s.copies or 1))
            total_faces = kept * copies
            sheets = (total_faces + 1) // 2 if s.duplex in ("long", "short") else total_faces
            parts = [f"{enabled} file(s)", f"{kept}/{len(self._preview_labels)} sheet(s) kept", f"{excl} excluded", f"~{sheets} sheet(s)"]
        else:
            parts = [f"{enabled} file(s)", f"~{pages} page(s)", f"~{sheets} sheet(s)"]
        if s.duplex in ("long", "short"):
            parts.append("2-sided")
        if s.nup > 1:
            parts.append(f"{s.nup}-up")
        if s.color_mode == "grayscale":
            parts.append("Grayscale")
        if s.delay_seconds > 0:
            parts.append(f"Delay {s.delay_seconds:g}s")
        ml, mt, mr, mb = s.margins_mm()
        if len({round(ml, 1), round(mt, 1), round(mr, 1), round(mb, 1)}) > 1:
            parts.append(f"Margins L{ml:g}/T{mt:g}/R{mr:g}/B{mb:g}mm")
        self.summary_lbl.configure(text="  •  ".join(parts))

    def _schedule_preview(self) -> None:
        if self._preview_after:
            self.after_cancel(self._preview_after)
        self._preview_after = self.after(300, self.refresh_preview)

    def refresh_preview(self) -> None:
        self._preview_after = None
        if self._closed:
            return
        enabled = [it for it in self.items if it.enabled]
        if not enabled:
            self._preview_labels = []
            self._preview_groups = []
            self._excluded_sheets.clear()
            self._populate_pv_pages_tree()
            self.viewer.clear("Tick at least one file to preview.")
            self.preview_info_lbl.configure(text="No files selected.")
            self._update_summary()
            return
        self._preview_token += 1
        tok = self._preview_token
        settings = self.current_settings()
        out_pdf = self._tmp_dir / f"preview_{tok}.pdf"
        self.preview_info_lbl.configure(text="Building WYSIWYG print preview…")

        def work():
            try:
                comp = P.compose_print_pdf(enabled, settings, out_pdf, include_copies=False)
                self.app.bridge.call(self._preview_ready, tok, comp, "")
            except Exception as exc:
                self.app.bridge.call(self._preview_ready, tok, None, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _preview_ready(self, tok: int, comp: P.ComposedPrint | None, err: str) -> None:
        if self._closed or not self.winfo_exists() or tok != self._preview_token:
            return
        if err or comp is None:
            self._preview_labels = []
            self._preview_groups = []
            self._excluded_sheets.clear()
            self._populate_pv_pages_tree()
            self.viewer.clear(f"Preview unavailable:\n{err}")
            self.preview_info_lbl.configure(text=err or "Preview unavailable.")
            self._update_summary()
            return

        if comp.sheet_labels != self._preview_labels or comp.sheet_groups != self._preview_groups:
            self._excluded_sheets.clear()
        self._preview_labels = list(comp.sheet_labels)
        self._preview_groups = list(comp.sheet_groups)

        # Build per-batch grouping & section header banners for PageViewer
        self._batch_sheets.clear()
        self._batch_titles.clear()
        batch_order: list[str] = []
        raw_titles: dict[str, str] = {}
        for idx, (bid, btitle) in enumerate(self._preview_groups):
            if bid not in self._batch_sheets:
                batch_order.append(bid)
                self._batch_sheets[bid] = []
                raw_titles[bid] = btitle
            self._batch_sheets[bid].append(idx)

        section_headers: dict[int, tuple[str, str]] = {}
        total_batches = len(batch_order)
        for bnum, bid in enumerate(batch_order, 1):
            idxs = self._batch_sheets[bid]
            btitle = raw_titles[bid]
            self._batch_titles[bid] = (bnum, btitle)
            banner_title = f"Batch {bnum}/{total_batches}  •  {btitle}" if total_batches > 1 else btitle
            section_headers[idxs[0]] = (banner_title, f"{len(idxs)} sheet(s)")

        self.viewer.show(comp.pdf_path)
        self.viewer.set_section_headers(section_headers)
        self.viewer.set_excluded_pages(self._excluded_sheets)
        self._populate_pv_pages_tree()
        self._sync_preview_page_lbl()
        self._update_summary()
        note = f"Preview: {comp.total_sheets} sheet(s) across {total_batches} batch(es)"
        if comp.skipped:
            note += f"  •  {len(comp.skipped)} skipped (shell print supported)"
        self.preview_info_lbl.configure(text=note)

    def _populate_pv_pages_tree(self) -> None:
        self._syncing_pv = True
        try:
            self.pv_pages_tree.delete(*self.pv_pages_tree.get_children())
            seen_batches: set[str] = set()
            for idx, lbl in enumerate(self._preview_labels):
                bid, _ = self._preview_groups[idx] if idx < len(self._preview_groups) else ("0", "")
                if bid and bid not in seen_batches:
                    seen_batches.add(bid)
                    bnum, btitle = self._batch_titles.get(bid, (1, bid))
                    self.pv_pages_tree.insert(
                        "", "end", iid=f"g:{bid}",
                        values=(self._batch_mark(bid), "", f"Batch {bnum}: {btitle}"),
                        tags=("group",),
                    )
                excl = idx in self._excluded_sheets
                self.pv_pages_tree.insert(
                    "", "end", iid=f"s:{idx}",
                    values=("—" if excl else "✔", str(idx + 1), lbl),
                    tags=("off",) if excl else (),
                )
        finally:
            self._syncing_pv = False
        self._update_kept_badge()

    def _batch_mark(self, bid: str) -> str:
        idxs = self._batch_sheets.get(bid, [])
        if not idxs:
            return "—"
        kept = sum(1 for i in idxs if i not in self._excluded_sheets)
        if kept == len(idxs):
            return "✔"
        if kept == 0:
            return "—"
        return "◐"

    def _update_kept_badge(self) -> None:
        total = len(self._preview_labels)
        if not total:
            self.pv_kept_lbl.configure(text="")
            return
        kept = total - len(self._excluded_sheets)
        self.pv_kept_lbl.configure(text=f"{kept}/{total} sheets selected")

    def _toggle_pv_list(self) -> None:
        self._pv_list_open = not self._pv_list_open
        if self._pv_list_open:
            self._pv_list_frame.grid()
            self._pv_body.columnconfigure(0, minsize=175)
            self.pv_list_btn.configure(text="◀ Pages")
        else:
            self._pv_list_frame.grid_remove()
            self._pv_body.columnconfigure(0, minsize=0)
            self.pv_list_btn.configure(text="▶ Pages")

    def _toggle_preview_sheet(self, idx: int, include: bool | None = None) -> None:
        if not (0 <= idx < len(self._preview_labels)):
            return
        if include is None:
            include = idx in self._excluded_sheets
        if include:
            self._excluded_sheets.discard(idx)
        else:
            self._excluded_sheets.add(idx)

        excl = idx in self._excluded_sheets
        iid = f"s:{idx}"
        if self.pv_pages_tree.exists(iid):
            self.pv_pages_tree.item(
                iid,
                values=("—" if excl else "✔", str(idx + 1), self._preview_labels[idx]),
                tags=("off",) if excl else (),
            )
        if idx < len(self._preview_groups):
            bid = self._preview_groups[idx][0]
            giid = f"g:{bid}"
            if self.pv_pages_tree.exists(giid):
                bnum, btitle = self._batch_titles.get(bid, (1, bid))
                self.pv_pages_tree.item(giid, values=(self._batch_mark(bid), "", f"Batch {bnum}: {btitle}"))
        self.viewer.set_excluded_pages(self._excluded_sheets)
        self._update_kept_badge()
        self._sync_preview_page_lbl()
        self._update_summary()

    def _toggle_batch(self, bid: str) -> None:
        idxs = self._batch_sheets.get(bid, [])
        if not idxs:
            return
        all_kept = all(i not in self._excluded_sheets for i in idxs)
        for i in idxs:
            self._toggle_preview_sheet(i, include=not all_kept)

    def _toggle_current_preview_sheet(self) -> None:
        if not self._preview_labels:
            return
        cur = self.viewer.current_page() if self.viewer.pages else 0
        self._toggle_preview_sheet(cur, include=bool(self.pv_include_var.get()))

    def _on_viewer_space(self, _e=None):
        if self._preview_labels and self.viewer.pages:
            cur = self.viewer.current_page()
            self._toggle_preview_sheet(cur)
            return "break"
        return None

    def _set_all_preview_sheets(self, include: bool) -> None:
        if include:
            self._excluded_sheets.clear()
        else:
            self._excluded_sheets = set(range(len(self._preview_labels)))
        self.viewer.set_excluded_pages(self._excluded_sheets)
        self._populate_pv_pages_tree()
        self._sync_preview_page_lbl()
        self._update_summary()

    def _invert_preview_sheets(self) -> None:
        total = len(self._preview_labels)
        self._excluded_sheets = set(range(total)) - self._excluded_sheets
        self.viewer.set_excluded_pages(self._excluded_sheets)
        self._populate_pv_pages_tree()
        self._sync_preview_page_lbl()
        self._update_summary()

    def _on_pv_tree_click(self, event):
        if self.pv_pages_tree.identify_region(event.x, event.y) != "cell" or self.pv_pages_tree.identify_column(event.x) != "#1":
            return None
        iid = self.pv_pages_tree.identify_row(event.y)
        if iid.startswith("s:"):
            self._toggle_preview_sheet(int(iid[2:]))
            return "break"
        if iid.startswith("g:"):
            self._toggle_batch(iid[2:])
            return "break"
        return None

    def _on_pv_tree_select(self, _e=None) -> None:
        if self._syncing_pv:
            return
        sel = self.pv_pages_tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("s:"):
            self.viewer.goto(int(iid[2:]))
        elif iid.startswith("g:"):
            idxs = self._batch_sheets.get(iid[2:], [])
            if idxs:
                self.viewer.goto(idxs[0])

    def _on_pv_tree_double(self, event) -> None:
        iid = self.pv_pages_tree.identify_row(event.y)
        if iid.startswith("s:"):
            self._toggle_preview_sheet(int(iid[2:]))
        elif iid.startswith("g:"):
            self._toggle_batch(iid[2:])

    def _on_pv_tree_space(self, _e=None):
        sel = self.pv_pages_tree.selection()
        if not sel:
            return None
        iid = sel[0]
        if iid.startswith("s:"):
            self._toggle_preview_sheet(int(iid[2:]))
            return "break"
        if iid.startswith("g:"):
            self._toggle_batch(iid[2:])
            return "break"
        return None

    def _sync_preview_page_lbl(self) -> None:
        if not hasattr(self, "pv_page_lbl"):
            return
        if not self.viewer.pages:
            self.pv_page_lbl.configure(text="– / –")
            self.pv_batch_badge.configure(text="")
            self.pv_excluded_lbl.configure(text="")
            return
        cur = self.viewer.current_page()
        self.pv_page_lbl.configure(text=f"{cur + 1} / {self.viewer.total_pages}")
        if cur < len(self._preview_labels):
            excl = cur in self._excluded_sheets
            self.pv_include_var.set(not excl)
            self.pv_include_cb.configure(text=f"Include sheet {cur + 1}")
            self.pv_excluded_lbl.configure(text="⊘ Excluded" if excl else "")
            if cur < len(self._preview_groups):
                bid = self._preview_groups[cur][0]
                bnum, btitle = self._batch_titles.get(bid, (1, ""))
                total_b = len(self._batch_titles)
                badge_txt = f"Batch {bnum}/{total_b}: {btitle}" if total_b > 1 else btitle
                self.pv_batch_badge.configure(text=badge_txt[:52] + ("…" if len(badge_txt) > 52 else ""))
            siid = f"s:{cur}"
            if self.pv_pages_tree.exists(siid) and self.pv_pages_tree.selection() != (siid,):
                self._syncing_pv = True
                try:
                    self.pv_pages_tree.selection_set(siid)
                    self.pv_pages_tree.see(siid)
                finally:
                    self._syncing_pv = False

    def _poll_preview_page_lbl(self) -> None:
        if self._closed or not self.winfo_exists():
            return
        self._sync_preview_page_lbl()
        self.after(300, self._poll_preview_page_lbl)

    # ------------------------------------------------------------------ save PDF & execute print
    def save_print_pdf(self) -> None:
        enabled = [it for it in self.items if it.enabled]
        if not enabled:
            info(self, "Tick at least one file first.")
            return
        if self._preview_labels and len(self._excluded_sheets) >= len(self._preview_labels):
            info(self, "All sheets are unticked — tick at least one sheet to save.")
            return
        init_dir = self.tool.output_dir() if (self.tool and hasattr(self.tool, "output_dir")) else None
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save print packet as PDF",
            initialdir=init_dir,
            initialfile="print-packet.pdf",
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf")],
        )
        if not path:
            return
        settings = self.current_settings()
        try:
            comp = P.compose_print_pdf(
                enabled, settings, Path(path),
                include_copies=False, include_item_copies=True,
                excluded_sheets=set(self._excluded_sheets) if self._excluded_sheets else None,
            )
            self.app.notify(f"Saved print PDF ({comp.total_sheets} sheets).", "ok")
        except Exception as exc:
            error(self, f"Could not save print PDF: {exc}")

    def start_print(self, indices: list[int] | None = None) -> None:
        if self._printing:
            return
        s = self.current_settings()
        if not s.printer:
            info(self, "No printer selected or available.")
            return
        if indices is None:
            targets = list(self.items)
            if self._preview_labels and len(self._excluded_sheets) >= len(self._preview_labels):
                info(self, "All sheets are unticked — tick at least one sheet to print.")
                return
            excl = set(self._excluded_sheets) if self._excluded_sheets else None
        else:
            targets = [P.PrintItem(it.path, it.label, it.copies, it.page_range,
                                   enabled=(idx in indices), page_count=it.page_count)
                       for idx, it in enumerate(self.items)]
            excl = None
        enabled_cnt = sum(1 for it in targets if it.enabled)
        if not enabled_cnt:
            info(self, "Tick at least one file to print.")
            return

        save_print_settings(self.app, s, self.settings_prefix)
        self._printing = True
        self._cancel_ev.clear()
        self._failed_indices.clear()
        self.retry_btn.pack_forget()
        self.print_btn.configure(state="disabled")
        self.save_pdf_btn.configure(state="disabled")
        self.close_btn.configure(text="Cancel Print")
        self.progress.configure(value=0, maximum=max(1, enabled_cnt))
        self.progress.pack(side="left", padx=(12, 0))

        def on_prog(done_n: int, total_n: int, msg: str) -> None:
            self.app.bridge.call(self._ui_progress, done_n, total_n, msg)

        def on_item_status(idx: int, st: str) -> None:
            self.app.bridge.call(self._ui_item_status, idx, st)

        def work():
            try:
                summary = P.print_batch(
                    targets, s,
                    progress_cb=on_prog,
                    item_status_cb=on_item_status,
                    cancelled=self._cancel_ev.is_set,
                    excluded_sheets=excl,
                )
                self.app.bridge.call(self._ui_print_done, summary, "")
            except Exception as exc:
                self.app.bridge.call(self._ui_print_done, None, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def retry_failed(self) -> None:
        if self._failed_indices:
            self.start_print(list(self._failed_indices))

    def _ui_progress(self, done_n: int, total_n: int, msg: str) -> None:
        if self._closed or not self.winfo_exists():
            return
        self.progress.configure(maximum=max(1, total_n), value=done_n)
        self.summary_lbl.configure(text=msg)

    def _ui_item_status(self, idx: int, st: str) -> None:
        if self._closed or not self.winfo_exists():
            return
        self._item_status[idx] = st
        if "Failed" in st and idx not in self._failed_indices:
            self._failed_indices.append(idx)
        iid = str(idx)
        if self.q_tree.exists(iid):
            vals = list(self.q_tree.item(iid, "values"))
            vals[-1] = st
            tag = ("err",) if "Failed" in st else ("ok",) if "Sent" in st else ()
            self.q_tree.item(iid, values=vals, tags=tag)

    def _ui_print_done(self, summary: P.PrintSummary | None, err: str) -> None:
        if self._closed or not self.winfo_exists():
            return
        self._printing = False
        self.progress.pack_forget()
        self.print_btn.configure(state="normal")
        self.save_pdf_btn.configure(state="normal")
        self.close_btn.configure(text="Close")
        self._refresh_printers()

        if err or summary is None:
            self._update_summary()
            error(self, f"Printing failed: {err}")
            return
        if summary.cancelled:
            self.summary_lbl.configure(text="Print cancelled.")
            return
        if summary.failed_files:
            self.retry_btn.pack(side="right", padx=(0, 6))
        msg = f"Sent {summary.printed_files}/{summary.total_files} file(s) ({summary.total_sheets} sheet(s)) to printer."
        self.summary_lbl.configure(text=msg)
        self.app.notify(msg, "warn" if summary.failed_files else "ok")
        if self.tool:
            self.tool.log(msg, "warn" if summary.failed_files else "ok")
        if summary.errors:
            detail = "\n".join(f"• {n}: {e}" for n, e in summary.errors[:8])
            error(self, f"Some files could not be printed:\n\n{detail}")
        if self.on_printed:
            self.on_printed(summary)

    # ------------------------------------------------------------------ hide / restore / close
    def hide_dialog(self) -> None:
        self.withdraw()
        if self.on_visibility_change:
            self.on_visibility_change("hidden")
        self.app.notify("Print Center hidden — click '🖨 Show Print…' in File Picker to restore it.", "info")

    def restore_dialog(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()
        if self.on_visibility_change:
            self.on_visibility_change("shown")

    def _on_cancel_or_close(self) -> None:
        if self._printing:
            self._cancel_ev.set()
            self.summary_lbl.configure(text="Cancelling print job…")
        else:
            self._on_close()

    def _on_close(self) -> None:
        if self._printing:
            self._cancel_ev.set()
        self._closed = True
        if self._preview_after:
            try:
                self.after_cancel(self._preview_after)
            except Exception:
                pass
        try:
            save_print_settings(self.app, self.current_settings(), self.settings_prefix)
        except Exception:
            pass
        try:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        except Exception:
            pass
        if self.on_visibility_change:
            try:
                self.on_visibility_change("closed")
            except Exception:
                pass
        self.destroy()
