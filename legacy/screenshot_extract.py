#!/usr/bin/env python3
"""
Screenshot -> spreadsheet row extractor.

HOW IT WORKS
  1. Run this script and leave it open.
  2. Take a screenshot (Win+Shift+S). The image lands in the clipboard.
  3. The script OCRs it, looks up every enabled field (phone, email, job title...),
     and shows a popup where you can fix anything.
  4. Press Esc / Enter: the row is copied to the clipboard, tab-separated.
     Click the first cell in your sheet and paste.

CONTROL PANEL (SHOW_PANEL = True)
  The window that opens with the script has a checkbox for every lookup. Tick or
  untick any of them while the program runs; the change applies to the next
  screenshot, or press "Re-scan last screenshot" to apply it to the last one.
  Closing the panel stops the program.

HOW TO CUSTOMISE (no need to touch anything below the ENGINE line)
  - SECTION 1  general switches and OCR settings
  - SECTION 2  FIELDS: the list of things to look up. Turn each on/off with
               enabled=True/False, or add your own lookup (see the field types).
  - SECTION 3  COLUMN_ORDER: the order of the columns when you paste.

FIELD TYPES (the third argument of Field)
  "phone"        phone number found by regex
  "email"        email address found by regex
  "name"         first line near the top that looks like a person's name
  "keywords"     look for words from a list, e.g. job titles, nationality, skills
                   keywords=["Store Keeper", "Driver"]
                   keywords={"Iraqi": ["iraq", "baghdad"]}   (canonical: [alternatives])
                   all_matches=True  -> join every match with ", " instead of the first only
  "regex"        your own pattern; if it has a (group), the group is used
                   pattern=r"(\\d+)\\s*years"
                   case_sensitive=True  -> "a" and "A" are different (default: ignored)
  "after_label"  text after a label on the same line: "Nationality: Iraqi"
                   labels=["Nationality", "Citizenship"]
  "today"        today's date                          format="%d-%b-%Y"
  "manual"       left empty so you can type it in the popup (notes, comments...)

Stop the program with Ctrl+C in the terminal.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
import tkinter as tk
from pathlib import Path

import pyperclip
import pytesseract
from PIL import Image, ImageGrab, ImageOps, ImageTk

# ======================================================================
# SECTION 1 - GENERAL SETTINGS            (True = on, False = off)
# ======================================================================

# --- Behaviour ---
SHOW_POPUP = True              # compare/edit window after each screenshot
ENABLE_AUTO_COPY = True        # copy the row to the clipboard right after extraction
ENABLE_OTHER_MATCHES = True    # show extra matches (second phone, second title...)
ENABLE_TERMINAL_OUTPUT = True  # print results in the terminal

# --- Control panel ---
SHOW_PANEL = True              # window with an on/off checkbox for every lookup
PANEL_ALWAYS_ON_TOP = True     # keep the panel above other windows
REMEMBER_PANEL_CHOICES = True  # save panel ticks to extractor_settings.json and reuse them next run
                               # (they override enabled=... below; delete the file to reset)

# --- OCR and cleanup ---
ENABLE_PREPROCESS = True       # enlarge small screenshots + boost contrast before OCR
ENABLE_ARABIC_DIGITS = False   # convert Arabic-Indic digits (٠١٢٣) to 0123
ENABLE_PHONE_NORMALIZE = True  # keep digits only, add country code to numbers starting with 0
ENABLE_EMAIL_REPAIR = True     # fix spaces OCR adds around "@"

# --- Settings ---
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"  # "" if it is on PATH
OCR_LANG = "eng"               # "eng+ara" if screenshots contain Arabic and Arabic data is installed
DEFAULT_COUNTRY_CODE = ""      # numbers starting with 0 get this prefix, e.g. "964". "" = off
DATE_FORMAT = "%d-%b-%Y"       # default for "today" fields, e.g. 10-Oct-2026
POLL_MS = 500                  # how often the clipboard is checked
PREVIEW_SIZE = (640, 360)      # max size of the screenshot preview in the popup


# ======================================================================
# SECTION 2 - FIELDS: WHAT TO LOOK UP
# ======================================================================
# Field(key, label, type, enabled=True/False, ...options for that type)
#   key     short unique id, used in COLUMN_ORDER below
#   label   what you see in the popup
# Set enabled=False to switch a lookup off without deleting it.
# Disabled fields are not searched, not shown and not pasted.

class Field:
    def __init__(self, key: str, label: str, kind: str, enabled: bool = True, **options):
        self.key = key
        self.label = label
        self.kind = kind
        self.enabled = enabled
        self.options = options


FIELDS = [
    # ---- Your current fields ----
    Field("date",  "Date",      "today",    enabled=False),
    Field("name",  "Name",      "name",     enabled=False),
    Field("title", "Job Title", "keywords", enabled=False, keywords=[
        "Machine Operator", "HSE Inspector", "General Worker", "Mechanical Engineer",
        "Store Keeper", "Electrical Engineer", "Electrical Technician",
    ]),
    Field("phone", "Phone",     "phone",    enabled=True),
    Field("email", "Email",     "email",    enabled=True),
    Field("iban",  "IBAN",      "regex",    enabled=False, case_sensitive=True,
          pattern=r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b"),
    Field("notes", "Notes",     "manual",   enabled=False),

    # ---- Ready-made extra lookups (all off; switch on and edit as you like) ----
    Field("nationality", "Nationality", "keywords", enabled=False, keywords={
        "Iraqi": ["iraq"], "Indian": ["india"], "Egyptian": ["egypt"],
        "Pakistani": ["pakistan"], "Bangladeshi": ["bangladesh"], "Nepali": ["nepal"],
    }),
    Field("gender", "Gender", "keywords", enabled=False, keywords=["Male", "Female"]),
    Field("age", "Age", "regex", enabled=False,
          pattern=r"\bage\s*[:\-]?\s*(\d{2})\b"),
    Field("experience", "Experience (yrs)", "regex", enabled=False,
          pattern=r"(\d{1,2})\+?\s*(?:years?|yrs?)\b"),
    Field("education", "Education", "keywords", enabled=False, keywords={
        "PhD": ["phd", "doctorate"], "Master": ["master", "msc"],
        "Bachelor": ["bachelor", "bsc", "b.sc"], "Diploma": ["diploma"],
    }),
    Field("location", "Location", "after_label", enabled=False,
          labels=["Location", "Current Location", "City", "Address"]),
    Field("id_number", "ID Number", "after_label", enabled=False,
          labels=["National ID", "ID No", "ID Number", "Passport No"]),
    Field("salary", "Expected Salary", "after_label", enabled=False,
          labels=["Expected Salary", "Salary Expectation", "Salary"]),
    Field("linkedin", "LinkedIn", "regex", enabled=False,
          pattern=r"((?:https?://)?(?:www\.)?linkedin\.com/\S+)"),
    Field("skills", "Skills", "keywords", enabled=False, all_matches=True,
          keywords=["AutoCAD", "Excel", "PLC", "Welding", "Forklift", "Safety", "SAP"]),
]


# ======================================================================
# SECTION 3 - PASTE ORDER
# ======================================================================
# Keys from FIELDS above, in the order of your sheet's columns.
# Switched-off fields are skipped. A field missing from this list is pasted at
# the end (you get a warning at start-up if it is switched on).
COLUMN_ORDER = ["name", "title", "phone", "email", "iban", "date", "notes"]

# Words that disqualify a line from being treated as a person's name ("name" fields).
NOT_A_NAME = [
    "cv", "resume", "curriculum", "vitae", "whatsapp", "online", "typing",
    "last seen", "email", "phone", "mobile", "address", "profile", "objective",
    "experience", "education", "skills", "page", "download", "message",
]


# ======================================================================
# ENGINE - you normally do not need to edit anything below this line
# ======================================================================

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(\+?\d[\d\s\-().]{7,}\d)")
DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


# ----------------------------- OCR -----------------------------
def ocr(img: Image.Image) -> str:
    img = img.convert("L")
    if ENABLE_PREPROCESS:
        w, h = img.size
        if w < 1600:  # small screenshots OCR better when enlarged
            img = img.resize((w * 2, h * 2), Image.LANCZOS)
        img = ImageOps.autocontrast(img)
    text = pytesseract.image_to_string(img, lang=OCR_LANG)
    if ENABLE_ARABIC_DIGITS:
        text = text.translate(DIGIT_MAP)
    return text


def clipboard_image():
    try:
        data = ImageGrab.grabclipboard()
    except Exception:
        return None
    if isinstance(data, Image.Image):
        return data
    if isinstance(data, list) and data:  # copied image file(s)
        try:
            return Image.open(data[0])
        except Exception:
            return None
    return None


# ----------------------------- extractors -----------------------------
# Each extractor takes (text, field) and returns a list of candidates, best first.

def normalize_phone(raw: str) -> str:
    if not ENABLE_PHONE_NORMALIZE:
        return re.sub(r"\s+", " ", raw).strip()
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if DEFAULT_COUNTRY_CODE and digits.startswith("0"):
        digits = DEFAULT_COUNTRY_CODE + digits[1:]
    return digits


def ex_phone(text: str, f: Field) -> list:
    found = []
    for m in PHONE_RE.finditer(text):
        n = normalize_phone(m.group(1))
        if 9 <= len(re.sub(r"\D", "", n)) <= 15 and n not in found:
            found.append(n)
    return found


def ex_email(text: str, f: Field) -> list:
    if ENABLE_EMAIL_REPAIR:
        text = re.sub(r"\s*@\s*", "@", text)  # OCR often adds spaces around @
    found = []
    for m in EMAIL_RE.finditer(text):
        e = m.group(0).lower().strip(".")
        if e not in found:
            found.append(e)
    return found


def ex_name(text: str, f: Field) -> list:
    """First line near the top that looks like a person's name (2-4 words, letters only)."""
    for line in text.splitlines()[:15]:
        line = line.strip(" \t-_:|•*")
        if not line or len(line) > 40:
            continue
        if any(w in line.lower() for w in NOT_A_NAME):
            continue
        if any(ch.isdigit() for ch in line) or "@" in line:
            continue
        if not 2 <= len(line.split()) <= 4:
            continue
        if sum(ch.isalpha() or ch.isspace() for ch in line) / len(line) < 0.9:
            continue
        return [line.title() if line.isascii() else line]
    return []


