"""Extract: read text, fields and passports out of screenshots, scans and photos (one place, three tabs)."""
from __future__ import annotations

from .hub import HubTool


class ExtractHub(HubTool):
    id = "extract"
    title = "Extract"
    icon = "🔎"
    description = "Read screenshots, scans and passports: fields into a table, MRZ checks, or find words and patterns in any file."
    order = 45
    MEMBERS = (("screenshot", "Screenshot", "Screenshots"),
               ("ocr_match", "OcrMatch", "Find text"),
               ("passport", "PassportTool", "Passports"))
