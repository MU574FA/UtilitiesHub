"""Workflows: build your own step-by-step procedures by snapping coloured bricks together.

    box (left)  ->  baseplate (middle)  ->  settings of the selected brick (right)

The engine and the bricks live in core/workflow.py; the drawing in ui/lego.py.  This tool owns the data (the
workflow being edited, the saved ones), runs a workflow in a background job and shows what every brick did.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from ..core.ocr import OcrConfig
from ..core.schema import Opt
from ..core.settings import data_dir
from ..core.system import open_path
from ..core.workflow import (BRICKS, TEMPLATES, Params, RunEnv, Step, StepResult, Workflow, WorkflowStore,
                             check_workflow, export_workflow, import_workflow, run_workflow)
from ..ui.lego import BrickPalette, BrickStack, BrickView
from ..ui.lego_layout import reorder
from ..ui.widgets import FolderEntry, LogView, ask_text, confirm, error, tip
from .base import Command, Tool

_LEVEL = {"ok": "ok", "warn": "warn", "error": "error", "skipped": "info", "stopped": "warn"}


class WorkflowsTool(Tool):
    id = "workflows"
    title = "Workflows"
    icon = "🧱"
    description = "Build your own step-by-step procedures by snapping bricks together, then run them on any files."
    order = 47

    @classmethod
    def options(cls) -> list[Opt]:
        return [Opt("workflows.output_root", "", "Folder for files the workflows make", "folder",
                    help="PDFs, zips and text files made by a workflow are put here, in a folder per run. "
                         "Empty = inside the Utils Hub data folder."),
                Opt("workflows.confirm_changes", True, "Ask before a workflow renames or moves my files", "bool"),
                Opt("workflows.last", "", "Last workflow opened", "str", advanced=True)]

    def __init__(self, app) -> None:
        super().__init__(app)
        self.store = WorkflowStore(data_dir() / "workflows.json")
        self.wf = Workflow(self.store.unique_name("New workflow"))
        self.saved_name: str | None = None
        self.dirty = False
        self.selected: int | None = None
        self.inputs: list[str] = []
        self.results: dict[int, StepResult] = {}
        self.issues: dict[int, list[str]] = {}
        self.last_report = None
        self.running = False

    # ------------------------------------------------------------------ UI
    def build(self, parent: ttk.Frame) -> None:
        head = ttk.Frame(parent)
        head.pack(fill="x")
        ttk.Label(head, text="Workflows", style="H2.TLabel").pack(side="left")
        ttk.Label(head, text="   Snap bricks together to make your own procedure.", style="Muted.TLabel").pack(side="left")

        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(8, 6))
        self.name_var = tk.StringVar()
        self.combo = ttk.Combobox(bar, textvariable=self.name_var, state="readonly", width=30)
        self.combo.pack(side="left")
        self.combo.bind("<<ComboboxSelected>>", lambda e: self._pick_saved())
        self.dirty_lbl = ttk.Label(bar, text="", style="Muted.TLabel", width=14)
        self.dirty_lbl.pack(side="left", padx=4)
        for text, cmd in (("New", self.new), ("Save", self.save), ("Save as…", self.save_as), ("Delete", self.delete)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        self.tpl_btn = ttk.Menubutton(bar, text="Start from a template ▾")
        menu = tk.Menu(self.tpl_btn, tearoff=False)
        for t in TEMPLATES:
            menu.add_command(label=f"{t.name}  —  {t.description}", command=lambda t=t: self.load_template(t))
        self.tpl_btn["menu"] = menu
        self.tpl_btn.pack(side="left", padx=(10, 2))
        ttk.Button(bar, text="Import…", command=self.import_file).pack(side="right")
        ttk.Button(bar, text="Export…", command=self.export_file).pack(side="right", padx=2)

        run = ttk.Frame(parent)
        run.pack(fill="x", pady=(0, 6), side="bottom")
        self.log_view = LogView(parent, height=6)
        self.log_view.pack(fill="x", side="bottom", pady=(6, 0))
        self.run_btn = ttk.Button(run, text="▶  Run", style="Accent.TButton", command=lambda: self.run(False))
        self.run_btn.pack(side="left")
        self.prev_btn = ttk.Button(run, text="👁  Preview (changes nothing)", command=lambda: self.run(True))
        self.prev_btn.pack(side="left", padx=6)
        ttk.Button(run, text="Choose files…", command=self.choose_files).pack(side="left", padx=(14, 2))
        ttk.Button(run, text="Clear", command=lambda: self.set_inputs([])).pack(side="left")
        self.inputs_lbl = ttk.Label(run, text="", style="Muted.TLabel")
        self.inputs_lbl.pack(side="left", padx=8)
        self.open_btn = ttk.Button(run, text="Open results folder", command=self.open_results, state="disabled")
        self.open_btn.pack(side="right")

        panes = ttk.PanedWindow(parent, orient="horizontal")
        panes.pack(fill="both", expand=True)
        self.stack = BrickStack(panes, self.app.theme, on_select=self.select, on_reorder=self._reorder,
                                on_drag_out=self._drag_out, on_context=self._context_menu, on_key=self._key)
        self.palette = BrickPalette(panes, self.app.theme, self.stack, on_add=self.add_brick, on_insert=self.insert_brick)
        self.side = ttk.Frame(panes, padding=(10, 0, 0, 0))
        panes.add(self.palette, weight=0)
        panes.add(self.stack, weight=3)
        panes.add(self.side, weight=1)
        try:
            panes.pane(self.palette, weight=0)
            self.frame.after(50, lambda: panes.sashpos(0, 230) if self.frame.winfo_exists() else None)
        except tk.TclError:
            pass
        self.palette.configure(width=230)
        self.side.configure(width=320)

        last = self.get("last")
        if last and self.store.get(last):
            self.wf, self.saved_name = self.store.get(last).copy(), last
        self._refresh_all()

    # ------------------------------------------------------------------ data helpers
    def _view(self, i: int, step: Step) -> BrickView:
        b = BRICKS.get(step.brick)
        if b is None:
            return BrickView("Unknown brick", "❓", "#667085", step.brick, step.enabled)
        summary = ""
        try:
            summary = b.summary(Params(b, step.params)) if b.summary else ""
        except Exception:
            summary = ""
        res = self.results.get(i)
        badge = ""
        if res:
            badge = {"error": "failed", "skipped": "skipped", "stopped": "stopped"}.get(res.status) or \
                    (f"{res.n_in} → {res.n_out}" if res.n_in != res.n_out else f"{res.n_out}")
        problems = self.issues.get(i, [])
        return BrickView(b.title, b.icon, b.color, summary, step.enabled, badge, res.status if res else "",
                         problems[0] if problems else "")

    def _refresh_all(self) -> None:
        names = self.store.names()
        self.combo.configure(values=names)
        self.name_var.set(self.saved_name or "")
        self._refresh_stack()
        self._refresh_inputs()
        self._refresh_title()

    def _refresh_stack(self, rebuild_side: bool = True) -> None:
        self.issues = check_workflow(self.wf)
        self.stack.set_items([self._view(i, s) for i, s in enumerate(self.wf.steps)], self.selected)
        if rebuild_side:
            self._build_side()

    def _refresh_title(self) -> None:
        self.dirty_lbl.configure(text="● unsaved" if self.dirty else ("saved" if self.saved_name else "not saved yet"))

    def _refresh_inputs(self) -> None:
        n = len(self.inputs)
        uses = self.wf.starts_with_input()
        if n:
            text = f"{n} file(s) ready" + ("" if uses else "  (this workflow does not use them)")
        else:
            text = "Drop files on this window to give them to the workflow" if uses else ""
        self.inputs_lbl.configure(text=text)

    def touch(self, results_stale: bool = True, rebuild_side: bool = True) -> None:
        """The workflow was edited.  (Typing in a setting must not rebuild the settings panel: focus would be lost.)"""
        self.dirty = True
        if results_stale:
            self.results.clear()
        self._refresh_stack(rebuild_side)
        self._refresh_inputs()
        self._refresh_title()

    # ------------------------------------------------------------------ editing the stack
    def select(self, index: int | None) -> None:
        if index == self.selected:
            return
        self.selected = index
        self.stack.set_items(self.stack.items, index)
        self._build_side()

    def _new_step(self, brick_id: str) -> Step:
        b = BRICKS[brick_id]
        return Step(brick_id, {p.key: p.default for p in b.params})

    def add_brick(self, brick_id: str) -> None:
        """Click on a brick in the box: add it under the selected brick (or at the end)."""
        at = (self.selected + 1) if self.selected is not None else len(self.wf.steps)
        self.insert_brick(brick_id, at)

    def insert_brick(self, brick_id: str, slot: int) -> None:
        if brick_id not in BRICKS:
            return
        self.wf.steps.insert(max(0, min(slot, len(self.wf.steps))), self._new_step(brick_id))
        self.selected = max(0, min(slot, len(self.wf.steps) - 1))
        self.touch()
        self.stack.scroll_to(self.selected)

    def _reorder(self, index: int, slot: int) -> None:
        order = reorder(list(range(len(self.wf.steps))), index, slot)
        if order == list(range(len(self.wf.steps))):
            return
        self.wf.steps = [self.wf.steps[i] for i in order]
        self.selected = order.index(index)
        self.touch()

    def _drag_out(self, index: int, x_root: int, y_root: int) -> None:
        """A brick dropped back onto the box is removed (like putting it back in the box)."""
        if self.palette.contains_root(x_root, y_root):
            self.remove_brick(index)

    def remove_brick(self, index: int | None = None) -> None:
        i = self.selected if index is None else index
        if i is None or not 0 <= i < len(self.wf.steps):
            return
        del self.wf.steps[i]
        self.selected = min(i, len(self.wf.steps) - 1) if self.wf.steps else None
        self.touch()

    def duplicate_brick(self, index: int | None = None) -> None:
        i = self.selected if index is None else index
        if i is None or not 0 <= i < len(self.wf.steps):
            return
        src = self.wf.steps[i]
        self.wf.steps.insert(i + 1, Step(src.brick, dict(src.params), src.enabled))
        self.selected = i + 1
        self.touch()

    def move_brick(self, delta: int) -> None:
        i = self.selected
        if i is None or not 0 <= i + delta < len(self.wf.steps):
            return
        s = self.wf.steps
        s[i], s[i + delta] = s[i + delta], s[i]
        self.selected = i + delta
        self.touch()
        self.stack.scroll_to(self.selected)

    def toggle_brick(self, index: int | None = None) -> None:
        i = self.selected if index is None else index
        if i is None or not 0 <= i < len(self.wf.steps):
            return
        self.wf.steps[i].enabled = not self.wf.steps[i].enabled
        self.touch()

    def _key(self, name: str) -> None:
        n = len(self.wf.steps)
        if name == "delete":
            self.remove_brick()
        elif name == "toggle":
            self.toggle_brick()
        elif name == "move_up":
            self.move_brick(-1)
        elif name == "move_down":
            self.move_brick(1)
        elif name in ("up", "down") and n:
            cur = -1 if self.selected is None else self.selected
            self.select(max(0, min(n - 1, cur + (-1 if name == "up" else 1))))
            self.stack.scroll_to(self.selected)

    def _context_menu(self, index: int, x_root: int, y_root: int) -> None:
        m = tk.Menu(self.frame, tearoff=False)
        on = self.wf.steps[index].enabled
        m.add_command(label="Switch off" if on else "Switch on", accelerator="Space", command=lambda: self.toggle_brick(index))
        m.add_command(label="Duplicate", command=lambda: self.duplicate_brick(index))
        m.add_command(label="Move up", accelerator="Alt+↑", command=lambda: self.move_brick(-1))
        m.add_command(label="Move down", accelerator="Alt+↓", command=lambda: self.move_brick(1))
        m.add_separator()
        m.add_command(label="Remove", accelerator="Del", command=lambda: self.remove_brick(index))
        try:
            m.tk_popup(x_root, y_root)
        finally:
            m.grab_release()

    # ------------------------------------------------------------------ the settings panel
    def _build_side(self) -> None:
        for w in self.side.winfo_children():
            w.destroy()
        step = self.wf.steps[self.selected] if self.selected is not None and self.selected < len(self.wf.steps) else None
        if step is None:
            self._build_workflow_form()
        else:
            self._build_brick_form(step)

    def _build_workflow_form(self) -> None:
        s = self.side
        ttk.Label(s, text="This workflow", style="H3.TLabel").pack(anchor="w")
        ttk.Label(s, text="Click a brick to set it up.", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(s, text="Name").pack(anchor="w")
        name = tk.StringVar(value=self.wf.name)
        ttk.Entry(s, textvariable=name).pack(fill="x", pady=(0, 6))
        name.trace_add("write", lambda *_: self._set_wf("name", name.get()))
        ttk.Label(s, text="What it is for (optional)").pack(anchor="w")
        desc = tk.StringVar(value=self.wf.description)
        ttk.Entry(s, textvariable=desc).pack(fill="x", pady=(0, 6))
        desc.trace_add("write", lambda *_: self._set_wf("description", desc.get()))
        ttk.Label(s, text="Folder for new files it makes").pack(anchor="w")
        out = tk.StringVar(value=self.wf.output_folder)
        FolderEntry(s, out, "dir", on_change=lambda v: self._set_wf("output_folder", v), width=24, open_button=True).pack(fill="x")
        ttk.Label(s, text="Empty = the default from Settings > Workflows. Each run gets its own sub-folder.",
                  style="Muted.TLabel", wraplength=290, justify="left").pack(anchor="w", pady=(2, 10))
        problems = [f"Brick {i + 1}: {m[0]}" for i, m in sorted(self.issues.items())]
        if problems:
            ttk.Label(s, text="To fix before running", style="H3.TLabel").pack(anchor="w")
            for text in problems:
                ttk.Label(s, text="⚠ " + text, wraplength=290, justify="left",
                          foreground=self.app.theme.p["warn"]).pack(anchor="w", pady=1)
        tk.Label(s, text="", height=1).pack()
        ttk.Label(s, text=("Tips:\n• Drag a brick to reorder it; drag it back to the box to remove it.\n"
                           "• Right-click a brick to switch it off, copy or remove it.\n"
                           "• Preview shows what would happen without touching any file."),
                  style="Muted.TLabel", wraplength=290, justify="left").pack(anchor="w", pady=(8, 0))

    def _set_wf(self, key: str, value: str) -> None:
        if getattr(self.wf, key) != value:
            setattr(self.wf, key, value)
            self.dirty = True
            self._refresh_title()

    def _build_brick_form(self, step: Step) -> None:
        s, b = self.side, BRICKS.get(step.brick)
        if b is None:
            ttk.Label(s, text=f"Unknown brick '{step.brick}'.\nIt may come from a newer version of Utils Hub.",
                      wraplength=290, justify="left").pack(anchor="w")
            ttk.Button(s, text="Remove it", command=self.remove_brick).pack(anchor="w", pady=8)
            return
        head = tk.Label(s, text=f" {b.icon}  {b.title}", bg=b.color, fg="#ffffff", anchor="w", padx=8, pady=6,
                        font=("TkDefaultFont", 11, "bold"))
        head.pack(fill="x")
        ttk.Label(s, text=b.description, style="Muted.TLabel", wraplength=290, justify="left").pack(anchor="w", pady=(6, 8))
        for msg in self.issues.get(self.selected, []):
            ttk.Label(s, text="⚠ " + msg, wraplength=290, justify="left", foreground=self.app.theme.p["warn"]).pack(anchor="w", pady=(0, 4))
        res = self.results.get(self.selected)
        if res and res.message:
            colour = self.app.theme.level_color(_LEVEL.get(res.status, "info"))
            ttk.Label(s, text=f"Last run: {res.message}", wraplength=290, justify="left", foreground=colour).pack(anchor="w", pady=(0, 6))
        for prm in b.params:
            self._param_row(s, step, prm)
        row = ttk.Frame(s)
        row.pack(fill="x", pady=(12, 0))
        on = tk.BooleanVar(value=step.enabled)
        ttk.Checkbutton(row, text="This brick is switched on", variable=on,
                        command=lambda: self.toggle_brick()).pack(anchor="w")
        ttk.Button(row, text="Duplicate", command=self.duplicate_brick).pack(side="left", pady=(6, 0))
        ttk.Button(row, text="Remove", style="Danger.TButton", command=self.remove_brick).pack(side="left", padx=6, pady=(6, 0))

    def _param_row(self, parent: ttk.Frame, step: Step, prm) -> None:
        ttk.Label(parent, text=prm.label + (" *" if prm.required else "")).pack(anchor="w", pady=(6, 0))
        cur = step.params.get(prm.key, prm.default)

        def put(value) -> None:
            step.params[prm.key] = value
            self.touch(results_stale=False, rebuild_side=False)

        if prm.kind == "bool":
            var = tk.BooleanVar(value=bool(cur))
            w = ttk.Checkbutton(parent, variable=var, text="Yes", command=lambda: put(var.get()))
        elif prm.kind == "choice":
            pairs = prm.choice_pairs()
            label_of = {v: l for v, l in pairs}
            var = tk.StringVar(value=label_of.get(cur, str(cur)))
            w = ttk.Combobox(parent, textvariable=var, values=[l for _, l in pairs], state="readonly")
            w.bind("<<ComboboxSelected>>", lambda e: put(next((v for v, l in pairs if l == var.get()), var.get())))
        elif prm.kind in ("folder", "file", "append"):
            var = tk.StringVar(value=str(cur or ""))
            kind = "dir" if prm.kind == "folder" else prm.kind
            w = FolderEntry(parent, var, kind, on_change=put, width=24, open_button=(prm.kind == "folder"),
                            filetypes=[("CSV (opens in Excel)", "*.csv")] if prm.kind == "append" else None)
        else:
            var = tk.StringVar(value=str(cur if cur is not None else ""))
            w = ttk.Entry(parent, textvariable=var)
            var.trace_add("write", lambda *_: put(self._typed(prm.kind, var.get())))
        w.pack(fill="x")
        if prm.help:
            tip(w, prm.help)
            ttk.Label(parent, text=prm.help, style="Muted.TLabel", wraplength=290, justify="left").pack(anchor="w")

    @staticmethod
    def _typed(kind: str, text: str):
        if kind in ("int", "float"):
            try:
                return int(text) if kind == "int" else float(text)
            except ValueError:
                return text
        return text

    # ------------------------------------------------------------------ saved workflows
    def _confirm_discard(self) -> bool:
        return not self.dirty or confirm(self.frame, "Your changes to this workflow are not saved. Discard them?")

    def _load(self, wf: Workflow, saved_name: str | None, dirty: bool = False) -> None:
        self.wf, self.saved_name, self.dirty = wf, saved_name, dirty
        self.selected, self.results = None, {}
        if saved_name:
            self.put("last", saved_name)
        self._refresh_all()

    def new(self) -> None:
        if self._confirm_discard():
            self._load(Workflow(self.store.unique_name("New workflow")), None)

    def _pick_saved(self) -> None:
        name = self.name_var.get()
        wf = self.store.get(name)
        if wf is None or name == self.saved_name:
            return
        if not self._confirm_discard():
            self.name_var.set(self.saved_name or "")
            return
        self._load(wf.copy(), name)

    def load_template(self, template: Workflow) -> None:
        if not self._confirm_discard():
            return
        wf = template.copy()
        wf.name = self.store.unique_name(wf.name)
        self._load(wf, None, dirty=True)
        self.log("Template loaded. Click each brick to fill in its settings, then Save.")

    def save(self) -> bool:
        name = (self.wf.name or "").strip() or "Workflow"
        self.wf.name = name
        if name != self.saved_name and self.store.get(name) and \
                not confirm(self.frame, f"A workflow called '{name}' already exists. Replace it?"):
            return False
        if self.saved_name and self.saved_name != name:
            self.store.remove(self.saved_name)                   # it was renamed
        self.store.put(self.wf)
        self.saved_name, self.dirty = name, False
        self.put("last", name)
        self._refresh_all()
        self.app.notify(f"Saved '{name}'", "ok")
        return True

    def save_as(self) -> None:
        name = ask_text(self.frame, "Save workflow", "Name for this workflow:", self.wf.name)
        if name and name.strip():
            self.wf.name = name.strip()
            self.saved_name = None                               # a copy: keep the original under its old name
            self.save()

    def delete(self) -> None:
        if not self.saved_name:
            self._load(Workflow(self.store.unique_name("New workflow")), None)
            return
        if confirm(self.frame, f"Delete the workflow '{self.saved_name}'?"):
            self.store.remove(self.saved_name)
            self._load(Workflow(self.store.unique_name("New workflow")), None)

    def export_file(self) -> None:
        path = filedialog.asksaveasfilename(title="Export workflow", defaultextension=".json", initialfile=f"{self.wf.name}.json",
                                            filetypes=[("Workflow", "*.json")], parent=self.frame.winfo_toplevel())
        if path:
            export_workflow(self.wf, path)
            self.app.notify("Workflow exported", "ok")

    def import_file(self) -> None:
        path = filedialog.askopenfilename(title="Import workflow", filetypes=[("Workflow", "*.json")], parent=self.frame.winfo_toplevel())
        if not path or not self._confirm_discard():
            return
        try:
            wf = import_workflow(path)
        except ValueError as exc:
            error(self.frame, str(exc))
            return
        wf.name = self.store.unique_name(wf.name)
        self._load(wf, None, dirty=True)

    # ------------------------------------------------------------------ input files
    def choose_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Files for the workflow", parent=self.frame.winfo_toplevel())
        if paths:
            self.set_inputs(list(paths))

    def set_inputs(self, paths: list[str]) -> None:
        self.inputs = paths
        if self.built:
            self._refresh_inputs()

    def accept_files(self, paths: list[str]) -> None:
        files: list[str] = []
        for p in paths:
            path = Path(p)
            if path.is_dir():
                files += sorted(str(f) for f in path.iterdir() if f.is_file() and not f.name.startswith((".", "~$")))
            elif path.is_file():
                files.append(str(path))
        self.set_inputs(files)
        if files and not self.wf.starts_with_input():
            self.log("These files are ready, but this workflow does not start with 'Files I give it'.", "warn")
        elif files:
            self.log(f"{len(files)} file(s) ready. Press Run (or Preview).")

    # ------------------------------------------------------------------ running
    def _confirm_run(self) -> bool:
        if not self.settings.get_bool("workflows.confirm_changes", True):
            return True
        risky = []
        for s in self.wf.steps:
            if not s.enabled:
                continue
            if s.brick == "rename":
                risky.append("rename your files")
            elif s.brick == "to_folder" and s.params.get("action") == "move":
                risky.append("move your files")
        if not risky:
            return True
        return confirm(self.frame, "This workflow will " + " and ".join(dict.fromkeys(risky)) +
                       ".\nYou can undo it afterwards in History & Undo.\n\nRun it now? (Use Preview to see first.)")

    def run(self, preview: bool = False) -> None:
        if self.running:
            return
        issues = check_workflow(self.wf)
        if issues:
            first = min(issues)
            self.select(first)
            self.stack.scroll_to(first)
            self.log(f"Brick {first + 1}: {issues[first][0]}", "warn")
            self.app.notify(f"Brick {first + 1} needs attention", "warn")
            return
        if self.wf.starts_with_input() and not self.inputs:
            self.choose_files()
            if not self.inputs:
                return
        if not preview and not self._confirm_run():
            return
        self.results.clear()
        self._refresh_stack()
        self.running = True
        self.run_btn.configure(state="disabled")
        self.prev_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        wf, inputs, bridge = self.wf.copy(), list(self.inputs), self.app.bridge
        out_root = Path(self.get("output_root") or data_dir() / "workflow_output")
        cfg = OcrConfig.from_settings(self.settings)
        self.log(f"{'Preview of' if preview else 'Running'} '{wf.name}' ({len(wf.steps)} bricks)…")

        def work(job):
            env = RunEnv(out_root=out_root, journal=self.app.journal, ocr_cfg=cfg,
                         log=lambda m, lv="info": bridge.call(self.log, m, lv),
                         cancelled=lambda: job.cancelled,
                         progress=lambda d, t, m="": job.progress(d, t, m),
                         notify=lambda m: bridge.call(self.app.notify, m, "ok"),
                         open_path=lambda p: bridge.call(open_path, p), tool_id=self.id)
            return run_workflow(wf, inputs, env, preview, on_step=lambda r: bridge.call(self._step_done, r))

        self.app.jobs.submit(work, title=f"{'Preview' if preview else 'Workflow'}: {wf.name}", tool=self.id,
                             on_done=self._finished, on_error=self._failed)

    def _step_done(self, res: StepResult) -> None:
        if not self.built:
            return
        self.results[res.index] = res
        self._refresh_stack()

    def _finished(self, report) -> None:
        self.running = False
        self.last_report = report
        if not self.built:
            return
        self.run_btn.configure(state="normal")
        self.prev_btn.configure(state="normal")
        level = "ok" if report.ok else "error"
        self.log(report.summary() if report.ok else report.message, level)
        for f in report.made[:20]:
            self.log(f"  made: {f}")
        if len(report.made) > 20:
            self.log(f"  … and {len(report.made) - 20} more")
        if report.work_dir or report.made:
            self.open_btn.configure(state="normal")
        self.app.notify(report.summary() if report.ok else report.message, "ok" if report.ok else "warn")
        self._refresh_stack()

    def _failed(self, message: str) -> None:
        self.running = False
        if self.built:
            self.run_btn.configure(state="normal")
            self.prev_btn.configure(state="normal")
        self.log(message, "error")

    def open_results(self) -> None:
        r = self.last_report
        if r and r.work_dir:
            open_path(r.work_dir)
        elif r and r.made:
            open_path(Path(r.made[-1]).parent)

    # ------------------------------------------------------------------ app integration
    def log(self, message: str, level: str = "info") -> None:
        super().log(message, level)
        if self.built:
            self.log_view.log(message, level)

    def commands(self) -> list[Command]:
        def runner(name: str):
            def go() -> None:
                self.app.show_tool(self.id)
                wf = self.store.get(name)
                if wf is not None and self._confirm_discard():
                    self._load(wf.copy(), name)
                    self.run(False)
            return go
        return [Command(f"workflow.run.{w.name}", f"Run workflow: {w.name}", runner(w.name), "Workflows", w.description)
                for w in self.store.items]

    def stats(self) -> dict[str, int | str]:
        return {"Saved workflows": len(self.store.items)}

    def on_show(self) -> None:
        self._refresh_all()
