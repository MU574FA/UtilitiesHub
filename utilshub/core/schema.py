"""Declarative option descriptions.

Every tool declares its settings as a list of `Opt`.  The same declaration gives
  * the default value (Settings.get falls back to it),
  * the widget on the generic settings page (kind -> widget),
  * help text, grouping and "advanced" flagging.

Kinds understood by the generic settings page:
  bool int float str text choice folder file append(a CSV to create / add to) color font list(of str) strings
  record (a RecordEditor; `editor` names it)   custom (tool builds the widget itself)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Opt:
    key: str                         # "cv.watch_folder"  (page = prefix before the first dot)
    default: Any
    label: str
    kind: str = "str"
    help: str = ""
    section: str = ""                # heading inside the page
    choices: Any = None              # list of values or list of (value, label)
    min: float | None = None
    max: float | None = None
    step: float | None = None
    advanced: bool = False
    editor: str = ""                 # for kind="record": name registered in ui.records
    filetypes: Any = None            # for kind="file"
    width: int = 0
    page: str = ""                   # override of the page (default: key prefix)

    @property
    def page_id(self) -> str:
        return self.page or self.key.split(".", 1)[0]

    def choice_pairs(self) -> list[tuple[Any, str]]:
        out = []
        for c in self.choices or []:
            out.append((c[0], str(c[1])) if isinstance(c, (tuple, list)) else (c, str(c)))
        return out


@dataclass
class Page:
    """A settings page (one per tool + a few shared ones)."""
    id: str
    title: str
    icon: str = ""
    order: int = 50
    description: str = ""
    builder: Any = None              # optional callable(parent, app) -> widget for hand-made pages
    opts: list[Opt] = field(default_factory=list)
