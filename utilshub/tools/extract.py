"""Extract: read text, fields, passports, similarity search, and Arabic ID badges (five tabs)."""
from __future__ import annotations

from .hub import HubTool


class ExtractHub(HubTool):
    id = "extract"
    title = "Extract"
    icon = "🔎"
    description = "Read screenshots, scans, passports, and Arabic ID badges: fields into a table, MRZ checks, find words, smart OCR similarity search, or badge extraction & renaming."
    order = 45
    MEMBERS = (("screenshot", "Screenshot", "Screenshots"),
               ("ocr_match", "OcrMatch", "Find text"),
               ("passport", "PassportTool", "Passports"),
               ("similarity_search", "SimilaritySearch", "Similarity Search"),
               ("badge_extractor", "BadgeExtractor", "Badge Extractor"))
