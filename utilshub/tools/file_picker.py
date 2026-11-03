"""File Picker: find the files that belong to a list of names, preview the result, then copy / move them."""
from __future__ import annotations

import hashlib
import re
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, ttk

from ..core.fileops import transfer
from ..core.fuzzy import name_score, prep_name
from ..core.schema import Opt, Page
from ..core.sheets import SheetError, cell_text, load_book, write_xlsx
from ..core.system import open_path, reveal
from ..core.textutil import human_size, normalise_name, parse_ext_list, split_list
from ..ui.dnd import make_drop_target
from ..ui.widgets import DataTable, FolderEntry, ask_text, confirm, error, info, style_text
from .base import Command, Tool

MODES = [("exact", "Exact (file name = name)"), ("starts", "File name starts with the name"),
         ("contains", "File name contains the name"), ("regex", "Each line is a regex"),
         ("fuzzy", "Fuzzy (typos, word order, Arabic spelling)")]


# --------------------------------------------------------------------------- pure logic (tested)
@dataclass
class Hit:
    name: str
    path: Path
    score: float = 1.0
    selected: bool = True


def list_files(folder: str | Path, recursive: bool, exts: set[str]) -> list[Path]:
    root = Path(folder)
    it = root.rglob("*") if recursive else root.glob("*")
    out = []
    for p in it:
        try:
            if p.is_file() and (not exts or p.suffix.lower() in exts):
                out.append(p)
        except OSError:
            continue
    return sorted(out)


def find_hits(names: list[str], files: list[Path], mode: str = "contains", ignore_case: bool = True,
              threshold: float = 0.8, use_fullname: bool = False, cancelled=None) -> dict[str, list[Hit]]:
    """name -> hits (best first).  Names with no hit map to []."""
    def key(p: Path) -> str:
        return p.name if use_fullname else p.stem

    out: dict[str, list[Hit]] = {n: [] for n in names}
    if mode == "regex":
        flags = re.I if ignore_case else 0
        for n in names:
            try:
                rx = re.compile(n, flags)
            except re.error:
                continue
            out[n] = [Hit(n, f) for f in files if rx.search(key(f))]
        return out
    norm = [(f, normalise_name(key(f))) for f in files]
    if mode == "fuzzy":
        pf = [(f, prep_name(key(f))) for f in files]
        for n in names:
            if cancelled and cancelled():
                break
            pn = prep_name(n)
            if not pn.s:
                continue
            hits = [Hit(n, f, s) for f, p in pf if (s := name_score(pn, p)) >= threshold]
            out[n] = sorted(hits, key=lambda h: -h.score)
        return out
    for n in names:
        nn = normalise_name(n) if ignore_case else n
        if not nn:
            continue
        for f, fn in norm:
            hay = fn if ignore_case else key(f)
            if (mode == "exact" and hay == nn) or (mode == "starts" and hay.startswith(nn)) \
                    or (mode == "contains" and nn in hay):
                out[n].append(Hit(n, f))
    return out


