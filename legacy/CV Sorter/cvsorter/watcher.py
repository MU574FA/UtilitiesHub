"""Watch a folder and report files once they have finished being copied."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)

_IGNORED_PREFIXES = ("~$", ".")
_IGNORED_SUFFIXES = (".tmp", ".crdownload", ".part", ".partial", ".download")


def wait_until_stable(path: Path, quiet_seconds: float, timeout: float = 300) -> bool:
    """True once the file is non-empty, unchanged for `quiet_seconds` and readable."""
    deadline = time.monotonic() + timeout
    last_sig, stable_since = None, time.monotonic()
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        sig, now = (stat.st_size, stat.st_mtime_ns), time.monotonic()
        if sig != last_sig:
            last_sig, stable_since = sig, now
        elif stat.st_size > 0 and now - stable_since >= quiet_seconds:
            try:
                with open(path, "rb"):
                    return True
            except OSError:  # still locked by the copying program
                stable_since = now
        time.sleep(0.4)
    return False


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "FolderWatcher") -> None:
        self.watcher = watcher

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.watcher.enqueue(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:  # renamed/moved INTO the folder
        if not event.is_directory:
            self.watcher.enqueue(Path(event.dest_path))


class FolderWatcher:
    def __init__(self, folder: Path, extensions: list[str], quiet_seconds: float,
                 on_file: Callable[[Path], None], process_existing: bool = False) -> None:
        self.folder = folder.resolve()
        self.extensions = {e.lower() for e in extensions}
        self.quiet_seconds = quiet_seconds
        self.on_file = on_file
        self.process_existing = process_existing
        self._observer: Observer | None = None
        self._pending: set[Path] = set()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._observer is not None

    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(self.folder), recursive=False)
        self._observer.start()
        log.info("Watching %s", self.folder)
        if self.process_existing:
            for path in sorted(self.folder.iterdir()):
                self.enqueue(path)

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None

    def enqueue(self, path: Path) -> None:
        name = path.name.lower()
        if (path.parent.resolve() != self.folder
                or path.suffix.lower() not in self.extensions
                or name.startswith(_IGNORED_PREFIXES) or name.endswith(_IGNORED_SUFFIXES)):
            return
        with self._lock:
            if path in self._pending:
                return
            self._pending.add(path)
        threading.Thread(target=self._wait_and_dispatch, args=(path,), daemon=True).start()

    def _wait_and_dispatch(self, path: Path) -> None:
        try:
            if wait_until_stable(path, self.quiet_seconds) and path.is_file():
                self.on_file(path)
        except Exception:
            log.exception("Error while handling %s", path)
        finally:
            with self._lock:
                self._pending.discard(path)
