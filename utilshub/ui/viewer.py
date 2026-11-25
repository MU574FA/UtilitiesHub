"""Page viewer: scrolling pages of a PDF / image / Office file with zoom, fit modes and manual rotation.

    v = PageViewer(parent, app)
    v.show("scan.pdf", rotations={0: 90})        # manual extra clockwise rotation per page
    v.on_rotate = lambda page, total_deg: ...    # called when the user turns a page
"""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable

from PIL import ImageTk

from ..core.render import PreviewUnavailable
from ..core.system import open_path
from .widgets import Tooltip


class PageViewer(ttk.Frame):
    GAP = 14

    def __init__(self, parent: tk.Misc, app, show_toolbar: bool = True, max_pages: int | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.renderer = app.renderer
        self.max_pages = max_pages
        self.path: Path | None = None
        self.rotations: dict[int, int] = {}
        self.fix: dict | None = None
        self.pages = 0
        self.total_pages = 0
        self.mode = app.settings.get("preview.default_zoom", "width")      # width | page | percent number
        self.zoom = 1.0
        self.on_rotate: Callable[[int, int], None] | None = None
        self.on_message: Callable[[str], None] | None = None
        self._sizes: list[tuple[float, float]] = []
        self._layout: list[tuple[int, int, int, int]] = []      # per page: y, height, width, x
        self._images: dict[int, ImageTk.PhotoImage] = {}
        self._keys: dict[int, tuple] = {}
        self._pending: set[tuple] = set()
        self._token = 0
        self._after = None
        self._error = ""
        self.section_headers: dict[int, tuple[str, str]] = {}
        self.excluded_pages: set[int] = set()
        if show_toolbar:
            self._toolbar()
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, highlightthickness=0)
        ys = ttk.Scrollbar(body, orient="vertical", command=self._yview)
        xs = ttk.Scrollbar(body, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.canvas.pack(fill="both", expand=True)
        app.theme.style_canvas(self.canvas)
        self.canvas.bind("<Configure>", lambda e: self._schedule_relayout())
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", self._wheel)
        self.canvas.bind("<Button-5>", self._wheel)
        self.canvas.bind("<ButtonPress-1>", lambda e: (self.canvas.focus_set(), self.canvas.scan_mark(e.x, e.y)))
        self.canvas.bind("<B1-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set(), add="+")
        for seq, fn in (("<Control-plus>", lambda e: self.zoom_by(1.2)), ("<Control-equal>", lambda e: self.zoom_by(1.2)),
                        ("<Control-minus>", lambda e: self.zoom_by(1 / 1.2)), ("<Control-0>", lambda e: self.fit("width")),
                        ("<Prior>", lambda e: self.canvas.yview_scroll(-1, "pages")),
                        ("<Next>", lambda e: self.canvas.yview_scroll(1, "pages")),
                        ("<Home>", lambda e: self.canvas.yview_moveto(0)), ("<End>", lambda e: self.canvas.yview_moveto(1))):
            self.canvas.bind(seq, fn)

    # ------------------------------------------------------------------ toolbar
    def _toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        def b(text, cmd, tip):
            btn = ttk.Button(bar, text=text, command=cmd, style="Tiny.TButton", width=3)
            btn.pack(side="left", padx=1)
            Tooltip(btn, tip)
            return btn
        b("◀", lambda: self.goto(self.current_page() - 1), "Previous page")
        self.page_var = tk.StringVar(value="–")
        ttk.Label(bar, textvariable=self.page_var, width=9, anchor="center").pack(side="left")
        b("▶", lambda: self.goto(self.current_page() + 1), "Next page")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        b("−", lambda: self.zoom_by(1 / 1.2), "Zoom out (Ctrl+-)")
        self.zoom_var = tk.StringVar(value="Fit")
        ttk.Label(bar, textvariable=self.zoom_var, width=6, anchor="center").pack(side="left")
        b("+", lambda: self.zoom_by(1.2), "Zoom in (Ctrl++)")
        ttk.Button(bar, text="Fit width", style="Tiny.TButton", command=lambda: self.fit("width")).pack(side="left", padx=(4, 1))
        ttk.Button(bar, text="Fit page", style="Tiny.TButton", command=lambda: self.fit("page")).pack(side="left", padx=1)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        b("⟲", lambda: self.rotate_page(-90), "Rotate this page left")
        b("⟳", lambda: self.rotate_page(90), "Rotate this page right")
        ttk.Button(bar, text="All ⟳", style="Tiny.TButton", command=lambda: self.rotate_all(90)).pack(side="left", padx=1)
        ttk.Button(bar, text="Open", style="Tiny.TButton",
                   command=lambda: self.path and open_path(self.path)).pack(side="right")

    # ------------------------------------------------------------------ public API
    def clear(self, message: str = "") -> None:
        self._token += 1
        self.path = None
        self.pages = 0
        self._images.clear()
        self._keys.clear()
        self._layout = []
        self.canvas.delete("all")
        if message:
            self.canvas.create_text(20, 20, anchor="nw", text=message, width=480,
                                    fill=self.app.theme.p["muted"], tags="msg")
        if hasattr(self, "page_var"):
            self.page_var.set("–")

    def show(self, path: str | Path, rotations: dict[int, int] | None = None, fix: dict | None = None) -> None:
        self.clear("Rendering preview…")
        self.path, self.rotations, self.fix = Path(path), dict(rotations or {}), fix
        token = self._token

        def work() -> None:
            try:
                total = self.renderer.page_count(self.path)
                sizes = [self.renderer.page_size(self.path, i) for i in range(min(total, self.max_pages or 10 ** 6))]
                self.app.bridge.call(self._loaded, token, total, sizes)
            except PreviewUnavailable as exc:
                self.app.bridge.call(self._failed, token, str(exc))
            except Exception as exc:
                self.app.bridge.call(self._failed, token, f"Preview failed: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def _failed(self, token: int, message: str) -> None:
        if token == self._token:
            self.clear(message)
            self._error = message
            if self.on_message:
                self.on_message(message)

    def _loaded(self, token: int, total: int, sizes: list) -> None:
        if token != self._token:
            return
        self.canvas.delete("msg")
        self.total_pages, self.pages, self._sizes = total, len(sizes), sizes
        self._relayout()

    # ------------------------------------------------------------------ geometry
    def _page_dims(self, i: int, width: int) -> tuple[int, int]:
        w0, h0 = self._sizes[i]
        if self.rotations.get(i, 0) % 180:
            w0, h0 = h0, w0
        if (self.fix or {}).get("rot", 0) % 180:
            w0, h0 = h0, w0
        return width, max(1, round(width * h0 / max(1, w0)))

    def _target_width(self) -> int:
        cw = max(200, self.canvas.winfo_width() - 24)
        if self.mode == "width":
            return cw
        if self.mode == "page" and self._sizes:
            ch = max(200, self.canvas.winfo_height() - 20)
            w0, h0 = self._page_dims(self.current_page() if self._layout else 0, 1000)
            return max(150, min(cw, int(ch * 1000 / h0)))
        base = float(self.mode) if not isinstance(self.mode, str) else 1.0
        return max(100, int(cw * base))

    def _schedule_relayout(self) -> None:
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(120, self._relayout)

    def set_section_headers(self, headers: dict[int, tuple[str, str]] | None) -> None:
        self.section_headers = dict(headers or {})
        if self.pages:
            self._relayout()

    def set_excluded_pages(self, excluded: set[int] | None) -> None:
        self.excluded_pages = set(excluded or ())
        self._draw_excluded_overlays()

    def _relayout(self) -> None:
        self._after = None
        if not self.pages:
            return
        width = self._target_width()
        cw = max(self.canvas.winfo_width(), width + 24)
        self._layout, y = [], self.GAP
        self.canvas.delete("page")
        p = self.app.theme.p
        batch_gap, banner_h = 26, 26
        for i in range(self.pages):
            w, h = self._page_dims(i, width)
            x = (cw - w) // 2
            if i in self.section_headers:
                if i > 0:
                    y += batch_gap
                    div_y = y - batch_gap // 2
                    self.canvas.create_line(
                        max(12, x - 6), div_y, min(cw - 12, x + w + 6), div_y,
                        fill=p["line"], dash=(4, 3), tags=("page", "banner"),
                    )
                title, badge_txt = self.section_headers[i]
                self.canvas.create_rectangle(
                    x, y, x + w, y + banner_h,
                    fill=p.get("accent_soft", p["surface"]), outline=self.app.theme.accent, width=1,
                    tags=("page", "banner"),
                )
                self.canvas.create_rectangle(
                    x, y, x + 4, y + banner_h,
                    fill=self.app.theme.accent, outline="", tags=("page", "banner"),
                )
                max_chars = max(16, int((w - 90) / 7))
                short_title = title if len(title) <= max_chars else title[:max_chars - 1] + "…"
                self.canvas.create_text(
                    x + 12, y + banner_h // 2, anchor="w", text=short_title,
                    fill=p["fg"], font=("TkDefaultFont", 9, "bold"), tags=("page", "banner"),
                )
                if badge_txt:
                    self.canvas.create_text(
                        x + w - 10, y + banner_h // 2, anchor="e", text=badge_txt,
                        fill=self.app.theme.accent, font=("TkDefaultFont", 8, "bold"), tags=("page", "banner"),
                    )
                y += banner_h + 6
            self._layout.append((y, h, w, x))
            self.canvas.create_rectangle(x, y, x + w, y + h, fill="#ffffff", outline=p["line"],
                                         tags=("page", f"ph{i}"))
            img = self._images.get(i)
            if img is not None and self._keys.get(i) == self._key(i, width):
                self.canvas.create_image(x, y, anchor="nw", image=img, tags=("page", f"im{i}"))
            y += h + self.GAP
        if self.total_pages > self.pages:
            self.canvas.create_text(cw // 2, y, text=f"… first {self.pages} of {self.total_pages} pages shown",
                                    fill=p["muted"], tags="page")
            y += 30
        self._draw_excluded_overlays()
        self.canvas.configure(scrollregion=(0, 0, cw, y))
        if hasattr(self, "zoom_var"):
            self.zoom_var.set("Fit" if self.mode in ("width", "page") else f"{int(float(self.mode) * 100)}%")
        self._request_visible()
        self._update_page_label()

    def _draw_excluded_overlays(self) -> None:
        self.canvas.delete("excl")
        if not self._layout or not self.excluded_pages:
            return
        p = self.app.theme.p
        err = p["err"]
        for i in sorted(self.excluded_pages):
            if not (0 <= i < len(self._layout)):
                continue
            y, h, w, x = self._layout[i]
            tag = ("page", "excl", f"excl{i}")
            self.canvas.create_rectangle(x, y, x + w, y + h, outline=err, width=2, tags=tag)
            self.canvas.create_line(x + 8, y + 8, x + w - 8, y + h - 8, fill=err, width=2, dash=(6, 6), tags=tag)
            self.canvas.create_line(x + w - 8, y + 8, x + 8, y + h - 8, fill=err, width=2, dash=(6, 6), tags=tag)
            pill_w = min(104, max(60, w - 16))
            px0 = x + w - pill_w - 8
            py0 = y + 8
            self.canvas.create_rectangle(px0, py0, px0 + pill_w, py0 + 22, fill=err, outline="", tags=tag)
            self.canvas.create_text(px0 + pill_w // 2, py0 + 11, text="⊘ EXCLUDED",
                                    fill="#ffffff", font=("TkDefaultFont", 8, "bold"), tags=tag)

    def _key(self, i: int, width: int) -> tuple:
        return (i, width, self.rotations.get(i, 0) % 360)

    def current_page(self) -> int:
        if not self._layout:
            return 0
        top = self.canvas.canvasy(self.canvas.winfo_height() * 0.3)
        for i, (y, h, *_rest) in enumerate(self._layout):
            if top < y + h + self.GAP / 2:
                return i
        return len(self._layout) - 1

    def _update_page_label(self) -> None:
        if hasattr(self, "page_var") and self.pages:
            self.page_var.set(f"{self.current_page() + 1} / {self.total_pages}")

    def goto(self, page: int) -> None:
        if not self._layout:
            return
        page = max(0, min(page, len(self._layout) - 1))
        extra = 42 if page in self.section_headers else self.GAP
        y = self._layout[page][0] - extra
        region = self.canvas.bbox("all")
        self.canvas.yview_moveto(max(0, y) / max(1, region[3] if region else 1))
        self._yview_done()

    # ------------------------------------------------------------------ rendering on demand
    def _yview(self, *args) -> None:
        self.canvas.yview(*args)
        self._yview_done()

    def _yview_done(self) -> None:
        self._update_page_label()
        self._request_visible()

    def _wheel(self, e) -> str:
        up = e.delta > 0 or getattr(e, "num", 0) == 4
        if e.state & 4:                                    # Ctrl + wheel = zoom
            self.zoom_by(1.15 if up else 1 / 1.15)
        else:
            self.canvas.yview_scroll(-3 if up else 3, "units")
            self._yview_done()
        return "break"

    def _request_visible(self) -> None:
        if not self._layout:
            return
        top = self.canvas.canvasy(0)
        bottom = self.canvas.canvasy(self.canvas.winfo_height())
        width = self._layout[0][2]
        token = self._token
        for i, (y, h, w, x) in enumerate(self._layout):
            if y + h < top - 400 or y > bottom + 400:
                continue
            key = self._key(i, width)
            if self._keys.get(i) == key or key in self._pending:
                continue
            self._pending.add(key)
            threading.Thread(target=self._render_page, args=(token, i, width, key), daemon=True).start()

    def _render_page(self, token: int, i: int, width: int, key: tuple) -> None:
        try:
            img = self.renderer.render(self.path, i, width, self.rotations.get(i, 0), self.fix)
            self.app.bridge.call(self._rendered, token, i, key, img)
        except Exception as exc:
            self.app.bridge.call(self._pending.discard, key)
            self.app.bridge.call(self._failed_page, token, i, str(exc))

    def _failed_page(self, token: int, i: int, msg: str) -> None:
        if token == self._token and i < len(self._layout):
            y, h, w, x = self._layout[i]
            self.canvas.create_text(x + 10, y + 10, anchor="nw", text=msg[:120], tags="page", width=w - 20)

    def _rendered(self, token: int, i: int, key: tuple, img) -> None:
        self._pending.discard(key)
        if token != self._token or not self._layout or i >= len(self._layout) or self._layout[0][2] != key[1]:
            return
        photo = ImageTk.PhotoImage(img)
        self._images[i], self._keys[i] = photo, key
        y, h, w, x = self._layout[i]
        self.canvas.delete(f"im{i}")
        self.canvas.create_image(x, y, anchor="nw", image=photo, tags=("page", f"im{i}"))
        if i in self.excluded_pages:
            self.canvas.tag_raise(f"excl{i}")

    # ------------------------------------------------------------------ zoom / rotate
    def fit(self, mode: str) -> None:
        self.mode = mode
        self._relayout()

    def zoom_by(self, factor: float) -> None:
        cur = 1.0 if isinstance(self.mode, str) else float(self.mode)
        self.mode = max(0.25, min(5.0, cur * factor))
        self._relayout()

    def rotate_page(self, degrees: int, page: int | None = None) -> None:
        if not self.pages:
            return
        page = self.current_page() if page is None else page
        total = (self.rotations.get(page, 0) + degrees) % 360
        if total:
            self.rotations[page] = total
        else:
            self.rotations.pop(page, None)
        self._relayout()
        if self.on_rotate:
            self.on_rotate(page, total)

    def rotate_all(self, degrees: int) -> None:
        for p in range(self.total_pages or self.pages):
            total = (self.rotations.get(p, 0) + degrees) % 360
            if total:
                self.rotations[p] = total
            else:
                self.rotations.pop(p, None)
            if self.on_rotate:
                self.on_rotate(p, total)
        self._relayout()
