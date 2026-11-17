"""Clean-up: find duplicates and fix scans in the same folders."""
from __future__ import annotations

from .hub import HubTool


class CleanupHub(HubTool):
    id = "cleanup"
    title = "Clean-up"
    icon = "🧼"
    description = "Find identical files and look-alike scans, and straighten, trim and clean scanned pages. Originals are never touched."
    order = 35
    MEMBERS = (("duplicates", "DuplicateFinder", "Duplicates"),
               ("scan_cleaner", "ScanCleaner", "Scans"))
