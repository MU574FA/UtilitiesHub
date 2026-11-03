"""Theme manager: light / dark / system, accent colour, fonts, density and UI scale.

All ttk styles are rebuilt from one palette; classic Tk widgets (Text, Canvas, Listbox, Menu) are
recoloured through `theme.style_text(widget)` etc. and registered so a live theme switch updates them.
"""
from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from ..core.system import system_prefers_dark

LIGHT = dict(bg="#f3f4f6", surface="#ffffff", surface2="#eceef2", fg="#1c2128", muted="#667085", line="#d0d5dd",
             input="#ffffff", ok="#157f3b", warn="#b45309", err="#b42318", info="#1d4ed8", sel_fg="#ffffff",
             canvas="#e4e7ec", hover="#e6e9ee")
DARK = dict(bg="#16191e", surface="#1f242b", surface2="#272d35", fg="#e6e9ee", muted="#98a2b3", line="#363d47",
            input="#171b20", ok="#4ade80", warn="#fbbf24", err="#f87171", info="#60a5fa", sel_fg="#0b1220",
            canvas="#101317", hover="#2d343d")
ACCENTS = {"Blue": "#2563eb", "Teal": "#0d9488", "Green": "#16a34a", "Purple": "#7c3aed", "Orange": "#ea580c",
           "Rose": "#e11d48", "Slate": "#475569"}
DENSITY_PAD = {"compact": 2, "normal": 4, "comfortable": 7}
DENSITY_ROW = {"compact": 22, "normal": 26, "comfortable": 32}


