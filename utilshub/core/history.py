"""Append-only CSV logs (one per tool) that Excel opens directly."""
from __future__ import annotations

import csv
import threading
from datetime import datetime
from pathlib import Path


class HistoryLog:
    BASE_COLUMNS = ["timestamp", "status", "original_file", "saved_as"]

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, row: dict, status: str = "saved", original: str = "", saved_as: str = "") -> None:
        record = {"timestamp": datetime.now().isoformat(timespec="seconds"), "status": status,
                  "original_file": original, "saved_as": saved_as, **{k: v for k, v in row.items()}}
        with self._lock:
            rows, columns = [], list(self.BASE_COLUMNS)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                with self.path.open(newline="", encoding="utf-8-sig") as fh:
                    reader = csv.DictReader(fh)
                    columns = list(reader.fieldnames or columns)
                    rows = list(reader)
            columns += [k for k in record if k not in columns]          # fields may change over time
            rows.append(record)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns, restval="")
                writer.writeheader()
                writer.writerows(rows)
            tmp.replace(self.path)

    def read(self) -> tuple[list[str], list[dict]]:
        if not self.path.exists():
            return list(self.BASE_COLUMNS), []
        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
            return list(reader.fieldnames or self.BASE_COLUMNS), rows


def join_fields(fields: dict[str, list[str]]) -> dict[str, str]:
    return {k: "; ".join(v) for k, v in fields.items()}
