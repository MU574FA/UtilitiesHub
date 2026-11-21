"""Create or append to a CSV file chosen by the user ("add this result to my master list").

New columns are added when the records have fields the file does not have yet; existing rows are kept.
Rows are appended in place when the file already has every column (fast, even for thousands of rows); only a new
column makes the file be rewritten.  A lock per file stops two tools from overwriting each other's rows, and text
that would run as a formula in Excel is neutralised (see `sheets.formula_safe`).
"""
from __future__ import annotations

import csv
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .sheets import cell_text, formula_safe


class AppendError(Exception):
    """A user-facing problem (file open in Excel, not a CSV ...)."""


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve()).lower()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def pending_path(path: str | Path) -> Path:
    """Where rows wait while the real file is locked (e.g. open in Excel)."""
    p = Path(str(path).strip().strip('"'))
    return p.with_name(p.stem + ".pending.csv")


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def append_records(path: str | Path, records: list[dict[str, Any]], timestamp: bool = True) -> int:
    """Append `records` (dicts) to the CSV at `path`, creating it when missing.  Returns the number of rows added."""
    path = Path(str(path).strip().strip('"'))
    if not records:
        return 0
    if path.suffix.lower() not in (".csv", ".txt", ""):
        raise AppendError("Appending works with .csv files. Pick a .csv file (Excel opens it directly).")
    if not path.suffix:
        path = path.with_suffix(".csv")
    stamp = datetime.now().isoformat(timespec="seconds")
    new = [({"timestamp": stamp} if timestamp else {}) | {k: formula_safe(cell_text(v)) for k, v in r.items()} for r in records]
    try:
        with _lock_for(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size:
                with path.open(newline="", encoding="utf-8-sig") as fh:
                    header = next(csv.reader(fh), [])
                if header and all(k in header for r in new for k in r):
                    _append_in_place(path, header, new)
                    return len(new)
                columns, rows = read_rows(path)
            else:
                columns, rows = [], []
            for r in new:
                columns += [k for k in r if k not in columns]
            tmp = path.with_name(path.name + ".tmp")
            with tmp.open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns, restval="")
                writer.writeheader()
                writer.writerows(rows + new)
            tmp.replace(path)
    except PermissionError as exc:
        raise AppendError(f"Cannot write {path.name}. Close it (e.g. in Excel) and try again.") from exc
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise AppendError(f"Cannot append to {path.name}: {exc}") from exc
    return len(new)


def _append_in_place(path: Path, header: list[str], rows: list[dict]) -> None:
    with path.open("rb") as fh:
        fh.seek(-1, 2)
        ends_with_newline = fh.read(1) in (b"\n", b"\r")
    with path.open("a", newline="", encoding="utf-8") as fh:
        if not ends_with_newline:
            fh.write("\r\n")
        csv.DictWriter(fh, fieldnames=header, restval="").writerows(rows)


def flush_pending(path: str | Path) -> int:
    """Move rows that were parked in `<name>.pending.csv` into the real file.  Returns how many were moved (0 = none)."""
    pend = pending_path(path)
    if not pend.exists():
        return 0
    _cols, rows = read_rows(pend)
    if not rows:
        pend.unlink(missing_ok=True)
        return 0
    n = append_records(path, rows, timestamp=False)           # rows keep the timestamp they were given
    pend.unlink(missing_ok=True)
    return n
