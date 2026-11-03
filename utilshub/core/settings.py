"""One JSON settings file for the whole suite, with defaults coming from `Opt` declarations.

    settings.register(opts)                # tools do this once at start-up
    settings.get("cv.watch_folder")        # value (or its declared default)
    settings.set("ui.theme", "dark")       # saved (debounced) + listeners called
    settings.subscribe("ui.", callback)    # callback(key, value) for every key with that prefix
    settings.section("ocr.")               # {"lang": "eng", ...}  (prefix stripped)

Stored flat (`{"cv.watch_folder": "..."}`) so a hand edit is easy and old keys survive.
Set UTILSHUB_HOME to relocate the data folder (or create a file called `portable.flag`
next to run.py to keep everything beside the program).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from .schema import Opt

log = logging.getLogger(__name__)
APP_DIR_NAME = "UtilsHub"
SETTINGS_VERSION = 1
_MISSING = object()


def app_root() -> Path:
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """Folder for settings, history, journal, caches and logs."""
    override = os.environ.get("UTILSHUB_HOME")
    if override:
        path = Path(override)
    elif (app_root() / "portable.flag").exists():
        path = app_root() / "data"
    elif sys.platform.startswith("win"):
        path = Path(os.environ.get("APPDATA", Path.home())) / APP_DIR_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_DIR_NAME.lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


class Settings:
    def __init__(self, path: Path | None = None, autosave: bool = True) -> None:
        self.path = Path(path) if path else data_dir() / "settings.json"
        self.autosave = autosave
        self._opts: dict[str, Opt] = {}
        self._values: dict[str, Any] = {}
        self._subs: list[tuple[str, Callable[[str, Any], None]]] = []
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self.load()

    # ------------------------------------------------------------------ declarations
    def register(self, opts: Iterable[Opt]) -> None:
        with self._lock:
            for opt in opts:
                self._opts[opt.key] = opt

    def opts(self, prefix: str = "") -> list[Opt]:
        return [o for k, o in self._opts.items() if k.startswith(prefix)]

    def opt(self, key: str) -> Opt | None:
        return self._opts.get(key)

    # ------------------------------------------------------------------ access
    def default(self, key: str, fallback: Any = None) -> Any:
        opt = self._opts.get(key)
        return copy.deepcopy(opt.default) if opt else fallback

    def get(self, key: str, fallback: Any = _MISSING) -> Any:
        with self._lock:
            if key in self._values:
                value = self._values[key]
                return copy.deepcopy(value) if isinstance(value, (list, dict)) else value
        opt = self._opts.get(key)
        if opt is not None:
            return copy.deepcopy(opt.default)
        if fallback is _MISSING:
            raise KeyError(f"Unknown setting: {key}")
        return fallback

    def get_int(self, key: str, fallback: int = 0) -> int:
        try:
            return int(float(self.get(key, fallback)))
        except (TypeError, ValueError):
            return fallback

    def get_float(self, key: str, fallback: float = 0.0) -> float:
        try:
            return float(self.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    def get_bool(self, key: str, fallback: bool = False) -> bool:
        value = self.get(key, fallback)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        with self._lock:
            old = self._values.get(key, _MISSING)
            if old is not _MISSING and old == value:
                return
            self._values[key] = copy.deepcopy(value) if isinstance(value, (list, dict)) else value
        self._notify(key, value)
        if save:
            self._schedule_save()

    def update(self, mapping: dict[str, Any]) -> None:
        for key, value in mapping.items():
            self.set(key, value, save=False)
        self._schedule_save()

    def reset(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)
        self._notify(key, self.get(key, None))
        self._schedule_save()

    def reset_prefix(self, prefix: str) -> None:
        for key in [k for k in list(self._values) if k.startswith(prefix)] + \
                   [k for k in self._opts if k.startswith(prefix) and k not in self._values]:
            self.reset(key)

    def is_default(self, key: str) -> bool:
        return key not in self._values or self._values[key] == self.default(key, None)

    def section(self, prefix: str) -> dict[str, Any]:
        keys = {k for k in self._opts if k.startswith(prefix)} | {k for k in self._values if k.startswith(prefix)}
        return {k[len(prefix):]: self.get(k) for k in sorted(keys)}

    def all_values(self) -> dict[str, Any]:
        keys = set(self._opts) | set(self._values)
        return {k: self.get(k) for k in sorted(keys)}

    # ------------------------------------------------------------------ change notification
    def subscribe(self, prefix: str, callback: Callable[[str, Any], None]) -> Callable[[], None]:
        entry = (prefix, callback)
        self._subs.append(entry)
        return lambda: self._subs.remove(entry) if entry in self._subs else None

    def _notify(self, key: str, value: Any) -> None:
        for prefix, callback in list(self._subs):
            if key.startswith(prefix):
                try:
                    callback(key, value)
                except Exception:
                    log.exception("Settings listener failed for %s", key)

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._values = {k: v for k, v in data.items() if not k.startswith("_")}
        except (OSError, ValueError) as exc:
            log.error("Could not read %s (%s) - using defaults. A copy was kept as .bad", self.path, exc)
            try:
                self.path.replace(self.path.with_suffix(".bad"))
            except OSError:
                pass

    def _schedule_save(self) -> None:
        if not self.autosave:
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(0.6, self.save)
            self._timer.daemon = True
            self._timer.start()

    def save(self) -> None:
        with self._lock:
            data = {"_version": SETTINGS_VERSION, **dict(sorted(self._values.items()))}
            tmp = self.path.with_suffix(".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                log.exception("Could not save settings")

    def flush(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        self.save()

    def export_to(self, path: Path, prefixes: Iterable[str] = ()) -> None:
        values = {k: v for k, v in self._values.items() if not prefixes or any(k.startswith(p) for p in prefixes)}
        Path(path).write_text(json.dumps(values, indent=2, ensure_ascii=False), encoding="utf-8")

    def import_from(self, path: Path, prefixes: Iterable[str] = ()) -> int:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        n = 0
        for key, value in data.items():
            if key.startswith("_") or (prefixes and not any(key.startswith(p) for p in prefixes)):
                continue
            self.set(key, value, save=False)
            n += 1
        self._schedule_save()
        return n
