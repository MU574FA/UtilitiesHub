"""Tests for consolidated Folders & paths settings panel and SettingsWindow deduplication."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import pytest

from utilshub import app as appmod
from utilshub.core import autocomplete as autocomplete_core
from utilshub.core.schema import Opt
from utilshub.core.settings import Settings
from utilshub.options import all_shared_opts, shared_pages
from utilshub.ui.folders_panel import build_folders_panel, feature_path_groups, feature_path_keys
from utilshub.ui.settings_ui import SettingsWindow
from utilshub.ui.theme import ThemeManager


class _MiniApp:
    sorted_tools = appmod.App.sorted_tools

    def __init__(self, tmp_path):
        self.tool_classes = appmod.discover_tool_classes()
        self.settings = Settings(tmp_path / "settings.json", autosave=False)
        self.settings.register(all_shared_opts())
        self.settings.register(appmod.STATE_OPTS)
        for cls in self.tool_classes:
            self.settings.register(cls.options())
        self.autocomplete = autocomplete_core.create(self.settings, tmp_path / "learned.json")
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.app = self
        self.theme = ThemeManager(self.root, self.settings)
        self.theme.apply()
        self.tools = {}
        for cls in self.tool_classes:
            self.tools[cls.id] = cls(self)
        self.aliases = {m.id: t.id for t in self.tools.values() for m in t.members() if m is not t}

    def settings_pages(self):
        pages = list(shared_pages())
        for cls in self.tool_classes:
            pages.extend(cls.pages())
        return sorted(pages, key=lambda p: p.order)

    def destroy(self):
        self.root.destroy()


@pytest.fixture
def mini_app(tmp_path):
    app = _MiniApp(tmp_path)
    yield app
    SettingsWindow._instance = None
    app.destroy()


def test_feature_path_keys_consolidates_all_non_advanced_paths(mini_app):
    keys = set(feature_path_keys(mini_app))
    expected_present = {
        "ocr.tesseract_cmd",
        "ocr.libreoffice_cmd",
        "match.names_list_path",
        "visa.input_folder",
        "visa.output_folder",
        "visa.info_sheet",
        "visa.data_folder",
        "cv_sorter.watch_folder",
        "cv_sorter.dest_folder",
        "cv_sorter.skip_folder",
        "cv_sorter.append_file",
        "docpacker.input_folder",
        "docpacker.output_folder",
        "docpacker.info_sheet",
        "docpacker.append_file",
        "file_picker.source",
        "file_picker.dest",
        "file_picker.append_file",
        "duplicates.input_folder",
        "duplicates.dest",
        "duplicates.protect",
        "duplicates.append_file",
        "scan_cleaner.input_folder",
        "scan_cleaner.folder",
        "pdf_desk.input_folder",
        "pdf_desk.output_folder",
        "pdf_desk.append_file",
        "screenshot.input_folder",
        "screenshot.output_folder",
        "screenshot.autosave_path",
        "screenshot.append_file",
        "ocr_match.input_folder",
        "ocr_match.output_folder",
        "ocr_match.append_file",
        "passport.input_folder",
        "passport.output_folder",
        "passport.append_file",
        "similarity_search.input_folder",
        "similarity_search.output_folder",
        "similarity_search.append_file",
        "badge_extractor.input_folder",
        "badge_extractor.output_folder",
        "badge_extractor.cv_folder",
        "badge_extractor.info_sheet",
        "badge_extractor.append_file",
        "name_matcher.input_folder",
        "name_matcher.output_folder",
        "name_matcher.append_file",
        "sheet_sync.input_folder",
        "sheet_sync.output_folder",
        "sheet_sync.append_file",
        "workflows.input_folder",
        "workflows.output_root",
        "history.output_folder",
    }
    assert expected_present.issubset(keys)
    # Advanced internal "last picked file" state must remain excluded
    for adv_key in ("name_matcher.a_path", "name_matcher.b_path", "sheet_sync.old_path", "sheet_sync.new_path"):
        assert adv_key not in keys


def test_settings_window_moves_paths_to_folders_page(mini_app):
    win = SettingsWindow.open(mini_app, "folders")
    mini_app.root.update_idletasks()
    managed = set(feature_path_keys(mini_app))

    for page in mini_app.settings_pages():
        if page.id == "folders":
            assert set(o.key for o in win._search_opts(page)) == managed
        else:
            shown_keys = {o.key for o in win._page_opts(page)}
            assert shown_keys.isdisjoint(managed), f"{page.id} still shows managed path opts: {shown_keys & managed}"

    # Verify individual tool/shared page shows quick-jump banner instead of duplicate path inputs
    win.show("ocr")
    mini_app.root.update_idletasks()
    assert len(win._moved_path_opts(next(p for p in mini_app.settings_pages() if p.id == "ocr"))) == 2

    # Searching for 'tesseract' or 'cv' finds the Folders & paths page
    win.search.set("tesseract")
    mini_app.root.update_idletasks()
    assert "folders" in [p.id for p in win._nav_pages]
    assert win.current == "folders"

    win.destroy()


def test_build_folders_panel_two_way_sync_filter_and_destroy(mini_app, tmp_path):
    subs_before = len(mini_app.settings._subs)
    host = ttk.Frame(mini_app.root)
    host.pack()
    panel = build_folders_panel(host, mini_app)
    mini_app.root.update_idletasks()

    assert len(mini_app.settings._subs) == subs_before + 1

    # Filter to Badge Extractor and verify external + widget sync
    entries = []

    def walk_entries(w):
        for c in w.winfo_children():
            if isinstance(c, ttk.Entry):
                entries.append(c)
            walk_entries(c)

    walk_entries(panel)
    filter_entry = entries[0]
    filter_entry.delete(0, "end")
    filter_entry.insert(0, "badge_extractor.cv_folder")
    mini_app.root.update_idletasks()

    cards_host = [c for c in panel.winfo_children() if isinstance(c, ttk.Frame)][1]
    cards = [c for c in cards_host.winfo_children() if isinstance(c, ttk.LabelFrame)]
    assert len(cards) == 1
    assert "Badge Extractor" in str(cards[0].cget("text"))

    # External setting update syncs into FolderEntry
    cv_dir = str(tmp_path / "sorted_cvs")
    mini_app.settings.set("badge_extractor.cv_folder", cv_dir)
    mini_app.root.update_idletasks()

    inner_entries = []
    def walk_card(w):
        for c in w.winfo_children():
            if isinstance(c, ttk.Entry):
                inner_entries.append(c)
            walk_card(c)
    walk_card(cards[0])
    assert len(inner_entries) == 1
    assert inner_entries[0].get() == cv_dir

    # Widget write syncs back to settings
    new_dir = str(tmp_path / "other_cvs")
    inner_entries[0].delete(0, "end")
    inner_entries[0].insert(0, new_dir)
    mini_app.root.update_idletasks()
    assert mini_app.settings.get("badge_extractor.cv_folder") == new_dir

    # Destroying container unsubscribes cleanly
    host.destroy()
    mini_app.root.update_idletasks()
    assert len(mini_app.settings._subs) == subs_before
