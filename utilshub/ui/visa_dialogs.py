"""Windows of the Visa Tracker: person form, contact log, page picker, multi-visa PDF review/import, spreadsheet import,
candidate info popup, message templates.

Every dialog is non-blocking: it gets a callback and calls it when the user confirms (so it is easy to test).
`tool` is the VisaTracker (it provides app, store, policy(), today(), fmt() ...).
"""
from __future__ import annotations

import copy
import datetime as dt
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable

from PIL import ImageTk

from ..core import visa as V
from ..core import visa_files, visa_io
from ..core.candidate_lookup import Hit, match_candidate, match_status
from ..core.render import PreviewUnavailable
from ..core.sheets import SheetError, load_book
from ..core.textutil import slugify
from .autocomplete import attach as autocomplete
from .thumbs import ThumbGrid, ThumbItem
from .widgets import DataTable, Tooltip, confirm, error, info, modal, style_text


# --------------------------------------------------------------------------- small helpers
def _grab(top: tk.Misc) -> None:
    try:
        top.grab_set()
    except tk.TclError:
        pass


def make_choice(parent: tk.Misc, pairs: list[tuple[str, str]], value: str, width: int = 24, state: str = "readonly"):
    """A combobox that shows labels and hands back the value.  Returns (widget, get, set)."""
    labels = [lbl for _, lbl in pairs]
    var = tk.StringVar(value=dict(pairs).get(value, value if state != "readonly" else (labels[0] if labels else "")))
    cb = ttk.Combobox(parent, textvariable=var, values=labels, state=state, width=width)

    def get() -> str:
        text = var.get()
        return next((v for v, lbl in pairs if lbl == text), text if state != "readonly" else "")

    def put(v: str) -> None:
        var.set(dict(pairs).get(v, v))

    return cb, get, put


class DateField(ttk.Frame):
    """A date box that understands what people type (15/11/2026, 15 Nov 2026, +30, today) and shows what it understood."""

    def __init__(self, parent: tk.Misc, tool, value: str | dt.date | None = None, width: int = 12,
                 on_change: Callable[[], None] | None = None, today_button: bool = True) -> None:
        super().__init__(parent)
        self.tool, self.on_change = tool, on_change
        d = V.to_date(value)
        self.var = tk.StringVar(value=tool.fmt(d) if d else "")
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(side="left")
        if today_button:
            b = ttk.Button(self, text="Today", style="Tiny.TButton", command=lambda: self.set(tool.today()))
            b.pack(side="left", padx=(4, 0))
        self.hint = ttk.Label(self, style="Muted.TLabel", width=22)
        self.hint.pack(side="left", padx=(6, 0))
        self.var.trace_add("write", lambda *_: self._update())
        self._update(notify=False)                       # the owner is not ready yet while we are being built

    def get_date(self) -> dt.date | None:
        return V.parse_date_input(self.var.get(), self.tool.today(), self.tool.date_order())

    def text(self) -> str:
        return self.var.get().strip()

    def valid(self) -> bool:
        return not self.text() or self.get_date() is not None

    def set(self, value: dt.date | str | None) -> None:
        d = V.to_date(value)
        self.var.set(self.tool.fmt(d) if d else "")

    def iso(self) -> str:
        return V.iso(self.get_date())

    def _update(self, notify: bool = True) -> None:
        if not self.text():
            self.hint.configure(text="", style="Muted.TLabel")
        else:
            d = self.get_date()
            if d is None:
                self.hint.configure(text="✗ not a date", style="Err.TLabel")
            else:
                self.hint.configure(text=f"{self.tool.fmt(d)} · {self.tool.relative(d)}", style="Muted.TLabel")
        if notify and self.on_change:
            self.on_change()


def _bind_close(top: tk.Misc, close: Callable[[], None]) -> None:
    top.bind("<Escape>", lambda e: close())


# --------------------------------------------------------------------------- candidate info pop-up (same as DocPacker)
def show_candidate_info_popup(parent: tk.Misc, app, name: str, path: str, tab: str = "", name_column: str = "",
                              header_row: int = 1, passport_no: str = "", dob: str = "",
                              settings_tool_id: str = "visa",
                              on_apply: Callable[[Hit], None] | None = None) -> tk.Misc | None:
    """Show the candidate's row from the configured spreadsheet in a pop-up (same style as DocPacker)."""
    path = (path or "").strip()
    if not path:
        if confirm(parent, "No candidates sheet is chosen yet.\n\nOpen the settings to choose one?"):
            app.open_settings(settings_tool_id)
        return None
    name = (name or "").strip()
    passport_no = (passport_no or "").strip()
    if not name and not passport_no:
        info(parent, "Select a candidate or enter a name first.")
        return None
    try:
        hits = match_candidate(path, name=name, passport_no=passport_no, dob=dob, sheet=tab,
                               name_column=name_column, header_row=header_row, threshold=0.55)
    except SheetError as exc:
        error(parent, str(exc))
        return None
    except Exception as exc:
        error(parent, f"Could not read the sheet: {exc}")
        return None
    if not hits:
        label = name or passport_no
        info(parent, f"No row for “{label}” was found in {Path(path).name}.")
        return None

    top, body = modal(parent, f"Candidate info - {name or passport_no}", 480, 0)
    top.transient(parent.winfo_toplevel())
    top.bind("<Escape>", lambda e: top.destroy())
    head = tk.StringVar()
    ttk.Label(body, textvariable=head, style="Muted.TLabel", wraplength=440, justify="left").pack(anchor="w")
    tree = ttk.Treeview(body, columns=("field", "value"), show="headings",
                        height=min(14, max(len(h.fields) for h in hits)), selectmode="browse")
    tree.heading("field", text="Field")
    tree.heading("value", text="Value")
    tree.column("field", width=155, stretch=False)
    tree.column("value", width=295)

    current = [0]

    def show(i: int) -> None:
        current[0] = i
        h = hits[i]
        how = {"passport": "passport match", "dob+name": "DOB + name", "partial_name": "partial name"}.get(h.matched_by, "name")
        head.set(f"{Path(path).name} - row {h.row} - match {h.score:.0%} ({how})")
        tree.delete(*tree.get_children())
        for k, v in h.fields:
            tree.insert("", "end", values=(k, v))

    if len(hits) > 1:
        labels = [f"{h.candidate_name or (h.fields[0][1] if h.fields else '?')}  ({h.score:.0%}, row {h.row})" for h in hits]
        pick = tk.StringVar(value=labels[0])
        cb = ttk.Combobox(body, textvariable=pick, values=labels, state="readonly")
        cb.pack(fill="x", pady=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: show(labels.index(pick.get())))
    tree.pack(fill="both", expand=True, pady=6)

    def copy_row() -> None:
        sel = tree.selection()
        rows = [tree.item(s, "values") for s in sel] or [tree.item(c, "values") for c in tree.get_children()]
        top.clipboard_clear()
        top.clipboard_append("\n".join(f"{k}: {v}" for k, v in rows))

    bar = ttk.Frame(body)
    bar.pack(fill="x")
    if on_apply is not None:
        def apply_now() -> None:
            on_apply(hits[current[0]])
            top.destroy()
        ttk.Button(bar, text="Fill empty fields", command=apply_now).pack(side="left")
    ttk.Button(bar, text="Close", style="Accent.TButton", command=top.destroy).pack(side="right")
    ttk.Button(bar, text="Copy", command=copy_row).pack(side="right", padx=6)
    show(0)
    return top


