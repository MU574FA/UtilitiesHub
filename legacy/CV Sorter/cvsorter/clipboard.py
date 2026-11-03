"""Render the user-defined clipboard formats (what gets pasted into Excel, a CRM, e-mail...).

A format is a template such as  {name}\t{job_title}\t{phone}
  {key}      first value of a field          {key_all}   all values joined with "; "
  {filename} the new file name               {date} {original}
  \t = tab (cells in Excel / Sheets)         \n = new line
Empty fields keep their position, so columns never shift.
"""
from __future__ import annotations

import string
from pathlib import Path

from .config import ClipboardFormat, Settings
from .fileops import placeholder_values


def unescape(template: str) -> str:
    return template.replace("\\t", "\t").replace("\\n", "\n")


def render_clipboard(template: str, fields: dict[str, list[str]], original: Path,
                     filename: str = "") -> str:
    """Raises ValueError for a malformed template."""
    values = placeholder_values(fields, original)
    values["filename"] = filename
    try:
        return string.Formatter().vformat(unescape(template), (), values)
    except (KeyError, ValueError, IndexError, AttributeError) as exc:
        raise ValueError(f"Invalid clipboard template: {exc}") from exc


def get_format(settings: Settings, name: str) -> ClipboardFormat | None:
    return next((f for f in settings.clipboard_formats if f.name == name), None)