def _keyword_map(keywords) -> dict:
    """Accept a list or a {canonical: [alternatives]} dict; return {canonical: [variants]}."""
    if isinstance(keywords, dict):
        return {k: [k, *v] for k, v in keywords.items()}
    return {k: [k] for k in keywords}


def ex_keywords(text: str, f: Field) -> list:
    """Words from the list found in the text. Earliest first; longer wins ties."""
    low = text.lower()
    hits = []
    for canonical, variants in _keyword_map(f.options["keywords"]).items():
        starts = []
        for v in variants:
            v = v.lower()
            pattern = rf"\b{re.escape(v)}\b" if v.isascii() else re.escape(v)
            m = re.search(pattern, low)
            if m:
                starts.append(m.start())
        if starts:
            hits.append((min(starts), -len(canonical), canonical))
    hits.sort()
    results = []
    for _, _, name in hits:
        # skip a shorter word inside one already picked ("Engineer" in "Mechanical Engineer")
        if not any(name.lower() in r.lower() for r in results):
            results.append(name)
    return results


def ex_regex(text: str, f: Field) -> list:
    flags = 0 if f.options.get("case_sensitive") else re.IGNORECASE
    rx = re.compile(f.options["pattern"], flags)
    found = []
    for m in rx.finditer(text):
        value = (m.group(1) if rx.groups else m.group(0)).strip()
        if value and value not in found:
            found.append(value)
    return found


