"""Shared (non-tool) settings: window, appearance, preview, OCR & converters, matching, automation."""
from __future__ import annotations

from .core.schema import Opt, Page

CV_JOB_TITLES = [
    "engineer", "developer", "programmer", "architect", "manager", "director", "analyst", "consultant", "specialist",
    "administrator", "accountant", "designer", "coordinator", "officer", "executive", "assistant", "scientist",
    "researcher", "technician", "supervisor", "recruiter", "intern", "teacher", "nurse", "team lead", "tech lead",
    "product owner", "scrum master", "devops", "head of", "vp", "president", "driver", "operator", "inspector",
    "store keeper", "electrician", "welder", "foreman"]
CV_COMPANY_SUFFIXES = ["Inc", "LLC", "Ltd", "Limited", "Corp", "Corporation", "GmbH", "S.A.", "PLC", "Co.", "Company",
                       "Group", "Technologies", "Solutions", "Holdings", "Bank", "Consulting", "Agency"]
NAME_STOPWORDS = [
    "resume", "résumé", "cv", "curriculum", "vitae", "profile", "summary", "objective", "contact", "experience",
    "education", "skills", "email", "phone", "mobile", "address", "page", "personal", "information", "details", "about",
    "references", "career", "professional", "work", "history", "employment", "languages", "certifications", "projects",
    "linkedin", "github", "www", "http", "https", "tel", "date", "birth", "nationality", "whatsapp", "online", "typing",
    "last seen", "download", "message"]

TOOL_CHOICES_NOTE = "Tools you hide are removed from the sidebar (they keep their settings)."

GENERAL = [
    Opt("window.remember_geometry", True, "Remember window size and position", "bool", section="Window"),
    Opt("window.start_maximized", False, "Start maximised", "bool", section="Window"),
    Opt("window.always_on_top", False, "Keep the main window on top", "bool", section="Window"),
    Opt("window.opacity", 100, "Window opacity (%)", "percent", min=40, max=100, section="Window"),
    Opt("window.show_statusbar", True, "Show the status bar", "bool", section="Window"),
    Opt("window.nav_style", "sidebar", "Navigation", "choice", choices=[("sidebar", "Sidebar (left)"), ("tabs", "Tabs (top)")],
        section="Window"),
    Opt("window.sidebar_compact", False, "Compact sidebar (icons only)", "bool", section="Window"),
    Opt("window.confirm_exit", True, "Ask before closing while work is running", "bool", section="Window"),
    Opt("window.minimize_to_tray", False, "Closing the window hides it to the tray (needs pystray)", "bool", section="Window",
        advanced=True),
    Opt("window.startup_tool", "last", "Open at start", "choice", choices=[("last", "The last tool I used"), ("home", "Home")],
        section="Startup"),
    Opt("window.hidden_tools", [], "Hidden tools", "list", section="Startup",
        help="Tool ids to hide from the sidebar, e.g. screenshot. " + TOOL_CHOICES_NOTE),
    Opt("app.toasts", True, "Show pop-up notifications", "bool", section="Notifications"),
    Opt("app.sound", False, "Beep when a file needs your review", "bool", section="Notifications"),
    Opt("app.keep_log_days", 30, "Keep the log file for (days)", "int", min=1, max=365, section="Housekeeping", advanced=True),
]

APPEARANCE = [
    Opt("ui.theme", "system", "Theme", "choice", choices=[("system", "Follow the system"), ("light", "Light"), ("dark", "Dark")]),
    Opt("ui.accent", "Blue", "Accent colour", "choice",
        choices=["Blue", "Teal", "Green", "Purple", "Orange", "Rose", "Slate"]),
    Opt("ui.density", "normal", "Density", "choice", choices=[("compact", "Compact"), ("normal", "Normal"), ("comfortable", "Comfortable")]),
    Opt("ui.font_family", "", "Font (empty = system default)", "font"),
    Opt("ui.font_size", 10, "Font size", "int", min=8, max=18),
    Opt("ui.scale", 1.0, "UI zoom", "float", min=0.8, max=2.0, step=0.1, help="Scales spacing and widgets (for high-DPI screens)"),
    Opt("ui.high_contrast", False, "High contrast text", "bool"),
    Opt("ui.color_ok", "", "Custom 'OK' colour (hex, optional)", "color", section="Status colours", advanced=True),
    Opt("ui.color_warn", "", "Custom 'warning' colour", "color", section="Status colours", advanced=True),
    Opt("ui.color_err", "", "Custom 'error' colour", "color", section="Status colours", advanced=True),
]

PREVIEW = [
    Opt("preview.enabled", True, "Show the original file next to the data (previews)", "bool"),
    Opt("preview.position", "right", "Preview position in review windows", "choice",
        choices=[("right", "Right of the form"), ("bottom", "Below the form")]),
    Opt("preview.default_zoom", "width", "Default zoom", "choice",
        choices=[("width", "Fit width"), ("page", "Fit whole page")]),
    Opt("preview.max_pages", 8, "Pages shown in a preview", "int", min=1, max=100),
    Opt("preview.thumb_size", 140, "Thumbnail size (px)", "int", min=80, max=300),
    Opt("preview.show_text_tab", True, "Show the 'Extracted text' tab", "bool"),
    Opt("preview.cache_items", 120, "Rendered pages kept in memory", "int", min=20, max=1000, advanced=True),
]

