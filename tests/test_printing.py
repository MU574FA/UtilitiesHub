"""Unit & integration tests for utilshub.core.printing (ranges, sorting, estimation, PDF composer, Win32 discovery)."""
from __future__ import annotations

import sys
from pathlib import Path

import docx
import pymupdf as fitz
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utilshub.core import printing as P  # noqa: E402


def _make_pdf(path: Path, pages: int = 3, title: str = "Doc") -> Path:
    doc = fitz.open()
    for i in range(1, pages + 1):
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((60, 80), f"{title} - Page {i}", fontsize=18)
    doc.save(str(path))
    doc.close()
    return path


def test_parse_page_range_and_selection():
    assert P.parse_page_range("1-3, 5, 7-", 9) == [0, 1, 2, 4, 6, 7, 8]
    assert P.parse_page_range("4-2, 2, 99", 5) == [3, 2, 1]
    assert P.parse_page_range("", 4) == [0, 1, 2, 3]

    assert P.select_page_indices(6, scope="first") == [0]
    assert P.select_page_indices(6, scope="all", subset="odd") == [0, 2, 4]
    assert P.select_page_indices(6, scope="all", subset="even", reverse=True) == [5, 3, 1]
    assert P.select_page_indices(6, scope="range", range_str="2-5", subset="odd") == [2, 4]


def test_sort_and_estimate_sheets(tmp_path):
    pdf = _make_pdf(tmp_path / "b_file.pdf", pages=4)
    img = tmp_path / "a_photo.png"
    Image.new("RGB", (400, 300), "white").save(img)
    txt = tmp_path / "c_note.txt"
    txt.write_text("hello", encoding="utf-8")

    items = [
        P.PrintItem(path=txt, label="Zed", page_count=1),
        P.PrintItem(path=img, label="Ali", page_count=1),
        P.PrintItem(path=pdf, label="Ali", page_count=4, copies=2),
    ]

    by_name = P.sort_print_items(items, "name")
    assert [it.label for it in by_name] == ["Ali", "Ali", "Zed"]
    by_type = P.sort_print_items(items, "type")
    assert by_type[0].path.suffix == ".pdf" and by_type[1].path.suffix == ".png"

    # Estimate sheets: 1 + 1 + (4*2) = 10 logical pages; per_name separator adds 2 faces -> 12 faces -> 6 duplex sheets
    s = P.PrintSettings(copies=2, duplex="long", separator="per_name", nup=1)
    pages, sheets = P.estimate_sheets(by_name, s)
    assert pages == 20
    assert sheets == 12

    # 4-up simplex without separators: 1 + 1 + ceil(4/4)*2 = 4 faces * 1 copy = 4 sheets
    s2 = P.PrintSettings(copies=1, duplex="simplex", separator="none", nup=4)
    pages2, sheets2 = P.estimate_sheets(by_name, s2)
    assert pages2 == 10
    assert sheets2 == 4


def test_compose_print_pdf_mixed_types_nup_separators_grayscale(tmp_path):
    pdf = _make_pdf(tmp_path / "cv.pdf", pages=4, title="Candidate CV")
    img = tmp_path / "id.jpg"
    Image.new("RGB", (600, 380), (40, 120, 220)).save(img)
    txt = tmp_path / "notes.txt"
    txt.write_text("Candidate Notes\nمرحبا بكم في اختبار الطباعة\nLine 3", encoding="utf-8")
    doc_p = tmp_path / "letter.docx"
    d = docx.Document()
    d.add_heading("Offer Letter", level=1)
    d.add_paragraph("Welcome aboard!")
    d.save(str(doc_p))

    items = [
        P.PrintItem(path=pdf, label="Ahmed Ali"),
        P.PrintItem(path=img, label="Ahmed Ali"),
        P.PrintItem(path=txt, label="Sara Noor"),
        P.PrintItem(path=doc_p, label="Sara Noor"),
    ]
    out_pdf = tmp_path / "composed.pdf"
    settings = P.PrintSettings(
        paper_size="a4",
        nup=2,
        separator="per_name",
        header_text="Utils Hub Print",
        footer_text="{name} - {file} - Page {page}/{total}",
        color_mode="grayscale",
        dpi=150,
    )
    comp = P.compose_print_pdf(items, settings, out_pdf)
    assert out_pdf.is_file()
    assert comp.skipped == []
    # 2 separators (Ahmed Ali, Sara Noor) + ceil(4/2)=2 + 1 (img) + 1 (txt) + 1 (docx) = 7 sheets
    assert comp.total_sheets == 7
    with fitz.open(out_pdf) as res:
        assert len(res) == 7
        # Confirm grayscale image embedded on sheet 0
        imgs = res[0].get_images(full=True)
        assert imgs