def ex_after_label(text: str, f: Field) -> list:
    labels = "|".join(re.escape(x) for x in f.options["labels"])
    rx = re.compile(rf"(?:{labels})\s*[:\-–—]\s*([^\n]+)", re.IGNORECASE)
    max_len = f.options.get("max_len", 60)
    found = []
    for m in rx.finditer(text):
        value = m.group(1).strip()[:max_len]
        if value and value not in found:
            found.append(value)
    return found


def ex_today(text: str, f: Field) -> list:
    return [datetime.datetime.now().strftime(f.options.get("format", DATE_FORMAT))]


def ex_manual(text: str, f: Field) -> list:
    return []


EXTRACTORS = {
    "phone": ex_phone,
    "email": ex_email,
    "name": ex_name,
    "keywords": ex_keywords,
    "regex": ex_regex,
    "after_label": ex_after_label,
    "today": ex_today,
    "manual": ex_manual,
}
# Fields that are not expected to be "found" in the screenshot (no red warning when empty)
NOT_SEARCHED = {"today", "manual"}


# ----------------------------- config checks -----------------------------
def check_config():
    """Return (errors, warnings, every field in paste order, switched on or off)."""
    errors, warnings = [], []
    by_key = {}
    for f in FIELDS:
        if f.key in by_key:
            errors.append(f"Duplicate field key '{f.key}'.")
        by_key[f.key] = f
        if f.kind not in EXTRACTORS:
            errors.append(f"Field '{f.key}': unknown type '{f.kind}'.")
            continue
        if f.kind == "keywords" and not f.options.get("keywords"):
            errors.append(f"Field '{f.key}': needs keywords=[...].")
        if f.kind == "after_label" and not f.options.get("labels"):
            errors.append(f"Field '{f.key}': needs labels=[...].")
        if f.kind == "regex":
            try:
                re.compile(f.options.get("pattern", ""))
            except re.error as e:
                errors.append(f"Field '{f.key}': bad regex ({e}).")
            if not f.options.get("pattern"):
                errors.append(f"Field '{f.key}': needs pattern=r\"...\".")

    for k in COLUMN_ORDER:
        if k not in by_key:
            warnings.append(f"COLUMN_ORDER has '{k}' but there is no such field. Ignored.")

    listed = [by_key[k] for k in COLUMN_ORDER if k in by_key]
    extra = [f for f in FIELDS if f.key not in COLUMN_ORDER]
    for f in extra:
        if f.enabled:
            warnings.append(f"Field '{f.key}' is enabled but missing from COLUMN_ORDER. Added at the end.")
    return errors, warnings, listed + extra


