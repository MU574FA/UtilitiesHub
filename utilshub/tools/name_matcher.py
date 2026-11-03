"""Name Matcher: compare two lists of names (spelling, word order, extra words, Arabic variants)."""
from __future__ import annotations

import re
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import filedialog, ttk
from typing import Callable

from ..core.matching import find_internal_duplicates, match_lists, one_to_one, status_for
from ..core.schema import Opt
from ..core.sheets import Book, SheetError, Table, cell_text, guess_name_column, load_book, write_csv, write_xlsx
from ..ui.widgets import DataTable, FolderEntry, error, info, modal, style_text
from .base import Command, Tool

STATUS_LABEL = {"strong": "Strong", "probable": "Probable", "weak": "Weak", "none": "No match", "manual": "Manual"}


# --------------------------------------------------------------------------- reusable picker
class SheetPicker(ttk.LabelFrame):
    """File + sheet + header row + column chooser.  Also used by Sheet Sync.

    picker.table() -> Table | None;  picker.column() -> selected column name
    on_load(table) is called after a file (or sheet / header row) is (re)loaded.
    """

    def __init__(self, parent, title: str, on_load: Callable[[Table], None] | None = None,
                 column_label: str = "Column", pick_column: bool = True, path: str = "") -> None:
        super().__init__(parent, text=title, padding=8)
        self.on_load = on_load
        self.book: Book | None = None
        self._table: Table | None = None
        self.path = tk.StringVar(value=path)
        self.sheet = tk.StringVar()
        self.header = tk.IntVar(value=1)
        self.col = tk.StringVar()
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="File").grid(row=0, column=0, sticky="w")
        FolderEntry(self, self.path, "file", [("Spreadsheets", "*.xlsx *.xlsm *.csv *.tsv *.txt"), ("All", "*.*")],
                    width=30).grid(row=0, column=1, sticky="ew", pady=2)
        row2 = ttk.Frame(self)
        row2.grid(row=1, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(row2, text="Sheet").pack(side="left")
        self.sheet_cb = ttk.Combobox(row2, textvariable=self.sheet, state="readonly", width=16)
        self.sheet_cb.pack(side="left", padx=4)
        ttk.Label(row2, text="Header row").pack(side="left", padx=(8, 2))
        ttk.Spinbox(row2, from_=1, to=50, width=3, textvariable=self.header, command=self._reload_table).pack(side="left")
        self.col_cb = None
        if pick_column:
            row3 = ttk.Frame(self)
            row3.grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)
            ttk.Label(row3, text=column_label).pack(side="left")
            self.col_cb = ttk.Combobox(row3, textvariable=self.col, state="readonly", width=28)
            self.col_cb.pack(side="left", padx=4)
        self.info = ttk.Label(self, style="Muted.TLabel")
        self.info.grid(row=3, column=0, columnspan=2, sticky="w")
        self.path.trace_add("write", lambda *_: self._debounce())
        self.sheet_cb.bind("<<ComboboxSelected>>", lambda e: self._reload_table())
        self._after = None
        if path:
            self.after(10, self.load)

    def _debounce(self) -> None:
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(400, self.load)

    def load(self) -> None:
        p = self.path.get().strip().strip('"')
        self.book = self._table = None
        if not p:
            return
        try:
            self.book = load_book(p)
        except SheetError as exc:
            self.info.configure(text=str(exc))
            return
        self.sheet_cb.configure(values=self.book.names)
        self.sheet.set(self.book.names[0] if self.book.names else "")
        self._reload_table()

    def _reload_table(self) -> None:
        if not self.book:
            return
        try:
            self._table = self.book.table(self.sheet.get(), max(0, self.header.get() - 1))
        except (tk.TclError, ValueError):
            return
        t = self._table
        if self.col_cb is not None:
            self.col_cb.configure(values=t.headers)
            if self.col.get() not in t.headers and t.headers:
                self.col.set(t.headers[guess_name_column(t.headers)])
        self.info.configure(text=f"{len(t.rows)} rows • {len(t.headers)} columns")
        if self.on_load:
            self.on_load(t)

    def table(self) -> Table | None:
        return self._table

    def column(self) -> str:
        return self.col.get()

    def values(self) -> list[str]:
        t = self._table
        if not t or self.col.get() not in t.headers:
            return []
        return [cell_text(v).strip() for v in t.column(self.col.get())]