# --------------------------------------------------------------------------- person form (DocPacker-style grid)
class PersonDialog:
    """Add or edit one person.  on_save(person) -> True when stored (False keeps the window open)."""

    def __init__(self, tool, person: V.Person | None, on_save: Callable[[V.Person], bool],
                 defaults: dict | None = None) -> None:
        self.tool, self.app, self.on_save = tool, tool.app, on_save
        self.is_new = person is None
        self.person = copy.deepcopy(person) if person else V.Person(**(defaults or {}))
        self.win, frame = modal(tool.frame, "Add person" if self.is_new else f"Edit {person.name}", width=860, height=720)
        self.win.minsize(720, 540)
        _bind_close(self.win, self.close)
        self.win.bind("<Control-Return>", lambda e: self.save())

        bar = ttk.Frame(frame)                                  # buttons first, so they are never pushed out of sight
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        self.scan_btn = ttk.Button(bar, text="📷 Fill from passport scan…", command=self.fill_from_passport)
        self.scan_btn.pack(side="left")
        ttk.Button(bar, text="👤 Candidate info", command=self.show_candidate_info).pack(side="left", padx=6)
        ttk.Button(bar, text="Save  (Ctrl+Enter)", style="Accent.TButton", command=lambda: self.save()).pack(side="right")
        if self.is_new:
            ttk.Button(bar, text="Save & add another", command=lambda: self.save(again=True)).pack(side="right", padx=6)
        ttk.Button(bar, text="Cancel", command=self.close).pack(side="right", padx=6)

        from .widgets import ScrollFrame
        scroll = ScrollFrame(frame)
        scroll.pack(fill="both", expand=True)
        body = scroll.body
        self.vars: dict[str, tk.StringVar] = {}
        p, t = self.person, tool

        def make_group(title: str, cols: int = 3) -> ttk.LabelFrame:
            box = ttk.LabelFrame(body, text=title, padding=(8, 4, 8, 6))
            box.pack(fill="x", pady=(0, 8))
            for c in range(cols):
                box.columnconfigure(c, weight=1, uniform=f"grp_{title}")
            return box

        def place(box: ttk.LabelFrame, r: int, c: int, label: str, builder, colspan: int = 1) -> None:
            cell = ttk.Frame(box)
            cell.grid(row=r, column=c, columnspan=colspan, sticky="ew", padx=(0, 10), pady=(0, 4))
            ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
            builder(cell)

        def entry(parent, key, width=22, value=None):
            var = tk.StringVar(value=str(getattr(p, key) if value is None else value))
            self.vars[key] = var
            e = ttk.Entry(parent, textvariable=var, width=width)
            e.pack(fill="x")
            autocomplete(e, f"visa.{key}")
            return var

        # -- Candidate (DocPacker-style labelled grid) ---------------------------------------------
        cand_box = make_group("Candidate", cols=3)
        place(cand_box, 0, 0, "Full name *", lambda c: entry(c, "name", 40), colspan=2)

        def group_box(c):
            cb = ttk.Combobox(c, textvariable=self.vars.setdefault("group", tk.StringVar(value=p.group)),
                              values=t.store.groups(), width=20)
            cb.pack(fill="x")
            autocomplete(cb, "visa.group", extra=t.store.groups)
            Tooltip(cb, "A batch, project or agency - lets you filter and report by group.")
        place(cand_box, 0, 2, "Group / batch", group_box)
        place(cand_box, 1, 0, "Passport no.", lambda c: entry(c, "passport_no", 18))
        place(cand_box, 1, 1, "Passport expiry", lambda c: self._date(c, "passport_expiry"), colspan=2)
        place(cand_box, 2, 0, "Nationality", lambda c: entry(c, "nationality", 18))
        place(cand_box, 2, 1, "Phone / WhatsApp", lambda c: entry(c, "phone", 20))
        place(cand_box, 2, 2, "Position", lambda c: entry(c, "position", 20))
        place(cand_box, 3, 0, "E-mail", lambda c: entry(c, "email", 28), colspan=2)

        # -- Visa ----------------------------------------------------------------------------------
        visa_box = make_group("Visa", cols=3)
        self.vars["visa_type"] = tk.StringVar(value=p.visa_type)

        def type_box(c):
            cb = ttk.Combobox(c, textvariable=self.vars["visa_type"], values=[x for x in t.get("visa_types")], width=22)
            cb.pack(fill="x")
            autocomplete(cb, "visa.visa_type")

        self.days_var = tk.StringVar(value=str(p.visa_days or ""))

        def days_box(c):
            row_f = ttk.Frame(c)
            row_f.pack(fill="x")
            ttk.Spinbox(row_f, textvariable=self.days_var, from_=0, to=720, width=7).pack(side="left")
            ttk.Label(row_f, text=f"days (empty = {t.policy().default_days})", style="Muted.TLabel").pack(side="left", padx=6)

        place(visa_box, 0, 0, "Visa no.", lambda c: entry(c, "visa_no", 20))
        place(visa_box, 0, 1, "Visa type", type_box)
        place(visa_box, 0, 2, "Entry window", days_box)
        place(visa_box, 1, 0, "Issue date", lambda c: self._date(c, "visa_issue", recalc=True), colspan=2)
        place(visa_box, 2, 0, "Fixed last entry (optional)", lambda c: self._date(c, "visa_deadline", recalc=True), colspan=2)
        self.days_var.trace_add("write", lambda *_: self._recalc())

        dl_frame = ttk.Frame(visa_box)
        dl_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.deadline_lbl = ttk.Label(dl_frame, text="", style="H3.TLabel")
        self.deadline_lbl.pack(anchor="w")
        ttk.Label(dl_frame, text="Fill the issue date and the deadline is worked out for you. Only use 'Fixed last entry' "
                                 "when the visa shows an exact last day.", style="Muted.TLabel", wraplength=760,
                  justify="left").pack(anchor="w")

        # -- Ticket --------------------------------------------------------------------------------
        ticket_box_grp = make_group("Ticket", cols=3)
        ticket_cb_holder: dict = {}

        def ticket_box(c):
            cbx, get, put = make_choice(c, V.TICKET_STATES, p.ticket_status, width=22)
            cbx.pack(fill="x")
            ticket_cb_holder["get"] = get

        place(ticket_box_grp, 0, 0, "Ticket status", ticket_box)
        place(ticket_box_grp, 0, 1, "Flight no.", lambda c: entry(c, "flight_no", 20))
        place(ticket_box_grp, 1, 0, "Flight date", lambda c: self._date(c, "flight_date"), colspan=2)
        self._ticket_get = lambda: ticket_cb_holder["get"]()

        # -- Extra fields & Notes ------------------------------------------------------------------
        notes_box = make_group("Notes & Extra Fields", cols=3)
        self.extra_vars: dict[str, tk.StringVar] = {}
        custom = t.custom_fields()
        for n, label in enumerate(custom):
            var = tk.StringVar(value=str(p.extra.get(label, "")))
            self.extra_vars[label] = var
            r, c = divmod(n, 3)
            place(notes_box, r, c, label,
                  lambda cell, v=var, l=label: autocomplete(self._pack_fill(ttk.Entry(cell, textvariable=v)),
                                                            f"visa.custom.{slugify(l)}"))
        note_row = (len(custom) + 2) // 3 if custom else 0
        nf = ttk.Frame(notes_box)
        nf.grid(row=note_row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(nf, text="Notes", style="Muted.TLabel").pack(anchor="w")
        self.notes = tk.Text(nf, height=4, wrap="word")
        style_text(frame, self.notes)
        self.notes.insert("1.0", p.notes)
        self.notes.pack(fill="x", pady=(2, 0))
        autocomplete(self.notes, "visa.notes")
        scroll.bind_children()
        self._recalc()
        _grab(self.win)

    # ------------------------------------------------------------------ pieces
    @staticmethod
    def _pack_fill(widget):
        widget.pack(fill="x")
        return widget

    def _date(self, parent, key: str, recalc: bool = False) -> DateField:
        field_ = DateField(parent, self.tool, getattr(self.person, key), on_change=self._recalc if recalc else None)
        field_.pack(anchor="w")
        setattr(self, f"date_{key}", field_)
        return field_

    def _collect(self) -> tuple[V.Person, list[str]]:
        p, errors = copy.deepcopy(self.person), []
        for key, var in self.vars.items():
            setattr(p, key, var.get().strip())
        if not p.name:
            errors.append("Enter the name.")
        for key, label in (("passport_expiry", "Passport expiry"), ("visa_issue", "Issue date"),
                           ("visa_deadline", "Fixed last entry date"), ("flight_date", "Flight date")):
            f = getattr(self, f"date_{key}")
            if not f.valid():
                errors.append(f"{label}: '{f.text()}' is not a date. Try 15/11/2026, 15 Nov 2026 or +30.")
            setattr(p, key, f.iso())
        raw = self.days_var.get().strip()
        try:
            p.visa_days = int(raw) if raw else 0
        except ValueError:
            errors.append("Entry window: enter a number of days (or leave it empty).")
        p.ticket_status = self._ticket_get() or "none"
        p.notes = self.notes.get("1.0", "end").strip()
        p.extra = {k: v.get().strip() for k, v in self.extra_vars.items() if v.get().strip()} | \
                  {k: v for k, v in self.person.extra.items() if k not in self.extra_vars}
        return p, errors

    def _recalc(self) -> None:
        if not hasattr(self, "deadline_lbl") or not hasattr(self, "date_visa_deadline"):
            return
        p, _ = self._collect()
        pol, today = self.tool.policy(), self.tool.today()
        dl = V.deadline_of(p, pol)
        if dl is None:
            self.deadline_lbl.configure(text="Deadline: enter the issue date", style="Muted.TLabel")
            return
        n = (dl - today).days
        style = "Err.TLabel" if n < 0 or n <= pol.critical else "Warn.TLabel" if n <= pol.urgent else "Ok.TLabel"
        self.deadline_lbl.configure(text=f"Deadline: {self.tool.fmt(dl)}  ·  "
                                         f"{V.human_days(n) + ' left' if n >= 0 else 'passed ' + V.human_days(n)}", style=style)

    # ------------------------------------------------------------------ actions
    def show_candidate_info(self) -> None:
        path, tab, name_col, header_row = self.tool.candidate_sheet_config()

        def apply_hit(hit: Hit) -> None:
            fmap = hit.field_map()
            filled = []
            for key in ("name", "passport_no", "nationality", "phone", "email", "position", "group"):
                val = fmap.get(key, "")
                if val and key in self.vars and not self.vars[key].get().strip():
                    self.vars[key].set(val)
                    filled.append(key.replace("_", " "))
            if filled:
                self.app.notify("Filled from candidate sheet: " + ", ".join(filled), "ok")

        show_candidate_info_popup(
            self.win, self.app, self.vars["name"].get().strip(), path, tab, name_col, header_row,
            passport_no=self.vars["passport_no"].get().strip(), settings_tool_id=self.tool.id, on_apply=apply_hit,
        )

    def fill_from_passport(self) -> None:
        from ..core import ocr
        from ..core.mrz import passport_summary
        from ..core.readers import ReadOptions, read_document, supported_extensions
        exts = " ".join(f"*{e}" for e in supported_extensions())
        path = filedialog.askopenfilename(title="Passport scan or photo", filetypes=[("Images & PDFs", exts), ("All", "*.*")],
                                          parent=self.win)
        if not path:
            return
        cfg = ocr.OcrConfig.from_settings(self.app.settings)
        cfg.mrz = True
        months = self.tool.policy().min_passport_months

        def work(job):
            doc = read_document(path, cfg, ReadOptions(ocr=cfg.enabled, rotate=cfg.autorotate, mrz=True, deskew=cfg.deskew))
            return passport_summary(doc.text, months), doc.error

        def done(result):
            rec, err = result
            if not self.win.winfo_exists():
                return
            filled = []
            full = (rec.get("full") or "").strip().title()
            if full and not self.vars["name"].get().strip():
                self.vars["name"].set(full)
                filled.append("name")
            for key, val in (("passport_no", rec.get("number")), ("nationality", rec.get("country"))):
                if val and not self.vars[key].get().strip():
                    self.vars[key].set(val)
                    filled.append(key.replace("_", " "))
            if rec.get("expiry") and not self.date_passport_expiry.text():
                self.date_passport_expiry.set(rec["expiry"])
                filled.append("passport expiry")
            if filled:
                self.app.notify("Read from the passport: " + ", ".join(filled) + ". Please check them.", "ok")
            else:
                info(self.win, "Nothing could be read from that file." + (f"\n\n{err}" if err else
                                                                           "\nTry a sharper scan of the photo page."))

        self.app.jobs.submit(work, "Reading passport", tool=self.tool.id, on_done=done,
                             on_error=lambda m: error(self.win, f"Reading failed: {m}"), serial_key="visa.passport")

    def save(self, again: bool = False) -> None:
        p, errors = self._collect()
        if not errors:
            dl, issued = V.to_date(p.visa_deadline), V.to_date(p.visa_issue)
            if dl and issued and dl < issued:
                errors.append("The last entry date is before the issue date.")
        if errors:
            error(self.win, "\n".join(errors))
            return
        twin = self.tool.store.find_passport(p.passport_no)
        if twin is not None and twin.id != p.id and p.passport_no.strip() and \
                (self.is_new or p.passport_no.strip() != self.person.passport_no.strip()):
            if not confirm(self.win, f"{twin.name} already has this passport number ({p.passport_no}).\n\nSave anyway?"):
                return
        if not self.on_save(p):
            return
        if again:
            self.person = V.Person(group=p.group, visa_type=p.visa_type, position=p.position, visa_days=p.visa_days,
                                   nationality=p.nationality)
            for key, var in self.vars.items():
                var.set(getattr(self.person, key, ""))
            for key in ("passport_expiry", "visa_issue", "visa_deadline", "flight_date"):
                getattr(self, f"date_{key}").set(None)
            self.days_var.set(str(self.person.visa_days or ""))
            self.notes.delete("1.0", "end")
            for var in self.extra_vars.values():
                var.set("")
            self.is_new = True
            self._recalc()
            self.app.notify(f"Saved {p.name}. Next person:", "ok")
        else:
            self.close()

    def close(self) -> None:
        self.win.destroy()


# --------------------------------------------------------------------------- contact log
class FollowupDialog:
    """Write a contact into the log of one or several people."""

    def __init__(self, tool, ids: list[str], on_done: Callable[[], None] | None = None,
                 channel: str = "call", outcome: str = "reached") -> None:
        self.tool, self.ids, self.on_done = tool, ids, on_done
        names = [tool.store.get(i).name for i in ids if tool.store.get(i)]
        title = f"Log contact - {names[0]}" if len(names) == 1 else f"Log contact - {len(names)} people"
        self.win, frame = modal(tool.frame, title, width=560)
        _bind_close(self.win, self.close)
        self.win.bind("<Control-Return>", lambda e: self.save())
        if len(names) > 1:
            ttk.Label(frame, text=", ".join(names[:6]) + (" ..." if len(names) > 6 else ""), style="Muted.TLabel",
                      wraplength=500, justify="left").pack(anchor="w", pady=(0, 6))
        grid = ttk.Frame(frame)
        grid.pack(fill="x")
        contact_channels = [c for c in V.CHANNELS]
        ttk.Label(grid, text="How").grid(row=0, column=0, sticky="w", pady=4)
        cb, self._ch_get, self._ch_set = make_choice(grid, contact_channels, channel, width=26)
        cb.grid(row=0, column=1, sticky="w")
        ttk.Label(grid, text="Result").grid(row=1, column=0, sticky="w", pady=4)
        cb2, self._out_get, self._out_set = make_choice(grid, V.OUTCOMES, outcome, width=26)
        cb2.grid(row=1, column=1, sticky="w")
        ttk.Label(grid, text="Date").grid(row=2, column=0, sticky="w", pady=4)
        self.when = DateField(grid, tool, tool.today())
        self.when.grid(row=2, column=1, sticky="w")
        ttk.Label(grid, text="Next reminder").grid(row=3, column=0, sticky="w", pady=4)
        nxt = ttk.Frame(grid)
        nxt.grid(row=3, column=1, sticky="w")
        self.next = DateField(nxt, tool, None, today_button=False, on_change=self._hint)
        self.next.pack(side="left")
        for label, days in (("+1d", 1), ("+3d", 3), ("+1w", 7)):
            ttk.Button(nxt, text=label, style="Tiny.TButton",
                       command=lambda n=days: self.next.set(tool.today() + dt.timedelta(days=n))).pack(side="left", padx=1)
        self.auto_lbl = ttk.Label(frame, text="", style="Muted.TLabel", wraplength=500, justify="left")
        self.auto_lbl.pack(anchor="w")
        ttk.Label(frame, text="What was said").pack(anchor="w", pady=(8, 2))
        self.note = tk.Text(frame, height=5, width=60, wrap="word")
        style_text(frame, self.note)
        self.note.pack(fill="x")
        autocomplete(self.note, "visa.log_note")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Save  (Ctrl+Enter)", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(row, text="Cancel", command=self.close).pack(side="right", padx=6)
        self._hint()
        _grab(self.win)

    def _hint(self) -> None:
        if self.next.text():
            self.auto_lbl.configure(text="")
        else:
            self.auto_lbl.configure(text="Empty = the next reminder follows your schedule (Settings > Visa Tracker).")

    def save(self) -> None:
        if not self.when.valid() or not self.next.valid():
            error(self.win, "One of the dates is not a date. Try 15/11/2026, 15 Nov 2026 or +3.")
            return
        day = self.when.get_date() or self.tool.today()
        stamp = dt.datetime.now() if day == self.tool.today() else dt.datetime.combine(day, dt.time(12, 0))
        n = self.tool.store.log_followup(self.ids, self._ch_get() or "call", self._out_get(), self.note.get("1.0", "end").strip(),
                                         self.tool.my_name(), stamp, self.next.get_date())
        self.close()
        if self.on_done:
            self.on_done()
        self.tool.app.notify(f"Logged for {n} person(s). Ctrl+Z undoes it.", "ok")

    def close(self) -> None:
        self.win.destroy()


# --------------------------------------------------------------------------- page picker
@dataclass
class PickResult:
    source: str
    pages: list[int]                                  # 0-based pages of the source
    rotations: dict[int, int] = field(default_factory=dict)
    dates: dict = field(default_factory=dict)         # {"issue": date, "expiry": date} / {"flight": date}
    apply_dates: bool = False


class PagePreview(ttk.Frame):
    """One page of a file, fitted into the box (rendered in a thread)."""

    def __init__(self, parent: tk.Misc, app) -> None:
        super().__init__(parent)
        self.app = app
        self.canvas = tk.Canvas(self, highlightthickness=0, width=420, height=520)
        self.canvas.pack(fill="both", expand=True)
        app.theme.style_canvas(self.canvas)
        self._photo = None
        self._token = 0
        self._req: tuple | None = None
        self._after = None
        self.canvas.bind("<Configure>", lambda e: self._later())

    def _later(self) -> None:
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(150, self.render)

    def show(self, path: str, page: int, rotation: int = 0) -> None:
        self._req = (path, page, rotation)
        self.render()

    def clear(self, message: str = "") -> None:
        self._req = None
        self._token += 1
        self.canvas.delete("all")
        if message:
            self.canvas.create_text(20, 20, anchor="nw", text=message, width=380, fill=self.app.theme.p["muted"])

    def render(self) -> None:
        self._after = None
        if not self._req:
            return
        path, page, rot = self._req
        cw, ch = max(200, self.canvas.winfo_width() - 16), max(200, self.canvas.winfo_height() - 16)
        self._token += 1
        token = self._token

        def work() -> None:
            try:
                w0, h0 = self.app.renderer.page_size(path, page)
                if rot % 180:
                    w0, h0 = h0, w0
                width = max(150, min(cw, int(ch * w0 / max(1.0, h0))))
                img = self.app.renderer.render(path, page, width, rot)
            except Exception as exc:
                img = f"No preview: {exc}"
            self.app.bridge.call(self._done, token, img)

        threading.Thread(target=work, daemon=True).start()

    def _done(self, token: int, img) -> None:
        if token != self._token or not self.winfo_exists():
            return
        self.canvas.delete("all")
        if isinstance(img, str):
            self.canvas.create_text(20, 20, anchor="nw", text=img, width=380, fill=self.app.theme.p["muted"])
            return
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.create_image(self.canvas.winfo_width() // 2, 8, anchor="n", image=self._photo)


class PagePicker:
    """Pick the visa page (or ticket pages) out of a big file.  The file is read in the background; likely pages get a ★
    and the best one is selected.  on_done(PickResult) is called when the user confirms."""

    KIND_TEXT = {"visa": "visa page", "ticket": "ticket page(s)", "passport": "passport page", "other": "page(s)"}

    def __init__(self, tool, person: V.Person, kind: str, source: str, on_done: Callable[[PickResult], None]) -> None:
        self.tool, self.app, self.person, self.kind, self.source, self.on_done = tool, tool.app, person, kind, str(source), on_done
        self.texts: list[str] = []
        self.scores: list[visa_files.PageScore] = []
        self._scanned = False
        name = Path(self.source).name
        self.win, frame = modal(tool.frame, f"Choose the {self.KIND_TEXT.get(kind, 'page')} - {person.name}", width=1180, height=780)
        self.win.minsize(900, 560)
        _bind_close(self.win, self.close)
        self.win.bind("<Return>", lambda e: self.save())
        try:
            self.total = self.app.renderer.page_count(self.source)
        except PreviewUnavailable as exc:
            ttk.Label(frame, text=str(exc), wraplength=520, justify="left").pack(padx=20, pady=20)
            ttk.Button(frame, text="Close", command=self.close).pack()
            self.total = 0
            return

        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text=f"{name}  ·  {self.total} page(s)", style="H3.TLabel").pack(side="left")
        self.status = ttk.Label(top, text="Reading the text of the pages…", style="Muted.TLabel")
        self.status.pack(side="left", padx=12)
        right_tools = ttk.Frame(top)
        right_tools.pack(side="right")
        self.find_var = tk.StringVar()
        fe = ttk.Entry(right_tools, textvariable=self.find_var, width=18)
        fe.pack(side="left")
        fe.bind("<Return>", lambda e: (self.find_next(), "break")[1])
        Tooltip(fe, "Search the text of the pages (Enter = next match)")
        ttk.Button(right_tools, text="Find next", style="Tiny.TButton", command=self.find_next).pack(side="left", padx=2)
        self.only_likely = tk.BooleanVar(value=False)
        ttk.Checkbutton(right_tools, text="Only likely pages", variable=self.only_likely, command=self._filter).pack(side="left", padx=8)

        bottom = ttk.Frame(frame)                                # packed before the big panes so it keeps its room
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        paned = ttk.PanedWindow(frame, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=(8, 0))
        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        self.grid_ = ThumbGrid(left, self.app, size=120, toolbar=False)
        self.grid_.pack(fill="both", expand=True)
        self.grid_.delete_selected = lambda: None                # this window never removes pages
        self.grid_.move_selected = self._step                    # arrow keys walk through the pages
        self.grid_.on_select = self._selected
        self.grid_.on_open = lambda it: self.save()
        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        self.preview = PagePreview(right, self.app)
        self.preview.pack(fill="both", expand=True)

        self.items = [ThumbItem(self.source, page=i, label=f"p.{i + 1}") for i in range(self.total)]
        self.grid_.set_items(list(self.items))

        # bottom: selection info, dates found, buttons
        self.sel_lbl = ttk.Label(bottom, text="Click the page you want (Ctrl / Shift for several).", style="Muted.TLabel")
        self.sel_lbl.pack(anchor="w")
        self.date_fields: dict[str, DateField] = {}
        self.apply_var = tk.BooleanVar(value=False)
        if kind in ("visa", "ticket"):
            drow = ttk.Frame(bottom)
            drow.pack(fill="x", pady=4)
            specs = [("issue", "Issue date"), ("expiry", "Valid until")] if kind == "visa" else [("flight", "Flight date")]
            for key, label in specs:
                ttk.Label(drow, text=label).pack(side="left", padx=(0, 4))
                f = DateField(drow, tool, None, today_button=False)
                f.pack(side="left", padx=(0, 14))
                self.date_fields[key] = f
            self.apply_chk = ttk.Checkbutton(drow, text=("Use these dates for this person" if kind == "ticket"
                                                          else "Use these dates for this person (deadline is recalculated)"),
                                             variable=self.apply_var)
            self.apply_chk.pack(side="left")
        btns = ttk.Frame(bottom)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="⟲", width=3, style="Tiny.TButton", command=lambda: self.rotate(-90)).pack(side="left")
        ttk.Button(btns, text="⟳", width=3, style="Tiny.TButton", command=lambda: self.rotate(90)).pack(side="left", padx=2)
        ttk.Label(btns, text="turn the selected page", style="Muted.TLabel").pack(side="left", padx=6)
        ttk.Button(btns, text="Save this page", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.close).pack(side="right", padx=6)
        _grab(self.win)
        self._start_scan()

    # ------------------------------------------------------------------ reading the text
    def _start_scan(self) -> None:
        from ..core import ocr
        cfg = ocr.OcrConfig.from_settings(self.app.settings)
        pages = self.tool.get("find_ocr_pages")
        path = self.source

        def work(job):
            return visa_files.read_page_texts(path, cfg, pages)

        self.app.jobs.submit(work, "Looking for the page", tool=self.tool.id, on_done=self._scan_done,
                             on_error=self._scan_failed, serial_key="visa.scan")

    def _scan_failed(self, message: str) -> None:
        if self.win.winfo_exists():
            self.status.configure(text=f"Could not read the text ({message}). Choose the page by looking at it.")

    def _scan_done(self, result) -> None:
        if not self.win.winfo_exists():
            return
        texts, err = result
        self.texts = texts
        self._scanned = True
        self.scores = visa_files.rank_pages(texts, self.kind, self.person.passport_no, self.person.name)
        best = visa_files.likely_pages(self.scores)
        self._likely = {s.page for s in best}
        for s in best:
            if s.page < len(self.items):
                self.items[s.page].label = f"p.{s.page + 1} ★ " + ", ".join(s.reasons[:3])
        if best and not self.grid_.selected():
            self._select([best[0].page])
            self.status.configure(text=f"{len(best)} likely page(s) marked ★ - the best one is selected. Check it on the right.")
        elif best:
            self.status.configure(text=f"{len(best)} likely page(s) marked ★.")
        else:
            has_text = any(t.strip() for t in texts)
            self.status.configure(text="No page looks like it - choose it by looking at the pages." if has_text else
                                  "No text could be read (a scan without OCR) - choose the page by looking at it."
                                  + (f"  {err}" if err else ""))
        self.grid_.redraw()
        if self.grid_.selected():
            self._selected(self.grid_.selected())

    # ------------------------------------------------------------------ selecting
    def _select(self, pages: list[int]) -> None:
        for it in self.items:
            it.selected = it.page in pages
        self.grid_.redraw()
        self._selected(self.grid_.selected())
        if pages:
            self._scroll_to(pages[0])

    def _scroll_to(self, page: int) -> None:
        shown = [it.page for it in self.grid_.items]
        if page not in shown or not self.grid_._pos:
            return
        x, y, w, h = self.grid_._pos[shown.index(page)]
        last = self.grid_._pos[-1]
        total = max(1, last[1] + last[3] + 20)
        try:
            self.grid_.canvas.yview_moveto(max(0.0, (y - 30) / total))
        except tk.TclError:
            pass

    def _step(self, step: int) -> None:
        shown = self.grid_.items
        sel = [i for i, it in enumerate(shown) if it.selected]
        if not shown:
            return
        base = (sel[-1] if step > 0 else sel[0]) if sel else (-1 if step > 0 else len(shown))
        n = max(0, min(len(shown) - 1, base + step))
        self._select([shown[n].page])

    def _selected(self, items: list[ThumbItem]) -> None:
        if not items:
            self.sel_lbl.configure(text="Click the page you want (Ctrl / Shift for several).")
            return
        pages = [it.page for it in items]
        self.sel_lbl.configure(text=("Selected: page " if len(pages) == 1 else "Selected: pages ")
                               + ", ".join(str(p + 1) for p in pages[:12]) + (" …" if len(pages) > 12 else ""))
        self.preview.show(self.source, pages[0], items[0].rotation)
        if self.date_fields and pages[0] < len(self.texts):
            found = visa_files.suggest_dates(self.texts[pages[0]], self.kind, self.tool.today())
            filled = False
            for key, f in self.date_fields.items():
                f.set(found.get(key))
                filled = filled or bool(found.get(key))
            if filled:
                current = V.to_date(self.person.visa_issue if self.kind == "visa" else self.person.flight_date)
                self.apply_var.set(current is None)           # a person without dates gets them filled in

    def rotate(self, degrees: int) -> None:
        self.grid_.rotate_selected(degrees)
        sel = self.grid_.selected()
        if sel:
            self.preview.show(self.source, sel[0].page, sel[0].rotation)

    def find_next(self) -> None:
        q = self.find_var.get().strip().lower()
        if not q or not self.texts:
            return
        sel = self.grid_.selected()
        start = (sel[-1].page + 1) if sel else 0
        order = list(range(start, len(self.texts))) + list(range(0, start))
        hit = next((p for p in order if q in self.texts[p].lower()), None)
        if hit is None:
            self.status.configure(text=f"'{q}' was not found in the text of the pages.")
        else:
            self._select([hit])

    def _filter(self) -> None:
        if self.only_likely.get() and getattr(self, "_likely", None):
            self.grid_.set_items([it for it in self.items if it.page in self._likely])
        else:
            self.grid_.set_items(list(self.items))

    # ------------------------------------------------------------------ result
    def save(self) -> None:
        chosen = self.grid_.selected()
        if not chosen:
            info(self.win, "Click the page you want first.")
            return
        for f in self.date_fields.values():
            if not f.valid():
                error(self.win, "One of the dates is not a date. Correct it or clear the box.")
                return
        dates = {k: f.get_date() for k, f in self.date_fields.items() if f.get_date()}
        result = PickResult(self.source, sorted(it.page for it in chosen),
                            {it.page: it.rotation for it in chosen if it.rotation % 360}, dates,
                            bool(self.apply_var.get() and dates))
        self.close()
        self.on_done(result)

    def close(self) -> None:
        self.win.destroy()


