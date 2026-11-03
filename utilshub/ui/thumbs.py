"""Thumbnail grid for pages: select (click / Ctrl / Shift), drag to reorder, rotate, remove.

    grid = ThumbGrid(parent, app, size=140)
    grid.set_items([ThumbItem(path, page=0, label="a.pdf p1"), ...])
    grid.on_change = lambda: ...        # order / rotation / removal changed
    grid.rotate_selected(90); grid.delete_selected(); grid.move_selected(-1)
"""
from __future__ import annotations

import threading
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk
from typing import Any, Callable

from PIL import ImageTk

from .widgets import Tooltip, bind_wheel


@dataclass
class ThumbItem:
    path: str
    page: int = 0
    rotation: int = 0                 # manual extra clockwise degrees
    fix: dict | None = None
    label: str = ""
    data: Any = None                  # caller's payload (e.g. a Piece)
    badge: str = ""                   # small text in the corner (e.g. "Passport")
    selected: bool = False
    uid: int = field(default=0)


class ThumbGrid(ttk.Frame):
    PAD = 10

    def __init__(self, parent: tk.Misc, app, size: int | None = None, toolbar: bool = True) -> None:
        super().__init__(parent)
        self.app = app
        self.size = size or app.settings.get_int("preview.thumb_size", 140)
        self.items: list[ThumbItem] = []
        self.on_change: Callable[[], None] | None = None
        self.on_select: Callable[[list[ThumbItem]], None] | None = None
        self.on_open: Callable[[ThumbItem], None] | None = None
        self._photos: dict[tuple, ImageTk.PhotoImage] = {}
        self._pending: set[tuple] = set()
        self._pos: list[tuple[int, int, int, int]] = []     # x, y, w, h per item
        self._drag: dict | None = None
        self._anchor = 0
        self._uid = 0
        if toolbar:
            self._toolbar()
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, highlightthickness=0)
        ys = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        app.theme.style_canvas(self.canvas)
        bind_wheel(self.canvas)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-1>", self._double)
        self.canvas.bind("<Delete>", lambda e: self.delete_selected())
        self.canvas.bind("<Control-a>", lambda e: self.select_all())
        self.canvas.bind("<Left>", lambda e: self.move_selected(-1))
        self.canvas.bind("<Right>", lambda e: self.move_selected(1))
        app.theme.on_change(self.redraw)

    def _toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        for text, cmd, tip in (("⟲", lambda: self.rotate_selected(-90), "Rotate selected pages left"),
                               ("⟳", lambda: self.rotate_selected(90), "Rotate selected pages right"),
                               ("◀", lambda: self.move_selected(-1), "Move selected earlier (←)"),
                               ("▶", lambda: self.move_selected(1), "Move selected later (→)"),
                               ("🗑", self.delete_selected, "Remove selected pages (Del)")):
            b = ttk.Button(bar, text=text, command=cmd, style="Tiny.TButton", width=3)
            b.pack(side="left", padx=1)
            Tooltip(b, tip)
        ttk.Button(bar, text="Select all", style="Tiny.TButton", command=self.select_all).pack(side="left", padx=(8, 1))
        self.info = ttk.Label(bar, style="Muted.TLabel")
        self.info.pack(side="right")
        ttk.Label(bar, text="Zoom").pack(side="right", padx=(0, 2))
        scale = ttk.Scale(bar, from_=80, to=300, value=self.size, length=100,
                          command=lambda v: self.set_size(int(float(v))))
        scale.pack(side="right", padx=4)

    # ------------------------------------------------------------------ items
    def set_items(self, items: list[ThumbItem]) -> None:
        for it in items:
            if not it.uid:
                self._uid += 1
                it.uid = self._uid
        self.items = items
        self.redraw()

    def selected(self) -> list[ThumbItem]:
        return [i for i in self.items if i.selected]

    def set_size(self, size: int) -> None:
        self.size = size
        self.redraw()

    def _changed(self) -> None:
        self.redraw()
        if self.on_change:
            self.on_change()

    def select_all(self) -> str:
        for i in self.items:
            i.selected = True
        self.redraw()
        self._notify_select()
        return "break"

    def _notify_select(self) -> None:
        if self.on_select:
            self.on_select(self.selected())

    def rotate_selected(self, deg: int) -> None:
        for it in self.selected() or []:
            it.rotation = (it.rotation + deg) % 360
        if self.selected():
            self._changed()

    def delete_selected(self) -> None:
        if self.selected():
            self.items = [i for i in self.items if not i.selected]
            self._changed()

    def move_selected(self, step: int) -> None:
        sel = [i for i, it in enumerate(self.items) if it.selected]
        if not sel:
            return
        order = sel if step > 0 else sel[::-1]
        order = order[::-1] if step > 0 else order
        for idx in order:
            j = idx + step
            if 0 <= j < len(self.items) and not self.items[j].selected:
                self.items[idx], self.items[j] = self.items[j], self.items[idx]
        self._changed()

    # ------------------------------------------------------------------ drawing
    def redraw(self) -> None:
        c, p = self.canvas, self.app.theme.p
        c.delete("all")
        cw = max(self.canvas.winfo_width(), 200)
        cell_w = self.size + self.PAD * 2
        cell_h = int(self.size * 1.42) + 34
        cols = max(1, cw // cell_w)
        self._pos = []
        for n, it in enumerate(self.items):
            col, row = n % cols, n // cols
            x, y = col * cell_w + self.PAD, row * cell_h + self.PAD
            self._pos.append((x, y, cell_w - self.PAD, cell_h - self.PAD))
            outline = p["accent"] if it.selected else p["line"]
            c.create_rectangle(x - 4, y - 4, x + self.size + 4, y + cell_h - self.PAD - 2,
                               fill=p["accent_soft"] if it.selected else p["surface"], outline=outline,
                               width=2 if it.selected else 1, tags=("cell", f"c{n}"))
            key = (it.path, it.page, it.rotation, self.size, tuple(sorted((it.fix or {}).items())))
            photo = self._photos.get(key)
            if photo is not None:
                c.create_image(x + self.size // 2, y + int(self.size * 1.42) // 2, image=photo, tags=f"c{n}")
            else:
                c.create_text(x + self.size // 2, y + self.size // 2, text="…", fill=p["muted"], tags=f"c{n}")
                self._request(key, it)
            label = it.label or f"{it.path.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]} p.{it.page + 1}"
            c.create_text(x + self.size // 2, y + int(self.size * 1.42) + 14, text=f"{n + 1}. {label}"[:34],
                          fill=p["fg"], width=self.size, tags=f"c{n}")
            if it.badge:
                c.create_text(x + 6, y + 6, anchor="nw", text=it.badge[:18], fill=p["accent_fg"], tags=f"c{n}")
                bb = c.bbox(c.find_withtag("current")) if False else None
            if it.rotation:
                c.create_text(x + self.size - 4, y + 4, anchor="ne", text=f"↻{it.rotation}°", fill=p["warn"], tags=f"c{n}")
        rows = (len(self.items) + cols - 1) // cols
        c.configure(scrollregion=(0, 0, cw, rows * cell_h + self.PAD))
        if hasattr(self, "info"):
            self.info.configure(text=f"{len(self.items)} pages • {len(self.selected())} selected")

    def _request(self, key: tuple, it: ThumbItem) -> None:
        if key in self._pending:
            return
        self._pending.add(key)

        def work() -> None:
            try:
                img = self.app.renderer.thumbnail(it.path, it.page, self.size, it.rotation, it.fix)
            except Exception:
                img = None
            self.app.bridge.call(self._ready, key, img)

        threading.Thread(target=work, daemon=True).start()

    def _ready(self, key: tuple, img) -> None:
        self._pending.discard(key)
        if img is None:
            return
        self._photos[key] = ImageTk.PhotoImage(img)
        if len(self._photos) > 400:
            self._photos.pop(next(iter(self._photos)))
        if self.winfo_exists():
            self.redraw()

    # ------------------------------------------------------------------ mouse
    def _hit(self, x: int, y: int) -> int:
        x, y = self.canvas.canvasx(x), self.canvas.canvasy(y)
        for n, (px, py, w, h) in enumerate(self._pos):
            if px - 4 <= x <= px + w and py - 4 <= y <= py + h:
                return n
        return -1

    def _press(self, e) -> None:
        self.canvas.focus_set()
        n = self._hit(e.x, e.y)
        ctrl, shift = bool(e.state & 4), bool(e.state & 1)
        if n < 0:
            if not ctrl:
                for i in self.items:
                    i.selected = False
            self.redraw()
            self._notify_select()
            return
        if shift:
            lo, hi = sorted((self._anchor, n))
            for i, it in enumerate(self.items):
                it.selected = lo <= i <= hi or (ctrl and it.selected)
        elif ctrl:
            self.items[n].selected = not self.items[n].selected
            self._anchor = n
        else:
            if not self.items[n].selected:
                for i in self.items:
                    i.selected = False
                self.items[n].selected = True
            self._anchor = n
        self._drag = {"n": n, "x": e.x, "y": e.y, "moved": False}
        self.redraw()
        self._notify_select()

    def _motion(self, e) -> None:
        d = self._drag
        if not d:
            return
        if not d["moved"] and abs(e.x - d["x"]) + abs(e.y - d["y"]) > 8:
            d["moved"] = True
        if d["moved"]:
            self.canvas.delete("dropline")
            t = self._drop_index(e.x, e.y)
            if t is not None and self._pos:
                t2 = min(t, len(self._pos) - 1)
                x, y, w, h = self._pos[t2]
                lx = x - 7 if t <= t2 else x + self.size + 7
                self.canvas.create_line(lx, y - 4, lx, y + h, fill=self.app.theme.p["accent"], width=3, tags="dropline")

    def _drop_index(self, x: int, y: int) -> int | None:
        n = self._hit(x, y)
        if n < 0:
            return len(self.items) if self.items else None
        px, py, w, h = self._pos[n]
        return n if self.canvas.canvasx(x) < px + self.size / 2 else n + 1

    def _release(self, e) -> None:
        d, self._drag = self._drag, None
        self.canvas.delete("dropline")
        if not d or not d["moved"]:
            return
        target = self._drop_index(e.x, e.y)
        if target is None:
            return
        moving = [i for i in self.items if i.selected]
        before = sum(1 for i, it in enumerate(self.items) if it.selected and i < target)
        rest = [i for i in self.items if not i.selected]
        at = target - before
        self.items = rest[:at] + moving + rest[at:]
        self._changed()

    def _double(self, e) -> None:
        n = self._hit(e.x, e.y)
        if n >= 0 and self.on_open:
            self.on_open(self.items[n])