def find_duplicates(files: list[Path], cancelled=None) -> list[list[Path]]:
    """Groups of byte-identical files (size first, then SHA-1)."""
    by_size: dict[int, list[Path]] = {}
    for f in files:
        try:
            by_size.setdefault(f.stat().st_size, []).append(f)
        except OSError:
            pass
    groups: list[list[Path]] = []
    for size, same in by_size.items():
        if len(same) < 2 or size == 0:
            continue
        by_hash: dict[str, list[Path]] = {}
        for f in same:
            if cancelled and cancelled():
                return groups
            h = hashlib.sha1()
            try:
                with open(f, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                continue
            by_hash.setdefault(h.hexdigest(), []).append(f)
        groups += [g for g in by_hash.values() if len(g) > 1]
    return groups


# --------------------------------------------------------------------------- the tool
class FilePicker(Tool):
    id = "file_picker"
    title = "File Picker"
    icon = "🗂"
    order = 30
    description = "Find the files that belong to a list of names, preview, then copy or move them."

    @classmethod
    def options(cls) -> list[Opt]:
        p = "file_picker."
        return [
            Opt(p + "source", "", "Source folder", "folder", section="Folders"),
            Opt(p + "dest", "", "Destination folder", "folder", section="Folders"),
            Opt(p + "recursive", True, "Search sub-folders too", "bool", section="Search"),
            Opt(p + "extensions", "", "Only these extensions (e.g. pdf, docx; empty = all)", "str", section="Search"),
            Opt(p + "mode", "contains", "Match mode", "choice", choices=MODES, section="Search"),
            Opt(p + "threshold", 85, "Fuzzy similarity (%)", "percent", min=50, max=100, section="Search"),
            Opt(p + "ignore_case", True, "Ignore case, accents and Arabic letter variants", "bool", section="Search"),
            Opt(p + "use_fullname", False, "Match against the full file name (with extension)", "bool", section="Search",
                advanced=True),
            Opt(p + "action", "copy", "Action", "choice", choices=[("copy", "Copy"), ("move", "Move")], section="Result"),
            Opt(p + "policy", "number", "If the file already exists", "choice",
                choices=[("number", "Keep both (add a number)"), ("overwrite", "Overwrite"), ("skip", "Skip")],
                section="Result"),
            Opt(p + "subfolder_per_name", False, "Put each name's files in its own sub-folder", "bool", section="Result"),
            Opt(p + "names", "", "Last list of names", "text", advanced=True),
            *cls.append_opts(),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.hits: dict[str, list[Hit]] = {}
        self.rows: dict[str, Hit] = {}
        self.missing: list[str] = []

    # ------------------------------------------------------------------ UI
    def build(self, parent: ttk.Frame) -> None:
        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=(0, 0, 8, 0))
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=3)

        ttk.Label(left, text="Names (one per line)", style="H3.TLabel").pack(anchor="w")
        self.names_text = tk.Text(left, width=30, height=14, wrap="none", undo=True)
        style_text(left, self.names_text)
        self.names_text.pack(fill="both", expand=True, pady=4)
        self.names_text.insert("1.0", self.get("names"))
        bar = ttk.Frame(left)
        bar.pack(fill="x")
        ttk.Button(bar, text="Paste", style="Tiny.TButton", command=self.paste_names).pack(side="left")
        ttk.Button(bar, text="From file / sheet…", style="Tiny.TButton", command=self.names_from_file).pack(side="left", padx=4)
        ttk.Button(bar, text="Clear", style="Tiny.TButton", command=lambda: self.names_text.delete("1.0", "end")).pack(side="left")
        self.count_lbl = ttk.Label(left, style="Muted.TLabel")
        self.count_lbl.pack(anchor="w")
        self.names_text.bind("<<Modified>>", self._names_modified)

        form = ttk.Frame(right)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.vars: dict[str, tk.Variable] = {}

        def var(key, kind=tk.StringVar):
            v = kind(value=self.get(key))
            v.trace_add("write", lambda *_a, k=key, vv=v: self.put(k, vv.get()))
            self.vars[key] = v
            return v

        row = 0
        for label, key in (("Source folder", "source"), ("Destination", "dest")):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2)
            FolderEntry(form, var(key), "dir", open_button=True).grid(row=row, column=1, sticky="ew", pady=2)
            row += 1
        opts = ttk.Frame(form)
        opts.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Label(opts, text="Match").pack(side="left")
        self.mode_labels = [l for _, l in MODES]
        mv = tk.StringVar(value=dict(MODES).get(self.get("mode"), MODES[2][1]))
        cb = ttk.Combobox(opts, textvariable=mv, values=self.mode_labels, state="readonly", width=34)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.put("mode", next(k for k, l in MODES if l == mv.get())))
        ttk.Label(opts, text="Fuzzy %").pack(side="left", padx=(8, 2))
        ttk.Spinbox(opts, from_=50, to=100, width=4, textvariable=var("threshold", tk.IntVar)).pack(side="left")
        ttk.Label(opts, text="Extensions").pack(side="left", padx=(8, 2))
        ttk.Entry(opts, width=14, textvariable=var("extensions")).pack(side="left")
        opts2 = ttk.Frame(form)
        opts2.grid(row=row + 1, column=0, columnspan=2, sticky="ew")
        for text, key in (("Sub-folders", "recursive"), ("Ignore case/accents", "ignore_case"),
                          ("Folder per name", "subfolder_per_name")):
            ttk.Checkbutton(opts2, text=text, variable=var(key, tk.BooleanVar)).pack(side="left", padx=(0, 10))
        av = tk.StringVar(value=self.get("action"))
        av.trace_add("write", lambda *_: self.put("action", av.get()))
        ttk.Radiobutton(opts2, text="Copy", value="copy", variable=av).pack(side="left", padx=(10, 2))
        ttk.Radiobutton(opts2, text="Move", value="move", variable=av).pack(side="left")

        act = ttk.Frame(right)
        act.pack(fill="x", pady=6)
        ttk.Button(act, text="🔍 Preview (dry run)", style="Accent.TButton", command=self.preview).pack(side="left")
        self.apply_btn = ttk.Button(act, text="Apply selected", command=self.apply, state="disabled")
        self.apply_btn.pack(side="left", padx=6)
        self.undo_btn = ttk.Button(act, text="↩ Undo last", command=self.undo)
        self.undo_btn.pack(side="left")
        ttk.Button(act, text="Find duplicates in source", command=self.duplicates).pack(side="left", padx=6)
        ttk.Button(act, text="Missing report…", command=self.export_missing).pack(side="right")
        ttk.Button(act, text="Add to file…", command=self.add_to_file).pack(side="right", padx=6)
        self.summary = ttk.Label(right, style="Muted.TLabel")
        self.summary.pack(anchor="w")

        self.table = DataTable(right, [("sel", "✔", 36, "center"), ("name", "Name", 180), ("file", "File", 260),
                                       ("score", "Match", 60, "e"), ("size", "Size", 70, "e"), ("folder", "Folder", 260)],
                               searchable=True, on_double=self.toggle_row, stretch="folder")
        self.table.pack(fill="both", expand=True)
        self.table.tag_colors(off="muted", miss="err")
        m = self.table.menu
        m.add_separator()
        m.add_command(label="Toggle selected ✔", command=lambda: [self.toggle_row(i) for i in self.table.selection()])
        m.add_command(label="Open file", command=lambda: self._each_selected(lambda h: open_path(h.path)))
        m.add_command(label="Show in folder", command=lambda: self._each_selected(lambda h: reveal(h.path)))
        make_drop_target(self.names_text, self._drop_names)
        self._names_modified()

    # ------------------------------------------------------------------ names
    def names(self) -> list[str]:
        seen, out = set(), []
        for line in self.names_text.get("1.0", "end").splitlines():
            line = line.strip()
            if line and line.lower() not in seen:
                seen.add(line.lower())
                out.append(line)
        return out

    def _names_modified(self, _e=None) -> None:
        if self.names_text.edit_modified():
            self.names_text.edit_modified(False)
            self.put("names", self.names_text.get("1.0", "end").strip())
            self.count_lbl.configure(text=f"{len(self.names())} names")

    def set_names(self, names: list[str], append: bool = False) -> None:
        if not append:
            self.names_text.delete("1.0", "end")
        self.names_text.insert("end", "\n".join(names) + "\n")

    def paste_names(self) -> None:
        try:
            self.set_names(split_list(self.app.root.clipboard_get().replace(",", "\n")), append=True)
        except tk.TclError:
            pass

    def names_from_file(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Lists", "*.txt *.csv *.xlsx *.xlsm"), ("All", "*.*")])
        if path:
            self._load_names_file(path)

    def _load_names_file(self, path: str) -> None:
        try:
            if Path(path).suffix.lower() in (".txt",):
                names = [l.strip() for l in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            else:
                tbl = load_book(path).table()
                col = 0
                if len(tbl.headers) > 1:
                    pick = ask_text(self.frame, "Which column?", "Column name:\n" + ", ".join(tbl.headers), tbl.headers[0])
                    if not pick or pick not in tbl.headers:
                        return
                    col = tbl.col(pick)
                names = [cell_text(r[col]).strip() for r in tbl.rows if cell_text(r[col]).strip()]
        except (SheetError, OSError) as exc:
            error(self.frame, str(exc))
            return
        self.set_names(names)

    def _drop_names(self, paths: list[str]) -> None:
        if paths:
            self._load_names_file(paths[0])

    def accept_files(self, paths: list[str]) -> None:
        if paths and Path(paths[0]).is_dir():
            self.vars["source"].set(paths[0])
        elif paths:
            self._load_names_file(paths[0])

    # ------------------------------------------------------------------ preview / apply
    def preview(self) -> None:
        names, src = self.names(), self.get("source")
        if not names:
            info(self.frame, "Add some names first.")
            return
        if not src or not Path(src).is_dir():
            info(self.frame, "Choose an existing source folder.")
            return
        mode, exts = self.get("mode"), parse_ext_list(self.get("extensions"))
        rec, ic = self.get("recursive"), self.get("ignore_case")
        th, full = self.get("threshold") / 100, self.get("use_fullname")

        def work(job):
            files = list_files(src, rec, exts)
            job.progress(0, len(files), f"Searching {len(files)} files")
            return files, find_hits(names, files, mode, ic, th, full, lambda: job.cancelled)

        self.app.jobs.submit(work, "Searching files", self.id, on_done=self._show_hits,
                             on_error=lambda m: error(self.frame, m), serial_key=self.id)

    def _show_hits(self, result) -> None:
        files, hits = result
        self.hits = hits
        self.table.clear()
        self.rows.clear()
        self.missing = [n for n, h in hits.items() if not h]
        used = 0
        for name, hs in hits.items():
            if not hs:
                self.table.add({"sel": "", "name": name, "file": "— not found —"}, tags=("miss",))
            for i, h in enumerate(hs):
                iid = self.table.add(self._row(h))
                self.rows[iid] = h
                used += 1
        found = len(hits) - len(self.missing)
        self.summary.configure(text=f"{len(files)} files scanned • {found}/{len(hits)} names found • "
                                    f"{used} files • {len(self.missing)} missing")
        self.apply_btn.configure(state="normal" if self.rows else "disabled")
        self.log(f"Preview: {found}/{len(hits)} names have files")

    @staticmethod
    def _row(h: Hit) -> dict:
        try:
            size = human_size(h.path.stat().st_size)
        except OSError:
            size = ""
        return {"sel": "✔" if h.selected else "", "name": h.name, "file": h.path.name,
                "score": f"{h.score * 100:.0f}%", "size": size, "folder": str(h.path.parent)}

    def toggle_row(self, iid: str) -> None:
        h = self.rows.get(iid)
        if h:
            h.selected = not h.selected
            self.table.update_row(iid, self._row(h), tags=() if h.selected else ("off",))

    def _each_selected(self, fn) -> None:
        for iid in self.table.selection():
            if iid in self.rows:
                fn(self.rows[iid])

    def apply(self) -> None:
        chosen = [h for h in self.rows.values() if h.selected]
        dest = self.get("dest")
        if not chosen:
            info(self.frame, "Nothing is ticked.")
            return
        if not dest:
            info(self.frame, "Choose a destination folder.")
            return
        action, policy, per = self.get("action"), self.get("policy"), self.get("subfolder_per_name")
        if action == "move" and not confirm(self.frame, f"Move {len(chosen)} files? (You can undo this.)"):
            return
        journal = self.app.journal

        def work(job):
            ok = skipped = 0
            for n, h in enumerate(chosen, 1):
                if job.cancelled:
                    break
                job.progress(n, len(chosen), h.path.name)
                folder = Path(dest) / re.sub(r'[\\/:*?"<>|]+', "_", h.name) if per else Path(dest)
                try:
                    t = transfer(h.path, folder, None, action, policy, journal, self.id, note=h.name)
                except OSError as exc:
                    self.app.bridge.call(self.log, f"{h.path.name}: {exc}", "error")
                    continue
                ok, skipped = ok + (t is not None), skipped + (t is None)
            return ok, skipped

        def done(res):
            self.log(f"{'Moved' if action == 'move' else 'Copied'} {res[0]} files" + (f", skipped {res[1]}" if res[1] else ""), "ok")
            self.app.notify(f"{res[0]} files {'moved' if action == 'move' else 'copied'}", "ok")

        self.app.jobs.submit(work, "Copying files", self.id, on_done=done, on_error=lambda m: error(self.frame, m))

    def undo(self) -> None:
        e = self.app.journal.last_undoable(self.id)
        if not e:
            info(self.frame, "Nothing to undo.")
            return
        ok, why = self.app.journal.can_undo(e)
        if not ok:
            error(self.frame, why)
            return
        self.log(self.app.journal.undo(e), "ok")

    # ------------------------------------------------------------------ extras
    def export_missing(self) -> None:
        if not self.hits:
            info(self.frame, "Run a preview first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", initialfile="file-picker-report.xlsx",
                                            filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        found = [[h.name, h.path.name, f"{h.score * 100:.0f}%", str(h.path.parent)] for hs in self.hits.values() for h in hs]
        write_xlsx(path, {"Missing": [["Name"]] + [[n] for n in self.missing],
                          "Found": [["Name", "File", "Match", "Folder"]] + found})
        self.log(f"Report saved to {path}", "ok")

    def add_to_file(self) -> None:
        """Append every name with the files found for it (and the names with none) to the user's CSV."""
        if not self.hits:
            info(self.frame, "Preview first.")
            return
        recs = []
        for name, hs in self.hits.items():
            if not hs:
                recs.append({"Name": name, "File": "", "Folder": "", "Match %": "", "Found": "no"})
            recs += [{"Name": name, "File": h.path.name, "Folder": str(h.path.parent), "Match %": f"{h.score * 100:.0f}",
                      "Found": "yes"} for h in hs]
        self.append_rows(recs, ask=True)

    def duplicates(self) -> None:
        src = self.get("source")
        if not src or not Path(src).is_dir():
            info(self.frame, "Choose a source folder first.")
            return
        exts, rec = parse_ext_list(self.get("extensions")), self.get("recursive")

        def work(job):
            return find_duplicates(list_files(src, rec, exts), lambda: job.cancelled)

        def done(groups):
            self.table.clear()
            self.rows.clear()
            for gi, g in enumerate(groups, 1):
                for f in g:
                    h = Hit(f"duplicate group {gi}", f, 1.0, selected=f is not g[0])
                    self.rows[self.table.add(self._row(h))] = h
            self.summary.configure(text=f"{len(groups)} groups of identical files. The first of each group is un-ticked (kept).")
            self.apply_btn.configure(state="normal" if self.rows else "disabled")

        self.app.jobs.submit(work, "Finding duplicates", self.id, on_done=done, on_error=lambda m: error(self.frame, m))

    def keys(self) -> dict:
        return {"<F5>": self.preview, "<Control-Return>": self.apply, "<Control-z>": self.undo}

    def commands(self) -> list[Command]:
        return [Command("fp.preview", "File Picker: preview", self.preview, "File Picker"),
                Command("fp.undo", "File Picker: undo last", self.undo, "File Picker")]

    def stats(self) -> dict:
        return {"Names": len(self.names()) if self.built else len(split_list(self.get("names")))}
