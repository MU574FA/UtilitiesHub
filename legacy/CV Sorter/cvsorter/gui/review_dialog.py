"""The verification pop-up.

Left : editable fields, live "is this name already in my list?" check, generated links,
       the resulting file name, clipboard controls.
Right: the ORIGINAL file (rendered pages) and the extracted text, side by side with the form.
Buttons: Save & Move  |  Send to Duplicates  |  Skip (leave the file where it is).
"""
from __future__ import annotations

import base64
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from ..clipboard import render_clipboard
from ..config import Settings
from ..extractor import field_definitions
from ..fileops import predicted_name, render_filename
from ..links import build_links, link_definitions
from ..preview import PreviewUnavailable, render_preview
from ..processor import AnalysisResult, Decision
from ..registry import NameRegistry
from .common import center, copy_to_clipboard, open_path

CloseCallback = Callable[[AnalysisResult, Decision], None]
GREEN, ORANGE, RED = "#0a7a3d", "#b36b00", "#b00020"


class ReviewDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, result: AnalysisResult, settings: Settings,
                 registry: NameRegistry, on_close: CloseCallback) -> None:
        super().__init__(parent)
        self.result, self.settings, self.registry, self.on_close = result, settings, registry, on_close
        self._closed = False
        self._after_ids: dict[str, str] = {}
        self._images: list[tk.PhotoImage] = []        # keep references or Tk drops the pictures
        self._preview_state: dict = {}
        self._best_match: str | None = None

        self.defs = field_definitions(settings)
        self.link_defs = link_definitions(settings)
        self.vars = {d.key: tk.StringVar(value="; ".join(result.fields.get(d.key, []))) for d in self.defs}
        self.link_vars = {d.key: tk.StringVar() for d in self.link_defs}
        self.preview = tk.StringVar()                 # resulting file name
        self.dest_hint = tk.StringVar()
        self.name_status = tk.StringVar()
        self.save_text = tk.StringVar()
        self.keep_in_place = tk.BooleanVar(value=False)

        self.clip_names = [f.name for f in settings.clipboard_formats]
        default = settings.clipboard_default if settings.clipboard_default in self.clip_names else (
            self.clip_names[0] if self.clip_names else "")
        self.clip_name = tk.StringVar(value=default)
        self.clip_on_save = tk.BooleanVar(value=settings.clipboard_auto)
        self.clip_status = tk.StringVar()

        self.title(f"Verify CV - {result.path.name}")
        self.attributes("-topmost", settings.popup_topmost)
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish("skip"))
        self.bind("<Escape>", lambda _e: self._finish("skip"))
        self.bind("<Return>", lambda _e: self._finish("save"))

        self._build()
        for var in self.vars.values():
            var.trace_add("write", self._on_change)
        if "name" in self.vars:
            self.vars["name"].trace_add("write", self._schedule_name_check)
        self._on_change()
        self._check_name()
        center(self, 1300 if settings.show_preview else 780, 780)
        self.lift()
        self.focus_force()
        if settings.show_preview:
            self._start_preview()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        pad = {"padx": 12, "pady": 4}
        header = ttk.Frame(self)
        header.pack(fill="x", **pad)
        ttk.Label(header, text=self.result.path.name, font=("TkDefaultFont", 11, "bold")).pack(side="left")
        if self.result.used_ocr:
            ttk.Label(header, text="  OCR used", style="Hint.TLabel").pack(side="left")
        if self.result.error:
            ttk.Label(self, text=f"⚠ Could not read this file: {self.result.error}\n"
                                 "You can still type the details manually.",
                      foreground=RED, wraplength=720).pack(fill="x", **pad)
        elif not self.result.text.strip():
            ttk.Label(self, text="⚠ No text was found in this file.", foreground=RED).pack(fill="x", **pad)

        buttons = ttk.Frame(self)                      # packed first so it is never pushed off-screen
        buttons.pack(side="bottom", fill="x", padx=12, pady=(4, 12))
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=6)
        left, right = ttk.Frame(paned), ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=2)

        self._build_form(left)
        self._build_right(right)
        self._build_buttons(buttons)

    def _build_form(self, parent: ttk.Frame) -> None:
        pad = {"padx": 6, "pady": 4}
        form = ttk.LabelFrame(parent, text="Extracted details (separate several values with ';')")
        form.pack(fill="x", **pad)
        form.columnconfigure(1, weight=1)
        row, first_entry = 0, None
        for d in self.defs:
            ttk.Label(form, text=d.label).grid(row=row, column=0, sticky="w", padx=8, pady=3)
            entry = ttk.Entry(form, textvariable=self.vars[d.key])
            entry.grid(row=row, column=1, sticky="ew", padx=8, pady=3)
            first_entry = first_entry or entry
            row += 1
            if d.key == "name" and self.registry.enabled:
                self.name_label = ttk.Label(form, textvariable=self.name_status, wraplength=380, justify="left")
                self.name_label.grid(row=row, column=1, sticky="w", padx=8)
                self.use_btn = ttk.Button(form, text="Use it", width=8, command=self._use_match)
                self.use_btn.grid(row=row, column=2, padx=(0, 8))
                self.use_btn.grid_remove()
                row += 1
        if first_entry:
            first_entry.focus_set()
            first_entry.selection_range(0, "end")

        if self.link_defs:
            links = ttk.LabelFrame(parent, text="Generated links (follow the final file name)")
            links.pack(fill="x", **pad)
            links.columnconfigure(1, weight=1)
            for i, d in enumerate(self.link_defs):
                ttk.Label(links, text=d.label).grid(row=i, column=0, sticky="w", padx=8, pady=3)
                ttk.Entry(links, textvariable=self.link_vars[d.key], state="readonly").grid(
                    row=i, column=1, sticky="ew", padx=4, pady=3)
                ttk.Button(links, text="Copy", width=6,
                           command=lambda k=d.key: self._copy_text(self.link_vars[k].get())).grid(
                    row=i, column=2, padx=(0, 8))

        out = ttk.LabelFrame(parent, text="Result")
        out.pack(fill="x", **pad)
        ttk.Label(out, text="File name:").pack(anchor="w", padx=8)
        ttk.Label(out, textvariable=self.preview, font=("TkDefaultFont", 10, "bold"),
                  foreground=GREEN, wraplength=460).pack(anchor="w", padx=8)
        ttk.Label(out, textvariable=self.dest_hint, style="Hint.TLabel", wraplength=460).pack(anchor="w", padx=8)
        ttk.Checkbutton(out, text="Don't move the file (only record the data / copy to clipboard)",
                        variable=self.keep_in_place, command=self._on_change).pack(anchor="w", padx=8, pady=4)

        if self.clip_names:
            clip = ttk.LabelFrame(parent, text="Clipboard")
            clip.pack(fill="x", **pad)
            line1, line2 = ttk.Frame(clip), ttk.Frame(clip)
            line1.pack(fill="x", pady=(4, 0))
            line2.pack(fill="x", pady=(0, 4))
            ttk.Label(line1, text="Format").pack(side="left", padx=(8, 4))
            ttk.Combobox(line1, textvariable=self.clip_name, values=self.clip_names,
                         state="readonly", width=22).pack(side="left")
            ttk.Button(line1, text="Copy now", command=self._copy_now).pack(side="left", padx=6)
            ttk.Label(line1, textvariable=self.clip_status, foreground=GREEN).pack(side="left")
            ttk.Checkbutton(line2, text="Also copy when saving", variable=self.clip_on_save).pack(
                side="left", padx=8)

    def _build_right(self, parent: ttk.Frame) -> None:
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=4)
        if self.settings.show_preview:
            orig = ttk.Frame(self.notebook)
            self.notebook.add(orig, text="Original file")
            self.canvas = tk.Canvas(orig, background="#e8e8e8", highlightthickness=0)
            ys = ttk.Scrollbar(orig, orient="vertical", command=self.canvas.yview)
            xs = ttk.Scrollbar(orig, orient="horizontal", command=self.canvas.xview)
            self.canvas.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
            ys.pack(side="right", fill="y")
            xs.pack(side="bottom", fill="x")
            self.canvas.pack(fill="both", expand=True)
            self.canvas.create_text(20, 20, anchor="nw", text="Rendering preview…", tags="msg")
            self.canvas.bind("<Enter>", lambda _e: self.canvas.focus_set())
            self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
            self.canvas.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-1, "units"))
            self.canvas.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(1, "units"))
        text_tab = ttk.Frame(self.notebook)
        self.notebook.add(text_tab, text="Extracted text")
        self.text_tab = text_tab
        text = tk.Text(text_tab, wrap="word")
        scroll = ttk.Scrollbar(text_tab, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", self.result.text.strip() or "(nothing extracted)")
        text.configure(state="disabled")

    def _build_buttons(self, parent: ttk.Frame) -> None:
        ttk.Button(parent, text="Open original", command=lambda: open_path(self.result.path)).pack(side="left")
        ttk.Button(parent, textvariable=self.save_text, style="Accent.TButton",
                   command=lambda: self._finish("save")).pack(side="right")
        ttk.Button(parent, text="Send to Duplicates", command=lambda: self._finish("duplicate")).pack(
            side="right", padx=8)
        ttk.Button(parent, text="Skip  (Esc)", command=lambda: self._finish("skip")).pack(side="right")

    # ------------------------------------------------------------------ live updates
    def _collect(self) -> dict[str, list[str]]:
        """Entry text -> field values (several values are separated by ';')."""
        return {key: [v.strip() for v in var.get().split(";") if v.strip()]
                for key, var in self.vars.items()}

    def _on_change(self, *_args) -> None:
        fields = self._collect()
        stem = render_filename(self.settings.filename_template, fields, self.result.path)
        suffix = self.result.path.suffix.lower()
        if self.keep_in_place.get():
            filename = f"{stem}{suffix}"
            self.dest_hint.set("The file stays where it is.")
            self.save_text.set("Save (no move)  (Enter)")
        else:
            filename = predicted_name(self.settings, stem, suffix)   # includes " (2)" numbering
            verb = "copied" if self.settings.file_action == "copy" else "moved"
            self.dest_hint.set(f"{verb} to {self.settings.output_folder}")
            self.save_text.set("Save & Copy  (Enter)" if self.settings.file_action == "copy"
                               else "Save & Move  (Enter)")
        self.preview.set(filename)
        links = build_links(self.settings, fields, self.result.path, filename)
        for key, var in self.link_vars.items():
            var.set(links.get(key, [""])[0])

    def _schedule_name_check(self, *_args) -> None:
        if "name" in self._after_ids:
            self.after_cancel(self._after_ids["name"])
        self._after_ids["name"] = self.after(250, self._check_name)   # debounce typing

    def _check_name(self) -> None:
        self._after_ids.pop("name", None)
        if not self.registry.enabled or "name" not in self.vars:
            return
        name = (self._collect()["name"] or [""])[0]
        check = self.registry.check(name)
        self._best_match = None
        self.use_btn.grid_remove()
        if check.status == "empty":
            self.name_status.set("")
            return
        if check.status == "new":
            self.name_status.set("✓ New name - not in your list (it will be added when you save).")
            color = GREEN
        else:
            listed = "; ".join(f"{m.name} ({round(m.score * 100)}%)" for m in check.matches[:3])
            if check.status == "exact":
                self.name_status.set(f"✖ Already in your list: {listed}\nLikely a duplicate - "
                                     "consider 'Send to Duplicates'.")
                color = RED
            else:
                self.name_status.set(f"⚠ Close to an existing name: {listed}")
                color = ORANGE
                self._best_match = check.best.name
                self.use_btn.configure(text="Use it")
                self.use_btn.grid()
        self.name_label.configure(foreground=color)

    def _use_match(self) -> None:
        if self._best_match:
            self.vars["name"].set(self._best_match)

    # ------------------------------------------------------------------ clipboard
    def _copy_text(self, text: str) -> None:
        copy_to_clipboard(self, text)
        self._flash("Copied ✓")

    def _flash(self, message: str) -> None:
        self.clip_status.set(message)
        if "flash" in self._after_ids:
            self.after_cancel(self._after_ids["flash"])
        self._after_ids["flash"] = self.after(2500, lambda: self.clip_status.set(""))

    def _copy_now(self) -> None:
        fmt = next((f for f in self.settings.clipboard_formats if f.name == self.clip_name.get()), None)
        if fmt is None:
            return
        fields = self._collect()
        filename = self.preview.get()
        links = build_links(self.settings, fields, self.result.path, filename)
        try:
            text = render_clipboard(fmt.template, {**fields, **links}, self.result.path, filename)
        except ValueError as exc:
            self._flash(f"⚠ {exc}")
            return
        self._copy_text(text)

    # ------------------------------------------------------------------ original-file preview
    def _start_preview(self) -> None:
        state = self._preview_state

        def work() -> None:                      # worker thread: never touch Tk here
            try:
                state["result"] = render_preview(self.result.path, self.settings)
            except PreviewUnavailable as exc:
                state["error"] = str(exc)
            except Exception as exc:
                state["error"] = f"Preview failed: {exc}"
            state["done"] = True

        threading.Thread(target=work, daemon=True).start()
        self._poll_preview()

    def _poll_preview(self) -> None:
        if self._closed:
            return
        if not self._preview_state.get("done"):
            self._after_ids["preview"] = self.after(200, self._poll_preview)
            return
        self.canvas.delete("msg")
        error = self._preview_state.get("error")
        if error:
            self.canvas.create_text(20, 20, anchor="nw", width=520, text=error)
            self.notebook.select(self.text_tab)
            return
        result, y, width = self._preview_state["result"], 8, 0
        for png in result.pages:
            image = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
            self._images.append(image)
            self.canvas.create_image(8, y, anchor="nw", image=image)
            y += image.height() + 8
            width = max(width, image.width() + 16)
        if result.total > len(result.pages):
            self.canvas.create_text(8, y, anchor="nw",
                                    text=f"… showing the first {len(result.pages)} of {result.total} pages "
                                         "(use 'Open original' for the rest)")
            y += 30
        self.canvas.configure(scrollregion=(0, 0, width, y))

    # ------------------------------------------------------------------ closing
    def _finish(self, action: str) -> None:
        if self._closed:
            return
        self._closed = True
        for after_id in self._after_ids.values():
            self.after_cancel(after_id)
        fields = self._collect()
        clip = self.clip_name.get() if (action == "save" and self.clip_on_save.get()) else None
        decision = Decision(action, fields, clip or None, move=not self.keep_in_place.get())
        self.destroy()
        self.on_close(self.result, decision)
