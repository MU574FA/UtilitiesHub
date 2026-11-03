"""RecordEditor: one table+form editor reused for rules, fields, checks, document types, clipboard formats...

A record is a plain dict.  Describe the columns and the form properties and you get:
add / edit / duplicate / remove / reorder / enable-toggle / import-export / "add from library" for free.
"""
from __future__ import annotations

import copy
import json
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import filedialog, ttk
from typing import Any, Callable

from ..core.rules import (KIND_LABELS, KINDS, LEVEL_HELP, LEVEL_LABELS, LEVELS, MATCH_MODES, REGEX_SNIPPETS,
                          RULE_LIBRARY, SCOPES, BUILTIN_DETECTORS, Rule, evaluate_rule)
from .widgets import (ScrollFrame, Tooltip, ask_text, confirm, copy_to_clipboard, error, info, modal, style_text)


@dataclass
class Prop:
    key: str
    label: str
    kind: str = "str"                 # str text bool int float choice level color
    choices: list = field(default_factory=list)   # values or (value, label)
    help: str = ""
    width: int = 40
    visible_if: Callable[[dict], bool] | None = None
    rows: int = 4                     # text kind


@dataclass
class Col:
    key: str
    heading: str
    width: int = 120
    fmt: Callable[[dict], str] | None = None


def pairs(choices: list) -> list[tuple[Any, str]]:
    return [(c[0], str(c[1])) if isinstance(c, (tuple, list)) else (c, str(c)) for c in choices]