OCR = [
    Opt("ocr.enabled", True, "Read scanned files with OCR", "bool", section="OCR (Tesseract)"),
    Opt("ocr.tesseract_cmd", "", "Tesseract program (empty = find automatically)", "file", section="OCR (Tesseract)"),
    Opt("ocr.languages", "eng", "OCR languages", "str", section="OCR (Tesseract)",
        help="e.g. eng+ara+fra  (the language data must be installed)"),
    Opt("ocr.dpi", 200, "Page render resolution (DPI)", "int", min=72, max=600, section="OCR (Tesseract)"),
    Opt("ocr.min_chars", 40, "OCR a page when it has fewer text characters than", "int", min=0, max=2000, section="OCR (Tesseract)"),
    Opt("ocr.max_pages", 8, "OCR at most this many pages per file (0 = all)", "int", min=0, max=500, section="OCR (Tesseract)"),
    Opt("ocr.autorotate", True, "Auto-rotate sideways / upside-down scans", "bool", section="Automatic fixes"),
    Opt("ocr.deskew", True, "Straighten slightly tilted scans", "bool", section="Automatic fixes"),
    Opt("ocr.mrz", True, "Read passport MRZ lines (second pass)", "bool", section="Automatic fixes"),
    Opt("ocr.libreoffice_cmd", "", "LibreOffice program (empty = find automatically)", "file", section="Converters",
        help="Needed for .doc .ppt .rtf and for previews / merging of Word files"),
]

MATCH = [
    Opt("match.fuzzy_threshold", 80, "Similarity needed for a fuzzy match (%)", "percent", min=50, max=100, section="Fuzzy matching",
        help="Lower = more tolerant of typos and OCR errors, but more false matches"),
    Opt("match.fuzzy_job_titles", False, "Tolerate typos in job-title keywords", "bool", section="Fuzzy matching"),
    Opt("match.company_snap", True, "Replace a detected company by the closest known one", "bool", section="Fuzzy matching"),
    Opt("match.default_country_code", "", "Default country code for phone numbers starting with 0 (e.g. 964)", "str",
        section="Phone numbers"),
    Opt("match.names_list_path", "", "Names list file (.txt / .csv)", "file", section="Known names",
        help="Used to spot duplicates: a name that is (nearly) in the list is flagged. Empty = feature off"),
    Opt("match.name_match_threshold", 85, "A name counts as 'close' from (%)", "percent", min=50, max=100, section="Known names"),
    Opt("match.names_auto_add", True, "Add accepted names to the list automatically", "bool", section="Known names"),
    Opt("match.name_stopwords", NAME_STOPWORDS, "Words that are never part of a person's name", "list", section="Word lists",
        advanced=True),
    Opt("match.job_title_keywords", CV_JOB_TITLES, "Job-title keywords", "list", section="Word lists"),
    Opt("match.company_suffixes", CV_COMPANY_SUFFIXES, "Company suffixes (Inc, LLC ...)", "list", section="Word lists"),
    Opt("match.known_companies", [], "Known companies (matched with typos tolerated)", "list", section="Word lists"),
]

AUTOMATION = [
    Opt("auto.start_watchers", True, "Start automatic watchers when the app opens", "bool", section="Autopilot",
        help="Each tool that can watch a folder starts it (if its own 'watch' option is on)"),
    Opt("auto.notify_done", True, "Notify when automatic work finishes", "bool", section="Autopilot"),
    Opt("auto.stable_seconds", 1.5, "A file must stop changing for (seconds) before it is taken", "float", min=0.5, max=60,
        section="Autopilot"),
    Opt("auto.max_parallel", 2, "Files processed in parallel", "int", min=1, max=8, section="Autopilot", advanced=True),
]


def shared_pages() -> list[Page]:
    return [
        Page("general", "General & window", "⚙", 1, "Window behaviour, startup and notifications.", opts=GENERAL),
        Page("ui", "Appearance", "🎨", 2, "Theme, colours, fonts and density.", opts=APPEARANCE),
        Page("preview", "Previews", "🖼", 3, "How the original file is shown next to extracted data.", opts=PREVIEW),
        Page("ocr", "OCR & converters", "🔎", 4, "Tesseract and LibreOffice - used by every tool.", opts=OCR),
        Page("match", "Matching & word lists", "🎯", 5, "Fuzzy matching, known names and the word lists the extractors use.",
             opts=MATCH),
        Page("auto", "Automation", "🤖", 6, "Autopilot: watch folders and process files without you.", opts=AUTOMATION),
    ]


def all_shared_opts() -> list[Opt]:
    out: list[Opt] = []
    for p in shared_pages():
        out.extend(p.opts)
    return out
