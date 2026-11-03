"""Create or append to a CSV file chosen by the user ("add this result to my master list").

New columns are added when the records have fields the file does not have yet; existing rows are kept.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from .sheets import cell_text


class AppendError(Exception):
    """A user-facing problem (file open in Excel, not a CSV ...)."""


def append_records(path: str | Path, records: list[dict[str, Any]], timestamp: bool = True) -> int:
    """Append `records` (dicts) to the CSV at `path`, creating it when missing.  Returns the number of rows added."""
    path = Path(str(path).strip().strip('"'))
    if not records:
        return 0
    if path.suffix.lower() not in (".csv", ".txt", ""):
        raise AppendError("Appending works with .csv files. Pick a .csv file (Excel opens it directly).")
    if not path.suffix:
        path = path.with_suffix(".csv")
    rows: list[dict] = []
    columns: list[str] = []
    try:
        if path.exists() and path.stat().st_size:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                columns = list(reader.fieldnames or [])
                rows = list(reader)
        stamp = datetime.now().isoformat(timespec="seconds")
        new = [({"timestamp": stamp} if timestamp else {}) | {k: cell_text(v) for k, v in r.items()} for r in records]
        for r in new:
            columns += [k for k in r if k not in columns]
        path.parent.mkdir(parents=True, exist_ok=True)
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

