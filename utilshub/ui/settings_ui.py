"""Generic settings window built from `Opt` / `Page` declarations (changes apply immediately)."""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import colorchooser, filedialog, ttk
from typing import Any

from ..core.schema import Opt, Page
from .editors import EDITORS
from .records import StringListEditor
from .widgets import FolderEntry, ScrollFrame, Tooltip, confirm, error, info, style_text


class OptionPage(ttk.Frame):
    """Renders the options of one page. Used by the settings window AND embeddable inside tools."""

    def __init__(self, parent: tk.Misc, app, opts: list[Opt], show_advanced: bool = True) -> None:
        super().__init__(parent)
        self.app, self.s = app, app.settings
        self.opts = opts
        self.widgets: dict[str, ttk.Frame] = {}
        self._reset_btns: dict[str, ttk.Button] = {}
        self._vars: dict[str, tk.Variable] = {}
        self.show_advanced = show_advanced
        self._guard = False
        self._unsub = self.s.subscribe("", self._external_change)
        self.bind("<Destroy>", lambda e: e.widget is self and self._unsub())
        self.build()

    def build(self) -> None:
        section = None
        for opt in self.opts:
            if opt.advanced and not self.show_advanced:
                continue
            if opt.section != section:
                section = opt.section
                if section:
                    ttk.Label(self, text=section, style="H3.TLabel").pack(anchor="w", pady=(14, 2))
                    ttk.Separator(self).pack(fill="x", pady=(0, 6))
            row = ttk.Frame(self)
            row.pack(fill="x", pady=3)
            self.widgets[opt.key] = row
            self._row(row, opt)

    # ------------------------------------------------------------------ one option
    def _row(self, row: ttk.Frame, opt: Opt) -> None:
        value = self.s.get(opt.key)
        wide = opt.kind in ("list", "record", "text")
        head = ttk.Frame(row)
        head.pack(fill="x")
        if opt.kind != "bool":
            ttk.Label(head, text=opt.label).pack(side="left")
        self._reset_btns[opt.key] = rb = ttk.Button(head, text="↺", width=2, style="Tiny.TButton",
                                                     command=lambda o=opt: self._reset(o))
        rb.pack(side="right")
        Tooltip(rb, "Reset to the default")
        ctl = ttk.Frame(row)
        ctl.pack(fill="x", pady=(2, 0))
        k = opt.kind
        if k == "bool":
            var = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(head, text=opt.label, variable=var).pack(side="left")
            var.trace_add("write", lambda *_: self._commit(opt, var.get()))
            self._vars[opt.key] = var
            ctl.pack_forget()
        elif k in ("int", "float"):
            var = tk.StringVar(value=str(value))
            kw = dict(from_=opt.min if opt.min is not None else -10 ** 9, to=opt.max if opt.max is not None else 10 ** 9,
                      increment=opt.step or (1 if k == "int" else 0.1))
            sp = ttk.Spinbox(ctl, textvariable=var, width=10, **kw)
            sp.pack(side="left")
            var.trace_add("write", lambda *_: self._commit_num(opt, var.get()))
            self._vars[opt.key] = var
        elif k == "percent":
            var = tk.IntVar(value=int(value))
            lbl = ttk.Label(ctl, text=f"{int(value)}%", width=5)
            sc = ttk.Scale(ctl, from_=opt.min or 0, to=opt.max or 100, variable=var, length=240,
                           command=lambda v, o=opt, l=lbl: (l.configure(text=f"{int(float(v))}%"), self._commit(o, int(float(v)))))
            sc.pack(side="left")
            lbl.pack(side="left", padx=8)
            self._vars[opt.key] = var
        elif k == "choice":
            pairs = opt.choice_pairs()
            labels = [lbl for _, lbl in pairs]
            cur = next((lbl for v, lbl in pairs if v == value), str(value))
            var = tk.StringVar(value=cur)
            cb = ttk.Combobox(ctl, textvariable=var, values=labels, state="readonly", width=32)
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda e, o=opt, v=var, p=pairs: self._commit(o, next((val for val, l in p if l == v.get()), v.get())))
            self._vars[opt.key] = var
        elif k in ("folder", "file", "append"):
            var = tk.StringVar(value=str(value))
            fe = FolderEntry(ctl, var, "dir" if k == "folder" else k, opt.filetypes, width=46, open_button=(k == "folder"))
            fe.pack(fill="x")
            var.trace_add("write", lambda *_: self._commit(opt, var.get().strip()))
            self._vars[opt.key] = var
        elif k == "color":
            var = tk.StringVar(value=str(value))
            ttk.Entry(ctl, textvariable=var, width=12).pack(side="left")
            ttk.Button(ctl, text="Pick…", style="Tiny.TButton",
                       command=lambda v=var: v.set(colorchooser.askcolor(parent=self.winfo_toplevel())[1] or v.get())).pack(side="left", padx=4)
            ttk.Button(ctl, text="Clear", style="Tiny.TButton", command=lambda v=var: v.set("")).pack(side="left")
            var.trace_add("write", lambda *_: self._commit(opt, var.get().strip()))
            self._vars[opt.key] = var
        elif k == "font":
            var = tk.StringVar(value=str(value))
            fams = [""] + sorted(set(tkfont.families()))
            ttk.Combobox(ctl, textvariable=var, values=fams, width=32).pack(side="left")
            var.trace_add("write", lambda *_: self._commit(opt, var.get().strip()))
            self._vars[opt.key] = var
        elif k == "secret":                                   # API key: shown as dots, with a Show/Hide toggle
            var = tk.StringVar(value=str(value))
            ent = ttk.Entry(ctl, textvariable=var, width=opt.width or 46, show="•")
            ent.pack(side="left", fill="x", expand=wide)
            ttk.Button(ctl, text="Show", style="Tiny.TButton",
                       command=lambda e=ent: e.configure(show="" if e.cget("show") else "•")).pack(side="left", padx=4)
            var.trace_add("write", lambda *_: self._commit(opt, var.get().strip()))
            self._vars[opt.key] = var
        elif k == "text":
            t = tk.Text(ctl, height=4, wrap="word")
            style_text(self, t)
            t.insert("1.0", str(value))
            t.pack(fill="x")
            t.bind("<FocusOut>", lambda e, o=opt, w=t: self._commit(o, w.get("1.0", "end").rstrip("\n")))
        elif k == "list":
            StringListEditor(ctl, self.app, value, lambda v, o=opt: self._commit(o, v), height=5).pack(fill="both", expand=True)
        elif k == "record":
            factory = EDITORS[opt.editor]
            factory(ctl, self.app, value, lambda v, o=opt: self._commit(o, v)).pack(fill="both", expand=True)
        else:                                                 # str
            var = tk.StringVar(value=str(value))
            ttk.Entry(ctl, textvariable=var, width=opt.width or 46).pack(side="left", fill="x", expand=wide)
            var.trace_add("write", lambda *_: self._commit(opt, var.get()))
            self._vars[opt.key] = var
        if opt.help:
            ttk.Label(row, text=opt.help, style="Muted.TLabel", wraplength=620, justify="left").pack(anchor="w", pady=(1, 0))
        self._paint_reset(opt)

    def _commit_num(self, opt: Opt, raw: str) -> None:
        try:
            v = int(float(raw)) if opt.kind == "int" else float(raw)
        except ValueError:
            return
        if opt.min is not None:
            v = max(opt.min, v)
        if opt.max is not None:
            v = min(opt.max, v)
        self._commit(opt, v)

    def _commit(self, opt: Opt, value: Any) -> None:
        if self._guard:
            return
        self.s.set(opt.key, value)
        self._paint_reset(opt)

    def _paint_reset(self, opt: Opt) -> None:
        btn = self._reset_btns.get(opt.key)
        if btn is not None:
            btn.state(["!disabled"] if not self.s.is_default(opt.key) else ["disabled"])

    def _reset(self, opt: Opt) -> None:
        self.s.reset(opt.key)
        self.rebuild()

    def _external_change(self, key: str, value: Any) -> None:
        if self._guard or not self.winfo_exists():
            return
        if key in self._reset_btns:
            opt = next(o for o in self.opts if o.key == key)
            self._paint_reset(opt)
            if key in self._vars:
                var = self._vars[key]
                if opt.kind in ("folder", "file", "append", "str", "secret", "color", "font"):
                    val = str(value or "")
                    if str(var.get()).strip() != val.strip():
                        self._guard = True
                        try:
                            var.set(val)
                        finally:
                            self._guard = False
                elif opt.kind == "bool":
                    if bool(var.get()) != bool(value):
                        self._guard = True
                        try:
                            var.set(bool(value))
                        finally:
                            self._guard = False

    def rebuild(self) -> None:
        for w in self.winfo_children():
            w.destroy()
        self.widgets.clear()
        self._reset_btns.clear()
        self._vars.clear()
        self.build()

    def reset_all(self) -> None:
        for o in self.opts:
            self.s.reset(o.key)
        self.rebuild()


