"""Folder triggers: "when a file lands in this folder, run that saved workflow on it".

Pure logic (no Tk): `Trigger` is the saved rule, `TriggerStore` keeps them in one JSON file and `TriggerRunner`
watches the folders (core.watcher) and calls `run(trigger, path)` for every finished file.

Safety nets built in, because an unattended rule can do damage if it loops:
  * a file is handled once (same path + same modified time),
  * a trigger that fires more than `max_per_minute` times is paused and reported - the usual cause is a workflow
    that writes its results back into the folder it watches,
  * files that exist when the trigger starts are ignored unless `process_existing` is set.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable

from .watcher import FolderWatcher

log = logging.getLogger(__name__)


def parse_extensions(text: str) -> set[str]:
    """'pdf, .JPG;png' -> {'.pdf', '.jpg', '.png'}; empty = every type."""
    out = set()
    for part in text.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip().lower().lstrip("*")
        if part:
            out.add(part if part.startswith(".") else "." + part)
    return out


@dataclass
class Trigger:
    name: str = ""
    folder: str = ""
    workflow: str = ""
    extensions: str = ""                 # "pdf, jpg" (empty = all)
    recursive: bool = False
    process_existing: bool = False
    quiet_seconds: float = 3.0           # wait until the file stopped growing
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Trigger":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def problem(self) -> str:
        """Why this trigger cannot run ('' = fine)."""
        if not self.folder.strip():
            return "no folder"
        if not self.workflow.strip():
            return "no workflow"
        return ""


class TriggerStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.items: list[Trigger] = []
        self.load()

    def load(self) -> None:
        self.items = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [Trigger.from_dict(d) for d in data.get("triggers", [])]
        except (OSError, ValueError, AttributeError, TypeError):
            log.exception("Could not read %s", self.path)
            try:
                self.path.replace(self.path.with_suffix(".broken.json"))
            except OSError:
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "triggers": [t.to_dict() for t in self.items]}, indent=2,
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, tid: str) -> Trigger | None:
        return next((t for t in self.items if t.id == tid), None)

    def put(self, trig: Trigger) -> None:
        for i, t in enumerate(self.items):
            if t.id == trig.id:
                self.items[i] = trig
                break
        else:
            self.items.append(trig)
        self.save()

    def remove(self, tid: str) -> None:
        self.items = [t for t in self.items if t.id != tid]
        self.save()


class TriggerRunner:
    """Starts/stops one folder watcher per trigger and calls `run(trigger, path)` for each finished file."""

    def __init__(self, run: Callable[[Trigger, Path], None], say: Callable[[str, str], None] | None = None,
                 max_per_minute: int = 30) -> None:
        self.run, self.say = run, say or (lambda message, level="info": None)
        self.max_per_minute = max_per_minute
        self._watchers: dict[str, FolderWatcher] = {}
        self._hits: dict[str, deque] = {}
        self._handled: dict[str, set] = {}
        self._order: dict[str, deque] = {}
        self._ignored: dict[str, float] = {}           # path -> when (ignored for 10 minutes)
        self._lock = threading.Lock()
        self.paused: set[str] = set()                    # tripped the rate limit

    def handled_names(self, tid: str) -> list[str]:
        """Names of the files this trigger handled since it started (newest last)."""
        with self._lock:
            return [Path(k[0]).name for k in self._order.get(tid, [])]

    def ignore(self, path: Path | str) -> None:
        """Never run a trigger on this file: it was made by a workflow (stops a workflow from re-triggering itself)."""
        with self._lock:
            self._ignored[str(path)] = time.monotonic()

    def is_running(self, tid: str) -> bool:
        w = self._watchers.get(tid)
        return bool(w and w.running)

    def running_ids(self) -> list[str]:
        return [t for t in self._watchers if self.is_running(t)]

    def start(self, trig: Trigger) -> bool:
        why = trig.problem()
        if why:
            self.say(f"Trigger '{trig.name or trig.id}' cannot start: {why}", "warn")
            return False
        self.stop(trig.id)
        self.paused.discard(trig.id)
        self._hits[trig.id] = deque()
        self._handled[trig.id] = set()
        self._order[trig.id] = deque(maxlen=500)
        w = FolderWatcher(Path(trig.folder), parse_extensions(trig.extensions), max(0.0, float(trig.quiet_seconds)),
                          lambda path, t=trig: self._on_file(t, path), process_existing=trig.process_existing,
                          recursive=trig.recursive)
        try:
            w.start()
        except OSError as exc:
            self.say(f"Trigger '{trig.name}': cannot watch {trig.folder} - {exc}", "error")
            return False
        self._watchers[trig.id] = w
        self.say(f"Watching {trig.folder} -> workflow '{trig.workflow}'", "info")
        return True

    def stop(self, tid: str) -> None:
        w = self._watchers.pop(tid, None)
        if w:
            w.stop()

    def stop_all(self) -> None:
        for tid in list(self._watchers):
            self.stop(tid)

    # ------------------------------------------------------------------ a finished file
    def _on_file(self, trig: Trigger, path: Path) -> None:
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError:
            return
        now = time.monotonic()
        with self._lock:
            if trig.id in self.paused or now - self._ignored.get(key[0], -1e9) < 600 or key in self._handled.setdefault(trig.id, set()):
                return
            hits = self._hits.setdefault(trig.id, deque())
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self.max_per_minute:
                self.paused.add(trig.id)
                tripped = True
            else:
                hits.append(now)
                self._handled[trig.id].add(key)
                order = self._order.setdefault(trig.id, deque(maxlen=500))
                if len(order) == order.maxlen:                    # keep memory bounded: forget the oldest key
                    self._handled[trig.id].discard(order[0])
                order.append(key)
                tripped = False
        if tripped:
            self.stop(trig.id)
            self.say(f"Trigger '{trig.name}' paused: more than {self.max_per_minute} files in a minute. "
                     "Does the workflow write its results into the folder it watches?", "warn")
            return
        try:
            self.run(trig, path)
        except Exception:
            log.exception("Trigger %s failed on %s", trig.name, path)
            self.say(f"Trigger '{trig.name}' failed on {path.name}", "error")
