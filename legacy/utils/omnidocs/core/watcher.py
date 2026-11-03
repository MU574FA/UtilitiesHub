"""Background Directory Watcher with File Stability Heuristics."""
from __future__ import annotations

import logging
import queue
import time
from pathlib import Path
from typing import Callable

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
    HAS_WATCHDOG = True
except Exception:
    HAS_WATCHDOG = False

log = logging.getLogger(__name__)

TEMP_PREFIXES = ("~", ".", "tmp_")
TEMP_SUFFIXES = (".tmp", ".crdownload", ".part", ".partial", ".download")


def wait_until_stable(path: Path | str, quiet_seconds: float = 1.5, timeout: float = 30.0) -> bool:
    """
    Polls file until its size and modification timestamp remain constant
    for `quiet_seconds`, preventing race conditions during active writes/downloads.
    """
    p = Path(path)
    start_time = time.time()
    last_size = -1
    last_mtime = -1
    stable_since = None

    while time.time() - start_time < timeout:
        try:
            if not p.exists():
                time.sleep(0.3)
                continue

            stat = p.stat()
            size = stat.st_size
            mtime = stat.st_mtime

            # Try opening file in read mode to test lock
            with open(p, "rb"):
                pass

            if size == last_size and mtime == last_mtime:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= quiet_seconds:
                    return True
            else:
                last_size = size
                last_mtime = mtime
                stable_since = time.time()

        except (OSError, PermissionError):
            stable_since = None

        time.sleep(0.3)

    return False


class WatcherHandler(FileSystemEventHandler if HAS_WATCHDOG else object):
    def __init__(self, event_queue: queue.Queue, filter_fn: Callable[[Path], bool] | None = None):
        super().__init__()
        self.queue = event_queue
        self.filter_fn = filter_fn

    def on_created(self, event: Any) -> None:
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_modified(self, event: Any) -> None:
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def _handle(self, path: Path) -> None:
        name = path.name
        if any(name.startswith(p) for p in TEMP_PREFIXES):
            return
        if any(name.endswith(s) for s in TEMP_SUFFIXES):
            return
        if self.filter_fn and not self.filter_fn(path):
            return
        self.queue.put(path)


class DirectoryWatcher:
    """Starts and stops background directory monitoring."""
    def __init__(self, folder: Path | str, event_queue: queue.Queue,
                 filter_fn: Callable[[Path], bool] | None = None):
        self.folder = Path(folder)
        self.queue = event_queue
        self.filter_fn = filter_fn
        self.observer: Any = None

    def start(self) -> bool:
        if not HAS_WATCHDOG or not self.folder.is_dir():
            return False
        try:
            self.observer = Observer()
            handler = WatcherHandler(self.queue, self.filter_fn)
            self.observer.schedule(handler, str(self.folder), recursive=False)
            self.observer.start()
            log.info("Started watching directory: %s", self.folder)
            return True
        except Exception as exc:
            log.error("Failed to start directory watcher: %s", exc)
            return False

    def stop(self) -> None:
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join(timeout=2.0)
                log.info("Stopped directory watcher")
            except Exception:
                pass
            self.observer = None