# --------------------------------------------------------------------------- multi-visa PDF semi-auto review & import
@dataclass
class VisaImportItem:
    """One extracted visa page ready for user review before importing."""
    page: int
    extracted: visa_files.ExtractedVisa
    selected: bool = False
    candidate_hits: list[Hit] = field(default_factory=list)
    match_state: str = "nosheet"              # match | possible | none | nosheet
    selected_hit_idx: int = 0
    existing_id: str = ""
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def current_hit(self) -> Hit | None:
        if 0 <= self.selected_hit_idx < len(self.candidate_hits):
            return self.candidate_hits[self.selected_hit_idx]
        return None


class VisaPdfImportDialog:
    """Semi-auto extraction from a multi-visa PDF: extracts every visa page, checks each against the candidate
    information sheet (handling passport vs. list name differences), and lets the user review, edit, and choose
    which visas to import before anything is added."""

    def __init__(self, tool, source: str,
                 on_done: Callable[[str, list[VisaImportItem], bool], None]) -> None:
        self.tool, self.app, self.source, self.on_done = tool, tool.app, str(source), on_done
        self.items: list[VisaImportItem] = []
        self.current_page: int | None = None
        self._loading_editor = False
        self._sheet_error = ""

        name = Path(self.source).name
        self.win, frame = modal(tool.frame, f"Review extracted visas - {name}", width=1240, height=780)
        self.win.minsize(960, 600)
        _bind_close(self.win, self.close)

        # -- top bar -------------------------------------------------------------------------------
        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text=f"📄 {name}", style="H3.TLabel").pack(side="left")
        self.status = ttk.Label(top, text="Reading visa pages and checking candidate list…", style="Muted.TLabel")
        self.status.pack(side="left", padx=12)
        self.sheet_lbl = ttk.Label(top, text="", style="Muted.TLabel")
        self.sheet_lbl.pack(side="right")

        # -- bottom bar (packed first so it stays visible) -----------------------------------------
        bottom = ttk.Frame(frame)
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        opts = ttk.Frame(bottom)
        opts.pack(fill="x", pady=(0, 6))
        self.attach_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Attach each visa's single PDF page to the person's Documents",
                        variable=self.attach_var).pack(side="left")
        ttk.Label(opts, text="Group (optional):").pack(side="left", padx=(16, 4))
        self.batch_group = tk.StringVar(value=tool._defaults().get("group", ""))
        ttk.Entry(opts, textvariable=self.batch_group, width=20).pack(side="left")

        btns = ttk.Frame(bottom)
        btns.pack(fill="x")
        self.summary_lbl = ttk.Label(btns, text="", style="H3.TLabel")
        self.summary_lbl.pack(side="left")
        self.import_btn = ttk.Button(btns, text="Import selected visas", style="Accent.TButton",
                                     command=self.run, state="disabled")
        self.import_btn.pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.close).pack(side="right", padx=6)

        # -- main split: left = extracted visas table, right = preview + editable review -----------
        paned = ttk.PanedWindow(frame, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=(8, 0))

        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        tbar = ttk.Frame(left)
        tbar.pack(fill="x", pady=(0, 4))
        for text, cmd in (("☑ Toggle use", self.toggle_selected),
                          ("Select matched", self.select_matched),
                          ("Select all", self.select_all),
                          ("Select none", self.select_none)):
            ttk.Button(tbar, text=text, style="Tiny.TButton", command=cmd).pack(side="left", padx=(0, 4))
        self.only_visas = tk.BooleanVar(value=True)
        ttk.Checkbutton(tbar, text="Hide non-visa pages", variable=self.only_visas,
                        command=self._rebuild_table).pack(side="right")

        cols = [("use", "Use", 42, "center"), ("page", "Page", 48, "center"), ("name", "Name (review)", 175, "w"),
                ("passport", "Passport", 92, "w"), ("issued", "Issued", 88, "w"), ("days", "Days", 46, "e"),
                ("candidate", "Candidate list check", 210, "w"), ("tracker", "Tracker", 68, "w")]
        self.table = DataTable(left, cols, height=16, searchable=True, stretch="candidate",
                               on_select=self._row_selected, on_double=lambda iid: self.toggle_selected())
        self.table.tag_colors(match="ok", possible="warn", none="muted", nosheet="fg")
        self.table.pack(fill="both", expand=True)
        self.table.tree.bind("<space>", lambda e: (self.toggle_selected(), "break")[1])

        right = ttk.Frame(paned, padding=(8, 0, 0, 0))
        paned.add(right, weight=2)
        self.preview = PagePreview(right, self.app)
        if hasattr(self.preview, "canvas"):
            self.preview.canvas.configure(height=240)
        self.preview.pack(fill="both", expand=True)

        # Editable fields for the currently selected visa (DocPacker-style label-above-entry grid)
        ed_box = ttk.LabelFrame(right, text="Extracted visa (review & edit)", padding=(8, 4, 8, 6))
        ed_box.pack(fill="x", pady=(6, 0))
        for c in range(3):
            ed_box.columnconfigure(c, weight=1, uniform="ed")
        self.ed_vars: dict[str, tk.StringVar] = {}
        ed_specs = [
            ("name", "Full name", 0, 0, 2), ("passport_no", "Passport no.", 0, 2, 1),
            ("nationality", "Nationality", 1, 0, 1), ("visa_issue", "Date of issue (YYYY-MM-DD)", 1, 1, 1),
            ("visa_days", "Entry validity (days)", 1, 2, 1),
            ("visa_no", "Entry visa no.", 2, 0, 1), ("visa_type", "Visa type", 2, 1, 2),
            ("phone", "Phone / WhatsApp", 3, 0, 1), ("email", "E-mail", 3, 1, 1), ("position", "Position", 3, 2, 1),
        ]
        for key, label, r, c, span in ed_specs:
            cell = ttk.Frame(ed_box)
            cell.grid(row=r, column=c, columnspan=span, sticky="ew", padx=(0, 6), pady=(0, 3))
            ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
            var = tk.StringVar()
            var.trace_add("write", lambda *_, k=key: self._field_edited(k))
            self.ed_vars[key] = var
            ttk.Entry(cell, textvariable=var).pack(fill="x")

        # Candidate list check panel for the selected visa
        cand_box = ttk.LabelFrame(right, text="Candidate list check", padding=(8, 4, 8, 6))
        cand_box.pack(fill="x", pady=(6, 0))
        self.cand_status_lbl = ttk.Label(cand_box, text="Select a visa page to inspect its match.", style="Muted.TLabel",
                                         wraplength=420, justify="left")
        self.cand_status_lbl.pack(anchor="w")
        self.cand_pick_var = tk.StringVar()
        self.cand_pick_cb = ttk.Combobox(cand_box, textvariable=self.cand_pick_var, state="readonly")
        self.cand_pick_cb.pack(fill="x", pady=(4, 2))
        self.cand_pick_cb.bind("<<ComboboxSelected>>", lambda e: self._candidate_hit_picked())
        self.cand_detail_lbl = ttk.Label(cand_box, text="", style="Muted.TLabel", wraplength=420, justify="left")
        self.cand_detail_lbl.pack(anchor="w", pady=(2, 4))
        cbtns = ttk.Frame(cand_box)
        cbtns.pack(fill="x")
        ttk.Button(cbtns, text="Use candidate name", style="Tiny.TButton",
                   command=self.use_candidate_name).pack(side="left", padx=(0, 4))
        ttk.Button(cbtns, text="Fill contact info", style="Tiny.TButton",
                   command=self.fill_from_candidate).pack(side="left", padx=(0, 4))
        ttk.Button(cbtns, text="👤 View full row", style="Tiny.TButton",
                   command=self.view_candidate_row).pack(side="left")

        _grab(self.win)
        self._start_extract()

    # ------------------------------------------------------------------ background extraction
    def _start_extract(self) -> None:
        from ..core import ocr
        cfg = ocr.OcrConfig.from_settings(self.app.settings)
        ocr_pages = self.tool.get("find_ocr_pages")
        sheet_path, sheet_tab, name_col, header_row = self.tool.candidate_sheet_config()
        source = self.source

        def work(job):
            texts, err = visa_files.read_page_texts(source, cfg, ocr_pages)
            extracted = visa_files.extract_visas_from_texts(texts)
            matches: list[tuple[list[Hit], str]] = []
            sheet_err = ""
            for ex in extracted:
                if sheet_path and not sheet_err:
                    try:
                        hits = match_candidate(
                            sheet_path, name=ex.full_name, passport_no=ex.passport_no, dob=ex.date_of_birth,
                            sheet=sheet_tab, name_column=name_col, header_row=header_row, threshold=0.55,
                        )
                        matches.append((hits, match_status(hits)))
                    except Exception as exc:
                        sheet_err = str(exc)
                        matches.append(([], "nosheet"))
                else:
                    matches.append(([], "nosheet"))
            return extracted, matches, err, sheet_path, sheet_err

        self.app.jobs.submit(work, "Extracting visas from PDF", tool=self.tool.id, on_done=self._extract_done,
                             on_error=self._extract_failed, serial_key="visa.pdf_import")

    def _extract_failed(self, message: str) -> None:
        if self.win.winfo_exists():
            self.status.configure(text=f"Could not read PDF: {message}", style="Err.TLabel")

    def _extract_done(self, result) -> None:
        if not self.win.winfo_exists():
            return
        extracted, matches, err, sheet_path, sheet_err = result
        self._sheet_error = sheet_err
        if sheet_path and not sheet_err:
            self.sheet_lbl.configure(text=f"Candidate sheet: {Path(sheet_path).name}")
        elif sheet_err:
            self.sheet_lbl.configure(text=f"Sheet error: {sheet_err}", style="Warn.TLabel")
        else:
            self.sheet_lbl.configure(text="No candidate sheet configured (Settings > Visa Tracker)")

        self.items = []
        for ex, (hits, mstate) in zip(extracted, matches):
            twin = self.tool.store.find_passport(ex.passport_no) if ex.passport_no else None
            # Auto-fill missing contact info from high-confidence candidate sheet match
            fmap = hits[0].field_map() if (hits and mstate == "match") else {}
            note_parts = []
            if ex.application_no:
                note_parts.append(f"App No: {ex.application_no}")
            if ex.duration_of_stay:
                note_parts.append(f"Stay: {ex.duration_of_stay}d")
            if ex.requester:
                note_parts.append(f"Requester: {ex.requester}")
            fields = {
                "name": ex.full_name or fmap.get("name", ""),
                "passport_no": ex.passport_no or fmap.get("passport_no", ""),
                "nationality": ex.nationality or fmap.get("nationality", ""),
                "visa_no": ex.visa_no,
                "visa_type": ex.visa_type,
                "visa_issue": ex.visa_issue,
                "visa_days": str(ex.entry_validity or ""),
                "visa_deadline": ex.visa_deadline,
                "phone": ex.mobile or fmap.get("phone", ""),
                "email": ex.email or fmap.get("email", ""),
                "position": fmap.get("position", ""),
                "group": fmap.get("group", ""),
                "notes": " | ".join(note_parts),
            }
            # Default selection: if candidate sheet is active, check confirmed matches; otherwise check all visas
            default_use = bool(ex.is_visa and (mstate == "match" if (sheet_path and not sheet_err) else True))
            self.items.append(VisaImportItem(
                page=ex.page, extracted=ex, selected=default_use, candidate_hits=hits,
                match_state=mstate, selected_hit_idx=0, existing_id=twin.id if twin else "", fields=fields,
            ))

        visas_found = sum(1 for it in self.items if it.extracted.is_visa)
        if not visas_found and self.items:
            self.only_visas.set(False)
        self.status.configure(
            text=f"{len(self.items)} page(s) read · {visas_found} visa(s) detected. Review and check the ones to import."
            + (f"  ({err})" if err else "")
        )
        self._rebuild_table()
        all_ids = self.table.all_ids()
        if all_ids:
            self.table.tree.selection_set(all_ids[0])
            self._row_selected([all_ids[0]])

    # ------------------------------------------------------------------ table & selection
    def _visible_items(self) -> list[VisaImportItem]:
        if self.only_visas.get():
            return [it for it in self.items if it.extracted.is_visa]
        return list(self.items)

    def _item_by_page(self, page: int) -> VisaImportItem | None:
        return next((it for it in self.items if it.page == page), None)

    @staticmethod
    def _match_text(it: VisaImportItem) -> str:
        if it.match_state == "nosheet":
            return "— No sheet"
        hit = it.current_hit
        if hit is None:
            return "✗ Not in candidate list"
        cname = hit.candidate_name or (hit.fields[0][1] if hit.fields else "?")
        if it.match_state == "match":
            tag = "passport" if hit.matched_by == "passport" else f"{hit.score:.0%}"
            return f"✔ {tag}: {cname}"
        return f"⚠ Possible ({hit.score:.0%}): {cname}"

    def _row_dict(self, it: VisaImportItem) -> dict:
        f = it.fields
        return {
            "use": "☑" if it.selected else "☐",
            "page": f"p.{it.page + 1}",
            "name": f.get("name") or "(no name)",
            "passport": f.get("passport_no", ""),
            "issued": f.get("visa_issue", ""),
            "days": f.get("visa_days", ""),
            "candidate": self._match_text(it),
            "tracker": "Update" if it.existing_id else "New",
        }

    def _rebuild_table(self) -> None:
        keep = list(self.table.selection())
        self.table.clear()
        for it in self._visible_items():
            self.table.add(self._row_dict(it), iid=str(it.page), tags=(it.match_state,))
        keep = [i for i in keep if self.table.tree.exists(i)]
        if keep:
            self.table.tree.selection_set(keep)
        self._update_summary()

    def _refresh_row(self, it: VisaImportItem) -> None:
        iid = str(it.page)
        if self.table.tree.exists(iid):
            row = self._row_dict(it)
            vals = [row.get(k, "") for k, *_ in self.table.columns]
            self.table.tree.item(iid, values=vals, tags=(it.match_state,))
            self.table._rows[iid] = (row, (it.match_state,))
        self._update_summary()

    def _update_summary(self) -> None:
        vis = self._visible_items()
        sel = sum(1 for it in self.items if it.selected)
        matched = sum(1 for it in vis if it.match_state == "match")
        possible = sum(1 for it in vis if it.match_state == "possible")
        parts = [f"{sel} of {len(vis)} selected"]
        if any(it.match_state != "nosheet" for it in vis):
            parts.append(f"{matched} matched")
            if possible:
                parts.append(f"{possible} possible (check)")
        self.summary_lbl.configure(text="  ·  ".join(parts))
        self.import_btn.configure(state="normal" if sel > 0 else "disabled")

    def toggle_selected(self) -> None:
        for iid in self.table.selection():
            it = self._item_by_page(int(iid))
            if it is not None:
                it.selected = not it.selected
                self._refresh_row(it)

    def select_matched(self) -> None:
        for it in self._visible_items():
            it.selected = it.match_state == "match"
            self._refresh_row(it)

    def select_all(self) -> None:
        for it in self._visible_items():
            it.selected = True
            self._refresh_row(it)

    def select_none(self) -> None:
        for it in self.items:
            it.selected = False
            self._refresh_row(it)

    # ------------------------------------------------------------------ right pane review & edit
    def _row_selected(self, ids) -> None:
        if not ids:
            self.current_page = None
            return
        it = self._item_by_page(int(ids[0]))
        if it is None:
            return
        self.current_page = it.page
        self.preview.show(self.source, it.page, 0)
        self._loading_editor = True
        try:
            for key, var in self.ed_vars.items():
                var.set(it.fields.get(key, ""))
        finally:
            self._loading_editor = False
        self._render_candidate_box(it)

    def _field_edited(self, key: str) -> None:
        if self._loading_editor or self.current_page is None:
            return
        it = self._item_by_page(self.current_page)
        if it is None:
            return
        it.fields[key] = self.ed_vars[key].get().strip()
        if key == "passport_no":
            twin = self.tool.store.find_passport(it.fields[key]) if it.fields[key] else None
            it.existing_id = twin.id if twin else ""
        self._refresh_row(it)

    def _render_candidate_box(self, it: VisaImportItem) -> None:
        if it.match_state == "nosheet":
            self.cand_status_lbl.configure(
                text=self._sheet_error or "No candidate sheet configured. Set one in Settings > Visa Tracker (or DocPacker).",
                style="Muted.TLabel",
            )
            self.cand_pick_cb.configure(values=[])
            self.cand_pick_var.set("")
            self.cand_detail_lbl.configure(text="")
            return
        if not it.candidate_hits:
            self.cand_status_lbl.configure(
                text=f"✗ Not found in candidate list (searched name “{it.extracted.full_name}” / passport “{it.extracted.passport_no}”).",
                style="Warn.TLabel",
            )
            self.cand_pick_cb.configure(values=[])
            self.cand_pick_var.set("")
            self.cand_detail_lbl.configure(text="Leave unchecked to skip this person, or check 'Use' to import anyway.")
            return
        labels = [
            f"{h.candidate_name or (h.fields[0][1] if h.fields else '?')}  ({h.score:.0%}, {h.matched_by}, row {h.row})"
            for h in it.candidate_hits
        ]
        self.cand_pick_cb.configure(values=labels)
        idx = max(0, min(it.selected_hit_idx, len(labels) - 1))
        self.cand_pick_var.set(labels[idx])
        hit = it.candidate_hits[idx]
        style = "Ok.TLabel" if it.match_state == "match" else "Warn.TLabel"
        prefix = "✔ Matched" if it.match_state == "match" else "⚠ Possible match (please check)"
        self.cand_status_lbl.configure(text=f"{prefix} — row {hit.row} ({hit.score:.0%}, {hit.matched_by})", style=style)
        preview_bits = [f"{k}: {v}" for k, v in hit.fields[:5]]
        self.cand_detail_lbl.configure(text="  ·  ".join(preview_bits))

    def _candidate_hit_picked(self) -> None:
        if self.current_page is None:
            return
        it = self._item_by_page(self.current_page)
        if it is None or not it.candidate_hits:
            return
        vals = list(self.cand_pick_cb.cget("values") or [])
        cur = self.cand_pick_var.get()
        if cur in vals:
            it.selected_hit_idx = vals.index(cur)
            self._render_candidate_box(it)
            self._refresh_row(it)

    def use_candidate_name(self) -> None:
        if self.current_page is None:
            return
        it = self._item_by_page(self.current_page)
        if it is None or it.current_hit is None:
            return
        cname = it.current_hit.candidate_name
        if cname:
            passport_name = it.extracted.full_name
            if passport_name and passport_name.lower() != cname.lower() and "Passport name:" not in it.fields.get("notes", ""):
                prefix = f"Passport name: {passport_name}"
                it.fields["notes"] = (prefix + " | " + it.fields["notes"]).strip(" |")
            self.ed_vars["name"].set(cname)

    def fill_from_candidate(self) -> None:
        if self.current_page is None:
            return
        it = self._item_by_page(self.current_page)
        if it is None or it.current_hit is None:
            return
        fmap = it.current_hit.field_map()
        for key in ("phone", "email", "position", "nationality"):
            if fmap.get(key) and not self.ed_vars[key].get().strip():
                self.ed_vars[key].set(fmap[key])
        if fmap.get("group") and not it.fields.get("group"):
            it.fields["group"] = fmap["group"]

    def view_candidate_row(self) -> None:
        if self.current_page is None:
            return
        it = self._item_by_page(self.current_page)
        if it is None:
            return
        path, tab, name_col, header_row = self.tool.candidate_sheet_config()
        show_candidate_info_popup(
            self.win, self.app, it.fields.get("name", ""), path, tab, name_col, header_row,
            passport_no=it.fields.get("passport_no", ""), dob=it.extracted.date_of_birth,
            settings_tool_id=self.tool.id,
        )

    # ------------------------------------------------------------------ confirm import
    def run(self) -> None:
        chosen = [it for it in self.items if it.selected]
        if not chosen:
            info(self.win, "Check at least one visa to import.")
            return
        grp = self.batch_group.get().strip()
        for it in chosen:
            if not it.fields.get("name", "").strip():
                error(self.win, f"Page {it.page + 1} has no name. Enter a name or uncheck it.")
                return
            if grp and not it.fields.get("group", "").strip():
                it.fields["group"] = grp
        attach = bool(self.attach_var.get())
        self.close()
        self.on_done(self.source, chosen, attach)

    def close(self) -> None:
        self.win.destroy()


