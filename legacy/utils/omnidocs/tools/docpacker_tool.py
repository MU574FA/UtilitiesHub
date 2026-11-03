"""DocPacker & Compliance Verifier: Document compliance checks, MRZ, facial detection & PDF compilation."""
from __future__ import annotations

import datetime as dt
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from PIL import Image

from ..core.config import AppConfig
from ..core.ocr import (
    detect_and_fix_orientation,
    detect_and_fix_skew,
    detect_faces,
    has_exif_camera,
    ocr_image,
    ocr_mrz_region,
    setup_tesseract,
)
from ..core.pdf_ops import build_merged_pdf, render_page_thumbnail
from ..core.rules import get_default_rules
from ..ui.widgets import DropTarget, PageViewer, StatusBadge


class DocPackerView(ttk.Frame):
    """DocPacker compliance verification and compilation studio."""
    def __init__(self, parent: Any, config: AppConfig, **kwargs):
        super().__init__(parent, padding=12, **kwargs)
        self.config = config
        self.rules = get_default_rules()

        # Slot assignments: req_id -> file_item
        self.assignments: dict[str, dict[str, Any]] = {}

        self.requirements = [
            {"id": "passport", "name": "Passport Copy (Scanner)", "req": True},
            {"id": "photo", "name": "Applicant Portrait Photo", "req": True},
            {"id": "bank", "name": "Bank IBAN Confirmation Letter", "req": True},
            {"id": "medical", "name": "Medical Screening Report", "req": True},
        ]

        # Applicant profile fields
        self.v_name = tk.StringVar()
        self.v_surname = tk.StringVar()
        self.v_title = tk.StringVar()
        self.v_phone = tk.StringVar()
        self.v_joining = tk.StringVar(value=dt.datetime.now().strftime("%Y-%m-%d"))
        self.v_passport_no = tk.StringVar()
        self.v_dob = tk.StringVar()
        self.v_expiry = tk.StringVar()

        self._build_ui()

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 3}

        # Title
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="DocPacker & Compliance Verifier", style="Header.TLabel").pack(side="left")
        ttk.Label(top, text="Onboarding document compliance, MRZ validation, OpenCV face detection & cover page compiler",
                  style="Muted.TLabel").pack(side="left", padx=12, pady=(2, 0))

        # Main splitter (Left: Requirements & Profile, Right: Visual Document Viewer & Checklist)
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # --------------------------------------------- Left: Requirements & Profile
        left = ttk.Frame(paned, padding=6)
        paned.add(left, weight=3)

        # Dropzone for applicant docs
        drop = DropTarget(left, text="Drop applicant document(s) here",
                          subtext="Files will be analyzed and assigned to requirements",
                          on_drop=self._ingest_files)
        drop.pack(fill="x", pady=(0, 6))

        # Profile Card
        prof_card = ttk.LabelFrame(left, text="Applicant Profile Information", padding=8)
        prof_card.pack(fill="x", pady=4)
        prof_card.columnconfigure(1, weight=1)
        prof_card.columnconfigure(3, weight=1)

        fields = [
            ("First Name:", self.v_name, 0, 0),
            ("Surname:", self.v_surname, 0, 2),
            ("Job Title:", self.v_title, 1, 0),
            ("Phone:", self.v_phone, 1, 2),
            ("Joining Date:", self.v_joining, 2, 0),
            ("Passport No:", self.v_passport_no, 2, 2),
            ("Date of Birth:", self.v_dob, 3, 0),
            ("Passport Expiry:", self.v_expiry, 3, 2),
        ]
        for lbl_t, var_t, r, c in fields:
            ttk.Label(prof_card, text=lbl_t).grid(row=r, column=c, sticky="w", **pad)
            ttk.Entry(prof_card, textvariable=var_t, width=16).grid(row=r, column=c + 1, sticky="ew", **pad)

        # Requirements Assignment Tree
        req_card = ttk.LabelFrame(left, text="Required Document Slots & Compliance Status", padding=8)
        req_card.pack(fill="both", expand=True, pady=6)

        cols = ("req", "file", "status", "notes")
        self.tree_reqs = ttk.Treeview(req_card, columns=cols, show="headings", height=6)
        self.tree_reqs.heading("req", text="Requirement")
        self.tree_reqs.heading("file", text="Assigned File")
        self.tree_reqs.heading("status", text="Compliance")
        self.tree_reqs.heading("notes", text="Verification Notes")

        self.tree_reqs.column("req", width=180)
        self.tree_reqs.column("file", width=150)
        self.tree_reqs.column("status", width=90)
        self.tree_reqs.column("notes", width=180)

        self.tree_reqs.pack(fill="both", expand=True)
        self.tree_reqs.bind("<<TreeviewSelect>>", lambda e: self._on_req_selected())

        # Compile Button
        compile_bar = ttk.Frame(left, style="Surface2.TFrame", padding=6)
        compile_bar.pack(fill="x", pady=4)

        self.btn_compile = ttk.Button(compile_bar, text="📦 Compile & Generate Package PDF...",
                                      style="Primary.TButton", command=self._compile_package)
        self.btn_compile.pack(side="left", padx=4)

        self.prog = ttk.Progressbar(compile_bar, orient="horizontal", mode="determinate")
        self.prog.pack(side="left", fill="x", expand=True, padx=8)

        # --------------------------------------------- Right: Visual Studio & Page Rotator
        right = ttk.LabelFrame(paned, text="Document Visual Inspector & Rotation", padding=8)
        paned.add(right, weight=3)

        self.page_viewer = PageViewer(right)
        self.page_viewer.pack(fill="both", expand=True)

        self._refresh_req_table()

    def _refresh_req_table(self) -> None:
        self.tree_reqs.delete(*self.tree_reqs.get_children())
        for req in self.requirements:
            rid = req["id"]
            assigned = self.assignments.get(rid)
            fn = assigned["name"] if assigned else "(None)"
            status = assigned.get("status", "Pending") if assigned else "Missing"
            notes = assigned.get("notes", "") if assigned else ""
            self.tree_reqs.insert("", "end", iid=rid, values=(req["name"], fn, status, notes))

    def _ingest_files(self, paths: list[str]) -> None:
        def worker():
            setup_tesseract(self.config.general.tesseract_path)
            for p_str in paths:
                p = Path(p_str)
                if not p.is_file():
                    continue

                # 1. OCR or text extract
                text = ""
                img = render_page_thumbnail(p, 0, dpi=140)
                if img:
                    if self.config.automation.auto_rotate:
                        img, rot = detect_and_fix_orientation(img)
                    if self.config.automation.auto_deskew:
                        img, skew = detect_and_fix_skew(img)
                    text = ocr_image(img, lang=self.config.general.ocr_lang)

                # Determine best slot
                text_low = text.lower()
                target_slot = None
                status = "OK"
                notes = []

                if "passport" in text_low or "p<" in text or "nationality" in text_low:
                    target_slot = "passport"
                    # MRZ check
                    mrz_rule = next((r for r in self.rules if r.id == "passport_mrz"), None)
                    if mrz_rule:
                        p_ok, _, m_msg = mrz_rule.evaluate(text)
                        if p_ok:
                            notes.append("MRZ Valid")
                        else:
                            notes.append("MRZ Unclear")
                    
                    # Auto-fill name if present
                    if not self.v_name.get():
                        name_rule = next((r for r in self.rules if r.id == "name"), None)
                        if name_rule:
                            _, vals, _ = name_rule.evaluate(text)
                            if vals:
                                self.after(0, lambda n=vals[0]: self.v_name.set(n))

                elif "iban" in text_low or "swift" in text_low or "account number" in text_low:
                    target_slot = "bank"
                    iban_rule = next((r for r in self.rules if r.id == "iban"), None)
                    if iban_rule:
                        i_ok, vals, _ = iban_rule.evaluate(text)
                        if i_ok:
                            notes.append(f"IBAN: {vals[0][:8]}...")

                elif "medical" in text_low or "hiv" in text_low or "hcv" in text_low or "hbsag" in text_low or "igra" in text_low:
                    target_slot = "medical"
                    notes.append("Lab tests detected")

                elif img and img.height > img.width:
                    target_slot = "photo"
                    # Face detection
                    faces = detect_faces(img)
                    if faces:
                        notes.append(f"Face detected ({len(faces)})")
                    else:
                        notes.append("No face detected")

                if target_slot:
                    self.assignments[target_slot] = {
                        "path": str(p),
                        "name": p.name,
                        "status": "✔ OK" if "Invalid" not in notes else "⚠ Warning",
                        "notes": " · ".join(notes) or "Matched",
                    }

            self.after(0, self._refresh_req_table)

        threading.Thread(target=worker, daemon=True).start()

    def _on_req_selected(self) -> None:
        sel = self.tree_reqs.selection()
        if not sel:
            return
        rid = sel[0]
        assigned = self.assignments.get(rid)
        if assigned:
            self.page_viewer.load_document(assigned["path"])

    def _compile_package(self) -> None:
        missing = [r["name"] for r in self.requirements if r["id"] not in self.assignments]
        if missing:
            if not messagebox.askyesno("Missing Requirements", f"The following documents are missing:\n- " + "\n- ".join(missing) + "\n\nProceed anyway?"):
                return

        out_path = filedialog.asksaveasfilename(
            title="Save Compiled Applicant Package",
            defaultextension=".pdf",
            filetypes=[("PDF Package", "*.pdf")]
        )
        if not out_path:
            return

        self.btn_compile.configure(state="disabled")
        self.prog["value"] = 15

        def worker():
            try:
                # Prepare cover page info
                profile_grid = {
                    "Full Name": f"{self.v_name.get()} {self.v_surname.get()}".strip(),
                    "Job Title": self.v_title.get().strip(),
                    "Phone": self.v_phone.get().strip(),
                    "Joining Date": self.v_joining.get().strip(),
                    "Passport No": self.v_passport_no.get().strip(),
                    "Date of Birth": self.v_dob.get().strip(),
                    "Passport Expiry": self.v_expiry.get().strip(),
                }

                checklist = []
                merge_items = []
                for req in self.requirements:
                    rid = req["id"]
                    assigned = self.assignments.get(rid)
                    if assigned:
                        checklist.append({
                            "icon": "✔",
                            "name": req["name"],
                            "status": assigned.get("notes", "Verified"),
                        })
                        merge_items.append({
                            "path": assigned["path"],
                            "title": req["name"],
                        })
                    else:
                        checklist.append({
                            "icon": "○",
                            "name": req["name"],
                            "status": "Skipped / Optional",
                        })

                cover_info = {
                    "title": "APPLICANT ONBOARDING PACKAGE",
                    "date": dt.datetime.now().strftime("%d-%b-%Y"),
                    "profile": profile_grid,
                    "checklist": checklist,
                }

                ok = build_merged_pdf(merge_items, out_path, cover_info=cover_info)
                self.after(0, lambda: self._on_compile_done(ok, out_path))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Compilation Failed", str(exc)))
                self.after(0, lambda: self.btn_compile.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_compile_done(self, success: bool, out_path: str) -> None:
        self.btn_compile.configure(state="normal")
        self.prog["value"] = 100
        if success:
            messagebox.showinfo("Compilation Complete", f"Executive application package successfully compiled:\n{out_path}")
        else:
            messagebox.showerror("Error", "Could not assemble package.")

