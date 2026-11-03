"""List of known candidate names: similarity check + automatic growth.

File formats: .txt (one name per line, '#' starts a comment) or .csv (first column, or the
column headed Name / Full name / Candidate). The file is re-read whenever it changes on disk,
so you can edit it in Excel / Notepad while the app is running.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .fuzzy import name_similarity, normalise_name

log = logging.getLogger(__name__)
HEADER_WORDS = {"name", "full name", "fullname", "candidate", "candidate name"}
EXACT = 0.999


class NameMatch(NamedTuple):
    name: str      # the name as written in the list (what you want to see)
    score: float   # 0..1


@dataclass
class NameCheck:
    status: str                    # "off" | "empty" | "new" | "similar" | "exact"
    matches: list[NameMatch]       # best first

    @property
    def best(self) -> NameMatch | None:
        return self.matches[0] if self.matches else None


class NameRegistry:
    def __init__(self, path: str = "", threshold: float = 0.85) -> None:
        self.path = Path(path.strip()).expanduser() if path and path.strip() else None
        self.threshold = min(1.0, max(0.5, threshold))
        self._entries: list[tuple[str, str]] = []     # (name as written, normalised)
        self._mtime: int | None = None
        self._csv_col, self._csv_cols = 0, 1

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def __len__(self) -> int:
        self._refresh()
        return len(self._entries)

    def names(self) -> list[str]:
        self._refresh()
        return [name for name, _ in self._entries]

    # ------------------------------------------------------------------ checking
    def check(self, name: str, limit: int = 5) -> NameCheck:
        if not self.enabled:
            return NameCheck("off", [])
        norm = normalise_name(name)
        if not norm:
            return NameCheck("empty", [])
        self._refresh()
        matches = []
        for original, other in self._entries:
            if abs(len(other) - len(norm)) > max(12, len(norm)):   # cheap pre-filter
                continue
            score = name_similarity(norm, other)
            if score >= self.threshold:
                matches.append(NameMatch(original, score))
        matches.sort(key=lambda m: m.score, reverse=True)
        matches = matches[:limit]
        if not matches:
            return NameCheck("new", [])
        return NameCheck("exact" if matches[0].score >= EXACT else "similar", matches)

    # ------------------------------------------------------------------ growing the list
    def add(self, name: str) -> bool:
        """Append `name` to the file. False if disabled, empty or already present."""
        name = " ".join(name.split())
        if not self.enabled or not name:
            return False
        self._refresh()
        norm = normalise_name(name)
        if any(name_similarity(norm, other) >= EXACT for _, other in self._entries):
            return False
        self.ensure_file()
        is_csv = self.path.suffix.lower() == ".csv"
        self._ensure_trailing_newline()
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            if is_csv:
                row = [""] * max(self._csv_cols, self._csv_col + 1)
                row[self._csv_col] = name
                csv.writer(fh).writerow(row)
            else:
                fh.write(name + "\n")
        self._entries.append((name, norm))
        self._mtime = self.path.stat().st_mtime_ns
        return True

    def ensure_file(self) -> None:
        if self.enabled and not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("Name\n" if self.path.suffix.lower() == ".csv" else "", encoding="utf-8")

    # ------------------------------------------------------------------ file handling
    def _ensure_trailing_newline(self) -> None:
        if self.path.stat().st_size == 0:
            return
        with self.path.open("rb+") as fh:
            fh.seek(-1, 2)
            if fh.read(1) not in (b"\n", b"\r"):
                fh.write(b"\n")

    def _refresh(self) -> None:
        if not self.enabled:
            return
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._entries, self._mtime = [], None
            return
        if mtime != self._mtime:
            self._load()
            self._mtime = mtime

    def _load(self) -> None:
        raw = self.path.read_bytes()
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeError:
                continue
        else:
            text = raw.decode("latin-1")
        if self.path.suffix.lower() == ".csv":
            names = self._names_from_csv(text)
        else:
            names = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        seen, entries = set(), []
        for name in names:
            norm = normalise_name(name)
            if norm and norm not in seen:
                seen.add(norm)
                entries.append((" ".join(name.split()), norm))
        self._entries = entries
        log.info("Names list: %d names loaded from %s", len(entries), self.path)

    def _names_from_csv(self, text: str) -> list[str]:
        rows = [row for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]
        self._csv_col, self._csv_cols = 0, max((len(r) for r in rows), default=1)
        if rows:
            for i, cell in enumerate(rows[0]):
                if cell.strip().casefold() in HEADER_WORDS:
                    self._csv_col = i
                    rows = rows[1:]
                    break
        return [row[self._csv_col].strip() for row in rows if len(row) > self._csv_col
                and row[self._csv_col].strip()]
