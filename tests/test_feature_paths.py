"""Tests for per-feature input & output path settings, FolderEntry, and Folders & paths UI."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utilshub.app import App, STATE_OPTS, discover_tool_classes  # noqa: E402
from utilshub.core.settings import Settings  # noqa: E402
from utilshub.options import all_shared_opts, shared_pages  # noqa: E402
from utilshub.tools.base import Tool, _existing_dir  # noqa: E402
from utilshub.tools.hub import HubTool  # noqa: E402
from utilshub.ui.widgets import _dir_base  # noqa: E402


def _all_leaf_tool_classes() -> list[type[Tool]]:
    out: list[type[Tool]] = []
    for cls in discover_tool_classes():
        if issubclass(cls, HubTool):
            out.extend(m for m, _label in cls.member_classes())
        else:
            out.append(cls)
    return out


def test_every_feature_declares_input_and_output_folder_options():
    classes = _all_leaf_tool_classes()
    by_id = {c.id: c for c in classes}
    assert len(classes) >= 14

    for cls in classes:
        if cls.id not in ("home", "autopilot"):
            assert cls.output_path_key, f"{cls.__name__} must declare output_path_key"
        if cls.id not in ("home", "autopilot", "history"):
            assert cls.input_path_key, f"{cls.__name__} must declare input_path_key"

        opts_by_key = {o.key: o for o in cls.options()}
        for rel in (cls.input_path_key, cls.output_path_key):
            if not rel:
                continue
            full = f"{cls.id}.{rel}"
            assert full in opts_by_key, f"{cls.__name__} missing Opt for {full}"
            opt = opts_by_key[full]
            assert opt.kind == "folder", f"{full} must be kind='folder', got {opt.kind!r}"
            assert opt.default == "", f"{full} default must be empty string, got {opt.default!r}"

    assert "cv_sorter" in by_id and by_id["cv_sorter"].input_path_key == "watch_folder"
    assert by_id["cv_sorter"].output_path_key == "dest_folder"
    assert by_id["file_picker"].input_path_key == "source"
    assert by_id["file_picker"].output_path_key == "dest"
    assert by_id["duplicates"].output_path_key == "dest"
    assert by_id["scan_cleaner"].output_path_key == "folder"
    assert by_id["workflows"].output_path_key == "output_root"


def test_existing_dir_and_dir_base(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    sample = folder / "a.pdf"
    sample.write_bytes(b"%PDF-1.4")

    assert _existing_dir(None) is None
    assert _existing_dir("") is None
    assert _existing_dir("   ") is None
    assert _existing_dir(str(tmp_path / "missing")) is None
    assert _existing_dir(str(folder)) == str(folder)
    assert _existing_dir(str(sample)) == str(folder)

    tpl = f"{folder}/{{interviewer}}/{{name}}"
    assert _dir_base(tpl) == str(folder)
    assert _existing_dir(tpl) == str(folder)
    assert _dir_base("{name}/{surname}") == ""
    assert _existing_dir("{name}/{surname}") is None


def test_tool_input_and_output_dir_helpers(tmp_path):
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()

    s = Settings(tmp_path / "settings.json", autosave=False)
    s.register(all_shared_opts())
    s.register(STATE_OPTS)
    for cls in discover_tool_classes():
        s.register(cls.options())

    dummy_app = type("DummyApp", (), {"settings": s})()

    class DemoTool(Tool):
        id = "docpacker"
        input_path_key = "input_folder"
        output_path_key = "output_folder"

    tool = DemoTool(dummy_app)
    assert tool.input_dir() is None
    assert tool.output_dir() is None
    s.set("docpacker.input_folder", str(in_dir))
    s.set("docpacker.output_folder", str(out_dir))
    assert tool.input_dir() == str(in_dir)
    assert tool.output_dir() == str(out_dir)


def test_shared_folders_page_registered():
    pages = shared_pages()
    folders = next((p for p in pages if p.id == "folders"), None)
    assert folders is not None
    assert callable(folders.builder)
    assert "input" in folders.description.lower() and "output" in folders.description.lower()


def test_headless_folders_panel_and_tool_sync(tmp_path):
    script = r'''
import sys, tempfile
from pathlib import Path
HERE = Path.cwd()
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests" / "ui_smoke"))

import faketk
faketk.install()
faketk.Widget.winfo_rootx = lambda self: 0
faketk.Widget.winfo_rooty = lambda self: 0
faketk.Widget.winfo_height = lambda self: 600
faketk.Widget.winfo_reqheight = lambda self: 200

from utilshub import app as appmod
from utilshub.core.fileops import Journal
from utilshub.core.settings import Settings
from utilshub.options import all_shared_opts, shared_pages
from utilshub.ui.folders_panel import build_folders_panel, feature_path_entries, feature_path_keys
from utilshub.ui.settings_ui import OptionPage, SettingsWindow
from utilshub.ui.widgets import FolderEntry
import utilshub.ui.widgets as widgets_mod
import fakeapp

tmp = Path(tempfile.mkdtemp())
in_dir = tmp / "in_dir"; in_dir.mkdir()
out_dir = tmp / "out_dir"; out_dir.mkdir()
other_dir = tmp / "other_dir"; other_dir.mkdir()

class FullFakeApp:
    sorted_tools = appmod.App.sorted_tools
    visible_tools = appmod.App.visible_tools
    hidden_tools = appmod.App.hidden_tools
    tool_order = appmod.App.tool_order
    iter_tools = appmod.App.iter_tools
    settings_pages = appmod.App.settings_pages

    def __init__(self):
        self.tool_classes = appmod.discover_tool_classes()
        self.settings = Settings(tmp / "settings.json", autosave=False)
        self.settings.register(all_shared_opts())
        self.settings.register(appmod.STATE_OPTS)
        for c in self.tool_classes:
            self.settings.register(c.options())
        self.root = faketk.Tk()
        self.root.app = self
        self.theme = fakeapp.Theme()
        self.jobs = fakeapp.SyncJobs()
        self.bridge = fakeapp.Bridge()
        self.journal = Journal(tmp / "journal.jsonl")
        self.logs = []
        self._log_listeners = []
        self.notes = []
        self.tools = {c.id: c(self) for c in self.tool_classes}
        self.aliases = {m.id: t.id for t in self.tools.values() for m in t.members() if m is not t}
        self.current = None

    def on_log(self, fn): self._log_listeners.append(fn)
    def log(self, msg, level="info", tool=""): pass
    def notify(self, msg, level="info"): self.notes.append((level, msg))
    def open_settings(self, page=None): pass
    def show_tool(self, tid): pass

app = FullFakeApp()

# 1. Unified Folders & paths panel discovers every feature
entries = feature_path_entries(app)
keys = feature_path_keys(app)
entry_ids = [t.id for t, _, _ in entries]
assert "visa" in entry_ids and "cv_sorter" in entry_ids and "docpacker" in entry_ids
assert "history" in entry_ids and "home" not in entry_ids and "autopilot" not in entry_ids
assert "cv_sorter.watch_folder" in keys and "cv_sorter.dest_folder" in keys
assert "docpacker.input_folder" in keys and "docpacker.output_folder" in keys

host = faketk.Widget(app.root)
panel = build_folders_panel(host, app)
frames = [c for c in panel.winfo_children() if "text" in getattr(c, "_kw", {})]
assert len(frames) == len(entries), (len(frames), len(entries))

# 2. SettingsWindow shows Folders & paths, searches by "input", and resets page keys
sw = SettingsWindow(app, "folders")
assert sw.current == "folders"
sw.search.set("input")
assert any(p.id == "folders" for p in sw._nav_pages)
app.settings.set("docpacker.input_folder", str(in_dir))
assert app.settings.get("docpacker.input_folder") == str(in_dir)
sw.search.set("")
sw.show("folders", force=True)
sw.reset_page()
assert app.settings.get("docpacker.input_folder") == ""

# 3. Tool.sync_var keeps open tool UI in sync without stripping trailing spaces during typing
cv = app.tools["cv_sorter"]
cv.ensure_built(app.root)
app.settings.set("cv_sorter.watch_folder", str(in_dir))
assert cv.vars["watch_folder"].get() == str(in_dir)
typed = str(in_dir) + " "
cv.vars["watch_folder"].set(typed)
assert cv.vars["watch_folder"].get() == typed, "trailing space must not be stripped while typing"

# 4. OptionPage syncs folder vars when changed externally
op = OptionPage(faketk.Widget(app.root), app, [o for o in type(cv).options() if o.key == "cv_sorter.watch_folder"])
app.settings.set("cv_sorter.watch_folder", str(out_dir))
assert op._vars["cv_sorter.watch_folder"].get() == str(out_dir)

# 5. DuplicateFinder.sources() falls back to duplicates.input_folder when sources list is empty
dup = app.tools["cleanup"].member("duplicates")
assert dup.sources() == []
app.settings.set("duplicates.input_folder", str(in_dir))
assert dup.sources() == [str(in_dir)]

# 6. ScanCleaner switches output mode to "folder" when scan_cleaner.folder is set
sc = app.tools["cleanup"].member("scan_cleaner")
assert sc.get("output") == "beside"
app.settings.set("scan_cleaner.folder", str(out_dir))
assert sc.get("output") == "folder"

# 7. FolderEntry.browse uses initialdir fallback, preserves {placeholders}, and _open strips them
calls = []
orig_askdir = widgets_mod.filedialog.askdirectory
orig_open = widgets_mod.open_path
opened = []
widgets_mod.open_path = lambda p: opened.append(p)
def fake_askdir(**kw):
    calls.append(kw)
    return str(other_dir)
widgets_mod.filedialog.askdirectory = fake_askdir

v_tpl = faketk.StringVar(value=f"{in_dir}/{{interviewer}}")
fe = FolderEntry(host, v_tpl, "dir", open_button=True, initialdir=lambda: str(out_dir))
fe.browse()
assert calls[-1]["initialdir"] == str(in_dir)
assert v_tpl.get() == f"{other_dir}/{{interviewer}}", v_tpl.get()
fe._open()
assert opened[-1] == str(other_dir), opened[-1]

v_empty = faketk.StringVar(value="")
fe2 = FolderEntry(host, v_empty, "dir", initialdir=lambda: str(out_dir))
fe2.browse()
assert calls[-1]["initialdir"] == str(out_dir)

# 8. PdfDesk._ask_save creates configured output_folder if missing
pd = app.tools["pdf_desk"]
new_out = tmp / "created_out"
app.settings.set("pdf_desk.output_folder", str(new_out))
saved_path = pd._ask_save("merged.pdf")
assert new_out.is_dir() and saved_path == str(new_out / "merged.pdf")

widgets_mod.filedialog.askdirectory = orig_askdir
widgets_mod.open_path = orig_open
print("ALL_SUBPROCESS_OK")
'''
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert "ALL_SUBPROCESS_OK" in proc.stdout
