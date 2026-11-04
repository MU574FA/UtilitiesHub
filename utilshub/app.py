"""The application shell: window, navigation, menus, status bar, tools, automation, logging."""
from __future__ import annotations

import importlib
import logging
import logging.handlers
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk

from . import APP_NAME, __version__
from .core import toolorder
from .core.fileops import Journal
from .core.jobs import JobRunner, UiBridge
from .core.render import FileRenderer
from .core.schema import Opt, Page
from .core.settings import Settings, data_dir
from .core.system import open_path
from .options import all_shared_opts, shared_pages
from .tools.base import Command, Tool
from .ui import dnd
from .ui.palette import CommandPalette
from .ui.settings_ui import SettingsWindow
from .ui.theme import ThemeManager
from .ui.widgets import Toast, confirm, info, tip

TOOL_MODULES = ["home", "cv_sorter", "docpacker", "file_picker", "duplicates", "scan_cleaner", "screenshot",
                "name_matcher", "ocr_match", "passport", "pdf_desk", "sheet_sync", "workflows", "autopilot", "history"]

STATE_OPTS = [
    Opt("window.geometry", "", "Window geometry", "str"),
    Opt("window.last_tool", "home", "Last tool", "str"),
    Opt("window.tool_order", [], "Tool order (ids; set it on the Home & tools page)", "list"),
]


def discover_tool_classes() -> list[type[Tool]]:
    out: list[type[Tool]] = []
    for name in TOOL_MODULES:
        try:
            mod = importlib.import_module(f"{__package__}.tools.{name}")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.endswith(f"tools.{name}"):
                continue                        # tool not installed
            logging.getLogger("app").exception("Tool %s failed to import", name)
            continue
        except Exception:
            logging.getLogger("app").exception("Tool %s failed to import", name)
            continue
        for obj in vars(mod).values():
            if isinstance(obj, type) and issubclass(obj, Tool) and obj is not Tool and obj.__module__ == mod.__name__:
                out.append(obj)
    return sorted(out, key=lambda c: c.order)


