"""File Batch Picker: High-speed batch search, organization, dry-run preview, and copy/move tool."""
from __future__ import annotations

import queue
import re
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from ..core.config import AppConfig


def unique_path(dest: Path) -> Path:
    """Incrementally names collision-free paths: report (1).pdf, report (2).pdf."""
    if not dest.exists():
        return dest
    i = 1
    while True:
        cand = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        if not cand.exists():
            return cand
        i += 1


def parse_names(text: str) -> list[str]:
    """Splits across newlines, commas, semicolons, tabs, strips quotes, dedupes, keeps order."""
    seen = set()
    out = []
    for raw in re.split(r"[\n,;\t]+", text):
        n = raw.strip().strip("\"'")
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


class FilePickerView(ttk.Frame):
    """File Batch Picker view."""
    def __init__(self, parent: Any, config: AppConfig, **kwargs):
        super().__init__(parent, padding=12, **kwargs)
        self.config = config

        self.q = queue.Queue()
        self.cancel_flag = threading.Event()
        self.worker = None
        self.missing: list[str] = []

        self.v_in = tk.StringVar(value=self.config.picker.get("input_folder", ""))
        self.v_out = tk.StringVar(value=self.config.picker.get("output_folder", ""))
        self.v_mode = tk.StringVar(value=self.config.picker.get("mode", "exact"))
        self.v_action = tk.StringVar(value=self.config.picker.get("action", "copy"))
        self.v_conflict = tk.StringVar(value=self.config.picker.get("conflict", "rename"))
        self.v_case = tk.BooleanVar(value=self.config.picker.get("case_sensitive", False))
        self.v_sub = tk.BooleanVar(value=self.config.picker.get("subfolders", True))
        self.v_keep = tk.BooleanVar(value=self.config.picker.get("keep_structure", False))
        self.v_ext = tk.StringVar(value=self.config.picker.get("extensions", ""))
        self.v_status = tk.StringVar(value="Ready.")

        self._build_ui()
        self.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}

        # Title
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="File Batch Picker", style="Header.TLabel").pack(side="left")
        ttk.Label(top, text="Batch locate, filter, and organize files by names list across folder hierarchies",
                  style="Muted.TLabel").pack(side="left", padx=12, pady=(2, 0))

        # 1. Folders
        f_box = ttk.LabelFrame(self, text="1. Folders", padding=8)
        f_box.pack(fill="x", **pad)
        f_box.columnconfigure(1, weight=1)

        ttk.Label(f_box, text="Input folder:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(f_box, textvariable=self.v_in).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(f_box, text="Browse...", command=lambda: self._browse_dir(self.v_in)).grid(row=0, column=2, **pad)

        ttk.Label(f_box, text="Output folder:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f_box, textvariable=self.v_out).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(f_box, text="Browse...", command=lambda: self._browse_dir(self.v_out)).grid(row=1, column=2, **pad)

        # 2. Names Input & Splitter
        main_split = ttk.PanedWindow(self, orient="horizontal")
        main_split.pack(fill="both", expand=True, pady=4)

        # Names Area (Left)
        n_box = ttk.LabelFrame(main_split, text="2. Target Names (pasted or loaded)", padding=8)
        main_split.add(n_box, weight=3)

        btn_bar = ttk.Frame(n_box)
        btn_bar.pack(fill="x", pady=(0, 4))
        ttk.Button(btn_bar, text="Load from file...", command=self._load_names_file).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="Paste", command=self._paste_names).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="Clear", command=self._clear_names).pack(side="left", padx=2)
        self.lbl_count = ttk.Label(btn_bar, text="0 names", style="Muted.TLabel")
        self.lbl_count.pack(side="right", padx=4)

        self.txt_names = tk.Text(n_box, height=8, wrap="word", font=("Consolas", 10))
        sb_n = ttk.Scrollbar(n_box, command=self.txt_names.yview)
        self.txt_names.configure(yscrollcommand=sb_n.set)
        self.txt_names.pack(side="left", fill="both", expand=True)
        sb_n.pack(side="right", fill="y")
        self.txt_names.bind("<KeyRelease>", lambda e: self._update_count())

        # Options Area (Right)
        o_box = ttk.LabelFrame(main_split, text="3. Options & Matching Mode", padding=8)
        main_split.add(o_box, weight=2)

        ttk.Label(o_box, text="Matching Mode:").pack(anchor="w", pady=(2, 1))
        for val, lbl in [("exact", "Exact name, any extension"),
                         ("starts", "Starts with"),
                         ("contains", "Contains"),
                         ("regex", "Regular expression")]:
            ttk.Radiobutton(o_box, text=lbl, variable=self.v_mode, value=val).pack(anchor="w")

        ttk.Separator(o_box).pack(fill="x", pady=6)
        ttk.Checkbutton(o_box, text="Case sensitive", variable=self.v_case).pack(anchor="w")
        ttk.Checkbutton(o_box, text="Include subfolders", variable=self.v_sub).pack(anchor="w")
        ttk.Checkbutton(o_box, text="Keep subfolder structure", variable=self.v_keep).pack(anchor="w")

        ttk.Label(o_box, text="Extensions (e.g. jpg, png, pdf):").pack(anchor="w", pady=(6, 1))
        ttk.Entry(o_box, textvariable=self.v_ext).pack(fill="x")

        # 4. Action & Run Bar
        act_box = ttk.Frame(self, style="Surface2.TFrame", padding=8)
        act_box.pack(fill="x", pady=4)

        ttk.Label(act_box, text="Action:").pack(side="left", padx=4)
        ttk.Radiobutton(act_box, text="Copy", variable=self.v_action, value="copy").pack(side="left", padx=2)
        ttk.Radiobutton(act_box, text="Move", variable=self.v_action, value="move").pack(side="left", padx=2)

        ttk.Label(act_box, text="If exists:").pack(side="left", padx=(12, 4))
        for c_val, c_lbl in [("rename", "Rename"), ("skip", "Skip"), ("overwrite", "Overwrite")]:
            ttk.Radiobutton(act_box, text=c_lbl, variable=self.v_conflict, value=c_val).pack(side="left", padx=2)

        self.btn_preview = ttk.Button(act_box, text="🔍 Preview (Dry Run)", command=lambda: self._start(dry_run=True))
        self.btn_preview.pack(side="left", padx=(16, 4))

        self.btn_run = ttk.Button(act_box, text="▶ Run", style="Primary.TButton", command=lambda: self._start(dry_run=False))
        self.btn_run.pack(side="left", padx=4)

        self.btn_cancel = ttk.Button(act_box, text="Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=4)

        self.btn_missing = ttk.Button(act_box, text="Save missing...", command=self._save_missing, state="disabled")
        self.btn_missing.pack(side="right", padx=4)

        # Progress & Log
        self.prog = ttk.Progressbar(self, orient="horizontal", mode="determinate")
        self.prog.pack(fill="x", pady=2)

        log_box = ttk.Frame(self)
        log_box.pack(fill="both", expand=True, pady=4)
        self.txt_log = tk.Text(log_box, height=8, wrap="word", font=("Consolas", 9), state="disabled", bg="#141816", fg="#e6ebe7")
        sb_log = ttk.Scrollbar(log_box, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb_log.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb_log.pack(side="right", fill="y")

        self.txt_log.tag_config("ok", foreground="#6fd6a0")
        self.txt_log.tag_config("warn", foreground="#f0c168")
        self.txt_log.tag_config("err", foreground="#f19a9a")

    def _browse_dir(self, var: tk.StringVar) -> None:
        p = filedialog.askdirectory()
        if p:
            var.set(p)

    def _load_names_file(self) -> None:
        p = filedialog.askopenfilename(filetypes=[("Text / CSV", "*.txt *.csv *.lst"), ("All", "*.*")])
        if p:
            try:
                content = Path(p).read_text(encoding="utf-8-sig", errors="replace")
                self.txt_names.delete("1.0", "end")
                self.txt_names.insert("1.0", content)
                self._update_count()
            except Exception as exc:
                messagebox.showerror("Error", str(exc))

    def _paste_names(self) -> None:
        try:
            cb = self.clipboard_get()
            self.txt_names.insert("insert", cb)
            self._update_count()
        except Exception:
            pass

    def _clear_names(self) -> None:
        self.txt_names.delete("1.0", "end")
        self._update_count()

    def _update_count(self) -> None:
        names = parse_names(self.txt_names.get("1.0", "end"))
        self.lbl_count.configure(text=f"{len(names)} names")

    def _log(self, msg: str, tag: str | None = None) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", msg + "\n", tag)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _start(self, dry_run: bool) -> None:
        in_dir = Path(self.v_in.get().strip())
        out_dir = Path(self.v_out.get().strip())
        names = parse_names(self.txt_names.get("1.0", "end"))

        if not in_dir.is_dir():
            messagebox.showerror("Error", "Please choose a valid input folder.")
            return
        if not out_dir.exists() and not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
        if not names:
            messagebox.showerror("Error", "No names specified.")
            return

        exts = {f".{e.strip().lstrip('.').lower()}" for e in self.v_ext.get().split(",") if e.strip()}

        self.btn_run.configure(state="disabled")
        self.btn_preview.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.btn_missing.configure(state="disabled")
        self.cancel_flag.clear()
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

        self.worker = threading.Thread(
            target=self._run_thread,
            args=(in_dir, out_dir, names, exts, dry_run),
            daemon=True,
        )
        self.worker.start()

    def _cancel(self) -> None:
        self.cancel_flag.set()
        self.v_status.set("Cancelling...")

    def _run_thread(self, in_dir: Path, out_dir: Path, names: list[str], exts: set[str], dry_run: bool) -> None:
        mode = self.v_mode.get()
        action = self.v_action.get()
        conflict = self.v_conflict.get()
        case = self.v_case.get()
        sub = self.v_sub.get()
        keep = self.v_keep.get()

        norm = (lambda s: s) if case else (lambda s: s.lower())

        # Collect files
        self.q.put(("log", f"Scanning {in_dir}...", None))
        it = in_dir.rglob("*") if sub else in_dir.glob("*")
        files = []
        for p in it:
            if not p.is_file():
                continue
            if exts and p.suffix.lower() not in exts:
                continue
            files.append(p)

        self.q.put(("log", f"Indexed {len(files)} candidate files.", "ok"))

        # Build index for exact mode
        index = {}
        if mode == "exact":
            for p in files:
                parts = p.name.split(".")
                for i in range(len(parts), 0, -1):
                    key = norm(".".join(parts[:i]))
                    index.setdefault(key, []).append(p)

        missing = []
        used = set()
        total = len(names)

        for i, name in enumerate(names, 1):
            if self.cancel_flag.is_set():
                self.q.put(("log", "Cancelled by user.", "warn"))
                break

            self.q.put(("progress", (i, total), None))
            key = norm(name)

            if mode == "exact":
                hits = index.get(key, [])
            elif mode == "starts":
                hits = [p for p in files if norm(p.name).startswith(key)]
            elif mode == "contains":
                hits = [p for p in files if key in norm(p.name)]
            elif mode == "regex":
                try:
                    rx = re.compile(name, 0 if case else re.I)
                    hits = [p for p in files if rx.search(p.name)]
                except re.error as e:
                    self.q.put(("log", f"Invalid regex '{name}': {e}", "err"))
                    hits = []

            if not hits:
                missing.append(name)
                self.q.put(("log", f"MISSING: {name}", "warn"))
                continue

            for p in hits:
                if p in used:
                    continue
                used.add(p)

                rel_dir = p.relative_to(in_dir).parent if keep else Path()
                dest = out_dir / rel_dir / p.name

                if dest.exists() and not dry_run:
                    if conflict == "skip":
                        self.q.put(("log", f"SKIP: {p.name}", "warn"))
                        continue
                    elif conflict == "rename":
                        dest = unique_path(dest)

                prefix = "WOULD " if dry_run else ""
                act_str = f"{prefix}{action.upper()}: {p.name} -> {dest}"
                self.q.put(("log", act_str, "ok"))

                if not dry_run:
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if action == "copy":
                            shutil.copy2(p, dest)
                        else:
                            shutil.move(p, dest)
                    except Exception as e:
                        self.q.put(("log", f"ERROR: {e}", "err"))

        self.q.put(("done", missing, dry_run))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, data, tag = self.q.get_nowait()
                if kind == "log":
                    self._log(data, tag)
                elif kind == "progress":
                    cur, total = data
                    self.prog["maximum"] = total
                    self.prog["value"] = cur
                elif kind == "done":
                    self.missing = data
                    dry_run = tag
                    self.btn_run.configure(state="normal")
                    self.btn_preview.configure(state="normal")
                    self.btn_cancel.configure(state="disabled")
                    if self.missing:
                        self.btn_missing.configure(state="normal")
                    summary = f"{'Preview' if dry_run else 'Operation'} finished. Missing: {len(self.missing)}"
                    self._log(summary, "warn" if self.missing else "ok")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _save_missing(self) -> None:
        if not self.missing:
            return
        p = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")])
        if p:
            Path(p).write_text("\n".join(self.missing), encoding="utf-8")
            messagebox.showinfo("Saved", f"Saved {len(self.missing)} missing names to {p}")