def _mix(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


class ThemeManager:
    def __init__(self, root: tk.Tk, settings) -> None:
        self.root, self.settings = root, settings
        self.p: dict[str, str] = dict(LIGHT)
        self.dark = False
        self._text_widgets: list[tk.Misc] = []
        self._canvas_widgets: list[tk.Canvas] = []
        self._callbacks: list = []
        self.style = ttk.Style(root)
        settings.subscribe("ui.", lambda k, v: self._deferred())
        self._after = None

    # ------------------------------------------------------------------ public
    def on_change(self, cb) -> None:
        self._callbacks.append(cb)

    def _deferred(self) -> None:
        if self._after:
            self.root.after_cancel(self._after)
        self._after = self.root.after(120, self.apply)

    def color(self, name: str) -> str:
        return self.p[name]

    def level_color(self, level: str) -> str:
        return {"ok": self.p["ok"], "fail": self.p["err"], "error": self.p["err"], "warn": self.p["warn"],
                "info": self.p["info"], "muted": self.p["muted"]}.get(level, self.p["fg"])

    def style_text(self, w: tk.Text, canvas_bg: bool = False) -> None:
        if w not in self._text_widgets:
            self._text_widgets.append(w)
        self._paint_text(w)

    def style_canvas(self, c: tk.Canvas) -> None:
        if c not in self._canvas_widgets:
            self._canvas_widgets.append(c)
        c.configure(background=self.p["canvas"], highlightthickness=0)

    def style_listbox(self, lb: tk.Listbox) -> None:
        if lb not in self._text_widgets:
            self._text_widgets.append(lb)
        self._paint_text(lb)

    def _paint_text(self, w) -> None:
        try:
            if isinstance(w, tk.Listbox):
                w.configure(background=self.p["input"], foreground=self.p["fg"], selectbackground=self.accent,
                            selectforeground=self.p["sel_fg"], highlightthickness=1, highlightbackground=self.p["line"],
                            relief="flat", activestyle="none")
            else:
                w.configure(background=self.p["input"], foreground=self.p["fg"], insertbackground=self.p["fg"],
                            selectbackground=self.accent, selectforeground=self.p["sel_fg"],
                            highlightthickness=1, highlightbackground=self.p["line"], highlightcolor=self.accent,
                            relief="flat", padx=6, pady=4, inactiveselectbackground=_mix(self.accent, self.p["input"], .6))
                for tag, level in (("ok", "ok"), ("warn", "warn"), ("error", "err"), ("err", "err"),
                                   ("info", "fg"), ("muted", "muted"), ("fail", "err")):
                    if tag in w.tag_names():
                        w.tag_configure(tag, foreground=self.p[level])
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ apply
    @property
    def accent(self) -> str:
        name = self.settings.get("ui.accent", "Blue")
        return ACCENTS.get(name, name if str(name).startswith("#") else ACCENTS["Blue"])

    def resolve_mode(self) -> bool:
        mode = self.settings.get("ui.theme", "system")
        return system_prefers_dark() if mode == "system" else mode == "dark"

    def apply(self) -> None:
        self._after = None
        s, g = self.settings, self.settings.get
        self.dark = self.resolve_mode()
        p = dict(DARK if self.dark else LIGHT)
        if g("ui.high_contrast", False):
            p.update(fg="#ffffff" if self.dark else "#000000", line=p["muted"])
        acc = self.accent
        p["accent"], p["accent_fg"] = acc, ("#0b1220" if self.dark and sum(int(acc[i:i + 2], 16) for i in (1, 3, 5)) > 450 else "#ffffff")
        p["accent_soft"] = _mix(acc, p["surface"], .82 if not self.dark else .72)
        p["accent_hover"] = _mix(acc, "#000000" if not self.dark else "#ffffff", .12)
        p["sel_fg"] = p["accent_fg"]
        for key in ("ok", "warn", "err"):
            custom = g(f"ui.color_{key}", "")
            if custom:
                p[key] = custom
        self.p = p

        root, st = self.root, self.style
        size = int(g("ui.font_size", 10))
        family = g("ui.font_family", "") or self._default_family()
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(family=family, size=size)
            except tk.TclError:
                pass
        tkfont.nametofont("TkFixedFont").configure(size=size)
        try:
            root.tk.call("tk", "scaling", float(g("ui.scale", 1.0)) * root.winfo_fpixels("1i") / 72.0)
        except tk.TclError:
            pass
        pad = DENSITY_PAD.get(g("ui.density", "normal"), 4)
        rowh = int(DENSITY_ROW.get(g("ui.density", "normal"), 26) * float(g("ui.scale", 1.0)) * size / 10)

        st.theme_use("clam")
        root.configure(background=p["bg"])
        base = (family, size)
        bold = (family, size, "bold")
        st.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["input"], font=base,
                     bordercolor=p["line"], lightcolor=p["line"], darkcolor=p["line"], troughcolor=p["surface2"],
                     focuscolor=acc, selectbackground=acc, selectforeground=p["accent_fg"], insertcolor=p["fg"])
        st.configure("TFrame", background=p["bg"])
        st.configure("Card.TFrame", background=p["surface"], relief="solid", borderwidth=1, bordercolor=p["line"])
        st.configure("Surface.TFrame", background=p["surface"])
        st.configure("Side.TFrame", background=p["surface"])
        st.configure("TLabel", background=p["bg"], foreground=p["fg"])
        st.configure("Surface.TLabel", background=p["surface"])
        st.configure("Muted.TLabel", foreground=p["muted"])
        st.configure("Title.TLabel", font=(family, size + 8, "bold"))
        st.configure("H2.TLabel", font=(family, size + 2, "bold"))
        st.configure("H3.TLabel", font=bold)
        for name, key in (("Ok", "ok"), ("Warn", "warn"), ("Err", "err"), ("Info", "info")):
            st.configure(f"{name}.TLabel", foreground=p[key], font=bold)
        st.configure("Badge.TLabel", background=p["accent_soft"], foreground=acc, padding=(6, 1), font=(family, size - 1, "bold"))
        st.configure("SideTitle.TLabel", background=p["surface"], foreground=p["muted"], font=(family, size - 1, "bold"))
        st.configure("TLabelframe", background=p["bg"], bordercolor=p["line"], relief="solid", borderwidth=1)
        st.configure("TLabelframe.Label", background=p["bg"], foreground=p["muted"], font=bold)
        st.configure("TButton", background=p["surface2"], foreground=p["fg"], bordercolor=p["line"], padding=(10, pad),
                     relief="flat", borderwidth=1, focusthickness=1, focuscolor=acc)
        st.map("TButton", background=[("disabled", p["surface"]), ("pressed", p["line"]), ("active", p["hover"])],
               foreground=[("disabled", p["muted"])], bordercolor=[("focus", acc)])
        st.configure("Accent.TButton", background=acc, foreground=p["accent_fg"], bordercolor=acc, font=bold)
        st.map("Accent.TButton", background=[("disabled", p["line"]), ("pressed", p["accent_hover"]), ("active", p["accent_hover"])],
               foreground=[("disabled", p["muted"])])
        st.configure("Danger.TButton", background=p["err"], foreground="#ffffff", bordercolor=p["err"])
        st.map("Danger.TButton", background=[("active", _mix(p["err"], "#000000", .15))])
        st.configure("Flat.TButton", background=p["bg"], bordercolor=p["bg"], padding=(8, pad))
        st.map("Flat.TButton", background=[("active", p["hover"])])
        st.configure("Tiny.TButton", padding=(4, 1))
        st.configure("Side.TButton", background=p["surface"], bordercolor=p["surface"], anchor="w", padding=(12, pad + 3),
                     font=base, relief="flat")
        st.map("Side.TButton", background=[("active", p["hover"])])
        st.configure("SideSel.TButton", background=p["accent_soft"], foreground=acc, bordercolor=p["accent_soft"],
                     anchor="w", padding=(12, pad + 3), font=bold)
        st.map("SideSel.TButton", background=[("active", p["accent_soft"])])
        st.configure("TEntry", fieldbackground=p["input"], padding=(6, pad), bordercolor=p["line"])
        st.map("TEntry", bordercolor=[("focus", acc)], fieldbackground=[("readonly", p["surface2"]), ("disabled", p["surface2"])])
        st.configure("TCombobox", fieldbackground=p["input"], padding=(6, pad), arrowcolor=p["fg"], background=p["surface2"])
        st.map("TCombobox", fieldbackground=[("readonly", p["input"])], bordercolor=[("focus", acc)],
               selectbackground=[("readonly", p["input"])], selectforeground=[("readonly", p["fg"])])
        root.option_add("*TCombobox*Listbox.background", p["input"])
        root.option_add("*TCombobox*Listbox.foreground", p["fg"])
        root.option_add("*TCombobox*Listbox.selectBackground", acc)
        root.option_add("*TCombobox*Listbox.selectForeground", p["accent_fg"])
        root.option_add("*Menu.background", p["surface"])
        root.option_add("*Menu.foreground", p["fg"])
        root.option_add("*Menu.activeBackground", acc)
        root.option_add("*Menu.activeForeground", p["accent_fg"])
        st.configure("TSpinbox", fieldbackground=p["input"], padding=(6, pad), arrowcolor=p["fg"], background=p["surface2"])
        st.configure("TCheckbutton", background=p["bg"], indicatorbackground=p["input"], indicatorcolor=p["input"])
        st.map("TCheckbutton", indicatorcolor=[("selected", acc), ("pressed", acc)], background=[("active", p["bg"])])
        st.configure("Surface.TCheckbutton", background=p["surface"])
        st.configure("TRadiobutton", background=p["bg"], indicatorbackground=p["input"])
        st.map("TRadiobutton", indicatorcolor=[("selected", acc)], background=[("active", p["bg"])])
        st.configure("TNotebook", background=p["bg"], bordercolor=p["line"], tabmargins=(0, 2, 0, 0))
        st.configure("TNotebook.Tab", background=p["surface2"], foreground=p["muted"], padding=(14, pad + 2), bordercolor=p["line"])
        st.map("TNotebook.Tab", background=[("selected", p["surface"])], foreground=[("selected", acc)])
        st.configure("Treeview", background=p["input"], fieldbackground=p["input"], foreground=p["fg"], rowheight=rowh,
                     bordercolor=p["line"], borderwidth=1)
        st.map("Treeview", background=[("selected", acc)], foreground=[("selected", p["accent_fg"])])
        st.configure("Treeview.Heading", background=p["surface2"], foreground=p["muted"], padding=(6, pad), font=bold,
                     relief="flat", bordercolor=p["line"])
        st.map("Treeview.Heading", background=[("active", p["hover"])])
        for orient in ("Horizontal", "Vertical"):
            st.configure(f"{orient}.TScrollbar", background=p["surface2"], troughcolor=p["bg"], bordercolor=p["bg"],
                         arrowcolor=p["muted"], relief="flat")
            st.map(f"{orient}.TScrollbar", background=[("active", p["line"])])
        st.configure("TProgressbar", background=acc, troughcolor=p["surface2"], bordercolor=p["line"], thickness=8)
        st.configure("Horizontal.TScale", background=p["bg"], troughcolor=p["surface2"])
        st.configure("TPanedwindow", background=p["bg"])
        st.configure("Sash", sashthickness=6, background=p["line"])
        st.configure("TSeparator", background=p["line"])
        st.configure("Status.TFrame", background=p["surface"])
        st.configure("Status.TLabel", background=p["surface"], foreground=p["muted"])

        for w in list(self._text_widgets):
            if w.winfo_exists():
                self._paint_text(w)
            else:
                self._text_widgets.remove(w)
        for c in list(self._canvas_widgets):
            if c.winfo_exists():
                c.configure(background=p["canvas"])
            else:
                self._canvas_widgets.remove(c)
        for cb in list(self._callbacks):
            try:
                cb()
            except Exception:
                pass

    @staticmethod
    def _default_family() -> str:
        if sys.platform.startswith("win"):
            return "Segoe UI"
        if sys.platform == "darwin":
            return "Helvetica Neue"
        fams = set(tkfont.families())
        for f in ("Noto Sans", "Cantarell", "Ubuntu", "DejaVu Sans"):
            if f in fams:
                return f
        return tkfont.nametofont("TkDefaultFont").actual("family")
