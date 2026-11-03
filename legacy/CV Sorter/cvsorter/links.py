"""Links generated from the final file name (OneDrive / SharePoint / archive URLs ...).

Template example:   https://contoso-my.sharepoint.com/personal/hr/Documents/Archive/{filename}
Every {placeholder} value is URL-encoded ("John Smith.pdf" -> "John%20Smith.pdf").
Use {key_raw} for the un-encoded value. Available: {filename} {stem} {ext} {date} {original}
and every field ({name}, {company}, your custom fields ...).
Each template becomes a field named after it (lower_snake_case), so a clipboard format can
contain e.g. {archive_link}.
"""
from __future__ import annotations

import string
from pathlib import Path
from urllib.parse import quote

from .config import Settings, slugify
from .extractor import FieldDef
from .fileops import placeholder_values


class _Encoded:
    """Mapping that URL-encodes values on lookup ('_raw' suffix = leave as is)."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def __getitem__(self, key: str) -> str:
        if key.endswith("_raw"):
            return self.values[key[:-4]]
        return quote(self.values[key], safe="")


def render_link(template: str, fields: dict[str, list[str]], original: Path, filename: str) -> str:
    """Raises ValueError for a malformed template."""
    values = placeholder_values(fields, original)
    values["filename"] = filename
    values["stem"] = Path(filename).stem
    values["ext"] = Path(filename).suffix
    try:
        return string.Formatter().vformat(template.strip(), (), _Encoded(values))
    except (KeyError, ValueError, IndexError, AttributeError) as exc:
        raise ValueError(f"Invalid link template: {exc}") from exc


def link_definitions(settings: Settings) -> list[FieldDef]:
    seen, defs = set(), []
    for tpl in settings.link_templates:
        key = slugify(tpl.name)
        if tpl.name.strip() and key not in seen:
            seen.add(key)
            defs.append(FieldDef(key, tpl.name.strip()))
    return defs


def build_links(settings: Settings, fields: dict[str, list[str]], original: Path,
                filename: str) -> dict[str, list[str]]:
    """{link key: [url]} for every configured link template."""
    links: dict[str, list[str]] = {}
    for tpl in settings.link_templates:
        key = slugify(tpl.name)
        if not tpl.name.strip() or key in links:
            continue
        try:
            links[key] = [render_link(tpl.template, fields, original, filename)]
        except ValueError:
            links[key] = [""]
    return links