# --------------------------------------------------------------------------- the editor
class RecordEditor(ttk.Frame):
    def __init__(self, parent: tk.Misc, app, columns: list[Col], props: list[Prop], new_record: Callable[[], dict],
                 value: list[dict] | None = None, on_change: Callable[[list[dict]], None] | None = None,
                 toggle_key: str | None = "enabled", library: list[tuple[str, str, dict]] | None = None,
                 dialog_extra: Callable | None = None, validate: Callable[[dict], str] | None = None,
                 noun: str = "item", height: int = 8, dialog_width: int = 560, help_text: str = "",
                 key_name: str | None = None) -> None:
        super().__init__(parent)
        self.app, self.columns, self.props = app, columns, props
        self.new_record, self.on_change = new_record, on_change
        self.toggle_key, self.library = toggle_key, library
        self.dialog_extra, self.validate = dialog_extra, validate
        self.noun, self.dialog_width = noun, dialog_width
        self.records: list[dict] = copy.deepcopy(value or [])
        if help_text:
            ttk.Label(self, text=help_text, style="Muted.TLabel", wraplength=640, justify="left").pack(anchor="w", pady=(0, 4))
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Button(bar, text=f"＋ Add {noun}", command=self.add, style="Accent.TButton").pack(side="left")
        if library:
            mb = ttk.Menubutton(bar, text="From library ▾")
            menu = tk.Menu(mb, tearoff=False)
            groups: dict[str, tk.Menu] = {}
            for group, label, rec in library:
                if group not in groups:
                    groups[group] = tk.Menu(menu, tearoff=False)
                    menu.add_cascade(label=group, menu=groups[group])
                groups[group].add_command(label=label, command=lambda r=rec: self.add_record(copy.deepcopy(r)))
            mb["menu"] = menu
            mb.pack(side="left", padx=4)
        for text, cmd in (("Edit", self.edit), ("Duplicate", self.duplicate), ("Remove", self.remove)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Button(bar, text="▲", width=3, command=lambda: self.move(-1)).pack(side="left", padx=(8, 1))
        ttk.Button(bar, text="▼", width=3, command=lambda: self.move(1)).pack(side="left")
        more = ttk.Menubutton(bar, text="⋯")
        mm = tk.Menu(more, tearoff=False)
        mm.add_command(label="Export to file…", command=self.export_json)
        mm.add_command(label="Import from file…", command=self.import_json)
        mm.add_command(label="Copy as JSON", command=lambda: copy_to_clipboard(self, json.dumps(self.records, indent=2, ensure_ascii=False)))
        mm.add_separator()
        mm.add_command(label="Enable all", command=lambda: self.set_all(True))
        mm.add_command(label="Disable all", command=lambda: self.set_all(False))
        more["menu"] = mm
        more.pack(side="right")
        cols = (["_on"] if toggle_key else []) + [c.key for c in columns]
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=height, selectmode="extended")
        if toggle_key:
            self.tree.heading("_on", text="On")
            self.tree.column("_on", width=38, anchor="center", stretch=False)
        for i, c in enumerate(columns):
            self.tree.heading(c.key, text=c.heading)
            self.tree.column(c.key, width=c.width, stretch=(i == len(columns) - 1))
        ys = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self._double)
        self.tree.bind("<Return>", lambda e: self.edit())
        self.tree.bind("<Delete>", lambda e: self.remove())
        self.tree.bind("<space>", lambda e: self.toggle_selected())
        self.tree.tag_configure("off", foreground=app.theme.p["muted"])
        app.theme.on_change(lambda: self.tree.winfo_exists() and self.tree.tag_configure("off", foreground=app.theme.p["muted"]))
        self.refresh()

    # -- data
    def get(self) -> list[dict]:
        return copy.deepcopy(self.records)

    def set(self, records: list[dict]) -> None:
        self.records = copy.deepcopy(records or [])
        self.refresh()

    def _changed(self) -> None:
        self.refresh()
        if self.on_change:
            self.on_change(self.get())

    def refresh(self) -> None:
        sel = [self.tree.index(i) for i in self.tree.selection()]
        self.tree.delete(*self.tree.get_children())
        for n, rec in enumerate(self.records):
            vals = ["✔" if rec.get(self.toggle_key, True) else "—"] if self.toggle_key else []
            for c in self.columns:
                v = c.fmt(rec) if c.fmt else rec.get(c.key, "")
                vals.append(str(v).replace("\n", " ⏎ ")[:140])
            tags = ("off",) if self.toggle_key and not rec.get(self.toggle_key, True) else ()
            self.tree.insert("", "end", iid=str(n), values=vals, tags=tags)
        for i in sel:
            if i < len(self.records):
                self.tree.selection_add(str(i))

    def _sel(self) -> list[int]:
        return sorted(int(i) for i in self.tree.selection())

    # -- actions
    def add(self) -> None:
        rec = self.new_record()
        out = self._dialog(rec, new=True)
        if out is not None:
            self.add_record(out)

    def add_record(self, rec: dict) -> None:
        self.records.append(rec)
        self._changed()
        self.tree.selection_set(str(len(self.records) - 1))
        self.tree.see(str(len(self.records) - 1))

    def edit(self) -> None:
        sel = self._sel()
        if not sel:
            return
        out = self._dialog(copy.deepcopy(self.records[sel[0]]))
        if out is not None:
            self.records[sel[0]] = out
            self._changed()

    def duplicate(self) -> None:
        for i in reversed(self._sel()):
            self.records.insert(i + 1, copy.deepcopy(self.records[i]))
        if self._sel():
            self._changed()

    def remove(self) -> None:
        sel = self._sel()
        if sel and confirm(self, f"Remove {len(sel)} {self.noun}{'s' if len(sel) > 1 else ''}?"):
            for i in reversed(sel):
                del self.records[i]
            self._changed()

    def move(self, step: int) -> None:
        sel = self._sel()
        if not sel:
            return
        order = sel if step < 0 else sel[::-1]
        for i in order:
            j = i + step
            if 0 <= j < len(self.records) and j not in sel:
                self.records[i], self.records[j] = self.records[j], self.records[i]
        self._changed()
        self.tree.selection_set([str(i + step) for i in sel if 0 <= i + step < len(self.records)])

    def toggle_selected(self) -> str:
        if self.toggle_key:
            for i in self._sel():
                self.records[i][self.toggle_key] = not self.records[i].get(self.toggle_key, True)
            self._changed()
        return "break"

    def set_all(self, on: bool) -> None:
        if self.toggle_key:
            for r in self.records:
                r[self.toggle_key] = on
            self._changed()

    def _double(self, e) -> None:
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        if self.toggle_key and self.tree.identify_column(e.x) == "#1":
            self.toggle_selected()
        else:
            self.edit()

    def export_json(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")], parent=self.winfo_toplevel())
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.records, fh, indent=2, ensure_ascii=False)

    def import_json(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")], parent=self.winfo_toplevel())
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, list):
                raise ValueError("expected a list")
        except (OSError, ValueError) as exc:
            error(self, f"Could not import: {exc}")
            return
        self.records.extend(data)
        self._changed()

    # -- form dialog
    def _dialog(self, rec: dict, new: bool = False) -> dict | None:
        top, frame = modal(self, f"{'New' if new else 'Edit'} {self.noun}", self.dialog_width)
        top.resizable(True, True)
        vars_: dict[str, tk.Variable] = {}
        rows: dict[str, tuple] = {}
        texts: dict[str, tk.Text] = {}
        form = ttk.Frame(frame)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        out: dict[str, Any] = {"rec": None}
        working = copy.deepcopy(rec)

        def collect() -> dict:
            d = dict(working)
            for p in self.props:
                if p.kind == "text":
                    d[p.key] = texts[p.key].get("1.0", "end").rstrip("\n")
                elif p.key in vars_:
                    v = vars_[p.key].get()
                    if p.kind == "int":
                        try:
                            v = int(float(v))
                        except (TypeError, ValueError):
                            v = 0
                    elif p.kind == "float":
                        try:
                            v = float(v)
                        except (TypeError, ValueError):
                            v = 0.0
                    elif p.kind == "list":
                        from ..core.textutil import split_list
                        v = split_list(v)
                    elif p.kind in ("choice", "level"):
                        labels = {lbl: val for val, lbl in pairs(self._choices(p))}
                        v = labels.get(v, v)
                    d[p.key] = v
            return d

        def update_visibility(*_a) -> None:
            cur = collect()
            for p in self.props:
                label, widget = rows[p.key]
                show = p.visible_if is None or p.visible_if(cur)
                for w in (label, widget):
                    (w.grid if show else w.grid_remove)()

        row = 0
        for p in self.props:
            value = working.get(p.key, "")
            label = ttk.Label(form, text=p.label)
            if p.kind == "bool":
                var = tk.BooleanVar(value=bool(value))
                widget = ttk.Checkbutton(form, variable=var)
            elif p.kind in ("choice", "level"):
                ch = pairs(self._choices(p))
                cur = next((lbl for val, lbl in ch if val == value), str(value))
                var = tk.StringVar(value=cur)
                widget = ttk.Combobox(form, textvariable=var, values=[lbl for _, lbl in ch], state="readonly", width=p.width)
            elif p.kind == "text":
                var = None
                widget = tk.Text(form, height=p.rows, width=p.width, wrap="word", undo=True)
                style_text(self, widget)
                widget.insert("1.0", str(value))
                texts[p.key] = widget
            elif p.kind == "list":
                var = tk.StringVar(value=", ".join(value) if isinstance(value, list) else str(value))
                widget = ttk.Entry(form, textvariable=var, width=p.width)
            elif p.kind == "color":
                var = tk.StringVar(value=str(value))
                widget = ttk.Entry(form, textvariable=var, width=12)
            else:
                var = tk.StringVar(value=str(value) if value is not None else "")
                widget = ttk.Entry(form, textvariable=var, width=p.width)
            if var is not None:
                vars_[p.key] = var
                var.trace_add("write", update_visibility)
            label.grid(row=row, column=0, sticky="nw" if p.kind == "text" else "w", padx=(0, 10), pady=3)
            widget.grid(row=row, column=1, sticky="ew", pady=3)
            if p.help:
                Tooltip(widget, p.help)
                Tooltip(label, p.help)
            rows[p.key] = (label, widget)
            row += 1
        extra_get: dict[str, Callable] = {}
        if self.dialog_extra:
            holder = ttk.Frame(frame)
            holder.pack(fill="both", expand=True, pady=(8, 0))
            self.dialog_extra(holder, working, collect, extra_get, vars_)
        update_visibility()
        msg = ttk.Label(frame, style="Err.TLabel", wraplength=self.dialog_width - 40)
        msg.pack(anchor="w", pady=(6, 0))

        def ok(_e=None) -> None:
            d = collect()
            for key, getter in extra_get.items():
                d[key] = getter()
            problem = self.validate(d) if self.validate else ""
            if problem:
                msg.configure(text=problem)
                return
            out["rec"] = d
            top.destroy()

        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right", padx=6)
        top.bind("<Escape>", lambda e: top.destroy())
        first = next((w for _, w in rows.values() if isinstance(w, ttk.Entry)), None)
        if first:
            first.focus_set()
        top.update_idletasks()
        top.grab_set()
        top.wait_window()
        return out["rec"]

    @staticmethod
    def _choices(p: Prop) -> list:
        if p.kind == "level":
            return [(lv, LEVEL_LABELS[lv]) for lv in LEVELS]
        return p.choices


