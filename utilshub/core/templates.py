"""One placeholder language for file names, links, clipboard formats and report lines.

    {name}            first value of a field            {name_all}   all values joined with "; "
    {name|upper}      filters, chainable:  {name|title|slug}
    {date} {time} {year} {month} {day} {original} {filename} {stem} {ext}   built-ins
    \\t = tab, \\n = new line  (clipboard formats)

Filters: upper lower title slug first last initials digits trim url words2 (first two words)
Unknown placeholders render as an empty string so a missing field never breaks a name.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .textutil import sanitize_filename, slugify

_PLACEHOLDER = re.compile(r"\{\s*([A-Za-z_][\w]*)\s*((?:\|\s*[A-Za-z0-9_]+\s*)*)\}")

FILTERS = {
    "upper": str.upper,
    "lower": str.lower,
    "title": lambda s: s.title(),
    "slug": slugify,
    "first": lambda s: s.split()[0] if s.split() else "",
    "last": lambda s: s.split()[-1] if s.split() else "",
    "initials": lambda s: "".join(w[0].upper() for w in s.split() if w),
    "digits": lambda s: re.sub(r"\D", "", s),
    "trim": lambda s: s.strip(),
    "url": lambda s: quote(s, safe=""),
    "words2": lambda s: " ".join(s.split()[:2]),
    "safe": sanitize_filename,
}


def base_values(fields: dict[str, list[str]] | None = None, original: Path | str | None = None,
                filename: str = "") -> dict[str, str]:
    """Placeholder values for a record: first value, `_all` joined values and the built-ins."""
    values: dict[str, str] = {}
    for key, vals in (fields or {}).items():
        values[key] = vals[0] if vals else ""
        values[f"{key}_all"] = "; ".join(vals)
    now = datetime.now()
    values.setdefault("date", now.strftime("%Y-%m-%d"))
    values.setdefault("time", now.strftime("%H-%M-%S"))
    values.update(year=now.strftime("%Y"), month=now.strftime("%m"), day=now.strftime("%d"))
    if original is not None:
        values["original"] = Path(original).stem
        values["original_ext"] = Path(original).suffix
    if filename:
        values["filename"] = filename
        values["stem"] = Path(filename).stem
        values["ext"] = Path(filename).suffix
    return values


def render(template: str, values: dict[str, str], unescape: bool = False, filters_default: str = "") -> str:
    """Fill `template`. Raises ValueError only for an unbalanced brace."""
    if template.count("{") != template.count("}"):
        raise ValueError("Unbalanced { } in template")
    if unescape:
        template = template.replace("\\t", "\t").replace("\\n", "\n")

    def sub(m: re.Match) -> str:
        text = str(values.get(m.group(1), ""))
        chain = [f.strip() for f in m.group(2).split("|") if f.strip()]
        for name in chain or ([filters_default] if filters_default else []):
            fn = FILTERS.get(name)
            if fn:
                text = fn(text)
        return text

    return _PLACEHOLDER.sub(sub, template)


def used_placeholders(template: str) -> list[str]:
    return [m.group(1) for m in _PLACEHOLDER.finditer(template)]


def tidy_filename(raw: str) -> str:
    """Remove the debris an empty placeholder leaves ('A -  - B', trailing dashes) and illegal characters."""
    raw = re.sub(r"(\s*[-_]\s*){2,}", " - ", raw)
    return sanitize_filename(raw.strip(" -_.,"))


def render_filename(template: str, fields: dict[str, list[str]], original: Path, fallback: str = "") -> str:
    try:
        raw = render(template, base_values(fields, original))
    except ValueError:
        raw = (fields.get("name") or [""])[0]
    return tidy_filename(raw) or sanitize_filename(fallback or original.stem)


def render_folder(template: str, values: dict[str, str]) -> Path:
    """'{surname}/{year}' -> Path('Smith/2026').  Empty parts are dropped; '..' and drive letters are never produced."""
    parts = []
    try:
        raw = render(template or "", values)
    except ValueError:
        return Path()
    for part in re.split(r"[\\/]+", raw):
        part = tidy_filename(part)
        if part and part not in (".", ".."):
            parts.append(part)
    return Path(*parts) if parts else Path()


def render_line(template: str, values: dict[str, str]) -> str:
    """Render one line of text; '' when the line uses placeholders and every one of them is empty."""
    names = used_placeholders(template)
    text = render(template, values)
    if names and not any(str(values.get(n, "")).strip() for n in names):
        return ""
    return text.strip()


def render_link(template: str, fields: dict[str, list[str]], original: Path, filename: str) -> str:
    """URL template: every value is URL-encoded unless it uses {key|raw}."""
    values = base_values(fields, original, filename)
    def sub(m: re.Match) -> str:
        key, chain = m.group(1), [f.strip() for f in m.group(2).split("|") if f.strip()]
        if "raw" in chain:
            return values.get(key, "")
        text = values.get(key, "")
        for name in chain:
            fn = FILTERS.get(name)
            if fn:
                text = fn(text)
        return quote(text, safe="")

    if template.count("{") != template.count("}"):
        raise ValueError("Unbalanced { } in template")
    return _PLACEHOLDER.sub(sub, template.strip())


def render_clipboard(template: str, fields: dict[str, list[str]], original: Path | str | None,
                     filename: str = "") -> str:
    return render(template, base_values(fields, original, filename), unescape=True)
