"""Unified Settings > Folders & paths page: set input and output folders for every feature in one place."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from .widgets import FolderEntry, Tooltip


def feature_path_entries(app) -> list[tuple[Any, str | None, str | None]]:
    """Return `(tool, input_setting_key, output_setting_key)` for each feature that uses input/output paths."""
    tools = app.sorted_tools() if hasattr(app, "sorted_tools") else list(app.tools.values())
    entries: list[tuple[Any, str | None, str | None]] = []
    for tool in tools:
        for member in tool.members():
            in_rel = getattr(member, "input_path_key", "")
            out_rel = getattr(member, "output_path_key", "")
            in_key = f"{member.id}.{in_rel}" if in_rel else None
            out_key = f"{member.id}.{out_rel}" if out_rel else None
            if in_key or out_key:
                entries.append((member, in_key, out_key))
    return entries


def feature_path_keys(app) -> list[str]:
    """Return every setting key managed by the Folders & paths page."""
    keys: list[str] = []
    for _tool, in_key, out_key in feature_path_entries(app):
        if in_key:
            keys.append(in_key)
        if out_key:
            keys.append(out_key)
    return keys


def build_folders_panel(parent, app) -> ttk.Frame:
    """Build per-feature input/output folder pickers bound directly to Settings."""
    container = ttk.Frame(parent)
    container.pack(fill="x", pady=(6, 4))
    vars_map: dict[str, tk.StringVar] = {}
    reset_btns: dict[str, ttk.Button] = {}
    guard = [False]

    def paint_reset(key: str) -> None:
        btn = reset_btns.get(key)
        if btn is not None:
            btn.state(["!disabled"] if not app.settings.is_default(key) else ["disabled"])

    for tool, in_key, out_key in feature_path_entries(app):
        card = ttk.LabelFrame(container, text=f"{tool.icon}  {tool.title}", padding=(10, 6))
        card.pack(fill="x", pady=4)
        card.columnconfigure(1, weight=1)

        def add_row(row_idx: int, label_text: str, full_key: str, dialog_title: str) -> None:
            ttk.Label(card, text=label_text, width=12).grid(row=row_idx, column=0, sticky="w", padx=(0, 8), pady=2)
            var = tk.StringVar(value=str(app.settings.get(full_key, "") or ""))
            fe = FolderEntry(card, var, "dir", width=44, open_button=True, title=dialog_title)
            fe.grid(row=row_idx, column=1, sticky="ew", pady=2)
            opt = app.settings.opt(full_key)
            if opt and opt.help:
                Tooltip(fe.entry, opt.help)
            rb = ttk.Button(card, text="↺", width=2, style="Tiny.TButton",
                            command=lambda k=full_key: app.settings.reset(k))
            rb.grid(row=row_idx, column=2, padx=(4, 0), pady=2)
            Tooltip(rb, "Reset to default (empty)")
            vars_map[full_key] = var
            reset_btns[full_key] = rb

            def on_write(*_a, k=full_key, v=var) -> None:
                if guard[0]:
                    return
                app.settings.set(k, v.get().strip())
                paint_reset(k)

            var.trace_add("write", on_write)
            paint_reset(full_key)

        row_idx = 0
        if in_key:
            add_row(row_idx, "Input path", in_key, f"{tool.title} — Input folder")
            row_idx += 1
        if out_key:
            add_row(row_idx, "Output path", out_key, f"{tool.title} — Output folder")

    def on_external(key: str, value) -> None:
        if not container.winfo_exists() or key not in vars_map:
            return
        paint_reset(key)
        if guard[0]:
            return
        val = str(value or "")
        var = vars_map[key]
        if var.get().strip() != val.strip():
            guard[0] = True
            try:
                var.set(val)
            finally:
                guard[0] = False

    unsub = app.settings.subscribe("", on_external)
    container.bind("<Destroy>", lambda e: e.widget is container and unsub())
    return container
