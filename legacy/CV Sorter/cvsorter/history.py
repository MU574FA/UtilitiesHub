"""Append-only CSV log of everything that was processed."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .config import Settings

BASE_COLUMNS = ["timestamp", "status", "original_file", "saved_as"]


def history_path(settings: Settings) -> Path:
    return Path(settings.output_folder) / "cv_history.csv"


def append(settings: Settings, original: Path, saved: Path | None, fields: dict[str, list[str]],
           status: str = "saved") -> None:
    """status: saved | duplicate | recorded (data kept, file left in place)."""
    path = history_path(settings)
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "original_file": original.name,
        "saved_as": saved.name if saved else "",
        **{key: "; ".join(vals) for key, vals in fields.items()},
    }
    rows: list[dict] = []
    columns = list(BASE_COLUMNS)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            columns = list(reader.fieldnames or columns)
            rows = list(reader)
    columns += [k for k in row if k not in columns]  # fields may change over time
    rows.append(row)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, restval="")
        writer.writeheader()
        writer.writerows(rows)
