"""Application settings: dataclasses + JSON persistence + tiny text helpers."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "CV Sorter"
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "cv_sorter.log"

# key -> label of the built-in fields (order = order shown in the pop-up)
BUILTIN_FIELDS = {
    "name": "Full name",
    "email": "Email",
    "phone": "Phone",
    "job_title": "Job title",
    "company": "Company",
}
RESERVED_KEYS = (set(BUILTIN_FIELDS) | {"date", "original", "filename", "stem", "ext"}
                 | {f"{k}_all" for k in BUILTIN_FIELDS})


# --------------------------------------------------------------------------- helpers
def slugify(text: str) -> str:
    """'Years of Exp.' -> 'years_of_exp' (usable as a {placeholder})."""
    return re.sub(r"\W+", "_", text.strip().lower()).strip("_") or "field"


def split_list(text: str) -> list[str]:
    """Split on commas / semicolons / new lines, dropping blanks."""
    return [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()]


def normalise_ext(ext: str) -> str:
    ext = ext.strip().lower()
    return ext if ext.startswith(".") else f".{ext}"


# --------------------------------------------------------------------------- defaults
DEFAULT_EXTENSIONS = [
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".txt", ".rtf",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
]
DEFAULT_JOB_TITLE_KEYWORDS = [
    "engineer", "developer", "programmer", "architect", "manager", "director",
    "analyst", "consultant", "specialist", "administrator", "accountant",
    "designer", "coordinator", "officer", "executive", "assistant", "scientist",
    "researcher", "technician", "supervisor", "recruiter", "intern", "teacher",
    "nurse", "team lead", "tech lead", "product owner", "scrum master",
    "devops", "head of", "vp", "president",
]
DEFAULT_COMPANY_SUFFIXES = [
    "Inc", "LLC", "Ltd", "Limited", "Corp", "Corporation", "GmbH", "S.A.",
    "PLC", "Co.", "Company", "Group", "Technologies", "Solutions", "Holdings",
    "Bank", "Consulting", "Agency",
]
DEFAULT_NAME_STOPWORDS = [
    "resume", "résumé", "cv", "curriculum", "vitae", "profile", "summary",
    "objective", "contact", "experience", "education", "skills", "email",
    "phone", "mobile", "address", "page", "personal", "information", "details",
    "about", "references", "career", "professional", "work", "history",
    "employment", "languages", "certifications", "projects", "linkedin",
    "github", "www", "http", "https", "tel", "date", "birth", "nationality",
]


@dataclass
class CustomField:
    """A user-defined field: either a regex or a list of keywords to look for."""
    name: str
    kind: str = "regex"          # "regex" | "keywords"
    pattern: str = ""            # regex, or keywords separated by , ; or new lines
    ignore_case: bool = True
    multiple: bool = True        # keep all matches (True) or only the first (False)
    fuzzy: bool = True           # keywords only: tolerate typos / OCR errors

    @property
    def key(self) -> str:
        return slugify(self.name)


@dataclass
class LinkTemplate:
    """A URL built from the final file name, e.g. https://.../Archive/{filename}.

    Its name becomes a field (lower_snake_case) usable in clipboard formats.
    """
    name: str
    template: str


@dataclass
class ClipboardFormat:
    """A named arrangement of values to paste elsewhere (see clipboard.py)."""
    name: str
    template: str    # e.g. {name}\t{job_title}\t{phone}   (\t = tab, \n = new line)


def _default_clipboard_formats() -> list[ClipboardFormat]:
    return [
        ClipboardFormat("Excel row", r"{name}\t{job_title}\t{phone}\t{email}\t{company}"),
        ClipboardFormat("One line", "{name} | {job_title} | {phone} | {email}"),
        ClipboardFormat("Contact card", r"{name}\n{job_title} - {company}\n{phone}\n{email}"),
    ]


def _default_custom_fields() -> list[CustomField]:
    return [
        CustomField("LinkedIn", "regex",
                    r"(?:https?://)?(?:[\w-]+\.)?linkedin\.com/in/[\w\-%]+/?", True, False),
        CustomField("Years of experience", "regex",
                    r"(\d{1,2})\+?\s*(?:years|yrs)", True, False),
        CustomField("Skills", "keywords",
                    "Python, Java, JavaScript, SQL, C++, Excel, Docker, AWS, Linux, Git", True, True),
    ]


@dataclass
class Settings:
    # --- folders & files
    watch_folder: str = str(Path.home() / "CV_Inbox")
    output_folder: str = str(Path.home() / "CV_Sorted")
    filename_template: str = "{name}"
    duplicate_policy: str = "number"      # number | overwrite | skip
    file_action: str = "move"             # move | copy
    extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    # --- behaviour
    autostart_watching: bool = True
    process_existing: bool = False
    auto_confirm: bool = False            # skip the pop-up and save straight away
    popup_topmost: bool = True
    stable_seconds: float = 1.5           # file must stop changing for this long
    keep_history: bool = True
    # --- OCR / converters
    ocr_enabled: bool = True
    tesseract_cmd: str = ""
    ocr_languages: str = "eng"            # e.g. "eng+ara+fra"
    ocr_dpi: int = 200
    ocr_min_chars: int = 80               # OCR a PDF page if it has fewer chars
    libreoffice_cmd: str = ""             # for .doc/.ppt/.rtf conversion
    # --- extraction
    builtin_fields: list[str] = field(default_factory=lambda: list(BUILTIN_FIELDS))
    job_title_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_JOB_TITLE_KEYWORDS))
    company_suffixes: list[str] = field(default_factory=lambda: list(DEFAULT_COMPANY_SUFFIXES))
    name_stopwords: list[str] = field(default_factory=lambda: list(DEFAULT_NAME_STOPWORDS))
    custom_fields: list[CustomField] = field(default_factory=_default_custom_fields)
    # --- similarity (fuzzy) matching
    fuzzy_threshold: int = 80             # similarity % required for a fuzzy match
    fuzzy_job_titles: bool = False        # also tolerate typos in job-title keywords
    known_companies: list[str] = field(default_factory=list)  # canonical names, fuzzy-matched
    company_snap: bool = True             # replace a detected company by the closest known one
    # --- clipboard
    clipboard_auto: bool = False          # copy after every "Save & Move"
    clipboard_default: str = "Excel row"
    clipboard_formats: list[ClipboardFormat] = field(default_factory=_default_clipboard_formats)
    # --- links built from the file name (OneDrive / SharePoint / archive URLs)
    link_templates: list[LinkTemplate] = field(default_factory=list)
    # --- list of known names (duplicate-candidate check)
    names_list_path: str = ""             # .txt (one name per line) or .csv; empty = feature off
    name_match_threshold: int = 85        # similarity % that counts as "close to"
    names_auto_add: bool = True           # add accepted names to the list
    # --- duplicates & preview
    duplicates_folder: str = ""           # empty = a "Duplicates" folder inside the output folder
    duplicates_rename: bool = True        # rename duplicates with the template as well
    show_preview: bool = True             # show the original file next to the extracted data
    preview_pages: int = 6
    preview_width: int = 760

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        nested = {"custom_fields": CustomField, "clipboard_formats": ClipboardFormat,
                  "link_templates": LinkTemplate}
        known = {f.name for f in fields(cls)} - set(nested)
        settings = cls(**{k: v for k, v in data.items() if k in known})
        for key, item_cls in nested.items():
            if key in data:
                item_known = {f.name for f in fields(item_cls)}
                setattr(settings, key, [
                    item_cls(**{k: v for k, v in item.items() if k in item_known})
                    for item in data[key]
                ])
        return settings


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    if not path.exists():
        settings = Settings()
        save_settings(settings, path)
        return settings
    try:
        return Settings.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        log.error("Could not read %s (%s) - using defaults", path, exc)
        return Settings()


def save_settings(settings: Settings, path: Path = CONFIG_PATH) -> None:
    path.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8")
