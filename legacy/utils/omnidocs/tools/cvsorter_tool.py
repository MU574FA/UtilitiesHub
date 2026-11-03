"""CVSorter & Applicant Organizer: Automated CV folder watching, metadata extraction, registry duplicate check & renaming."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from PIL import Image, ImageTk

from ..core.clipboard import copy_to_clipboard, format_spreadsheet_row
from ..core.config import AppConfig
from ..core.fuzzy import levenshtein_ratio
from ..core.ocr import detect_and_fix_orientation, ocr_image, preprocess_image, repair_email_whitespace, setup_tesseract
from ..core.pdf_ops import render_page_thumbnail
from ..core.rules import get_default_rules
from ..core.watcher import DirectoryWatcher, wait_until_stable
from ..ui.widgets import DropTarget, MetricPill, StatusBadge


class CVSorterView(ttk.Frame):
    """CVSorter view: Watcher, Extraction, Candidate Registry and Verification Dialog."""
    def __init__(self, parent: Any, config: AppConfig, **kwargs):
        super().__init__(parent, padding=12, **kwargs)
        self.config = config
        self.rules = get_default_rules()

        self.event_queue = queue.Queue()
        self.watcher: DirectoryWatcher | None = None
        self.registry_names: list[str] = []

        self.v_watch = tk.StringVar(value=self.config.cvsorter.get("watch_folder", ""))
        self.v_output = tk.StringVar(value=self.config.cvsorter.get("output_folder", ""))
        self.v_dups = tk.StringVar(value=self.config.cvsorter.get("duplicates_folder", ""))
        self.v_reg = tk.StringVar(value=self.config.cvsorter.get("names_list_path", ""))
        self.v_tmpl = tk.StringVar(value=self.config.cvsorter.get("filename_template", "{name} - {job_title}"))

        self.current_analysis: dict[str, Any] | None = None

        self._build_ui()
        self._load_registry()
        self.after(200, self._poll_events)

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}

        # Title
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="CV Sorter & Recruiter Studio", style="Header.TLabel").pack(side="left")
        ttk.Label(top, text="Real-time folder watcher, multi-format CV ingestion, OCR, duplicate registry & routing",
                  style="Muted.TLabel").pack(side="left", padx=12, pady=(2, 0))

        # Folders Configuration Card
        f_card = ttk.LabelFrame(self, text="Folder Watcher & Target Directories", padding=8)
        f_card.pack(fill="x", pady=(0, 6))
        f_card.columnconfigure(1, weight=1)

        ttk.Label(f_card, text="Watch folder:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(f_card, textvariable=self.v_watch).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(f_card, text="Browse...", command=lambda: self._browse_dir(self.v_watch)).grid(row=0, column=2, **pad)

        ttk.Label(f_card, text="Output folder:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f_card, textvariable=self.v_output).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(f_card, text="Browse...", command=lambda: self._browse_dir(self.v_output)).grid(row=1, column=2, **pad)

        ttk.Label(f_card, text="Duplicates folder:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(f_card, textvariable=self.v_dups).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(f_card, text="Browse...", command=lambda: self._browse_dir(self.v_dups)).grid(row=2, column=2, **pad)

        ttk.Label(f_card, text="Candidate registry (.txt/.csv):").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(f_card, textvariable=self.v_reg).grid(row=3, column=1, sticky="ew", **pad)
        ttk.Button(f_card, text="Browse...", command=self._browse_registry).grid(row=3, column=2, **pad)

        # Controls & Watcher Status Bar
        ctrl_bar = ttk.Frame(self, style="Surface2.TFrame", padding=8)
        ctrl_bar.pack(fill="x", pady=4)

        self.btn_watch = ttk.Button(ctrl_bar, text="▶ Start Watcher", style="Primary.TButton", command=self._toggle_watcher)
        self.btn_watch.pack(side="left", padx=4)

        ttk.Button(ctrl_bar, text="📁 Process Watch Folder Now", command=self._process_existing_files).pack(side="left", padx=4)

        self.v_auto_process = tk.BooleanVar(value=self.config.automation.auto_process_cvs)
        ttk.Checkbutton(ctrl_bar, text="Auto-process without prompt if not duplicate", variable=self.v_auto_process).pack(side="left", padx=12)

        self.lbl_watch_status = ttk.Label(ctrl_bar, text="Watcher stopped", style="Muted.TLabel")
        self.lbl_watch_status.pack(side="right", padx=6)

        # Main splitter (Left: Verification & Metadata Editor, Right: Visual Document Viewer)
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=4)

        # --------------------------------------------- Left: Verification Form
        left = ttk.LabelFrame(paned, text="Applicant Verification & Dynamic Naming", padding=8)
        paned.add(left, weight=3)

        # Duplicate status banner
        self.dup_banner = ttk.Frame(left, style="Surface2.TFrame", padding=6)
        self.dup_banner.pack(fill="x", pady=(0, 6))

        self.badge_dup = StatusBadge(self.dup_banner, text="No File Loaded", kind="neutral")
        self.badge_dup.pack(side="left", padx=4)
        self.lbl_dup_msg = ttk.Label(self.dup_banner, text="Waiting for CV ingestion...", style="Muted.TLabel")
        self.lbl_dup_msg.pack(side="left", padx=6)

        # Fields Form
        self.v_name = tk.StringVar()
        self.v_title = tk.StringVar()
        self.v_phone = tk.StringVar()
        self.v_email = tk.StringVar()
        self.v_company = tk.StringVar()

        for lbl_t, var_t in [("Candidate Name:", self.v_name),
                             ("Job Title:", self.v_title),
                             ("Phone Number:", self.v_phone),
                             ("Email Address:", self.v_email),
                             ("Company / School:", self.v_company)]:
            r_f = ttk.Frame(left)
            r_f.pack(fill="x", pady=2)
            ttk.Label(r_f, text=lbl_t, width=16, anchor="w").pack(side="left")
            ent = ttk.Entry(r_f, textvariable=var_t, font=("Segoe UI", 10))
            ent.pack(side="left", fill="x", expand=True, padx=4)
            var_t.trace_add("write", lambda *a: self._update_filename_preview())

        # Filename preview
        ttk.Separator(left).pack(fill="x", pady=6)
        ttk.Label(left, text="Dynamic Filename Preview:", style="Muted.TLabel").pack(anchor="w")
        self.lbl_fn_preview = ttk.Label(left, text="", font=("Consolas", 10, "bold"))
        self.lbl_fn_preview.pack(anchor="w", pady=(2, 8))

        # Actions
        act_box = ttk.Frame(left)
        act_box.pack(fill="x", pady=4)

        self.btn_save = ttk.Button(act_box, text="✔ Save & Route File", style="Primary.TButton",
                                   command=lambda: self._handle_decision("save"))
        self.btn_save.pack(side="left", padx=4)

        ttk.Button(act_box, text="⚠ Send to Duplicates", command=lambda: self._handle_decision("duplicate")).pack(side="left", padx=4)
        ttk.Button(act_box, text="Skip", command=lambda: self._handle_decision("skip")).pack(side="left", padx=4)
        ttk.Button(act_box, text="📋 Copy Excel Row", command=self._copy_excel_row).pack(side="right", padx=4)

        # --------------------------------------------- Right: Document Preview
        right = ttk.LabelFrame(paned, text="Document Visual Preview", padding=8)
        paned.add(right, weight=3)

        self.lbl_doc_preview = ttk.Label(right, text="No document active", anchor="center", justify="center")
        self.lbl_doc_preview.pack(fill="both", expand=True)

    def _browse_dir(self, var: tk.StringVar) -> None:
        p = filedialog.askdirectory()
        if p:
            var.set(p)

    def _browse_registry(self) -> None:
        p = filedialog.askopenfilename(filetypes=[("Text / CSV", "*.txt *.csv"), ("All", "*.*")])
        if p:
            self.v_reg.set(p)
            self._load_registry()

    def _load_registry(self) -> None:
        p_str = self.v_reg.get().strip()
        if not p_str:
            return
        p = Path(p_str)
        if not p.is_file():
            return
        names = []
        try:
            for ln in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                clean = ln.strip()
                if clean and not clean.startswith("#"):
                    names.append(clean)
            self.registry_names = names
        except Exception:
            pass

    def _toggle_watcher(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
            self.btn_watch.configure(text="▶ Start Watcher")
            self.lbl_watch_status.configure(text="Watcher stopped")
        else:
            w_dir = self.v_watch.get().strip()
            if not w_dir or not Path(w_dir).is_dir():
                messagebox.showerror("Invalid Folder", "Please select a valid folder to watch.")
                return

            self.watcher = DirectoryWatcher(w_dir, self.event_queue)
            ok = self.watcher.start()
            if ok:
                self.btn_watch.configure(text="⏹ Stop Watcher")
                self.lbl_watch_status.configure(text=f"Watching: {Path(w_dir).name}")
            else:
                messagebox.showerror("Error", "Could not start directory watcher.")

    def _process_existing_files(self) -> None:
        w_dir = self.v_watch.get().strip()
        if not w_dir or not Path(w_dir).is_dir():
            messagebox.showinfo("Select Folder", "Please select a watch folder first.")
            return

        for p in Path(w_dir).iterdir():
            if p.is_file() and p.suffix.lower() in (".pdf", ".docx", ".jpg", ".jpeg", ".png"):
                self.event_queue.put(p)

    def _poll_events(self) -> None:
        try:
            while not self.event_queue.empty():
                file_path = self.event_queue.get_nowait()
                threading.Thread(target=self._analyze_file_thread, args=(file_path,), daemon=True).start()
        except Exception:
            pass
        self.after(300, self._poll_events)

    def _analyze_file_thread(self, file_path: Path) -> None:
        if not wait_until_stable(file_path):
            return

        setup_tesseract(self.config.general.tesseract_path)
        
        # 1. Read document text & render thumbnail
        thumb_img = render_page_thumbnail(file_path, page_num=0, dpi=120)
        
        # Try native text extraction or OCR
        extracted_text = ""
        try:
            if file_path.suffix.lower() == ".pdf":
                import pymupdf as fitz
                with fitz.open(str(file_path)) as d:
                    for page in d:
                        extracted_text += page.get_text()
            elif file_path.suffix.lower() in (".jpg", ".jpeg", ".png"):
                if thumb_img:
                    proc = preprocess_image(thumb_img)
                    extracted_text = ocr_image(proc, lang=self.config.general.ocr_lang)
        except Exception:
            pass

        extracted_text = repair_email_whitespace(extracted_text)

        # 2. Extract fields using Rules
        extracted = {}
        for r in self.rules:
            if r.id in ("name", "job_title", "phone", "email"):
                _, vals, _ = r.evaluate(extracted_text)
                extracted[r.id] = vals[0] if vals else ""

        # Check candidate duplicate against registry
        cand_name = extracted.get("name", "")
        best_score = 0.0
        match_name = ""
        for reg_n in self.registry_names:
            sc = levenshtein_ratio(cand_name.lower(), reg_n.lower())
            if sc > best_score:
                best_score = sc
                match_name = reg_n

        is_dup = best_score >= 0.85
        dup_info = {"is_dup": is_dup, "score": best_score, "matched": match_name}

        analysis = {
            "path": file_path,
            "text": extracted_text,
            "fields": extracted,
            "dup_info": dup_info,
            "thumb": thumb_img,
        }

        self.after(0, lambda: self._display_analysis(analysis))

    def _display_analysis(self, analysis: dict[str, Any]) -> None:
        self.current_analysis = analysis
        f = analysis["fields"]
        self.v_name.set(f.get("name", ""))
        self.v_title.set(f.get("job_title", ""))
        self.v_phone.set(f.get("phone", ""))
        self.v_email.set(f.get("email", ""))
        self.v_company.set(f.get("company", ""))

        # Update duplicate indicator
        dup = analysis["dup_info"]
        if dup["score"] >= 0.98:
            self.badge_dup.set_status("EXACT DUPLICATE", "danger")
            self.lbl_dup_msg.configure(text=f"Already registered as: '{dup['matched']}'")
        elif dup["is_dup"]:
            self.badge_dup.set_status("PROBABLE DUPLICATE", "warn")
            self.lbl_dup_msg.configure(text=f"Similar to '{dup['matched']}' ({int(dup['score'] * 100)}%)")
        else:
            self.badge_dup.set_status("NEW CANDIDATE", "ok")
            self.lbl_dup_msg.configure(text="Candidate name not found in registry")

        # Update thumbnail preview
        thumb = analysis.get("thumb")
        if thumb:
            scale = min(360 / thumb.width, 420 / thumb.height, 1.0)
            dw = max(50, int(thumb.width * scale))
            dh = max(50, int(thumb.height * scale))
            resized = thumb.resize((dw, dh), Image.Resampling.LANCZOS)
            self._thumb_ref = ImageTk.PhotoImage(resized)
            self.lbl_doc_preview.configure(image=self._thumb_ref, text="")

        self._update_filename_preview()

        # Check auto-process
        if self.v_auto_process.get() and not dup["is_dup"]:
            self._handle_decision("save")

    def _update_filename_preview(self) -> None:
        tmpl = self.v_tmpl.get()
        name = self.v_name.get().strip() or "Candidate"
        title = self.v_title.get().strip() or "Applicant"
        fn = tmpl.replace("{name}", name).replace("{job_title}", title)
        # Scrub reserved characters
        import re
        safe_fn = re.sub(r'[<>:"/\\|?*]', "_", fn).strip()
        self.lbl_fn_preview.configure(text=f"{safe_fn}.pdf")

    def _handle_decision(self, action: str) -> None:
        if not self.current_analysis:
            return

        src_path = self.current_analysis["path"]
        out_dir = Path(self.v_output.get().strip()) if action == "save" else Path(self.v_dups.get().strip())
        
        if action == "skip":
            self.lbl_dup_msg.configure(text="Skipped file without moving.")
            return

        if not out_dir.exists():
            out_dir.mkdir(parents=True, exist_ok=True)

        target_name = self.lbl_fn_preview.cget("text")
        dest_path = out_dir / target_name

        import shutil
        try:
            shutil.copy2(src_path, dest_path)
            self.lbl_dup_msg.configure(text=f"File successfully saved to {dest_path.name}")
            
            # Auto add to registry
            cand_name = self.v_name.get().strip()
            if cand_name and self.config.automation.auto_add_candidates_to_registry:
                reg_path = Path(self.v_reg.get().strip())
                if reg_path.is_file() and cand_name not in self.registry_names:
                    with open(reg_path, "a", encoding="utf-8") as f:
                        f.write(f"\n{cand_name}")
                    self.registry_names.append(cand_name)
        except Exception as exc:
            messagebox.showerror("Error", f"Failed to transfer file: {exc}")

    def _copy_excel_row(self) -> None:
        vals = [self.v_name.get(), self.v_title.get(), self.v_phone.get(), self.v_email.get(), self.v_company.get()]
        tab_str = format_spreadsheet_row(vals)
        copy_to_clipboard(tab_str)
        messagebox.showinfo("Copied", "Copied candidate row to clipboard (ready to paste in Excel)!")