class App:
    def __init__(self, settings_path: Path | None = None, root: tk.Tk | None = None) -> None:
        self.tool_classes = discover_tool_classes()
        self.settings = Settings(settings_path)
        self.settings.register(all_shared_opts())
        self.settings.register(STATE_OPTS)
        for cls in self.tool_classes:
            self.settings.register(cls.options())
        self._setup_logging()
        self.root = root or dnd.create_root()
        self.root.app = self
        self.root.title(APP_NAME)
        self.root.minsize(900, 600)
        self.logs: deque = deque(maxlen=500)
        self.last_text = ""
        self.theme = ThemeManager(self.root, self.settings)
        self.theme.apply()
        self.bridge = UiBridge(self.root)
        self.jobs = JobRunner(self.bridge, max_workers=max(2, self.settings.get_int("auto.max_parallel", 2) + 1))
        self.renderer = FileRenderer(self.settings.get("ocr.libreoffice_cmd"), self.settings.get_int("preview.cache_items", 120))
        self.journal = Journal(data_dir() / "journal.jsonl")
        self.toast = Toast(self.root, self.theme)
        self.tools: dict[str, Tool] = {}
        self.current: Tool | None = None
        self._nav_buttons: dict[str, ttk.Button] = {}
        self._nav_after = None
        self._alt_keys: list[str] = []
        self.extra_commands: list[Command] = []
        for cls in self.tool_classes:
            try:
                self.tools[cls.id] = cls(self)
            except Exception:
                logging.getLogger("app").exception("Could not create tool %s", cls.id)
        self._build_shell()
        self._restore_window()
        self._bind_keys()
        self.settings.subscribe("window.", self._window_changed)
        self.settings.subscribe("ocr.libreoffice_cmd", lambda k, v: setattr(self.renderer, "cmd", v))
        self.jobs.on_change(self._job_changed)
        dnd.make_drop_target(self.root, self.on_drop)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        start = self.settings.get("window.last_tool") if self.settings.get("window.startup_tool") == "last" else "home"
        self.show_tool(start if start in self.tools else next(iter(self.tools), ""))
        self.root.after(600, self._start_automation)
        self.root.after(1500, self._prune_logs)

    # ------------------------------------------------------------------ logging
    def _setup_logging(self) -> None:
        root = logging.getLogger()
        if any(getattr(h, "_utilshub", False) for h in root.handlers):
            return
        try:
            h = logging.handlers.RotatingFileHandler(data_dir() / "utilshub.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            h._utilshub = True
            root.addHandler(h)
            root.setLevel(logging.INFO)
        except OSError:
            pass

    def _prune_logs(self) -> None:
        pass                                    # RotatingFileHandler already bounds the size

    def log(self, message: str, level: str = "info", tool: str = "") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append((stamp, level, tool, message))
        logging.getLogger(f"tool.{tool}" if tool else "app").log(
            {"error": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO), message)
        if hasattr(self, "status_var"):
            self.status_var.set(message[:160])
        for cb in list(getattr(self, "_log_listeners", [])):
            try:
                cb(stamp, level, tool, message)
            except Exception:
                pass

    def on_log(self, cb) -> None:
        self.__dict__.setdefault("_log_listeners", []).append(cb)

    def notify(self, message: str, level: str = "info") -> None:
        if self.settings.get_bool("app.toasts", True):
            self.toast.show(message, level)
        if level == "warn" and self.settings.get_bool("app.sound"):
            self.root.bell()

    # ------------------------------------------------------------------ helpers used by tools
    def fuzzy_threshold(self) -> float:
        return self.settings.get_int("match.fuzzy_threshold", 80) / 100.0

    def settings_pages(self) -> list[Page]:
        pages = list(shared_pages())
        for cls in self.tool_classes:
            pages.extend(cls.pages())
        return sorted(pages, key=lambda p: p.order)

    def open_settings(self, page: str | None = None) -> None:
        SettingsWindow.open(self, page)

    def send_to(self, tool_id: str, paths: list[str]) -> None:
        tool = self.tools.get(tool_id)
        if tool:
            self.show_tool(tool_id)
            tool.accept_files(list(paths))

    # ------------------------------------------------------------------ tool order and visibility
    def sorted_tools(self) -> list[Tool]:
        """Every tool in the user's order (Home first); hidden ones included."""
        default = [c.id for c in self.tool_classes if c.id in self.tools]
        ids = toolorder.ordered_ids(default, self.settings.get("window.tool_order"))
        return [self.tools[i] for i in ids]

    def visible_tools(self) -> list[Tool]:
        hidden = set(self.settings.get("window.hidden_tools") or [])
        return [t for t in self.sorted_tools() if t.id not in hidden or t.id == toolorder.PINNED]

    def hidden_tools(self) -> list[Tool]:
        shown = {t.id for t in self.visible_tools()}
        return [t for t in self.sorted_tools() if t.id not in shown]

    def tool_order(self) -> list[str]:
        return [t.id for t in self.sorted_tools()]

    def set_tool_order(self, ids: list[str]) -> None:
        """Save a new order (an empty list = the default order)."""
        self.settings.set("window.tool_order", list(ids))

    def set_tool_hidden(self, tool_id: str, hidden: bool = True) -> None:
        current = self.settings.get("window.hidden_tools") or []
        self.settings.set("window.hidden_tools", toolorder.set_hidden(current, tool_id, hidden))

    def move_tool(self, tool_id: str, delta: int) -> None:
        self.set_tool_order(toolorder.move(self.tool_order(), tool_id, delta))

    def show_all_tools(self) -> None:
        self.settings.set("window.hidden_tools", [])

    def _schedule_nav_refresh(self) -> None:
        if self._nav_after:
            self.root.after_cancel(self._nav_after)
        self._nav_after = self.root.after(60, self._refresh_tool_nav)

    def _refresh_tool_nav(self) -> None:
        """Order / visibility changed: redraw the buttons only - open tools keep their state."""
        self._nav_after = None
        self._populate_nav()
        self._build_menus()
        self._bind_tool_keys()

    # ------------------------------------------------------------------ shell
    def _build_shell(self) -> None:
        r = self.root
        self.menubar = tk.Menu(r)
        r.configure(menu=self.menubar)
        self._build_menus()
        self.outer = ttk.Frame(r)
        self.outer.pack(fill="both", expand=True)
        self.status = ttk.Frame(r, padding=(8, 3))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self.status, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        self.cancel_btn = ttk.Button(self.status, text="Cancel", style="Tiny.TButton", command=self.jobs.cancel_all)
        self.progress = ttk.Progressbar(self.status, length=160, mode="determinate")
        self.job_lbl = ttk.Label(self.status, style="Muted.TLabel")
        self.status.pack(side="bottom", fill="x", before=self.outer)
        if not self.settings.get_bool("window.show_statusbar", True):
            self.status.pack_forget()
        self._build_nav()

    def _build_nav(self) -> None:
        for w in self.outer.winfo_children():
            w.destroy()
        tabs = self.settings.get("window.nav_style") == "tabs"
        self.nav = ttk.Frame(self.outer, style="Side.TFrame")
        if tabs:
            self.nav.pack(side="top", fill="x")
        else:
            self.nav.pack(side="left", fill="y")
        self._populate_nav()
        self.content = ttk.Frame(self.outer)
        self.content.pack(side="left" if not tabs else "top", fill="both", expand=True)
        for t in self.tools.values():
            if t.frame is not None:
                t.frame.pack_forget()
                t.frame = None
                t.built = False
        cur, self.current = self.current, None
        if cur:
            self.show_tool(cur.id)

    def _populate_nav(self) -> None:
        """(Re)create the sidebar / tab buttons inside self.nav."""
        for w in self.nav.winfo_children():
            w.destroy()
        self._nav_buttons.clear()
        tabs = self.settings.get("window.nav_style") == "tabs"
        compact = self.settings.get_bool("window.sidebar_compact")
        if not tabs:
            ttk.Label(self.nav, text="UTILS HUB" if not compact else "UH", style="SideTitle.TLabel").pack(anchor="w", padx=14, pady=(12, 6))
        for t in self.visible_tools():
            text = t.icon if (compact and not tabs) else f"{t.icon}  {t.title}"
            b = ttk.Button(self.nav, text=text, style="Side.TButton", command=lambda i=t.id: self.show_tool(i), width=3 if compact and not tabs else 0)
            b.pack(side="left" if tabs else "top", fill=None if tabs else "x")
            tip(b, t.description or t.title)
            b.bind("<Button-3>", lambda e, i=t.id: self._nav_menu(e, i))
            self._nav_buttons[t.id] = b
        sb = ttk.Button(self.nav, text="⚙" if compact or tabs else "⚙  Settings", style="Side.TButton", command=self.open_settings)
        if tabs:
            sb.pack(side="right")
        else:
            sb.pack(side="bottom", fill="x", pady=(0, 6))
        if self.current and self.current.id in self._nav_buttons:
            self._nav_buttons[self.current.id].configure(style="SideSel.TButton")

    def _nav_menu(self, event, tool_id: str) -> None:
        """Right-click a sidebar button: move it or hide it."""
        m = tk.Menu(self.root, tearoff=False)
        if tool_id != toolorder.PINNED:
            m.add_command(label="Move up" if self.settings.get("window.nav_style") != "tabs" else "Move left",
                          command=lambda: self.move_tool(tool_id, -1))
            m.add_command(label="Move down" if self.settings.get("window.nav_style") != "tabs" else "Move right",
                          command=lambda: self.move_tool(tool_id, 1))
            m.add_command(label="Hide this tool", command=lambda: self.set_tool_hidden(tool_id, True))
            m.add_separator()
        m.add_command(label="Customize tools…", command=lambda: self.open_settings("home"))
        m.tk_popup(event.x_root, event.y_root)

    def show_tool(self, tool_id: str) -> None:
        tool = self.tools.get(tool_id)
        if tool is None:
            return
        if self.current is tool:
            return
        if self.current:
            try:
                self.current.on_hide()
            except Exception:
                logging.getLogger("app").exception("on_hide")
            if self.current.frame:
                self.current.frame.pack_forget()
        try:
            frame = tool.ensure_built(self.content)
        except Exception as exc:
            logging.getLogger("app").exception("Building %s failed", tool_id)
            self.log(f"{tool.title} could not be opened: {exc}", "error")
            return
        frame.pack(fill="both", expand=True)
        self.current = tool
        for i, b in self._nav_buttons.items():
            b.configure(style="SideSel.TButton" if i == tool_id else "Side.TButton")
        self.root.title(f"{tool.title} - {APP_NAME}")
        self.settings.set("window.last_tool", tool_id)
        try:
            tool.on_show()
        except Exception:
            logging.getLogger("app").exception("on_show")

    def _build_menus(self) -> None:
        m = self.menubar
        m.delete(0, "end")
        f = tk.Menu(m, tearoff=False)
        f.add_command(label="Settings…", accelerator="Ctrl+,", command=self.open_settings)
        f.add_command(label="Open data folder", command=lambda: open_path(data_dir()))
        f.add_separator()
        f.add_command(label="Quit", accelerator="Ctrl+Q", command=self.quit)
        m.add_cascade(label="File", menu=f)
        v = tk.Menu(m, tearoff=False)
        for label, key, val in (("Light theme", "ui.theme", "light"), ("Dark theme", "ui.theme", "dark"), ("System theme", "ui.theme", "system")):
            v.add_command(label=label, command=lambda k=key, x=val: self.settings.set(k, x))
        v.add_separator()
        v.add_command(label="Toggle always on top", command=lambda: self.settings.set("window.always_on_top", not self.settings.get_bool("window.always_on_top")))
        v.add_command(label="Toggle status bar", command=lambda: self.settings.set("window.show_statusbar", not self.settings.get_bool("window.show_statusbar", True)))
        v.add_command(label="Toggle sidebar / tabs", command=lambda: self.settings.set("window.nav_style", "tabs" if self.settings.get("window.nav_style") == "sidebar" else "sidebar"))
        v.add_command(label="Toggle full screen", accelerator="F11", command=self.toggle_fullscreen)
        m.add_cascade(label="View", menu=v)
        t = tk.Menu(m, tearoff=False)
        for tool in self.visible_tools():
            t.add_command(label=f"{tool.icon}  {tool.title}", command=lambda i=tool.id: self.show_tool(i))
        hidden = self.hidden_tools()
        if hidden:
            sub = tk.Menu(t, tearoff=False)
            for tool in hidden:
                sub.add_command(label=f"{tool.icon}  {tool.title}", command=lambda i=tool.id: self.show_tool(i))
            sub.add_separator()
            sub.add_command(label="Show all in the sidebar", command=self.show_all_tools)
            t.add_cascade(label=f"Hidden tools ({len(hidden)})", menu=sub)
        t.add_separator()
        t.add_command(label="Customize tools…", command=lambda: self.open_settings("home"))
        t.add_command(label="Start all watchers", command=self.start_all_auto)
        t.add_command(label="Stop all watchers", command=self.stop_all_auto)
        m.add_cascade(label="Tools", menu=t)
        h = tk.Menu(m, tearoff=False)
        h.add_command(label="Command palette", accelerator="Ctrl+K", command=lambda: CommandPalette.show(self))
        h.add_command(label="About", command=lambda: info(self.root, f"{APP_NAME} {__version__}\nData folder: {data_dir()}"))
        m.add_cascade(label="Help", menu=h)

    def _bind_keys(self) -> None:
        r = self.root
        r.bind_all("<Control-k>", lambda e: CommandPalette.show(self))
        r.bind_all("<Control-comma>", lambda e: self.open_settings())
        r.bind_all("<Control-q>", lambda e: self.quit())
        r.bind_all("<F11>", lambda e: self.toggle_fullscreen())
        self._bind_tool_keys()
        for seq in sorted({s for t in self.tools.values() for s in t.keys()}):    # only fire while that tool is shown
            r.bind_all(seq, lambda e, s=seq: self._tool_key(s), add="+")

    def _bind_tool_keys(self) -> None:
        """Alt+1..9 open the first nine *visible* tools, in the current order."""
        for seq in self._alt_keys:
            self.root.unbind_all(seq)
        self._alt_keys = []
        for n, tool in enumerate(self.visible_tools()[:9], 1):
            seq = f"<Alt-Key-{n}>"
            self.root.bind_all(seq, lambda e, i=tool.id: self.show_tool(i))
            self._alt_keys.append(seq)

    def _tool_key(self, seq: str):
        tool = self.current
        fn = tool.keys().get(seq) if tool else None
        try:
            focus = self.root.focus_get()
        except KeyError:
            focus = None
        if fn is None or (focus is not None and focus.winfo_toplevel() is not self.root):
            return None                               # dialogs (review window ...) handle their own keys
        fn()
        return "break"

    def all_commands(self) -> list[Command]:
        cmds = [Command("settings", "Open settings", self.open_settings, "App", "preferences options", "Ctrl+,"),
                Command("theme.dark", "Dark theme", lambda: self.settings.set("ui.theme", "dark"), "App", "appearance"),
                Command("theme.light", "Light theme", lambda: self.settings.set("ui.theme", "light"), "App", "appearance"),
                Command("auto.start", "Start all watchers", self.start_all_auto, "Autopilot"),
                Command("auto.stop", "Stop all watchers", self.stop_all_auto, "Autopilot"),
                Command("data", "Open data folder", lambda: open_path(data_dir()), "App", "logs files")]
        cmds.append(Command("tools.customize", "Customize tools: order, hide, show", lambda: self.open_settings("home"),
                            "App", "rearrange sidebar home"))
        for t in self.sorted_tools():
            cmds.append(Command(f"go.{t.id}", f"Go to {t.title}", lambda i=t.id: self.show_tool(i), "Navigate", t.description))
        for t in self.hidden_tools():
            cmds.append(Command(f"unhide.{t.id}", f"Show {t.title} in the sidebar", lambda i=t.id: self.set_tool_hidden(i, False),
                                "App", "unhide"))
            for c in t.commands():
                cmds.append(Command(c.id, c.title, c.callback, c.group or t.title, c.keywords, c.shortcut))
        for p in self.settings_pages():
            cmds.append(Command(f"set.{p.id}", f"Settings: {p.title}", lambda i=p.id: self.open_settings(i), "Settings", p.description))
        return cmds + self.extra_commands

    # ------------------------------------------------------------------ window state
    def _restore_window(self) -> None:
        s = self.settings
        geo = s.get("window.geometry") if s.get_bool("window.remember_geometry", True) else ""
        try:
            self.root.geometry(geo or "1180x760")
        except tk.TclError:
            self.root.geometry("1180x760")
        if s.get_bool("window.start_maximized"):
            try:
                self.root.state("zoomed")
            except tk.TclError:
                try:
                    self.root.attributes("-zoomed", True)
                except tk.TclError:
                    pass
        self._apply_window_flags()

    def _apply_window_flags(self) -> None:
        s = self.settings
        for fn in (lambda: self.root.attributes("-topmost", s.get_bool("window.always_on_top")),
                   lambda: self.root.attributes("-alpha", max(0.4, s.get_int("window.opacity", 100) / 100))):
            try:
                fn()
            except tk.TclError:
                pass

    def _window_changed(self, key: str, value) -> None:
        if key in ("window.always_on_top", "window.opacity"):
            self._apply_window_flags()
        elif key == "window.show_statusbar":
            if value:
                self.status.pack(side="bottom", fill="x", before=self.outer)
            else:
                self.status.pack_forget()
        elif key in ("window.nav_style", "window.sidebar_compact"):
            self.root.after(50, self._build_nav)
            self.root.after(60, self._build_menus)
        elif key in ("window.hidden_tools", "window.tool_order"):
            self._schedule_nav_refresh()

    def toggle_fullscreen(self) -> None:
        self.root.attributes("-fullscreen", not bool(self.root.attributes("-fullscreen")))

    # ------------------------------------------------------------------ jobs
    def _job_changed(self, _job) -> None:
        active = self.jobs.active()
        if not active:
            self.progress.pack_forget()
            self.cancel_btn.pack_forget()
            self.job_lbl.pack_forget()
            return
        if not self.progress.winfo_ismapped():
            self.cancel_btn.pack(side="right")
            self.progress.pack(side="right", padx=6)
            self.job_lbl.pack(side="right")
        j = active[0]
        self.progress.configure(value=j.fraction * 100)
        more = f" (+{len(active) - 1})" if len(active) > 1 else ""
        self.job_lbl.configure(text=f"{j.title}{more}")

    # ------------------------------------------------------------------ files & automation
    def on_drop(self, paths: list[str]) -> None:
        if self.current and paths:
            self.current.accept_files(paths)

    def start_all_auto(self) -> None:
        for t in self.tools.values():
            if t.can_automate() and not t.auto_running():
                try:
                    t.start_auto()
                except Exception as exc:
                    self.log(f"{t.title}: could not start - {exc}", "error")

    def stop_all_auto(self) -> None:
        for t in self.tools.values():
            if t.can_automate() and t.auto_running():
                t.stop_auto()

    def _start_automation(self) -> None:
        if self.settings.get_bool("auto.start_watchers", True):
            self.start_all_auto()

    # ------------------------------------------------------------------ exit
    def quit(self) -> None:
        if self.jobs.active() and self.settings.get_bool("window.confirm_exit", True):
            if not confirm(self.root, "Work is still running. Quit anyway?"):
                return
        if self.settings.get_bool("window.remember_geometry", True) and self.root.state() == "normal":
            self.settings.set("window.geometry", self.root.geometry())
        for t in self.tools.values():
            try:
                t.on_close()
            except Exception:
                logging.getLogger("app").exception("on_close")
        self.jobs.shutdown()
        self.settings.flush()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    App().run()