# --------------------------------------------------------------------------- logic (tested)
def split_typed_names(text: str) -> list[str]:
    """Names typed or pasted into the box: one per line (or separated by ';'), blanks dropped."""
    return [n.strip() for n in re.split(r"[\n;]+", text or "") if n.strip()]


@dataclass
class MatchRow:
    a_index: int
    a_name: str
    candidates: list[tuple[int, float]]
    chosen: int | None = None           # index into B
    score: float = 0.0
    manual: bool = False

    def status(self, threshold: float, strong: float) -> str:
        if self.chosen is None:
            return "none"
        if self.manual:
            return "manual"
        return status_for(self.score, threshold, strong)


def build_rows(a_names: list[str], b_names: list[str], results: list[list[tuple[int, float]]], threshold: float,
               unique: bool, old: dict[int, MatchRow] | None = None) -> list[MatchRow]:
    """Turn raw candidates into rows with the chosen match (keeps manual overrides from `old`)."""
    rows = [MatchRow(i, n, c) for i, (n, c) in enumerate(zip(a_names, results))]
    pairs = one_to_one(results, threshold) if unique else {
        i: c[0] for i, c in enumerate(results) if c and c[0][1] >= threshold}
    for r in rows:
        if old and r.a_index in old and old[r.a_index].manual:
            r.chosen, r.score, r.manual = old[r.a_index].chosen, old[r.a_index].score, True
        elif r.a_index in pairs:
            r.chosen, r.score = pairs[r.a_index]
    return rows


