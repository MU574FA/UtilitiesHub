"""Duplicate Finder: identical files, same-text documents and look-alike scans.

Three modes (Settings or the radio buttons):
  Manual    nothing is ticked; you decide group by group, with the two files side by side.
  Assisted  everything at or above the "confident" score is pre-ticked; you confirm with one click.   (default)
  Auto      confident duplicates are moved away straight after the scan; the doubtful ones are left for you.

Duplicates are never deleted: they are *moved* to a folder (default `_duplicates` next to the first source) and
every move can be undone - one click undoes the whole batch.  The engine is core/similarity.py.
"""
from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk

from ..core import similarity as sim
from ..core.fileops import batch_note, new_batch, transfer
from ..core.ocr import OcrConfig
from ..core.schema import Opt
from ..core.settings import data_dir
from ..core.sheets import write_xlsx
from ..core.system import open_path, reveal
from ..core.textutil import human_size, parse_ext_list
from ..ui.dnd import make_drop_target
from ..ui.viewer import PageViewer
from ..ui.widgets import DataTable, FolderEntry, Tooltip, confirm, error, info, style_text
from .base import Command, Tool

MODES = [("manual", "Manual"), ("assisted", "Assisted"), ("auto", "Auto")]
MODE_HELP = {"manual": "Nothing is ticked. You choose.",
             "assisted": "Confident matches are ticked for you; you confirm with one click.",
             "auto": "Confident matches are moved away right after the scan. Doubtful ones are left for you. Undo is one click."}


