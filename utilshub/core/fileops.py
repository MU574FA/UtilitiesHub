"""Moving / copying files safely, with an operation journal so everything can be undone.

Every tool moves files through `transfer()`; if a `Journal` is supplied each move/copy/rename is
recorded and `Journal.undo(entry)` puts it back.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- naming
def numbered(target: Path) -> Path:
    """photo.jpg -> photo (2).jpg -> photo (3).jpg ..."""
    for n in range(2, 10_000):
        candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Too many duplicates of {target.name}")


def resolve_target(dest_dir: Path, stem: str, suffix: str, policy: str) -> Path | None:
    """Final path for `<stem><suffix>` in `dest_dir`, or None when the policy says skip.

    policy: number | overwrite | skip
    """
    target = dest_dir / f"{stem}{suffix}"
    if target.exists():
        if policy == "skip":
            return None
        if policy == "number":
            return numbered(target)
    return target


def predicted_name(dest_dir: Path, stem: str, suffix: str, policy: str) -> str:
    target = resolve_target(dest_dir, stem, suffix, policy)
    return target.name if target else f"{stem}{suffix}"


# --------------------------------------------------------------------------- journal
@dataclass
class JournalEntry:
    id: str
    time: str
    tool: str
    action: str                 # move | copy | rename | create | delete-copy
    src: str
    dest: str
    note: str = ""
    undone: bool = False


class Journal:
    """Append-only JSON-lines log of file operations (newest last) with undo support."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._entries: list[JournalEntry] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._entries.append(JournalEntry(**json.loads(line)))
        except (OSError, ValueError, TypeError):
            log.exception("Could not read the journal")

    def _rewrite(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(json.dumps(asdict(e), ensure_ascii=False) for e in self._entries) + "\n",
                       encoding="utf-8")
        tmp.replace(self.path)

    def record(self, tool: str, action: str, src: Path | str, dest: Path | str, note: str = "") -> JournalEntry:
        entry = JournalEntry(uuid.uuid4().hex[:10], datetime.now().isoformat(timespec="seconds"), tool, action,
                             str(src), str(dest), note)
        with self._lock:
            self._entries.append(entry)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            except OSError:
                log.exception("Could not write the journal")
        return entry

    def entries(self, tool: str | None = None) -> list[JournalEntry]:
        with self._lock:
            return [e for e in self._entries if tool is None or e.tool == tool]

    def last_undoable(self, tool: str | None = None) -> JournalEntry | None:
        for e in reversed(self.entries(tool)):
            if not e.undone and e.action in ("move", "copy", "rename"):
                return e
        return None

    def can_undo(self, entry: JournalEntry) -> tuple[bool, str]:
        if entry.undone:
            return False, "Already undone."
        if entry.action not in ("move", "copy", "rename"):
            return False, "This kind of action cannot be undone."
        dest = Path(entry.dest)
        if not dest.exists():
            return False, "The file is no longer there."
        if entry.action in ("move", "rename") and Path(entry.src).exists():
            return False, "Something else now sits at the original location."
        return True, ""

    def undo(self, entry: JournalEntry) -> str:
        """Put the file back (move/rename) or remove the copy. Returns a message."""
        ok, why = self.can_undo(entry)
        if not ok:
            raise OSError(why)
        dest, src = Path(entry.dest), Path(entry.src)
        if entry.action == "copy":
            dest.unlink()
            msg = f"Removed the copy {dest.name}"
        else:
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(src))
            msg = f"Moved {dest.name} back to {src.parent}"
        with self._lock:
            entry.undone = True
            self._rewrite()
        return msg

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._rewrite()


# --------------------------------------------------------------------------- transfer
def transfer(src: Path, dest_dir: Path, stem: str | None = None, action: str = "move", policy: str = "number",
             journal: Journal | None = None, tool: str = "", note: str = "") -> Path | None:
    """Move/copy `src` into `dest_dir` as `<stem><ext>`. Returns the new path, or None if skipped."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = resolve_target(dest_dir, stem if stem is not None else src.stem, src.suffix.lower()
                            if stem is not None else src.suffix, policy)
    if target is None:
        return None
    if target.exists():
        target.unlink()
    (shutil.copy2 if action == "copy" else shutil.move)(str(src), str(target))
    if journal is not None:
        journal.record(tool, "copy" if action == "copy" else "move", src, target, note)
    return target


def rename_in_place(src: Path, new_name: str, journal: Journal | None = None, tool: str = "") -> Path:
    target = src.with_name(new_name)
    if target.exists() and target != src:
        target = numbered(target)
    src.rename(target)
    if journal is not None:
        journal.record(tool, "rename", src, target)
    return target


def wait_until_stable(path: Path, quiet_seconds: float = 1.5, timeout: float = 300) -> bool:
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
            except OSError:                      # still locked by the copying program
                stable_since = now
        time.sleep(0.4)
    return False