# --------------------------------------------------------------------------- rule tester
class RuleTester(ttk.LabelFrame):
    """Paste text, see live which parts a rule finds (highlighted) and what values it returns."""

    def __init__(self, parent: tk.Misc, app, get_rule: Callable[[], Rule], height: int = 6) -> None:
        super().__init__(parent, text="Try it", padding=6)
        self.app, self.get_rule = app, get_rule
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Paste sample text:", style="Muted.TLabel").pack(side="left")
        if getattr(app, "last_text", ""):
            ttk.Button(top, text="Use last scanned text", style="Tiny.TButton", command=self._use_last).pack(side="right")
        ttk.Button(top, text="Paste", style="Tiny.TButton", command=self._paste).pack(side="right", padx=4)
        self.text = tk.Text(self, height=height, wrap="word", undo=True)
        style_text(self, self.text)
        self.text.tag_configure("hit", background=app.theme.p["accent_soft"], foreground=app.theme.p["accent"],
                                underline=True)
        self.text.pack(fill="both", expand=True, pady=4)
        self.result = ttk.Label(self, text="", wraplength=520, justify="left")
        self.result.pack(anchor="w")
        self.text.bind("<<Modified>>", self._modified)
        self._job = None

    def _paste(self) -> None:
        try:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", self.clipboard_get())
        except tk.TclError:
            pass

    def _use_last(self) -> None:
        self.text.delete("1.0", "end")
        self.text.insert("1.0", self.app.last_text[:20000])

    def _modified(self, _e=None) -> None:
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self.schedule()

    def schedule(self, *_a) -> None:
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(250, self.run)

    def run(self) -> None:
        self._job = None
        self.text.tag_remove("hit", "1.0", "end")
        sample = self.text.get("1.0", "end-1c")
        try:
            rule = self.get_rule()
        except Exception:
            return
        if not sample.strip():
            self.result.configure(text="Type or paste some text to test this rule.", style="Muted.TLabel")
            return
        res = evaluate_rule(rule, sample, "", self.app.fuzzy_threshold())
        if res.error:
            self.result.configure(text=f"⚠ {res.error}", style="Warn.TLabel")
            return
        for a, b in res.spans:
            self.text.tag_add("hit", f"1.0+{a}c", f"1.0+{b}c")
        if res.hit:
            vals = "; ".join(res.values[:8])
            self.result.configure(text=f"✔ Found {len(res.values)}: {vals}", style="Ok.TLabel")
        else:
            self.result.configure(text="✖ Nothing found in this text.", style="Err.TLabel")