# --------------------------------------------------------------------------- what kind of file is it?
class KindDialog:
    """A dropped file: is it the visa, the ticket ...?"""

    def __init__(self, tool, filename: str, on_pick: Callable[[str], None]) -> None:
        self.win, frame = modal(tool.frame, "What is this file?", width=460)
        _bind_close(self.win, self.win.destroy)
        ttk.Label(frame, text=filename, style="H3.TLabel", wraplength=420).pack(anchor="w")
        ttk.Label(frame, text="Which document are you adding to this person?", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        for kind, label in V.DOC_KINDS:
            ttk.Button(frame, text=f"{label}" + ("  - choose the page" if kind == "visa" else ""),
                       command=lambda k=kind: (self.win.destroy(), on_pick(k))).pack(fill="x", pady=2)
        ttk.Button(frame, text="Cancel", command=self.win.destroy).pack(anchor="e", pady=(8, 0))
        _grab(self.win)


# --------------------------------------------------------------------------- spreadsheet import
class ImportDialog:
    """Choose which column is which, check the result, import.  on_done(people, mode, problems)."""

    def __init__(self, tool, path: str, on_done: Callable[[list[V.Person], str, list[str]], None]) -> None:
        self.tool, self.on_done = tool, on_done
        try:
            self.book = load_book(path)
        except SheetError as exc:
            error(tool.frame, str(exc))
            self.win = None
            return
        self.win, frame = modal(tool.frame, f"Import people - {Path(path).name}", width=760, height=700)
        _bind_close(self.win, self.close)
        self.table = None
        self.people: list[V.Person] = []
        self.problems: list[str] = []
        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text="Sheet").pack(side="left")
        self.sheet = tk.StringVar(value=self.book.names[0] if self.book.names else "")
        sb = ttk.Combobox(top, textvariable=self.sheet, values=self.book.names, state="readonly", width=24)
        sb.pack(side="left", padx=6)
        sb.bind("<<ComboboxSelected>>", lambda e: self._load_sheet())
        ttk.Label(top, text="Header row").pack(side="left", padx=(12, 2))
        self.header_row = tk.StringVar(value="1")
        sp = ttk.Spinbox(top, textvariable=self.header_row, from_=1, to=50, width=4)
        sp.pack(side="left")
        self.header_row.trace_add("write", lambda *_: self._load_sheet(keep_header=True))

        bottom = ttk.Frame(frame)
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        self.result_lbl = ttk.Label(bottom, text="", style="H3.TLabel")
        self.result_lbl.pack(anchor="w")
        opts = ttk.Frame(bottom)
        opts.pack(fill="x", pady=4)
        ttk.Label(opts, text="If the person already exists (same passport):").pack(side="left")
        self.mode = tk.StringVar(value="skip")
        for value, label in (("skip", "Skip"), ("update", "Update"), ("add", "Add again")):
            ttk.Radiobutton(opts, text=label, value=value, variable=self.mode).pack(side="left", padx=6)
        grp = ttk.Frame(bottom)
        grp.pack(fill="x", pady=2)
        ttk.Label(grp, text="Put everybody into the group (optional):").pack(side="left")
        self.group = tk.StringVar()
        ttk.Entry(grp, textvariable=self.group, width=24).pack(side="left", padx=6)
        row = ttk.Frame(bottom)
        row.pack(fill="x", pady=(8, 0))
        self.import_btn = ttk.Button(row, text="Import", style="Accent.TButton", command=self.run)
        self.import_btn.pack(side="right")
        ttk.Button(row, text="Cancel", command=self.close).pack(side="right", padx=6)
        ttk.Button(row, text="Show problems…", command=self.show_problems).pack(side="left")

        ttk.Label(frame, text="Match each field to a column of your sheet (done automatically - please check):",
                  style="Muted.TLabel").pack(anchor="w", pady=(10, 2))
        from .widgets import ScrollFrame
        sc = ScrollFrame(frame)
        sc.pack(fill="both", expand=True)
        self.map_vars: dict[str, tk.StringVar] = {}
        self.map_boxes: dict[str, ttk.Combobox] = {}
        for field_name, label, _ in visa_io.IMPORT_FIELDS[::-1]:          # name first
            r = ttk.Frame(sc.body)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=label + (" *" if field_name == "name" else ""), width=30).pack(side="left")
            var = tk.StringVar()
            cb = ttk.Combobox(r, textvariable=var, state="readonly", width=34)
            cb.pack(side="left")
            var.trace_add("write", lambda *_: self._recount())
            self.map_vars[field_name], self.map_boxes[field_name] = var, cb
        self._custom_cols = []
        self._load_sheet()
        _grab(self.win)

    NONE = "(not in the file)"

    def _guess_header_row(self, aoa: list[list]) -> int:
        best, best_n = 0, -1
        for i, row in enumerate(aoa[:10]):
            n = sum(1 for c in row if isinstance(c, str) and c.strip())
            if n > best_n:
                best, best_n = i, n
        return best

    def _load_sheet(self, keep_header: bool = False) -> None:
        aoa = self.book.sheets.get(self.sheet.get(), [])
        if not keep_header:
            self.header_row.set(str(self._guess_header_row(aoa) + 1))
        try:
            hr = max(0, int(self.header_row.get()) - 1)
        except ValueError:
            hr = 0
        self.table = self.book.table(self.sheet.get(), hr)
        headers = [self.NONE] + self.table.headers
        guess = visa_io.guess_mapping(self.table.headers)
        for name, cb in self.map_boxes.items():
            cb.configure(values=headers)
            self.map_vars[name].set(guess.get(name) or self.NONE)
        self._recount()

    def mapping(self) -> dict[str, str]:
        return {k: ("" if v.get() == self.NONE else v.get()) for k, v in self.map_vars.items()}

    def _recount(self) -> None:
        if self.table is None:
            return
        if not self.mapping().get("name"):
            self.people, self.problems = [], []
            self.result_lbl.configure(text="Choose the column that holds the names.", style="Warn.TLabel")
            self.import_btn.configure(state="disabled")
            return
        self.people, self.problems = visa_io.table_to_people(self.table.records(), self.mapping(), self.tool.today(),
                                                             self.tool.date_order(), self.tool.custom_fields())
        text = f"{len(self.people)} people found"
        if self.problems:
            text += f"  ·  {len(self.problems)} problem(s)"
        self.result_lbl.configure(text=text, style="Warn.TLabel" if self.problems else "Ok.TLabel")
        self.import_btn.configure(state="normal" if self.people else "disabled")

    def show_problems(self) -> None:
        if not self.problems:
            info(self.win, "No problems found.")
            return
        more = f"\n... and {len(self.problems) - 25} more" if len(self.problems) > 25 else ""
        info(self.win, "\n".join(self.problems[:25]) + more, "Problems found")

    def run(self) -> None:
        if not self.people:
            return
        grp = self.group.get().strip()
        if grp:
            for p in self.people:
                p.group = grp
        self.close()
        self.on_done(self.people, self.mode.get(), self.problems)

    def close(self) -> None:
        if self.win is not None:
            self.win.destroy()


