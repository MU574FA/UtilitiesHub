"""Operating-system helpers: open files, reveal in folder, dark-mode detection."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def open_path(path: str | Path) -> None:
    """Open a file or folder with the OS default application."""
    path = str(path)
    if IS_WINDOWS:
        os.startfile(path)  # noqa: S606
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def reveal(path: str | Path) -> None:
    """Show a file selected in the file manager (or just open its folder)."""
    p = Path(path)
    if IS_WINDOWS:
        subprocess.Popen(["explorer", "/select,", str(p)])
    elif IS_MAC:
        subprocess.Popen(["open", "-R", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else p)])


def system_prefers_dark() -> bool:
    try:
        if IS_WINDOWS:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        if IS_MAC:
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True,
                                 text=True, timeout=2)
            return "dark" in out.stdout.lower()
        theme = os.environ.get("GTK_THEME", "").lower()
        if "dark" in theme:
            return True
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, text=True, timeout=2)
        return "dark" in out.stdout.lower()
    except Exception:
        return False


def safe_stem(name: str) -> str:
    import re
    return re.sub(r"[^\w\-]+", "_", name).strip("_")