class NameMatcher(Tool):
    id = "name_matcher"
    title = "Name Matcher"
    icon = "🧑‍🤝‍🧑"
    order = 40
    description = "Compare two name lists, or type a few names and find them in one file. Typos, word order, Arabic spelling."

    @classmethod
    def options(cls) -> list[Opt]:
        p = "name_matcher."
        return [
            Opt(p + "threshold", 80, "A match counts from (%)", "percent", min=40, max=100),
            Opt(p + "strong", 92, "'Strong' match from (%)", "percent", min=50, max=100),
            Opt(p + "one_to_one", True, "Each name in list B can be used only once", "bool"),
            Opt(p + "top_n", 3, "Candidates kept per name", "int", min=1, max=10),
            Opt(p + "a_path", "", "Last file A", "file", advanced=True),
            Opt(p + "b_path", "", "Last file B", "file", advanced=True),
            Opt(p + "a_mode", "file", "List A comes from", "choice", advanced=True,
                choices=[("file", "A file"), ("text", "Typed / pasted names")]),
            Opt(p + "a_text", "", "Last typed names", "text", advanced=True),
            Opt(p + "show", "all", "Show", "choice", choices=[("all", "All"), ("matched", "Matched"),
                                                              ("unmatched", "Unmatched"), ("review", "Needs review")],
                advanced=True),
            *cls.append_opts(),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.rows: list[MatchRow] = []
        self.a: list[str] = []
        self.b: list[str] = []
        self.results: list[list[tuple[int, float]]] = []

    def build(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)
        a_box = ttk.Frame(top)
        a_box.grid(row=0, column=0, sticky="new", padx=(0, 6))
        self.a_mode = tk.StringVar(value=self.get("a_mode"))
        modes = ttk.Frame(a_box)
        modes.pack(fill="x")
        for text, v in (("Names from a file", "file"), ("Type / paste names (no file needed)", "text")):
            ttk.Radiobutton(modes, text=text, value=v, variable=self.a_mode, command=self._a_mode_changed).pack(side="left", padx=(0, 10))
        self.pa = SheetPicker(a_box, "List A (the names to check)", column_label="Name column", path=self.get("a_path"))
        self.a_text_box = ttk.LabelFrame(a_box, text="Names to check - one per line (or separated by ;)", padding=8)
        self.a_text = tk.Text(self.a_text_box, height=6, width=40, wrap="word", undo=True)
        style_text(self.frame, self.a_text)
        self.a_text.pack(fill="both", expand=True)
        self.a_text.insert("1.0", self.get("a_text"))
        self.a_text.bind("<<Modified>>", self._a_text_modified)
        self.pb = SheetPicker(top, "List B (the reference list / the file to search in)", column_label="Name column", path=self.get("b_path"))
        self.pb.grid(row=0, column=1, sticky="new")
        self._a_mode_changed()
        self.pa.path.trace_add("write", lambda *_: self.put("a_path", self.pa.path.get()))
        self.pb.path.trace_add("write", lambda *_: self.put("b_path", self.pb.path.get()))

        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=6)
        self.th = tk.IntVar(value=self.get("threshold"))
        self.uniq = tk.BooleanVar(value=self.get("one_to_one"))
        ttk.Button(bar, text="▶ Compare  (F5)", style="Accent.TButton", command=self.compare).pack(side="left")
        ttk.Label(bar, text="Threshold %").pack(side="left", padx=(10, 2))
        ttk.Spinbox(bar, from_=40, to=100, width=4, textvariable=self.th, command=self.refilter).pack(side="left")
        ttk.Checkbutton(bar, text="One-to-one", variable=self.uniq, command=self.rebuild).pack(side="left", padx=8)
        self.th.trace_add("write", lambda *_: self._th_changed())
        self.show_var = tk.StringVar(value="all")
        for text, v in (("All", "all"), ("Matched", "matched"), ("Unmatched", "unmatched"), ("Review", "review")):
            ttk.Radiobutton(bar, text=text, value=v, variable=self.show_var, command=self.refilter).pack(side="left", padx=2)
        ttk.Button(bar, text="Export…  (Ctrl+E)", command=self.export).pack(side="right")
        ttk.Button(bar, text="Add to file…", command=self.add_to_file).pack(side="right", padx=4)
        ttk.Button(bar, text="Duplicates in A", command=self.duplicates).pack(side="right", padx=4)
        self.summary = ttk.Label(parent, style="Muted.TLabel")
        self.summary.pack(anchor="w")

        self.table = DataTable(parent, [("n", "#", 40, "e"), ("a", "Name in A", 240), ("b", "Best match in B", 240),
                                        ("score", "Score", 60, "e"), ("status", "Status", 80),
                                        ("alt", "Other candidates", 300)], searchable=True,
                               on_double=self.choose, stretch="alt")
        self.table.pack(fill="both", expand=True)
        self.table.tag_colors(strong="ok", probable="info", weak="warn", none="err", manual="accent")
        m = self.table.menu
        m.add_separator()
        m.add_command(label="Choose match manually…", command=lambda: self.table.selection() and self.choose(self.table.selection()[0]))
        m.add_command(label="Mark as 'no match'", command=self.clear_choice)
        ttk.Label(parent, text="Double-click a row to pick the match yourself.", style="Muted.TLabel").pack(anchor="w")

    # ------------------------------------------------------------------ list A: file or typed
    def _a_mode_changed(self) -> None:
        self.put("a_mode", self.a_mode.get())
        if self.a_mode.get() == "text":
            self.pa.pack_forget()
            self.a_text_box.pack(fill="x", pady=(4, 0))
        else:
            self.a_text_box.pack_forget()
            self.pa.pack(fill="x", pady=(4, 0))

    def _a_text_modified(self, _e=None) -> None:
        if self.a_text.edit_modified():
            self.put("a_text", self.a_text.get("1.0", "end-1c"))
            self.a_text.edit_modified(False)

    def a_values(self) -> list[str]:
        """Names of list A: typed / pasted in the box, or the chosen column of file A."""
        if self.a_mode.get() == "text":
            return split_typed_names(self.a_text.get("1.0", "end-1c"))
        return self.pa.values()

    # ------------------------------------------------------------------ run
    def _th_changed(self) -> None:
        try:
            v = int(self.th.get())
        except (tk.TclError, ValueError):
            return
        self.put("threshold", v)
        self.refilter()

    def compare(self) -> None:
        self.a, self.b = self.a_values(), self.pb.values()
        if not self.a or not self.b:
            info(self.frame, "Give the names to check (a file or typed names) and load list B with its name column.")
            return
        top = self.get("top_n")
        a, b = self.a, self.b

        def work(job):
            return match_lists(a, b, top_n=top, floor=0.4, progress=lambda d, t: job.progress(d, t, "Comparing names"),
                               cancelled=lambda: job.cancelled)

        def done(res):
            self.results = res
            self.rows = build_rows(self.a, self.b, res, self.th.get() / 100, self.uniq.get())
            self.refilter()
            self.log(f"Compared {len(a)} × {len(b)} names", "ok")

        self.app.jobs.submit(work, "Comparing names", self.id, on_done=done, on_error=lambda m: error(self.frame, m),
                             serial_key=self.id)

    def rebuild(self) -> None:
        if self.results:
            old = {r.a_index: r for r in self.rows}
            self.rows = build_rows(self.a, self.b, self.results, self.th.get() / 100, self.uniq.get(), old)
            self.refilter()

    def _strong(self) -> float:
        return self.get("strong") / 100

    def refilter(self) -> None:
        if not self.rows:
            return
        try:
            th = self.th.get() / 100
        except (tk.TclError, ValueError):
            return
        if not self.table.winfo_exists():
            return
        # a threshold change re-decides non-manual rows
        if any(not r.manual and ((r.chosen is None) != (not r.candidates or r.candidates[0][1] < th)) for r in self.rows):
            self.rebuild_silent(th)
        mode = self.show_var.get()
        self.table.clear()
        counts = {"strong": 0, "probable": 0, "weak": 0, "none": 0, "manual": 0}
        for r in self.rows:
            st = r.status(th, self._strong())
            counts[st] = counts.get(st, 0) + 1
            if mode == "matched" and r.chosen is None or mode == "unmatched" and r.chosen is not None \
                    or mode == "review" and st not in ("probable", "weak", "none"):
                continue
            best = self.b[r.chosen] if r.chosen is not None else (f"({self.b[r.candidates[0][0]]}?)" if r.candidates else "")
            alt = " | ".join(f"{self.b[j]} ({s * 100:.0f}%)" for j, s in r.candidates if j != r.chosen)
            self.table.add({"n": r.a_index + 1, "a": r.a_name, "b": best,
                            "score": f"{r.score * 100:.0f}%" if r.chosen is not None else "",
                            "status": STATUS_LABEL[st], "alt": alt}, iid=f"a{r.a_index}", tags=(st,))
        self.summary.configure(text=" • ".join(f"{STATUS_LABEL[k]} {v}" for k, v in counts.items() if v) +
                                    f" • {len(self.rows)} names")

    def rebuild_silent(self, th: float) -> None:
        old = {r.a_index: r for r in self.rows}
        self.rows = build_rows(self.a, self.b, self.results, th, self.uniq.get(), old)

    # ------------------------------------------------------------------ manual
    def _row(self, iid: str) -> MatchRow:
        return self.rows[int(iid[1:])]

    def choose(self, iid: str) -> None:
        r = self._row(iid)
        win, f = modal(self.frame, f"Match for: {r.a_name}", 520, 420)
        ttk.Label(f, text="Pick the right name from list B (type to search):").pack(anchor="w")
        q = tk.StringVar()
        ttk.Entry(f, textvariable=q).pack(fill="x", pady=4)
        lb = tk.Listbox(f, activestyle="none", height=12)
        self.app.theme.style_listbox(lb)
        lb.pack(fill="both", expand=True)
        shown: list[int] = []

        def fill(*_):
            lb.delete(0, "end")
            shown.clear()
            text = q.get().strip().lower()
            pool = [j for j, _ in r.candidates] if not text else [j for j, n in enumerate(self.b) if text in n.lower()][:300]
            for j in pool:
                shown.append(j)
                sc = dict(r.candidates).get(j)
                lb.insert("end", self.b[j] + (f"   ({sc * 100:.0f}%)" if sc else ""))
            if shown:
                lb.selection_set(0)

        q.trace_add("write", fill)
        fill()

        def ok(_e=None):
            sel = lb.curselection()
            if sel:
                j = shown[sel[0]]
                r.chosen, r.manual = j, True
                r.score = dict(r.candidates).get(j, 0.0)
                win.destroy()
                self.refilter()

        lb.bind("<Double-1>", ok)
        bb = ttk.Frame(f)
        bb.pack(fill="x", pady=(8, 0))
        ttk.Button(bb, text="Use this", style="Accent.TButton", command=ok).pack(side="right")
        ttk.Button(bb, text="No match", command=lambda: (self._none(r), win.destroy())).pack(side="right", padx=6)

    def _none(self, r: MatchRow) -> None:
        r.chosen, r.score, r.manual = None, 0.0, True
        self.refilter()

    def clear_choice(self) -> None:
        for iid in self.table.selection():
            r = self._row(iid)
            r.chosen, r.score, r.manual = None, 0.0, True
        self.refilter()

    # ------------------------------------------------------------------ extras
    def duplicates(self) -> None:
        names = self.a_values()
        if not names:
            info(self.frame, "Give the names of list A first.")
            return
        th = self.th.get() / 100

        def work(job):
            return find_internal_duplicates(names, max(th, 0.8))

        def done(pairs):
            win, f = modal(self.frame, "Probable duplicates inside list A", 640, 420)
            t = DataTable(f, [("a", "Name", 250), ("b", "Looks like", 250), ("s", "Score", 60, "e")], stretch="b")
            t.pack(fill="both", expand=True)
            for i, j, s in pairs:
                t.add([names[i], names[j], f"{s * 100:.0f}%"])
            if not pairs:
                ttk.Label(f, text="No duplicates found.").pack()

        self.app.jobs.submit(work, "Finding duplicates", self.id, on_done=done, on_error=lambda m: error(self.frame, m))

    def export_rows(self) -> tuple[list[str], list[list]]:
        th = self.th.get() / 100
        t = self.pa.table()
        headers = ["Name in A", "Matched name in B", "Score %", "Status"]
        rows = [[r.a_name, self.b[r.chosen] if r.chosen is not None else "", round(r.score * 100) if r.chosen is not None else "",
                 STATUS_LABEL[r.status(th, self._strong())]] for r in self.rows]
        return headers, rows

    def export(self) -> None:
        if not self.rows:
            info(self.frame, "Compare the lists first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", initialfile="name-matches.xlsx",
                                            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if not path:
            return
        headers, rows = self.export_rows()
        th = self.th.get() / 100
        matched_b = {r.chosen for r in self.rows if r.chosen is not None}
        only_b = [[n] for j, n in enumerate(self.b) if j not in matched_b]
        if path.lower().endswith(".csv"):
            write_csv(path, [headers] + rows)
        else:
            write_xlsx(path, {"Matches": [headers] + rows,
                              "Only in A": [["Name"]] + [[r[0]] for r in rows if not r[1]],
                              "Only in B": [["Name"]] + only_b})
        self.log(f"Exported to {path}", "ok")

    def add_to_file(self) -> None:
        """Append the current matches to the user's CSV (created when it does not exist)."""
        if not self.rows:
            info(self.frame, "Compare the lists first.")
            return
        headers, rows = self.export_rows()
        self.append_rows([dict(zip(headers, r)) for r in rows], ask=True)

    def accept_files(self, paths: list[str]) -> None:
        if len(paths) == 1 and self.a_mode.get() == "text":      # typed names + one file: the file is list B
            self.pb.path.set(paths[0])
            return
        for p, picker in zip(paths[:2], (self.pa, self.pb)):
            picker.path.set(p)

    def keys(self) -> dict:
        return {"<F5>": self.compare, "<Control-Return>": self.compare, "<Control-e>": self.export}
    def commands(self) -> list[Command]:
        return [Command("nm.compare", "Name Matcher: compare lists", self.compare, "Name Matcher")]
