"""Lego-style widgets for the Workflows tool.

    BrickPalette   the "box": coloured bricks grouped by kind; drag one onto the stack (or click it to add it)
    BrickStack     the "baseplate": bricks snapped one above the other; drag to reorder, drag back to the box to remove

Both only *draw and report*: the Workflows tool owns the data.  Geometry lives in `lego_layout` (unit-tested).
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Callable

from ..core.workflow import Brick, bricks_by_group
from .lego_layout import StackLayout, mix, shade
from .widgets import ScrollFrame, bind_wheel, tip

DRAG_START_PX = 6


@dataclass
class BrickView:
    """What the stack needs to know to draw one brick."""
    title: str
    icon: str
    color: str
    summary: str = ""
    enabled: bool = True
    badge: str = ""          # e.g. "12 → 8" after a run
    level: str = ""          # "" | ok | warn | error | skipped  (colour of the badge)
    problem: str = ""        # shown as a warning mark


class DragGhost:
    """A small floating brick that follows the mouse while dragging."""

    def __init__(self, master: tk.Misc, text: str, color: str) -> None:
        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        try:
            self.top.attributes("-topmost", True)
            self.top.attributes("-alpha", 0.88)
        except tk.TclError:
            pass
        tk.Label(self.top, text=f"  {text}  ", bg=color, fg="#ffffff", font=("TkDefaultFont", 10, "bold"),
                 padx=10, pady=8, relief="raised", bd=2).pack()

    def move(self, x_root: int, y_root: int) -> None:
        self.top.geometry(f"+{x_root + 14}+{y_root + 10}")

    def destroy(self) -> None:
        try:
            self.top.destroy()
        except tk.TclError:
            pass


def _inside(widget: tk.Misc, x_root: int, y_root: int) -> bool:
    try:
        x, y = x_root - widget.winfo_rootx(), y_root - widget.winfo_rooty()
        return 0 <= x < widget.winfo_width() and 0 <= y < widget.winfo_height()
    except tk.TclError:
        return False


# =========================================================================== the stack
class BrickStack(tk.Canvas):
    def __init__(self, parent: tk.Misc, theme, *, on_select: Callable[[int | None], None],
                 on_reorder: Callable[[int, int], None], on_drag_out: Callable[[int, int, int], None],
                 on_context: Callable[[int, int, int], None], on_key: Callable[[str], None]) -> None:
        super().__init__(parent, highlightthickness=0, takefocus=True)
        self.theme = theme
        theme.style_canvas(self)
        self.items: list[BrickView] = []
        self.selected: int | None = None
        self.layout = StackLayout()
        self.slot: int | None = None
        self._press_info: tuple[int, int, int] | None = None
        self._drag_index: int | None = None
        self._dx = 0
        self._ghost: DragGhost | None = None
        self.on_select, self.on_reorder, self.on_drag_out = on_select, on_reorder, on_drag_out
        self.on_context, self.on_key = on_context, on_key
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            self.bind(seq, self._on_context)
        for seq, name in (("<Delete>", "delete"), ("<BackSpace>", "delete"), ("<Up>", "up"), ("<Down>", "down"),
                          ("<Alt-Up>", "move_up"), ("<Alt-Down>", "move_down"), ("<space>", "toggle")):
            self.bind(seq, lambda e, n=name: (self.on_key(n), "break")[1])
        bind_wheel(self)
        theme.on_change(self._themed)

    # ------------------------------------------------------------------ data
    def set_items(self, items: list[BrickView], selected: int | None) -> None:
        self.items, self.selected = items, selected if selected is not None and selected < len(items) else None
        self.redraw()

    def _themed(self) -> None:
        if self.winfo_exists():
            self.configure(background=self.theme.p["canvas"])
            self.redraw()

    # ------------------------------------------------------------------ drawing
    def _rrect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1,
               x0, y1 - r, x0, y0 + r, x0, y0]
        return self.create_polygon(pts, smooth=True, **kw)

    def redraw(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        p, n = self.theme.p, len(self.items)
        w = max(self.winfo_width(), 320)
        self.layout = self.layout.with_width(w)
        L = self.layout
        self._dx = max(0, (w - (L.width + 2 * L.pad)) // 2)
        px0, py0, px1, py1 = L.plate_rect(n)
        px0, px1 = px0 + self._dx, px1 + self._dx
        plate = "#3f9d4f"
        self._rrect(px0, py0, px1, py1, 4, fill=plate, outline=shade(plate, -.3))
        for sx in range(px0 + 14, px1 - 20, 30):
            self.create_oval(sx, py0 + 3, sx + 14, py0 + 10, fill=shade(plate, .25), outline=shade(plate, -.2))
        if n == 0:
            self.create_text(w // 2, py1 + 58, text="Your workflow is empty.\n\nDrag bricks here from the box on the left,\n"
                             "or click a brick in the box to add it.", fill=p["muted"], justify="center",
                             font=("TkDefaultFont", 11))
            self.create_text(w // 2, py1 + 128, text="Tip: start with a blue \"Get files\" brick.", fill=p["muted"],
                             font=("TkDefaultFont", 9))
        for i in reversed(range(n)):                # bottom first: each brick then covers the studs of the one below
            self._draw_brick(i)
        self.configure(scrollregion=(0, 0, w, max(L.total_height(n) + (150 if n == 0 else 0), self.winfo_height())))
        self._draw_slot()

    def _draw_brick(self, i: int) -> None:
        L, it, p = self.layout, self.items[i], self.theme.p
        x0, y0, x1, y1 = L.rect(i)
        x0, x1 = x0 + self._dx, x1 + self._dx
        base = it.color if it.enabled else mix(it.color, "#9aa0a6", .78)
        if i == self._drag_index:
            base = mix(base, p["canvas"], .65)
        dark, light = shade(base, -.28), shade(base, .32)
        for sx in range(x0 + 18, x1 - 40, 44):                              # studs
            self.create_oval(sx, y0 - L.stud, sx + 24, y0 + 4, fill=light, outline=dark)
        self._rrect(x0, y0, x1, y1, 7, fill=base, outline=dark)
        self.create_line(x0 + 8, y1 - 1, x1 - 8, y1 - 1, fill=dark, width=2)
        if i == self.selected:
            self._rrect(x0 - 3, y0 - 3, x1 + 3, y1 + 3, 9, fill="", outline=p["accent"], width=3)
        fg = "#ffffff" if it.enabled else "#eceff1"
        cy = (y0 + y1) // 2
        self.create_text(x0 + 14, cy, text=str(i + 1), fill=shade(base, .6), font=("TkDefaultFont", 9, "bold"), anchor="w")
        self.create_text(x0 + 34, y0 + 19, text=f"{it.icon}  {it.title}", fill=fg, anchor="w",
                         font=("TkDefaultFont", 11, "bold"))
        right = 12 + (90 if it.badge else 0) + (24 if it.problem else 0)
        room = max(8, (x1 - x0 - 34 - right) // 7)
        text = it.summary if len(it.summary) <= room else it.summary[: room - 1] + "…"
        if not it.enabled:
            text = "switched off  •  " + text if text else "switched off"
        if text:
            self.create_text(x0 + 34, y0 + 40, text=text, fill=shade(base, .7), anchor="w", font=("TkDefaultFont", 9))
        rx = x1 - 12
        if it.problem:
            self.create_text(rx, cy, text="⚠", fill="#ffe066", anchor="e", font=("TkDefaultFont", 14, "bold"))
            rx -= 24
        if it.badge:
            pill = {"ok": "#14532d", "warn": "#92400e", "error": "#7f1d1d", "skipped": "#374151"}.get(it.level, "#1f2937")
            t = self.create_text(rx - 8, cy, text=it.badge, fill="#ffffff", anchor="e", font=("TkDefaultFont", 9, "bold"))
            bx0, by0, bx1, by1 = self.bbox(t)
            r = self._rrect(bx0 - 7, by0 - 3, bx1 + 7, by1 + 3, 6, fill=pill, outline="")
            self.tag_lower(r, t)

    def _draw_slot(self) -> None:
        self.delete("slot")
        if self.slot is None:
            return
        L, y = self.layout, self.layout.slot_y(self.slot)
        x0, x1 = L.pad + self._dx - 10, L.pad + L.width + self._dx + 10
        acc = self.theme.p["accent"]
        self.create_line(x0, y, x1, y, fill=acc, width=5, capstyle="round", tags="slot")
        self.create_polygon(x0 - 8, y - 7, x0 - 8, y + 7, x0 + 4, y, fill=acc, tags="slot")

    # ------------------------------------------------------------------ drop slot (used by the palette too)
    def slot_from_root(self, x_root: int, y_root: int) -> int | None:
        """Insertion slot under the screen position, or None when the pointer is not over the stack."""
        if not _inside(self, x_root, y_root):
            return None
        return self.layout.slot_at(self.canvasy(y_root - self.winfo_rooty()), len(self.items))

    def show_slot(self, slot: int | None) -> None:
        if slot != self.slot:
            self.slot = slot
            self._draw_slot()

    def contains_root(self, x_root: int, y_root: int) -> bool:
        return _inside(self, x_root, y_root)

    def scroll_to(self, index: int) -> None:
        """Make sure brick `index` is visible."""
        if not 0 <= index < len(self.items):
            return
        _, y0, _, y1 = self.layout.rect(index)
        top, h = self.canvasy(0), self.winfo_height()
        total = max(self.layout.total_height(len(self.items)), 1)
        if y0 < top:
            self.yview_moveto(max(0, (y0 - 10) / total))
        elif y1 > top + h:
            self.yview_moveto(max(0, (y1 - h + 10) / total))

    # ------------------------------------------------------------------ mouse
    def _on_press(self, e) -> None:
        self.focus_set()
        i = self.layout.index_at(self.canvasy(e.y), len(self.items))
        self._press_info = (i, e.x_root, e.y_root) if i is not None else None
        self.on_select(i)

    def _on_motion(self, e) -> None:
        if not self._press_info:
            return
        i, x0, y0 = self._press_info
        if self._ghost is None:
            if abs(e.x_root - x0) + abs(e.y_root - y0) < DRAG_START_PX:
                return
            it = self.items[i]
            self._ghost = DragGhost(self, f"{it.icon} {it.title}", it.color)
            self._drag_index = i
            self.redraw()
        self._ghost.move(e.x_root, e.y_root)
        self.show_slot(self.slot_from_root(e.x_root, e.y_root))

    def _on_release(self, e) -> None:
        info, ghost = self._press_info, self._ghost
        self._press_info, self._ghost = None, None
        if ghost is None or info is None:
            return
        ghost.destroy()
        i, slot = info[0], self.slot_from_root(e.x_root, e.y_root)
        self._drag_index, self.slot = None, None
        if slot is not None:
            self.on_reorder(i, slot)
        else:
            self.on_drag_out(i, e.x_root, e.y_root)
        self.redraw()

    def _on_context(self, e) -> None:
        self.focus_set()
        i = self.layout.index_at(self.canvasy(e.y), len(self.items))
        if i is not None:
            self.on_select(i)
            self.on_context(i, e.x_root, e.y_root)


# =========================================================================== the box
class BrickPalette(ttk.Frame):
    def __init__(self, parent: tk.Misc, theme, stack: BrickStack, *, on_add: Callable[[str], None],
                 on_insert: Callable[[str, int], None]) -> None:
        super().__init__(parent)
        self.theme, self.stack, self.on_add, self.on_insert = theme, stack, on_add, on_insert
        self._pending: tuple[str, int, int] | None = None
        self._ghost: DragGhost | None = None
        ttk.Label(self, text="Brick box", style="H3.TLabel").pack(anchor="w")
        self.query = tk.StringVar()
        ent = ttk.Entry(self, textvariable=self.query)
        ent.pack(fill="x", pady=(4, 6))
        tip(ent, "Type to find a brick (e.g. pdf, rename, folder)")
        self.query.trace_add("write", lambda *_: self.rebuild())
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        self.rebuild()

    def contains_root(self, x_root: int, y_root: int) -> bool:
        return _inside(self, x_root, y_root)

    def rebuild(self) -> None:
        body = self.scroll.body
        for w in body.winfo_children():
            w.destroy()
        q = self.query.get().strip().lower()
        shown = 0
        for _gid, title, color, bricks in bricks_by_group():
            found = [b for b in bricks if not q or q in b.title.lower() or q in b.description.lower() or q in b.id]
            if not found:
                continue
            tk.Label(body, text=title.upper(), fg=color, bg=self.theme.p["bg"], anchor="w",
                     font=("TkDefaultFont", 8, "bold")).pack(fill="x", pady=(8, 2))
            for b in found:
                self._chip(body, b)
                shown += 1
        if not shown:
            ttk.Label(body, text="No brick matches.", style="Muted.TLabel").pack(anchor="w", pady=8)
        self.scroll.bind_children()

    def _chip(self, parent: tk.Misc, b: Brick) -> None:
        chip = tk.Label(parent, text=f" {b.icon}  {b.title}", bg=b.color, fg="#ffffff", anchor="w", cursor="hand2",
                        font=("TkDefaultFont", 10, "bold"), padx=6, pady=6, relief="raised", bd=1)
        chip.pack(fill="x", pady=2, padx=(0, 6))
        tip(chip, f"{b.description}\n\nDrag it onto the baseplate, or click to add it.")
        chip.bind("<ButtonPress-1>", lambda e, bid=b.id: self._down(e, bid))
        chip.bind("<B1-Motion>", lambda e, b=b: self._move(e, b))
        chip.bind("<ButtonRelease-1>", self._up)

    def _down(self, e, brick_id: str) -> None:
        self._pending = (brick_id, e.x_root, e.y_root)

    def _move(self, e, b: Brick) -> None:
        if not self._pending:
            return
        if self._ghost is None:
            if abs(e.x_root - self._pending[1]) + abs(e.y_root - self._pending[2]) < DRAG_START_PX:
                return
            self._ghost = DragGhost(self, f"{b.icon} {b.title}", b.color)
        self._ghost.move(e.x_root, e.y_root)
        self.stack.show_slot(self.stack.slot_from_root(e.x_root, e.y_root))

    def _up(self, e) -> None:
        press, ghost = self._pending, self._ghost
        self._pending, self._ghost = None, None
        if press is None:
            return
        if ghost is None:                                   # a plain click
            self.on_add(press[0])
            return
        ghost.destroy()
        slot = self.stack.slot_from_root(e.x_root, e.y_root)
        self.stack.show_slot(None)
        if slot is not None:
            self.on_insert(press[0], slot)
