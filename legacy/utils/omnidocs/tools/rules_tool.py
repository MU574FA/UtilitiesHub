"""Rules & Regex Studio: Manage, toggle, edit necessity levels, and test rules in a live sandbox."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any

from ..core.config import AppConfig
from ..core.rules import NECESSITY_LEVELS, RuleDef, get_default_rules


class RulesStudioView(ttk.Frame):
    """Studio for viewing, configuring, adding, and testing all regexes and compliance rules."""
    def __init__(self, parent: Any, config: AppConfig, **kwargs):
        super().__init__(parent, padding=12, **kwargs)
        self.config = config
        self.rules: list[RuleDef] = get_default_rules()

        # Load persisted overrides if any
        if self.config.rules:
            for r in self.rules:
                if r.id in self.config.rules:
                    saved = self.config.rules[r.id]
                    r.enabled = saved.get("enabled", r.enabled)
                    r.necessity = saved.get("necessity", r.necessity)
                    if "pattern" in saved:
                        r.pattern = saved["pattern"]

        self._build_ui()

    def _build_ui(self) -> None:
        # Title bar
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 10))

        ttk.Label(top, text="Rules & Regex Studio", style="Header.TLabel").pack(side="left")
        ttk.Label(top, text="Manage extraction patterns, toggle necessity levels, or test in the sandbox",
                  style="Muted.TLabel").pack(side="left", padx=12, pady=(2, 0))

        # Main splitter (PanedWindow: Left = Rules table, Right = Live Sandbox)
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # ---------------------------------------------------- Left: Rules Catalog
        left = ttk.Frame(paned, style="Card.TFrame", padding=10)
        paned.add(left, weight=3)

        # Toolbar
        tb = ttk.Frame(left)
        tb.pack(fill="x", pady=(0, 8))

        ttk.Button(tb, text="➕ Add Rule", style="Primary.TButton", command=self._add_rule).pack(side="left", padx=2)
        ttk.Button(tb, text="✎ Edit Rule", command=self._edit_rule).pack(side="left", padx=2)
        ttk.Button(tb, text="⟲ Reset Defaults", command=self._reset_defaults).pack(side="left", padx=2)
        ttk.Button(tb, text="💾 Save Rules", command=self._save_rules).pack(side="right", padx=2)

        # Quick Necessity Filter
        self.var_filter = tk.StringVar(value="All")
        ttk.Label(tb, text="Filter:", style="Muted.TLabel").pack(side="left", padx=(12, 4))
        cb = ttk.Combobox(tb, textvariable=self.var_filter, values=["All", "Strict", "Warning", "Optional", "Disabled"],
                          state="readonly", width=10)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._populate_table())

        # Table
        cols = ("id", "name", "category", "kind", "necessity", "status", "pattern")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        
        self.tree.heading("id", text="ID")
        self.tree.heading("name", text="Rule Name")
        self.tree.heading("category", text="Category")
        self.tree.heading("kind", text="Kind")
        self.tree.heading("necessity", text="Necessity Level")
        self.tree.heading("status", text="Status")
        self.tree.heading("pattern", text="Pattern / Keywords")

        self.tree.column("id", width=80)
        self.tree.column("name", width=140)
        self.tree.column("category", width=90)
        self.tree.column("kind", width=70)
        self.tree.column("necessity", width=100)
        self.tree.column("status", width=70)
        self.tree.column("pattern", width=220)

        sb_y = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb_y.set)
        
        self.tree.pack(side="left", fill="both", expand=True)
        sb_y.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", lambda e: self._toggle_or_edit())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._sync_to_sandbox())

        # ---------------------------------------------------- Right: Live Sandbox
        right = ttk.LabelFrame(paned, text="Interactive Rule Sandbox", padding=10)
        paned.add(right, weight=2)

        ttk.Label(right, text="Test selected rule against custom text:", style="Muted.TLabel").pack(anchor="w")

        # Active test rule indicator
        self.var_active_rule = tk.StringVar(value="Select a rule from the left table")
        ttk.Label(right, textvariable=self.var_active_rule, style="Accent.TLabel").pack(anchor="w", pady=(2, 6))

        # Sample input text box
        self.txt_sample = tk.Text(right, height=8, wrap="word", font=("Consolas", 10))
        self.txt_sample.pack(fill="x", pady=4)
        self.txt_sample.insert("1.0", "Candidate: John Doe\nEmail: john.doe@example.com\nPhone: +1 555-0199\nJob Title: Senior Mechanical Engineer\nExpected Salary: $4,500\nIBAN: GB29NWBK60161331926819\nIssued date: 15/09/2026")

        btn_box = ttk.Frame(right)
        btn_box.pack(fill="x", pady=6)
        ttk.Button(btn_box, text="▶ Run Test on Text", style="Primary.TButton", command=self._run_sandbox_test).pack(side="left")
        ttk.Button(btn_box, text="Clear", command=lambda: self.txt_sample.delete("1.0", "end")).pack(side="right")

        # Result display
        self.var_res_badge = tk.StringVar(value="Ready")
        ttk.Label(right, textvariable=self.var_res_badge, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(8, 2))

        self.txt_res = tk.Text(right, height=6, wrap="word", font=("Consolas", 9), state="disabled", bg="#1a1e1b", fg="#e6ebe7")
        self.txt_res.pack(fill="both", expand=True, pady=4)

        self._populate_table()

    def _populate_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        filt = self.var_filter.get()
        for r in self.rules:
            if filt != "All" and r.necessity != filt:
                continue
            status_txt = "Enabled" if r.enabled else "Disabled"
            self.tree.insert("", "end", iid=r.id, values=(
                r.id, r.name, r.category, r.kind, r.necessity, status_txt, r.pattern
            ))

    def _get_selected_rule(self) -> RuleDef | None:
        sel = self.tree.selection()
        if not sel:
            return None
        rule_id = sel[0]
        return next((r for r in self.rules if r.id == rule_id), None)

    def _sync_to_sandbox(self) -> None:
        r = self._get_selected_rule()
        if r:
            self.var_active_rule.set(f"Testing Rule: {r.name} ({r.kind})")

    def _toggle_or_edit(self) -> None:
        r = self._get_selected_rule()
        if not r:
            return
        # Toggle enabled state or cycle necessity
        r.enabled = not r.enabled
        self._populate_table()
        self.tree.selection_set(r.id)

    def _edit_rule(self) -> None:
        r = self._get_selected_rule()
        if not r:
            messagebox.showinfo("Select Rule", "Please select a rule to edit.")
            return

        dlg = tk.Toplevel(self)
        dlg.title(f"Edit Rule: {r.name}")
        dlg.geometry("440x360")
        dlg.transient(self)
        dlg.grab_set()

        pad = {"padx": 10, "pady": 6}
        ttk.Label(dlg, text="Rule Name:").pack(anchor="w", **pad)
        v_name = tk.StringVar(value=r.name)
        ttk.Entry(dlg, textvariable=v_name).pack(fill="x", **pad)

        ttk.Label(dlg, text="Necessity Level:").pack(anchor="w", **pad)
        v_nec = tk.StringVar(value=r.necessity)
        ttk.Combobox(dlg, textvariable=v_nec, values=NECESSITY_LEVELS, state="readonly").pack(fill="x", **pad)

        ttk.Label(dlg, text="Pattern / Keywords / Expression:").pack(anchor="w", **pad)
        v_pat = tk.StringVar(value=r.pattern)
        ttk.Entry(dlg, textvariable=v_pat).pack(fill="x", **pad)

        v_en = tk.BooleanVar(value=r.enabled)
        ttk.Checkbutton(dlg, text="Rule is Enabled", variable=v_en).pack(anchor="w", **pad)

        def save_and_close():
            r.name = v_name.get().strip() or r.name
            r.necessity = v_nec.get()
            r.pattern = v_pat.get().strip()
            r.enabled = v_en.get()
            dlg.destroy()
            self._populate_table()
            self.tree.selection_set(r.id)

        ttk.Button(dlg, text="Save Changes", style="Primary.TButton", command=save_and_close).pack(pady=12)

    def _add_rule(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Add New Rule / Regex")
        dlg.geometry("460x420")
        dlg.transient(self)
        dlg.grab_set()

        pad = {"padx": 10, "pady": 5}
        ttk.Label(dlg, text="Rule Unique ID (e.g. custom_cert):").pack(anchor="w", **pad)
        v_id = tk.StringVar()
        ttk.Entry(dlg, textvariable=v_id).pack(fill="x", **pad)

        ttk.Label(dlg, text="Rule Name:").pack(anchor="w", **pad)
        v_name = tk.StringVar()
        ttk.Entry(dlg, textvariable=v_name).pack(fill="x", **pad)

        ttk.Label(dlg, text="Kind:").pack(anchor="w", **pad)
        v_kind = tk.StringVar(value="regex")
        ttk.Combobox(dlg, textvariable=v_kind, values=["regex", "keywords", "label", "doc_age", "mrz"], state="readonly").pack(fill="x", **pad)

        ttk.Label(dlg, text="Category:").pack(anchor="w", **pad)
        v_cat = tk.StringVar(value="Extractor")
        ttk.Combobox(dlg, textvariable=v_cat, values=["Extractor", "Compliance"], state="readonly").pack(fill="x", **pad)

        ttk.Label(dlg, text="Necessity:").pack(anchor="w", **pad)
        v_nec = tk.StringVar(value="Warning")
        ttk.Combobox(dlg, textvariable=v_nec, values=NECESSITY_LEVELS, state="readonly").pack(fill="x", **pad)

        ttk.Label(dlg, text="Pattern / Expression:").pack(anchor="w", **pad)
        v_pat = tk.StringVar()
        ttk.Entry(dlg, textvariable=v_pat).pack(fill="x", **pad)

        def save_new():
            rid = v_id.get().strip()
            name = v_name.get().strip()
            pat = v_pat.get().strip()
            if not rid or not name or not pat:
                messagebox.showerror("Missing Fields", "Please complete all fields.")
                return
            new_r = RuleDef(id=rid, name=name, category=v_cat.get(), kind=v_kind.get(), pattern=pat, necessity=v_nec.get())
            self.rules.append(new_r)
            dlg.destroy()
            self._populate_table()
            self.tree.selection_set(new_r.id)

        ttk.Button(dlg, text="Add Rule", style="Primary.TButton", command=save_new).pack(pady=12)

    def _reset_defaults(self) -> None:
        if messagebox.askyesno("Reset Rules", "Reset all rules to default patterns and levels?"):
            self.rules = get_default_rules()
            self.config.rules = {}
            self.config.save()
            self._populate_table()

    def _save_rules(self) -> None:
        rule_dict = {}
        for r in self.rules:
            rule_dict[r.id] = {
                "name": r.name,
                "enabled": r.enabled,
                "necessity": r.necessity,
                "pattern": r.pattern,
                "kind": r.kind,
                "category": r.category,
            }
        self.config.rules = rule_dict
        self.config.save()
        messagebox.showinfo("Saved", "Rule configurations successfully saved!")

    def _run_sandbox_test(self) -> None:
        r = self._get_selected_rule()
        if not r:
            messagebox.showinfo("Select Rule", "Please select a rule from the left table to test.")
            return

        text = self.txt_sample.get("1.0", "end-1c")
        passed, vals, msg = r.evaluate(text)

        status_str = f"Status: {'✔ PASSED' if passed else '✖ FAILED'} ({r.necessity})"
        self.var_res_badge.set(status_str)

        self.txt_res.configure(state="normal")
        self.txt_res.delete("1.0", "end")
        self.txt_res.insert("end", f"Message: {msg}\n")
        self.txt_res.insert("end", f"Matches Extracted ({len(vals)}):\n")
        for i, v in enumerate(vals, 1):
            self.txt_res.insert("end", f"  [{i}] {v}\n")
        self.txt_res.configure(state="disabled")

