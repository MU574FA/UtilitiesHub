"""Workflows: small "bricks" snapped one after another into a procedure.

A *brick* is one step (get files, keep only PDFs, merge, copy to a folder ...).  A *workflow* is an ordered list
of bricks.  The files flow from one brick to the next like this:

    [Files I drop in] -> [Keep only types: pdf] -> [Merge into one PDF] -> [Put files in a folder]

Everything here is pure logic (no Tk), so it is testable and could also be run from a script or a watcher:

    wf = Workflow("Photos to PDF", [Step("given"), Step("sort", {"by": "name"}), Step("merge_pdf", {"name": "photos"})])
    report = run_workflow(wf, inputs=["a.jpg", "b.jpg"], env=RunEnv(out_root=Path("out")))

Adding a brick = writing one function and decorating it with @brick(...)  (see the bottom of the file for examples).
Bricks that need pymupdf / OCR import them lazily, so the engine itself loads without them.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .fileops import Journal, rename_in_place, transfer, resolve_target
from .textutil import human_size, parse_ext_list, split_list

log = logging.getLogger(__name__)

# group id -> (title, colour).  Colours are the "plastic" of the bricks in the UI.
GROUPS: dict[str, tuple[str, str]] = {
    "source": ("Get files", "#2f80ed"),
    "filter": ("Filter", "#f2994a"),
    "pdf": ("PDF & pages", "#eb5757"),
    "organise": ("Organise", "#27ae60"),
    "read": ("Read & record", "#9b51e0"),
    "finish": ("Finish", "#667085"),
}


# --------------------------------------------------------------------------- declarations
@dataclass
class Param:
    """One setting of a brick.  kind: str | text | int | float | bool | choice | folder | file | append"""
    key: str
    label: str
    kind: str = "str"
    default: Any = ""
    choices: list = field(default_factory=list)       # values, or (value, label) pairs
    help: str = ""
    required: bool = False

    def choice_pairs(self) -> list[tuple[Any, str]]:
        return [(c[0], str(c[1])) if isinstance(c, (tuple, list)) else (c, str(c)) for c in self.choices]


@dataclass
class Brick:
    id: str
    title: str
    group: str
    icon: str
    description: str
    params: list[Param]
    run: Callable[["Flow", "Params"], str | None]
    source: bool = False               # starts a workflow (does not need incoming files)
    side_effect: bool = False          # changes things outside the work folder (moves, renames, writes a CSV ...)
    summary: Callable[["Params"], str] | None = None      # one-line text shown on the brick
    virtual_ok: bool = False           # works on names only, so it can follow a preview step whose files don't exist yet

    @property
    def color(self) -> str:
        return GROUPS[self.group][1]


BRICKS: dict[str, Brick] = {}


def brick(id: str, title: str, group: str, icon: str, description: str, params: list[Param] | None = None, *,
          source: bool = False, side_effect: bool = False, summary: Callable[["Params"], str] | None = None,
          virtual_ok: bool = False):
    def deco(fn):
        BRICKS[id] = Brick(id, title, group, icon, description, params or [], fn, source, side_effect, summary, virtual_ok)
        return fn
    return deco


def bricks_by_group() -> list[tuple[str, str, str, list[Brick]]]:
    """[(group id, title, colour, [bricks])] in display order."""
    return [(g, t, c, [b for b in BRICKS.values() if b.group == g]) for g, (t, c) in GROUPS.items()]


class Params:
    """Step settings with the brick's defaults filled in:  p.str("name"), p.int("n"), p.bool("x") ..."""

    def __init__(self, brick_: Brick, values: dict) -> None:
        self._defaults = {p.key: p.default for p in brick_.params}
        self._v = values or {}

    def get(self, key: str, fallback: Any = None) -> Any:
        v = self._v.get(key, self._defaults.get(key, fallback))
        return self._defaults.get(key, fallback) if v is None else v

    def str(self, key: str) -> str:
        return str(self.get(key, "") or "").strip()

    def int(self, key: str) -> int:
        try:
            return int(float(self.get(key, 0) or 0))
        except (TypeError, ValueError):
            return 0

    def float(self, key: str) -> float:
        try:
            return float(self.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def bool(self, key: str) -> bool:
        v = self.get(key, False)
        return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")

    def words(self, key: str) -> list[str]:
        return split_list(self.str(key))


# --------------------------------------------------------------------------- the workflow itself
@dataclass
class Step:
    brick: str
    params: dict = field(default_factory=dict)
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict:
        return {"brick": self.brick, "params": self.params, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(str(d.get("brick", "")), dict(d.get("params") or {}), bool(d.get("enabled", True)))


@dataclass
class Workflow:
    name: str = "New workflow"
    steps: list[Step] = field(default_factory=list)
    description: str = ""
    output_folder: str = ""            # where files MADE by the workflow go ("" = the app's default)

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "output_folder": self.output_folder,
                "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict) -> "Workflow":
        return cls(str(d.get("name") or "Workflow"), [Step.from_dict(s) for s in d.get("steps") or []],
                   str(d.get("description") or ""), str(d.get("output_folder") or ""))

    def copy(self) -> "Workflow":
        return Workflow.from_dict(copy.deepcopy(self.to_dict()))

    def starts_with_input(self) -> bool:
        first = next((s for s in self.steps if s.enabled), None)
        return bool(first and first.brick == "given")


def check_workflow(wf: Workflow) -> dict[int, list[str]]:
    """Problems per step index (empty dict = all good).  Used for the warning marks on the bricks."""
    issues: dict[int, list[str]] = {}
    have_files = False
    for i, step in enumerate(wf.steps):
        b = BRICKS.get(step.brick)
        if b is None:
            issues.setdefault(i, []).append(f"Unknown brick '{step.brick}' (from a newer version?).")
            continue
        if not step.enabled:
            continue
        if not b.source and not have_files:
            issues.setdefault(i, []).append("Nothing flows into this brick yet. Put a 'Get files' brick above it.")
        values = Params(b, step.params)
        for p in b.params:
            if p.required and not values.str(p.key):
                issues.setdefault(i, []).append(f"'{p.label}' is empty.")
        have_files = have_files or b.source
    return issues


# --------------------------------------------------------------------------- running
class FlowStop(Exception):
    """A brick asks the workflow to stop politely (e.g. no files left).  Not an error."""


@dataclass
class RunEnv:
    """What the engine needs from the outside world (the app supplies the real ones)."""
    out_root: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "utilshub_workflows")
    journal: Journal | None = None
    ocr_cfg: Any = None                                # core.ocr.OcrConfig (None = defaults)
    llm_cfg: Any = None                                # core.llm.LlmConfig (None = the one inside ocr_cfg, else AI off)
    log: Callable[[str, str], None] = lambda message, level="info": None
    cancelled: Callable[[], bool] = lambda: False
    progress: Callable[[int, int, str], None] = lambda done, total, message="": None
    notify: Callable[[str], None] = lambda message: None
    open_path: Callable[[Path], None] = lambda path: None
    tool_id: str = "workflows"


class Flow:
    """Everything the bricks share while one workflow runs."""

    def __init__(self, wf: Workflow, env: RunEnv, preview: bool, inputs: list[Path]) -> None:
        self.wf, self.env, self.preview = wf, env, preview
        self.inputs = inputs
        self.files: list[Path] = []
        self.texts: dict[str, str] = {}                # path -> extracted text (Read text brick)
        self.rows: dict[str, dict] = {}                # path -> values collected for the CSV brick
        self.written: list[Path] = []                  # new files made by this run
        self.virtual: set[Path] = set()                # preview only: files that WOULD exist after a rename / move
        self._work: Path | None = None
        self._tmp: Path | None = None
        self.started = datetime.now()

    # ---- folders
    @property
    def work_dir(self) -> Path:
        """Folder for files the workflow MAKES (originals are never touched by those bricks)."""
        if self._work is None:
            if self.preview:
                self._tmp = Path(tempfile.mkdtemp(prefix="utilshub_preview_"))
                self._work = self._tmp
            else:
                base = Path(self.wf.output_folder.strip().strip('"')) if self.wf.output_folder.strip() else self.env.out_root
                safe = re.sub(r'[\\/:*?"<>|]+', "_", self.wf.name).strip() or "workflow"
                self._work = base / safe / self.started.strftime("%Y%m%d-%H%M%S")
                self._work.mkdir(parents=True, exist_ok=True)
        return self._work

    def new_file(self, stem: str, suffix: str) -> Path:
        """A fresh path inside the work folder (numbered if the name is taken)."""
        safe = re.sub(r'[\\/:*?"<>|]+', "_", stem).strip(" .") or "file"
        target = self.work_dir / f"{safe}{suffix}"
        n = 2
        while target.exists():
            target = self.work_dir / f"{safe} ({n}){suffix}"
            n += 1
        return target

    def cleanup(self) -> None:
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    # ---- helpers for bricks
    def log(self, message: str, level: str = "info") -> None:
        self.env.log(message, level)

    def check_cancel(self) -> None:
        if self.env.cancelled():
            raise FlowStop("Cancelled.")

    def row(self, path: Path) -> dict:
        return self.rows.setdefault(str(path), {})

    def move_state(self, old: Path, new: Path) -> None:
        """Keep collected text / values attached to a file that was renamed or moved."""
        for store in (self.texts, self.rows):
            if str(old) in store:
                store[str(new)] = store.pop(str(old))

    def ocr_cfg(self):
        if self.env.ocr_cfg is not None:
            return self.env.ocr_cfg
        from .ocr import OcrConfig
        return OcrConfig()

    def llm_cfg(self):
        """The local-AI settings (disabled when the app did not provide any)."""
        if self.env.llm_cfg is not None:
            return self.env.llm_cfg
        inner = getattr(self.ocr_cfg(), "llm", None)
        if inner is not None:
            return inner
        from .llm import LlmConfig
        return LlmConfig()

    def read_text(self, path: Path) -> str:
        key = str(path)
        if path in self.virtual:
            return ""
        if key not in self.texts:
            from .readers import read_document
            info = read_document(path, self.ocr_cfg())
            if info.error:
                self.log(f"{path.name}: {info.error}", "warn")
            self.texts[key] = info.text or ""
        return self.texts[key]


@dataclass
class StepResult:
    index: int
    brick: str
    title: str
    status: str = "ok"                  # ok | warn | error | skipped | stopped
    n_in: int = 0
    n_out: int = 0
    message: str = ""
    seconds: float = 0.0


@dataclass
class RunReport:
    workflow: str
    preview: bool
    steps: list[StepResult] = field(default_factory=list)
    ok: bool = True
    message: str = ""
    files: list[str] = field(default_factory=list)       # files at the end of the workflow
    made: list[str] = field(default_factory=list)        # new files written by the workflow
    work_dir: str = ""

    def summary(self) -> str:
        kind = "Preview" if self.preview else "Done"
        return f"{kind}: {self.message}" if self.message else kind


def run_workflow(wf: Workflow, inputs: list[str | Path] | None = None, env: RunEnv | None = None,
                 preview: bool = False, on_step: Callable[[StepResult], None] | None = None) -> RunReport:
    """Run the bricks top to bottom.  `preview=True` changes nothing outside a throw-away folder: bricks that move,
    rename or write elsewhere only say what they WOULD do."""
    env = env or RunEnv()
    flow = Flow(wf, env, preview, [Path(p) for p in (inputs or [])])
    report = RunReport(wf.name, preview)
    problems = check_workflow(wf)
    if problems:
        first = min(problems)
        report.ok, report.message = False, f"Step {first + 1}: {problems[first][0]}"
        return report
    active = [(i, s) for i, s in enumerate(wf.steps) if s.enabled]
    if not active:
        report.ok, report.message = False, "The workflow has no bricks yet."
        return report
    try:
        for n, (i, step) in enumerate(active):
            b = BRICKS[step.brick]
            res = StepResult(i, b.id, b.title, n_in=len(flow.files))
            t0 = time.monotonic()
            env.progress(n, len(active), b.title)
            try:
                flow.check_cancel()
                if preview and not b.virtual_ok and not b.source and any(f in flow.virtual for f in flow.files):
                    res.status, res.n_out = "skipped", len(flow.files)
                    res.message = "preview stops here: these files only exist once the earlier step really runs"
                    report.steps.append(res)
                    if on_step:
                        on_step(res)
                    env.log(f"{b.icon} {b.title}: {res.message}", "info")
                    continue
                res.message = b.run(flow, Params(b, step.params)) or ""
                res.n_out = len(flow.files)
            except FlowStop as stop:
                res.status, res.message, res.n_out = "stopped", str(stop), len(flow.files)
                report.message = str(stop)
                report.steps.append(res)
                res.seconds = time.monotonic() - t0
                if on_step:
                    on_step(res)
                report.ok = str(stop) != "Cancelled."
                break
            except Exception as exc:                          # a brick failed: stop here, keep what was done
                log.exception("Brick %s failed", b.id)
                res.status, res.message = "error", f"{exc.__class__.__name__}: {exc}" if not str(exc) else str(exc)
                res.n_out = len(flow.files)
                report.steps.append(res)
                res.seconds = time.monotonic() - t0
                if on_step:
                    on_step(res)
                report.ok, report.message = False, f"'{b.title}' failed: {res.message}"
                break
            res.seconds = time.monotonic() - t0
            if "skipped" in res.message.lower() or "could not" in res.message.lower():
                res.status = "warn"
            report.steps.append(res)
            if on_step:
                on_step(res)
            env.log(f"{b.icon} {b.title}: {res.message or str(res.n_out) + ' file(s)'}", "warn" if res.status == "warn" else "info")
        else:
            report.message = f"{len(flow.files)} file(s) at the end"
        report.files = [str(p) for p in flow.files]
        report.made = [str(p) for p in flow.written]
        report.work_dir = str(flow._work) if flow._work and not preview else ""
    finally:
        flow.cleanup()
    return report


# --------------------------------------------------------------------------- store
class WorkflowStore:
    """The user's own workflows, one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.items: list[Workflow] = []
        self.load()

    def load(self) -> None:
        self.items = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [Workflow.from_dict(d) for d in data.get("workflows", [])]
        except (OSError, ValueError, AttributeError, TypeError):
            log.exception("Could not read %s", self.path)
            try:
                self.path.replace(self.path.with_suffix(".broken.json"))      # keep it, don't lose the user's work
            except OSError:
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "workflows": [w.to_dict() for w in self.items]}, indent=2,
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def names(self) -> list[str]:
        return [w.name for w in self.items]

    def get(self, name: str) -> Workflow | None:
        return next((w for w in self.items if w.name == name), None)

    def put(self, wf: Workflow) -> None:
        for i, w in enumerate(self.items):
            if w.name == wf.name:
                self.items[i] = wf.copy()
                break
        else:
            self.items.append(wf.copy())
        self.save()

    def remove(self, name: str) -> None:
        self.items = [w for w in self.items if w.name != name]
        self.save()

    def unique_name(self, base: str) -> str:
        names, n, name = set(self.names()), 2, base
        while name in names:
            name, n = f"{base} ({n})", n + 1
        return name


def export_workflow(wf: Workflow, path: str | Path) -> None:
    Path(path).write_text(json.dumps({"utilshub_workflow": 1, **wf.to_dict()}, indent=2, ensure_ascii=False), encoding="utf-8")


def import_workflow(path: str | Path) -> Workflow:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Not a workflow file: {exc}") from exc
    if not isinstance(data, dict) or "steps" not in data:
        raise ValueError("This file does not contain a workflow.")
    return Workflow.from_dict(data)


# =========================================================================== the bricks
def _dedupe(paths: list[Path]) -> list[Path]:
    seen, out = set(), []
    for p in paths:
        k = str(p.resolve()) if p.exists() else str(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _is_pdf(p: Path) -> bool:
    return p.suffix.lower() == ".pdf"


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _pdfs_only(flow: Flow, what: str) -> list[Path]:
    pdfs = [p for p in flow.files if _is_pdf(p)]
    if len(pdfs) < len(flow.files):
        flow.log(f"{what}: skipped {len(flow.files) - len(pdfs)} file(s) that are not PDFs", "warn")
    return pdfs


def _plural(n: int, word: str = "file") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------- get files
@brick("given", "Files I give it", "source", "📥",
       "Starts with the files you drop on the Workflows screen (or choose with 'Choose files').", source=True)
def b_given(flow: Flow, p: Params):
    files = _dedupe([f for f in flow.inputs if f.is_file()])
    if not files:
        raise FlowStop("No files were given. Drop files on the Workflows screen or press 'Choose files…'.")
    flow.files = files
    return _plural(len(files)) + " given"


@brick("folder", "Files in a folder", "source", "📂", "Starts with the files found in a folder.",
       [Param("folder", "Folder", "folder", "", required=True),
        Param("types", "Only these types", "str", "", help="e.g. pdf, jpg   (empty = every file)"),
        Param("recursive", "Include sub-folders", "bool", False)], source=True,
       summary=lambda p: f"{p.str('folder') or '(choose a folder)'}" + (f"  •  {p.str('types')}" if p.str("types") else ""))
def b_folder(flow: Flow, p: Params):
    root = Path(p.str("folder").strip('"'))
    if not root.is_dir():
        raise FlowStop(f"Folder not found: {root}")
    exts = parse_ext_list(p.str("types"))
    it = root.rglob("*") if p.bool("recursive") else root.iterdir()
    files = []
    for f in it:
        try:
            if f.is_file() and not f.name.startswith(("~$", ".")) and (not exts or f.suffix.lower() in exts):
                files.append(f)
        except OSError:
            continue
    flow.files = sorted(files, key=lambda x: str(x).lower())
    if not flow.files:
        raise FlowStop(f"No matching files in {root.name}.")
    return f"found {_plural(len(flow.files))}"


# ---------------------------------------------------------------- filter
@brick("keep_types", "Keep only types", "filter", "🧹", "Keeps (or removes) files by their type.",
       [Param("types", "File types", "str", "pdf", required=True, help="e.g. pdf, jpg, png, docx"),
        Param("mode", "Action", "choice", "keep", [("keep", "Keep these"), ("drop", "Remove these")])],
       summary=lambda p: f"{'keep' if p.str('mode') != 'drop' else 'remove'}: {p.str('types')}")
def b_keep_types(flow: Flow, p: Params):
    exts, keep = parse_ext_list(p.str("types")), p.str("mode") != "drop"
    before = len(flow.files)
    flow.files = [f for f in flow.files if (f.suffix.lower() in exts) == keep]
    return f"{before} → {len(flow.files)}"


@brick("name_has", "Name contains", "filter", "🔤", "Keeps (or removes) files whose name has certain words.",
       [Param("words", "Words", "str", "", required=True, help="Several words separated by commas"),
        Param("match", "Match", "choice", "any", [("any", "any of the words"), ("all", "all of the words"),
                                                  ("starts", "name starts with"), ("ends", "name ends with"),
                                                  ("regex", "regular expression (name without .ext)")]),
        Param("mode", "Action", "choice", "keep", [("keep", "Keep matching"), ("drop", "Remove matching")])],
       summary=lambda p: f"{'keep' if p.str('mode') != 'drop' else 'remove'} {p.str('match')}: {p.str('words')}")
def b_name_has(flow: Flow, p: Params):
    words, how, keep = [w.lower() for w in p.words("words")], p.str("match"), p.str("mode") != "drop"
    if how == "regex":
        try:
            rx = re.compile(p.str("words"), re.I)
        except re.error as exc:
            raise ValueError(f"Bad regular expression: {exc}") from exc

    def hit(f: Path) -> bool:
        name = f.stem.lower()
        if how == "regex":
            return bool(rx.search(f.stem))
        if how == "all":
            return all(w in name for w in words)
        if how == "starts":
            return any(name.startswith(w) for w in words)
        if how == "ends":
            return any(name.endswith(w) for w in words)
        return any(w in name for w in words)

    before = len(flow.files)
    flow.files = [f for f in flow.files if hit(f) == keep]
    return f"{before} → {len(flow.files)}"


@brick("size", "Size between", "filter", "📏", "Keeps files by size (in MB). 0 means no limit.",
       [Param("min_mb", "At least (MB)", "float", 0), Param("max_mb", "At most (MB)", "float", 0)],
       summary=lambda p: f"{p.float('min_mb'):g} – {p.float('max_mb') or '∞'} MB")
def b_size(flow: Flow, p: Params):
    lo, hi = p.float("min_mb") * 1048576, p.float("max_mb") * 1048576
    before = len(flow.files)
    flow.files = [f for f in flow.files if _size(f) >= lo and (hi <= 0 or _size(f) <= hi)]
    return f"{before} → {len(flow.files)}"


@brick("text_has", "Text contains", "filter", "🔎", "Reads each file (OCR for scans) and keeps those mentioning the words.",
       [Param("words", "Words", "str", "", required=True, help="Several words separated by commas"),
        Param("match", "Match", "choice", "any", [("any", "any of the words"), ("all", "all of the words")]),
        Param("mode", "Action", "choice", "keep", [("keep", "Keep matching"), ("drop", "Remove matching")])],
       summary=lambda p: f"{'keep' if p.str('mode') != 'drop' else 'remove'} text with {p.str('match')}: {p.str('words')}")
def b_text_has(flow: Flow, p: Params):
    words, keep = [w.lower() for w in p.words("words")], p.str("mode") != "drop"
    out, n = [], len(flow.files)
    for i, f in enumerate(flow.files):
        flow.check_cancel()
        flow.env.progress(i, n, f.name)
        text = flow.read_text(f).lower()
        ok = all(w in text for w in words) if p.str("match") == "all" else any(w in text for w in words)
        if ok == keep:
            out.append(f)
    before, flow.files = len(flow.files), out
    return f"{before} → {len(out)}"


@brick("no_dupes", "Remove duplicates", "filter", "👯", "Drops files that are exact copies of an earlier file (same content).",
       [Param("by", "Compare", "choice", "content", [("content", "File content"), ("name", "File name")])])
def b_no_dupes(flow: Flow, p: Params):
    from .readers import file_hash
    seen, out, dupes = set(), [], 0
    for f in flow.files:
        key = file_hash(f) if p.str("by") != "name" and f.exists() else f.name.lower()
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        out.append(f)
    flow.files = out
    return f"removed {_plural(dupes, 'duplicate')}"


@brick("sort", "Sort / take first", "filter", "↕️", "Puts the files in order (matters before merging) and can keep only the first few.",
       [Param("by", "Sort by", "choice", "name", [("name", "Name (natural: 2 before 10)"), ("date", "Date modified"),
                                                   ("size", "Size"), ("type", "Type")]),
        Param("reverse", "Reverse order", "bool", False),
        Param("limit", "Keep only the first … (0 = all)", "int", 0)],
       summary=lambda p: f"by {p.str('by')}" + (" ↓" if p.bool("reverse") else "") + (f", first {p.int('limit')}" if p.int("limit") else ""))
def b_sort(flow: Flow, p: Params):
    def natural(s: str):
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s.lower())]
    keys = {"name": lambda f: natural(f.name), "date": _mtime, "size": _size, "type": lambda f: (f.suffix.lower(), natural(f.name))}
    flow.files = sorted(flow.files, key=keys.get(p.str("by"), keys["name"]), reverse=p.bool("reverse"))
    if p.int("limit") > 0:
        flow.files = flow.files[:p.int("limit")]
    return f"{_plural(len(flow.files))} in order"


@brick("stop_unless", "Continue only if", "filter", "🚦", "Stops the workflow quietly unless enough files are left.",
       [Param("min", "At least this many files", "int", 1)], summary=lambda p: f"at least {p.int('min')} file(s)")
def b_stop_unless(flow: Flow, p: Params):
    if len(flow.files) < max(1, p.int("min")):
        raise FlowStop(f"Stopped: only {_plural(len(flow.files))} left (needed {max(1, p.int('min'))}).")
    return "enough files, continuing"


# ---------------------------------------------------------------- PDF & pages
@brick("to_pdf", "Turn each into a PDF", "pdf", "📄", "Photos and Word / PowerPoint files become PDFs (one PDF per file).",
       [Param("page", "Page size (photos)", "choice", "a4", [("a4", "A4"), ("letter", "Letter"), ("fit", "Same as the photo")]),
        Param("quality", "Photo quality", "int", 85)])
def b_to_pdf(flow: Flow, p: Params):
    from .pdfops import AssembleOptions, Piece, assemble
    opts = AssembleOptions(page_size=p.str("page") or "a4", quality=max(30, min(100, p.int("quality") or 85)),
                           libreoffice_cmd=getattr(flow.ocr_cfg(), "libreoffice_cmd", ""))
    out, failed = [], 0
    for f in flow.files:
        flow.check_cancel()
        if _is_pdf(f):
            out.append(f)
            continue
        try:
            res = assemble([Piece(str(f))], flow.new_file(f.stem, ".pdf"), opts)
            out.append(Path(res.path))
            flow.written.append(Path(res.path))
            flow.move_state(f, Path(res.path))
        except Exception as exc:
            failed += 1
            flow.log(f"{f.name}: could not convert ({exc})", "warn")
    flow.files = out
    return f"{_plural(len(out), 'PDF')}" + (f", {failed} could not be converted" if failed else "")


@brick("merge_pdf", "Merge into one PDF", "pdf", "🧩", "Joins all files (PDFs, photos, Word) into a single PDF, in the current order.",
       [Param("name", "New file name", "str", "Merged"),
        Param("page", "Page size (photos)", "choice", "a4", [("a4", "A4"), ("letter", "Letter"), ("fit", "Same as the photo")]),
        Param("numbers", "Page numbers", "choice", "", [("", "None"), ("{n}", "1, 2, 3"), ("Page {n} of {total}", "Page 1 of 10")]),
        Param("watermark", "Watermark text", "str", "", help="Leave empty for none")],
       summary=lambda p: p.str("name") + ".pdf")
def b_merge_pdf(flow: Flow, p: Params):
    from .pdfops import AssembleOptions, Piece, assemble
    opts = AssembleOptions(page_size=p.str("page") or "a4", page_numbers=p.str("numbers"), watermark=p.str("watermark"),
                           libreoffice_cmd=getattr(flow.ocr_cfg(), "libreoffice_cmd", ""))
    res = assemble([Piece(str(f)) for f in flow.files], flow.new_file(p.str("name") or "Merged", ".pdf"), opts)
    out = Path(res.path)
    flow.written.append(out)
    flow.files = [out]
    extra = f", {len(res.skipped)} skipped" if res.skipped else ""
    return f"{res.pages} page(s) from {_plural(len(res.starts))}{extra}"


@brick("split_pdf", "Split PDF", "pdf", "✂️", "Cuts each PDF into parts of a fixed number of pages.",
       [Param("every", "Pages per part", "int", 1)], summary=lambda p: f"{p.int('every') or 1} page(s) per part")
def b_split_pdf(flow: Flow, p: Params):
    import pymupdf
    from .pdfops import split_pdf
    step, out = max(1, p.int("every")), []
    for f in _pdfs_only(flow, "Split"):
        flow.check_cancel()
        with pymupdf.open(f) as d:
            n = len(d)
        ranges = [list(range(i, min(i + step, n))) for i in range(0, n, step)]
        parts = [Path(x) for x in split_pdf(f, ranges, flow.work_dir, f.stem)]
        flow.written += parts
        out += parts
    flow.files = out
    return f"{_plural(len(out), 'part')}"


@brick("keep_pages", "Keep pages", "pdf", "🔢", "Keeps only some pages of each PDF, e.g. 1-3, 5, 8-",
       [Param("pages", "Pages", "str", "1", required=True, help="Like 1-3, 5, 8-  (8- means page 8 to the end)")],
       summary=lambda p: f"pages {p.str('pages')}")
def b_keep_pages(flow: Flow, p: Params):
    import pymupdf
    from .pdfops import extract_pages
    from .textutil import parse_page_ranges
    out = []
    for f in _pdfs_only(flow, "Keep pages"):
        flow.check_cancel()
        with pymupdf.open(f) as d:
            n = len(d)
        pages = parse_page_ranges(p.str("pages"), n)
        if not pages:
            flow.log(f"{f.name}: none of those pages exist (it has {n})", "warn")
            continue
        target = flow.new_file(f.stem, ".pdf")
        extract_pages(f, pages, target)
        flow.written.append(target)
        out.append(target)
    flow.files = out
    return f"{_plural(len(out), 'PDF')}"


@brick("stamp_pdf", "Page numbers / watermark", "pdf", "🏷️", "Adds page numbers and/or a diagonal watermark to each PDF (copies).",
       [Param("numbers", "Page numbers", "choice", "Page {n} of {total}", [("", "None"), ("{n}", "1, 2, 3"), ("Page {n} of {total}", "Page 1 of 10")]),
        Param("watermark", "Watermark text", "str", "")])
def b_stamp_pdf(flow: Flow, p: Params):
    import pymupdf
    from .pdfops import add_page_numbers, add_watermark
    out = []
    for f in _pdfs_only(flow, "Stamp"):
        flow.check_cancel()
        target = flow.new_file(f.stem, ".pdf")
        with pymupdf.open(f) as doc:
            if p.str("watermark"):
                add_watermark(doc, p.str("watermark"))
            if p.str("numbers"):
                add_page_numbers(doc, p.str("numbers"))
            doc.save(str(target), garbage=3, deflate=True)
        flow.written.append(target)
        flow.move_state(f, target)
        out.append(target)
    flow.files = out
    return f"{_plural(len(out), 'PDF')} stamped"


@brick("compress_pdf", "Make PDFs smaller", "pdf", "🗜️", "Shrinks PDFs (copies); tries harder until the target size is reached.",
       [Param("target_mb", "Target size (MB, 0 = just optimise)", "float", 0)],
       summary=lambda p: f"target {p.float('target_mb'):g} MB" if p.float("target_mb") else "optimise")
def b_compress_pdf(flow: Flow, p: Params):
    from .pdfops import compress_pdf
    out, before, after = [], 0, 0
    for f in _pdfs_only(flow, "Compress"):
        flow.check_cancel()
        target = flow.new_file(f.stem, ".pdf")
        size, _lvl = compress_pdf(f, target, int(p.float("target_mb") * 1048576))
        before, after = before + _size(f), after + size
        flow.written.append(target)
        flow.move_state(f, target)
        out.append(target)
    flow.files = out
    return f"{human_size(before)} → {human_size(after)}"


# ---------------------------------------------------------------- organise
_TOKENS_HELP = (
    "Tokens: {name} {ext} {n} {n:3} (001) {date} {parent} {text:20} (first 20 characters of the text) "
    "and any value made by a Grab / AI brick, e.g. {category}")


def _expand(pattern: str, f: Path, n: int, flow: Flow) -> str:
    """Fill {name} {ext} {n} {n:3} {date} {parent} {text:20} in a file-name pattern."""
    def sub(m: re.Match) -> str:
        key, arg = m.group(1).lower(), m.group(2)
        if key == "name":
            return f.stem
        if key == "ext":
            return f.suffix.lstrip(".").lower()
        if key == "n":
            return str(n).zfill(int(arg)) if arg and arg.isdigit() else str(n)
        if key == "date":
            return datetime.fromtimestamp(_mtime(f) or time.time()).strftime("%Y-%m-%d")
        if key == "parent":
            return f.parent.name
        if key == "text":
            txt = re.sub(r"\s+", " ", flow.read_text(f)).strip()
            return txt[:int(arg) if arg and arg.isdigit() else 30]
        for col, val in flow.rows.get(str(f), {}).items():        # values from Grab / AI bricks: {category} {value} ...
            if re.sub(r"\W+", "_", str(col)).strip("_").lower() == key and str(val).strip():
                return str(val)
        return m.group(0)
    return re.sub(r"\{(\w+)(?::(\w+))?\}", sub, pattern)


def _clean_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s).strip(" .")


@brick("rename", "Rename", "organise", "🏷️", "Renames the files from a pattern.  " + _TOKENS_HELP,
       [Param("pattern", "New name (without .ext)", "str", "{name}", required=True, help=_TOKENS_HELP)],
       side_effect=True, summary=lambda p: p.str("pattern"))
def b_rename(flow: Flow, p: Params):
    return _rename_all(flow, lambda n, f: _clean_name(_expand(p.str("pattern"), f, n, flow)) or f.stem)


def _rename_all(flow: Flow, stem_of) -> str:
    """Rename every file to `stem_of(n, file)` (+ its extension); previews only say what would happen."""
    out, changed = [], 0
    for n, f in enumerate(flow.files, 1):
        flow.check_cancel()
        stem = stem_of(n, f)
        new_name = stem + f.suffix
        if new_name == f.name:
            out.append(f)
            continue
        if flow.preview:
            target = resolve_target(f.parent, stem, f.suffix, "number") or f
            flow.log(f"would rename {f.name} → {target.name}")
            flow.virtual.add(target)
        else:
            target = rename_in_place(f, new_name, flow.env.journal, flow.env.tool_id)
            flow.move_state(f, target)
        changed += 1
        out.append(target)
    flow.files = out
    return f"{_plural(changed)} renamed" + (" (preview)" if flow.preview else "")


_BY = [("", "Don't make sub-folders"), ("type", "By type (pdf, jpg …)"), ("month", "By month (2026-11)"),
       ("year", "By year"), ("letter", "By first letter"), ("name", "By name (one folder per file)")]


def _subfolder(how: str, f: Path) -> str:
    if how == "type":
        return f.suffix.lstrip(".").lower() or "other"
    if how in ("month", "year"):
        return datetime.fromtimestamp(_mtime(f) or time.time()).strftime("%Y-%m" if how == "month" else "%Y")
    if how == "letter":
        return (f.stem[:1] or "_").upper()
    if how == "name":
        return _clean_name(f.stem) or "file"
    return ""


@brick("to_folder", "Put in a folder", "organise", "🗃️", "Copies or moves the files into a folder (moves can be undone in History).",
       [Param("dest", "Folder", "folder", "", required=True),
        Param("action", "Action", "choice", "copy", [("copy", "Copy"), ("move", "Move")]),
        Param("by", "Sort into sub-folders", "choice", "", _BY),
        Param("conflict", "If the name exists", "choice", "number", [("number", "Add (2), (3) …"), ("skip", "Skip it"), ("overwrite", "Replace it")])],
       side_effect=True, summary=lambda p: f"{'move' if p.str('action') == 'move' else 'copy'} → {p.str('dest') or '(choose a folder)'}")
def b_to_folder(flow: Flow, p: Params):
    dest, out, skipped = Path(p.str("dest").strip('"')), [], 0
    action = "move" if p.str("action") == "move" else "copy"
    for f in flow.files:
        flow.check_cancel()
        folder = dest / _subfolder(p.str("by"), f) if p.str("by") else dest
        if flow.preview:
            target = resolve_target(folder, f.stem, f.suffix, p.str("conflict") or "number")
            flow.log(f"would {action} {f.name} → {folder}")
            if target is None:
                skipped += 1
            else:
                flow.virtual.add(target)
                out.append(target)
            continue
        target = transfer(f, folder, None, action, p.str("conflict") or "number", flow.env.journal, flow.env.tool_id,
                          note=f"workflow: {flow.wf.name}")
        if target is None:
            skipped += 1
            continue
        flow.move_state(f, target)
        out.append(target)
    flow.files = out
    verb = "moved" if action == "move" else "copied"
    return f"{_plural(len(out))} {verb}" + (f", {skipped} skipped" if skipped else "") + (" (preview)" if flow.preview else "")


@brick("zip", "Zip them", "organise", "🤐", "Packs all files into one .zip file.",
       [Param("name", "Zip name", "str", "Files")], summary=lambda p: p.str("name") + ".zip")
def b_zip(flow: Flow, p: Params):
    target = flow.new_file(p.str("name") or "Files", ".zip")
    used: set[str] = set()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in flow.files:
            flow.check_cancel()
            arc, n = f.name, 2
            while arc in used:
                arc, n = f"{f.stem} ({n}){f.suffix}", n + 1
            used.add(arc)
            z.write(f, arc)
    flow.written.append(target)
    flow.files = [target]
    return f"{_plural(len(used))} → {human_size(_size(target))}"


# ---------------------------------------------------------------- read & record
@brick("read_text", "Read the text", "read", "👓", "Reads every file (OCR for scans) so later bricks can use the text.",
       [Param("save", "Also save a .txt for each", "bool", False)])
def b_read_text(flow: Flow, p: Params):
    n, empty = len(flow.files), 0
    for i, f in enumerate(flow.files):
        flow.check_cancel()
        flow.env.progress(i, n, f.name)
        text = flow.read_text(f)
        empty += not text.strip()
        if p.bool("save") and text.strip():
            t = flow.new_file(f.stem, ".txt")
            t.write_text(text, encoding="utf-8")
            flow.written.append(t)
    return f"read {_plural(n)}" + (f", {empty} had no text" if empty else "")


@brick("grab", "Grab a value", "read", "🎯", "Finds a value in each file's text (e.g. a passport number) and remembers it for the list.",
       [Param("column", "Name of the value", "str", "Value", required=True),
        Param("pattern", "Look for", "str", "", required=True,
              help="A regular expression. If it has a (group), the group is taken; e.g.  Passport\\s*No\\.?\\s*:?\\s*([A-Z]\\d{7})")],
       summary=lambda p: f"{p.str('column')} ← {p.str('pattern')}")
def b_grab(flow: Flow, p: Params):
    try:
        rx = re.compile(p.str("pattern"), re.I)
    except re.error as exc:
        raise ValueError(f"Bad pattern: {exc}") from exc
    found = 0
    for f in flow.files:
        flow.check_cancel()
        m = rx.search(flow.read_text(f))
        val = (m.group(1) if m and m.groups() else m.group(0)) if m else ""
        flow.row(f)[p.str("column")] = val.strip()
        found += bool(m)
    return f"found in {found} of {len(flow.files)}"


# ---------------------------------------------------------------- AI bricks (local Ollama, see core/aimode.py)
def _ai_modes():
    from .aimode import MODE_CHOICES
    return MODE_CHOICES


def _ai_setup(flow: Flow, mode: str) -> tuple[str, Any]:
    """(effective mode, LlmConfig).  AI off: 'ai' stops with a clear message, the combined modes use the Rules only."""
    from . import aimode
    cfg = flow.llm_cfg()
    mode = aimode.normalise(mode)
    if mode != aimode.RULES and not cfg.enabled:
        if mode == aimode.AI:
            raise ValueError("AI is switched off - turn it on in Settings > AI (Ollama), or choose a mode with Rules.")
        flow.log("AI is switched off: the Rules are used instead.", "warn")
        return aimode.RULES, cfg
    return mode, cfg


_MODE_PARAM = Param("mode", "Method", "choice", "ai", [], help="Rules only • AI only • Rules first, AI if they find "
                    "nothing • AI first, Rules if it finds nothing.  Needs Settings > AI (Ollama) switched on.")


def _mode_param(default: str = "ai") -> Param:
    return Param("mode", _MODE_PARAM.label, "choice", default, _ai_modes(), help=_MODE_PARAM.help)


@brick("ai_read", "AI read the text", "read", "✨", "Reads every file with the local vision model (good for photos, "
       "handwriting and messy scans) so later bricks can use the text. Choose whether AI or Tesseract reads first.",
       [_mode_param("ai_rules")], summary=lambda p: p.str("mode").replace("_", " → ") or "ai")
def b_ai_read(flow: Flow, p: Params):
    from dataclasses import replace
    from .readers import read_document
    mode, cfg = _ai_setup(flow, p.str("mode") or "ai_rules")
    ocr_cfg = replace(flow.ocr_cfg(), ai_mode=mode, llm=cfg)
    n, empty = len(flow.files), 0
    for i, f in enumerate(flow.files):
        flow.check_cancel()
        flow.env.progress(i, n, f.name)
        if f in flow.virtual:
            continue
        info = read_document(f, ocr_cfg)
        if info.error:
            flow.log(f"{f.name}: {info.error}", "warn")
        flow.texts[str(f)] = info.text or ""
        empty += not (info.text or "").strip()
    return f"read {_plural(n)}" + (f", {empty} had no text" if empty else "")


@brick("ai_grab", "AI grab a value", "read", "✨", "Asks the AI a question about each file's text (e.g. 'passport number', "
       "'employer') and remembers the answer for the list. The Rules side is an optional regular expression.",
       [Param("column", "Name of the value", "str", "Value", required=True),
        Param("question", "What to find", "str", "", required=True, help="Plain words, e.g.  the candidate's current employer"),
        Param("pattern", "Rules: regular expression (optional)", "str", "",
              help="Used by the Rules methods. If it has a (group), the group is taken."),
        _mode_param("ai")],
       summary=lambda p: f"{p.str('column')} ← {p.str('question')}")
def b_ai_grab(flow: Flow, p: Params):
    from . import aimode, aitasks
    mode, cfg = _ai_setup(flow, p.str("mode") or "ai")
    rx = None
    if p.str("pattern"):
        try:
            rx = re.compile(p.str("pattern"), re.I)
        except re.error as exc:
            raise ValueError(f"Bad pattern: {exc}") from exc
    found, by_ai = 0, 0
    for i, f in enumerate(flow.files):
        flow.check_cancel()
        flow.env.progress(i, len(flow.files), f.name)
        text = flow.read_text(f)

        def rules(text=text) -> str:
            m = rx.search(text) if rx else None
            return ((m.group(1) if m.groups() else m.group(0)) if m else "").strip()

        out = aimode.run(mode, rules, lambda text=text, f=f: aitasks.answer_question(cfg, text, p.str("question"), f.name),
                         empty="")
        if out.error:
            flow.log(f"{f.name}: {out.error}", "warn")
        flow.row(f)[p.str("column")] = out.value or ""
        found += bool((out.value or "").strip())
        by_ai += out.from_ai
    return f"found in {found} of {len(flow.files)}" + (f" ({by_ai} by AI)" if by_ai else "")


@brick("ai_classify", "AI sort into categories", "read", "✨", "Decides which category each file belongs to and remembers it "
       "for the list (and for the {category} rename token). The Rules side is 'Category = word, word' lines.",
       [Param("column", "Name of the value", "str", "Category", required=True),
        Param("categories", "Categories (comma separated)", "str", "", required=True,
              help="e.g.  Invoice, Contract, CV, Passport"),
        Param("rules", "Rules: keywords per category (optional)", "text", "",
              help="One line per category:  Invoice = invoice, bill, amount due"),
        _mode_param("ai")],
       summary=lambda p: f"{p.str('column')} ∈ {p.str('categories')}")
def b_ai_classify(flow: Flow, p: Params):
    from . import aimode, aitasks
    mode, cfg = _ai_setup(flow, p.str("mode") or "ai")
    labels = p.words("categories")
    rule_map: dict[str, list[str]] = {}
    for line in str(p.get("rules") or "").splitlines():
        if "=" in line:
            cat, words = line.split("=", 1)
            rule_map[cat.strip()] = [w.strip().lower() for w in words.split(",") if w.strip()]
    done = 0
    for i, f in enumerate(flow.files):
        flow.check_cancel()
        flow.env.progress(i, len(flow.files), f.name)
        text = flow.read_text(f)

        def rules(text=text) -> str:
            low = text.lower()
            for cat, words in rule_map.items():
                if any(w in low for w in words):
                    return cat
            return ""

        out = aimode.run(mode, rules, lambda text=text, f=f: aitasks.classify_into(cfg, text, labels, f.name), empty="")
        if out.error:
            flow.log(f"{f.name}: {out.error}", "warn")
        flow.row(f)[p.str("column")] = out.value or ""
        done += bool(out.value)
    return f"{done} of {len(flow.files)} sorted"


@brick("ai_rename", "AI rename", "organise", "✨", "Gives each file a new name suggested by the AI from its content. "
       "The Rules side is a normal name pattern.  " + _TOKENS_HELP,
       [Param("instruction", "How should the name look?", "str", "document type - person's name", required=True,
              help="Plain words, e.g.  invoice number and company"),
        Param("pattern", "Rules: name pattern", "str", "{name}", help=_TOKENS_HELP),
        _mode_param("ai_rules")],
       side_effect=True, summary=lambda p: p.str("instruction"))
def b_ai_rename(flow: Flow, p: Params):
    from . import aimode, aitasks
    mode, cfg = _ai_setup(flow, p.str("mode") or "ai_rules")

    def stem_of(n: int, f: Path) -> str:
        def rules() -> str:
            return _clean_name(_expand(p.str("pattern") or "{name}", f, n, flow))

        def ask() -> str:
            return _clean_name(aitasks.suggest_filename(cfg, flow.read_text(f), p.str("instruction"), f.name))

        flow.env.progress(n - 1, len(flow.files), f.name)
        out = aimode.run(mode, rules, ask, empty="")
        if out.error:
            flow.log(f"{f.name}: {out.error}", "warn")
        return out.value or f.stem

    return _rename_all(flow, stem_of)


@brick("to_csv", "Add to a list (CSV)", "read", "📋", "Adds one row per file to a CSV list that Excel opens. Values from 'Grab' bricks become columns.",
       [Param("file", "CSV file", "append", "", required=True, help="Pick a new name to create it, or an existing .csv to add rows to it."),
        Param("details", "Include", "choice", "basic", [("basic", "Name, folder, size, date"), ("name", "Only the name"), ("text", "Basic + first 200 characters of text")])],
       side_effect=True, summary=lambda p: p.str("file") or "(choose a file)")
def b_to_csv(flow: Flow, p: Params):
    from .appendfile import append_records
    rows = []
    for f in flow.files:
        rec = {"name": f.name}
        if p.str("details") != "name":
            rec |= {"folder": str(f.parent), "size": human_size(_size(f)),
                    "modified": datetime.fromtimestamp(_mtime(f)).strftime("%Y-%m-%d %H:%M") if f.exists() else ""}
        if p.str("details") == "text":
            rec["text"] = re.sub(r"\s+", " ", flow.read_text(f)).strip()[:200]
        rec |= flow.rows.get(str(f), {})
        rows.append(rec)
    if flow.preview:
        flow.log(f"would add {_plural(len(rows), 'row')} to {Path(p.str('file')).name}")
        return f"{_plural(len(rows), 'row')} would be added (preview)"
    n = append_records(p.str("file"), rows)
    return f"{_plural(n, 'row')} added to {Path(p.str('file')).name}"


# ---------------------------------------------------------------- finish
@brick("notify", "Show a message", "finish", "🔔", "Pops up a message when the workflow reaches this brick.",
       [Param("text", "Message", "str", "Done: {count} file(s)", help="{count} = number of files")],
       summary=lambda p: p.str("text"))
def b_notify(flow: Flow, p: Params):
    msg = p.str("text").replace("{count}", str(len(flow.files)))
    if not flow.preview:
        flow.env.notify(msg)
    return msg


@brick("open_result", "Open the result", "finish", "📂", "Opens the folder with the new files (or the first file's folder).")
def b_open_result(flow: Flow, p: Params):
    target = flow.written[-1].parent if flow.written else (flow.files[0].parent if flow.files else None)
    if target is None:
        return "nothing to open"
    if not flow.preview:
        flow.env.open_path(target)
    return f"opens {target.name}"


# bricks that only look at names / do bookkeeping, so they still make sense after a preview rename or move
for _id in ("keep_types", "name_has", "sort", "stop_unless", "rename", "to_folder", "to_csv", "notify", "open_result"):
    BRICKS[_id].virtual_ok = True


# --------------------------------------------------------------------------- starter workflows
def _t(name: str, description: str, *steps: tuple) -> Workflow:
    return Workflow(name, [Step(b, dict(pr)) for b, pr in steps], description)


TEMPLATES: list[Workflow] = [
    _t("Photos → one PDF", "Drop photos or scans; get a single PDF in the right order.",
       ("given", {}), ("keep_types", {"types": "jpg, jpeg, png, tif, tiff, pdf"}), ("sort", {"by": "name"}),
       ("merge_pdf", {"name": "Combined", "numbers": "Page {n} of {total}"}), ("open_result", {})),
    _t("Tidy a messy folder", "Pick a folder; remove exact duplicates and sort what is left into folders by type.",
       ("folder", {"folder": "", "recursive": False}), ("no_dupes", {}), ("to_folder", {"dest": "", "action": "copy", "by": "type"})),
    _t("Find files that mention…", "Reads the files (OCR for scans), keeps those with your words and lists them in a CSV.",
       ("given", {}), ("text_has", {"words": "", "match": "any"}), ("to_csv", {"file": "", "details": "basic"})),
    _t("Shrink big PDFs", "Keeps PDFs over 2 MB, compresses them to about 1 MB and puts the copies in a folder.",
       ("given", {}), ("keep_types", {"types": "pdf"}), ("size", {"min_mb": 2}), ("compress_pdf", {"target_mb": 1}),
       ("to_folder", {"dest": "", "action": "copy"})),
    _t("Zip files in order", "Sorts the files by name and packs them into one zip.",
       ("given", {}), ("sort", {"by": "name"}), ("zip", {"name": "Files"})),
]
