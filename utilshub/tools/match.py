"""Match: compare name lists and update one spreadsheet from another."""
from __future__ import annotations

from .hub import HubTool


class MatchHub(HubTool):
    id = "match"
    title = "Match & Sync"
    icon = "🔄"
    description = "Compare name lists (typos, word order, Arabic spelling) and update an old sheet from a new one, reviewing every change."
    order = 50
    MEMBERS = (("name_matcher", "NameMatcher", "Names"),
               ("sheet_sync", "SheetSync", "Sheet sync"))