class DuplicateFinder(Tool):
    id = "duplicates"
    title = "Duplicate Finder"
    icon = "🧬"
    order = 35
    description = "Find identical files and look-alike scans. Review them yourself, or let it clean up."

    @classmethod
    def options(cls) -> list[Opt]:
        p = "duplicates."
        return [
            Opt(p + "sources", [], "Folders and files to search", "list", section="Search", advanced=True),
            Opt(p + "recursive", True, "Search sub-folders too", "bool", section="Search"),
            Opt(p + "extensions", "", "Only these extensions (e.g. pdf, jpg; empty = all)", "str", section="Search"),
            Opt(p + "min_size_kb", 0, "Ignore files smaller than (KB)", "int", min=0, max=100000, section="Search"),
            Opt(p + "scan_on_drop", True, "Start scanning as soon as a folder is dropped", "bool", section="Search"),
            Opt(p + "mode", "assisted", "Mode", "choice", choices=MODES, section="How sure",
                help="Manual: you tick everything. Assisted: confident matches are pre-ticked. Auto: confident matches are "
                     "moved away immediately (undo is always available)."),
            Opt(p + "review", 70, "Show matches from (%)", "percent", min=30, max=100, section="How sure",
                help="Lower = shows more look-alikes (and more that are not really duplicates)."),
            Opt(p + "auto", 95, "Confident from (%)", "percent", min=50, max=100, section="How sure",
                help="Used by Assisted and Auto. Documents that merely resemble each other (two forms with another name) "
                     "can never reach the default 95 %."),
            Opt(p + "keep", "largest", "Which file to keep", "choice", choices=sim.KEEP_RULES, section="How sure"),
            Opt(p + "protect", "", "Never touch files in this folder", "folder", section="How sure",
                help="Files here are always the ones that are kept and are never offered for removal."),
            Opt(p + "use_text", True, "Compare the words of PDFs, Word and text files", "bool", section="What to compare"),
            Opt(p + "use_images", True, "Compare pictures and scanned pages", "bool", section="What to compare"),
            Opt(p + "strict", True, "Strict check (fewer false matches, a little slower)", "bool", section="What to compare",
                help="Compares the real strokes of two look-alike pages and lowers the score when they differ in a place "
                     "or are not pixel-identical."),
            Opt(p + "rotation", True, "A scan that is turned still matches", "bool", section="What to compare"),
            Opt(p + "ocr", False, "Also read the text of scans with OCR (slow)", "bool", section="What to compare", advanced=True),
            Opt(p + "use_cache", True, "Remember fingerprints between runs (faster next time)", "bool", section="What to compare",
                advanced=True),
            Opt(p + "dest", "", "Move duplicates to (empty = a '_duplicates' folder next to the first source)", "folder",
                section="Result"),
            Opt(p + "preview", True, "Show the two files side by side", "bool", section="Result"),
            *cls.append_opts(),
        ]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.result: sim.ScanResult | None = None
        self.ticked: set[str] = set()
        self.rows: dict[str, tuple[sim.Group, sim.Member, bool]] = {}      # iid -> (group, member, is_keeper)
        self._preview_after = None

    # ------------------------------------------------------------------ UI
    def build(self, parent: ttk.Frame) -> None:
        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=(0, 0, 8, 0))
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=4)
        self.vars: dict[str, tk.Variable] = {}

        def var(key, kind=tk.StringVar):
            v = kind(value=self.get(key))
            v.trace_add("write", lambda *_a, k=key, vv=v: self._store(k, vv))
            self.vars[key] = v
            return v

        # --- sources
        ttk.Label(left, text="Search in", style="H3.TLabel").pack(anchor="w")
        box = ttk.Frame(left)
        box.pack(fill="both", expand=True, pady=4)
        self.src_list = tk.Listbox(box, width=30, height=10, selectmode="extended", activestyle="none")
        sb = ttk.Scrollbar(box, command=self.src_list.yview)
        self.src_list.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.src_list.pack(side="left", fill="both", expand=True)
        style_text(left, self.src_list)
        make_drop_target(self.src_list, self.accept_files)
        bar = ttk.Frame(left)
        bar.pack(fill="x")
        ttk.Button(bar, text="Add folder…", style="Tiny.TButton", command=self.add_folder).pack(side="left")
        ttk.Button(bar, text="Add files…", style="Tiny.TButton", command=self.add_files).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove", style="Tiny.TButton", command=self.remove_sources).pack(side="left")
        ttk.Button(bar, text="Clear", style="Tiny.TButton", command=self.clear_sources).pack(side="left", padx=4)
        self.src_lbl = ttk.Label(left, style="Muted.TLabel", wraplength=230, justify="left",
                                 text="Drop folders or files here.")
        self.src_lbl.pack(anchor="w", pady=(4, 0))
        for text, key in (("Sub-folders too", "recursive"), ("Scan as soon as I drop", "scan_on_drop")):
            ttk.Checkbutton(left, text=text, variable=var(key, tk.BooleanVar)).pack(anchor="w")
        row = ttk.Frame(left)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Extensions").pack(side="left")
        ttk.Entry(row, width=14, textvariable=var("extensions")).pack(side="left", padx=4)

        # --- form
        form = ttk.Frame(right)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        r1 = ttk.Frame(form)
        r1.grid(row=0, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(r1, text="Mode").pack(side="left")
        mv = var("mode")
        for value, label in MODES:
            rb = ttk.Radiobutton(r1, text=label, value=value, variable=mv)
            rb.pack(side="left", padx=(8, 0))
            Tooltip(rb, MODE_HELP[value])
        ttk.Label(r1, text="   Show from").pack(side="left")
        ttk.Spinbox(r1, from_=30, to=100, width=4, textvariable=var("review", tk.IntVar)).pack(side="left", padx=2)
        ttk.Label(r1, text="%   Confident from").pack(side="left")
        ttk.Spinbox(r1, from_=50, to=100, width=4, textvariable=var("auto", tk.IntVar)).pack(side="left", padx=2)
        ttk.Label(r1, text="%   Keep").pack(side="left")
        kv = tk.StringVar(value=dict(sim.KEEP_RULES).get(self.get("keep"), sim.KEEP_RULES[0][1]))
        kc = ttk.Combobox(r1, textvariable=kv, values=[l for _k, l in sim.KEEP_RULES], state="readonly", width=20)
        kc.pack(side="left", padx=4)
        kc.bind("<<ComboboxSelected>>", lambda e: self.put("keep", next(k for k, l in sim.KEEP_RULES if l == kv.get())))
        r2 = ttk.Frame(form)
        r2.grid(row=1, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(r2, text="Compare").pack(side="left")
        for text, key in (("Document text", "use_text"), ("Pictures & scans", "use_images"), ("Strict check", "strict"),
                          ("Turned scans", "rotation"), ("OCR scans (slow)", "ocr")):
            ttk.Checkbutton(r2, text=text, variable=var(key, tk.BooleanVar)).pack(side="left", padx=(8, 0))
        for i, (label, key, tipx) in enumerate((("Protect folder", "protect", "Files here are always kept, never removed."),
                                                ("Move duplicates to", "dest", "Empty = '_duplicates' next to the first source."))):
            ttk.Label(form, text=label).grid(row=2 + i, column=0, sticky="w", pady=2, padx=(0, 8))
            fe = FolderEntry(form, var(key), "dir", open_button=True)
            fe.grid(row=2 + i, column=1, sticky="ew", pady=2)
            Tooltip(fe, tipx)

        # --- actions
        act = ttk.Frame(right)
        act.pack(fill="x", pady=6)
        ttk.Button(act, text="🔍 Scan", style="Accent.TButton", command=self.run_scan).pack(side="left")
        self.apply_btn = ttk.Button(act, text="Move ticked", command=self.apply, state="disabled")
        self.apply_btn.pack(side="left", padx=6)
        ttk.Button(act, text="Tick confident", command=self.tick_confident).pack(side="left")
        ttk.Button(act, text="Untick all", command=self.untick_all).pack(side="left", padx=6)
        ttk.Button(act, text="↩ Undo last batch", command=self.undo_batch).pack(side="left")
        ttk.Button(act, text="Report…", command=self.export_report).pack(side="right")
        ttk.Button(act, text="Add to file…", command=self.add_to_file).pack(side="right", padx=6)
        self.summary = ttk.Label(right, style="Muted.TLabel")
        self.summary.pack(anchor="w")

        # --- results + preview
        self.vpane = ttk.PanedWindow(right, orient="vertical")
        self.vpane.pack(fill="both", expand=True, pady=(4, 0))
        top = ttk.Frame(self.vpane)
        self.vpane.add(top, weight=3)
        self.table = DataTable(top, [("sel", "✔", 36, "center"), ("grp", "Group", 52, "center"), ("file", "File", 220),
                                     ("match", "Match", 56, "e"), ("band", "How sure", 84), ("why", "Why", 230),
                                     ("size", "Size", 70, "e"), ("mod", "Modified", 118), ("folder", "Folder", 260)],
                               searchable=True, on_double=self.toggle_row, on_select=self._selected, stretch="folder")
        self.table.pack(fill="both", expand=True)
        self.table.tag_colors(keep="ok", maybe="warn", off="muted")
        m = self.table.menu
        m.add_separator()
        m.add_command(label="Tick / untick", command=lambda: [self.toggle_row(i) for i in self.table.selection()])
        m.add_command(label="Keep this one instead", command=self.keep_selected)
        m.add_command(label="Open file", command=lambda: self._each_selected(lambda g, mem, k: open_path(mem.path)))
        m.add_command(label="Show in folder", command=lambda: self._each_selected(lambda g, mem, k: reveal(mem.path)))
        self.table.tree.bind("<space>", lambda e: ([self.toggle_row(i) for i in self.table.selection()], "break")[1])

        self.preview_box = ttk.Frame(self.vpane)
        both = ttk.PanedWindow(self.preview_box, orient="horizontal")
        both.pack(fill="both", expand=True)
        self.viewers = []
        self.viewer_labels = []
        for caption in ("Kept", "Duplicate (ticked = will move)"):
            col = ttk.Frame(both)
            lbl = ttk.Label(col, text=caption, style="Muted.TLabel")
            lbl.pack(anchor="w")
            viewer = PageViewer(col, self.app, show_toolbar=False, max_pages=3)
            viewer.pack(fill="both", expand=True)
            both.add(col, weight=1)
            self.viewers.append(viewer)
            self.viewer_labels.append(lbl)
        pv = var("preview", tk.BooleanVar)
        ttk.Checkbutton(right, text="Show the two files side by side", variable=pv, command=self._apply_preview_setting).pack(anchor="w")
        self._apply_preview_setting()
        self._show_sources()

    def _store(self, key: str, v: tk.Variable) -> None:
        try:
            self.put(key, v.get())
        except tk.TclError:                       # a spinbox that is momentarily empty / not a number
            pass

    def _apply_preview_setting(self) -> None:
        shown = str(self.preview_box) in [str(p) for p in self.vpane.panes()]
        want = bool(self.get("preview"))
        if want and not shown:
            self.vpane.add(self.preview_box, weight=2)
        elif not want and shown:
            self.vpane.forget(self.preview_box)

    # ------------------------------------------------------------------ sources
    def sources(self) -> list[str]:
        return [s for s in (self.get("sources") or []) if Path(s).exists()]

    def _show_sources(self) -> None:
        self.src_list.delete(0, "end")
        for s in self.get("sources") or []:
            self.src_list.insert("end", s)
        n = len(self.get("sources") or [])
        self.src_lbl.configure(text=f"{n} place(s) to search" if n else "Drop folders or files here.")

    def _add_sources(self, paths: list[str]) -> None:
        cur = list(self.get("sources") or [])
        for p in paths:
            if p and p not in cur:
                cur.append(p)
        self.put("sources", cur)
        self._show_sources()

    def add_folder(self) -> None:
        path = filedialog.askdirectory(title="Folder to search")
        if path:
            self._add_sources([path])

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Files to compare")
        if paths:
            self._add_sources(list(paths))

    def remove_sources(self) -> None:
        cur = list(self.get("sources") or [])
        for i in sorted(self.src_list.curselection(), reverse=True):
            if i < len(cur):
                del cur[i]
        self.put("sources", cur)
        self._show_sources()

    def clear_sources(self) -> None:
        self.put("sources", [])
        self._show_sources()

    def accept_files(self, paths: list[str]) -> None:
        if not paths:
            return
        self._add_sources(list(paths))
        if self.get("scan_on_drop"):
            self.run_scan()

    # ------------------------------------------------------------------ scanning
    def _options(self) -> sim.DupOptions:
        review = max(30, min(100, int(self.get("review") or 70))) / 100
        auto = max(review, min(1.0, int(self.get("auto") or 95) / 100))
        return sim.DupOptions(
            use_text=bool(self.get("use_text")), use_images=bool(self.get("use_images")), strict=bool(self.get("strict")),
            rotation_invariant=bool(self.get("rotation")),
            ocr_cfg=OcrConfig.from_settings(self.settings) if self.get("ocr") else None,
            review_threshold=review, auto_threshold=auto, keep=self.get("keep") or "largest",
            protect=(self.get("protect") or "").strip(), workers=max(2, self.settings.get_int("auto.max_parallel", 2) + 1))

    def run_scan(self) -> None:
        sources = self.sources()
        if not sources:
            info(self.frame, "Add a folder (or some files) to search first.")
            return
        opts = self._options()
        exts, rec = parse_ext_list(self.get("extensions")), bool(self.get("recursive"))
        min_size = max(1, int(self.get("min_size_kb") or 0) * 1024)
        cache = sim.FingerprintCache(data_dir() / "dupcache.json", opts.signature()) if self.get("use_cache") else None

        def work(job):
            files = sim.collect_files(sources, rec, exts, min_size)
            job.progress(0, len(files), f"Found {len(files)} files")
            return sim.scan(files, opts, cache, lambda: job.cancelled, job.progress)

        self.summary.configure(text="Scanning…")
        self.app.jobs.submit(work, "Looking for duplicates", self.id, on_done=self._show_result,
                             on_error=lambda m: (self.summary.configure(text=""), error(self.frame, m)),
                             serial_key=self.id)

    def _show_result(self, res: sim.ScanResult) -> None:
        self.result = res
        self.ticked.clear()
        if res.cancelled:
            self.summary.configure(text="Cancelled.")
            self.log("Scan cancelled", "warn")
            return
        for path, why in res.problems[:8]:
            self.log(f"{Path(path).name}: {why}", "warn")
        mode = self.get("mode")
        if mode != "manual":
            self._tick_confident()
        self.fill_table()
        self.log(f"Scanned {res.files} files in {res.seconds:.1f}s: {len(res.groups)} group(s) of duplicates", "ok")
        if mode == "auto" and self.ticked:
            self.apply(ask=False)

    def _selected_members(self) -> list[tuple[sim.Group, sim.Member]]:
        if not self.result:
            return []
        return [(g, m) for g in self.result.groups for m in g.members if m.path in self.ticked and not m.protected]

    def fill_table(self) -> None:
        self.table.clear()
        self.rows.clear()
        res = self.result
        if not res:
            return
        auto = res.opts.auto_threshold
        for gi, g in enumerate(res.groups, 1):
            k = g.keeper
            iid = self.table.add(self._row(gi, k, True), tags=("keep",))
            self.rows[iid] = (g, k, True)
            for m in g.members:
                tags = ("off",) if m.protected else (() if m.score >= auto else ("maybe",))
                iid = self.table.add(self._row(gi, m, False), tags=tags)
                self.rows[iid] = (g, m, False)
        n = sum(len(g.members) for g in res.groups)
        free = sum(g.reclaimable for g in res.groups)
        sure = sum(1 for g in res.groups for m in g.members if m.score >= auto and not m.protected)
        extra = f" • {len(res.problems)} unreadable" if res.problems else ""
        self.summary.configure(text=f"{res.files} files • {len(res.groups)} groups • {n} duplicates ({sure} confident) • "
                                    f"{human_size(free)} could be freed{extra}")
        self._update_apply()

    def _row(self, gi: int, m: sim.Member, keeper: bool) -> dict:
        if keeper:
            sel = "🔒" if m.protected else "★"
        else:
            sel = "🔒" if m.protected else ("✔" if m.path in self.ticked else "")
        p = Path(m.path)
        return {"sel": sel, "grp": f"G{gi}", "file": p.name, "match": "" if keeper else f"{m.score * 100:.0f}%",
                "band": "kept" if keeper else m.band, "why": "" if keeper else m.why, "size": human_size(m.size),
                "mod": datetime.fromtimestamp(m.mtime).strftime("%Y-%m-%d %H:%M") if m.mtime else "", "folder": str(p.parent)}

    def _update_apply(self) -> None:
        n = len(self._selected_members())
        self.apply_btn.configure(state="normal" if n else "disabled", text=f"Move ticked ({n})" if n else "Move ticked")

    # ------------------------------------------------------------------ ticking
    def toggle_row(self, iid: str) -> None:
        entry = self.rows.get(iid)
        if not entry or entry[2] or entry[1].protected:
            return
        self.ticked.symmetric_difference_update({entry[1].path})
        gi = self.result.groups.index(entry[0]) + 1 if self.result else 0
        self.table.update_row(iid, self._row(gi, entry[1], False), tags=self._tags(entry[1]))
        self._update_apply()

    def _tags(self, m: sim.Member) -> tuple:
        auto = self.result.opts.auto_threshold if self.result else 1.0
        return ("off",) if m.protected else (() if m.score >= auto else ("maybe",))

    def _tick_confident(self) -> None:
        if not self.result:
            return
        auto = self.result.opts.auto_threshold
        self.ticked = {m.path for g in self.result.groups for m in g.members if m.score >= auto and not m.protected}

    def tick_confident(self) -> None:
        if not self.result:
            info(self.frame, "Scan first.")
            return
        self._tick_confident()
        self.fill_table()

    def untick_all(self) -> None:
        self.ticked.clear()
        self.fill_table()

    def keep_selected(self) -> None:
        for iid in self.table.selection():
            g, m, is_keeper = self.rows.get(iid, (None, None, True))
            if g is not None and not is_keeper and self.result:
                self.ticked.discard(m.path)
                self.result.make_keeper(g, m.path)
                self.fill_table()
                return

    def _each_selected(self, fn) -> None:
        for iid in self.table.selection():
            if iid in self.rows:
                g, m, k = self.rows[iid]
                fn(g, m, k)

    # ------------------------------------------------------------------ side-by-side preview
    def _selected(self, iids: list[str]) -> None:
        if not self.get("preview") or not iids or iids[0] not in self.rows:
            return
        if self._preview_after:
            self.app.root.after_cancel(self._preview_after)
        self._preview_after = self.app.root.after(180, lambda: self._show_preview(iids[0]))

    def _show_preview(self, iid: str) -> None:
        self._preview_after = None
        entry = self.rows.get(iid)
        if not entry or not self.frame.winfo_exists():
            return
        g, m, is_keeper = entry
        other = m if not is_keeper else (g.members[0] if g.members else m)
        self.viewers[0].show(g.keeper.path)
        self.viewers[1].show(other.path)
        self.viewer_labels[0].configure(text=f"Kept: {Path(g.keeper.path).name}")
        self.viewer_labels[1].configure(text=f"Duplicate: {Path(other.path).name}   ({other.score * 100:.0f}% – {other.why})")

    # ------------------------------------------------------------------ apply / undo
    def _dest(self) -> Path:
        d = (self.get("dest") or "").strip()
        if d:
            return Path(d)
        srcs = self.sources()
        first = Path(srcs[0]) if srcs else Path.cwd()
        return (first if first.is_dir() else first.parent) / sim.QUARANTINE_NAME

    def apply(self, ask: bool = True) -> None:
        chosen = self._selected_members()
        if not chosen:
            info(self.frame, "Nothing is ticked.")
            return
        dest = self._dest()
        total = sum(m.size for _g, m in chosen)
        if ask and not confirm(self.frame, f"Move {len(chosen)} duplicate file(s) ({human_size(total)}) to\n{dest}\n\n"
                                          "Nothing is deleted, and you can undo this."):
            return
        batch, journal = new_batch(), self.app.journal

        def work(job):
            moved, failed = [], []
            for n, (g, m) in enumerate(chosen, 1):
                if job.cancelled:
                    break
                job.progress(n, len(chosen), Path(m.path).name)
                try:
                    transfer(Path(m.path), dest, None, "move", "number", journal, self.id,
                             note=batch_note(batch, f"duplicate of {Path(g.keeper.path).name} ({m.score:.0%})"))
                    moved.append(m.path)
                except OSError as exc:
                    failed.append((m.path, str(exc)))
            return moved, failed

        def done(res):
            moved, failed = set(res[0]), res[1]
            if self.result:
                for g in self.result.groups:
                    g.members = [m for m in g.members if m.path not in moved]
                self.result.groups[:] = [g for g in self.result.groups if g.members]
            self.ticked -= moved
            self.fill_table()
            for path, why in failed[:5]:
                self.log(f"{Path(path).name}: {why}", "error")
            self.log(f"Moved {len(moved)} duplicate(s) to {dest}" + (f", {len(failed)} failed" if failed else ""), "ok")
            self.app.notify(f"{len(moved)} duplicate(s) moved – undo is in this tool", "ok")

        self.app.jobs.submit(work, "Moving duplicates", self.id, on_done=done, on_error=lambda m: error(self.frame, m))

    def undo_batch(self) -> None:
        j = self.app.journal
        batch = j.last_batch(self.id)
        if batch:
            n, problems = j.undo_batch(batch, self.id)
            self.log(f"Put {n} file(s) back" + (f" ({len(problems)} could not be restored)" if problems else ""), "ok" if n else "warn")
            if problems:
                error(self.frame, "\n".join(problems[:8]))
            if n:
                self.log("Scan again to refresh the list.")
            return
        e = j.last_undoable(self.id)
        if not e:
            info(self.frame, "Nothing to undo.")
            return
        ok, why = j.can_undo(e)
        if not ok:
            error(self.frame, why)
            return
        self.log(j.undo(e), "ok")

    # ------------------------------------------------------------------ output
    def _records(self) -> list[dict]:
        out = []
        for gi, g in enumerate(self.result.groups if self.result else [], 1):
            for role, m in [("Keep", g.keeper)] + [("Duplicate", x) for x in g.members]:
                out.append({"Group": gi, "Role": role, "File": Path(m.path).name, "Match %": "" if role == "Keep" else f"{m.score * 100:.0f}",
                            "How sure": "" if role == "Keep" else m.band, "Why": m.why if role != "Keep" else "",
                            "Size": human_size(m.size), "Folder": str(Path(m.path).parent)})
        return out

    def export_report(self) -> None:
        if not self.result or not self.result.groups:
            info(self.frame, "Scan first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", initialfile="duplicates-report.xlsx",
                                            filetypes=[("Excel", "*.xlsx")])
        if not path:
            return
        recs = self._records()
        heads = list(recs[0])
        write_xlsx(path, {"Duplicates": [heads] + [[r[h] for h in heads] for r in recs]})
        self.log(f"Report saved to {path}", "ok")

    def add_to_file(self) -> None:
        if not self.result or not self.result.groups:
            info(self.frame, "Scan first.")
            return
        self.append_rows(self._records(), ask=True)

    # ------------------------------------------------------------------ app hooks
    def keys(self) -> dict:
        return {"<F5>": self.run_scan, "<Control-Return>": self.apply, "<Control-z>": self.undo_batch}

    def commands(self) -> list[Command]:
        return [Command("dup.scan", "Duplicate Finder: scan", self.run_scan, "Duplicate Finder", "find duplicates"),
                Command("dup.undo", "Duplicate Finder: undo last batch", self.undo_batch, "Duplicate Finder")]

    def stats(self) -> dict:
        return {"Places": len(self.get("sources") or [])}
