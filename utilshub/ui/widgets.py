"""Reusable Tk widgets: tooltips, scroll frames, folder entry, log view, sortable data table, dialogs."""
from __future__ import annotations

import csv
import io
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Iterable

from ..core.system import open_path


def app_of(widget: tk.Misc):
    return getattr(widget.winfo_toplevel(), "app", None) or getattr(widget.nametowidget("."), "app", None)


def style_text(widget: tk.Misc, text_widget) -> None:
    app = app_of(widget)
    if app is not None:
        if isinstance(text_widget, tk.Listbox):
            app.theme.style_listbox(text_widget)
        elif isinstance(text_widget, tk.Canvas):
            app.theme.style_canvas(text_widget)
        else:
            app.theme.style_text(text_widget)


def copy_to_clipboard(widget: tk.Misc, text: str) -> None:
    root = widget.nametowidget(".")
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()                                           # flush so other applications can read it


def bind_wheel(widget: tk.Misc, target: tk.Misc | None = None, horizontal: bool = False) -> None:
    """Mouse-wheel scrolling for a canvas / text / tree on every platform."""
    target = target or widget

    def wheel(e):
        step = -1 if (e.delta > 0 or getattr(e, "num", 0) == 4) else 1
        (target.xview_scroll if horizontal or (e.state & 1) else target.yview_scroll)(step * 3, "units")
        return "break"

    widget.bind("<MouseWheel>", wheel, add="+")
    widget.bind("<Button-4>", wheel, add="+")
    widget.bind("<Button-5>", wheel, add="+")


# --------------------------------------------------------------------------- tooltip
class Tooltip:
    def __init__(self, widget: tk.Misc, text: str | Callable[[], str], delay: int = 500) -> None:
        self.widget, self.text, self.delay = widget, text, delay
        self.tip: tk.Toplevel | None = None
        self._job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._job:
            self.widget.after_cancel(self._job)
            self._job = None

    def _show(self) -> None:
        text = self.text() if callable(self.text) else self.text
        if not text or self.tip:
            return
        app = app_of(self.widget)
        p = app.theme.p if app else {"surface": "#ffffe0", "fg": "#000", "line": "#888"}
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.attributes("-topmost", True)
        x, y = self.widget.winfo_rootx() + 12, self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=text, justify="left", wraplength=360, background=p["surface"], foreground=p["fg"],
                 relief="solid", borderwidth=1, padx=8, pady=4).pack()

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


def tip(widget: tk.Misc, text) -> tk.Misc:
    Tooltip(widget, text)
    return widget


# --------------------------------------------------------------------------- layout helpers
class ScrollFrame(ttk.Frame):
    """A frame whose `.body` scrolls vertically (and follows the window width)."""

    def __init__(self, parent: tk.Misc, **kw) -> None:
        super().__init__(parent, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.after(50, lambda: self._paint())
        for w in (self.canvas, self.body):
            self.bind_wheel_to(w)

    def _paint(self) -> None:
        app = app_of(self)
        if app:
            self.canvas.configure(background=app.theme.p["bg"])
            app.theme.on_change(lambda: self.canvas.winfo_exists() and self.canvas.configure(background=app.theme.p["bg"]))

    def bind_wheel_to(self, w: tk.Misc) -> None:
        def wheel(e):
            if self.body.winfo_reqheight() <= self.canvas.winfo_height():
                return
            self.canvas.yview_scroll(-1 if (e.delta > 0 or getattr(e, "num", 0) == 4) else 1, "units")
        w.bind("<MouseWheel>", wheel, add="+")
        w.bind("<Button-4>", wheel, add="+")
        w.bind("<Button-5>", wheel, add="+")

    def bind_children(self) -> None:
        def walk(w):
            for c in w.winfo_children():
                if not isinstance(c, (tk.Text, ttk.Treeview, tk.Listbox)):
                    self.bind_wheel_to(c)
                walk(c)
        walk(self.body)


def card(parent: tk.Misc, title: str = "", padding: int = 10, **grid) -> ttk.Frame:
    """A titled frame; returns the inner frame to fill."""
    frame = ttk.LabelFrame(parent, text=title, padding=padding)
    return frame


def form_row(parent: tk.Misc, row: int, label: str, widget: tk.Misc, help: str = "", col: int = 0) -> None:
    ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=(0, 10), pady=3)
    widget.grid(row=row, column=col + 1, sticky="ew", pady=3)
    if help:
        Tooltip(widget, help)


