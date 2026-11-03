"""
File Picker - copy/move files by name (any extension) from one folder to another.
Pure standard library. Run:  python file_picker.py
"""
import json
import queue
import re
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

SETTINGS_FILE = Path.home() / ".file_picker_settings.json"


def unique_path(dest: Path) -> Path:
    """photo.jpg -> photo (1).jpg -> photo (2).jpg ..."""
    if not dest.exists():
        return dest
    i = 1
    while True:
        cand = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        if not cand.exists():
            return cand
        i += 1


def parse_names(text: str):
    """Split on newlines/commas/semicolons/tabs, strip quotes, dedupe, keep order."""
    seen, out = set(), []
    for raw in re.split(r"[\n,;\t]+", text):
        n = raw.strip().strip("\"'")
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("File Picker - copy files by name")
        self.geometry("820x760")
        self.minsize(700, 620)

        self.q = queue.Queue()
        self.cancel_flag = threading.Event()
        self.worker = None
        self.missing = []

        self.v_in = tk.StringVar()
        self.v_out = tk.StringVar()
        self.v_mode = tk.StringVar(value="exact")
        self.v_action = tk.StringVar(value="copy")
        self.v_conflict = tk.StringVar(value="rename")
        self.v_case = tk.BooleanVar(value=False)
        self.v_sub = tk.BooleanVar(value=True)
        self.v_keep = tk.BooleanVar(value=False)
        self.v_ext = tk.StringVar()
        self.v_status = tk.StringVar(value="Ready.")

        self._build()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)

    # ---------------------------------------------------------------- UI
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        root.rowconfigure(3, weight=2)

        # Folders
        f = ttk.LabelFrame(root, text="1. Folders")
        f.grid(row=0, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        for r, (label, var, cmd) in enumerate([
            ("Input folder", self.v_in, lambda: self._browse(self.v_in)),
            ("Output folder", self.v_out, lambda: self._browse(self.v_out)),
        ]):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", **pad)
            ttk.Entry(f, textvariable=var).grid(row=r, column=1, sticky="ew", **pad)
            ttk.Button(f, text="Browse...", command=cmd).grid(row=r, column=2, **pad)

        # Names
        n = ttk.LabelFrame(root, text="2. Names (one per line, or separated by commas)")
        n.grid(row=1, column=0, sticky="nsew", **pad)
        n.columnconfigure(0, weight=1)
        n.rowconfigure(0, weight=1)
        self.txt = tk.Text(n, height=8, undo=True, wrap="word")
        sb = ttk.Scrollbar(n, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=4)
        sb.grid(row=0, column=1, sticky="ns", pady=4)
        side = ttk.Frame(n)
        side.grid(row=0, column=2, sticky="n", **pad)
        ttk.Button(side, text="Load from file...", command=self._load_names).pack(fill="x", pady=2)
        ttk.Button(side, text="Paste", command=self._paste).pack(fill="x", pady=2)
        ttk.Button(side, text="Clear", command=lambda: self.txt.delete("1.0", "end")).pack(fill="x", pady=2)
        self.lbl_count = ttk.Label(side, text="0 names")
        self.lbl_count.pack(pady=8)
        self.txt.bind("<<Modified>>", self._on_text)

        # Options
        o = ttk.LabelFrame(root, text="3. Options")
        o.grid(row=2, column=0, sticky="ew", **pad)
        o.columnconfigure(1, weight=1)

        ttk.Label(o, text="Match").grid(row=0, column=0, sticky="w", **pad)
        mf = ttk.Frame(o)
        mf.grid(row=0, column=1, sticky="w")
        for text, val in [("Exact name, any extension", "exact"),
                          ("Starts with", "starts"),
                          ("Contains", "contains"),
                          ("Regex", "regex")]:
            ttk.Radiobutton(mf, text=text, value=val, variable=self.v_mode).pack(side="left", padx=4)

        ttk.Label(o, text="Action").grid(row=1, column=0, sticky="w", **pad)
        af = ttk.Frame(o)
        af.grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(af, text="Copy", value="copy", variable=self.v_action).pack(side="left", padx=4)
        ttk.Radiobutton(af, text="Move", value="move", variable=self.v_action).pack(side="left", padx=4)
        ttk.Label(af, text="   If file exists:").pack(side="left")
        ttk.Combobox(af, textvariable=self.v_conflict, state="readonly", width=12,
                     values=["rename", "skip", "overwrite"]).pack(side="left", padx=4)

        ttk.Label(o, text="Only extensions").grid(row=2, column=0, sticky="w", **pad)
        ef = ttk.Frame(o)
        ef.grid(row=2, column=1, sticky="ew")
        ttk.Entry(ef, textvariable=self.v_ext, width=30).pack(side="left", padx=4)
        ttk.Label(ef, text="e.g. jpg, png, pdf  (blank = all)").pack(side="left")

        cf = ttk.Frame(o)
        cf.grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(cf, text="Include subfolders", variable=self.v_sub).pack(side="left", padx=4)
        ttk.Checkbutton(cf, text="Keep subfolder structure in output", variable=self.v_keep).pack(side="left", padx=4)
        ttk.Checkbutton(cf, text="Case sensitive", variable=self.v_case).pack(side="left", padx=4)

        # Log
        lg = ttk.LabelFrame(root, text="Log")
        lg.grid(row=3, column=0, sticky="nsew", **pad)
        lg.columnconfigure(0, weight=1)
        lg.rowconfigure(0, weight=1)
        self.log = tk.Text(lg, height=10, state="disabled", wrap="none")
        lsb = ttk.Scrollbar(lg, command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=4)
        lsb.grid(row=0, column=1, sticky="ns", pady=4)
        self.log.tag_configure("ok", foreground="#1a7f37")
        self.log.tag_configure("warn", foreground="#b35900")
        self.log.tag_configure("err", foreground="#c62828")

        # Bottom bar
        b = ttk.Frame(root)
        b.grid(row=4, column=0, sticky="ew", **pad)
        b.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(b, mode="determinate")
        self.bar.grid(row=0, column=0, sticky="ew", padx=4)
        ttk.Label(b, textvariable=self.v_status).grid(row=1, column=0, sticky="w", padx=4)
        bf = ttk.Frame(b)
        bf.grid(row=0, column=1, rowspan=2, padx=4)
        self.btn_prev = ttk.Button(bf, text="Preview", command=lambda: self._start(True))
        self.btn_run = ttk.Button(bf, text="Run", command=lambda: self._start(False))
        self.btn_cancel = ttk.Button(bf, text="Cancel", command=self.cancel_flag.set, state="disabled")
        self.btn_miss = ttk.Button(bf, text="Save missing...", command=self._save_missing, state="disabled")
        for w in (self.btn_prev, self.btn_run, self.btn_cancel, self.btn_miss):
            w.pack(side="left", padx=2)

    # ------------------------------------------------------------ helpers
    def _browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or None)
        if d:
            var.set(d)

    def _paste(self):
        try:
            self.txt.insert("end", self.clipboard_get())
        except tk.TclError:
            pass

    def _load_names(self):
        p = filedialog.askopenfilename(filetypes=[("Text/CSV", "*.txt *.csv *.lst"), ("All", "*.*")])
        if p:
            try:
                self.txt.insert("end", Path(p).read_text(encoding="utf-8-sig", errors="replace") + "\n")
            except OSError as e:
                messagebox.showerror("Error", str(e))

    def _on_text(self, _=None):
        if self.txt.edit_modified():
            self.lbl_count.config(text=f"{len(parse_names(self.txt.get('1.0', 'end')))} names")
            self.txt.edit_modified(False)

    def _write(self, msg, tag=None):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n", tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def _load_settings(self):
        try:
            d = json.loads(SETTINGS_FILE.read_text())
            self.v_in.set(d.get("in", ""))
            self.v_out.set(d.get("out", ""))
            self.v_mode.set(d.get("mode", "exact"))
            self.v_action.set(d.get("action", "copy"))
            self.v_conflict.set(d.get("conflict", "rename"))
            self.v_case.set(d.get("case", False))
            self.v_sub.set(d.get("sub", True))
            self.v_keep.set(d.get("keep", False))
            self.v_ext.set(d.get("ext", ""))
        except (OSError, ValueError):
            pass

    def _on_close(self):
        d = {"in": self.v_in.get(), "out": self.v_out.get(), "mode": self.v_mode.get(),
             "action": self.v_action.get(), "conflict": self.v_conflict.get(),
             "case": self.v_case.get(), "sub": self.v_sub.get(), "keep": self.v_keep.get(),
             "ext": self.v_ext.get()}
        try:
            SETTINGS_FILE.write_text(json.dumps(d, indent=2))
        except OSError:
            pass
        self.cancel_flag.set()
        self.destroy()

    def _save_missing(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="missing_names.txt")
        if p:
            Path(p).write_text("\n".join(self.missing), encoding="utf-8")

    # ------------------------------------------------------------- run
    def _start(self, dry):
        src, dst = Path(self.v_in.get().strip()), Path(self.v_out.get().strip())
        names = parse_names(self.txt.get("1.0", "end"))
        if not src.is_dir():
            return messagebox.showerror("Input folder", "Please choose a valid input folder.")
        if not self.v_out.get().strip():
            return messagebox.showerror("Output folder", "Please choose an output folder.")
        if not names:
            return messagebox.showerror("Names", "Please enter at least one name.")
        if self.v_mode.get() == "regex":
            try:
                for nm in names:
                    re.compile(nm)
            except re.error as e:
                return messagebox.showerror("Regex error", str(e))
        if src.resolve() == dst.resolve() and not self.v_keep.get():
            return messagebox.showerror("Folders", "Input and output folders must differ.")
        if not dry and self.v_action.get() == "move":
            if not messagebox.askyesno("Confirm move", "Files will be MOVED out of the input folder. Continue?"):
                return

        opts = dict(mode=self.v_mode.get(), action=self.v_action.get(), conflict=self.v_conflict.get(),
                    case=self.v_case.get(), sub=self.v_sub.get(), keep=self.v_keep.get(),
                    exts={("." + e.strip().lstrip(".").lower()) for e in self.v_ext.get().split(",") if e.strip()})
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.cancel_flag.clear()
        self.missing = []
        for b in (self.btn_prev, self.btn_run, self.btn_miss):
            b.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.worker = threading.Thread(target=self._work, args=(src, dst, names, opts, dry), daemon=True)
        self.worker.start()

    def _work(self, src, dst, names, o, dry):
        q = self.q
        try:
            q.put(("status", "Scanning input folder..."))
            dst_r = dst.resolve()
            files = []
            it = src.rglob("*") if o["sub"] else src.glob("*")
            for p in it:
                if not p.is_file():
                    continue
                try:
                    p.resolve().relative_to(dst_r)
                    continue  # skip anything already inside the output folder
                except ValueError:
                    pass
                if o["exts"] and p.suffix.lower() not in o["exts"]:
                    continue
                files.append(p)
            q.put(("log", f"Scanned {len(files)} files.", None))

            norm = (lambda s: s) if o["case"] else (lambda s: s.lower())
            index = {}
            if o["mode"] == "exact":
                # "a.tar.gz" is indexed as "a.tar.gz", "a.tar" and "a"
                for p in files:
                    parts = p.name.split(".")
                    for i in range(len(parts), 0, -1):
                        index.setdefault(norm(".".join(parts[:i])), []).append(p)

            copied = skipped = errors = 0
            total = len(names)
            used = set()
            for i, name in enumerate(names, 1):
                if self.cancel_flag.is_set():
                    q.put(("log", "Cancelled.", "warn"))
                    break
                key = norm(name)
                if o["mode"] == "exact":
                    hits = list(dict.fromkeys(index.get(key, [])))
                elif o["mode"] == "starts":
                    hits = [p for p in files if norm(p.name).startswith(key)]
                elif o["mode"] == "contains":
                    hits = [p for p in files if key in norm(p.name)]
                else:
                    rx = re.compile(name, 0 if o["case"] else re.I)
                    hits = [p for p in files if rx.search(p.name)]

                if not hits:
                    self.missing.append(name)
                    q.put(("log", f"NOT FOUND: {name}", "warn"))
                for p in hits:
                    rel = p.relative_to(src).parent if o["keep"] else Path()
                    target = dst / rel / p.name
                    try:
                        if p in used:
                            q.put(("log", f"Already handled: {p.name}", None))
                            continue
                        used.add(p)
                        if target.exists() and o["conflict"] == "skip":
                            skipped += 1
                            q.put(("log", f"SKIP (exists): {p.name}", "warn"))
                            continue
                        if target.exists() and o["conflict"] == "rename":
                            target = unique_path(target)
                        verb = "WOULD " + o["action"].upper() if dry else o["action"].upper()
                        if not dry:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            if o["action"] == "move":
                                shutil.move(str(p), str(target))
                            else:
                                shutil.copy2(p, target)
                        copied += 1
                        q.put(("log", f"{verb}: {p.name}  ->  {target.relative_to(dst)}", "ok"))
                    except Exception as e:  # keep going on per-file errors
                        errors += 1
                        q.put(("log", f"ERROR {p.name}: {e}", "err"))
                q.put(("progress", i, total))

            summary = (f"{'Preview' if dry else 'Done'}: {copied} file(s) "
                       f"{'matched' if dry else 'processed'}, {skipped} skipped, "
                       f"{len(self.missing)} name(s) not found, {errors} error(s).")
            q.put(("log", summary, "ok" if not errors else "err"))
            q.put(("status", summary))
        except Exception as e:
            q.put(("log", f"FATAL: {e}", "err"))
            q.put(("status", "Failed."))
        finally:
            q.put(("done",))

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "log":
                    self._write(m[1], m[2])
                elif m[0] == "status":
                    self.v_status.set(m[1])
                elif m[0] == "progress":
                    self.bar.config(maximum=m[2], value=m[1])
                    self.v_status.set(f"Processing {m[1]}/{m[2]} names...")
                elif m[0] == "done":
                    self.btn_prev.config(state="normal")
                    self.btn_run.config(state="normal")
                    self.btn_cancel.config(state="disabled")
                    if self.missing:
                        self.btn_miss.config(state="normal")
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
