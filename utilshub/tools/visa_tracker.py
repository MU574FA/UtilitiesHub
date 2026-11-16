"""Visa Tracker: who has a visa that must be used in time, how many days are left, who to contact today, tickets,
the stored visa page - and a log that proves every follow-up.

Core logic lives in core/visa.py (rules, storage), core/visa_io.py (import / export / messages) and
core/visa_files.py (finding and cutting the visa page); the windows are in ui/visa_dialogs.py.
To add a field to a person: add it to `Person` (core/visa.py), then to PersonDialog and to COLUMNS / the Details tab here.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

from ..core import visa as V
from ..core import visa_files, visa_io
from ..core.schema import Opt
from ..core.settings import data_dir
from ..core.sheets import write_csv, write_xlsx
from ..core.system import open_path, reveal
from ..ui.dnd import DropZone
from ..ui.viewer import PageViewer
from ..ui.visa_dialogs import (FollowupDialog, ImportDialog, KindDialog, PagePicker, PersonDialog, PickResult,
                               TemplateDialog)
from ..ui.widgets import DataTable, ask_text, confirm, copy_to_clipboard, error, info, style_text, tip
from .base import Command, Tool

log = logging.getLogger(__name__)

# key, heading, width, anchor
COLUMNS = [("lvl", "●", 36, "center"), ("name", "Name", 170, "w"), ("days", "Days left", 70, "e"),
           ("deadline", "Deadline", 95, "w"), ("todo", "To do", 190, "w"), ("ticket", "Ticket", 120, "w"),
           ("flight", "Flight", 90, "w"), ("last", "Last contact", 100, "w"), ("next", "Next follow-up", 105, "w"),
           ("phone", "Phone", 115, "w"), ("passport", "Passport", 100, "w"), ("position", "Position", 110, "w"),
           ("group", "Group", 90, "w"), ("issued", "Visa issued", 95, "w"), ("status", "Status", 120, "w")]
DEFAULT_COLUMNS = ["lvl", "name", "days", "deadline", "todo", "ticket", "last", "next", "phone"]
CHIPS = {"active": "Waiting", "todo": "📞 To do today", "overdue": "🔴 Overdue", "d7": "🔴 Critical", "d14": "🟠 Urgent",
         "d30": "🟡 Soon", "noticket": "🎫 No ticket", "closed": "📁 Closed", "all": "Everyone"}
TABS = [("details", "Details"), ("follow", "Follow-ups & messages"), ("docs", "Documents")]
ALL_GROUPS = "All groups"


class VisaTracker(Tool):
    id = "visa"
    title = "Visa Tracker"
    icon = "🛫"
    order = 5
    stats_when_closed = True
    description = ("Track every candidate's visa window (e.g. 90 days), who to contact today, tickets, the stored visa "
                   "page and a proof log of every follow-up.")

    # ------------------------------------------------------------------ settings
    @classmethod
    def options(cls) -> list[Opt]:
        return [
            Opt("visa.default_days", 90, "Visa entry window (days)", "int", min=1, max=720, section="Visa rules",
                help="How long after the issue date the person can still enter. A single visa can override it."),
            Opt("visa.inclusive", False, "The issue date counts as day 1", "bool", section="Visa rules",
                help="On = deadline is issue date + days - 1. Off = issue date + days."),
            Opt("visa.critical_days", 7, "Red (critical) when this many days are left or fewer", "int", min=0, max=365,
                section="Visa rules"),
            Opt("visa.urgent_days", 14, "Orange (urgent) from", "int", min=0, max=365, section="Visa rules"),
            Opt("visa.warn_days", 30, "Yellow (soon) from", "int", min=0, max=365, section="Visa rules"),
            Opt("visa.ticket_warn_days", 21, "Warn when there is still no ticket and this many days are left", "int",
                min=0, max=365, section="Visa rules"),
            Opt("visa.cadence", "60:14, 30:7, 14:3, 0:1", "How often to contact a person", "str", width=40,
                section="Follow-up rhythm",
                help="Pairs 'days left : contact every N days'. The default means: with 60+ days left every 14 days, "
                     "from 30 every 7, from 14 every 3, and every day when less than 14 days are left."),
            Opt("visa.retry_days", 1, "After 'no answer' try again after (days)", "int", min=1, max=30, section="Follow-up rhythm"),
            Opt("visa.remind_on_start", True, "Show a reminder when the program opens", "bool", section="Reminders"),
            Opt("visa.remind_every_min", 60, "Remind again every (minutes, 0 = never)", "int", min=0, max=1440, section="Reminders",
                help="Only while the program is open and something needs doing."),
            Opt("visa.badge", True, "Show the number of things to do on the sidebar button", "bool", section="Reminders"),
            Opt("visa.ask_to_log", True, "After opening WhatsApp / e-mail ask whether to log it as a contact", "bool",
                section="Messages"),
            Opt("visa.my_name", "", "Your name (written in the contact log and in messages)", "str", section="Messages"),
            Opt("visa.company", "", "Company name for messages", "str", section="Messages"),
            Opt("visa.date_format", "", "Date format", "choice", choices=visa_io.DATE_FORMATS, section="Display"),
            Opt("visa.visa_types", ["Work visa", "Residence", "Visit", "Other"], "Visa types offered in the form", "list",
                section="Lists"),
            Opt("visa.custom_fields", [], "Extra fields to keep for each person (e.g. Agency, Contract no.)", "list",
                section="Lists", help="They appear in the person form, in {placeholders} and in the exports."),
            Opt("visa.find_ocr_pages", 40, "Read at most this many scanned pages to find the visa page", "int", min=0,
                max=500, section="Visa page finder", help="Pages that already contain text are always read. 0 = all."),
            Opt("visa.data_folder", "", "Folder for the visa data and files (empty = the program's data folder)", "folder",
                section="Data", help="The list, the stored visa/ticket pages and daily backups live here. To use a shared "
                                     "or synced folder, copy the 'visa_tracker' folder there first, then choose it."),
            Opt("visa.backups_keep", 14, "Daily backups to keep", "int", min=1, max=365, section="Data", advanced=True),
            # not shown on a settings page (other prefix): remembered by the tool itself
            Opt("visastate.columns", DEFAULT_COLUMNS, "Visible columns", "list"),
            Opt("visastate.filter", "active", "Last filter", "str"),
            Opt("visastate.templates", visa_io.DEFAULT_TEMPLATES, "Message templates", "list"),
            Opt("visastate.tab", "details", "Last tab", "str"),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.store: V.Store | None = None
        self.store_error = ""
        self.filter = self.settings.get("visastate.filter") or "active"
        self.tab = self.settings.get("visastate.tab") or "details"
        self.shown_id = ""
        self.msg_pid = ""
        self.msg_text: tk.Text | None = None
        self.tpl_var: tk.StringVar | None = None
        self.docs_table: DataTable | None = None
        self.viewer = None
        self._last_alert = 0.0
        self._alert_day = None
        self._detail_sig = None
        self.today = dt.date.today                          # tests replace it
        self.settings.subscribe("visa.", self._setting_changed)
        app.root.after(2500, self._startup_alert)
        app.root.after(300_000, self._tick)

    # ------------------------------------------------------------------ small helpers used by the dialogs too
    def policy(self) -> V.Policy:
        return V.Policy.from_settings(self.settings)

    def fmt(self, d) -> str:
        return visa_io.fmt(d, self.get("date_format"))

    def date_order(self) -> str:
        return "mdy" if self.get("date_format") == "%m/%d/%Y" else "dmy"

    def relative(self, d: dt.date) -> str:
        n = (d - self.today()).days
        return "today" if n == 0 else f"in {n} day{'s' if n != 1 else ''}" if n > 0 else f"{-n} day{'s' if n != -1 else ''} ago"

    def custom_fields(self) -> list[str]:
        return [str(x).strip() for x in (self.get("custom_fields") or []) if str(x).strip()]

    def my_name(self) -> str:
        return (self.get("my_name") or "").strip()

    def templates(self) -> list[dict]:
        raw = self.settings.get("visastate.templates")
        good = [t for t in raw if isinstance(t, dict) and t.get("text")] if isinstance(raw, list) else []
        return good or [dict(t) for t in visa_io.DEFAULT_TEMPLATES]

    def put_templates(self, items: list[dict]) -> None:
        self.settings.set("visastate.templates", items)

    # ------------------------------------------------------------------ storage
    def folder(self) -> Path:
        custom = (self.get("data_folder") or "").strip().strip('"')
        return Path(custom) if custom else data_dir() / "visa_tracker"

    def ensure_store(self) -> V.Store | None:
        if self.store is None and not self.store_error:
            try:
                self.store = V.Store(self.folder(), self.get("backups_keep"))
                self.store.on_change(self._store_changed)
                if self.store.warning:
                    self.log(self.store.warning, "warn")
                    self.app.notify(self.store.warning, "warn")
            except OSError as exc:
                self.store_error = f"The visa data folder cannot be opened ({self.folder()}): {exc}"
                self.log(self.store_error, "error")
        return self.store

    def _setting_changed(self, key: str, _value) -> None:
        if key == "visa.data_folder":
            self.store, self.store_error = None, ""
        if self.built and self.frame is not None and self.frame.winfo_exists():
            self.app.root.after_idle(self._rebuild if key == "visa.data_folder" else self.refresh)
        self._refresh_badge()

    def _rebuild(self) -> None:
        if self.frame is not None:
            for w in self.frame.winfo_children():
                w.destroy()
            self.build(self.frame)

    def _store_changed(self) -> None:
        if self.built and self.frame is not None and self.frame.winfo_exists():
            self.refresh()
        self._refresh_badge()

    # ------------------------------------------------------------------ badge, stats, reminders
    def _counts(self) -> dict:
        st = self.ensure_store()
        return V.counts(st.all(), self.today(), self.policy()) if st else {}

    def badge(self) -> str:
        if not self.settings.get_bool("visa.badge", True):
            return ""
        n = self._counts().get("todo", 0)
        return str(n) if n else ""

    def _refresh_badge(self) -> None:
        fn = getattr(self.app, "refresh_badges", None)
        if fn:
            fn()

    def stats(self) -> dict:
        c = self._counts()
        if not c:
            return {}
        out = {"Waiting": c["active"], "To do today": c["todo"]}
        if c["overdue"] + c["d7"]:
            out["Critical"] = c["overdue"] + c["d7"]
        return out

    def _alert_text(self) -> str:
        st = self.ensure_store()
        if not st:
            return ""
        today, pol = self.today(), self.policy()
        c = V.counts(st.all(), today, pol)
        bits = []
        if c["overdue"]:
            bits.append(f"{c['overdue']} past the deadline")
        if c["d7"]:
            bits.append(f"{c['d7']} critical (≤ {pol.critical} days)")
        if c["todo"]:
            bits.append(f"{c['todo']} to contact today")
        return ("Visa Tracker: " + ", ".join(bits)) if bits else ""

    def _alert(self, force: bool = False) -> None:
        text = self._alert_text()
        if not text:
            return
        self._last_alert = time.time()
        self._alert_day = self.today()
        self.log(text, "warn")
        self.app.notify(text, "warn")

    def _startup_alert(self) -> None:
        if self.settings.get_bool("visa.remind_on_start", True):
            try:
                self._alert()
            except Exception:
                log.exception("Visa reminder failed")
        self._refresh_badge()

    def _tick(self) -> None:
        """Every few minutes: pick up changes made elsewhere, refresh the badge, remind when it is time."""
        try:
            st = self.ensure_store()
            if st:
                st.refresh()
            minutes = self.settings.get_int("visa.remind_every_min", 60)
            new_day = self._alert_day != self.today()
            if minutes > 0 and (new_day or time.time() - self._last_alert >= minutes * 60):
                self._alert()
            if self.built and self.frame is not None and self.frame.winfo_exists():
                self.refresh()
            self._refresh_badge()
        except Exception:
            log.exception("Visa tick failed")
        self.app.root.after(300_000, self._tick)

    # ------------------------------------------------------------------ build
    def build(self, parent: ttk.Frame) -> None:
        st = self.ensure_store()
        if st is None:
            ttk.Label(parent, text="Visa Tracker", style="H2.TLabel").pack(anchor="w")
            ttk.Label(parent, text=self.store_error, style="Err.TLabel", wraplength=700, justify="left").pack(anchor="w", pady=8)
            row = ttk.Frame(parent)
            row.pack(anchor="w")
            ttk.Button(row, text="Try again", command=self._retry_store).pack(side="left")
            ttk.Button(row, text="Open settings", command=lambda: self.app.open_settings(self.id)).pack(side="left", padx=6)
            return
        head = ttk.Frame(parent)
        head.pack(fill="x")
        left = ttk.Frame(head)
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="🛫  Visa Tracker", style="H2.TLabel").pack(anchor="w")
        self.summary = ttk.Label(left, style="Muted.TLabel")
        self.summary.pack(anchor="w")
        ttk.Button(head, text="⋯ More", command=lambda: self._menu_button(more)).pack(side="right")
        ttk.Button(head, text="Export ▾", command=lambda: self._menu_button(export)).pack(side="right", padx=6)
        ttk.Button(head, text="Import…", command=self.import_file).pack(side="right")
        ttk.Button(head, text="＋ Add person  (Ctrl+N)", style="Accent.TButton", command=self.add_person).pack(side="right", padx=6)

        export, more = tk.Menu(parent, tearoff=False), tk.Menu(parent, tearoff=False)
        for label, cmd in (("Excel / CSV list of everybody…", self.export_people),
                           ("Follow-up proof log (Excel)…", self.export_followups),
                           ("Calendar file (.ics) for Outlook / Google…", self.export_calendar),
                           ("Copy a status summary for e-mail", self.copy_summary)):
            export.add_command(label=label, command=cmd)
        for label, cmd in (("Choose columns…", self.columns_menu), ("Message templates…", self.edit_templates),
                           (None, None), ("Back up now", self.backup_now), ("Restore from a backup…", self.restore_backup),
                           ("Open the data folder", lambda: open_path(self.store.folder)), (None, None),
                           ("Settings…", lambda: self.app.open_settings(self.id))):
            if label is None:
                more.add_separator()
            else:
                more.add_command(label=label, command=cmd)
        self._menus = (export, more)

        self.chip_bar = ttk.Frame(parent)
        self.chip_bar.pack(fill="x", pady=(8, 2))
        self.chips: dict[str, ttk.Button] = {}
        for key in CHIPS:
            b = ttk.Button(self.chip_bar, command=lambda k=key: self.set_filter(k))
            b.pack(side="left", padx=1)
            self.chips[key] = b
        self.group_var = tk.StringVar(value=ALL_GROUPS)
        self.group_box = ttk.Combobox(self.chip_bar, textvariable=self.group_var, state="readonly", width=18, values=[ALL_GROUPS])
        self.group_box.pack(side="right")
        self.group_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=(4, 0))
        lf = ttk.Frame(paned)
        paned.add(lf, weight=3)
        self.table = DataTable(lf, COLUMNS, height=14, searchable=True, stretch="todo", on_select=self._selected,
                               on_double=lambda iid: self.edit_person(iid))
        self.table.pack(fill="both", expand=True)
        self.table.tag_colors(overdue="err", critical="err", urgent="warn", soon="info", ok="fg", nodate="muted",
                              arrived="muted", cancelled="muted", missed="muted")
        self.table.tree.bind("<Delete>", lambda e: self.delete_selected())
        self.table.menu.add_separator()
        self.table.menu.add_command(label="Log contact…", command=self.log_contact)
        self.table.menu.add_command(label="Edit…", command=self.edit_person)
        self.table.tree.bind("<Button-3>", self._row_menu)
        self.table.tree.bind("<Button-2>", self._row_menu)
        self._apply_columns()
        right = ttk.Frame(paned, padding=(10, 0, 0, 0))
        paned.add(right, weight=2)
        self.detail = ttk.Frame(right)
        self.detail.pack(fill="both", expand=True)
        DropZone(parent, "Drop a visa / ticket file here (adds it to the selected person)", self.accept_files,
                 theme=self.app.theme, height=44).pack(fill="x", pady=(6, 0))
        self.refresh()

    def _retry_store(self) -> None:
        self.store, self.store_error = None, ""
        self._rebuild()

    def _menu_button(self, menu: tk.Menu) -> None:
        w = self.frame
        try:
            menu.tk_popup(w.winfo_pointerx(), w.winfo_pointery())
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    # ------------------------------------------------------------------ table
    def _apply_columns(self) -> None:
        want = [c for c in (self.settings.get("visastate.columns") or []) if c in {k for k, *_ in COLUMNS}] or DEFAULT_COLUMNS
        self.table.tree.configure(displaycolumns=want)

    def columns_menu(self) -> None:
        win = tk.Toplevel(self.frame)
        win.title("Columns")
        win.transient(self.app.root)
        box = ttk.Frame(win, padding=12)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="Columns shown in the list", style="H3.TLabel").pack(anchor="w", pady=(0, 6))
        current = set(self.settings.get("visastate.columns") or DEFAULT_COLUMNS)
        vars_: dict[str, tk.BooleanVar] = {}

        def apply() -> None:
            chosen = [k for k, *_ in COLUMNS if vars_[k].get()] or ["name"]
            self.settings.set("visastate.columns", chosen)
            self._apply_columns()

        for key, head, *_ in COLUMNS:
            var = tk.BooleanVar(value=key in current)
            vars_[key] = var
            ttk.Checkbutton(box, text=head if key != "lvl" else "● Colour dot", variable=var, command=apply).pack(anchor="w")
        ttk.Button(box, text="Close", command=win.destroy).pack(anchor="e", pady=(8, 0))
        win.bind("<Escape>", lambda e: win.destroy())

    def _row(self, p: V.Person, today: dt.date, pol: V.Policy) -> tuple[dict, str]:
        lvl = V.level_of(p, today, pol)
        n = V.days_left(p, today, pol)
        due, last = V.next_due(p, today, pol), V.last_contact(p)
        if due is None:
            nxt = ""
        elif due < today:
            nxt = f"{(today - due).days} d late"
        else:
            nxt = "today" if due == today else self.fmt(due)
        lc = ""
        if last:
            k = (today - last.date()).days
            lc = "today" if k == 0 else f"{k} day{'s' if k != 1 else ''} ago"
        elif p.status == "active":
            lc = "never"
        row = {"lvl": V.LEVEL_ICON.get(lvl, ""), "name": p.name, "days": "" if (n is None or p.status != "active") else n,
               "deadline": self.fmt(V.deadline_of(p, pol)), "todo": "; ".join(V.todo(p, today, pol)),
               "ticket": visa_io.label_of(V.TICKET_STATES, p.ticket_status), "flight": self.fmt(V.to_date(p.flight_date)),
               "last": lc, "next": nxt, "phone": p.phone, "passport": p.passport_no, "position": p.position,
               "group": p.group, "issued": self.fmt(V.to_date(p.visa_issue)), "status": visa_io.label_of(V.STATUSES, p.status)}
        return row, lvl

    def refresh(self) -> None:
        st = self.ensure_store()
        if st is None or not hasattr(self, "table") or not self.table.winfo_exists():
            return
        today, pol = self.today(), self.policy()
        people = st.all()
        keep = list(self.table.selection())
        groups = [ALL_GROUPS] + st.groups()
        self.group_box.configure(values=groups)
        if self.group_var.get() not in groups:
            self.group_var.set(ALL_GROUPS)
        group = self.group_var.get()
        counts = V.counts(people, today, pol)
        for key, label in CHIPS.items():
            n = counts.get(key, 0)
            self.chips[key].configure(text=f"{label} {n}", style="SideSel.TButton" if key == self.filter else "Flat.TButton")
        shown = [p for p in people if V.matches_filter(p, self.filter, today, pol) and (group == ALL_GROUPS or p.group == group)]
        shown.sort(key=lambda q: V.priority(q, today, pol))
        self.table.clear()
        for p in shown:
            row, lvl = self._row(p, today, pol)
            self.table.add(row, iid=p.id, tags=(lvl,))
        keep = [i for i in keep if self.table.tree.exists(i)]
        if keep:
            self.table.tree.selection_set(keep)
        crit = counts["overdue"] + counts["d7"]
        self.summary.configure(text=(f"{counts['active']} waiting to arrive  ·  {counts['todo']} to contact today  ·  "
                                     f"{crit} critical" if people else "No people yet - add the first one or import your sheet."))
        ids = [i for i in self.table.selection() if st.get(i)]
        self.shown_id = ids[0] if ids else ""
        self._render_detail(force=False)

    def set_filter(self, key: str) -> None:
        self.filter = key
        self.settings.set("visastate.filter", key)
        self.refresh()

    def _ids(self) -> list[str]:
        return [i for i in self.table.selection() if self.store.get(i)] if hasattr(self, "table") else []

    def _people(self) -> list[V.Person]:
        return [self.store.get(i) for i in self._ids()]

    def _selected(self, ids=None) -> None:
        ids = [i for i in (ids if ids is not None else self.table.selection()) if self.store.get(i)]
        pid = ids[0] if ids else ""
        changed = pid != self.shown_id
        if changed:
            self.msg_pid = ""
        self.shown_id = pid
        self._render_detail(force=changed)

    # ------------------------------------------------------------------ detail panel
    def _color(self, lvl: str) -> str:
        p = self.app.theme.p
        return p["err"] if lvl in ("overdue", "critical") else p["warn"] if lvl == "urgent" else p["info"] if lvl == "soon" \
            else p["ok"] if lvl in ("ok", "arrived") else p["muted"]

    def _render_detail(self, force: bool = True) -> None:
        """Draw the panel.  A periodic refresh (force=False) leaves it alone unless the person or the day changed,
        so a message you are editing is never wiped."""
        d = self.detail
        p = self.store.get(self.shown_id) if self.shown_id else None
        sig = (json.dumps(p.to_dict(), sort_keys=True, default=str) if p else "", self.tab, len(self._ids()), str(self.today()),
               self.shown_id)
        if not force and sig == self._detail_sig:
            return
        self._detail_sig = sig
        for w in d.winfo_children():
            w.destroy()
        self.docs_table, self.viewer, self.msg_text = None, None, None
        if p is None:
            ttk.Label(d, text="Select a person to see the details, the follow-up log and the documents.",
                      style="Muted.TLabel", wraplength=360, justify="left").pack(anchor="w", pady=20)
            return
        many = len(self._ids())
        today, pol = self.today(), self.policy()
        lvl, n = V.level_of(p, today, pol), V.days_left(p, today, pol)
        ttk.Label(d, text=p.name, style="H2.TLabel", wraplength=400).pack(anchor="w")
        sub = " · ".join(x for x in (p.position, p.nationality, p.group) if x)
        if many > 1:
            sub = f"{many} people selected - the buttons below act on all of them"
        ttk.Label(d, text=sub, style="Muted.TLabel", wraplength=400).pack(anchor="w")
        if p.status == "active":
            big = "No visa date" if n is None else (f"{V.human_days(n)} left" if n >= 0 else f"Expired {V.human_days(n)}")
        else:
            big = visa_io.label_of(V.STATUSES, p.status) + (f" on {self.fmt(V.to_date(p.arrived_on))}" if p.arrived_on else "")
        big_lbl = ttk.Label(d, text=big, style="Title.TLabel")
        big_lbl.pack(anchor="w", pady=(6, 0))
        big_lbl.configure(foreground=self._color(lvl))
        dl = V.deadline_of(p, pol)
        if dl:
            window = "fixed date" if p.visa_deadline else f"issued {self.fmt(V.to_date(p.visa_issue))} + {p.visa_days or pol.default_days} days"
            ttk.Label(d, text=f"Last entry: {self.fmt(dl)}  ({window})", style="Muted.TLabel", wraplength=400).pack(anchor="w")

        bar = ttk.Frame(d)
        bar.pack(fill="x", pady=(8, 2))
        for text, cmd, tp in (("📞 Spoke", lambda: self.quick_log("call", "reached"), "Log a call where you reached the person"),
                              ("📵 No answer", lambda: self.quick_log("call", "no_answer"), "Log a call nobody answered (retry soon)"),
                              ("💬 Sent", lambda: self.quick_log("whatsapp", "sent"), "Log a WhatsApp message you sent"),
                              ("📝 Log…  Ctrl+L", self.log_contact, "Write a full entry: how, result, note, next reminder")):
            tip(ttk.Button(bar, text=text, command=cmd, style="Tiny.TButton"), tp).pack(side="left", padx=1)
        bar2 = ttk.Frame(d)
        bar2.pack(fill="x", pady=2)
        if p.status == "active":
            ttk.Button(bar2, text="✔ Arrived", style="Tiny.TButton", command=self.mark_arrived).pack(side="left", padx=1)
            ttk.Button(bar2, text="🎫 Ticket sent", style="Tiny.TButton", command=lambda: self.set_ticket("sent")).pack(side="left", padx=1)
            ttk.Button(bar2, text="⏰ Snooze", style="Tiny.TButton", command=lambda: self.snooze_menu(bar2)).pack(side="left", padx=1)
        else:
            ttk.Button(bar2, text="↩ Reopen", style="Tiny.TButton", command=self.reopen).pack(side="left", padx=1)
        ttk.Button(bar2, text="✏ Edit", style="Tiny.TButton", command=self.edit_person).pack(side="left", padx=1)

        if many == 1:
            for issue in V.issues(p, today, pol)[:6]:
                icon, style = {"err": ("⛔", "Err.TLabel"), "warn": ("⚠", "Warn.TLabel")}.get(issue.level, ("ℹ", "Muted.TLabel"))
                ttk.Label(d, text=f"{icon} {issue.text}", style=style, wraplength=400, justify="left").pack(anchor="w", pady=1)
            for t in V.todo(p, today, pol):
                ttk.Label(d, text=f"📌 {t}", style="Info.TLabel", wraplength=400, justify="left").pack(anchor="w", pady=1)

        tabs = ttk.Frame(d)
        tabs.pack(fill="x", pady=(10, 4))
        for key, label in TABS:
            ttk.Button(tabs, text=label, command=lambda k=key: self._set_tab(k),
                       style="SideSel.TButton" if key == self.tab else "Flat.TButton").pack(side="left", padx=1)
        body = ttk.Frame(d)
        body.pack(fill="both", expand=True)
        {"details": self._tab_details, "follow": self._tab_follow, "docs": self._tab_docs}.get(self.tab, self._tab_details)(body, p)

    def _set_tab(self, key: str) -> None:
        self.tab = key
        self.settings.set("visastate.tab", key)
        self._render_detail(force=True)

    def _tab_details(self, body: ttk.Frame, p: V.Person) -> None:
        pol = self.policy()
        pairs = [("Passport", p.passport_no), ("Passport expiry", self.fmt(V.to_date(p.passport_expiry))),
                 ("Phone", p.phone), ("E-mail", p.email), ("Visa no.", p.visa_no), ("Visa type", p.visa_type),
                 ("Visa issued", self.fmt(V.to_date(p.visa_issue))),
                 ("Ticket", visa_io.label_of(V.TICKET_STATES, p.ticket_status)),
                 ("Flight", " ".join(x for x in (self.fmt(V.to_date(p.flight_date)), p.flight_no) if x)),
                 ("Next reminder", self.fmt(V.next_due(p, self.today(), pol))), ("Added", self.fmt(V.to_date(p.added_on)))]
        pairs += [(k, v) for k, v in p.extra.items() if v]
        pairs.append(("Notes", p.notes))
        grid = ttk.Frame(body)
        grid.pack(fill="x")
        r = 0
        for label, value in pairs:
            if not value:
                continue
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(row=r, column=0, sticky="nw", padx=(0, 12), pady=1)
            ttk.Label(grid, text=value, wraplength=300, justify="left").grid(row=r, column=1, sticky="w", pady=1)
            r += 1
        tools = ttk.Frame(body)
        tools.pack(fill="x", pady=8)
        if p.phone:
            ttk.Button(tools, text="Copy phone", style="Tiny.TButton", command=lambda: copy_to_clipboard(self.frame, p.phone)).pack(side="left", padx=1)
        if p.passport_no:
            ttk.Button(tools, text="Copy passport no.", style="Tiny.TButton",
                       command=lambda: copy_to_clipboard(self.frame, p.passport_no)).pack(side="left", padx=1)
        if p.status == "active":
            ttk.Button(tools, text="Cancelled", style="Tiny.TButton", command=lambda: self.close_person("cancelled")).pack(side="right", padx=1)
            ttk.Button(tools, text="Visa missed", style="Tiny.TButton", command=lambda: self.close_person("missed")).pack(side="right", padx=1)

    def _tab_follow(self, body: ttk.Frame, p: V.Person) -> None:
        # message composer
        tpls = self.templates()
        names = [t.get("name", "?") for t in tpls]
        auto = visa_io.pick_template(tpls, p, self.today(), self.policy())
        self.tpl_var = tk.StringVar(value=auto.get("name", names[0]))
        top = ttk.Frame(body)
        top.pack(fill="x")
        ttk.Label(top, text="Message").pack(side="left")
        cb = ttk.Combobox(top, textvariable=self.tpl_var, values=names, state="readonly", width=26)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._fill_message(p))
        ttk.Button(top, text="Edit texts…", style="Tiny.TButton", command=self.edit_templates).pack(side="right")
        self.msg_text = tk.Text(body, height=5, wrap="word")
        style_text(body, self.msg_text)
        self.msg_text.pack(fill="x", pady=4)
        self.msg_pid = p.id
        self._fill_message(p)
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Button(row, text="💬 WhatsApp", command=self.send_whatsapp).pack(side="left", padx=(0, 4))
        ttk.Button(row, text="✉ E-mail", command=self.send_email).pack(side="left", padx=4)
        ttk.Button(row, text="📋 Copy", command=self.copy_message).pack(side="left", padx=4)
        # log
        ttk.Label(body, text="Contact log  (proof that this person was reminded)", style="H3.TLabel").pack(anchor="w", pady=(10, 2))
        table = DataTable(body, [("when", "When", 125), ("channel", "How", 95), ("result", "Result", 115),
                                 ("by", "By", 70), ("note", "Note", 220)], height=6, stretch="note")
        table.pack(fill="both", expand=True)
        for f in sorted(p.followups, key=lambda x: str(x.get("when")), reverse=True):
            try:
                when = dt.datetime.fromisoformat(str(f.get("when"))).strftime("%d-%b-%Y %H:%M")
            except ValueError:
                when = str(f.get("when"))
            channel = "System" if f.get("channel") == "system" else visa_io.label_of(V.CHANNELS, f.get("channel", ""))
            table.add({"when": when, "channel": channel, "result": visa_io.label_of(V.OUTCOMES, f.get("outcome", "")),
                       "by": f.get("by", ""), "note": f.get("note", "")}, iid=str(f.get("id")))
        table.tree.bind("<Delete>", lambda e, t=table, pid=p.id: self._delete_entry(pid, t))
        ttk.Label(body, text="Select an entry and press Delete to remove it (Ctrl+Z brings it back).", style="Muted.TLabel").pack(anchor="w")

    def _delete_entry(self, pid: str, table: DataTable) -> None:
        sel = table.selection()
        if sel and confirm(self.frame, "Delete this log entry?"):
            self.store.delete_followup(pid, sel[0])

    def _tab_docs(self, body: ttk.Frame, p: V.Person) -> None:
        bar = ttk.Frame(body)
        bar.pack(fill="x")
        ttk.Button(bar, text="＋ Visa page from a big file…", style="Accent.TButton", command=lambda: self.attach("visa")).pack(side="left")
        ttk.Button(bar, text="＋ Ticket…", command=lambda: self.attach("ticket")).pack(side="left", padx=4)
        ttk.Button(bar, text="＋ Other…", command=lambda: self.attach("other")).pack(side="left")
        self.docs_table = DataTable(body, [("kind", "Type", 70), ("name", "File", 200), ("src", "Taken from", 150),
                                           ("added", "Added", 100)], height=4, stretch="src", on_select=self._doc_selected,
                                    on_double=lambda iid: self.open_doc())
        self.docs_table.pack(fill="x", pady=6)
        for d in p.docs:
            src = d.get("source", "") + (f" p.{', '.join(map(str, d.get('pages', [])))}" if d.get("pages") else "")
            self.docs_table.add({"kind": visa_io.label_of(V.DOC_KINDS, d.get("kind", "")), "name": d.get("name", ""),
                                 "src": src, "added": str(d.get("added", ""))[:10]}, iid=str(d.get("id")))
        row = ttk.Frame(body)
        row.pack(fill="x")
        for text, cmd in (("Open", self.open_doc), ("Show in folder", self.reveal_doc), ("Remove", self.remove_doc)):
            ttk.Button(row, text=text, style="Tiny.TButton", command=cmd).pack(side="left", padx=1)
        ttk.Label(row, text="Files are kept in the data folder and never deleted.", style="Muted.TLabel").pack(side="right")
        self.viewer = PageViewer(body, self.app)
        self.viewer.pack(fill="both", expand=True, pady=(6, 0))
        if p.docs:
            first = str(p.docs[-1].get("id"))
            self.docs_table.tree.selection_set(first)
            self._doc_selected([first])
        else:
            self.viewer.clear("No documents yet. Use 'Visa page from a big file…' and pick the page - only that page is stored.")

    # ------------------------------------------------------------------ people
    def _defaults(self) -> dict:
        group = self.group_var.get() if hasattr(self, "group_var") else ""
        return {"group": "" if group == ALL_GROUPS else group}

    def add_person(self) -> None:
        if self.ensure_store():
            PersonDialog(self, None, self._person_saved, defaults=self._defaults())

    def edit_person(self, pid: str | None = None) -> None:
        p = self.store.get(pid) if pid else (self._people() or [None])[0]
        if p is not None:
            PersonDialog(self, p, self._person_saved)

    def _person_saved(self, person: V.Person) -> bool:
        try:
            if person.id and self.store.get(person.id):
                self.store.update(person)
            else:
                self.store.add(person)
        except V.VisaError as exc:
            error(self.frame, str(exc))
            return False
        self.log(f"Saved {person.name}", "ok")
        if hasattr(self, "table"):
            if not self.table.tree.exists(person.id):                 # hidden by the filter: show where it went
                self.set_filter("active" if person.status == "active" else "all")
            if self.table.tree.exists(person.id):
                self.table.tree.selection_set(person.id)
                self._selected([person.id])
        return True

    def delete_selected(self) -> None:
        ids = self._ids()
        if ids and confirm(self.frame, f"Delete {len(ids)} person(s) from the list?\n\nTheir stored files stay in the data "
                                       "folder. Ctrl+Z brings them back."):
            self.store.delete(ids)

    # ------------------------------------------------------------------ contacts
    def quick_log(self, channel: str, outcome: str, note: str = "") -> None:
        ids = self._ids()
        if ids:
            self.store.log_followup(ids, channel, outcome, note, self.my_name())
            self.app.notify(f"Logged for {len(ids)} person(s). Ctrl+Z undoes it.", "ok")

    def log_contact(self) -> None:
        ids = self._ids()
        if ids:
            FollowupDialog(self, ids)

    def snooze_menu(self, anchor) -> None:
        m = tk.Menu(self.frame, tearoff=False)
        today = self.today()
        for label, days in (("Tomorrow", 1), ("In 3 days", 3), ("In a week", 7)):
            m.add_command(label=label, command=lambda n=days: self.store.snooze(self._ids(), today + dt.timedelta(days=n)))
        m.add_command(label="Pick a date…", command=self.snooze_custom)
        m.add_command(label="Back to the normal schedule", command=lambda: self.store.snooze(self._ids(), None))
        self._menu_button(m)

    def snooze_custom(self) -> None:
        text = ask_text(self.frame, "Remind me again", "Date (15/11/2026, 15 Nov 2026, or +5 = in five days):")
        if text:
            d = V.parse_date_input(text, self.today(), self.date_order())
            if d is None:
                error(self.frame, f"'{text}' is not a date.")
            else:
                self.store.snooze(self._ids(), d)

    # ------------------------------------------------------------------ status and ticket
    def mark_arrived(self) -> None:
        ids = self._ids()
        if not ids:
            return
        text = ask_text(self.frame, "Arrived", "Arrival date (empty = today; 15/11/2026, or -2 = two days ago):")
        if text is None:
            return
        d = V.parse_date_input(text, self.today(), self.date_order()) if text.strip() else self.today()
        if d is None:
            error(self.frame, f"'{text}' is not a date.")
            return
        self.store.set_status(ids, "arrived", d, by=self.my_name())

    def close_person(self, status: str) -> None:
        ids = self._ids()
        label = dict(V.STATUSES)[status].lower()
        if ids:
            reason = ask_text(self.frame, "Close", f"Mark as {label}. Reason (optional):")
            if reason is not None:
                self.store.set_status(ids, status, note=reason.strip(), by=self.my_name())

    def reopen(self) -> None:
        ids = self._ids()
        if ids:
            self.store.set_status(ids, "active", by=self.my_name())

    def set_ticket(self, state: str) -> None:
        ids = self._ids()
        if not ids:
            return
        label = dict(V.TICKET_STATES)[state]
        by = self.my_name()

        def apply(p: V.Person) -> None:
            p.ticket_status = state
            p.followups.append(V.make_followup("system", "", f"Ticket status: {label}", by))

        self.store.edit_many(ids, apply, label)

    # ------------------------------------------------------------------ messages
    def _template_by_name(self, name: str, p: V.Person) -> dict:
        tpls = self.templates()
        return next((t for t in tpls if t.get("name") == name), None) or visa_io.pick_template(tpls, p, self.today(), self.policy())

    def _render(self, tpl: dict, p: V.Person) -> str:
        return visa_io.render_template(tpl.get("text", ""), p, self.today(), self.policy(), self.get("date_format"),
                                       self.my_name(), (self.get("company") or "").strip())

    def _fill_message(self, p: V.Person) -> None:
        if self.msg_text is None:
            return
        tpl = self._template_by_name(self.tpl_var.get(), p)
        self.msg_text.delete("1.0", "end")
        self.msg_text.insert("1.0", self._render(tpl, p))

    def _message_for(self, p: V.Person) -> tuple[str, str]:
        """(text, template name): what is in the box when it belongs to this person, else the fitting template."""
        if self.msg_text is not None and self.msg_pid == p.id and self.tpl_var is not None:
            return self.msg_text.get("1.0", "end").strip(), self.tpl_var.get()
        tpl = visa_io.pick_template(self.templates(), p, self.today(), self.policy())
        return self._render(tpl, p), tpl.get("name", "")

    def _after_send(self, p: V.Person, channel: str, template: str) -> None:
        note = f"Message sent (template: {template})" if template else "Message sent"
        if not self.settings.get_bool("visa.ask_to_log", True) or confirm(
                self.frame, f"Did you send the message to {p.name}?\n\nYes = write it into the contact log."):
            self.store.log_followup(p.id, channel, "sent", note, self.my_name())
            self.app.notify("Logged. Ctrl+Z undoes it.", "ok")

    def send_whatsapp(self) -> None:
        for p in self._people()[:1]:
            text, tpl = self._message_for(p)
            url = visa_io.whatsapp_url(p.phone, text, str(self.settings.get("match.default_country_code", "") or ""))
            if not url:
                error(self.frame, f"{p.name} has no usable phone number. Add it with the country code, e.g. +964 750 123 4567.")
                return
            webbrowser.open(url)
            self._after_send(p, "whatsapp", tpl)

    def send_email(self) -> None:
        for p in self._people()[:1]:
            text, tpl = self._message_for(p)
            url = visa_io.mailto_url(p.email, "Your visa - please confirm your travel", text)
            if not url:
                error(self.frame, f"{p.name} has no e-mail address.")
                return
            webbrowser.open(url)
            self._after_send(p, "email", tpl)

    def copy_message(self) -> None:
        for p in self._people()[:1]:
            text, tpl = self._message_for(p)
            copy_to_clipboard(self.frame, text)
            self.app.notify("Message copied.", "ok")

    def edit_templates(self) -> None:
        TemplateDialog(self, on_done=lambda: self._render_detail(force=True))

    # ------------------------------------------------------------------ documents
    def attach(self, kind: str) -> None:
        p = (self._people() or [None])[0]
        if p is None:
            info(self.frame, "Select a person first.")
            return
        label = dict(V.DOC_KINDS)[kind].lower()
        path = filedialog.askopenfilename(title=f"Choose the file that contains the {label}",
                                          filetypes=[("PDF, images, Word", "*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.docx"),
                                                     ("All files", "*.*")])
        if path:
            self._attach_path(p, kind, path)

    def _attach_path(self, p: V.Person, kind: str, path: str) -> None:
        if self.app.renderer.kind(path) == "other":
            error(self.frame, f"{Path(path).name} cannot be used here. Use a PDF, an image or a Word file.")
            return
        try:
            pages = self.app.renderer.page_count(path)
        except Exception as exc:
            error(self.frame, f"Could not open {Path(path).name}: {exc}")
            return
        if pages <= 1:
            self._store_doc(p.id, kind, PickResult(str(path), [0]))
        else:
            PagePicker(self, p, kind, str(path), lambda r: self._store_doc(p.id, kind, r))

    def _store_doc(self, pid: str, kind: str, res: PickResult) -> None:
        p = self.store.get(pid)
        if p is None:
            return
        dest = self.store.new_doc_path(p, kind)
        cmd = str(self.settings.get("ocr.libreoffice_cmd", "") or "")

        def work(job):
            return visa_files.extract_pages(res.source, res.pages, dest, res.rotations, cmd)

        def done(path):
            try:
                self.store.add_doc(pid, kind, path, Path(res.source).name, res.pages)
                self._apply_page_facts(pid, kind, res)
            except V.VisaError as exc:
                error(self.frame, str(exc))
                return
            self.app.notify(f"Stored the {dict(V.DOC_KINDS)[kind].lower()} for {p.name}.", "ok")
            self.log(f"Stored {kind} file for {p.name} (page {', '.join(str(n + 1) for n in res.pages)} of {Path(res.source).name})", "ok")

        self.app.jobs.submit(work, "Cutting out the page", tool=self.id, on_done=done,
                             on_error=lambda m: error(self.frame, m), serial_key="visa.cut")

    def _apply_page_facts(self, pid: str, kind: str, res: PickResult) -> None:
        def apply(p: V.Person) -> None:
            if kind == "ticket" and p.ticket_status in ("none", "requested"):
                p.ticket_status = "booked"
            if res.apply_dates:
                if kind == "visa":
                    if res.dates.get("issue"):
                        p.visa_issue = V.iso(res.dates["issue"])
                    if res.dates.get("expiry"):
                        p.visa_deadline = V.iso(res.dates["expiry"])
                elif kind == "ticket" and res.dates.get("flight"):
                    p.flight_date = V.iso(res.dates["flight"])

        p = self.store.get(pid)
        if p and (kind == "ticket" or res.apply_dates):
            self.store.edit_many([pid], apply, "Update from the stored page")

    def _doc(self) -> dict | None:
        p = self.store.get(self.shown_id)
        sel = self.docs_table.selection() if self.docs_table is not None else []
        return next((d for d in (p.docs if p else []) if sel and str(d.get("id")) == sel[0]), None)

    def _doc_selected(self, ids) -> None:
        d = self._doc()
        if d is not None and self.viewer is not None:
            path = self.store.doc_path(d)
            self.viewer.show(path) if path.exists() else self.viewer.clear(f"The file is missing: {path}")

    def open_doc(self) -> None:
        d = self._doc()
        if d is not None and self.store.doc_path(d).exists():
            open_path(self.store.doc_path(d))

    def reveal_doc(self) -> None:
        d = self._doc()
        if d is not None:
            reveal(self.store.doc_path(d))

    def remove_doc(self) -> None:
        d = self._doc()
        if d is not None and confirm(self.frame, "Remove this file from the person?\n\nThe file stays in the data folder."):
            self.store.remove_doc(self.shown_id, str(d.get("id")))

    # ------------------------------------------------------------------ files dropped / chosen
    def accept_files(self, paths: list[str]) -> None:
        if not self.ensure_store() or not paths:
            return
        path = paths[0]
        if Path(path).suffix.lower() in (".xlsx", ".xlsm", ".csv", ".tsv"):
            self._import_path(path)
            return
        p = (self._people() or [None])[0]
        if p is None:
            info(self.frame, "Select a person first, then drop the file again.\n\n(Spreadsheets are imported as a list of people.)")
            return
        KindDialog(self, Path(path).name, lambda kind: self._attach_path(p, kind, path))

    # ------------------------------------------------------------------ import / export
    def import_file(self) -> None:
        path = filedialog.askopenfilename(title="Your list of people (Excel or CSV)",
                                          filetypes=[("Excel / CSV", "*.xlsx *.xlsm *.csv *.tsv"), ("All files", "*.*")])
        if path:
            self._import_path(path)

    def _import_path(self, path: str) -> None:
        ImportDialog(self, path, self._do_import)

    def _do_import(self, people: list[V.Person], mode: str, problems: list[str]) -> None:
        try:
            added, updated, skipped = self.store.merge_people(people, mode, "Import")
        except V.VisaError as exc:
            error(self.frame, str(exc))
            return
        msg = f"Imported: {added} added, {updated} updated, {skipped} skipped."
        self.log(msg, "ok")
        info(self.frame, msg + ("\n\nCtrl+Z undoes the whole import." if added or updated else ""))

    def _save_as(self, title: str, name: str, types: list) -> str:
        return filedialog.asksaveasfilename(title=title, initialfile=name, defaultextension=types[0][1].lstrip("*"),
                                            filetypes=types)

    def export_people(self) -> None:
        today = self.today()
        path = self._save_as("Export the list", f"visa-tracker-{today:%Y-%m-%d}.xlsx",
                             [("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if not path:
            return
        rows = visa_io.people_table(self.store.all(), today, self.policy(), self.get("date_format"), self.custom_fields(), self.store)
        try:
            if path.lower().endswith(".csv"):
                write_csv(path, rows)
            else:
                write_xlsx(path, {"People": rows, "Follow-ups": visa_io.followup_table(self.store.all(), self.get("date_format"))})
        except OSError as exc:
            error(self.frame, f"Could not write the file: {exc}\n\nClose it in Excel and try again.")
            return
        self.app.notify(f"Exported {len(rows) - 1} people.", "ok")

    def export_followups(self) -> None:
        people = self._people() or self.store.all()
        path = self._save_as("Export the follow-up log", f"followups-{self.today():%Y-%m-%d}.xlsx", [("Excel", "*.xlsx")])
        if path:
            try:
                rows = visa_io.followup_table(people, self.get("date_format"))
                write_xlsx(path, {"Follow-ups": rows})
            except OSError as exc:
                error(self.frame, f"Could not write the file: {exc}")
                return
            self.app.notify(f"Exported {len(rows) - 1} log entries.", "ok")

    def export_calendar(self) -> None:
        path = self._save_as("Calendar file", "visa-deadlines.ics", [("Calendar", "*.ics")])
        if path:
            try:
                Path(path).write_text(visa_io.make_ics(self.store.all(), self.today(), self.policy()), encoding="utf-8", newline="")
            except OSError as exc:
                error(self.frame, f"Could not write the file: {exc}")
                return
            self.app.notify("Calendar file written. Open it to add the deadlines to Outlook / Google Calendar.", "ok")

    def copy_summary(self) -> None:
        copy_to_clipboard(self.frame, visa_io.summary_text(self.store.all(), self.today(), self.policy(), self.get("date_format")))
        self.app.notify("Summary copied - paste it into an e-mail.", "ok")

    # ------------------------------------------------------------------ backup / undo
    def backup_now(self) -> None:
        try:
            path = self.store.backup_now()
        except V.VisaError as exc:
            error(self.frame, str(exc))
            return
        info(self.frame, f"Backup saved:\n{path}")

    def restore_backup(self) -> None:
        start = self.store.backup_dir if self.store.backup_dir.exists() else self.store.folder
        path = filedialog.askopenfilename(title="Choose a backup", initialdir=str(start), filetypes=[("Backup", "*.json")])
        if path and confirm(self.frame, "Replace the whole list by this backup?\n\nCtrl+Z can undo it."):
            try:
                n = self.store.restore(path)
            except V.VisaError as exc:
                error(self.frame, str(exc))
                return
            self.app.notify(f"Restored {n} people.", "ok")

    def _typing(self) -> bool:
        try:
            w = self.app.root.focus_get()
            return w is not None and w.winfo_class() in ("TEntry", "Entry", "Text", "TCombobox", "TSpinbox")
        except Exception:
            return False

    def undo(self) -> None:
        if self._typing() or not self.store:
            return
        what = self.store.undo()
        self.app.notify(f"Undone: {what}" if what else "Nothing to undo.", "info")

    def redo(self) -> None:
        if self._typing() or not self.store:
            return
        what = self.store.redo()
        self.app.notify(f"Redone: {what}" if what else "Nothing to redo.", "info")

    def _row_menu(self, event) -> None:
        iid = self.table.tree.identify_row(event.y)
        if iid and iid not in self.table.tree.selection():
            self.table.tree.selection_set(iid)
        m = tk.Menu(self.table.tree, tearoff=False)
        m.add_command(label="Log contact…  (Ctrl+L)", command=self.log_contact)
        m.add_command(label="📞 Spoke to them", command=lambda: self.quick_log("call", "reached"))
        m.add_command(label="📵 No answer", command=lambda: self.quick_log("call", "no_answer"))
        m.add_command(label="💬 WhatsApp…", command=self.send_whatsapp)
        m.add_command(label="✉ E-mail…", command=self.send_email)
        m.add_separator()
        m.add_command(label="⏰ Snooze…", command=lambda: self.snooze_menu(self.table.tree))
        m.add_command(label="🎫 Ticket sent", command=lambda: self.set_ticket("sent"))
        m.add_command(label="🎫 Ticket booked", command=lambda: self.set_ticket("booked"))
        m.add_command(label="✔ Arrived…", command=self.mark_arrived)
        m.add_separator()
        m.add_command(label="Edit…", command=self.edit_person)
        m.add_command(label="Delete…", command=self.delete_selected)
        m.add_separator()
        m.add_command(label="Copy table", command=lambda: self.table.copy_selected(all_rows=True))
        m.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------------ app integration
    def on_show(self) -> None:
        st = self.ensure_store()
        if st is not None and hasattr(self, "table"):
            st.refresh()
            self.refresh()
        self._refresh_badge()

    def keys(self) -> dict:
        return {"<Control-n>": self.add_person, "<Control-l>": self.log_contact, "<F5>": self.on_show,
                "<Control-z>": self.undo, "<Control-y>": self.redo, "<Control-e>": self.export_people,
                "<Control-f>": lambda: self.table.tree.focus_set() if hasattr(self, "table") else None}

    def commands(self) -> list[Command]:
        t = self.title
        out = [Command("visa.add", "Visa Tracker: add a person", self.add_person, t, "new candidate", "Ctrl+N"),
               Command("visa.import", "Visa Tracker: import people from Excel / CSV", self.import_file, t, "spreadsheet"),
               Command("visa.todo", "Visa Tracker: show who to contact today", lambda: (self.app.show_tool(self.id), self.set_filter("todo")), t, "follow up"),
               Command("visa.critical", "Visa Tracker: show critical visas", lambda: (self.app.show_tool(self.id), self.set_filter("d7")), t, "urgent expiring"),
               Command("visa.export", "Visa Tracker: export the list", self.export_people, t, "excel csv", "Ctrl+E"),
               Command("visa.ics", "Visa Tracker: export deadlines to a calendar file", self.export_calendar, t, "outlook google ics"),
               Command("visa.summary", "Visa Tracker: copy a status summary", self.copy_summary, t, "report email"),
               Command("visa.undo", "Visa Tracker: undo the last change", self.undo, t, "", "Ctrl+Z"),
               Command("visa.backup", "Visa Tracker: back up now", self.backup_now, t, "save")]
        return out if self.ensure_store() else []
