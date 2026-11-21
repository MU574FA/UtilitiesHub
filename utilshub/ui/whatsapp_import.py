"""Import from WhatsApp: choose the files of an exported chat ZIP, and take values out of its messages.

The dialog does not know DocPacker.  It talks to a `host` that offers:
    app                           the application (theme, notifications)
    wa_types()                    the document types [DocType]
    wa_supported()                file extensions DocPacker can read
    wa_field_choices()            [(key, label)] of the boxes a message can fill (name, surname, joining, phone, custom)
    wa_current(key)               what is in that box now
    wa_apply_field(key, value)    put a value in the box
    wa_add_field(label)           create a new custom field -> (key, label)
    wa_fill_person(info, replace) name / phone of the person -> list of what was filled
    wa_import(items, note)        items = [(path, type_id | None, several_documents)]
    wa_people_get(key) / wa_people_set(key, sender)   remember who the person is in a chat
"""
from __future__ import annotations

import datetime as dt
import re
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, ttk

from ..core import whatsapp as wa
from ..core.system import open_path
from .autocomplete import attach as autocomplete
from .viewer import PageViewer
from .widgets import DataTable, ask_text, copy_to_clipboard, error, info as info_box

AUTO = "(recognise automatically)"
KIND_LABEL = {"sticker": "sticker", "voice": "voice note", "audio": "audio", "video": "video", "contact": "contact card",
              "other": "other file", "image": "photo", "document": "document"}
SENDER_COLORS = ["#128c7e", "#c2185b", "#6a1b9a", "#e65100", "#1565c0", "#2e7d32", "#ad1457", "#4527a0"]


@dataclass
class Item:
    pkg: wa.WaPackage
    f: wa.WaFile
    importable: bool = False
    use: bool = False                       # marked for DocPacker
    type_id: str | None = None              # None = let DocPacker recognise it
    several: bool = False                   # the file holds several documents (open the split view afterwards)
    suggest: str = ""                       # type suggested by the file name / caption
    added: bool = False
    merged_into: str = ""                   # name of the merged PDF this file was joined into

    @property
    def iid(self) -> str:
        return f"{self.pkg.index}:{self.f.name}"


def fmt_when(w: dt.datetime | None) -> str:
    return w.strftime("%d %b %Y %H:%M") if w else ""


