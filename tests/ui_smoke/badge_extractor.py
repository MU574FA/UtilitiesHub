"""Headless UI logic smoke test for ExtractHub -> Badge Extractor tab (fake tkinter)."""
from __future__ import annotations

import logging
import sys
import tempfile
import time
from pathlib import Path

import openpyxl
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))
logging.disable(logging.CRITICAL)

import faketk  # noqa: E402
faketk.install()
faketk.Widget.winfo_rootx = lambda self: 0
faketk.Widget.winfo_rooty = lambda self: 0
faketk.Widget.winfo_width = lambda self: 1000
faketk.Widget.winfo_height = lambda self: 700
faketk.Widget.winfo_reqheight = lambda self: 300
import fakeapp  # noqa: E402
from utilshub.core import badges as B  # noqa: E402
from utilshub.tools.extract import ExtractHub  # noqa: E402
from utilshub.ui.folders_panel import feature_path_keys  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, cond: bool, extra: object = "") -> None:
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))


class _DummyRenderer:
    def page_sizes(self, path, max_pages=None):
        return [(600.0, 380.0)]

    def render(self, path, page, zoom=1.0, rotation=0, fix=None):
        return Image.new("RGB", (300, 190), "white")


tmp = Path(tempfile.mkdtemp())
app = fakeapp.FakeApp(tmp)
app.renderer = _DummyRenderer()
app.settings.register(ExtractHub.options())
hub = ExtractHub(app)
app.tools[hub.id] = hub
app.sorted_tools = lambda: list(app.tools.values())

member_ids = [m.id for m in hub.members()]
check(
    "ExtractHub includes badge_extractor as last tab",
    member_ids[-1] == "badge_extractor" and "passport" in member_ids and "similarity_search" in member_ids,
    member_ids,
)

# Settings > Folders & paths discovers badge_extractor input & output folders
keys = feature_path_keys(app)
check(
    "Folders & paths panel includes badge_extractor input/output",
    "badge_extractor.input_folder" in keys and "badge_extractor.output_folder" in keys,
    keys,
)

# Build hub and open Badge Extractor tab
hub.ensure_built(app.root)
hub.on_show()
hub.select("badge_extractor")
be = hub.member("badge_extractor")
check("BadgeExtractor tab builds cleanly", be is not None and be.built)

# Create input/output folders and sample Excel sheet with multiple similar names
in_dir = tmp / "badges_in"
out_dir = tmp / "badges_out"
in_dir.mkdir()
out_dir.mkdir()

wb = openpyxl.Workbook()
ws = wb.active
ws.append(["Full Name", "Department", "Badge ID"])
ws.append(["Firoj Khan", "Slaughterhouse Cleaning", "64847"])
ws.append(["Firoz Khan", "Slaughterhouse Packing", "64899"])
ws.append(["Firoj Ahmed Khan", "Maintenance", "65100"])
sheet_path = tmp / "candidates.xlsx"
wb.save(sheet_path)

app.settings.set("badge_extractor.input_folder", str(in_dir))
app.settings.set("badge_extractor.output_folder", str(out_dir))
app.settings.set("badge_extractor.info_sheet", str(sheet_path))
app.settings.set("badge_extractor.quiet_seconds", 0.0)
faketk.flush()
check("Settings sync updates folder vars on tab", be.in_var.get() == str(in_dir) and be.out_var.get() == str(out_dir))

# Stub extract_badge to simulate Arabic OCR output deterministically
orig_extract = B.extract_badge


def fake_extract(path, cfg, languages="ara+eng", crop_mode="auto", rotation=0):
    return B.parse_badge_text("فيروج خان\nص-المجزر ( عمال التنظيف)\n64847")


B.extract_badge = fake_extract
try:
    # Start watching and drop a badge image into input folder
    be.start_watching()
    check("Watcher starts and reports auto_running", be.auto_running() and be.can_automate())

    badge_img = in_dir / "IMG_001.jpg"
    Image.new("RGB", (600, 380), "white").save(badge_img)

    t0 = time.monotonic()
    while time.monotonic() - t0 < 6 and not be.items:
        time.sleep(0.1)
        faketk.flush()
    be.stop_watching()

    check("Dropping badge into watched folder extracts & queues it", len(be.items) == 1, len(be.items))
    it = next(iter(be.items.values()))
    check("Fields extracted & transliterated into review form",
          be.ar_var.get() == "فيروج خان" and be.en_var.get() == "Firoj Khan"
          and be.job_var.get() == "ص-المجزر ( عمال التنظيف)" and be.badge_var.get() == "64847",
          (be.ar_var.get(), be.en_var.get(), be.job_var.get(), be.badge_var.get()))
    check("Multiple similar Excel matches surfaced in inline table",
          len(it.matches) == 3 and len(be.match_tree.get_children()) == 3,
          [(m.name_value, m.percent) for m in it.matches])
    check("Live filename preview renders default template",
          be.preview_lbl._kw.get("text") == "فيروج خان-Firoj Khan-64847-ص-المجزر ( عمال التنظيف).jpg",
          be.preview_lbl._kw.get("text"))

    # Customize filename template and verify preview updates
    be.tpl_var.set("{badge_no}-{en_name}")
    check("Custom filename template updates preview live",
          be.preview_lbl._kw.get("text") == "64847-Firoj Khan.jpg",
          be.preview_lbl._kw.get("text"))

    # Apply a different similar candidate match (Firoz Khan)
    firoz = next(m for m in it.matches if m.name_value == "Firoz Khan")
    be.apply_selected_match(firoz)
    check("Applying a similar candidate updates English name and filename preview",
          be.en_var.get() == "Firoz Khan" and be.preview_lbl._kw.get("text") == "64847-Firoz Khan.jpg",
          (be.en_var.get(), be.preview_lbl._kw.get("text")))

    # Open Candidate Info Check modal (DocPacker-style multi-match popup)
    be.show_candidate_info()
    check("Candidate Info Check modal opens without error", True)

    # Save renamed badge and verify move + journal + undo
    be.save_current()
    expected = out_dir / "64847-Firoz Khan.jpg"
    check("Save moves & renames badge into output folder", expected.is_file() and not badge_img.exists(), list(out_dir.iterdir()))
    check("Item status updated to saved", it.status == "saved")

    # Undo restores original file
    be.undo_last()
    check("Undo restores badge back to input folder", badge_img.is_file() and not expected.exists() and it.status == "pending")
    check("No background job errors", not fakeapp.SyncJobs.errors, fakeapp.SyncJobs.errors)
finally:
    B.extract_badge = orig_extract
    be.on_close()

print()
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} Badge Extractor UI smoke checks passed" + (f"; FAILED: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