def toolbar(parent: tk.Misc) -> ttk.Frame:
    f = ttk.Frame(parent)
    f.pack(fill="x", pady=(0, 6))
    return f


def hint(parent: tk.Misc, text: str, wrap: int = 520, **pack) -> ttk.Label:
    lbl = ttk.Label(parent, text=text, style="Muted.TLabel", wraplength=wrap, justify="left")
    return lbl


# --------------------------------------------------------------------------- folder / file entry
class FolderEntry(ttk.Frame):
    """Entry + Browse (+ Open) for a folder / file / save-path. Accepts dropped paths."""

    def __init__(self, parent: tk.Misc, variable: tk.StringVar | None = None, kind: str = "dir",
                 filetypes: list | None = None, on_change: Callable[[str], None] | None = None,
                 width: int = 40, title: str = "Choose", open_button: bool = False) -> None:
        super().__init__(parent)
        self.var = variable or tk.StringVar()
        self.kind, self.filetypes, self.title_text = kind, filetypes, title
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(side="left", fill="x", expand=True)
        ttk.Button(self, text="Browse…", command=self.browse, style="Tiny.TButton").pack(side="left", padx=(4, 0))
        if open_button:
            ttk.Button(self, text="Open", command=lambda: self.var.get() and open_path(self.var.get()),
                       style="Tiny.TButton").pack(side="left", padx=(4, 0))
        if on_change:
            self.var.trace_add("write", lambda *_: on_change(self.var.get()))
        from .dnd import make_drop_target
        make_drop_target(self.entry, lambda paths: self.var.set(paths[0]) if paths else None)

    def browse(self) -> None:
        cur = self.var.get().strip()
        if self.kind == "dir":
            path = filedialog.askdirectory(title=self.title_text, initialdir=cur or None, parent=self.winfo_toplevel())
        elif self.kind == "append":
            path = filedialog.asksaveasfilename(title=self.title_text, initialfile=cur, filetypes=self.filetypes or [],
                                                defaultextension=".csv", confirmoverwrite=False,
                                                parent=self.winfo_toplevel())
        elif self.kind == "save":
            path = filedialog.asksaveasfilename(title=self.title_text, initialfile=cur, filetypes=self.filetypes or [],
                                                parent=self.winfo_toplevel())
        else:
            path = filedialog.askopenfilename(title=self.title_text, initialdir=None, filetypes=self.filetypes or [],
                                              parent=self.winfo_toplevel())
        if path:
            self.var.set(path)


# --------------------------------------------------------------------------- log view
class LogView(ttk.Frame):
    """Scrolling coloured log with filter/clear/copy/save. `log(msg, level)` is thread-safe only via app.ui."""

    LEVELS = ("info", "ok", "warn", "error")

    def __init__(self, parent: tk.Misc, height: int = 10, timestamps: bool = True, max_lines: int = 2000) -> None:
        super().__init__(parent)
        self.timestamps, self.max_lines = timestamps, max_lines
        self.text = tk.Text(self, height=height, wrap="word", state="disabled", font="TkFixedFont")
        sb = ttk.Scrollbar(self, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        for lv in self.LEVELS:
            self.text.tag_configure(lv)
        self.text.tag_configure("muted")
        style_text(self, self.text)
        self._menu = tk.Menu(self.text, tearoff=False)
        self._menu.add_command(label="Copy all", command=self.copy_all)
        self._menu.add_command(label="Save as…", command=self.save)
        self._menu.add_command(label="Clear", command=self.clear)
        self.text.bind("<Button-3>", lambda e: self._menu.tk_popup(e.x_root, e.y_root))
        self.text.bind("<Button-2>", lambda e: self._menu.tk_popup(e.x_root, e.y_root))

    def log(self, message: str, level: str = "info") -> None:
        from datetime import datetime
        stamp = f"[{datetime.now():%H:%M:%S}] " if self.timestamps else ""
        t = self.text
        t.configure(state="normal")
        t.insert("end", f"{stamp}{message}\n", level if level in self.LEVELS else "info")
        lines = int(t.index("end-1c").split(".")[0])
        if lines > self.max_lines:
            t.delete("1.0", f"{lines - self.max_lines}.0")
        t.see("end")
        t.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def copy_all(self) -> None:
        copy_to_clipboard(self, self.text.get("1.0", "end"))

    def save(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="log.txt", parent=self.winfo_toplevel())
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.text.get("1.0", "end"))


