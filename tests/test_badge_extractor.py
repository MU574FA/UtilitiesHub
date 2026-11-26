"""Unit tests for Badge Extractor core logic (OCR parsing, transliteration, templates, Excel multi-match)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utilshub.core import badges as B  # noqa: E402


def test_parse_badge_text_three_lines_arabic():
    raw = "فيروج خان\nص-المجزر ( عمال التنظيف)\n64847"
    data = B.parse_badge_text(raw)
    assert data.ar_name == "فيروج خان"
    assert data.job_title == "ص-المجزر ( عمال التنظيف)"
    assert data.badge_no == "64847"
    assert data.en_name == "Firoj Khan"


def test_parse_badge_text_arabic_digits_and_noise():
    raw = "\n  ---\nمحمد علي حسين\nمشرف الانتاج\n٦٤٨٤٧\n|\n"
    data = B.parse_badge_text(raw)
    assert data.ar_name == "محمد علي حسين"
    assert data.job_title == "مشرف الانتاج"
    assert data.badge_no == "64847"
    assert data.en_name == "Mohammed Ali Hussein"


def test_transliterate_arabic_common_and_compound_names():
    assert B.transliterate_arabic("عبد الله الكريم") == "Abdullah Al-Karim"
    assert B.transliterate_arabic("أحمد حسن") == "Ahmed Hassan"
    assert B.transliterate_arabic("فيروج خان") == "Firoj Khan"
    assert B.transliterate_arabic("حيدر كاظم") == "Haider Kadhim"


def test_render_badge_stem_default_and_custom():
    stem = B.render_badge_stem(
        B.DEFAULT_FILENAME_TEMPLATE,
        ar_name="فيروج خان",
        en_name="Firoj Khan",
        job_title="ص-المجزر ( عمال التنظيف)",
        badge_no="64847",
    )
    assert stem == "فيروج خان-Firoj Khan-64847-ص-المجزر ( عمال التنظيف)"

    custom = B.render_badge_stem(
        "{badge_no}_{en_name|upper}",
        ar_name="فيروج خان",
        en_name="Firoj Khan",
        job_title="عمال التنظيف",
        badge_no="64847",
    )
    assert custom == "64847_FIROJ KHAN"

    # Missing fields collapse cleanly without ugly double dashes
    partial = B.render_badge_stem("{ar_name}-{en_name}-{badge_no}", ar_name="أحمد", en_name="", badge_no="102")
    assert "--" not in partial and "أحمد" in partial and "102" in partial


def test_search_badge_candidates_multiple_similar_names(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Staff"
    ws.append(["Full Name", "Arabic Name", "Badge ID", "Department", "Phone"])
    ws.append(["Firoj Khan", "فيروج خان", "64847", "Slaughterhouse Cleaning", "+9647700001"])
    ws.append(["Firoz Khan", "فيروز خان", "64899", "Slaughterhouse Packing", "+9647700002"])
    ws.append(["Firoj Ahmed Khan", "فيروج أحمد خان", "65100", "Maintenance", "+9647700003"])
    ws.append(["Farooq Khan", "فاروق خان", "65200", "Logistics", "+9647700004"])
    ws.append(["John Smith", "جون سميث", "99999", "Admin", "+9647700099"])
    xlsx = tmp_path / "staff.xlsx"
    wb.save(xlsx)

    # Search by English transliteration: should return multiple similar candidates ranked by %
    hits = B.search_badge_candidates(str(xlsx), en_name="Firoj Khan", search_by="en_name", threshold=0.55, limit=10)
    assert len(hits) >= 3
    assert hits[0].name_value == "Firoj Khan"
    assert hits[0].percent == 100
    assert hits[1].percent < 100 and hits[1].percent >= 75
    assert all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1))
    assert not any(h.name_value == "John Smith" for h in hits)
    assert B.has_ambiguous_matches(hits)

    # Search by Arabic name
    ar_hits = B.search_badge_candidates(
        str(xlsx), ar_name="فيروج خان", search_by="ar_name", search_column="Arabic Name", threshold=0.60
    )
    assert len(ar_hits) >= 2
    assert ar_hits[0].matched_value == "فيروج خان"
    assert ar_hits[0].percent == 100

    # Search by badge number
    id_hits = B.search_badge_candidates(str(xlsx), badge_no="64847", search_by="badge_no", threshold=0.80)
    assert len(id_hits) == 1
    assert id_hits[0].percent == 100
    assert id_hits[0].row == 2


def test_find_candidate_cvs(tmp_path):
    cv_dir = tmp_path / "cvs"
    cv_dir.mkdir()
    cv1 = cv_dir / "Firoj_Khan_CV_2026.pdf"
    cv1.write_text("dummy")
    sub = cv_dir / "Ahmed Hassan"
    sub.mkdir()
    cv2 = sub / "Resume_Final.docx"
    cv2.write_text("dummy")
    cv3 = cv_dir / "Worker_64847.pdf"
    cv3.write_text("dummy")
    other = cv_dir / "John_Smith_CV.pdf"
    other.write_text("dummy")

    hits1 = B.find_candidate_cvs(cv_dir, en_name="Firoj Khan")
    assert hits1 and hits1[0][1] == cv1
    assert all(p != other for _, p in hits1)

    hits2 = B.find_candidate_cvs(cv_dir, en_name="Ahmed Hassan")
    assert hits2 and hits2[0][1] == cv2

    hits3 = B.find_candidate_cvs(cv_dir, badge_no="64847")
    assert hits3 and hits3[0][1] == cv3


def test_badge_extractor_ui_smoke():
    script = Path(__file__).resolve().parent / "ui_smoke" / "badge_extractor.py"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
