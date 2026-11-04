"""Order and visibility of tools in the sidebar / Home cards (pure logic, no Tk).

    ids = ordered_ids(["home", "cv", "docs"], saved=["docs", "cv"])      # -> ["home", "docs", "cv"]
    ids = move(ids, "cv", -1)                                              # one place earlier
    ids = move_to(ids, "docs", 2)                                          # to a given place
    hidden = set_hidden(hidden, "cv", True)

Rules
  * `home` is pinned to the first place and can never be hidden.
  * Ids the user never placed (a tool added by an update) land right after the tool that precedes them
    by default, so they appear where they "belong" instead of at the very end.
  * Unknown ids in the saved list (a tool that was removed) are ignored.
"""
from __future__ import annotations

from typing import Iterable

PINNED = "home"


def ordered_ids(default_ids: Iterable[str], saved: Iterable[str] | None) -> list[str]:
    """`default_ids` in their natural order; `saved` is the user's own order (may be stale or partial)."""
    default = list(dict.fromkeys(default_ids))
    known = set(default)
    out: list[str] = []
    seen: set[str] = set()
    for tid in saved or []:
        if tid in known and tid not in seen:
            out.append(tid)
            seen.add(tid)
    for idx, tid in enumerate(default):
        if tid in seen:
            continue
        pos = 0
        for prev in reversed(default[:idx]):
            if prev in seen:
                pos = out.index(prev) + 1
                break
        out.insert(pos, tid)
        seen.add(tid)
    return _pin(out)


def _pin(ids: list[str]) -> list[str]:
    if PINNED in ids and ids[0] != PINNED:
        ids = [PINNED] + [i for i in ids if i != PINNED]
    return ids


def _floor(ids: list[str]) -> int:
    """Lowest index a movable tool may take (the pinned tool keeps place 0)."""
    return 1 if ids and ids[0] == PINNED else 0


def move(ids: list[str], tid: str, delta: int) -> list[str]:
    """Move `tid` by `delta` places (negative = earlier)."""
    if tid not in ids or tid == PINNED:
        return list(ids)
    return move_to(ids, tid, ids.index(tid) + delta)


def move_to(ids: list[str], tid: str, index: int) -> list[str]:
    """Put `tid` at `index` (clamped; never before the pinned tool)."""
    if tid not in ids or tid == PINNED:
        return list(ids)
    rest = [i for i in ids if i != tid]
    index = max(_floor(ids), min(index, len(rest)))
    rest.insert(index, tid)
    return rest


def set_hidden(hidden: Iterable[str], tid: str, value: bool) -> list[str]:
    """New hidden list with `tid` hidden (value=True) or shown.  The pinned tool cannot be hidden."""
    out = [h for h in dict.fromkeys(hidden)]
    if tid == PINNED:
        return [h for h in out if h != PINNED]
    if value and tid not in out:
        out.append(tid)
    if not value:
        out = [h for h in out if h != tid]
    return out


def visible(ids: Iterable[str], hidden: Iterable[str]) -> list[str]:
    hide = set(hidden) - {PINNED}
    return [i for i in ids if i not in hide]


def prune_hidden(hidden: Iterable[str], known: Iterable[str]) -> list[str]:
    """Drop ids of tools that no longer exist (keeps the stored list tidy)."""
    k = set(known)
    return [h for h in dict.fromkeys(hidden) if h in k and h != PINNED]