def check_tesseract() -> bool:
    if TESSERACT_PATH:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        print("Tesseract OCR was not found.")
        print("Install it (UB Mannheim installer for Windows) and check TESSERACT_PATH in SECTION 1.")
        return False


# ----------------------------- extraction -----------------------------
def extract_all(text: str, fields: list) -> tuple:
    """Return (values, others): values[key] = best text, others[key] = extra matches."""
    values, others = {}, {}
    for f in fields:
        candidates = EXTRACTORS[f.kind](text, f)
        if f.options.get("all_matches"):
            values[f.key], others[f.key] = ", ".join(candidates), []
        else:
            values[f.key] = candidates[0] if candidates else ""
            others[f.key] = candidates[1:]
    return values, others


def row_text(values: dict, fields: list) -> str:
    cells = (re.sub(r"[\t\r\n]+", " ", values.get(f.key, "")).strip() for f in fields)
    return "\t".join(cells)


# ----------------------------- popup -----------------------------
class Popup:
    def __init__(self, root):
        self.root = root
        self.fields = []  # set on every show(), because the panel can change them
        self.top = None

    def close(self, _event=None):
        if self.top is not None:
            try:
                self.top.destroy()
            except tk.TclError:
                pass
            self.top = None

    def show(self, img, fields: list, values: dict, others: dict):
        self.fields = fields
        self.close()  # replace any popup that is still open
        top = tk.Toplevel(self.root)
        self.top = top
        top.title("Screenshot vs extracted result")
        top.attributes("-topmost", True)
        top.resizable(False, False)

        # Screenshot preview
        preview = img.copy().convert("RGB")
        preview.thumbnail(PREVIEW_SIZE)
        photo = ImageTk.PhotoImage(preview)
        pic = tk.Label(top, image=photo, relief="solid", borderwidth=1)
        pic.image = photo  # keep a reference so it is not garbage collected
        pic.pack(padx=10, pady=(10, 6))

        # Editable boxes, one per enabled field, in paste order
        form = tk.Frame(top)
        form.pack(padx=10, pady=4, fill="x")
        label_width = max(9, max(len(f.label) for f in self.fields))
        vars_, first_entry = {}, None
        for row, f in enumerate(self.fields):
            vars_[f.key] = tk.StringVar(value=values.get(f.key, ""))
            tk.Label(form, text=f.label, width=label_width, anchor="w").grid(row=row, column=0, pady=2)
            entry = tk.Entry(form, textvariable=vars_[f.key], width=38, font=("Consolas", 12))
            entry.grid(row=row, column=1, pady=2)
            if first_entry is None:
                first_entry = entry

        # Red notes: what was missing, and other matches
        notes = []
        for f in self.fields:
            if f.kind not in NOT_SEARCHED and not values.get(f.key):
                notes.append(f"No {f.label.lower()} found, type it in")
        if ENABLE_OTHER_MATCHES:
            for f in self.fields:
                if others.get(f.key):
                    notes.append(f"Other {f.label.lower()} matches: " + ", ".join(others[f.key]))
        if notes:
            tk.Label(top, text="\n".join(notes), fg="#b00020", justify="left",
                     wraplength=PREVIEW_SIZE[0]).pack(padx=10, anchor="w")

        tk.Label(top, text="Edit the boxes if needed.  Esc / Enter = save to clipboard and close",
                 fg="gray").pack(pady=(4, 10))

        def save_and_close(_event=None):
            edited = {k: v.get().strip() for k, v in vars_.items()}
            if any(edited.values()):
                pyperclip.copy(row_text(edited, self.fields))
                if ENABLE_TERMINAL_OUTPUT:
                    print("Saved: " + " | ".join(edited[f.key] or "-" for f in self.fields))
            self.close()

        top.bind("<Escape>", save_and_close)
        top.bind("<Return>", save_and_close)
        top.protocol("WM_DELETE_WINDOW", save_and_close)  # the X button saves too

        # Place at the top-right of the screen
        top.update_idletasks()
        x = top.winfo_screenwidth() - top.winfo_width() - 20
        top.geometry(f"+{max(x, 0)}+40")
        top.lift()
        top.focus_force()
        if first_entry is not None:
            first_entry.focus_set()


