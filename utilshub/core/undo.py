"""A tiny generic undo/redo stack (DocPacker assignments, list edits, ...).

    undo.push("assign file", undo_fn=lambda: ..., redo_fn=lambda: ...)
    undo.undo(); undo.redo()
or, for state-snapshot style tools:
    undo.snapshot("remove files", capture=lambda: copy.deepcopy(state), restore=lambda s: ...)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class _Action:
    label: str
    undo_fn: Callable[[], None]
    redo_fn: Callable[[], None] | None = None


class UndoStack:
    def __init__(self, limit: int = 60) -> None:
        self.limit = limit
        self._undo: list[_Action] = []
        self._redo: list[_Action] = []
        self.listeners: list[Callable[[], None]] = []

    def _fire(self) -> None:
        for cb in self.listeners:
            cb()

    def push(self, label: str, undo_fn: Callable[[], None], redo_fn: Callable[[], None] | None = None) -> None:
        self._undo.append(_Action(label, undo_fn, redo_fn))
        del self._undo[:-self.limit]
        self._redo.clear()
        self._fire()

    def snapshot(self, label: str, capture: Callable[[], Any], restore: Callable[[Any], None]) -> None:
        """Remember the state *before* a change; undo restores it (and redo restores the state after)."""
        before = capture()
        holder: dict[str, Any] = {}

        def undo() -> None:
            holder["after"] = capture()
            restore(before)

        def redo() -> None:
            restore(holder["after"])

        self.push(label, undo, redo)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def next_label(self) -> str:
        return self._undo[-1].label if self._undo else ""

    def undo(self) -> str:
        if not self._undo:
            return ""
        action = self._undo.pop()
        action.undo_fn()
        if action.redo_fn:
            self._redo.append(action)
        self._fire()
        return action.label

    def redo(self) -> str:
        if not self._redo:
            return ""
        action = self._redo.pop()
        if action.redo_fn:
            action.redo_fn()
        self._undo.append(action)
        self._fire()
        return action.label

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._fire()
