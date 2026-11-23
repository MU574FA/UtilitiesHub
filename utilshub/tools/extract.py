"""Extract: read text, fields, passports, and run smart cached OCR similarity search (four tabs)."""
from __future__ import annotations

from .hub import HubTool


class ExtractHub(HubTool):
    id = "extract"
    title = "Extract"
    icon = "🔎"
    description = "Read screenshots, scans and passports: fields into a table, MRZ checks, find words, or smart cached OCR similarity search."
    order = 45
    MEMBERS = (("screenshot", "Screenshot", "Screenshots"),
               ("ocr_match", "OcrMatch", "Find text"),
               ("passport", "PassportTool", "Passports"),
               ("similarity_search", "SimilaritySearch", "Similarity Search"))