# --------------------------------------------------------------------------- concrete editors
def _rule_props() -> list[Prop]:
    return [
        Prop("label", "Name", "str", help="A short name for yourself"),
        Prop("kind", "Looks for", "choice", [(k, KIND_LABELS[k]) for k in KINDS],
             help="Keywords: a word list • Regex: a pattern • After label: the value after 'Label:' • Built-in: ready-made detector"),
        Prop("pattern", "Keywords / pattern", "text", rows=4, width=52,
             help="Keywords: one per line or comma separated. 'Canonical = a, b' reports the canonical name.\n"
                  "Regex: the (group) is the value kept.\nAfter label: the label words."),
        Prop("level", "Necessity", "level", help="\n".join(f"{LEVEL_LABELS[k]}: {v}" for k, v in LEVEL_HELP.items())),
        Prop("weight", "Weight", "float", help="How strongly a hit counts when recognising a document type (1 = normal)",
             visible_if=lambda d: d.get("_for_signals", True)),
        Prop("match", "Keyword matching", "choice", list(MATCH_MODES), visible_if=lambda d: d.get("kind") == "keyword"),
        Prop("fuzzy", "Tolerate typos (fuzzy)", "bool", help="Also match near-spellings / OCR errors",
             visible_if=lambda d: d.get("kind") == "keyword"),
        Prop("fuzzy_level", "Fuzzy level % (0 = global)", "int", visible_if=lambda d: d.get("kind") == "keyword" and d.get("fuzzy")),
        Prop("ignore_case", "Ignore upper/lower case", "bool", visible_if=lambda d: d.get("kind") != "builtin"),
        Prop("compact", "Ignore spaces in the text", "bool", help="Helps with IBANs / MRZ lines that OCR splits",
             visible_if=lambda d: d.get("kind") in ("keyword", "regex")),
        Prop("scope", "Search in", "choice", list(SCOPES)),
        Prop("multiple", "Keep all matches", "bool"),
        Prop("note", "Note", "str"),
    ]


