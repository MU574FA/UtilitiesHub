"""Screenshot extractor: copy a screenshot (or drop an image) -> OCR -> pop-up with the fields -> session table.

Uses the same FieldSpecs / rule engine as CV Sorter (shared `FieldForm`, fields editor, clipboard formats).
"""
from __future__ import annotations

import hashlib
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk

from PIL import Image, ImageGrab, ImageOps, ImageTk

from ..core import ocr
from ..core.fields import ExtractContext, FieldEngine, FieldSpec, default_screenshot_fields, join_values, parse_specs
from ..core.schema import Opt
from ..core.sheets import write_csv, write_xlsx
from ..core.templates import render_clipboard
from ..ui.dnd import DropZone
from ..ui.widgets import DataTable, Tooltip, confirm, copy_to_clipboard, error, modal, style_text
from .base import Command, Tool
from .cv_sorter import FieldForm, assign_menu

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif")


class Capture:
    def __init__(self, image: Image.Image, source: str) -> None:
        self.image, self.source = image, source
        self.text = ""
        self.values: dict[str, list[str]] = {}
        self.ai_fields: list[str] = []                 # labels of fields the AI filled
        self.when = datetime.now()


class CapturePopup:
    """Review pop-up for one capture (or an existing row when image is None)."""

    def __init__(self, tool: "Screenshot", cap: Capture, on_ok, title: str = "New capture") -> None:
        self.tool, self.app, self.cap, self.on_ok = tool, tool.app, cap, on_ok
        self.win, body = modal(tool.app.root, title, 980, 640)
        self.win.minsize(760, 480)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        self.form = FieldForm(left, self.app, tool.specs(), cap.values)
        self.form.pack(fill="x")
        self.ai_note = ttk.Label(left, style="Muted.TLabel", wraplength=300, justify="left")
        self._show_ai_note()
        self.name_ok = ttk.Label(left, style="Muted.TLabel")
        right = ttk.Notebook(body)
        right.grid(row=0, column=1, sticky="nsew")
        self.img_label = ttk.Label(right, anchor="center")
        if cap.image is not None and self.app.settings.get_bool("preview.enabled", True):
            right.add(self.img_label, text="Image")
            self._show_image()
        tf = ttk.Frame(right)
        right.add(tf, text="Text (select → right-click to assign)")
        self.text = tk.Text(tf, wrap="word")
        self.text.pack(fill="both", expand=True)
        style_text(self.win, self.text)
        self.text.insert("1.0", cap.text)
        assign_menu(self.win, self.text, lambda: self.form)
        bar = ttk.Frame(body)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(bar, text="Add to session  (Enter)", style="Accent.TButton", command=self.ok).pack(side="left")
        ttk.Button(bar, text="Discard  (Esc)", command=self.win.destroy).pack(side="left", padx=6)
        if cap.image is not None:
            for txt, deg, tip_ in (("⟲", -90, "Rotate the image left and read it again"), ("⟳", 90, "Rotate right and read again")):
                b = ttk.Button(bar, text=txt, width=3, command=lambda d=deg: self.rotate(d))
                b.pack(side="left", padx=(6 if deg < 0 else 0, 0))
                Tooltip(b, tip_)
        ttk.Button(bar, text="Re-run extraction on text", command=self.rerun).pack(side="left", padx=10)
        mb = ttk.Menubutton(bar, text="Copy ▾")
        m = tk.Menu(mb, tearoff=False)
        for rec in tool.get("clipboard") or []:
            m.add_command(label=rec["name"], command=lambda r=rec: self.copy(r))
        mb["menu"] = m
        mb.pack(side="left")
        self.win.bind("<Return>", lambda e: None if isinstance(e.widget, tk.Text) else (self.ok(), "break")[1])
        self.win.bind("<Control-Return>", lambda e: self.ok())
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.win.lift()
        self.win.focus_force()

    def _show_image(self) -> None:
        im = self.cap.image.copy()
        im.thumbnail((700, 520))
        self._photo = ImageTk.PhotoImage(im)
        self.img_label.configure(image=self._photo)

    def rotate(self, deg: int) -> None:
        self.cap.image = self.cap.image.rotate(-deg, expand=True)
        self._show_image()
        self.tool.reread(self.cap, self._reloaded)

    def _reloaded(self) -> None:
        if not self.win.winfo_exists():
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", self.cap.text)
        for s in self.form.specs:
            self.form.vars[s.key].set(join_values(self.cap.values.get(s.key, []), s))
            self.form.boxes[s.key].configure(values=self.cap.values.get(s.key, []))
        self._show_ai_note()

    def _show_ai_note(self) -> None:
        self.ai_note.configure(text=("✨ Filled by the AI - please check: " + ", ".join(self.cap.ai_fields))
                               if self.cap.ai_fields else "")
        if self.cap.ai_fields:
            self.ai_note.pack(anchor="w", pady=(6, 0))
        else:
            self.ai_note.pack_forget()

    def rerun(self) -> None:
        self.cap.text = self.text.get("1.0", "end").strip()
        # off the UI thread: with AI switched on this can take a few seconds
        self.app.jobs.submit(lambda job: self.tool.extract_into(self.cap), "Extracting fields", tool=self.tool.id,
                             on_done=lambda _r: self._reloaded(),
                             on_error=lambda m: self.tool.log(f"Extraction failed: {m}", "error"),
                             serial_key=self.tool.id + ".extract")

    def copy(self, rec: dict) -> None:
        copy_to_clipboard(self.win, render_clipboard(rec["template"], self.form.fields(), self.cap.source))
        self.app.notify(f"Copied: {rec['name']}")

    def ok(self) -> None:
        fields = self.form.fields()
        miss = [s.label for s in self.tool.specs() if s.level == "required" and s.searched and not fields.get(s.key)]
        if miss and not confirm(self.win, "Required fields are empty: " + ", ".join(miss) + "\nAdd anyway?"):
            return
        self.win.destroy()
        self.on_ok(self.cap, fields)