class WhatsAppImport:
    def __init__(self, parent: tk.Misc, host, zips: list[str]) -> None:
        self.host, self.app = host, host.app
        self.parent = parent
        self.pkgs: list[wa.WaPackage] = []
        self.items: dict[str, Item] = {}
        self.person: dict[int, str] = {}            # package index -> chosen participant
        self.types = list(host.wa_types())
        self.supported = {e.lower() for e in host.wa_supported()}
        self.sel: list[str] = []
        self.cur: wa.WaPackage | None = None        # the package whose chat is shown
        self._chip_files: dict[str, tuple[int, str]] = {}
        self._matches: list[str] = []
        self._match_at = -1
        self._search_job = None
        self._added_pkgs: set[int] = set()
        self._build()
        self.add_zips(zips)

    # ================================================================== window
    def _build(self) -> None:
        p = self.app.theme.p
        top = self.top = tk.Toplevel(self.parent)
        top.title("Import from WhatsApp")
        top.geometry("1220x740")
        top.minsize(900, 520)
        try:
            top.configure(background=p["bg"])
            top.app = self.app
        except Exception:
            pass
        top.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(top, padding=10)
        body.pack(fill="both", expand=True)

        # ---- row 1: chat, person, date order, noise
        r1 = ttk.Frame(body)
        r1.pack(fill="x")
        ttk.Button(r1, text="Add another ZIP…", command=self.pick_zip).pack(side="left")
        ttk.Label(r1, text="  Chat").pack(side="left")
        self.chat_var = tk.StringVar()
        self.chat_box = ttk.Combobox(r1, textvariable=self.chat_var, state="readonly", width=26)
        self.chat_box.pack(side="left", padx=(4, 10))
        self.chat_box.bind("<<ComboboxSelected>>", lambda e: self._chat_picked())
        ttk.Label(r1, text="Person").pack(side="left")
        self.person_var = tk.StringVar()
        self.person_box = ttk.Combobox(r1, textvariable=self.person_var, state="readonly", width=30)
        self.person_box.pack(side="left", padx=(4, 10))
        self.person_box.bind("<<ComboboxSelected>>", lambda e: self._person_picked())
        ttk.Label(r1, text="Dates").pack(side="left")
        self.order_var = tk.StringVar(value="Auto")
        order = ttk.Combobox(r1, textvariable=self.order_var, state="readonly", width=18,
                             values=["Auto", "Day / month", "Month / day"])
        order.pack(side="left", padx=(4, 10))
        order.bind("<<ComboboxSelected>>", lambda e: self._order_picked())
        self.show_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(r1, text="Show stickers, voice notes, videos…", variable=self.show_all,
                        command=self.refresh_list).pack(side="left")

        # ---- row 2: the person
        r2 = ttk.Frame(body)
        r2.pack(fill="x", pady=(6, 0))
        self.person_info_var = tk.StringVar()
        ttk.Label(r2, textvariable=self.person_info_var, style="Muted.TLabel").pack(side="left")
        ttk.Button(r2, text="Fill name & phone in DocPacker", style="Tiny.TButton",
                   command=self.fill_person).pack(side="left", padx=10)
        self.order_note_var = tk.StringVar()
        ttk.Label(r2, textvariable=self.order_note_var, style="Muted.TLabel").pack(side="right")

        # ---- main: files | preview + chat
        paned = ttk.PanedWindow(body, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=8)
        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        cols = [("use", "Use", 42, "center"), ("file", "File", 200), ("as", "Use as", 150), ("sug", "Suggested", 110),
                ("from", "From", 120), ("when", "Date", 110), ("note", "Note", 120)]
        self.table = DataTable(left, cols, height=12, on_select=self._selected, on_double=self._open_external)
        self.table.tag_colors(added="ok", dup="muted", noise="muted")
        self.table.pack(fill="both", expand=True)
        self.table.tree.bind("<Button-1>", self._click, add="+")
        self.table.tree.bind("<space>", lambda e: self._toggle_selected())

        ctl = ttk.Frame(left)
        ctl.pack(fill="x", pady=(6, 0))
        ttk.Label(ctl, text="Use selected as").pack(side="left")
        self.type_var = tk.StringVar(value=AUTO)
        self.type_box = ttk.Combobox(ctl, textvariable=self.type_var, state="readonly", width=24,
                                     values=[AUTO] + [t.name for t in self.types])
        self.type_box.pack(side="left", padx=6)
        self.type_box.bind("<<ComboboxSelected>>", lambda e: self._type_picked())
        self.several_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Several documents in this file", variable=self.several_var,
                        command=self._several_toggled).pack(side="left", padx=8)
        ctl2 = ttk.Frame(left)
        ctl2.pack(fill="x", pady=(4, 0))
        ttk.Button(ctl2, text="Mark all documents", style="Tiny.TButton", command=self.mark_all).pack(side="left")
        ttk.Button(ctl2, text="Unmark all", style="Tiny.TButton", command=self.unmark_all).pack(side="left", padx=4)
        ttk.Button(ctl2, text="💡 Use suggestions", style="Tiny.TButton", command=self.use_suggestions).pack(side="left")
        ttk.Button(ctl2, text="🔗 Merge selected into one file", style="Tiny.TButton",
                   command=self.merge_selected).pack(side="left", padx=4)
        ttk.Label(ctl2, text="Click ☐ (or Space) to mark. Ctrl/Shift+click to select several, then merge.", style="Muted.TLabel").pack(side="left", padx=8)

        right = ttk.Frame(paned)
        paned.add(right, weight=4)
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        prev = ttk.Frame(self.tabs, padding=4)
        chat = ttk.Frame(self.tabs, padding=4)
        self.tabs.add(prev, text="Preview")
        self.tabs.add(chat, text="Chat")
        self.prev_tab, self.chat_tab = prev, chat

        self.prev_var = tk.StringVar()
        head = ttk.Frame(prev)
        head.pack(fill="x")
        ttk.Label(head, textvariable=self.prev_var, wraplength=560, justify="left").pack(side="left", fill="x", expand=True)
        ttk.Button(head, text="Show message", style="Tiny.TButton", command=self.show_message).pack(side="right")
        ttk.Button(head, text="Open", style="Tiny.TButton", command=self._open_external).pack(side="right", padx=4)
        self.viewer = PageViewer(prev, self.app)
        self.viewer.pack(fill="both", expand=True, pady=(4, 0))

        self._build_chat(chat)

        bot = ttk.Frame(body)
        bot.pack(fill="x")
        self.status_var = tk.StringVar()
        ttk.Label(bot, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        ttk.Button(bot, text="Close", command=self.close).pack(side="right")
        ttk.Button(bot, text="Add only the selected file", command=lambda: self.add_to_docpacker(True)).pack(side="right", padx=6)
        ttk.Button(bot, text="Add marked files to DocPacker", style="Accent.TButton",
                   command=lambda: self.add_to_docpacker(False)).pack(side="right")

    def _build_chat(self, frame: ttk.Frame) -> None:
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        ttk.Label(bar, text="Search").pack(side="left")
        self.q_var = tk.StringVar()
        q = ttk.Entry(bar, textvariable=self.q_var, width=22)
        q.pack(side="left", padx=4)
        autocomplete(q, "whatsapp.search")
        self.q_var.trace_add("write", lambda *_: self._search_soon())
        q.bind("<Return>", lambda e: self.find_next(1))
        ttk.Button(bar, text="▲", width=3, style="Tiny.TButton", command=lambda: self.find_next(-1)).pack(side="left")
        ttk.Button(bar, text="▼", width=3, style="Tiny.TButton", command=lambda: self.find_next(1)).pack(side="left", padx=2)
        self.hits_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.hits_var, style="Muted.TLabel").pack(side="left", padx=6)
        ttk.Label(bar, text="  From").pack(side="left")
        self.from_var = tk.StringVar(value="Everyone")
        self.from_box = ttk.Combobox(bar, textvariable=self.from_var, state="readonly", width=22, values=["Everyone"])
        self.from_box.pack(side="left", padx=4)
        self.from_box.bind("<<ComboboxSelected>>", lambda e: self.render_chat())
        ttk.Label(frame, text="Right-click a message (select some words first to take only those) → Use for…",
                  style="Muted.TLabel").pack(fill="x", pady=(4, 0))

        wrap = ttk.Frame(frame)
        wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.chat = tk.Text(wrap, wrap="word", state="disabled", cursor="arrow", borderwidth=0, padx=10, pady=8,
                            spacing1=1, spacing3=1, undo=False)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.chat.yview)
        self.chat.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        for seq in ("<Button-3>", "<Button-2>"):
            self.chat.bind(seq, self._chat_menu)
        self._style_chat()
        self.app.theme.on_change(self._style_chat)

    def _style_chat(self) -> None:
        """Colours of the WhatsApp look (they follow the light / dark theme)."""
        t = self.chat
        if not t.winfo_exists():
            return
        dark = bool(getattr(self.app.theme, "dark", False))
        bg, fg = ("#0b141a", "#e9edef") if dark else ("#efeae2", "#111b21")
        them, me = ("#202c33", "#005c4b") if dark else ("#ffffff", "#d9fdd3")
        muted = "#8696a0" if dark else "#667781"
        t.configure(background=bg, foreground=fg, insertbackground=fg, selectbackground="#3b82f6", selectforeground="#ffffff")
        try:
            from tkinter import font as tkfont
            base = tkfont.Font(font=t.cget("font"))
            bold, small = base.copy(), base.copy()
            bold.configure(weight="bold")
            small.configure(size=max(7, int(base.cget("size")) - 1))
            self._fonts = (base, bold, small)                       # keep a reference or Tk drops them
            f_head, f_small = bold, small
        except Exception:
            f_head = f_small = None
        kw = (lambda f: {"font": f} if f is not None else {})
        t.tag_configure("them", background=them, foreground=fg, lmargin1=10, lmargin2=10, rmargin=150)
        t.tag_configure("me", background=me, foreground=fg, lmargin1=150, lmargin2=150, rmargin=10)
        t.tag_configure("head", **kw(f_head))
        t.tag_configure("time", foreground=muted, **kw(f_small))
        t.tag_configure("day", justify="center", foreground=muted, spacing1=12, spacing3=6, **kw(f_small))
        t.tag_configure("sys", justify="center", foreground=muted, spacing1=4, spacing3=4, **kw(f_small))
        t.tag_configure("muted", foreground=muted)
        t.tag_configure("chip", foreground="#53bdeb" if dark else "#027eb5", underline=True)
        t.tag_configure("hit", background="#ffe58f", foreground="#000000")
        t.tag_configure("cur", background="#ff9f1a", foreground="#000000")
        t.tag_configure("flash", background="#fff3b0", foreground="#000000")
        for i, col in enumerate(SENDER_COLORS):
            t.tag_configure(f"who{i}", foreground=col)
        t.tag_raise("hit")
        t.tag_raise("cur")
        t.tag_raise("flash")
        t.tag_raise("sel")
        t.tag_bind("chip", "<Button-1>", self._chip_clicked)
        t.tag_bind("chip", "<Enter>", lambda e: t.configure(cursor="hand2"))
        t.tag_bind("chip", "<Leave>", lambda e: t.configure(cursor="arrow"))

    # ================================================================== packages
    def pick_zip(self) -> None:
        paths = filedialog.askopenfilenames(parent=self.top, title="WhatsApp export (ZIP)",
                                            filetypes=[("ZIP files", "*.zip"), ("All", "*.*")])
        if paths:
            self.add_zips(list(paths))

    def add_zips(self, paths: list[str]) -> None:
        first = not self.pkgs
        for path in paths:
            if any(str(pk.path) == str(path) for pk in self.pkgs):
                continue
            try:
                pkg = wa.WaPackage(path, index=len(self.pkgs))
            except wa.WaError as exc:
                error(self.top, str(exc))
                continue
            except Exception as exc:                                  # a damaged file must not close the dialog
                error(self.top, f"{Path(path).name} could not be read: {exc}")
                continue
            self.pkgs.append(pkg)
            self._load_items(pkg)
            if pkg.chat is not None:
                self.person[pkg.index] = self._pick_person(pkg)
            if len(pkg.files) == 0 and pkg.chat is None:
                info_box(self.top, f"{pkg.path.name} has no files or chat in it.")
        if not self.pkgs:
            return
        self.chat_box.configure(values=[self._pkg_label(pk) for pk in self.pkgs])
        if self.cur is None or len(self.pkgs) > 1 and first:
            self.cur = self.pkgs[0]
        self.chat_var.set(self._pkg_label(self.cur))
        self._after_chat_change()
        self.refresh_list()
        if first:
            self._fill_person_quietly()

    @staticmethod
    def _pkg_label(pkg: wa.WaPackage) -> str:
        return pkg.title or pkg.path.stem

    def _load_items(self, pkg: wa.WaPackage) -> None:
        thr = 0.8
        for f in pkg.files:
            it = Item(pkg, f, importable=f.kind in ("document", "image") and Path(f.name).suffix.lower() in self.supported)
            if it.importable:
                try:
                    it.suggest = wa.suggest_type(self.types, f, thr)
                except Exception:
                    it.suggest = ""
            self.items[it.iid] = it

    def _chat_picked(self) -> None:
        for pk in self.pkgs:
            if self._pkg_label(pk) == self.chat_var.get():
                self.cur = pk
        self._after_chat_change()

    def _after_chat_change(self) -> None:
        pkg = self.cur
        chat = pkg.chat if pkg else None
        parts = chat.participants() if chat else []
        self._person_names = [name for name, _n in parts]
        self.person_box.configure(values=[f"{name}  ({n})" for name, n in parts])
        who = self.person.get(pkg.index, "") if pkg else ""
        self.person_var.set(next((f"{n}  ({c})" for n, c in parts if n == who), ""))
        self.from_box.configure(values=["Everyone"] + self._person_names)
        self.from_var.set("Everyone")
        self.order_note_var.set(self._order_note(chat))
        self._show_person()
        self.render_chat()

    @staticmethod
    def _order_note(chat) -> str:
        if chat is None:
            return "No chat text in this ZIP - only the files."
        what = {"dmy": "day / month", "mdy": "month / day", "ymd": "year first"}[chat.date_order]
        return f"Dates read as {what}" + (" (could be the other way round - change 'Dates')" if chat.ambiguous else "")

    # ================================================================== the person
    def _pick_person(self, pkg: wa.WaPackage) -> str:
        chat = pkg.chat
        names = [n for n, _c in chat.participants()]
        key = wa.chat_key(pkg.title, pkg.path.stem)
        saved = self.host.wa_people_get(key)
        if saved in names:
            return saved
        return wa.guess_person(chat, pkg.title)

    def _person_picked(self) -> None:
        idx = self.person_box.current() if hasattr(self.person_box, "current") else -1
        names = getattr(self, "_person_names", [])
        who = names[idx] if isinstance(idx, int) and 0 <= idx < len(names) else ""
        if not who:
            label = self.person_var.get()
            who = next((n for n in names if label.startswith(n)), "")
        if who and self.cur is not None:
            self._set_person(who, remember=True)

    def _set_person(self, who: str, remember: bool = True) -> None:
        pkg = self.cur
        if pkg is None:
            return
        self.person[pkg.index] = who
        if remember:
            self.host.wa_people_set(wa.chat_key(pkg.title, pkg.path.stem), who)
        parts = pkg.chat.participants() if pkg.chat else []
        self.person_var.set(next((f"{n}  ({c})" for n, c in parts if n == who), who))
        self._show_person()
        self.render_chat()

    def _person_data(self) -> wa.PersonInfo:
        pkg = self.cur
        who = self.person.get(pkg.index, "") if pkg else ""
        return wa.person_info(pkg.chat, who) if pkg and pkg.chat and who else wa.PersonInfo()

    def _show_person(self) -> None:
        pi = self._person_data()
        if not pi.sender:
            self.person_info_var.set("Pick the person the documents are about (Person box).")
            return
        phone = f"{pi.phone}  ({'the number it was sent from' if pi.phone_from == 'sender' else 'found in a message'})" \
            if pi.phone else "not in the chat - right-click a message with it, or type it"
        self.person_info_var.set(f"Name: {pi.name or 'not saved on the phone - type it'}    Phone: {phone}")

    def _fill_person_quietly(self) -> None:
        pi = self._person_data()
        if pi.sender:
            done = self.host.wa_fill_person(pi, replace=False)
            if done:
                self.status_var.set("Filled in DocPacker: " + ", ".join(done) + " (from WhatsApp - please check).")

    def fill_person(self) -> None:
        pi = self._person_data()
        if not pi.sender:
            return
        done = self.host.wa_fill_person(pi, replace=True)
        self.status_var.set("Filled in DocPacker: " + ", ".join(done) if done else "Nothing to fill.")

    def _order_picked(self) -> None:
        order = {"Auto": "auto", "Day / month": "dmy", "Month / day": "mdy"}.get(self.order_var.get(), "auto")
        for pk in self.pkgs:
            try:
                pk.reparse(order)
            except wa.WaError as exc:
                error(self.top, str(exc))
        self._after_chat_change()
        self.refresh_list()

    # ================================================================== the file list
    def _visible(self, it: Item) -> bool:
        return self.show_all.get() or (it.importable and not it.f.noise) or it.use or it.added

    def _row(self, it: Item) -> dict:
        f = it.f
        types = {t.id: t.name for t in self.types}
        as_ = ""
        if it.use:
            as_ = types.get(it.type_id or "", "") or "(recognise)"
            if it.several:
                as_ += "  ▤ several"
        note = "✔ added" if it.added else (f"merged → {it.merged_into}" if it.merged_into else f"duplicate of {f.dup_of}" if f.dup_of else
                                             (KIND_LABEL.get(f.kind, "") if not it.importable else ("small image" if f.small else "")))
        if not it.importable and f.kind in ("document", "image"):
            note = "cannot be read here"
        return {"use": "☑" if it.use else ("☐" if it.importable else "·"), "file": f.name, "as": as_,
                "sug": types.get(it.suggest, "") if it.suggest and not it.use else "",
                "from": f.sender, "when": fmt_when(f.when), "note": note}

    def _tags(self, it: Item) -> tuple:
        return ("added",) if it.added else ("dup",) if it.f.dup_of else ("noise",) if (not it.importable or it.f.noise) else ()

    def refresh_list(self) -> None:
        keep = list(self.sel)
        self.table.clear()
        for it in self.items.values():
            if self._visible(it):
                self.table.add(self._row(it), iid=it.iid, tags=self._tags(it))
        keep = [i for i in keep if self.table.tree.exists(i)]
        if keep:
            self.table.tree.selection_set(*keep)
        self._count()

    def _refresh_row(self, it: Item) -> None:
        if self.table.tree.exists(it.iid):
            self.table.update_row(it.iid, self._row(it), tags=self._tags(it))
        self._count()

    def _count(self) -> None:
        marked = sum(1 for i in self.items.values() if i.use and not i.added)
        docs = sum(1 for i in self.items.values() if i.importable)
        hidden = sum(1 for i in self.items.values() if not self._visible(i))
        self.status_var.set(f"{len(self.items)} files ({docs} documents / photos) • {marked} marked"
                            + (f" • {hidden} hidden" if hidden else ""))

    def _items_of(self, ids: list[str]) -> list[Item]:
        return [self.items[i] for i in ids if i in self.items]

    def _click(self, e):
        tree = self.table.tree
        if tree.identify_region(e.x, e.y) == "cell" and tree.identify_column(e.x) == "#1":
            iid = tree.identify_row(e.y)
            if iid in self.items:
                self._toggle(self.items[iid])
                return "break"
        return None

    def _toggle(self, it: Item) -> None:
        if not it.importable:
            self.status_var.set(f"{it.f.name} cannot be added to DocPacker (not a document or photo it can read).")
            return
        it.use = not it.use
        self._refresh_row(it)

    def _toggle_selected(self) -> None:
        for it in self._items_of(self.sel):
            self._toggle(it)

    def mark_all(self) -> None:
        for it in self.items.values():
            if it.importable and not it.f.noise and not it.f.dup_of and not it.added:
                it.use = True
        self.refresh_list()

    def unmark_all(self) -> None:
        for it in self.items.values():
            it.use = False
        self.refresh_list()

    def use_suggestions(self) -> None:
        targets = self._items_of(self.sel) or [i for i in self.items.values() if i.importable]
        n = 0
        for it in targets:
            if it.suggest and it.importable:
                it.use, it.type_id = True, it.suggest
                self._refresh_row(it)
                n += 1
        self.status_var.set(f"{n} file(s) set from the suggestions - check the 'Use as' column." if n
                            else "Nothing to suggest (the file names and captions did not say what the files are).")

    def _type_picked(self) -> None:
        name = self.type_var.get()
        tid = next((t.id for t in self.types if t.name == name), None)
        for it in self._items_of(self.sel):
            if it.importable:
                it.use, it.type_id = True, tid
                self._refresh_row(it)

    def merge_selected(self) -> None:
        """Join the selected photos / documents into ONE PDF (in list order) that then behaves like any other file."""
        chosen = [it for it in self._items_of(self.sel) if it.importable and not it.added]
        if len(chosen) < 2:
            self.status_var.set("Select two or more documents or photos (Ctrl/Shift+click), then merge them.")
            return
        if len({it.pkg.index for it in chosen}) > 1:
            self.status_var.set("Only files of the same chat can be merged.")
            return
        rank = {iid: i for i, iid in enumerate(self.table.tree.get_children())}
        chosen.sort(key=lambda it: rank.get(it.iid, 10 ** 6))            # the order you see is the page order
        types = {t.id: t.name for t in self.types}
        tids = {it.type_id for it in chosen if it.use and it.type_id}
        tid = tids.pop() if len(tids) == 1 else None
        name = ask_text(self.top, "Merge files", f"Name of the merged PDF ({len(chosen)} files, in the order of the list):",
                        types.get(tid or "", "") or "Merged documents")
        if name is None:
            return
        self.status_var.set("Merging…")
        self.top.update_idletasks()
        pkg = chosen[0].pkg
        try:
            merged, skipped = pkg.merge([it.f.name for it in chosen], name)
        except wa.WaError as exc:
            self.status_var.set("")
            error(self.top, str(exc))
            return
        except Exception as exc:
            self.status_var.set("")
            error(self.top, f"The files could not be merged: {exc}")
            return
        new = Item(pkg, merged, importable=True, use=True, type_id=tid)
        self.items[new.iid] = new
        for it in chosen:
            it.use, it.merged_into = False, merged.name
        self.refresh_list()
        if self.table.tree.exists(new.iid):
            self.table.tree.selection_set(new.iid)
            self.table.tree.see(new.iid)
        self.status_var.set(f"{len(chosen) - len(skipped)} files merged into {merged.name} - it is marked; "
                            "the originals are not added.")
        if skipped:
            info_box(self.top, "These files could not be merged and were left out:\n" + "\n".join(skipped[:6]))

    def _several_toggled(self) -> None:
        for it in self._items_of(self.sel):
            if it.importable:
                it.several = bool(self.several_var.get())
                if it.several:
                    it.use = True
                self._refresh_row(it)

    # ================================================================== preview
    def _selected(self, ids: list[str]) -> None:
        self.sel = list(ids)
        its = self._items_of(ids)
        if not its:
            return
        it = its[0]
        types = {t.id: t.name for t in self.types}
        self.type_var.set(types.get(it.type_id or "", AUTO) if it.use else AUTO)
        self.several_var.set(it.several)
        self._preview(it)

    def _preview(self, it: Item) -> None:
        f = it.f
        bits = [f.name, f"{f.size // 1024} KB" if f.size >= 1024 else f"{f.size} B"]
        if f.sender:
            bits.append(f"from {f.sender}")
        if f.when:
            bits.append(fmt_when(f.when))
        text = "  •  ".join(bits) + (f"\n“{f.caption}”" if f.caption else "")
        self.prev_var.set(text)
        if not it.importable:
            self.viewer.clear()
            self.prev_var.set(text + f"\nNo preview: this is a {KIND_LABEL.get(f.kind, 'file')}.")
            return
        try:
            path = it.pkg.extract(f.name)
        except wa.WaError as exc:
            self.viewer.clear()
            self.prev_var.set(text + f"\n{exc}")
            return
        self.viewer.show(str(path))

    def _open_external(self, *_a) -> None:
        its = self._items_of(self.sel)
        if its:
            try:
                open_path(str(its[0].pkg.extract(its[0].f.name)))
            except wa.WaError as exc:
                error(self.top, str(exc))

    def show_message(self) -> None:
        its = self._items_of(self.sel)
        if its and its[0].f.message is not None:
            self._goto_message(its[0].pkg, its[0].f.message)
        else:
            self.status_var.set("This file is not linked to a message (the chat text does not mention it).")

    # ================================================================== the chat
    def _who_tag(self, sender: str) -> str:
        return f"who{sum(map(ord, sender)) % len(SENDER_COLORS)}"

    @staticmethod
    def _message_text(m: wa.Message) -> str:
        return (m.caption if m.files else m.text).strip()

    def render_chat(self) -> None:
        t = self.chat
        pkg = self.cur
        t.configure(state="normal")
        t.delete("1.0", "end")
        self._chip_files.clear()
        self._matches, self._match_at = [], -1
        chat = pkg.chat if pkg else None
        if chat is None:
            t.insert("end", "\nThis ZIP has no chat text (the export may have been made without messages).", ("sys",))
            t.configure(state="disabled")
            return
        person = self.person.get(pkg.index, "")
        only = self.from_var.get()
        last_day = None
        k = 0
        for m in chat.messages:
            if only != "Everyone" and m.sender != only:
                continue
            if m.when and m.when.date() != last_day:
                last_day = m.when.date()
                t.insert("end", f"{last_day.strftime('%A, %d %B %Y')}\n", ("day",))
            start = t.index("end-1c")
            if m.system:
                t.insert("end", m.text.strip() + "\n", ("sys",))
            else:
                side = "me" if (person and m.sender != person) else "them"
                t.insert("end", m.sender, (side, "head", self._who_tag(m.sender)))
                t.insert("end", "   " + (m.when.strftime("%H:%M") if m.when else "") + "\n", (side, "time"))
                for name in m.files:
                    k += 1
                    tag = f"fx{k}"
                    self._chip_files[tag] = (pkg.index, name)
                    t.insert("end", f"📎 {name}", (side, "chip", tag))
                    t.insert("end", "\n", (side,))
                body = self._message_text(m)
                if m.omitted and not m.files:
                    t.insert("end", "🚫 media not included in the export\n", (side, "muted"))
                elif body:
                    t.insert("end", body + "\n", (side,))
            t.tag_add(f"m{m.n}", start, "end-1c")
            t.insert("end", "\n")
        t.configure(state="disabled")
        if self.q_var.get().strip():
            self._search_now()

    def _msg_at(self, x: int, y: int) -> int | None:
        for tag in self.chat.tag_names(self.chat.index(f"@{x},{y}")):
            if re.fullmatch(r"m\d+", tag):
                return int(tag[1:])
        return None

    def _chip_clicked(self, e) -> None:
        for tag in self.chat.tag_names(self.chat.index(f"@{e.x},{e.y}")):
            if tag in self._chip_files:
                self.goto_file(*self._chip_files[tag])
                return

    def goto_file(self, pkg_index: int, name: str) -> None:
        it = self.items.get(f"{pkg_index}:{name}")
        if it is None:
            return
        if not self._visible(it):
            self.show_all.set(True)
            self.refresh_list()
        self.table.tree.selection_set(it.iid)
        try:
            self.table.tree.see(it.iid)
        except Exception:
            pass
        self.sel = [it.iid]
        self._preview(it)
        self.tabs.select(self.prev_tab)

    def _goto_message(self, pkg: wa.WaPackage, n: int) -> None:
        self.cur = pkg
        self.chat_var.set(self._pkg_label(pkg))
        if self.from_var.get() != "Everyone":
            self.from_var.set("Everyone")
        self.render_chat()
        self.tabs.select(self.chat_tab)
        rng = self.chat.tag_ranges(f"m{n}")
        if rng:
            self.chat.see(rng[0])
            self.chat.tag_add("flash", rng[0], rng[1])
            self.top.after(2200, lambda: self.chat.tag_remove("flash", "1.0", "end"))

    # ---- search
    def _search_soon(self) -> None:
        if self._search_job is not None:
            self.top.after_cancel(self._search_job)
        self._search_job = self.top.after(250, self._search_now)

    def _search_now(self) -> None:
        t = self.chat
        t.tag_remove("hit", "1.0", "end")
        t.tag_remove("cur", "1.0", "end")
        self._matches, self._match_at = [], -1
        q = self.q_var.get().strip()
        if not q:
            self.hits_var.set("")
            return
        pos = "1.0"
        while True:
            pos = t.search(q, pos, stopindex="end", nocase=True)
            if not pos:
                break
            end = f"{pos}+{len(q)}c"
            t.tag_add("hit", pos, end)
            self._matches.append(pos)
            pos = end
        self.hits_var.set(f"{len(self._matches)} found" if self._matches else "not found")
        if self._matches:
            self.find_next(1)

    def find_next(self, step: int) -> None:
        if not self._matches:
            return
        t = self.chat
        t.tag_remove("cur", "1.0", "end")
        self._match_at = (self._match_at + step) % len(self._matches)
        pos = self._matches[self._match_at]
        t.tag_add("cur", pos, f"{pos}+{len(self.q_var.get().strip())}c")
        t.see(pos)
        self.hits_var.set(f"{self._match_at + 1} of {len(self._matches)}")

    # ---- message -> field
    def _chat_menu(self, e) -> None:
        n = self._msg_at(e.x, e.y)
        pkg = self.cur
        if n is None or pkg is None or pkg.chat is None:
            return
        msg = pkg.chat.messages[n]
        sel = ""
        try:
            if self.chat.tag_ranges("sel"):
                sel = self.chat.get("sel.first", "sel.last").strip()
        except tk.TclError:
            sel = ""
        text = sel or self._message_text(msg) or msg.text.strip()
        if not text:
            return
        short = text if len(text) <= 44 else text[:41] + "…"
        menu = tk.Menu(self.chat, tearoff=False)
        menu.add_command(label=f"{'Selected words' if sel else 'Whole message'}: “{short.replace(chr(10), ' ')}”", state="disabled")
        menu.add_separator()
        for key, label in self.host.wa_field_choices():
            menu.add_command(label=f"Use for  {label}", command=lambda k=key, l=label: self.use_for(k, l, text))
        menu.add_command(label="➕ Use for a new field…", command=lambda: self._new_field(text))
        menu.add_separator()
        menu.add_command(label="Copy", command=lambda: copy_to_clipboard(self.chat, text))
        if msg.sender:
            menu.add_command(label=f"{msg.sender} is the person", command=lambda: self._set_person(msg.sender))
        for name in msg.files:
            menu.add_command(label=f"Show file  {name}", command=lambda nm=name: self.goto_file(pkg.index, nm))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def use_for(self, key: str, label: str, text: str) -> None:
        """Put a message (or the selected words) into a box of DocPacker, after you have seen / corrected the value."""
        value = re.sub(r"\s+", " ", text).strip()
        note = ""
        if key == "joining":
            d = wa.date_from_text(value)
            if d is None:
                error(self.top, f"No date found in:\n{value[:120]}\n\nSelect just the date words and try again.")
                return
            note, value = f"Understood as {d.isoformat()} (from: {value[:50]})", d.isoformat()
        elif key == "phone":
            found = wa.find_phone([value]) or (wa.clean_phone(value) if wa.looks_like_phone(value) else "")
            if found:
                value = found
        cur = (self.host.wa_current(key) or "").strip()
        prompt = f"Put this into “{label}”" + (f"\n{note}" if note else "") + \
                 (f"\n\nIt will replace: {cur}" if cur and cur != value else "")
        out = ask_text(self.top, f"Use for {label}", prompt, value)
        if out is None:
            return
        out = out.strip()
        if key == "joining":
            d = wa.date_from_text(out)
            if d is None:
                error(self.top, "That is not a date I can read. Use YYYY-MM-DD.")
                return
            out = d.isoformat()
        self.host.wa_apply_field(key, out)
        self.status_var.set(f"{label} = {out}")

    def _new_field(self, text: str) -> None:
        label = ask_text(self.top, "New field", "Name of the new field (for example Position):", "")
        if not label or not label.strip():
            return
        made = self.host.wa_add_field(label.strip())
        if made:
            self.use_for(made[0], made[1], text)

    # ================================================================== hand-off
    def add_to_docpacker(self, only_selected: bool) -> None:
        if only_selected:
            chosen = [it for it in self._items_of(self.sel) if it.importable]
            if not chosen:
                self.status_var.set("Select a document or photo first.")
                return
        else:
            chosen = [it for it in self.items.values() if it.use and it.importable and not it.added]
            if not chosen:
                self.status_var.set("Nothing is marked. Click ☐ next to the files you need, or use 'Add only the selected file'.")
                return
        out, failed = [], []
        for it in chosen:
            try:
                out.append((str(it.pkg.extract(it.f.name)), it.type_id if it.use else None, it.several, it))
            except wa.WaError as exc:
                failed.append(str(exc))
        if out:
            who = self._person_data()
            note = f"WhatsApp {self._pkg_label(chosen[0].pkg)}" + (f" ({who.name or who.phone})" if who.sender else "")
            self.host.wa_import([(p, t, s, self._source(it)) for p, t, s, it in out], note)
            for _p, _t, _s, it in out:
                it.added = True
                it.use = False
                self._added_pkgs.add(it.pkg.index)
                self._refresh_row(it)
            self.status_var.set(f"{len(out)} file(s) added to DocPacker.")
        if failed:
            error(self.top, "\n".join(failed[:5]))

    @staticmethod
    def _source(it: Item) -> str:
        f = it.f
        bits = [b for b in (f.sender, fmt_when(f.when)) if b]
        return ("from " + ", ".join(bits)) if bits else ""

    def close(self) -> None:
        keep = []
        for pk in self.pkgs:
            if pk.index in self._added_pkgs and pk.workdir is not None:
                keep.append(str(pk.workdir))                       # DocPacker is still using these files
            else:
                pk.cleanup()
        self.host.wa_closed(keep)
        try:
            self.top.destroy()
        except Exception:
            pass

    def lift(self) -> None:
        try:
            self.top.deiconify()
            self.top.lift()
            self.top.focus_force()
        except Exception:
            pass
