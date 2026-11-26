"""Autopilot: one switchboard for everything that can run by itself.

  * the watchers of other tools (clipboard watcher, CV folder ...), started/stopped together or one by one
  * **folder triggers**: "when a file lands in this folder, run that saved workflow on it" - the bridge between the
    watchers and the Workflows tab (engine in core/triggers.py)
"""
from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ..core.ocr import OcrConfig
from ..core.schema import Opt
from ..core.settings import data_dir
from ..core.triggers import Trigger, TriggerRunner, TriggerStore, parse_extensions
from ..core.workflow import RunEnv, WorkflowStore, run_workflow
from ..ui.widgets import FolderEntry, confirm, info, modal
from .base import Command, Tool

_log = __import__("logging").getLogger(__name__)


class AutopilotTool(Tool):
    id = "autopilot"
    title = "Autopilot"
    icon = "🤖"
    description = "Start or stop every watcher, and run saved workflows automatically when files arrive in a folder."
    order = 90
    stats_when_closed = True          # the trigger list is loaded in __init__

    @classmethod
    def options(cls) -> list[Opt]:
        return [Opt("autopilot.max_per_minute", 30, "Pause a folder trigger after this many files in one minute", "int",
                    min=3, max=500, page="auto", section="Folder triggers", advanced=True,
                    help="Safety net against loops, e.g. a workflow that saves its results into the folder it watches.")]

    @classmethod
    def pages(cls):
        return []

    def __init__(self, app) -> None:
        super().__init__(app)
        self.triggers = TriggerStore(data_dir() / "triggers.json")
        self.runner = TriggerRunner(self._from_watcher, say=lambda m, lv="info": self._say(m, lv),
                                    max_per_minute=self.settings.get_int("autopilot.max_per_minute", 30))
        self.last: dict[str, str] = {}               # trigger id -> "12:30 Done: 3 files"
        self._after = None                           # the single refresh timer (never stack a second one)
        self.settings.subscribe("autopilot.max_per_minute",
                                lambda k, v: setattr(self.runner, "max_per_minute", max(3, int(v or 30))))
        self.tree = None
        self.rows = None

    # ------------------------------------------------------------------ UI
    def build(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Autopilot", style="H2.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Everything that can work without you. Global switches live in Settings > Automation.",
                  style="Muted.TLabel").pack(anchor="w")
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(4, 4))
        ttk.Button(bar, text="Start all", style="Accent.TButton", command=self._all(True)).pack(side="left")
        ttk.Button(bar, text="Stop all", command=self._all(False)).pack(side="left", padx=4)
        ttk.Button(bar, text="Automation settings…", command=lambda: self.app.open_settings("auto")).pack(side="right")
        self.rows = ttk.Frame(parent)
        self.rows.pack(fill="x")

        ttk.Label(parent, text="Folder triggers", style="H3.TLabel").pack(anchor="w", pady=(8, 0))
        ttk.Label(parent, text="When a file finishes arriving in a folder, run a saved workflow on it.",
                  style="Muted.TLabel").pack(anchor="w")
        tb = ttk.Frame(parent)
        tb.pack(fill="x", pady=(4, 4))
        ttk.Button(tb, text="＋ New trigger", style="Accent.TButton", command=self.add_trigger).pack(side="left")
        ttk.Button(tb, text="Edit", command=self.edit_trigger).pack(side="left", padx=4)
        ttk.Button(tb, text="Delete", command=self.delete_trigger).pack(side="left")
        ttk.Button(tb, text="Start / stop", command=self.toggle_selected).pack(side="left", padx=4)
        ttk.Button(tb, text="Resume paused", command=self.resume_paused).pack(side="left")
        ttk.Button(tb, text="Files handled…", command=self.show_handled).pack(side="left", padx=4)
        ttk.Button(tb, text="Test (preview)", command=self.test_selected).pack(side="left")
        cols = ("name", "folder", "workflow", "types", "state", "last")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse", height=6)
        for c, w in zip(cols, (130, 210, 130, 75, 80, 170)):
            self.tree.heading(c, text=c.title() if c != "last" else "Last run")
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_trigger())
        self.refresh()

    def _all(self, on: bool):
        def go():
            (self.app.start_all_auto if on else self.app.stop_all_auto)()
            self.refresh()
        return go

    def refresh(self) -> None:
        if not self.built or self.rows is None or not self.rows.winfo_exists():
            return
        for w in self.rows.winfo_children():
            w.destroy()
        autos = [t for t in self.app.iter_tools() if t is not self and t.can_automate()]
        if not autos:
            ttk.Label(self.rows, text="No other tool is watching anything right now (switch a watcher on in that tool's settings).",
                      style="Muted.TLabel").pack(anchor="w")
        for t in autos:
            r = ttk.Frame(self.rows, padding=4)
            r.pack(fill="x")
            running = t.auto_running()
            ttk.Label(r, text=f"{t.icon} {t.title}", width=22).pack(side="left")
            ttk.Label(r, text="● running" if running else "○ stopped",
                      foreground=self.app.theme.p["ok" if running else "muted"], width=12).pack(side="left")
            ttk.Label(r, text=t.auto_summary(), style="Muted.TLabel").pack(side="left", padx=8)
            ttk.Button(r, text="Stop" if running else "Start",
                       command=lambda t=t, run=running: self._toggle(t, run)).pack(side="right")
        sel = self._selected_id()
        self.tree.delete(*self.tree.get_children())
        for t in self.triggers.items:
            if t.id in self.runner.paused:
                state = "paused"
            elif self.runner.is_running(t.id):
                state = "● on"
            else:
                state = "○ off" if t.enabled else "disabled"
            self.tree.insert("", "end", iid=t.id, values=(t.name, t.folder, t.workflow, t.extensions or "all", state,
                                                         self.last.get(t.id, "")))
        if sel and self.triggers.get(sel):
            self.tree.selection_set(sel)
        if self._after is not None:
            try:
                self.frame.after_cancel(self._after)
            except Exception:
                _log.debug("ignored error", exc_info=True)
        self._after = self.frame.after(3000, self._tick)

    def _tick(self) -> None:
        self._after = None
        if self.frame is not None and self.frame.winfo_exists() and self.is_current():
            self.refresh()

    def on_hide(self) -> None:
        if self._after is not None and self.frame is not None:
            try:
                self.frame.after_cancel(self._after)
            except Exception:
                _log.debug("ignored error", exc_info=True)
            self._after = None

    def _toggle(self, tool, running: bool) -> None:
        try:
            (tool.stop_auto if running else tool.start_auto)()
        except Exception as exc:
            self.app.log(f"{tool.title}: {exc}", "error")
        self.refresh()

    def on_show(self) -> None:
        self.refresh()

    # ------------------------------------------------------------------ editing triggers
    def _selected_id(self) -> str:
        if self.tree is None:
            return ""
        sel = self.tree.selection()
        return sel[0] if sel else ""

    def _workflow_store(self) -> WorkflowStore:
        wf_tool = self.host.member("workflows") if self.host is not None and hasattr(self.host, "member") else None
        store = getattr(wf_tool, "store", None)
        return store if store is not None else WorkflowStore(data_dir() / "workflows.json")

    def add_trigger(self) -> None:
        self._edit(Trigger(name=self.unique_name()))

    def edit_trigger(self) -> None:
        t = self.triggers.get(self._selected_id())
        if t is None:
            info(self.frame, "Select a trigger first.")
        else:
            self._edit(Trigger.from_dict(t.to_dict()))

    def unique_name(self) -> str:
        names, n = {t.name for t in self.triggers.items}, 1
        while f"Trigger {n}" in names:
            n += 1
        return f"Trigger {n}"

    def _edit(self, trig: Trigger) -> None:
        names = self._workflow_store().names()
        if not names:
            info(self.frame, "Save a workflow first (Workflows tab), then come back to run it from a folder.")
            return
        win, f = modal(self.frame, "Folder trigger", 560)
        name, folder = tk.StringVar(value=trig.name), tk.StringVar(value=trig.folder)
        wf = tk.StringVar(value=trig.workflow if trig.workflow in names else names[0])
        types, quiet = tk.StringVar(value=trig.extensions), tk.StringVar(value=str(trig.quiet_seconds))
        rec, existing, on = (tk.BooleanVar(value=trig.recursive), tk.BooleanVar(value=trig.process_existing),
                             tk.BooleanVar(value=trig.enabled))
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Name").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(f, textvariable=name).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(f, text="Watch this folder").grid(row=1, column=0, sticky="w", pady=3)
        FolderEntry(f, folder, title="Folder to watch").grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(f, text="Run this workflow").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Combobox(f, textvariable=wf, values=names, state="readonly").grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Label(f, text="Only these types").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(f, textvariable=types).grid(row=3, column=1, sticky="ew", pady=3)
        ttk.Label(f, text="e.g. pdf, jpg, png - empty means every file", style="Muted.TLabel").grid(row=4, column=1, sticky="w")
        ttk.Label(f, text="Wait until file is still for (s)").grid(row=5, column=0, sticky="w", pady=3)
        ttk.Entry(f, textvariable=quiet, width=8).grid(row=5, column=1, sticky="w", pady=3)
        ttk.Checkbutton(f, text="Include sub-folders", variable=rec).grid(row=6, column=1, sticky="w")
        ttk.Checkbutton(f, text="Also run on the files already in the folder when it starts",
                        variable=existing).grid(row=7, column=1, sticky="w")
        ttk.Checkbutton(f, text="Start automatically with the app (Start all)", variable=on).grid(row=8, column=1, sticky="w")

        def save() -> None:
            try:
                q = max(0.0, float(quiet.get().replace(",", ".")))
            except ValueError:
                q = 3.0
            trig.name, trig.folder, trig.workflow = name.get().strip() or trig.name, folder.get().strip(), wf.get()
            trig.extensions = ", ".join(sorted(e.lstrip(".") for e in parse_extensions(types.get())))
            trig.quiet_seconds = q
            trig.recursive, trig.process_existing, trig.enabled = rec.get(), existing.get(), on.get()
            why = trig.problem()
            if why:
                info(win, f"Cannot save: {why}.")
                return
            was_on = self.runner.is_running(trig.id)
            self.triggers.put(trig)
            if was_on:
                self.runner.start(trig)                         # apply the change right away
            win.destroy()
            self.refresh()

        row = ttk.Frame(f)
        row.grid(row=9, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="Save", style="Accent.TButton", command=save).pack(side="right")

    def delete_trigger(self) -> None:
        t = self.triggers.get(self._selected_id())
        if t and confirm(self.frame, f"Delete the trigger '{t.name}'?"):
            self.runner.stop(t.id)
            self.triggers.remove(t.id)
            self.refresh()

    def toggle_selected(self) -> None:
        t = self.triggers.get(self._selected_id())
        if t is None:
            info(self.frame, "Select a trigger first.")
            return
        if self.runner.is_running(t.id):
            self.runner.stop(t.id)
        else:
            self.runner.start(t)
        self.refresh()

    # ------------------------------------------------------------------ running a workflow for a file
    def _say(self, message: str, level: str = "info") -> None:
        self.app.bridge.call(self.log, message, level)

    def _from_watcher(self, trig: Trigger, path: Path) -> None:
        self.app.bridge.call(self._submit, trig, [path], False)       # hop to the UI thread, then run as a normal job

    def _submit(self, trig: Trigger, files: list[Path], preview: bool) -> None:
        wf = self._workflow_store().get(trig.workflow)
        if wf is None:
            self.log(f"Trigger '{trig.name}': the workflow '{trig.workflow}' no longer exists.", "error")
            return
        wf, bridge = wf.copy(), self.app.bridge
        out_root = Path(self.settings.get("workflows.output_root") or data_dir() / "workflow_output")
        cfg = OcrConfig.from_settings(self.settings)
        label = files[0].name + (f" (+{len(files) - 1})" if len(files) > 1 else "")

        def work(job):
            env = RunEnv(out_root=out_root, journal=self.app.journal, ocr_cfg=cfg,
                         log=lambda m, lv="info": bridge.call(self.log, m, lv), cancelled=lambda: job.cancelled,
                         progress=lambda d, t, m="": job.progress(d, t, m),
                         notify=lambda m: bridge.call(self.app.notify, m, "ok"), tool_id=self.id)
            report = run_workflow(wf, [str(p) for p in files], env, preview)
            if not preview:                                    # what the workflow made / renamed must not trigger it again
                mine = {str(p) for p in files}
                for out in list(report.files) + list(report.made):
                    if out not in mine:
                        self.runner.ignore(out)
            return report

        def done(report) -> None:
            self.last[trig.id] = f"{time.strftime('%H:%M')} {report.summary() if report.ok else report.message}"[:80]
            self.log(f"{trig.name}: {label} - " + (report.summary() if report.ok else report.message),
                     "ok" if report.ok else "error")
            if self.settings.get_bool("auto.notify_done", True):
                self.app.notify(f"{trig.name}: {label} - " + (report.summary() if report.ok else report.message),
                                "ok" if report.ok else "warn")
            self.refresh()

        self.app.jobs.submit(work, title=f"{'Preview' if preview else 'Trigger'}: {trig.name}", tool=self.id,
                             on_done=done, on_error=lambda m: self.log(f"{trig.name}: {m}", "error"))

    def test_selected(self) -> None:
        """Preview: run the workflow on up to five files already in the folder - nothing is changed."""
        t = self.triggers.get(self._selected_id())
        if t is None:
            info(self.frame, "Select a trigger first.")
            return
        exts, folder = parse_extensions(t.extensions), Path(t.folder)
        try:
            files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith((".", "~$"))
                           and (not exts or p.suffix.lower() in exts))[:5]
        except OSError as exc:
            info(self.frame, f"Cannot read the folder: {exc}")
            return
        if not files:
            info(self.frame, "There is no matching file in that folder to test with.")
            return
        self._submit(t, files, True)

    # ------------------------------------------------------------------ automation interface (Start all / Stop all)
    def can_automate(self) -> bool:
        return any(t.enabled and not t.problem() for t in self.triggers.items)

    def auto_running(self) -> bool:
        return bool(self.runner.running_ids())

    def start_auto(self) -> None:
        for t in self.triggers.items:
            if t.enabled and not self.runner.is_running(t.id):
                self.runner.start(t)

    def stop_auto(self) -> None:
        self.runner.stop_all()

    def auto_incomplete(self) -> bool:
        wanted = [t for t in self.triggers.items if t.enabled and not t.problem()]
        return any(not self.runner.is_running(t.id) for t in wanted)

    def resume_paused(self) -> None:
        """Start again every trigger the safety net paused (check what made the loop first)."""
        paused = [t for t in self.triggers.items if t.id in self.runner.paused]
        if not paused:
            info(self.frame, "No trigger is paused.")
            return
        if confirm(self.frame, "Resume " + ", ".join(t.name for t in paused) + "?\n\nA trigger is paused when it fired too "
                   "often - usually because its workflow writes into the folder it watches. Make sure that is not the case."):
            for t in paused:
                self.runner.start(t)
            self.refresh()

    def show_handled(self) -> None:
        t = self.triggers.get(self._selected_id())
        if t is None:
            info(self.frame, "Select a trigger first.")
            return
        names = self.runner.handled_names(t.id)
        info(self.frame, f"'{t.name}' handled {len(names)} file(s) since it started:\n\n" + "\n".join(names[-30:]) if names
             else f"'{t.name}' has not handled any file since it started.")

    def auto_summary(self) -> str:
        return f"{len(self.runner.running_ids())} of {len(self.triggers.items)} folder triggers on"

    def on_close(self) -> None:
        self.runner.stop_all()

    def stats(self) -> dict:
        return {"Triggers on": len(self.runner.running_ids())}

    def commands(self) -> list[Command]:
        return [Command("autopilot.start", "Autopilot: start all watchers and triggers", self.app.start_all_auto, "Autopilot"),
                Command("autopilot.stop", "Autopilot: stop all watchers and triggers", self.app.stop_all_auto, "Autopilot")]
