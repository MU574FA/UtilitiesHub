"""Geometry of the brick stack (pure maths, no Tk, so it is unit-tested).

Bricks are stacked top to bottom with no gap; the studs of a brick sit in the bottom strip of the brick above it
(and so are hidden by it), exactly like real bricks.  "Slot k" means "insert before brick k" (k == n: at the end).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StackLayout:
    width: int = 380           # brick width
    height: int = 58           # brick body height
    stud: int = 7              # stud height
    pad: int = 14              # space around the stack
    plate: int = 16            # base plate under the last brick

    def rect(self, i: int) -> tuple[int, int, int, int]:
        """(x0, y0, x1, y1) of brick i's body."""
        y0 = self.pad + self.stud + i * self.height
        return self.pad, y0, self.pad + self.width, y0 + self.height

    def plate_rect(self, n: int) -> tuple[int, int, int, int]:
        y0 = self.pad + self.stud + n * self.height
        return self.pad - 6, y0, self.pad + self.width + 6, y0 + self.plate

    def total_height(self, n: int) -> int:
        return self.pad + self.stud + max(n, 0) * self.height + self.plate + self.pad

    def index_at(self, y: float, n: int) -> int | None:
        """Brick under canvas y (None when above / below the stack)."""
        k = (y - self.pad - self.stud) // self.height
        return int(k) if 0 <= k < n else None

    def slot_at(self, y: float, n: int) -> int:
        """Where a brick dropped at canvas y would be inserted (0 .. n)."""
        k = round((y - self.pad - self.stud) / self.height)
        return max(0, min(n, int(k)))

    def slot_y(self, slot: int) -> int:
        """Canvas y of the line that marks `slot`."""
        return self.pad + self.stud + slot * self.height

    def with_width(self, canvas_width: int) -> "StackLayout":
        """Bricks follow the canvas width (within sensible limits)."""
        w = max(300, min(560, canvas_width - 2 * self.pad - 12))
        return StackLayout(w, self.height, self.stud, self.pad, self.plate) if w != self.width else self


def reorder(items: list, index: int, slot: int) -> list:
    """Move items[index] so it lands before original position `slot` (what a drag-and-drop means)."""
    out = list(items)
    if not 0 <= index < len(out):
        return out
    item = out.pop(index)
    out.insert(slot - 1 if slot > index else slot, item)
    return out


def shade(color: str, amount: float) -> str:
    """Lighten (amount > 0) or darken (amount < 0) a #rrggbb colour."""
    c = color.lstrip("#")
    rgb = [int(c[i:i + 2], 16) for i in (0, 2, 4)]
    target = 255 if amount >= 0 else 0
    return "#" + "".join(f"{round(v + (target - v) * abs(amount)):02x}" for v in rgb)


def mix(c1: str, c2: str, t: float) -> str:
    a = [int(c1.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    b = [int(c2.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))