# ----------------------------- remembered panel choices -----------------------------
SETTINGS_FILE = Path(__file__).with_name("extractor_settings.json")


def load_choices(fields: list):
    """Apply the choices saved by the panel last time (needs REMEMBER_PANEL_CHOICES)."""
    global SHOW_POPUP, ENABLE_AUTO_COPY
    if not REMEMBER_PANEL_CHOICES or not SETTINGS_FILE.exists():
        return
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"Could not read {SETTINGS_FILE.name}, using the settings in the code.")
        return
    saved = data.get("fields", {})
    for f in fields:
        if f.key in saved:
            f.enabled = bool(saved[f.key])
    SHOW_POPUP = bool(data.get("show_popup", SHOW_POPUP))
    ENABLE_AUTO_COPY = bool(data.get("auto_copy", ENABLE_AUTO_COPY))


def save_choices(fields: list):
    if not REMEMBER_PANEL_CHOICES:
        return
    data = {
        "fields": {f.key: f.enabled for f in fields},
        "show_popup": SHOW_POPUP,
        "auto_copy": ENABLE_AUTO_COPY,
    }
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"Could not save panel choices: {e}")


# ----------------------------- control panel -----------------------------
class Panel:
    """The main window: one checkbox per lookup, plus the behaviour switches."""

    ROWS_PER_COLUMN = 8

    def __init__(self, root, fields: list, on_rescan, on_quit):
        self.root = root
        self.fields = fields
        root.title("Screenshot extractor")
        root.resizable(False, False)
        if PANEL_ALWAYS_ON_TOP:
            root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", on_quit)  # closing this window stops the program

        tk.Label(root, text="Look up", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=10, pady=(10, 2))
        grid = tk.Frame(root)
        grid.pack(padx=10, anchor="w")
        self.field_vars = {}
        for i, f in enumerate(fields):
            var = tk.BooleanVar(value=f.enabled)
            self.field_vars[f.key] = var
            tk.Checkbutton(
                grid, text=f.label, variable=var, anchor="w", width=18,
                command=lambda f=f, var=var: self.toggle_field(f, var),
            ).grid(row=i % self.ROWS_PER_COLUMN, column=i // self.ROWS_PER_COLUMN, sticky="w")

        tk.Frame(root, height=1, bg="#bbbbbb").pack(fill="x", padx=10, pady=8)

        self.popup_var = tk.BooleanVar(value=SHOW_POPUP)
        self.copy_var = tk.BooleanVar(value=ENABLE_AUTO_COPY)
        tk.Checkbutton(root, text="Show compare popup", variable=self.popup_var,
                       command=self.toggle_popup).pack(anchor="w", padx=10)
        tk.Checkbutton(root, text="Copy result automatically", variable=self.copy_var,
                       command=self.toggle_copy).pack(anchor="w", padx=10)

        tk.Button(root, text="Re-scan last screenshot", command=on_rescan).pack(
            fill="x", padx=10, pady=(8, 2))
        self.status = tk.Label(root, text="Waiting for a screenshot...", fg="gray",
                               anchor="w", justify="left", wraplength=260)
        self.status.pack(fill="x", padx=10, pady=(2, 10))

        root.geometry("+20+40")  # top-left; the result popup opens at the top-right

    def toggle_field(self, field, var):
        field.enabled = var.get()
        save_choices(self.fields)

    def toggle_popup(self):
        global SHOW_POPUP
        SHOW_POPUP = self.popup_var.get()
        save_choices(self.fields)

    def toggle_copy(self):
        global ENABLE_AUTO_COPY
        ENABLE_AUTO_COPY = self.copy_var.get()
        save_choices(self.fields)

    def set_status(self, text: str):
        self.status.config(text=text)


# ----------------------------- main loop -----------------------------
class App:
    def __init__(self, fields: list):
        self.fields = fields  # every field in paste order, switched on or off
        self.root = tk.Tk()
        self.panel = None
        if SHOW_PANEL:
            self.panel = Panel(self.root, fields, self.rescan, self.quit)
        else:
            self.root.withdraw()  # no panel: only the result popup is shown
        self.popup = Popup(self.root)
        self.last_hash = None
        self.last_img = None
        self.last_text = ""

    def active_fields(self) -> list:
        return [f for f in self.fields if f.enabled]

    def say(self, text: str):
        if self.panel is not None:
            self.panel.set_status(text)

    def quit(self):
        self.root.destroy()

    def tick(self):
        img = clipboard_image()
        if img is not None:
            h = hashlib.md5(img.tobytes()).hexdigest()
            if h != self.last_hash:
                self.last_hash = h
                self.handle(img)
        self.root.after(POLL_MS, self.tick)

    def handle(self, img):
        """A new screenshot: run OCR once, then look up the fields."""
        try:
            text = ocr(img)
        except Exception as e:
            print(f"OCR failed: {e}")
            self.say(f"OCR failed: {e}")
            return
        self.last_img, self.last_text = img, text
        self.process(img, text)

    def rescan(self):
        """Look up the fields again on the last screenshot, using the current checkboxes."""
        if self.last_img is None:
            self.say("No screenshot yet.")
            return
        self.process(self.last_img, self.last_text)

    def process(self, img, text: str):
        fields = self.active_fields()
        if not fields:
            print("Every lookup is switched off. Tick at least one in the panel.")
            self.say("Everything is switched off. Tick a lookup above.")
            return
        values, others = extract_all(text, fields)

        if ENABLE_TERMINAL_OUTPUT:
            width = max(len(f.label) for f in fields)
            print("-" * 40)
            for f in fields:
                print(f"{f.label:<{width}} : {values[f.key] or '(not found)'}")
                if ENABLE_OTHER_MATCHES and others[f.key]:
                    print(f"{'':<{width}}   other: {', '.join(others[f.key])}")

        searched = [f for f in fields if f.kind not in NOT_SEARCHED]
        hits = sum(1 for f in searched if values[f.key])
        if searched:
            self.say(f"Found {hits} of {len(searched)} lookups.")
        else:
            self.say("Done.")

        found = hits > 0
        if found and ENABLE_AUTO_COPY:
            pyperclip.copy(row_text(values, fields))
            if ENABLE_TERMINAL_OUTPUT:
                print("Copied to clipboard. Paste into the sheet.")
        elif not found and ENABLE_TERMINAL_OUTPUT:
            print("Nothing found. Try a larger or clearer screenshot.")

        if SHOW_POPUP:
            self.popup.show(img, fields, values, others)

    def run(self):
        on = [f.label for f in self.active_fields()]
        print(f"Looking up: {', '.join(on) if on else '(nothing ticked yet)'}")
        print("Waiting for a screenshot... (Ctrl+C to stop)")
        self.root.after(POLL_MS, self.tick)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            print("\nStopped.")


def main():
    errors, warnings, fields = check_config()
    for w in warnings:
        print(f"Warning: {w}")
    if errors:
        print("Fix these problems in the settings first:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    if not check_tesseract():
        sys.exit(1)
    load_choices(fields)
    if not SHOW_PANEL and not any(f.enabled for f in fields):
        print("All fields are switched off. Set enabled=True on a field, or SHOW_PANEL = True.")
        sys.exit(1)
    App(fields).run()


if __name__ == "__main__":
    main()
