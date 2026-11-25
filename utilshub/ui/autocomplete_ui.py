"""Settings > Autocomplete: choose, box by box, where it works - and edit each box's keywords and shortcuts.

Left: every box the program knows (filter, click the ✔ to switch one on / off, Enable / Disable all shown).
Right: the selected box - its own switch, "remember what I type", keywords (one per line), shortcuts, a try-it box.
Changes are saved at once.  Right-click any box in the program -> "Keywords & shortcuts for this box…" jumps here.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, ttk

from ..core.autocomplete import (Autocomplete, format_keywords, format_shortcuts, parse_keywords, parse_shortcuts,
                                 validate_shortcut)
from .autocomplete import attach
from .widgets import Tooltip, confirm, error, info, style_text

ON, OFF = "✔", "—"


class AutocompletePanel(ttk.Frame):
    def __init__(self, parent: tk.Misc, app) -> None:
        super().__init__(parent)
        self.app: object = app
        self.svc: Autocomplete = app.autocomplete
        self.current = ""
        self._loading = False
        self._kw_job = None
        self._kw_draft: str | None = None
        self._shown: list[str] = []
        ttk.Label(self, text="Boxes", style="H3.TLabel").pack(anchor="w", pady=(14, 2))
        ttk.Separator(self).pack(fill="x", pady=(0, 6))
        ttk.Label(self, text="Click the ✔ in front of a box to switch autocomplete on or off for that box only. Select a box "
                             "to edit its keywords and shortcuts.", style="Muted.TLabel", wraplength=640,
                  justify="left").pack(anchor="w", pady=(0, 6))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        self._build_list(body)
        self._build_editor(body)
        self._unsubs = [self.svc.on_change(self._external), self.svc.on_select(self.select)]
        self.bind("<Destroy>", self._destroy, add="+")
        self.fill()
        first = self.svc.pending_select or (self._shown[0] if self._shown else "")
        self.svc.pending_select = ""
        if first:
            self.select(first)

    # ------------------------------------------------------------------ left: the list
    def _build_list(self, body: ttk.Frame) -> None:
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        self.filter = tk.StringVar()
        e = ttk.Entry(left, textvariable=self.filter, width=34)
        e.pack(fill="x")
        Tooltip(e, "Filter the boxes")
        self.filter.trace_add("write", lambda *_: self.fill())
        cols = ("on", "name", "kw", "sc")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse", height=16)
        for key, head, w, anchor in (("on", "", 30, "center"), ("name", "Box", 210, "w"), ("kw", "Words", 48, "e"),
                                     ("sc", "Short.", 46, "e")):
            self.tree.heading(key, text=head)
            self.tree.column(key, width=w, anchor=anchor, stretch=(key == "name"))
        self.tree.tag_configure("off", foreground=self.app.theme.p["muted"])
        self.tree.tag_configure("group", foreground=self.app.theme.accent)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", pady=(6, 0))
        self.tree.pack(fill="both", expand=True, pady=(6, 0))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-1>", self._click)
        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(6, 0), side="bottom")
        ttk.Button(bar, text="Enable all shown", style="Tiny.TButton", command=lambda: self._bulk(True)).pack(side="left")
        ttk.Button(bar, text="Disable all shown", style="Tiny.TButton", command=lambda: self._bulk(False)).pack(side="left", padx=4)

    def fill(self) -> None:
        q = self.filter.get().strip().lower()
        keep = self.current
        self.tree.delete(*self.tree.get_children())
        self._shown = []
        group = None
        for d in self.svc.fields():
            if q and q not in (d.label + " " + d.group + " " + d.id).lower():
                continue
            if d.group != group:
                group = d.group
                self.tree.insert("", "end", iid=f"g:{group}", values=("", group.upper(), "", ""), tags=("group",))
            self.tree.insert("", "end", iid=d.id, values=self._row(d.id), tags=self._tags(d.id))
            self._shown.append(d.id)
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)

    def _row(self, fid: str) -> tuple:
        d, cfg = self.svc.definition(fid), self.svc.config(fid)
        return (ON if cfg.enabled else OFF, d.label, len(cfg.keywords), len(cfg.shortcuts))

    def _tags(self, fid: str) -> tuple:
        return () if self.svc.config(fid).enabled else ("off",)

    def _refresh_row(self, fid: str) -> None:
        if self.tree.exists(fid):
            self.tree.item(fid, values=self._row(fid), tags=self._tags(fid))

    def _click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell" or self.tree.identify_column(e.x) != "#1":
            return None
        fid = self.tree.identify_row(e.y)
        if fid and not fid.startswith("g:"):
            self.svc.set_enabled(fid, not self.svc.config(fid).enabled)
            self.tree.selection_set(fid)
            return "break"
        return None

    def _bulk(self, on: bool) -> None:
        self.svc.set_many_enabled(self._shown, on)
        self.fill()
        self._load_editor()

    def _on_select(self, _e=None) -> None:
        sel = self.tree.selection()
        if sel and not sel[0].startswith("g:") and sel[0] != self.current:
            self.select(sel[0])

    def select(self, fid: str) -> None:
        if not fid or fid == self.current:
            return
        self._flush_keywords()
        if self.filter.get() and not self.tree.exists(fid):
            self.filter.set("")
        if not self.tree.exists(fid):
            self.fill()
        self.current = fid
        if self.tree.exists(fid):
            self.tree.selection_set(fid)
            self.tree.see(fid)
        self._load_editor()

    # ------------------------------------------------------------------ right: the editor
    def _build_editor(self, body: ttk.Frame) -> None:
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        self.title = ttk.Label(right, style="H3.TLabel")
        self.title.pack(anchor="w")
        self.desc = ttk.Label(right, style="Muted.TLabel", wraplength=420, justify="left")
        self.desc.pack(anchor="w", pady=(0, 4))
        self.on_var, self.learn_var = tk.BooleanVar(), tk.BooleanVar()
        ttk.Checkbutton(right, text="Autocomplete for this box", variable=self.on_var, command=self._toggle_on).pack(anchor="w")
        self.learn_cb = ttk.Checkbutton(right, text="Remember what I type here and offer it again", variable=self.learn_var,
                                        command=self._toggle_learn)
        self.learn_cb.pack(anchor="w")
        self.learn_note = ttk.Label(right, style="Muted.TLabel", wraplength=420, justify="left")
        self.learn_note.pack(anchor="w")

        ttk.Label(right, text="Keywords  (one per line)", style="H3.TLabel").pack(anchor="w", pady=(10, 0))
        self.linked = ttk.Label(right, style="Muted.TLabel", wraplength=420, justify="left")
        self.linked.pack(anchor="w")
        self.kw = tk.Text(right, height=7, width=48, wrap="none", undo=True)
        style_text(self, self.kw)
        self.kw.pack(fill="x", pady=2)
        self.kw.bind("<KeyRelease>", lambda e: self._kw_later())
        self.kw.bind("<FocusOut>", lambda e: self._flush_keywords())
        kb = ttk.Frame(right)
        kb.pack(fill="x")
        ttk.Button(kb, text="Import…", style="Tiny.TButton", command=self._import_keywords).pack(side="left")
        ttk.Button(kb, text="Sort A→Z", style="Tiny.TButton", command=self._sort_keywords).pack(side="left", padx=4)

        ttk.Label(right, text="Shortcuts", style="H3.TLabel").pack(anchor="w", pady=(10, 0))
        ttk.Label(right, text="Type the shortcut, then Space or Tab: it becomes the text. A shortcut that starts with a symbol "
                              "(@g, ;t) works in the middle of a word. In the text use {today} {time} {cursor}.",
                  style="Muted.TLabel", wraplength=420, justify="left").pack(anchor="w")
        self.sc = ttk.Treeview(right, columns=("abbr", "text"), show="headings", selectmode="browse", height=5)
        self.sc.heading("abbr", text="Shortcut")
        self.sc.heading("text", text="Becomes")
        self.sc.column("abbr", width=90, stretch=False)
        self.sc.column("text", width=300)
        self.sc.pack(fill="x", pady=2)
        self.sc.bind("<<TreeviewSelect>>", self._sc_pick)
        row = ttk.Frame(right)
        row.pack(fill="x")
        self.abbr_var, self.text_var = tk.StringVar(), tk.StringVar()
        ttk.Entry(row, textvariable=self.abbr_var, width=10).pack(side="left")
        ttk.Label(row, text="→").pack(side="left", padx=4)
        te = ttk.Entry(row, textvariable=self.text_var)
        te.pack(side="left", fill="x", expand=True)
        te.bind("<Return>", lambda e: self._sc_save())
        ttk.Button(row, text="Add / update", style="Tiny.TButton", command=self._sc_save).pack(side="left", padx=4)
        ttk.Button(row, text="Delete", style="Tiny.TButton", command=self._sc_delete).pack(side="left")
        self.sc_msg = ttk.Label(right, style="Err.TLabel")
        self.sc_msg.pack(anchor="w")
        sb = ttk.Frame(right)
        sb.pack(fill="x")
        ttk.Button(sb, text="Import…", style="Tiny.TButton", command=self._sc_import).pack(side="left")
        ttk.Button(sb, text="Export…", style="Tiny.TButton", command=self._sc_export).pack(side="left", padx=4)

        ttk.Label(right, text="Try it", style="H3.TLabel").pack(anchor="w", pady=(10, 0))
        self.try_var = tk.StringVar()
        self.try_entry = ttk.Entry(right, textvariable=self.try_var)
        self.try_entry.pack(fill="x", pady=2)
        self.try_ctl = None
        bot = ttk.Frame(right)
        bot.pack(fill="x", pady=(8, 0))
        ttk.Button(bot, text="Reset this box to the defaults", command=self._reset).pack(side="left")
        self.clear_btn = ttk.Button(bot, text="Forget what was typed", command=self._forget)
        self.clear_btn.pack(side="left", padx=6)

    def _load_editor(self) -> None:
        fid = self.current
        if not fid:
            return
        self._loading = True
        try:
            d, cfg = self.svc.definition(fid), self.svc.config(fid)
            self.title.configure(text=d.label)
            self.desc.configure(text=(d.description + "  " if d.description else "") + f"[{d.group}]")
            self.on_var.set(cfg.enabled)
            self.learn_var.set(cfg.learn)
            if d.sensitive:
                self.learn_cb.state(["disabled"])
                self.learn_note.configure(text="Never remembered: this box holds personal numbers or addresses.")
            elif d.mode == "text":
                self.learn_cb.state(["disabled"])
                self.learn_note.configure(text="Free text is not remembered - use keywords and shortcuts.")
            else:
                self.learn_cb.state(["!disabled"])
                self.learn_note.configure(text="")
            self.linked.configure(text=("Also offered: " + ", ".join(d.linked) + " (Settings word lists)") if d.linked else "")
            self.kw.delete("1.0", "end")
            self.kw.insert("1.0", format_keywords(cfg.keywords))
            self._kw_draft = None
            self._fill_shortcuts(cfg.shortcuts)
            self.abbr_var.set("")
            self.text_var.set("")
            self.sc_msg.configure(text="")
            self._update_forget()
            self.try_var.set("")
            if self.try_ctl is None:
                self.try_ctl = attach(self.try_entry, fid, learn=False)
            else:
                self.try_ctl.field_id = fid
        finally:
            self._loading = False

    def _fill_shortcuts(self, shortcuts: dict) -> None:
        self.sc.delete(*self.sc.get_children())
        for a, t in shortcuts.items():
            self.sc.insert("", "end", iid=a, values=(a, t.replace("\n", "\\n")))

    def _update_forget(self) -> None:
        n = self.svc.learned_count(self.current)
        self.clear_btn.configure(text=f"Forget what was typed ({n})")
        self.clear_btn.state(["!disabled"] if n else ["disabled"])

    # ------------------------------------------------------------------ edits
    def _toggle_on(self) -> None:
        if not self._loading and self.current:
            self.svc.set_enabled(self.current, self.on_var.get())

    def _toggle_learn(self) -> None:
        if not self._loading and self.current:
            self.svc.set_learn(self.current, self.learn_var.get())

    def _kw_later(self) -> None:
        if self._loading:
            return
        try:
            if self.kw.winfo_exists():
                self._kw_draft = self.kw.get("1.0", "end")
        except tk.TclError:
            pass
        if self._kw_job is not None:
            self.after_cancel(self._kw_job)
        self._kw_job = self.after(600, self._flush_keywords)

    def _flush_keywords(self) -> None:
        if self._kw_job is not None:
            try:
                self.after_cancel(self._kw_job)
            except Exception:
                pass
            self._kw_job = None
        if self._loading or not self.current:
            return
        raw: str | None = None
        try:
            if self.kw.winfo_exists():
                raw = self.kw.get("1.0", "end")
        except tk.TclError:
            pass
        if raw is None:
            raw = self._kw_draft
        self._kw_draft = None
        if raw is None:
            return
        new = parse_keywords(raw)
        if new != self.svc.config(self.current).keywords:
            self.svc.set_keywords(self.current, new)

    def _import_keywords(self) -> None:
        path = filedialog.askopenfilename(title="Keywords file (one per line)", parent=self.winfo_toplevel(),
                                          filetypes=[("Text / CSV", "*.txt *.csv"), ("All", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as fh:
                extra = parse_keywords(fh.read())
        except (OSError, UnicodeDecodeError) as exc:
            error(self, f"Could not read the file: {exc}")
            return
        merged = parse_keywords("\n".join(self.svc.config(self.current).keywords + extra))
        added = len(merged) - len(self.svc.config(self.current).keywords)
        self.svc.set_keywords(self.current, merged)
        self._load_editor()
        info(self, f"Added {added} new keyword(s).")

    def _sort_keywords(self) -> None:
        self._flush_keywords()
        self.svc.set_keywords(self.current, sorted(self.svc.config(self.current).keywords, key=str.casefold))
        self._load_editor()

    def _sc_pick(self, _e=None) -> None:
        sel = self.sc.selection()
        if sel:
            a = sel[0]
            self.abbr_var.set(a)
            self.text_var.set(self.svc.config(self.current).shortcuts.get(a, "").replace("\n", "\\n"))

    def _sc_save(self) -> None:
        fid = self.current
        abbr, text = self.abbr_var.get().strip(), self.text_var.get().replace("\\n", "\n").strip()
        cur = dict(self.svc.config(fid).shortcuts)
        sel = self.sc.selection()
        replacing = sel[0] if sel and sel[0] in cur else (abbr if abbr in cur else "")
        err = validate_shortcut(abbr, text, cur, replacing=replacing)
        if err:
            self.sc_msg.configure(text=err)
            return
        if replacing:
            cur.pop(replacing, None)
        cur[abbr] = text
        self.svc.set_shortcuts(fid, cur)
        self._after_shortcuts(abbr)

    def _sc_delete(self) -> None:
        sel = self.sc.selection()
        if not sel:
            return
        cur = dict(self.svc.config(self.current).shortcuts)
        cur.pop(sel[0], None)
        self.svc.set_shortcuts(self.current, cur)
        self._after_shortcuts("")

    def _after_shortcuts(self, select: str) -> None:
        self._loading = True
        try:
            self._fill_shortcuts(self.svc.config(self.current).shortcuts)
            self.abbr_var.set("")
            self.text_var.set("")
            self.sc_msg.configure(text="")
            if select and self.sc.exists(select):
                self.sc.selection_set(select)
        finally:
            self._loading = False

    def _sc_import(self) -> None:
        path = filedialog.askopenfilename(title="Shortcuts file (shortcut = text per line)", parent=self.winfo_toplevel(),
                                          filetypes=[("Text / CSV", "*.txt *.csv"), ("All", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as fh:
                parsed, problems = parse_shortcuts(fh.read())
        except (OSError, UnicodeDecodeError) as exc:
            error(self, f"Could not read the file: {exc}")
            return
        cur = dict(self.svc.config(self.current).shortcuts)
        cur.update(parsed)
        self.svc.set_shortcuts(self.current, cur)
        self._after_shortcuts("")
        info(self, f"Imported {len(parsed)} shortcut(s)." + (("\n\nSkipped:\n" + "\n".join(problems[:8])) if problems else ""))

    def _sc_export(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="shortcuts.txt", parent=self.winfo_toplevel(),
                                            filetypes=[("Text", "*.txt")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(format_shortcuts(self.svc.config(self.current).shortcuts) + "\n")
            except OSError as exc:
                error(self, f"Could not save: {exc}")

    def _reset(self) -> None:
        if self.current and confirm(self, "Put this box back to its default keywords, shortcuts and switches?"):
            self.svc.reset_field(self.current)
            self.fill()
            self._load_editor()

    def _forget(self) -> None:
        if self.current and confirm(self, "Forget everything that was typed in this box?"):
            self.svc.clear_learned(self.current)
            self._update_forget()

    # ------------------------------------------------------------------ outside changes
    def _external(self) -> None:
        """Something changed the settings (right-click menu, Enable all ...): refresh the rows, not the text being edited."""
        if not self.winfo_exists():
            return
        for fid in self._shown:
            self._refresh_row(fid)
        if self.current and not self._loading:
            cfg = self.svc.config(self.current)
            self._loading = True
            try:
                self.on_var.set(cfg.enabled)
            finally:
                self._loading = False

    def _destroy(self, e=None) -> None:
        if e is not None and e.widget is not self:
            return
        self._flush_keywords()
        unsubs, self._unsubs = self._unsubs, []
        for u in unsubs:
            u()


def build_autocomplete_panel(parent: tk.Misc, app) -> None:
    AutocompletePanel(parent, app).pack(fill="both", expand=True)
