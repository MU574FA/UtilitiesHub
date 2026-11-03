"""Watch a folder and report files once they have finished being copied.

Uses watchdog when installed, otherwise polls every couple of seconds (so it always works).
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from .fileops import wait_until_stable

log = logging.getLogger(__name__)

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    HAS_WATCHDOG = True
except ImportError:                                  # polling fallback
    HAS_WATCHDOG = False
    FileSystemEventHandler = object  # type: ignore
    Observer = None  # type: ignore

_IGNORED_PREFIXES = ("~$", ".")
_IGNORED_SUFFIXES = (".tmp", ".crdownload", ".part", ".partial", ".download")


class _Handler(FileSystemEventHandler):  # type: ignore[misc]
    def __init__(self, watcher: "FolderWatcher") -> None:
        self.watcher = watcher

    def on_created(self, event) -> None:
        if not event.is_directory:
            self.watcher.enqueue(Path(event.src_path))

    def on_moved(self, event) -> None:               # renamed/moved INTO the folder
        if not event.is_directory:
            self.watcher.enqueue(Path(event.dest_path))


class FolderWatcher:
    def __init__(self, folder: Path, extensions: list[str] | set[str], quiet_seconds: float,
                 on_file: Callable[[Path], None], process_existing: bool = False, recursive: bool = False,
                 poll_seconds: float = 2.0) -> None:
        self.folder = Path(folder).resolve()
        self.extensions = {e.lower() for e in extensions}
        self.quiet_seconds = quiet_seconds
        self.on_file = on_file
        self.process_existing = process_existing
        self.recursive = recursive
        self.poll_seconds = poll_seconds
        self._observer = None
        self._poller: threading.Thread | None = None
        self._stop = threading.Event()
        self._pending: set[Path] = set()
        self._seen: set[Path] = set()
        self._lock = threading.Lock()
        self.running = False

    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        existing = self._scan()
        if HAS_WATCHDOG:
            self._observer = Observer()
            self._observer.schedule(_Handler(self), str(self.folder), recursive=self.recursive)
            self._observer.start()
        else:
            self._seen = set(existing)
            self._poller = threading.Thread(target=self._poll, daemon=True, name="folder-poll")
            self._poller.start()
        self.running = True
        log.info("Watching %s (%s)", self.folder, "watchdog" if HAS_WATCHDOG else "polling")
        if self.process_existing:
            for path in sorted(existing):
                self.enqueue(path)

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None
        self.running = False

    def _scan(self) -> list[Path]:
        it = self.folder.rglob("*") if self.recursive else self.folder.iterdir()
        try:
            return [p for p in it if p.is_file()]
        except OSError:
            return []

    def _poll(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            for path in self._scan():
                if path not in self._seen:
                    self._seen.add(path)
                    self.enqueue(path)

    def enqueue(self, path: Path) -> None:
        name = path.name.lower()
        if not self.recursive and path.parent.resolve() != self.folder:
            return
        if (self.extensions and path.suffix.lower() not in self.extensions) or \
                name.startswith(_IGNORED_PREFIXES) or name.endswith(_IGNORED_SUFFIXES):
            return
        with self._lock:
            if path in self._pending:
                return
            self._pending.add(path)
        threading.Thread(target=self._wait_and_dispatch, args=(path,), daemon=True).start()

    def _wait_and_dispatch(self, path: Path) -> None:
        try:
            if wait_until_stable(path, self.quiet_seconds) and path.is_file() and not self._stop.is_set():
                self.on_file(path)
        except Exception:
            log.exception("Error while handling %s", path)
        finally:
            with self._lock:
                self._pending.discard(path)
            time.sleep(0)
