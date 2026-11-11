"""Manual page editor for the Scan Cleaner.

    CleanEditor(tool, entry)        # a window; changes are stored in entry.edits[page] as PageEdit objects

Three views of the page:  Original  |  Edit (turned / straightened, with the crop frame)  |  Result.
Everything the program detected (turned, tilted, borders) is the starting point; every control can go back to
"Auto".  Drag on the picture in the Edit view to draw your own crop.
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import ttk

from PIL import ImageTk

from ..core import scanclean as sc
from ..core.imaging import fit_within
from ..core.ocr import detect_skew
from ..core.scanclean import PageEdit
from .widgets import Tooltip, attach_window_controls, error

ENHANCE_CHOICES = [(None, "As in settings")] + list(sc.ENHANCE_MODES)
CROP_CHOICES = [(None, "As in settings"), ("auto", "Automatic"), ("none", "No crop"), ("manual", "Drawn by me")]
PREVIEW_SIDE = 1100


class CleanEditor(tk.Toplevel):
    def __init__(self, tool, entry, page: int = 0) -> None:
        super().__init__(tool.app.root)
        self.tool, self.app, self.entry = tool, tool.app, entry
        self.spec = tool.spec()
        self.page = max(0, min(page, max(0, entry.pages - 1)))
        self.small: dict[tuple[str, int], object] = {}
        self._photo = None
        self._after = None
        self._loading = False
        self._geom = (0, 0, 1, 1)
        self._drag = None
        self.title("Adjust scan")
        self.geometry("1180x780")
        self.minsize(900, 560)
        self.transient(tool.app.root)
        self.configure(background=self.app.theme.p["bg"])
        attach_window_controls(self, title="Adjust scan")
        self._build()
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Left>", lambda e: self.goto(self.page - 1))
        self.bind("<Right>", lambda e: self.goto(self.page + 1))
        self.load_entry(entry, self.page)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        side = ttk.Frame(body, width=300)
        side.pack(side="right", fill="y", padx=(10, 0))
        side.pack_propagate(False)
        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")
        self.view = tk.StringVar(value="edit")
        for value, label in (("original", "Original"), ("edit", "Edit"), ("result", "Result")):
            ttk.Radiobutton(top, text=label, value=value, variable=self.view, command=self.render_soon).pack(side="left", padx=(0, 10))
        self.info_lbl = ttk.Label(top, style="Muted.TLabel")
        self.info_lbl.pack(side="right")
        self.canvas = tk.Canvas(main, highlightthickness=0, cursor="crosshair")
        self.app.theme.style_canvas(self.canvas)
        self.canvas.pack(fill="both", expand=True, pady=6)
        self.canvas.bind("<Configure>", lambda e: self.render_soon())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.note_lbl = ttk.Label(main, style="Muted.TLabel", wraplength=700, justify="left")
        self.note_lbl.pack(anchor="w")

        # --- navigation
        nav = ttk.LabelFrame(side, text="Page", padding=6)
        nav.pack(fill="x")
        row = ttk.Frame(nav)
        row.pack(fill="x")
        ttk.Button(row, text="◀", width=3, command=lambda: self.goto(self.page - 1)).pack(side="left")
        self.page_lbl = ttk.Label(row, anchor="center")
        self.page_lbl.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="▶", width=3, command=lambda: self.goto(self.page + 1)).pack(side="left")
        row = ttk.Frame(nav)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="◀ Previous file", style="Tiny.TButton", command=lambda: self.step_file(-1)).pack(side="left")
        ttk.Button(row, text="Next file ▶", style="Tiny.TButton", command=lambda: self.step_file(1)).pack(side="right")
        self.file_lbl = ttk.Label(nav, style="Muted.TLabel", wraplength=270)
        self.file_lbl.pack(anchor="w", pady=(4, 0))
        self.keep_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(nav, text="Keep this page in the result", variable=self.keep_var, command=self._keep_changed).pack(anchor="w", pady=(4, 0))

        # --- turn / straighten
        geo = ttk.LabelFrame(side, text="Turn and straighten", padding=6)
        geo.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(geo)
        row.pack(fill="x")
        ttk.Button(row, text="⟲ 90°", width=7, command=lambda: self._turn(-90)).pack(side="left")
        ttk.Button(row, text="⟳ 90°", width=7, command=lambda: self._turn(90)).pack(side="left", padx=4)
        ttk.Button(row, text="Auto", style="Tiny.TButton", command=lambda: self._set(rot=None)).pack(side="right")
        self.rot_lbl = ttk.Label(geo, style="Muted.TLabel")
        self.rot_lbl.pack(anchor="w")
        self.skew_var = tk.DoubleVar(value=0.0)
        row = ttk.Frame(geo)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Tilt").pack(side="left")
        self.skew_lbl = ttk.Label(row, width=7, anchor="e")
        self.skew_lbl.pack(side="right")
        self.skew_scale = ttk.Scale(geo, from_=-10.0, to=10.0, variable=self.skew_var, command=self._skew_moved)
        self.skew_scale.pack(fill="x")
        row = ttk.Frame(geo)
        row.pack(fill="x")
        ttk.Button(row, text="Auto-detect tilt", style="Tiny.TButton", command=self._detect_tilt).pack(side="left")
        ttk.Button(row, text="Auto", style="Tiny.TButton", command=lambda: self._set(skew=None)).pack(side="right")

        # --- crop
        crop = ttk.LabelFrame(side, text="Crop", padding=6)
        crop.pack(fill="x", pady=(8, 0))
        self.crop_var = tk.StringVar(value="default")
        for value, label in CROP_CHOICES:
            ttk.Radiobutton(crop, text=label, value=value or "default", variable=self.crop_var, command=self._crop_mode).pack(anchor="w")
        ttk.Label(crop, text="Drag on the picture (Edit view) to draw a crop.", style="Muted.TLabel", wraplength=270).pack(anchor="w")

        # --- colour
        col = ttk.LabelFrame(side, text="Look", padding=6)
        col.pack(fill="x", pady=(8, 0))
        self.enh_var = tk.StringVar()
        cb = ttk.Combobox(col, textvariable=self.enh_var, values=[l for _v, l in ENHANCE_CHOICES], state="readonly")
        cb.pack(fill="x")
        cb.bind("<<ComboboxSelected>>", self._enh_changed)
        self.bri_var, self.con_var = tk.IntVar(value=0), tk.IntVar(value=0)
        for text, var in (("Brightness", self.bri_var), ("Contrast", self.con_var)):
            row = ttk.Frame(col)
            row.pack(fill="x", pady=(4, 0))
            ttk.Label(row, text=text, width=10).pack(side="left")
            ttk.Scale(row, from_=-60, to=60, variable=var, command=lambda v, t=text: self._look_moved(t)).pack(side="left", fill="x", expand=True)

        # --- actions
        act = ttk.Frame(side)
        act.pack(fill="x", pady=(10, 0))
        ttk.Button(act, text="Apply to all pages of this file", command=self._apply_all).pack(fill="x")
        row = ttk.Frame(act)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Reset page", command=self._reset_page).pack(side="left")
        ttk.Button(row, text="Done", style="Accent.TButton", command=self.destroy).pack(side="right")
        Tooltip(self.skew_scale, "Degrees to turn the page so the text lines are level.")

    # ------------------------------------------------------------------ loading
    def load_entry(self, entry, page: int = 0) -> None:
        self.entry = entry
        self.page = max(0, min(page, max(0, entry.pages - 1)))
        self.file_lbl.configure(text=Path(entry.path).name)
        if entry.analyses is None:
            self.info_lbl.configure(text="Analysing…")
            self.tool.ensure_analysis(entry, lambda: self.winfo_exists() and self._after_analysis())
        else:
            self._after_analysis()

    def _after_analysis(self) -> None:
        self._ensure_small(self.page, self._refresh_controls)

    def _ensure_small(self, page: int, then) -> None:
        key = (self.entry.path, page)
        if key in self.small:
            then()
            return
        path = self.entry.path
        self.info_lbl.configure(text="Loading…")

        def work(job):
            return sc.load_page(path, page, dpi=self.spec.dpi, max_side=PREVIEW_SIDE)

        def done(img):
            if not self.winfo_exists():
                return
            if len(self.small) > 8:
                self.small.pop(next(iter(self.small)))
            self.small[key] = img
            then()

        self.app.jobs.submit(work, "Loading page", self.tool.id, on_done=done,
                             on_error=lambda m: self.winfo_exists() and error(self, m))

    def goto(self, page: int) -> None:
        if 0 <= page < self.entry.pages and page != self.page:
            self.page = page
            self._ensure_small(page, self._refresh_controls)

    def step_file(self, delta: int) -> None:
        entries = self.tool.entries()
        if not entries or self.entry not in entries:
            return
        i = entries.index(self.entry) + delta
        if 0 <= i < len(entries):
            self.load_entry(entries[i], 0)

    # ------------------------------------------------------------------ current values
    def _ana(self) -> sc.PageAnalysis | None:
        a = self.entry.analyses
        return a[self.page] if a and self.page < len(a) else None

    def _edit(self) -> PageEdit | None:
        return self.entry.edits.get(self.page)

    def _values(self) -> dict:
        return sc.effective(self.spec, self._ana(), self._edit())

    def _set(self, **changes) -> None:
        """Change fields of this page's PageEdit (None = back to automatic)."""
        edit = replace(self._edit() or PageEdit(), **changes)
        if edit.is_empty():
            self.entry.edits.pop(self.page, None)
        else:
            self.entry.edits[self.page] = edit
        self.tool.entry_changed(self.entry)
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        if not self.winfo_exists():
            return
        self._loading = True
        try:
            ana, edit, v = self._ana(), self._edit() or PageEdit(), self._values()
            self.page_lbl.configure(text=f"Page {self.page + 1} of {self.entry.pages}")
            self.rot_lbl.configure(text=f"Turned {v['rot']}°" + ("  (you)" if edit.rot is not None else "  (auto)"))
            self.skew_var.set(v["skew"])
            self.skew_lbl.configure(text=f"{v['skew']:+.1f}°")
            self.crop_var.set("default" if edit.crop is None else ("manual" if isinstance(edit.crop, tuple) else edit.crop))
            self.enh_var.set(next(l for val, l in ENHANCE_CHOICES if val == edit.enhance))
            self.bri_var.set(v["brightness"])
            self.con_var.set(v["contrast"])
            dropped_by_default = bool(ana and ana.blank and self.spec.drop_blank and self.entry.pages > 1)
            self.keep_var.set(edit.keep if edit.keep is not None else not dropped_by_default)
            issues = ana.issues() if ana else []
            self.info_lbl.configure(text="Found: " + ", ".join(issues) if issues else "Looks fine")
            native = bool(ana and ana.native)
            self.note_lbl.configure(text="This page already contains real text, so it is copied unchanged "
                                         "(only turning is applied)." if native else "")
        finally:
            self._loading = False
        self.render_soon()

    # ------------------------------------------------------------------ control handlers
    def _turn(self, delta: int) -> None:
        self._set(rot=(self._values()["rot"] + delta) % 360)

    def _skew_moved(self, value) -> None:
        if self._loading:
            return
        v = round(float(value), 1)
        self.skew_lbl.configure(text=f"{v:+.1f}°")
        edit = self._edit() or PageEdit()
        self.entry.edits[self.page] = replace(edit, skew=v)
        self.tool.entry_changed(self.entry)
        self.render_soon()

    def _detect_tilt(self) -> None:
        small = self.small.get((self.entry.path, self.page))
        if small is None:
            return
        v = self._values()
        straight, _box = sc.geometry(small, self.spec, None, PageEdit(rot=v["rot"], skew=0.0, crop="none"), with_crop=False)
        self._set(skew=detect_skew(straight))

    def _crop_mode(self) -> None:
        mode = self.crop_var.get()
        if mode == "default":
            self._set(crop=None)
        elif mode == "manual":
            current = self._edit().crop if self._edit() and isinstance(self._edit().crop, tuple) else None
            if current is None:
                small = self.small.get((self.entry.path, self.page))
                box = None
                if small is not None:
                    _img, box = sc.geometry(small, self.spec, self._ana(), self._edit(), with_crop=False)
                current = box or (0.05, 0.05, 0.95, 0.95)
            self._set(crop=current)
            self.view.set("edit")
        else:
            self._set(crop=mode)

    def _enh_changed(self, _e=None) -> None:
        label = self.enh_var.get()
        self._set(enhance=next(v for v, l in ENHANCE_CHOICES if l == label))

    def _look_moved(self, which: str) -> None:
        if self._loading:
            return
        edit = self._edit() or PageEdit()
        if which == "Brightness":
            edit = replace(edit, brightness=int(float(self.bri_var.get())))
        else:
            edit = replace(edit, contrast=int(float(self.con_var.get())))
        self.entry.edits[self.page] = edit
        self.tool.entry_changed(self.entry)
        self.render_soon()

    def _keep_changed(self) -> None:
        if self._loading:
            return
        ana = self._ana()
        default_keep = not bool(ana and ana.blank and self.spec.drop_blank and self.entry.pages > 1)
        want = bool(self.keep_var.get())
        self._set(keep=None if want == default_keep else want)

    def _apply_all(self) -> None:
        src = self._edit()
        if src is None:
            return
        for i in range(self.entry.pages):
            if i != self.page:
                self.entry.edits[i] = replace(src, keep=self.entry.edits.get(i, PageEdit()).keep)
        self.tool.entry_changed(self.entry)
        self.tool.app.notify(f"Applied to all {self.entry.pages} pages", "ok")

    def _reset_page(self) -> None:
        self.entry.edits.pop(self.page, None)
        self.tool.entry_changed(self.entry)
        self._refresh_controls()

    # ------------------------------------------------------------------ drawing
    def render_soon(self) -> None:
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(120, self._render)

    def _render(self) -> None:
        self._after = None
        if not self.winfo_exists():
            return
        small = self.small.get((self.entry.path, self.page))
        self.canvas.delete("all")
        if small is None:
            return
        ana, edit, v = self._ana(), self._edit(), self._values()
        mode, overlay = self.view.get(), None
        try:
            if mode == "original":
                img = small
            elif mode == "edit":
                geo, overlay = sc.geometry(small, self.spec, ana, edit, with_crop=False)
                img = sc.enhance_image(geo, v["enhance"], v["brightness"], v["contrast"])
            else:
                img = sc.process_page(small, self.spec, ana, edit)
        except Exception as exc:
            self.canvas.create_text(20, 20, anchor="nw", text=f"Could not draw the preview: {exc}", fill="#cc3333")
            return
        cw, ch = max(80, self.canvas.winfo_width()), max(80, self.canvas.winfo_height())
        shown = fit_within(img.convert("RGB"), cw - 16, ch - 16)
        self._photo = ImageTk.PhotoImage(shown)
        x0, y0 = (cw - shown.width) // 2, (ch - shown.height) // 2
        self.canvas.create_image(x0, y0, anchor="nw", image=self._photo)
        self._geom = (x0, y0, shown.width, shown.height)
        if overlay:
            l, t, r, b = overlay
            self.canvas.create_rectangle(x0 + l * shown.width, y0 + t * shown.height, x0 + r * shown.width,
                                         y0 + b * shown.height, outline="#22bb66", width=2, dash=(6, 3), tags="crop")

    # ------------------------------------------------------------------ drag to crop
    def _rel(self, x: int, y: int) -> tuple[float, float]:
        x0, y0, w, h = self._geom
        return min(1.0, max(0.0, (x - x0) / max(1, w))), min(1.0, max(0.0, (y - y0) / max(1, h)))

    def _press(self, e) -> None:
        self._drag = (e.x, e.y) if self.view.get() == "edit" else None

    def _motion(self, e) -> None:
        if not self._drag:
            return
        self.canvas.delete("drag")
        self.canvas.create_rectangle(self._drag[0], self._drag[1], e.x, e.y, outline="#ffaa00", width=2, tags="drag")

    def _release(self, e) -> None:
        if not self._drag:
            return
        (x1, y1), self._drag = self._drag, None
        a, b = self._rel(x1, y1), self._rel(e.x, e.y)
        l, r = sorted((a[0], b[0]))
        t, bt = sorted((a[1], b[1]))
        if r - l < 0.05 or bt - t < 0.05:                 # a click, not a drag
            self.canvas.delete("drag")
            return
        self._set(crop=(l, t, r, bt))