def test_compose_print_pdf_custom_range_and_item_override(tmp_path):
    p1 = _make_pdf(tmp_path / "one.pdf", pages=5, title="One")
    p2 = _make_pdf(tmp_path / "two.pdf", pages=4, title="Two")
    items = [
        P.PrintItem(path=p1, label="A", page_range="1-2"),
        P.PrintItem(path=p2, label="B"),
    ]
    out = tmp_path / "range.pdf"
    s = P.PrintSettings(page_scope="range", page_range="2,4", nup=1, separator="none")
    comp = P.compose_print_pdf(items, s, out)
    # Item 1 uses override 1-2 (2 pages), Item 2 uses global 2,4 (2 pages) -> 4 sheets
    assert comp.total_sheets == 4
    with fitz.open(out) as doc:
        assert "One - Page 1" in doc[0].get_text()
        assert "One - Page 2" in doc[1].get_text()
        assert "Two - Page 2" in doc[2].get_text()
        assert "Two - Page 4" in doc[3].get_text()


def test_printer_discovery_and_batch_mocked(tmp_path, monkeypatch):
    printers = P.list_printers()
    assert isinstance(printers, list)
    default = P.default_printer()
    assert isinstance(default, str)

    p1 = _make_pdf(tmp_path / "a.pdf", pages=2)
    p2 = _make_pdf(tmp_path / "b.pdf", pages=1)
    sent_jobs: list[tuple[str, int, int]] = []

    def fake_direct(pdf_path, settings, job_name="", copies=1, collate=True, **kw):
        with fitz.open(pdf_path) as d:
            sent_jobs.append((job_name, len(d), copies))
            return len(d) * copies

    monkeypatch.setattr(P, "print_pdf_direct", fake_direct)
    items = [P.PrintItem(path=p1, label="Ali", copies=2), P.PrintItem(path=p2, label="Sara")]
    statuses: list[tuple[int, str]] = []
    summary = P.print_batch(
        items,
        P.PrintSettings(printer="FakePrinter", job_mode="combined", copies=2, separator="per_name"),
        item_status_cb=lambda idx, st: statuses.append((idx, st)),
    )
    assert summary.printed_files == 2
    assert summary.failed_files == 0
    # 1 sep (Ali) + 2*2=4 pages (Ali copies=2) + 1 sep (Sara) + 1 page (Sara) = 7 sheets * 2 global copies = 14
    assert sent_jobs[0][1] == 7 and sent_jobs[0][2] == 2
    assert summary.total_sheets == 14
    assert any(st == "Sent ✔" for _, st in statuses)


def test_compose_and_batch_excluded_sheets(tmp_path, monkeypatch):
    p1 = _make_pdf(tmp_path / "one.pdf", pages=2, title="Batch1")
    p2 = _make_pdf(tmp_path / "two.pdf", pages=2, title="Batch2")
    items = [P.PrintItem(path=p1, label="Ali"), P.PrintItem(path=p2, label="Sara")]

    # First compose full preview to inspect batch groups and labels
    full_pdf = tmp_path / "full.pdf"
    full = P.compose_print_pdf(items, P.PrintSettings(nup=1), full_pdf)
    assert full.unfiltered_sheets == 4
    assert full.sheet_labels == ["p.1", "p.2", "p.1", "p.2"]
    assert full.sheet_groups[0][0] != full.sheet_groups[2][0]
    assert full.sheet_groups[0] == full.sheet_groups[1]

    # Exclude sheet index 1 (Batch1 p.2) and index 2 (Batch2 p.1)
    filt_pdf = tmp_path / "filtered.pdf"
    filt = P.compose_print_pdf(items, P.PrintSettings(nup=1), filt_pdf, excluded_sheets={1, 2})
    assert filt.total_sheets == 2
    assert filt.unfiltered_sheets == 4
    with fitz.open(filt_pdf) as d:
        assert "Batch1 - Page 1" in d[0].get_text()
        assert "Batch2 - Page 2" in d[1].get_text()

    # print_batch in per_file mode when all sheets of Batch1 ({0, 1}) are excluded -> Batch1 skipped, Batch2 printed
    sent: list[str] = []
    monkeypatch.setattr(P, "print_pdf_direct", lambda pdf_path, s, job_name="", **kw: (sent.append(job_name), 2)[1])
    statuses: list[tuple[int, str]] = []
    summary = P.print_batch(
        items,
        P.PrintSettings(printer="Fake", job_mode="per_file"),
        item_status_cb=lambda idx, st: statuses.append((idx, st)),
        excluded_sheets={0, 1},
    )
    assert sent == ["two.pdf"]
    assert summary.printed_files == 1
    assert (0, "Skipped (0 pages)") in statuses
    assert (1, "Sent ✔") in statuses


