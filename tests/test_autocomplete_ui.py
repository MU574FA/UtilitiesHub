"""Autocomplete pop-up, keys, right-click menu and settings panel (fake tkinter, separate process)."""
import subprocess
import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utilshub.core.autocomplete import AUTOCOMPLETE_OPTS, AUTOCOMPLETE_STATE, create as create_autocomplete  # noqa: E402
from utilshub.core.settings import Settings  # noqa: E402
from utilshub.ui.autocomplete_ui import AutocompletePanel  # noqa: E402
from utilshub.ui.theme import ThemeManager  # noqa: E402


def test_autocomplete_ui_in_subprocess():
    run = Path(__file__).parent / "ui_smoke" / "autocomplete_ui.py"
    proc = subprocess.run([sys.executable, "-X", "utf8", str(run)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=600, cwd=str(Path(__file__).parents[1]))
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]


def test_autocomplete_panel_destroy_flushes_keywords_without_tclerror(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    settings.register(AUTOCOMPLETE_OPTS)
    settings.register(AUTOCOMPLETE_STATE)

    root = tk.Tk()
    root.withdraw()
    errors: list[BaseException] = []
    root.report_callback_exception = lambda exc, val, tb: errors.append(val)

    class _App:
        pass

    app = _App()
    app.root = root
    app.settings = settings
    app.theme = ThemeManager(root, settings)
    app.theme.apply()
    app.autocomplete = create_autocomplete(settings, tmp_path / "learned.json")
    root.app = app

    win = tk.Toplevel(root)
    panel = AutocompletePanel(win, app)
    panel.pack(fill="both", expand=True)
    win.update()

    fid = panel.current
    assert fid
    panel.kw.insert("end", "\nPendingDraftKeyword")
    panel._kw_later()

    win.destroy()
    root.update()
    root.destroy()

    assert not errors, errors
    assert "PendingDraftKeyword" in app.autocomplete.config(fid).keywords
