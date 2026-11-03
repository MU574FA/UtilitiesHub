"""File-name templating and moving/copying files."""
from __future__ import annotations

import re
import shutil
import string
from datetime import datetime
from pathlib import Path

from .config import Settings

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class _Values(dict):
    """Missing placeholders render as an empty string instead of raising."""

    def __missing__(self, key: str) -> str:
        return ""


def sanitize(name: str, max_len: int = 150) -> str:
    name = _ILLEGAL.sub("", name)
    return " ".join(name.split()).strip(" .")[:max_len].strip(" .")


def placeholder_values(fields: dict[str, list[str]], original: Path) -> _Values:
    """Values for {key} (first value), {key_all} (all values), {date} and {original}."""
    values = _Values()
    for key, vals in fields.items():
        values[key] = vals[0] if vals else ""
        values[f"{key}_all"] = "; ".join(vals)
    values["date"] = datetime.now().strftime("%Y-%m-%d")
    values["original"] = original.stem
    return values


def render_filename(template: str, fields: dict[str, list[str]], original: Path) -> str:
    """Render e.g. '{name} - {job_title}' using the first value of each field."""
    values = placeholder_values(fields, original)
    try:
        raw = string.Formatter().vformat(template, (), values)
    except (KeyError, ValueError, IndexError, AttributeError):
        raw = values["name"]
    raw = re.sub(r"(\s*[-_]\s*){2,}", " - ", raw)   # "A -  - B" when a field is empty
    stem = sanitize(raw.strip(" -_.,"))
    return stem or sanitize(original.stem)


def _numbered(target: Path) -> Path:
    for n in range(2, 10_000):
        candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Too many duplicates of {target.name}")


def duplicates_dir(settings: Settings) -> Path:
    custom = settings.duplicates_folder.strip()
    return Path(custom) if custom else Path(settings.output_folder) / "Duplicates"


def resolve_target(dest_dir: Path, stem: str, suffix: str, duplicate_policy: str) -> Path | None:
    """Final path for `<stem><suffix>` in `dest_dir`, or None if it must be skipped."""
    target = dest_dir / f"{stem}{suffix}"
    if target.exists():
        if duplicate_policy == "skip":
            return None
        if duplicate_policy == "number":
            return _numbered(target)
    return target  # "overwrite" (or no clash): transfer() replaces an existing file


def predicted_name(settings: Settings, stem: str, suffix: str, duplicate: bool = False) -> str:
    """The file name that saving would really produce (includes ' (2)' numbering)."""
    folder = duplicates_dir(settings) if duplicate else Path(settings.output_folder)
    policy = "number" if duplicate else settings.duplicate_policy
    target = resolve_target(folder, stem, suffix, policy)
    return target.name if target else f"{stem}{suffix}"


def transfer(src: Path, dest_dir: Path, stem: str, action: str = "move",
             duplicate_policy: str = "number") -> Path | None:
    """Move/copy `src` into `dest_dir` as `<stem><ext>`. Returns None if skipped."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = resolve_target(dest_dir, stem, src.suffix.lower(), duplicate_policy)
    if target is None:
        return None
    if target.exists():
        target.unlink()
    (shutil.copy2 if action == "copy" else shutil.move)(str(src), str(target))
    return target
