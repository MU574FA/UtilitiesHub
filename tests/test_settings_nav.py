"""Tests for the organized, grouped Settings sidebar."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utilshub.app import discover_tool_classes  # noqa: E402
from utilshub.options import shared_pages  # noqa: E402
from utilshub.ui.settings_ui import group_pages  # noqa: E402


def _all_sorted_pages():
    pages = list(shared_pages())
    for cls in discover_tool_classes():
        pages.extend(cls.pages())
    return sorted(pages, key=lambda p: p.order)


def test_settings_pages_have_unique_orders_and_expected_groups():
    pages = _all_sorted_pages()
    assert len(pages) == 25
    orders = [p.order for p in pages]
    assert len(orders) == len(set(orders)), f"Duplicate page orders: {orders}"
    assert all(p.group for p in pages), [p.id for p in pages if not p.group]

    grouped = group_pages(pages)
    assert [g for g, _ in grouped] == [
        "App",
        "Engines & AI",
        "Documents & files",
        "Clean-up",
        "Extract",
        "Match & Sync",
        "Automation",
    ]
    by_group = {g: [p.id for p in items] for g, items in grouped}
    assert by_group["App"] == ["general", "ui", "home", "folders"]
    assert by_group["Engines & AI"] == ["ai", "ocr", "match", "preview", "autocomplete"]
    assert by_group["Documents & files"] == ["visa", "cv_sorter", "docpacker", "file_picker", "pdf_desk"]
    assert by_group["Clean-up"] == ["duplicates", "scan_cleaner"]
    assert by_group["Extract"] == ["screenshot", "ocr_match", "passport", "similarity_search", "badge_extractor"]
    assert by_group["Match & Sync"] == ["name_matcher", "sheet_sync"]
    assert by_group["Automation"] == ["auto", "workflows"]


def test_headless_grouped_settings_sidebar():
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
from utilshub.options import all_shared_opts
from utilshub.ui.settings_ui import SettingsWindow
import fakeapp

tmp = Path(tempfile.mkdtemp())

class FullFakeApp:
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
        self.tools = {c.id: c(self) for c in self.tool_classes}
        self.current = None

    def on_log(self, fn): pass
    def log(self, msg, level="info", tool=""): pass
    def notify(self, msg, level="info"): pass

app = FullFakeApp()
sw = SettingsWindow(app, "general")

assert list(sw._group_buttons) == [
    "App", "Engines & AI", "Documents & files", "Clean-up", "Extract", "Match & Sync", "Automation"
]
assert len(sw._nav_buttons) == 25
assert sw._nav_buttons["general"]._kw["style"] == "SideItemSel.TButton"
assert sw._nav_buttons["ui"]._kw["style"] == "SideItem.TButton"
assert sw.group_lbl._kw["text"] == "APP"

# Collapse Extract group
sw._toggle_group("Extract")
assert "Extract" in sw._collapsed
assert "(5)" in sw._group_buttons["Extract"]._kw["text"]
for pid in ("screenshot", "ocr_match", "passport", "similarity_search", "badge_extractor"):
    assert pid not in sw._nav_buttons

# Navigating to a page inside a collapsed group auto-expands it
sw.show("passport")
assert "Extract" not in sw._collapsed
assert sw._nav_buttons["passport"]._kw["style"] == "SideItemSel.TButton"
assert sw.group_lbl._kw["text"] == "EXTRACT"

# Search by group name filters sidebar and matches hub members
sw.search.set("clean-up")
assert [p.id for p in sw._nav_pages] == ["duplicates", "scan_cleaner"]
assert sw.current == "duplicates"

# Clearing search restores all pages and preserves highlight
sw.search.set("")
assert len(sw._nav_pages) == 25
assert sw._nav_buttons[sw.current]._kw["style"] == "SideItemSel.TButton"

print("SETTINGS_NAV_OK")
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
    assert "SETTINGS_NAV_OK" in proc.stdout