def _rule_validate(d: dict) -> str:
    return Rule.from_dict(d).error()


def _rule_dialog_extra(holder, working, collect, extra_get, vars_):
    app = holder.winfo_toplevel().app
    tester = RuleTester(holder, app, lambda: Rule.from_dict(collect()))
    tester.pack(fill="both", expand=True)
    for v in vars_.values():
        v.trace_add("write", tester.schedule)
    # regex helper
    bar = ttk.Frame(holder)
    bar.pack(fill="x", pady=(4, 0))
    mb = ttk.Menubutton(bar, text="Regex helper ▾")
    m = tk.Menu(mb, tearoff=False)
    for label, snippet in REGEX_SNIPPETS:
        m.add_command(label=f"{label}   {snippet}", command=lambda s=snippet: copy_to_clipboard(holder, s))
    mb["menu"] = m
    mb.pack(side="left")
    ttk.Label(bar, text="Pick a snippet to copy it, then paste into the pattern box.", style="Muted.TLabel").pack(side="left", padx=8)


def rules_editor(parent, app, value=None, on_change=None, noun="rule", signals: bool = True, height: int = 7,
                 help_text: str = "") -> RecordEditor:
    props = _rule_props()
    if not signals:
        props = [p for p in props if p.key != "weight"]
    new = lambda: Rule(level="recommended", kind="keyword").to_dict() | {"_for_signals": signals}

    def strip(recs):
        return [{k: v for k, v in r.items() if not k.startswith("_")} for r in recs]

    ed = RecordEditor(parent, app, [Col("label", "Name", 150, lambda r: r.get("label") or Rule.from_dict(r).display_name()),
                                    Col("kind", "Looks for", 90, lambda r: KIND_LABELS.get(r.get("kind"), "")),
                                    Col("level", "Necessity", 90, lambda r: LEVEL_LABELS.get(r.get("level"), "")),
                                    Col("pattern", "What", 260)],
                      props, new, value, (lambda recs: on_change(strip(recs))) if on_change else None,
                      library=[(g, r.label, r.to_dict()) for g, r in RULE_LIBRARY], dialog_extra=_rule_dialog_extra,
                      validate=_rule_validate, noun=noun, height=height, dialog_width=600, help_text=help_text)
    return ed