def test_print_batch_delay_all_modes_and_cancel(tmp_path, monkeypatch):
    import time

    p1 = _make_pdf(tmp_path / "a.pdf", pages=1, title="A")
    p2 = _make_pdf(tmp_path / "b.pdf", pages=1, title="B")

    events: list[tuple[str, float, str]] = []
    sleeps: list[float] = []
    orig_sleep = time.sleep
    monkeypatch.setattr(time, "sleep", lambda d: (sleeps.append(d), orig_sleep(0.001)))

    def fake_direct(pdf_path, s, job_name="", copies=1, **kw):
        events.append(("job", time.monotonic(), job_name))
        return 1 * copies

    monkeypatch.setattr(P, "print_pdf_direct", fake_direct)

    prog_msgs: list[str] = []
    # 1. Combined mode with 2 files + delay > 0 must pause between the 2 files
    s_comb = P.PrintSettings(printer="Fake", job_mode="combined", delay_seconds=0.35)
    res1 = P.print_batch(
        [P.PrintItem(p1, "Ali"), P.PrintItem(p2, "Sara")],
        s_comb,
        progress_cb=lambda d, t, m: prog_msgs.append(m),
    )
    assert res1.printed_files == 2
    assert len([e for e in events if e[0] == "job"]) == 2
    assert sleeps and sum(sleeps) >= 0.25
    assert any("Waiting " in m for m in prog_msgs)

    # 2. Combined mode with 1 file and copies=3 + delay > 0 pauses twice between copies
    events.clear(); sleeps.clear()
    res2 = P.print_batch(
        [P.PrintItem(p1, "Ali")],
        P.PrintSettings(printer="Fake", job_mode="combined", copies=3, delay_seconds=0.25),
    )
    assert res2.printed_files == 1 and res2.total_sheets == 3
    assert len([e for e in events if e[0] == "job"]) == 3
    assert len(sleeps) >= 2

    # 3. Cancellation during delay aborts before second file prints
    events.clear(); sleeps.clear()
    cancel_flag = {"stop": False}

    def fake_sleep_cancel(d):
        sleeps.append(d)
        cancel_flag["stop"] = True

    monkeypatch.setattr(time, "sleep", fake_sleep_cancel)
    res3 = P.print_batch(
        [P.PrintItem(p1, "Ali"), P.PrintItem(p2, "Sara")],
        P.PrintSettings(printer="Fake", job_mode="per_file", delay_seconds=5.0),
        cancelled=lambda: cancel_flag["stop"],
    )
    assert res3.cancelled is True
    assert res3.printed_files == 1
    assert len([e for e in events if e[0] == "job"]) == 1


def test_asymmetric_margins_shift_content(tmp_path):
    src = _make_pdf(tmp_path / "doc.pdf", pages=1, title="MarginCheck")
    out = tmp_path / "shifted.pdf"
    s = P.PrintSettings(
        paper_size="a4",
        separator="per_file",
        margin_left_mm=40.0,
        margin_top_mm=25.0,
        margin_right_mm=5.0,
        margin_bottom_mm=5.0,
    )
    comp = P.compose_print_pdf([P.PrintItem(src, "Ali")], s, out)
    assert comp.total_sheets == 2
    pt_per_mm = 72.0 / 25.4
    expected_left = 40.0 * pt_per_mm
    expected_top = 25.0 * pt_per_mm
    with fitz.open(out) as doc:
        # Cover sheet (index 0) and content sheet (index 1) must both respect the 40mm left & 25mm top margins
        for pg_i in (0, 1):
            blocks = doc[pg_i].get_text("blocks")
            assert blocks
            min_x = min(b[0] for b in blocks)
            min_y = min(b[1] for b in blocks)
            assert min_x >= expected_left - 1.0, (pg_i, min_x, expected_left)
            assert min_y >= expected_top - 1.0, (pg_i, min_y, expected_top)
