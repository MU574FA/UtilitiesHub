"""Unified Configuration & Settings Persistence for OmniDocs Studio."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).resolve().parent.parent.parent / "omnidocs_config.json"


@dataclass
class WindowSettings:
    width: int = 1200
    height: int = 800
    x: int | None = None
    y: int | None = None
    maximized: bool = False
    always_on_top: bool = False


@dataclass
class ThemeSettings:
    theme: str = "dark"           # "dark", "light", "system"
    accent: str = "emerald"       # "emerald", "blue", "purple", "amber", "crimson"
    font_scale: str = "medium"    # "small", "medium", "large", "xl"


@dataclass
class PreviewSettings:
    thumb_size: int = 120         # 80, 120, 160, 220
    preview_dpi: int = 150        # 100, 150, 200, 300
    render_mode: str = "image"    # "image", "text", "both"
    show_page_numbers: bool = True
    page_fit: str = "fit_width"   # "fit_width", "fit_page", "actual"


@dataclass
class AutomationSettings:
    auto_process_cvs: bool = False
    auto_deskew: bool = True
    auto_rotate: bool = True
    auto_copy_extracted: bool = True
    auto_sync_on_load: bool = False
    auto_open_merged: bool = False
    auto_add_candidates_to_registry: bool = True


@dataclass
class GlobalSettings:
    tesseract_path: str = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    ocr_lang: str = "eng"
    enable_arabic_ocr: bool = True
    default_country_code: str = ""
    date_format: str = "%d-%b-%Y"
    temp_dir: str = ""


@dataclass
class AppConfig:
    window: WindowSettings = field(default_factory=WindowSettings)
    theme: ThemeSettings = field(default_factory=ThemeSettings)
    preview: PreviewSettings = field(default_factory=PreviewSettings)
    automation: AutomationSettings = field(default_factory=AutomationSettings)
    general: GlobalSettings = field(default_factory=GlobalSettings)
    
    # Tool-specific configurations (stored as dicts for flexibility)
    cvsorter: dict[str, Any] = field(default_factory=lambda: {
        "watch_folder": "",
        "output_folder": "",
        "duplicates_folder": "",
        "names_list_path": "",
        "filename_template": "{name} - {job_title}",
        "file_action": "copy",
        "duplicate_policy": "number",
        "fuzzy_threshold": 85,
        "stable_seconds": 1.5,
    })
    
    docpacker: dict[str, Any] = field(default_factory=lambda: {
        "watch_folder": "",
        "output_folder": "",
        "target_size_mb": 10.0,
        "feat_autorotate": True,
        "feat_deskew": True,
        "feat_arabic": True,
        "feat_compress": True,
    })
    
    merger: dict[str, Any] = field(default_factory=lambda: {
        "img_mode": "a4",        # "a4" (fit with margins) or "own" (photo canvas)
        "margin_pt": 28,
        "output_filename": "Merged_Document.pdf",
    })
    
    picker: dict[str, Any] = field(default_factory=lambda: {
        "input_folder": "",
        "output_folder": "",
        "mode": "exact",
        "action": "copy",
        "conflict": "rename",
        "case_sensitive": False,
        "subfolders": True,
        "keep_structure": False,
        "extensions": "",
    })
    
    matcher: dict[str, Any] = field(default_factory=lambda: {
        "threshold": 80,
        "top_n": 3,
        "normalize_arabic": True,
        "strip_diacritics": True,
    })
    
    sync: dict[str, Any] = field(default_factory=lambda: {
        "ignore_case_and_spaces": True,
        "add_new_rows": True,
        "keep_old_on_blank": True,
        "add_new_columns": True,
        "include_status_column": True,
    })
    
    screenshot: dict[str, Any] = field(default_factory=lambda: {
        "poll_ms": 500,
        "show_popup": True,
        "auto_copy": True,
        "preprocess": True,
        "arabic_digits": False,
        "phone_normalize": True,
        "email_repair": True,
    })

    rules: dict[str, Any] = field(default_factory=dict)

    def save(self, path: Path | None = None) -> None:
        target = path or CONFIG_FILE
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = asdict(self)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            log.info("Saved configuration to %s", target)
        except Exception as exc:
            log.error("Failed to save config to %s: %s", target, exc)

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        target = path or CONFIG_FILE
        if not target.exists():
            instance = cls()
            instance.save(target)
            return instance
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Map dataclass sub-fields safely
            cfg = cls()
            if "window" in data:
                cfg.window = WindowSettings(**{k: v for k, v in data["window"].items() if hasattr(WindowSettings, k)})
            if "theme" in data:
                cfg.theme = ThemeSettings(**{k: v for k, v in data["theme"].items() if hasattr(ThemeSettings, k)})
            if "preview" in data:
                cfg.preview = PreviewSettings(**{k: v for k, v in data["preview"].items() if hasattr(PreviewSettings, k)})
            if "automation" in data:
                cfg.automation = AutomationSettings(**{k: v for k, v in data["automation"].items() if hasattr(AutomationSettings, k)})
            if "general" in data:
                cfg.general = GlobalSettings(**{k: v for k, v in data["general"].items() if hasattr(GlobalSettings, k)})
            
            for key in ("cvsorter", "docpacker", "merger", "picker", "matcher", "sync", "screenshot", "rules"):
                if key in data and isinstance(data[key], dict):
                    getattr(cfg, key).update(data[key])
            return cfg
        except Exception as exc:
            log.warning("Failed to load config from %s (%s), returning defaults", target, exc)
            return cls()