class Screenshot(Tool):
    id = "screenshot"
    title = "Screenshot Extractor"
    icon = "📸"
    order = 40
    description = "Copy a screenshot (or drop an image): the text is read and the fields are collected into a table."

    @classmethod
    def options(cls) -> list[Opt]:
        p = cls.id + "."
        return [
            Opt(p + "watch_clipboard", False, "Watch the clipboard for screenshots", "bool", section="Capture",
                help="Copy any screenshot (Win+Shift+S, PrtScn...) and it is read automatically"),
            Opt(p + "poll_ms", 700, "Clipboard check interval (ms)", "int", min=200, max=5000, section="Capture", advanced=True),
            Opt(p + "auto_mode", "off", "Add to the table without asking", "choice", section="Capture",
                choices=[("off", "Never - show the pop-up"), ("clean", "When all required fields are found"),
                         ("always", "Always")]),
            Opt(p + "copy_format", "", "Copy to the clipboard after adding (format name)", "str", section="Capture",
                help="Name of one of your clipboard formats; empty = off"),
            Opt(p + "autosave_path", "", "Keep a CSV copy of the session at", "file", section="Session",
                help="Rewritten after every change (empty = off)"),
            Opt(p + "fields", default_screenshot_fields(), "Fields to extract", "record", editor="fields", section="Fields",
                help="Enable the fields you need; add keyword / regex / label fields; set how necessary each is."),
            Opt(p + "clipboard", [{"name": "Phone + email", "template": r"{phone}\t{email}"},
                                  {"name": "Name + phone", "template": r"{name}\t{phone}"}],
                "Clipboard formats", "record", editor="clipboard", section="Copy formats"),
            Opt(p + "links", [], "Link templates", "record", editor="links", section="Copy formats"),
            *cls.append_opts(auto=True),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.rows: list[dict] = []                 # session: {"_time":..., key: value}
        self._last_hash = ""
        self._poll_after = None
        self._watching = False
        self.popup: CapturePopup | None = None
        self.table = None
        self.holder = None
        app.settings.subscribe(self.id + ".fields", lambda k, v: self._rebuild_table())
        app.settings.subscribe(self.id + ".watch_clipboard", lambda k, v: self._watch_setting(v))

    # ------------------------------------------------------------------ extraction
    def specs(self) -> list[FieldSpec]:
        return parse_specs(self.get("fields") or [])

    def extract_into(self, cap: Capture) -> None:
        engine = FieldEngine(self.specs(), ExtractContext.from_settings(self.settings))
        if not cap.text.strip():
            cap.values, cap.ai_fields = {}, []
            return
        result = engine.extract_detailed(cap.text, None)
        cap.values = result.values
        label = {sp.key: sp.label for sp in engine.specs}
        cap.ai_fields = [label.get(k, k) for k in result.ai_keys()]
        for err in result.errors:
            self.log(f"AI: {err} (the Rules result was used where possible)", "warn")

    def _ocr_job(self, cap: Capture, done) -> None:
        cfg = ocr.OcrConfig.from_settings(self.settings)

        def work(job):
            img = cap.image
            cap.text = ocr.safe_ocr(img, cfg) if cfg.enabled else ""
            self.extract_into(cap)
            return cap

        self.app.jobs.submit(work, "Reading screenshot", self.id, lambda _c: done(),
                             lambda m: (self.log(f"OCR failed: {m}", "error"), done()), serial_key=self.id + ".ocr")

    def reread(self, cap: Capture, done) -> None:
        self._ocr_job(cap, done)

    def ingest(self, image: Image.Image, source: str = "clipboard") -> None:
        try:
            image = ImageOps.exif_transpose(image).convert("RGB")
        except Exception:
            image = image.convert("RGB")
        cap = Capture(image, source)
        self._ocr_job(cap, lambda: self._captured(cap))

    def _captured(self, cap: Capture) -> None:
        self.app.last_text = cap.text
        if not cap.text.strip():
            self.log(f"{cap.source}: no text found in the image", "warn")
        specs = self.specs()
        missing = [s for s in specs if s.enabled and s.searched and s.level == "required" and not cap.values.get(s.key)]
        mode = self.get("auto_mode")
        if mode == "always" and cap.text.strip() or mode == "clean" and cap.text.strip() and not missing:
            self.add_row(cap, {k: join_values(v[:1] if s.kind not in ("keywords",) else v, s)
                               for s in specs for k, v in [(s.key, cap.values.get(s.key, []))]})
            return
        self.popup = CapturePopup(self, cap, lambda c, f: self.add_row(c, {k: ", ".join(v) for k, v in f.items()}))

    # ------------------------------------------------------------------ session
    def add_row(self, cap: Capture, values: dict[str, str]) -> None:
        row = {"_time": cap.when.strftime("%H:%M:%S"), "_source": cap.source, **values}
        self.rows.append(row)
        if self.built and self.table is not None:
            row["_iid"] = self.table.add(row)
        self.log(f"Added: {self._summary(row)}", "ok")
        fmt = self.get("copy_format")
        rec = next((r for r in self.get("clipboard") or [] if r["name"] == fmt), None) if fmt else None
        if rec:
            fields = {k: [v] for k, v in values.items() if v}
            copy_to_clipboard(self.app.root, render_clipboard(rec["template"], fields, cap.source))
        self._autosave()
        self.append_rows([{"source": cap.source, **{h: row.get(k, "") for k, h in self._header()[1:]}}], quiet=True)
        self._count()

    def _summary(self, row: dict) -> str:
        return " | ".join(str(row.get(s.key, "")) for s in self.specs() if s.enabled and row.get(s.key))[:100] or "(empty)"

    def _header(self) -> list[tuple[str, str]]:
        return [("_time", "Time")] + [(s.key, s.label) for s in self.specs() if s.enabled]

    def _matrix(self) -> list[list[str]]:
        head = self._header()
        return [[h for _, h in head]] + [[r.get(k, "") for k, _ in head] for r in self.rows]

    def _autosave(self) -> None:
        path = self.get("autosave_path")
        if path:
            try:
                write_csv(path, self._matrix())
            except OSError as exc:
                self.log(f"Could not write {path}: {exc}", "error")

    def edit_row(self, iid: str) -> None:
        row = next((r for r in self.rows if r.get("_iid") == iid), None)
        if row is None:
            return
        cap = Capture(None, row.get("_source", ""))
        cap.values = {k: [v] for k, v in row.items() if not k.startswith("_") and v}

        def ok(c, fields):
            row.update({k: ", ".join(v) for k, v in fields.items()})
            self._rebuild_table()
            self._autosave()

        CapturePopup(self, cap, ok, "Edit row")

    def delete_selected(self) -> None:
        sel = self.table.selection()
        if not sel:
            return
        self.rows = [r for r in self.rows if r.get("_iid") not in sel]
        self._rebuild_table()
        self._autosave()

    def clear_session(self) -> None:
        if self.rows and confirm(self.app.root, "Clear the whole session table?"):
            self.rows.clear()
            self._rebuild_table()
            self._autosave()

    def export(self) -> None:
        if not self.rows:
            self.app.notify("The table is empty", "warn")
            return
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", initialfile=f"screenshots-{datetime.now():%Y%m%d-%H%M}.xlsx",
                                            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if not path:
            return
        try:
            (write_csv(path, self._matrix()) if path.lower().endswith(".csv") else write_xlsx(path, {"Session": self._matrix()}))
        except Exception as exc:
            error(self.app.root, f"Could not save: {exc}")
            return
        self.app.notify(f"Saved {len(self.rows)} rows")

    def copy_table(self) -> None:
        copy_to_clipboard(self.app.root, "\n".join("\t".join(r) for r in self._matrix()))
        self.app.notify("Table copied (paste into Excel)")

    # ------------------------------------------------------------------ input
    def accept_files(self, paths: list[str]) -> None:
        from pathlib import Path
        for p in paths:
            f = Path(p)
            files = [x for x in sorted(f.iterdir()) if x.suffix.lower() in IMAGE_EXTS] if f.is_dir() else [f]
            for x in files:
                if x.suffix.lower() not in IMAGE_EXTS:
                    continue
                try:
                    with Image.open(x) as im:
                        im.load()
                        self.ingest(im.copy(), x.name)
                except Exception as exc:
                    self.log(f"{x.name}: {exc}", "error")

    def paste_now(self) -> None:
        img = self._grab()
        if img is None:
            self.app.notify("No image on the clipboard", "warn")
            return
        self._last_hash = self._hash(img)
        self.ingest(img)

    def choose_files(self) -> None:
        files = filedialog.askopenfilenames(filetypes=[("Images", " ".join("*" + e for e in IMAGE_EXTS))])
        if files:
            self.accept_files(list(files))

    @staticmethod
    def _grab():
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            return None
        if isinstance(data, Image.Image):
            return data
        if isinstance(data, list) and data and str(data[0]).lower().endswith(IMAGE_EXTS):
            try:
                return Image.open(data[0])
            except Exception:
                return None
        return None

    @staticmethod
    def _hash(img: Image.Image) -> str:
        small = img.convert("RGB").resize((64, 64))
        return hashlib.md5(small.tobytes() + str(img.size).encode()).hexdigest()

    # ------------------------------------------------------------------ clipboard watcher (automation)
    def can_automate(self) -> bool:
        return bool(self.get("watch_clipboard"))

    def auto_running(self) -> bool:
        return self._watching

    def start_auto(self) -> None:
        if self._watching:
            return
        img = self._grab()
        self._last_hash = self._hash(img) if img is not None else ""        # ignore what is already there
        self._watching = True
        self._tick()
        self.log("Watching the clipboard for screenshots", "info")
        self._sync_var()

    def stop_auto(self) -> None:
        self._watching = False
        if self._poll_after:
            try:
                self.app.root.after_cancel(self._poll_after)
            except tk.TclError:
                pass
            self._poll_after = None
        self._sync_var()

    def _tick(self) -> None:
        if not self._watching:
            return
        try:
            busy = self.popup is not None and self.popup.win.winfo_exists()
            img = None if busy else self._grab()
            if img is not None:
                h = self._hash(img)
                if h != self._last_hash:
                    self._last_hash = h
                    self.ingest(img)
        finally:
            self._poll_after = self.app.root.after(self.settings.get_int(self.id + ".poll_ms", 700), self._tick)

    def _watch_setting(self, value) -> None:
        if value and not self._watching:
            self.start_auto()
        elif not value and self._watching:
            self.stop_auto()

    def toggle_watch(self) -> None:
        self.put("watch_clipboard", not self._watching)

    def _sync_var(self) -> None:
        if self.built:
            self.watch_var.set(self._watching)

    def auto_summary(self) -> str:
        return f"{len(self.rows)} rows in the session"

    def stats(self) -> dict:
        return {"Rows": len(self.rows)}

    def on_close(self) -> None:
        self.stop_auto()

    def commands(self) -> list[Command]:
        return [Command("shot.paste", "Screenshot: read the clipboard image now", self.paste_now, self.title, "ocr"),
                Command("shot.watch", "Screenshot: start / stop clipboard watching", self.toggle_watch, self.title),
                Command("shot.export", "Screenshot: export the session…", self.export, self.title),
                Command("shot.clear", "Screenshot: clear the session", self.clear_session, self.title)]

    # ------------------------------------------------------------------ UI
    def build(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        self.watch_var = tk.BooleanVar(value=self._watching)
        ttk.Checkbutton(bar, text="Watch clipboard", variable=self.watch_var,
                        command=lambda: self.put("watch_clipboard", self.watch_var.get())).pack(side="left")
        for text, cmd, accent in (("Read clipboard now", self.paste_now, True), ("Open image…", self.choose_files, False),
                                  ("Export…", self.export, False), ("Copy table", self.copy_table, False),
                                  ("Delete selected", self.delete_selected, False), ("Clear", self.clear_session, False)):
            ttk.Button(bar, text=text, command=cmd, style="Accent.TButton" if accent else "TButton").pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="⚙ Settings", command=lambda: self.app.open_settings(self.id)).pack(side="right")
        self.count_lbl = ttk.Label(bar, style="Muted.TLabel")
        self.count_lbl.pack(side="right", padx=10)
        DropZone(parent, "Drop screenshots here (or copy one with Win+Shift+S)", self.accept_files,
                 theme=self.app.theme, height=58).pack(fill="x", pady=8)
        self.holder = ttk.Frame(parent)
        self.holder.pack(fill="both", expand=True)
        self._rebuild_table()
        self.app.root.bind("<Control-v>", self._ctrl_v, add="+")

    def _ctrl_v(self, e) -> None:
        if self.app.current is self and not isinstance(e.widget, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox)):
            self.paste_now()

    def _rebuild_table(self) -> None:
        if getattr(self, "holder", None) is None or not self.holder.winfo_exists():
            return
        for w in self.holder.winfo_children():
            w.destroy()
        cols = [("_time", "Time", 70, "w")] + [(s.key, s.label, 140, "w") for s in self.specs() if s.enabled]
        self.table = DataTable(self.holder, cols, height=12, searchable=True, on_double=self.edit_row)
        self.table.pack(fill="both", expand=True)
        for r in self.rows:
            r["_iid"] = self.table.add(r)
        self._count()

    def _count(self) -> None:
        if self.built:
            self.count_lbl.configure(text=f"{len(self.rows)} rows")
