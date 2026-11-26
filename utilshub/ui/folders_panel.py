"""Unified Settings > Folders & paths page: manage all input/output folders, files, sheets, and CSV paths in one place."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from ..core.schema import Opt
from .widgets import FolderEntry, Tooltip

PATH_KINDS = ("folder", "file", "append", "save")
_KIND_BADGES = {
    "folder": "Folder",
    "file": "File",
    "append": "CSV log",
    "save": "Save file",
}
_TYPE_FILTERS: list[tuple[str, tuple[str, ...] | None]] = [
    ("All", None),
    ("Folders", ("folder",)),
    ("Files & sheets", ("file",)),
    ("CSV logs", ("append", "save")),
]


def is_managed_path_opt(opt: Opt) -> bool:
    """True for non-advanced folder/file/append/save options consolidated into Folders & paths."""
    return opt.kind in PATH_KINDS and not opt.advanced


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


def _ordered_member_path_opts(member, opts: list[Opt]) -> list[Opt]:
    in_key = f"{member.id}.{member.input_path_key}" if getattr(member, "input_path_key", "") else ""
    out_key = f"{member.id}.{member.output_path_key}" if getattr(member, "output_path_key", "") else ""

    def sort_key(opt: Opt) -> tuple[int, str]:
        if opt.key == in_key:
            return (0, opt.key)
        if opt.key == out_key:
            return (1, opt.key)
        if opt.kind == "folder":
            return (2, opt.label.lower())
        if opt.kind == "file":
            return (3, opt.label.lower())
        return (4, opt.label.lower())

    return sorted(opts, key=sort_key)


def feature_path_groups(app) -> list[tuple[str, str, str, list[Opt]]]:
    """Return `(group_id, icon, title, opts)` across shared pages and all tools."""
    from ..options import shared_pages

    groups: list[tuple[str, str, str, list[Opt]]] = []
    has_opt_lookup = hasattr(app.settings, "opt")

    for page in shared_pages():
        if page.id == "folders":
            continue
        opts = [
            o for o in page.opts
            if is_managed_path_opt(o) and (not has_opt_lookup or app.settings.opt(o.key) is not None)
        ]
        if opts:
            groups.append((page.id, page.icon, page.title, opts))

    tools = app.sorted_tools() if hasattr(app, "sorted_tools") else list(app.tools.values())
    for tool in tools:
        for member in tool.members():
            raw_opts = app.settings.opts(f"{member.id}.")
            managed = [o for o in raw_opts if is_managed_path_opt(o)]
            if managed:
                groups.append((member.id, member.icon, member.title, _ordered_member_path_opts(member, managed)))

    return groups


def feature_path_opts(app) -> list[Opt]:
    """Return every `Opt` managed by the Folders & paths page."""
    return [opt for _gid, _icon, _title, opts in feature_path_groups(app) for opt in opts]


def feature_path_keys(app) -> list[str]:
    """Return every setting key managed by the Folders & paths page."""
    return [opt.key for opt in feature_path_opts(app)]


def build_folders_panel(parent, app, initial_filter: str = "") -> ttk.Frame:
    """Build categorized path/folder pickers bound directly to Settings with live filter."""
    container = ttk.Frame(parent)
    container.pack(fill="x", pady=(6, 4))
    groups = feature_path_groups(app)

    vars_map: dict[str, tk.StringVar] = {}
    reset_btns: dict[str, ttk.Button] = {}
    guard = [False]

    fbar = ttk.Frame(container)
    fbar.pack(fill="x", pady=(0, 6))
    ttk.Label(fbar, text="Filter:").pack(side="left", padx=(0, 6))
    filter_var = tk.StringVar(value=initial_filter)
    filter_entry = ttk.Entry(fbar, textvariable=filter_var, width=28)
    filter_entry.pack(side="left", fill="x", expand=True)
    Tooltip(filter_entry, "Filter by feature name, setting label, path, or CSV")

    ttk.Label(fbar, text="Type:").pack(side="left", padx=(10, 4))
    type_var = tk.StringVar(value=_TYPE_FILTERS[0][0])
    type_cb = ttk.Combobox(fbar, textvariable=type_var, values=[lbl for lbl, _ in _TYPE_FILTERS],
                           state="readonly", width=15)
    type_cb.pack(side="left")

    def clear_filters() -> None:
        filter_var.set("")
        type_var.set(_TYPE_FILTERS[0][0])
        rebuild_cards()

    ttk.Button(fbar, text="Clear", style="Tiny.TButton", command=clear_filters).pack(side="left", padx=(6, 0))

    cards_host = ttk.Frame(container)
    cards_host.pack(fill="x")

    def paint_reset(key: str) -> None:
        btn = reset_btns.get(key)
        if btn is not None and btn.winfo_exists():
            btn.state(["!disabled"] if not app.settings.is_default(key) else ["disabled"])

    def rebuild_cards() -> None:
        for w in cards_host.winfo_children():
            w.destroy()
        vars_map.clear()
        reset_btns.clear()

        q = filter_var.get().strip().lower()
        allowed_kinds = next((kinds for lbl, kinds in _TYPE_FILTERS if lbl == type_var.get()), None)
        shown_cards = 0

        for gid, icon, title, opts in groups:
            kind_opts = [o for o in opts if allowed_kinds is None or o.kind in allowed_kinds]
            if not kind_opts:
                continue
            group_hit = bool(q and q in f"{title} {gid}".lower())
            if not q or group_hit:
                matched = kind_opts
            else:
                matched = [o for o in kind_opts if q in f"{o.label} {o.help} {o.key} {o.section}".lower()]
            if not matched:
                continue

            shown_cards += 1
            card = ttk.LabelFrame(cards_host, text=f"{icon}  {title}", padding=(10, 6))
            card.pack(fill="x", pady=4)

            for opt in matched:
                row = ttk.Frame(card)
                row.pack(fill="x", pady=(2, 4))
                head = ttk.Frame(row)
                head.pack(fill="x")
                ttk.Label(head, text=opt.label).pack(side="left")

                rb = ttk.Button(head, text="↺", width=2, style="Tiny.TButton",
                                command=lambda k=opt.key: app.settings.reset(k))
                rb.pack(side="right")
                Tooltip(rb, "Reset to default")

                badge = _KIND_BADGES.get(opt.kind, opt.kind.title())
                ttk.Label(head, text=badge, style="Muted.TLabel").pack(side="right", padx=(6, 6))

                var = tk.StringVar(value=str(app.settings.get(opt.key, "") or ""))
                kind_arg = "dir" if opt.kind == "folder" else opt.kind
                fe = FolderEntry(row, var, kind_arg, opt.filetypes, width=44,
                                 open_button=(opt.kind == "folder"), title=f"{title} — {opt.label}")
                fe.pack(fill="x", pady=(2, 0))
                if opt.help:
                    Tooltip(fe.entry, opt.help)

                vars_map[opt.key] = var
                reset_btns[opt.key] = rb

                def on_write(*_a, k=opt.key, v=var) -> None:
                    if guard[0]:
                        return
                    app.settings.set(k, v.get().strip())
                    paint_reset(k)

                var.trace_add("write", on_write)
                paint_reset(opt.key)

        if not shown_cards:
            ttk.Label(cards_host, text="No matching folders or paths.", style="Muted.TLabel").pack(anchor="w", pady=8)

    filter_var.trace_add("write", lambda *_: rebuild_cards())
    type_cb.bind("<<ComboboxSelected>>", lambda _e: rebuild_cards())
    rebuild_cards()

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