# --------------------------------------------------------------------------- message templates
AUTO_CHOICES = [("", "Only when I pick it"), ("check", "Normal check-in"), ("ticket", "When the ticket is booked"),
                ("urgent", "When time is running out")]


class TemplateDialog:
    """Edit the message texts.  Placeholders in {braces} are replaced with the person's data."""

    HELP = ("Placeholders: {name} {first_name} {deadline} {days_left} {days_text} {issue_date} {flight_date} "
            "{flight_no} {position} {passport_no} {my_name} {company} {today}  - and your own extra fields, "
            "e.g. {agency}.")

    def __init__(self, tool, on_done: Callable[[], None] | None = None) -> None:
        self.tool, self.on_done = tool, on_done
        self.items: list[dict] = [dict(t) for t in tool.templates()]
        self.win, frame = modal(tool.frame, "Message templates", width=820, height=560)
        _bind_close(self.win, self.close)
        ttk.Label(frame, text=self.HELP, style="Muted.TLabel", wraplength=760, justify="left").pack(anchor="w", pady=(0, 6))
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.listbox = tk.Listbox(left, width=26, height=14, exportselection=False)
        self.listbox.pack(fill="y", expand=True)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self._pick())
        lb = ttk.Frame(left)
        lb.pack(fill="x", pady=4)
        ttk.Button(lb, text="＋ New", style="Tiny.TButton", command=self.add).pack(side="left")
        ttk.Button(lb, text="Delete", style="Tiny.TButton", command=self.delete).pack(side="left", padx=4)
        right = ttk.Frame(body, padding=(10, 0))
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="Name").pack(anchor="w")
        self.name = tk.StringVar()
        ttk.Entry(right, textvariable=self.name).pack(fill="x")
        ttk.Label(right, text="Use it automatically").pack(anchor="w", pady=(6, 0))
        cb, self._auto_get, self._auto_set = make_choice(right, AUTO_CHOICES, "", width=30)
        cb.pack(anchor="w")
        ttk.Label(right, text="Text").pack(anchor="w", pady=(6, 0))
        self.text = tk.Text(right, height=10, wrap="word")
        style_text(frame, self.text)
        self.text.pack(fill="both", expand=True)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(row, text="Cancel", command=self.close).pack(side="right", padx=6)
        ttk.Button(row, text="Back to the standard texts", command=self.reset).pack(side="left")
        self.current = -1
        self._fill_list()
        if self.items:
            self._show(0)
        _grab(self.win)

    def _fill_list(self) -> None:
        self.listbox.delete(0, "end")
        for t in self.items:
            self.listbox.insert("end", t.get("name", "?"))

    def _store_current(self) -> None:
        if 0 <= self.current < len(self.items):
            self.items[self.current].update(name=self.name.get().strip() or "Untitled", auto=self._auto_get(),
                                            text=self.text.get("1.0", "end").strip())

    def _show(self, index: int) -> None:
        self.current = index
        t = self.items[index]
        self.name.set(t.get("name", ""))
        self._auto_set(t.get("auto", ""))
        self.text.delete("1.0", "end")
        self.text.insert("1.0", t.get("text", ""))
        self.listbox.selection_set(index)

    def _pick(self) -> None:
        sel = self.listbox.curselection()
        if sel and sel[0] != self.current:
            self._store_current()
            self._fill_list()
            self._show(sel[0])

    def add(self) -> None:
        self._store_current()
        self.items.append({"name": "New template", "auto": "", "text": "Hello {first_name}, "})
        self._fill_list()
        self._show(len(self.items) - 1)

    def delete(self) -> None:
        if 0 <= self.current < len(self.items) and len(self.items) > 1:
            del self.items[self.current]
            self._fill_list()
            self._show(min(self.current, len(self.items) - 1))

    def reset(self) -> None:
        if confirm(self.win, "Replace all templates by the standard ones?"):
            self.items = [dict(t) for t in visa_io.DEFAULT_TEMPLATES]
            self._fill_list()
            self._show(0)

    def save(self) -> None:
        self._store_current()
        self.tool.put_templates(self.items)
        self.close()
        if self.on_done:
            self.on_done()

    def close(self) -> None:
        self.win.destroy()