# --------------------------------------------------------------------------- data table
class DataTable(ttk.Frame):
    """Sortable, filterable, multi-select table with copy / export.

    columns: list of (key, heading, width, anchor)  e.g. ("name", "Name", 200, "w")
    rows are added with add(values_dict_or_list, iid=None, tags=())
    """

    def __init__(self, parent: tk.Misc, columns: list[tuple], height: int = 10, select: str = "extended",
                 on_select: Callable[[list[str]], None] | None = None,
                 on_double: Callable[[str], None] | None = None, searchable: bool = False,
                 stretch: str | None = None) -> None:
        super().__init__(parent)
        self.columns = columns
        self.keys = [c[0] for c in columns]
        self._rows: dict[str, list] = {}
        self._order: list[str] = []
        self._sort: tuple[str, bool] | None = None
        self._query = tk.StringVar()
        self._counter = 0
        self._filter_fn: Callable[[list], bool] | None = None
        self.on_select, self.on_double = on_select, on_double
        if searchable:
            bar = ttk.Frame(self)
            bar.pack(fill="x", pady=(0, 4))
            ttk.Label(bar, text="🔍").pack(side="left")
            e = ttk.Entry(bar, textvariable=self._query)
            e.pack(side="left", fill="x", expand=True, padx=4)
            self._query.trace_add("write", lambda *_: self.refresh())
            self.count_label = ttk.Label(bar, style="Muted.TLabel")
            self.count_label.pack(side="right")
        else:
            self.count_label = None
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=self.keys, show="headings", selectmode=select, height=height)
        ys = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        for key, head, width, *rest in columns:
            self.tree.heading(key, text=head, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor=(rest[0] if rest else "w"), stretch=(key == (stretch or self.keys[-1])))
        bind_wheel(self.tree)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select and self.on_select(list(self.tree.selection())))
        self.tree.bind("<Double-1>", self._double)
        self.tree.bind("<Return>", lambda e: self.on_double and self.tree.selection() and self.on_double(self.tree.selection()[0]))
        self.tree.bind("<Control-a>", lambda e: (self.tree.selection_set(self.tree.get_children()), "break")[1])
        self.tree.bind("<Control-c>", lambda e: (self.copy_selected(), "break")[1])
        self.menu = tk.Menu(self.tree, tearoff=False)
        self.menu.add_command(label="Copy selected rows", command=self.copy_selected)
        self.menu.add_command(label="Copy all rows", command=lambda: self.copy_selected(all_rows=True))
        self.menu.add_command(label="Export to CSV…", command=self.export_csv)
        self.tree.bind("<Button-3>", self._popup)
        self.tree.bind("<Button-2>", self._popup)
        app = app_of(self)
        self._tags_painted = False

    # -- content
    def tag_colors(self, **tags: str) -> None:
        """tag_colors(ok="ok", fail="err") -> maps tag -> theme colour name."""
        app = app_of(self)
        for tag, colour in tags.items():
            hexcol = app.theme.p.get(colour, colour) if app else colour
            self.tree.tag_configure(tag, foreground=hexcol)

    def clear(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        self._order.clear()
        self._update_count()

    def add(self, values, iid: str | None = None, tags: Iterable[str] = ()) -> str:
        row = [values.get(k, "") for k in self.keys] if isinstance(values, dict) else list(values)
        self._counter += 1
        iid = iid or f"r{self._counter}"
        self._rows[iid] = (row, tuple(tags))
        if iid not in self._order:
            self._order.append(iid)
        self._insert(iid)
        self._update_count()
        return iid

    def _insert(self, iid: str) -> None:
        row, tags = self._rows[iid]
        if self._matches(row):
            if self.tree.exists(iid):
                self.tree.item(iid, values=row, tags=tags)
            else:
                self.tree.insert("", "end", iid=iid, values=row, tags=tags)

    def update_row(self, iid: str, values=None, tags: Iterable[str] | None = None) -> None:
        if iid not in self._rows:
            return
        row, old_tags = self._rows[iid]
        if values is not None:
            row = [values.get(k, row[i]) for i, k in enumerate(self.keys)] if isinstance(values, dict) else list(values)
        self._rows[iid] = (row, tuple(tags) if tags is not None else old_tags)
        if self.tree.exists(iid):
            self.tree.item(iid, values=row, tags=self._rows[iid][1])
        else:
            self._insert(iid)

    def set_cell(self, iid: str, key: str, value) -> None:
        if iid in self._rows:
            row, tags = self._rows[iid]
            row[self.keys.index(key)] = value
            self.update_row(iid, row)

    def remove(self, iids: Iterable[str]) -> None:
        for iid in list(iids):
            self._rows.pop(iid, None)
            if iid in self._order:
                self._order.remove(iid)
            if self.tree.exists(iid):
                self.tree.delete(iid)
        self._update_count()

    def get(self, iid: str) -> dict:
        row = self._rows[iid][0]
        return dict(zip(self.keys, row))

    def selection(self) -> list[str]:
        return list(self.tree.selection())

    def all_ids(self) -> list[str]:
        return list(self.tree.get_children())

    def set_filter(self, fn: Callable[[list], bool] | None) -> None:
        self._filter_fn = fn
        self.refresh()

    def _matches(self, row: list) -> bool:
        if self._filter_fn and not self._filter_fn(row):
            return False
        q = self._query.get().strip().lower()
        return not q or any(q in str(c).lower() for c in row)

    def refresh(self) -> None:
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        order = list(self._order)
        if self._sort:
            idx = self.keys.index(self._sort[0])
            order.sort(key=lambda i: self._sort_key(self._rows[i][0][idx]), reverse=self._sort[1])
        for iid in order:
            self._insert(iid)
        keep = [i for i in sel if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        self._update_count()

    @staticmethod
    def _sort_key(v):
        s = str(v).strip().rstrip("%")
        try:
            return (0, float(s.replace(",", "")))
        except ValueError:
            return (1, s.lower())

    def sort_by(self, key: str) -> None:
        desc = self._sort == (key, False)
        self._sort = (key, desc)
        for k, head, *_ in self.columns:
            self.tree.heading(k, text=head + (" ▼" if desc and k == key else " ▲" if k == key else ""))
        self.refresh()

    def _update_count(self) -> None:
        if self.count_label is not None:
            self.count_label.configure(text=f"{len(self.tree.get_children())} of {len(self._rows)}")

    def _double(self, e) -> None:
        iid = self.tree.identify_row(e.y)
        if iid and self.on_double:
            self.on_double(iid)

    def _popup(self, e) -> None:
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.menu.tk_popup(e.x_root, e.y_root)

    # -- export
    def to_rows(self, all_rows: bool = False) -> list[list]:
        ids = self.tree.get_children() if (all_rows or not self.tree.selection()) else self.tree.selection()
        return [[c[1] for c in self.columns]] + \
               [list(self._rows[i][0]) for i in ids if i in self._rows]

    def copy_selected(self, all_rows: bool = False) -> None:
        rows = self.to_rows(all_rows)
        copy_to_clipboard(self, "\n".join("\t".join(str(c) for c in r) for r in rows))

    def export_csv(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            parent=self.winfo_toplevel())
        if path:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                csv.writer(fh).writerows(self.to_rows(all_rows=True))


# --------------------------------------------------------------------------- dialogs
def confirm(parent: tk.Misc, message: str, title: str = "Please confirm") -> bool:
    return messagebox.askyesno(title, message, parent=parent.winfo_toplevel())


def info(parent: tk.Misc, message: str, title: str = "Utils Hub") -> None:
    messagebox.showinfo(title, message, parent=parent.winfo_toplevel())


def error(parent: tk.Misc, message: str, title: str = "Utils Hub") -> None:
    messagebox.showerror(title, message, parent=parent.winfo_toplevel())


def ask_text(parent: tk.Misc, title: str, prompt: str, initial: str = "", multiline: bool = False) -> str | None:
    """Small modal text prompt. Returns None when cancelled."""
    top = tk.Toplevel(parent)
    top.title(title)
    top.transient(parent.winfo_toplevel())
    top.resizable(False, False)
    frame = ttk.Frame(top, padding=14)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=prompt, wraplength=420, justify="left").pack(anchor="w", pady=(0, 6))
    result: dict[str, Any] = {"v": None}
    if multiline:
        entry = tk.Text(frame, width=56, height=8)
        style_text(parent, entry)
        entry.insert("1.0", initial)
        getter = lambda: entry.get("1.0", "end").strip()
    else:
        var = tk.StringVar(value=initial)
        entry = ttk.Entry(frame, textvariable=var, width=56)
        getter = var.get
    entry.pack(fill="x")
    entry.focus_set()

    def ok(_e=None):
        result["v"] = getter()
        top.destroy()

    row = ttk.Frame(frame)
    row.pack(fill="x", pady=(10, 0))
    ttk.Button(row, text="OK", style="Accent.TButton", command=ok).pack(side="right")
    ttk.Button(row, text="Cancel", command=top.destroy).pack(side="right", padx=6)
    top.bind("<Escape>", lambda e: top.destroy())
    if not multiline:
        top.bind("<Return>", ok)
    top.update_idletasks()
    px, py = parent.winfo_toplevel().winfo_rootx(), parent.winfo_toplevel().winfo_rooty()
    top.geometry(f"+{px + 80}+{py + 80}")
    top.grab_set()
    top.wait_window()
    return result["v"]


def modal(parent: tk.Misc, title: str, width: int = 520, height: int = 0) -> tuple[tk.Toplevel, ttk.Frame]:
    """A themed modal window; returns (window, content frame)."""
    top = tk.Toplevel(parent)
    top.title(title)
    top.transient(parent.winfo_toplevel())
    app = app_of(parent)
    if app:
        top.configure(background=app.theme.p["bg"])
        top.app = app
    frame = ttk.Frame(top, padding=14)
    frame.pack(fill="both", expand=True)
    top.update_idletasks()
    root = parent.winfo_toplevel()
    h = height or top.winfo_reqheight()
    x = root.winfo_rootx() + max(20, (root.winfo_width() - width) // 2)
    y = root.winfo_rooty() + max(20, (root.winfo_height() - h) // 3)
    top.geometry(f"{width}x{h}+{x}+{y}" if height else f"+{x}+{y}")
    return top, frame


class Toast:
    """A message that fades after a moment (bottom-right of the main window)."""

    def __init__(self, root: tk.Tk, theme) -> None:
        self.root, self.theme = root, theme
        self._win: tk.Toplevel | None = None
        self._job = None

    def show(self, message: str, level: str = "info", ms: int = 3500) -> None:
        if self._win is not None:
            self._win.destroy()
        if self._job:
            self.root.after_cancel(self._job)
        p = self.theme.p
        color = {"ok": p["ok"], "warn": p["warn"], "error": p["err"]}.get(level, p["accent"])
        win = tk.Toplevel(self.root)
        win.wm_overrideredirect(True)
        win.attributes("-topmost", True)
        frame = tk.Frame(win, background=p["surface"], highlightbackground=color, highlightthickness=2)
        frame.pack()
        tk.Label(frame, text=message, background=p["surface"], foreground=p["fg"], padx=14, pady=8,
                 wraplength=380, justify="left").pack()
        win.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() - win.winfo_width() - 24
        y = self.root.winfo_rooty() + self.root.winfo_height() - win.winfo_height() - 48
        win.geometry(f"+{x}+{y}")
        self._win = win
        self._job = self.root.after(ms, self._close)

    def _close(self) -> None:
        if self._win is not None:
            self._win.destroy()
            self._win = None
