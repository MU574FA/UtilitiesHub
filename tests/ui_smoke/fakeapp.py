"""FakeApp: just enough of utilshub.app.App for tools, with synchronous jobs."""
import sys, tempfile, traceback
from collections import deque
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import faketk
faketk.install()
from utilshub import app as appmod
from utilshub.core.fileops import Journal
from utilshub.core.settings import Settings
from utilshub.options import all_shared_opts


class FakeJob:
    cancelled = False
    def progress(self, d, t, m=""): pass


class SyncJobs:
    errors = []
    def submit(self, fn, title="", tool="", on_done=None, on_error=None, on_progress=None, serial_key=""):
        job = FakeJob()
        try:
            res = fn(job)
        except Exception as exc:
            traceback.print_exc()
            SyncJobs.errors.append(str(exc))
            if on_error: on_error(str(exc))
            return job
        if on_done: on_done(res)
        return job


class Bridge:
    def call(self, fn, *a, **k): fn(*a, **k)


class Theme:
    p = {k: "#888888" for k in ("bg", "fg", "muted", "surface", "line", "canvas", "ok", "err", "warn", "info", "input", "hover", "accent_soft", "accent_fg", "sel_fg")}
    accent = "#0a84ff"
    dark = False
    def style_canvas(self, c): pass
    def style_listbox(self, c): pass
    def style_text(self, c): pass
    def on_change(self, fn): pass
    def apply(self): pass


def tool_classes():
    """The tools under test plus plain dummies for the rest: deterministic on any machine (no PyMuPDF/Tesseract needed)."""
    from utilshub.tools.base import Tool
    from utilshub.tools.duplicates import DuplicateFinder
    from utilshub.tools.home import HomeTool
    from utilshub.tools.scan_cleaner import ScanCleaner
    dummies = []
    for tid, title, order in (("file_picker", "File Picker", 30), ("name_matcher", "Name Matcher", 40), ("ocr_match", "OCR Match", 45),
                              ("sheet_sync", "Sheet Sync", 45), ("passport", "Passport", 46), ("workflows", "Workflows", 47),
                              ("autopilot", "Autopilot", 90), ("history", "History", 95)):
        dummies.append(type(title.replace(" ", ""), (Tool,), {"id": tid, "title": title, "order": order, "description": f"{title} (dummy)",
                                                             "build": lambda self, parent: None}))
    return sorted([HomeTool, DuplicateFinder, ScanCleaner, *dummies], key=lambda c: c.order)


class FakeApp:
    sorted_tools = appmod.App.sorted_tools
    visible_tools = appmod.App.visible_tools
    hidden_tools = appmod.App.hidden_tools
    tool_order = appmod.App.tool_order
    set_tool_order = appmod.App.set_tool_order
    set_tool_hidden = appmod.App.set_tool_hidden
    move_tool = appmod.App.move_tool
    show_all_tools = appmod.App.show_all_tools

    def __init__(self, tmp: Path, tool_ids=None):
        self.settings = Settings(tmp / "settings.json")
        self.tool_classes = tool_classes()
        self.settings.register(all_shared_opts()); self.settings.register(appmod.STATE_OPTS)
        for c in self.tool_classes: self.settings.register(c.options())
        self.root = faketk.Tk(); self.root.app = self
        self.theme = Theme(); self.jobs = SyncJobs(); self.bridge = Bridge()
        self.journal = Journal(tmp / "journal.jsonl")
        self.logs = deque(maxlen=500); self._log_listeners = []
        self.notes = []; self.opened = []; self.shown = []; self.sent = []
        self.tools = {}
        for c in self.tool_classes:
            self.tools[c.id] = c(self)

    def on_log(self, fn): self._log_listeners.append(fn)
    def log(self, message, level="info", tool=""):
        self.logs.append(("00:00", level, tool, message))
        for fn in self._log_listeners: fn("00:00", level, tool, message)
    def notify(self, msg, level="info"): self.notes.append((level, msg))
    def open_settings(self, page=None): self.opened.append(page)
    def show_tool(self, tid): self.shown.append(tid)
    def send_to(self, tid, paths): self.sent.append((tid, paths))


class FakeViewer(faketk.Widget):
    """Stands in for ui.viewer.PageViewer (needs a renderer + threads); records what it was asked to show."""
    def __init__(self, parent, app, **kw):
        super().__init__(parent)
        self.shown = []
    def show(self, path, *a, **k): self.shown.append(path)
    def clear(self, *a, **k): self.shown.append(None)

import utilshub.tools.duplicates as _dup
_dup.PageViewer = FakeViewer