def group_pages(pages: list[Page]) -> list[tuple[str, list[Page]]]:
    """Group settings pages by `page.group` while preserving page order."""
    grouped: dict[str, list[Page]] = {}
    for p in pages:
        grouped.setdefault(p.group or "Other", []).append(p)
    return list(grouped.items())


# --------------------------------------------------------------------------- window
class SettingsWindow(tk.Toplevel):
    _instance: "SettingsWindow | None" = None

    @classmethod
    def open(cls, app, page: str | None = None) -> "SettingsWindow":
        if cls._instance is not None and cls._instance.winfo_exists():
            cls._instance.lift()
            if page:
                cls._instance.show(page)
            return cls._instance
        cls._instance = cls(app, page)
        return cls._instance

    def __init__(self, app, page: str | None = None) -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Settings")
        self.configure(background=app.theme.p["bg"])
        app.theme.on_change(lambda: self.winfo_exists() and self.configure(background=app.theme.p["bg"]))
        w, h = 980, 700
        self.geometry(f"{w}x{h}+{app.root.winfo_rootx() + 40}+{app.root.winfo_rooty() + 30}")
        self.minsize(760, 480)
        self.pages: list[Page] = app.settings_pages()
        self._nav_pages: list[Page] = []
        self._nav_buttons: dict[str, ttk.Button] = {}
        self._group_buttons: dict[str, ttk.Button] = {}
        self._collapsed: set[str] = set()
        self._last_query: str = ""
        self.current: str = ""
        self.show_advanced = tk.BooleanVar(value=False)

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Settings", style="H2.TLabel").pack(side="left")
        ttk.Checkbutton(top, text="Show advanced options", variable=self.show_advanced,
                        command=lambda: self.show(self.current, force=True)).pack(side="right", padx=(0, 10))
        self.search = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.search, width=26)
        e.pack(side="right", padx=10)
        Tooltip(e, "Search settings")
        self.search.trace_add("write", lambda *_: self._search())
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, style="Side.TFrame")
        self.nav_scroll = ScrollFrame(left, bg_key="surface")
        self.nav_scroll.canvas.configure(width=245)
        self.nav_scroll.pack(fill="both", expand=True, padx=2, pady=4)
        paned.add(left, weight=0)
        right = ttk.Frame(paned)
        paned.add(right, weight=1)
        self.group_lbl = ttk.Label(right, style="Muted.TLabel")
        self.group_lbl.pack(anchor="w", padx=14, pady=(8, 0))
        self.title_lbl = ttk.Label(right, style="H2.TLabel")
        self.title_lbl.pack(anchor="w", padx=14, pady=(0, 0))
        self.desc_lbl = ttk.Label(right, style="Muted.TLabel", wraplength=640, justify="left")
        self.desc_lbl.pack(anchor="w", padx=14)
        self.scroll = ScrollFrame(right)
        self.scroll.pack(fill="both", expand=True, padx=(14, 4), pady=6)
        bot = ttk.Frame(self, padding=(10, 6))
        bot.pack(fill="x")
        mb = ttk.Menubutton(bot, text="Backup & restore ▾")
        m = tk.Menu(mb, tearoff=False)
        m.add_command(label="Export all settings…", command=self.export_all)
        m.add_command(label="Import settings…", command=self.import_all)
        m.add_command(label="Import from the old separate apps…", command=self.import_old)
        m.add_separator()
        m.add_command(label="Open the data folder", command=self.open_data)
        mb["menu"] = m
        mb.pack(side="left")
        ttk.Button(bot, text="Reset this page", command=self.reset_page).pack(side="left", padx=8)
        ttk.Button(bot, text="Close", style="Accent.TButton", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda e: self.destroy())
        self._fill_nav(self.pages)
        self.show(page or self.pages[0].id)

    def _fill_nav(self, pages: list[Page]) -> None:
        self._nav_pages = list(pages)
        self._nav_buttons.clear()
        self._group_buttons.clear()
        body = self.nav_scroll.body
        for w in body.winfo_children():
            w.destroy()
        if not pages:
            ttk.Label(body, text="No matching settings", style="SideTitle.TLabel").pack(anchor="w", padx=12, pady=12)
            return
        searching = bool(self.search.get().strip())
        for gi, (group_name, group_items) in enumerate(group_pages(pages)):
            if gi > 0:
                ttk.Separator(body).pack(fill="x", padx=10, pady=(6, 2))
            collapsed = (group_name in self._collapsed) and not searching
            label = f"▸  {group_name.upper()} ({len(group_items)})" if collapsed else f"▾  {group_name.upper()}"
            hdr = ttk.Button(body, text=label, style="SideGroup.TButton", cursor="hand2",
                             command=lambda g=group_name: self._toggle_group(g))
            hdr.pack(fill="x", padx=4, pady=(4 if gi == 0 else 2, 1))
            self._group_buttons[group_name] = hdr
            if not collapsed:
                for p in group_items:
                    st = "SideItemSel.TButton" if p.id == self.current else "SideItem.TButton"
                    btn = ttk.Button(body, text=f"{p.icon}  {p.title}", style=st, cursor="hand2",
                                     command=lambda pid=p.id: self.show(pid))
                    btn.pack(fill="x", padx=4, pady=1)
                    self._nav_buttons[p.id] = btn
        self.nav_scroll.bind_children()

    def _toggle_group(self, group_name: str) -> None:
        if self.search.get().strip():
            return
        if group_name in self._collapsed:
            self._collapsed.remove(group_name)
        else:
            self._collapsed.add(group_name)
        self._fill_nav(self._nav_pages)

    def _page_opts(self, page: Page) -> list[Opt]:
        return page.opts or self.app.settings.opts(f"{page.id}.")

    def _search(self) -> None:
        q = self.search.get().strip().lower()
        had_query = bool(self._last_query)
        self._last_query = q
        if not q:
            self._fill_nav(self.pages)
            if had_query and self.current:
                self.show(self.current, force=True)
            return
        hits = [p for p in self.pages
                if q in (p.title + " " + p.description + " " + p.group).lower()
                or any(q in (o.label + o.help + o.section).lower() for o in self._page_opts(p))]
        self._fill_nav(hits)
        if hits:
            self.show(hits[0].id, query=q)

    def show(self, page_id: str, force: bool = False, query: str = "") -> None:
        page = next((p for p in self.pages if p.id == page_id), None)
        if not page or (page_id == self.current and not force and not query):
            return
        self.current = page_id
        grp = page.group or "Other"
        if grp in self._collapsed:
            self._collapsed.remove(grp)
            self._fill_nav(self._nav_pages)
        else:
            for pid, btn in self._nav_buttons.items():
                if btn.winfo_exists():
                    btn.configure(style="SideItemSel.TButton" if pid == page_id else "SideItem.TButton")
        self.group_lbl.configure(text=grp.upper() if page.group else "")
        self.title_lbl.configure(text=f"{page.icon}  {page.title}")
        self.desc_lbl.configure(text=page.description)
        for w in self.scroll.body.winfo_children():
            w.destroy()
        opts = self._page_opts(page)
        if query:
            opts = [o for o in opts if query in (o.label + o.help + o.section).lower()] or opts
        if opts:
            OptionPage(self.scroll.body, self.app, opts, self.show_advanced.get()).pack(fill="x")
        if page.builder:
            page.builder(self.scroll.body, self.app)
        self.scroll.bind_children()
        self.scroll.canvas.yview_moveto(0)

    def reset_page(self) -> None:
        page = next((p for p in self.pages if p.id == self.current), None)
        if page and confirm(self, f"Reset all '{page.title}' settings to their defaults?"):
            keys = [o.key for o in self._page_opts(page)]
            if page.id == "folders":
                from .folders_panel import feature_path_keys
                keys = feature_path_keys(self.app)
            for key in keys:
                self.app.settings.reset(key)
            self.show(self.current, force=True)

    def export_all(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile="utilshub-settings.json",
                                            filetypes=[("JSON", "*.json")], parent=self)
        if path:
            self.app.settings.export_to(path)
            info(self, "Settings exported.")

    def import_all(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")], parent=self)
        if path:
            try:
                n = self.app.settings.import_from(path)
            except (OSError, ValueError) as exc:
                error(self, f"Could not import: {exc}")
                return
            info(self, f"Imported {n} settings. Some changes need a restart.")
            self.show(self.current, force=True)

    def import_old(self) -> None:
        from ..core.migrate import import_old_settings
        folder = filedialog.askdirectory(title="Folder of the old apps (with config.json / docpacker_config.json)", parent=self)
        if folder:
            done = import_old_settings(self.app.settings, folder)
            info(self, "\n".join(done) if done else "No old settings files were found in that folder.")
            self.show(self.current, force=True)

    def open_data(self) -> None:
        from ..core.settings import data_dir
        from ..core.system import open_path
        open_path(data_dir())
