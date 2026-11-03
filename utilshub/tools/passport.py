"""Passport & MRZ: read passport scans, parse and validate the machine-readable zone, check expiry, export."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from ..core import ocr
from ..core.mrz import parse_mrz, passport_summary
from ..core.readers import ReadOptions, read_document, supported_extensions
from ..core.schema import Opt
from ..ui.dnd import DropZone
from ..ui.widgets import DataTable, error, info, style_text
from .base import Command, Tool

HEADERS = ["File", "Surname", "Given names", "Passport no", "Nationality", "Date of birth", "Sex", "Expiry",
           "Days left", "Expiry status", "MRZ", "Issues"]
KEYS = ["file", "surname", "given", "number", "country", "dob", "sex", "expiry", "days_left", "status", "mrz", "issues"]


class PassportTool(Tool):
    id = "passport"
    title = "Passport & MRZ"
    icon = "🛂"
    order = 46
    description = "Read passport scans: MRZ parsing and check digits, surname, expiry and validity, export."

    @classmethod
    def options(cls) -> list[Opt]:
        return [
            Opt("passport.min_months", 6, "Warn when the passport is valid for less than (months)", "int", min=0, max=60,
                section="Checks"),
            *cls.append_opts(),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.rows: dict[str, dict] = {}
        self.texts: dict[str, str] = {}

    # ------------------------------------------------------------------ UI
    def build(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        ttk.Button(bar, text="Add passports…  (Ctrl+O)", command=self.pick_files).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left", padx=6)
        ttk.Button(bar, text="Add to file…", command=self.add_to_file).pack(side="right")
        ttk.Button(bar, text="Copy table", command=lambda: self.table.copy_selected(all_rows=True)).pack(side="right", padx=6)
        self.summary = ttk.Label(parent, style="Muted.TLabel", text="Add passport scans or photos (or drop them anywhere).")
        self.summary.pack(anchor="w", pady=(4, 0))
        self.table = DataTable(parent, [("file", "File", 200), ("surname", "Surname", 120), ("given", "Given names", 140),
                                        ("number", "Passport no", 90), ("country", "Nat.", 45), ("dob", "Born", 85),
                                        ("sex", "Sex", 35), ("expiry", "Expiry", 85), ("days_left", "Days left", 70, "e"),
                                        ("status", "Expiry status", 95), ("mrz", "MRZ", 120), ("issues", "Issues", 260)],
                               height=9, stretch="issues", searchable=True, on_select=lambda ids: self._show_text())
        self.table.pack(fill="both", expand=True, pady=6)
        self.table.tag_colors(valid="ok", expired="err", soon="warn", bad="err")
        DropZone(parent, "Drop passport scans here", self.accept_files, theme=self.app.theme, height=44).pack(fill="x")

        lower = ttk.PanedWindow(parent, orient="horizontal")
        lower.pack(fill="both", expand=True, pady=(8, 0))
        left = ttk.LabelFrame(lower, text="Paste MRZ text to check it (2 lines of 44 characters)", padding=6)
        lower.add(left, weight=1)
        self.paste = tk.Text(left, height=5, wrap="none")
        self.paste.pack(fill="x")
        style_text(self.frame, self.paste)
        row = ttk.Frame(left)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Check MRZ  (F5)", style="Accent.TButton", command=self.check_pasted).pack(side="left")
        ttk.Button(row, text="Paste from clipboard", command=self.paste_clip).pack(side="left", padx=6)
        right = ttk.LabelFrame(lower, text="Details", padding=6)
        lower.add(right, weight=1)
        self.detail = tk.Text(right, height=8, wrap="word")
        self.detail.pack(fill="both", expand=True)
        style_text(self.frame, self.detail)
        for iid, rec in self.rows.items():
            self._add_row(iid, rec)

    # ------------------------------------------------------------------ reading
    def accept_files(self, paths: list[str]) -> None:
        self.add_paths(paths)

    def pick_files(self) -> None:
        exts = " ".join(f"*{e}" for e in supported_extensions())
        paths = filedialog.askopenfilenames(title="Add passport scans", filetypes=[("Images & PDFs", exts), ("All", "*.*")])
        if paths:
            self.add_paths(list(paths))

    def add_paths(self, paths: list[str]) -> None:
        supported = set(supported_extensions())
        files: list[str] = []
        for p in map(Path, paths):
            found = [f for f in sorted(p.rglob("*")) if f.is_file() and f.suffix.lower() in supported] if p.is_dir() else [p]
            files.extend(str(f) for f in found if f.is_file())
        files = [f for f in dict.fromkeys(files) if f not in self.rows]
        if not files:
            return
        cfg = ocr.OcrConfig.from_settings(self.settings)
        cfg.mrz = True                                    # this tool exists for the MRZ pass
        opts = ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=True, deskew=cfg.deskew)
        months = self.get("min_months")

        def work(job):
            out = []
            for n, f in enumerate(files):
                if job.cancelled:
                    break
                job.progress(n, len(files), Path(f).name)
                doc = read_document(f, cfg, opts)
                rec = passport_summary(doc.text, months)
                if doc.error:
                    rec["issues"].insert(0, doc.error)
                out.append((f, rec, doc.text))
            return out

        def done(results):
            for path, rec, text in results or []:
                rec["file"] = Path(path).name
                self.rows[path], self.texts[path] = rec, text
                if self.built:
                    self._add_row(path, rec)
            self._summarise()

        self.app.jobs.submit(work, f"Reading {len(files)} passport(s)", tool=self.id, on_done=done,
                             on_error=lambda m: error(self.frame, f"Reading failed: {m}"), serial_key="passport.read")

    def _tag(self, rec: dict) -> str:
        if rec["mrz"] in ("check digits failed", "line 2 unreadable") or rec["status"] == "expired":
            return "expired" if rec["status"] == "expired" else "bad"
        return {"valid": "valid", "expires soon": "soon"}.get(rec["status"], "bad" if rec["mrz"] == "not found" else "")

    def _add_row(self, path: str, rec: dict) -> None:
        row = {k: ("; ".join(rec[k]) if k == "issues" else rec.get(k, "")) for k in KEYS}
        self.table.add(row, iid=path, tags=(self._tag(rec),))

    def _summarise(self) -> None:
        n = len(self.rows)
        bad = sum(1 for r in self.rows.values() if r["issues"])
        self.summary.configure(text=f"{n} passport(s) • {bad} with issues" if n else "")

    def clear(self) -> None:
        self.rows.clear()
        self.texts.clear()
        self.table.clear()
        self.detail.delete("1.0", "end")
        self._summarise()

    # ------------------------------------------------------------------ details
    def _show(self, text: str) -> None:
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)

    def _show_text(self) -> None:
        ids = self.table.selection()
        if ids and ids[0] in self.texts:
            self._show(self._describe(self.texts[ids[0]]))

    def _describe(self, text: str) -> str:
        mz = parse_mrz(text)
        if not mz:
            return "No MRZ found in the text.\n\n--- text read ---\n" + text.strip()
        lines = [f"Name: {mz['full']}", f"Surname: {mz['surname']}   Given: {mz['given']}", f"Nationality: {mz['country']}",
                 f"Passport no: {mz['number']}", f"Born: {mz['dob']}   Sex: {mz['sex'].replace('<', '')}",
                 f"Expiry: {mz['expiry']}", ""]
        lines += [f"{'✔' if ok else '✘'} check digit of {name}" for name, ok in mz["checks"]] or ["(second MRZ line not readable)"]
        return "\n".join(lines)

    def paste_clip(self) -> None:
        try:
            self.paste.delete("1.0", "end")
            self.paste.insert("1.0", self.frame.clipboard_get())
        except tk.TclError:
            info(self.frame, "The clipboard has no text.")
            return
        self.check_pasted()

    def check_pasted(self) -> None:
        text = self.paste.get("1.0", "end-1c")
        if not text.strip():
            info(self.frame, "Paste the MRZ lines first.")
            return
        rec = passport_summary(text, self.get("min_months"))
        self._show(self._describe(text) + "\n\n" + (("Issues: " + "; ".join(rec["issues"])) if rec["issues"] else "No issues."))

    # ------------------------------------------------------------------ output
    def add_to_file(self) -> None:
        if not self.rows:
            info(self.frame, "Add some passports first.")
            return
        recs = [{h: ("; ".join(r[k]) if k == "issues" else r.get(k, "")) for h, k in zip(HEADERS, KEYS)} for r in self.rows.values()]
        self.append_rows(recs, ask=True)

    def keys(self) -> dict:
        return {"<F5>": self.check_pasted, "<Control-o>": self.pick_files}

    def commands(self) -> list[Command]:
        return [Command("passport.add", "Passport & MRZ: add passports…", self.pick_files, self.title, "mrz scan"),
                Command("passport.check", "Passport & MRZ: check pasted MRZ", self.check_pasted, self.title, "validate")]

    def stats(self) -> dict:
        return {"Passports read": len(self.rows)}