def string_list_editor(parent, app, value: list[str] | None, on_change: Callable[[list[str]], None] | None,
                       height: int = 6, placeholder: str = "Add…") -> "StringListEditor":
    return StringListEditor(parent, app, value, on_change, height, placeholder)


class StringListEditor(ttk.Frame):
    """Simple list of strings: type to add, double-click to edit, paste many, import a text file, sort, dedupe."""

    def __init__(self, parent, app, value, on_change, height: int = 6, placeholder: str = "Add…") -> None:
        super().__init__(parent)
        self.app, self.on_change = app, on_change
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.add())
        ttk.Button(top, text="Add", command=self.add).pack(side="left", padx=4)
        mb = ttk.Menubutton(top, text="⋯")
        m = tk.Menu(mb, tearoff=False)
        m.add_command(label="Paste many (one per line / comma)…", command=self.paste_many)
        m.add_command(label="Import from text file…", command=self.import_file)
        m.add_command(label="Sort A→Z", command=self.sort)
        m.add_command(label="Remove duplicates", command=self.dedupe)
        m.add_command(label="Clear all", command=self.clear)
        mb["menu"] = m
        mb.pack(side="left")
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, pady=(4, 0))
        self.lb = tk.Listbox(body, height=height, selectmode="extended", exportselection=False)
        sb = ttk.Scrollbar(body, command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.lb.pack(fill="both", expand=True)
        style_text(self, self.lb)
        self.lb.bind("<Delete>", lambda e: self.remove())
        self.lb.bind("<Double-1>", lambda e: self.edit())
        self.count = ttk.Label(self, style="Muted.TLabel")
        self.count.pack(anchor="e")
        self.set(value or [])

    def get(self) -> list[str]:
        return list(self.lb.get(0, "end"))

    def set(self, items: list[str]) -> None:
        self.lb.delete(0, "end")
        for i in items:
            self.lb.insert("end", i)
        self.count.configure(text=f"{len(items)} items — Del removes, double-click edits")

    def _changed(self) -> None:
        self.count.configure(text=f"{self.lb.size()} items — Del removes, double-click edits")
        if self.on_change:
            self.on_change(self.get())

    def add(self) -> None:
        from ..core.textutil import split_list
        for item in split_list(self.var.get()):
            if item.lower() not in {x.lower() for x in self.get()}:
                self.lb.insert("end", item)
        self.var.set("")
        self._changed()

    def remove(self) -> None:
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)
        self._changed()

    def edit(self) -> None:
        sel = self.lb.curselection()
        if sel:
            new = ask_text(self, "Edit", "Value:", self.lb.get(sel[0]))
            if new:
                self.lb.delete(sel[0])
                self.lb.insert(sel[0], new)
                self._changed()

    def paste_many(self) -> None:
        from ..core.textutil import split_list
        text = ask_text(self, "Paste many", "One value per line (or separated by commas):", "", multiline=True)
        if text:
            have = {x.lower() for x in self.get()}
            for item in split_list(text):
                if item.lower() not in have:
                    have.add(item.lower())
                    self.lb.insert("end", item)
            self._changed()

    def import_file(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Text / CSV", "*.txt *.csv *.lst"), ("All", "*.*")],
                                          parent=self.winfo_toplevel())
        if path:
            from ..core.textutil import split_list
            try:
                text = open(path, encoding="utf-8-sig", errors="replace").read()
            except OSError as exc:
                error(self, str(exc))
                return
            have = {x.lower() for x in self.get()}
            for item in split_list(text):
                if item.lower() not in have:
                    have.add(item.lower())
                    self.lb.insert("end", item)
            self._changed()

    def sort(self) -> None:
        self.set(sorted(self.get(), key=str.lower))
        self._changed()

    def dedupe(self) -> None:
        from ..core.textutil import unique
        self.set(unique(self.get()))
        self._changed()

    def clear(self) -> None:
        if self.lb.size() and confirm(self, "Remove all items?"):
            self.lb.delete(0, "end")
            self._changed()
