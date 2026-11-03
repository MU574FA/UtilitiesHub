"""Main window: start/stop watching, live log, counters, review queue."""
from __future__ import annotations

import logging
import queue
import tkinter as tk
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import history, ocr
from ..clipboard import get_format, render_clipboard
from ..config import APP_NAME, LOG_PATH, Settings, load_settings
from ..fileops import duplicates_dir
from ..processor import AnalysisResult, Decision, analyze, finalize
from ..registry import NameRegistry
from ..watcher import FolderWatcher
from .common import center, copy_to_clipboard, open_path, setup_theme
from .review_dialog import ReviewDialog
from .settings_window import SettingsWindow

log = logging.getLogger(__name__)
POLL_MS = 150


class MainWindow(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.settings: Settings = load_settings()
        self.events: queue.Queue = queue.Queue()      # thread-safe bridge into the Tk loop
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analyzer")
        self.watcher: FolderWatcher | None = None
        self.registry = NameRegistry()
        self.review_queue: deque[AnalysisResult] = deque()
        self.review_open = False
        self.active: set[Path] = set()                # files currently in the pipeline
        self.stats = {"saved": 0, "duplicates": 0, "skipped": 0, "errors": 0}
        self.last_record: tuple | None = None         # (original path, fields incl. links, file name)

        setup_theme(self)
        self._build_menu()
        self._build_ui()
        self._make_registry()
        self.protocol("WM_DELETE_WINDOW", self._quit)
        center(self, 800, 540)
        self.after(POLL_MS, self._poll)
        if self.settings.autostart_watching:
            self.start_watching()

    # ------------------------------------------------------------------ UI
    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Process file(s)…", command=self.process_files)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._quit)
        tools = tk.Menu(menu, tearoff=False)
        tools.add_command(label="Settings…", command=self.open_settings)
        tools.add_command(label="Test OCR (Tesseract)", command=self._test_ocr)
        tools.add_separator()
        self.copy_menu = tk.Menu(tools, tearoff=False, postcommand=self._fill_copy_menu)
        tools.add_cascade(label="Copy last record as", menu=self.copy_menu)
        tools.add_command(label="Open names list", command=self._open_names)
        tools.add_command(label="Open duplicates folder", command=self._open_duplicates)
        tools.add_command(label="Open history CSV", command=self._open_history)
        menu.add_cascade(label="File", menu=file_menu)
        menu.add_cascade(label="Tools", menu=tools)
        self.config(menu=menu)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        self.status = tk.Label(top, text="● Stopped", fg="#888888", font=("TkDefaultFont", 11, "bold"))
        self.status.pack(side="left")
        ttk.Button(top, text="Settings", command=self.open_settings).pack(side="right")
        self.toggle_btn = ttk.Button(top, text="Start watching", style="Accent.TButton",
                                     command=self.toggle_watching)
        self.toggle_btn.pack(side="right", padx=6)

        self.folder_label = ttk.Label(self, style="Hint.TLabel", padding=(10, 0))
        self.folder_label.pack(fill="x")

        tools = ttk.Frame(self, padding=(10, 8))
        tools.pack(fill="x")
        ttk.Button(tools, text="Process file(s)…", command=self.process_files).pack(side="left")
        ttk.Button(tools, text="Open inbox", command=lambda: open_path(self.settings.watch_folder)).pack(side="left", padx=6)
        ttk.Button(tools, text="Open output", command=lambda: open_path(self.settings.output_folder)).pack(side="left")
        ttk.Button(tools, text="Open duplicates", command=self._open_duplicates).pack(side="left", padx=6)
        self.counters = ttk.Label(tools)
        self.counters.pack(side="right")

        box = ttk.Frame(self, padding=(10, 0, 10, 10))
        box.pack(fill="both", expand=True)
        self.log_text = tk.Text(box, state="disabled", wrap="word", font=("TkFixedFont", 9))
        scroll = ttk.Scrollbar(box, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        for tag, color in (("ok", "#0a7a3d"), ("warn", "#b36b00"), ("error", "#b00020"), ("info", "#222222")):
            self.log_text.tag_configure(tag, foreground=color)
        self._refresh_status()

    def log(self, message: str, level: str = "info") -> None:
        log.log(logging.ERROR if level == "error" else logging.INFO, message)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh_status(self) -> None:
        running = self.watcher is not None
        self.status.config(text="● Watching" if running else "● Stopped", fg="#0a7a3d" if running else "#888888")
        self.toggle_btn.config(text="Stop watching" if running else "Start watching")
        self.folder_label.config(text=f"Inbox: {self.settings.watch_folder}   →   Output: {self.settings.output_folder}")
        pending = len(self.review_queue) + (1 if self.review_open else 0)
        st = self.stats
        self.counters.config(text=f"Saved {st['saved']}  •  Duplicates {st['duplicates']}  •  "
                                  f"Skipped {st['skipped']}  •  Errors {st['errors']}  •  Review queue {pending}")

    def _make_registry(self) -> None:
        s = self.settings
        self.registry = NameRegistry(s.names_list_path, s.name_match_threshold / 100)
        if self.registry.enabled:
            try:
                self.log(f"Names list: {len(self.registry)} names ({self.registry.path})")
            except OSError as exc:
                self.log(f"Could not read the names list: {exc}", "error")

    # ------------------------------------------------------------------ watching
    def toggle_watching(self) -> None:
        self.stop_watching() if self.watcher else self.start_watching()

    def start_watching(self) -> None:
        if self.watcher:
            return
        s = self.settings
        try:
            Path(s.output_folder).mkdir(parents=True, exist_ok=True)
            watcher = FolderWatcher(Path(s.watch_folder), s.extensions, s.stable_seconds,
                                    lambda p: self.events.put(("file", p)), s.process_existing)
            watcher.start()
        except Exception as exc:
            self.log(f"Could not start watching: {exc}", "error")
            messagebox.showerror(APP_NAME, f"Could not start watching:\n{exc}")
            return
        self.watcher = watcher
        self.log(f"Watching {s.watch_folder}", "ok")
        self._refresh_status()

    def stop_watching(self) -> None:
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
            self.log("Stopped watching.", "warn")
        self._refresh_status()

    # ------------------------------------------------------------------ pipeline
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "file":
                    self._on_file(payload)
                elif kind == "analyzed":
                    self._on_analyzed(payload)
        except queue.Empty:
            pass
        self.after(POLL_MS, self._poll)

    def _on_file(self, path: Path) -> None:
        if path in self.active:
            return
        self.active.add(path)
        self.log(f"Detected: {path.name}")
        future = self.executor.submit(analyze, path, self.settings)
        future.add_done_callback(lambda f: self.events.put(("analyzed", f.result())))

    def _name_needs_review(self, result: AnalysisResult) -> bool:
        """True if the name is (almost) in the names list - never auto-save those."""
        if not self.registry.enabled:
            return False
        name = (result.fields.get("name") or [""])[0]
        return bool(name) and self.registry.check(name).status in ("exact", "similar")

    def _on_analyzed(self, result: AnalysisResult) -> None:
        s = self.settings
        if result.error:
            self.log(f"Could not read {result.path.name}: {result.error}", "error")
            self.stats["errors"] += 1
            if s.auto_confirm:
                self.active.discard(result.path)
                self._refresh_status()
                return
        elif s.auto_confirm:
            if self._name_needs_review(result):
                self.log(f"{result.path.name}: name is already (or nearly) in the list - please review.", "warn")
            else:
                clip = s.clipboard_default if s.clipboard_auto else None
                self._save(result, Decision("save", result.fields, clip))
                return
        self.review_queue.append(result)
        self._next_review()

    def _next_review(self) -> None:
        if not self.review_open and self.review_queue:
            self.review_open = True
            ReviewDialog(self, self.review_queue.popleft(), self.settings, self.registry, self._review_done)
        self._refresh_status()

    def _review_done(self, result: AnalysisResult, decision: Decision) -> None:
        self.review_open = False
        if decision.action == "skip":
            self.stats["skipped"] += 1
            self.active.discard(result.path)
            self.log(f"Skipped: {result.path.name} (left in place)", "warn")
        else:
            self._save(result, decision)
        self.after_idle(self._next_review)

    def _save(self, result: AnalysisResult, decision: Decision) -> None:
        try:
            outcome = finalize(result, decision, self.settings, self.registry)
        except Exception as exc:
            self.stats["errors"] += 1
            self.log(f"Could not save {result.path.name}: {exc}", "error")
        else:
            if outcome.status == "duplicate":
                self.stats["duplicates"] += 1
                self.log(f"⧉ {result.path.name}  →  Duplicates/{outcome.filename}", "warn")
            elif outcome.status == "exists":
                self.stats["skipped"] += 1
                self.log(f"Skipped {result.path.name}: target already exists", "warn")
            else:
                self.stats["saved"] += 1
                if outcome.status == "saved":
                    self.log(f"✔ {result.path.name}  →  {outcome.filename}", "ok")
                else:
                    self.log(f"✔ Recorded {result.path.name} (file left in place)", "ok")
                if outcome.name_added:
                    self.log(f"Added '{decision.fields['name'][0]}' to the names list.", "ok")
                all_fields = {**decision.fields, **outcome.links}
                self.last_record = (result.path, all_fields, outcome.filename)
                if decision.clip_name:
                    self._copy(decision.clip_name, result.path, all_fields, outcome.filename)
        finally:
            self.active.discard(result.path)
            self._refresh_status()

    # ------------------------------------------------------------------ clipboard
    def _copy(self, format_name: str, original: Path, fields: dict, filename: str) -> None:
        fmt = get_format(self.settings, format_name)
        if fmt is None:
            self.log(f"Clipboard format '{format_name}' no longer exists.", "warn")
            return
        try:
            text = render_clipboard(fmt.template, fields, original, filename)
        except ValueError as exc:
            self.log(str(exc), "error")
            return
        copy_to_clipboard(self, text)
        self.log(f"Copied to clipboard ({fmt.name}) - ready to paste.", "ok")

    def _fill_copy_menu(self) -> None:
        self.copy_menu.delete(0, "end")
        for fmt in self.settings.clipboard_formats:
            self.copy_menu.add_command(label=fmt.name, command=lambda n=fmt.name: self._copy_last(n))

    def _copy_last(self, format_name: str) -> None:
        if not self.last_record:
            messagebox.showinfo(APP_NAME, "Nothing saved yet in this session.")
            return
        original, fields, filename = self.last_record
        self._copy(format_name, original, fields, filename)

    # ------------------------------------------------------------------ actions
    def process_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Choose CV files to process")
        for p in paths:
            self.events.put(("file", Path(p)))

    def open_settings(self) -> None:
        SettingsWindow(self, self.settings, self._apply_settings)

    def _apply_settings(self, new: Settings) -> None:
        was_running = self.watcher is not None
        self.stop_watching()
        self.settings = new
        self.log("Settings saved.", "ok")
        self._make_registry()
        if was_running:
            self.start_watching()
        self._refresh_status()

    def _test_ocr(self) -> None:
        try:
            messagebox.showinfo(APP_NAME, f"Tesseract {ocr.check_tesseract(self.settings)} is ready.")
        except ocr.OcrUnavailable as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _open_names(self) -> None:
        if not self.registry.enabled:
            messagebox.showinfo(APP_NAME, "No names list is set. Choose one in Settings → Names list.")
            return
        self.registry.ensure_file()
        open_path(self.registry.path)

    def _open_duplicates(self) -> None:
        folder = duplicates_dir(self.settings)
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

    def _open_history(self) -> None:
        path = history.history_path(self.settings)
        if path.exists():
            open_path(path)
        else:
            messagebox.showinfo(APP_NAME, "No history yet.")

    def _quit(self) -> None:
        if self.watcher:
            self.watcher.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()


def run() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()])
    MainWindow().mainloop()
