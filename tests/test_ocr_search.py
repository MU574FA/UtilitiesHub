"""Unit tests for utilshub.core.ocr_search (smart OCR cache, change detection, and similarity search)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utilshub.core.ocr import OcrConfig
from utilshub.core.ocr_search import (
    OcrTextStore,
    SearchOptions,
    classify_files,
    collect_search_files,
    find_highlight_spans,
    format_doc_pages,
    index_files,
    make_pasted_doc,
    ocr_config_signature,
    search_by_documents,
    search_documents,
)
from utilshub.core.readers import DocInfo, file_hash


def _counting_reader(texts: dict[str, list[str]], calls: list[str], fail_paths: set[str] | None = None):
    def reader(path, _cfg, _opts):
        p = Path(path)
        calls.append(p.name)
        if fail_paths and p.name in fail_paths:
            return DocInfo(str(p), kind="pdf", error="Simulated OCR failure")
        pages = texts.get(p.name, [p.read_text(encoding="utf-8", errors="replace")])
        return DocInfo(
            str(p),
            kind="pdf" if p.suffix.lower() == ".pdf" else "image",
            text="\n".join(pages),
            pages=len(pages),
            page_texts=list(pages),
            hash=file_hash(p),
            used_ocr=True,
        )
    return reader


def test_store_persistence_stat_fastpath_touch_copy_and_change_detection(tmp_path):
    db_path = tmp_path / "cache.sqlite"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    a = docs_dir / "invoice_a.pdf"
    b = docs_dir / "contract_b.png"
    a.write_bytes(b"pdf-bytes-a-v1")
    b.write_bytes(b"img-bytes-b-v1")

    calls: list[str] = []
    reader = _counting_reader(
        {"invoice_a.pdf": ["Invoice 1001 for Acme Corp"], "contract_b.png": ["Employment contract John Smith"]},
        calls,
    )
    cfg = OcrConfig(enabled=True, languages="eng", dpi=200)
    store = OcrTextStore(db_path)

    # 1. First classify -> both are new
    plan1 = classify_files([a, b], store, ocr_sig=ocr_config_signature(cfg))
    assert len(plan1.new_files) == 2 and not plan1.cached and not plan1.changed
    index_files(plan1.new_files, store, cfg, reader_fn=reader)
    assert calls == ["invoice_a.pdf", "contract_b.png"]
    store.close()

    # 2. Reopen DB -> stat fast-path hits cache with 0 OCR calls
    store2 = OcrTextStore(db_path)
    calls.clear()
    plan2 = classify_files([a, b], store2, ocr_sig=ocr_config_signature(cfg))
    assert len(plan2.cached) == 2 and not plan2.new_files and not plan2.changed
    assert all(d.origin == "cached" for d in plan2.cached)
    assert calls == []

    # 3. Touch mtime without changing bytes -> SHA-1 matches, silently reused, NOT flagged as changed
    st_a = a.stat()
    os.utime(a, ns=(st_a.st_atime_ns, st_a.st_mtime_ns + 5_000_000_000))
    plan3 = classify_files([a, b], store2, ocr_sig=ocr_config_signature(cfg))
    assert len(plan3.cached) == 2 and not plan3.changed and plan3.reused_count == 1
    assert any(d.origin == "reused" for d in plan3.cached)

    # 4. Copy file to a new location -> SHA-1 content reuse, 0 OCR calls
    a_copy = docs_dir / "invoice_a_copy.pdf"
    a_copy.write_bytes(a.read_bytes())
    plan4 = classify_files([a, b, a_copy], store2, ocr_sig=ocr_config_signature(cfg))
    assert len(plan4.cached) == 3 and not plan4.new_files and not plan4.changed
    assert plan4.reused_count == 1
    assert "Acme Corp" in store2.get_by_path(a_copy).text

    # 5. Genuine edit of file b -> ONLY b is flagged in plan.changed
    b.write_bytes(b"img-bytes-b-v2-modified")
    plan5 = classify_files([a, b, a_copy], store2, ocr_sig=ocr_config_signature(cfg))
    assert len(plan5.cached) == 2
    assert len(plan5.changed) == 1
    cf = plan5.changed[0]
    assert Path(cf.path).name == "contract_b.png" and cf.reason == "modified"
    assert cf.old_sha1 != cf.new_sha1

    # 6. If re-OCR fails, previous clean cache entry is preserved
    fail_reader = _counting_reader({}, calls, fail_paths={"contract_b.png"})
    res_fail = index_files([b], store2, cfg, reader_fn=fail_reader)
    assert res_fail[0].error
    assert "John Smith" in store2.get_by_path(b).text

    # 7. Successful re-OCR updates cache and clears changed state
    ok_reader = _counting_reader({"contract_b.png": ["Updated employment contract Jane Doe"]}, calls)
    index_files([b], store2, cfg, reader_fn=ok_reader)
    plan6 = classify_files([a, b, a_copy], store2, ocr_sig=ocr_config_signature(cfg))
    assert len(plan6.cached) == 3 and not plan6.changed
    assert "Jane Doe" in store2.get_by_path(b).text

    # 8. OCR config signature check when opted in
    cfg_ara = OcrConfig(enabled=True, languages="eng+ara", dpi=200)
    plan_sig = classify_files([a], store2, ocr_sig=ocr_config_signature(cfg_ara), check_ocr_sig=True)
    assert len(plan_sig.changed) == 1 and plan_sig.changed[0].reason == "ocr_settings"

    # 9. Stats, summaries, exports, prune_missing, delete, clear
    st = store2.stats()
    assert st["docs"] == 3 and st["pages"] == 3 and st["chars"] > 0
    summaries = store2.list_summaries()
    assert len(summaries) == 3 and all(s["exists"] for s in summaries)

    txt_out = tmp_path / "export.txt"
    json_out = tmp_path / "export.json"
    assert store2.export_to_txt(txt_out) == 3 and "Jane Doe" in txt_out.read_text(encoding="utf-8")
    assert store2.export_to_json(json_out) == 3
    loaded_json = json.loads(json_out.read_text(encoding="utf-8"))
    assert len(loaded_json) == 3

    a_copy.unlink()
    assert store2.prune_missing() == 1
    assert store2.delete([a]) == 1
    assert store2.stats()["docs"] == 1
    store2.clear()
    assert store2.stats()["docs"] == 0
    store2.close()


def test_collect_search_files_filters(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    dup = tmp_path / "_duplicates"
    dup.mkdir()
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "b.png").write_bytes(b"png")
    (sub / "c.pdf").write_bytes(b"pdf")
    (sub / "~$temp.docx").write_bytes(b"tmp")
    (dup / "ignored.txt").write_text("no")

    all_rec = collect_search_files([tmp_path], recursive=True)
    assert {p.name for p in all_rec} == {"a.txt", "b.png", "c.pdf"}

    non_rec = collect_search_files([tmp_path], recursive=False)
    assert {p.name for p in non_rec} == {"a.txt", "b.png"}

    imgs = collect_search_files([tmp_path], recursive=True, file_type="images")
    assert [p.name for p in imgs] == ["b.png"]

    only_txt = collect_search_files([tmp_path], recursive=True, extensions={"txt"})
    assert [p.name for p in only_txt] == ["a.txt"]


def test_similarity_search_modes_ocr_tolerance_and_batch_docs(tmp_path):
    store = OcrTextStore(":memory:")
    files = []
    contents = {
        "scan_ocr.png": [
            "Page one intro",
            "Custorner: Ahmed Al-Mansoor\nInvoice Total: O0125 USD\nPayment via wire transfer approved.",
        ],
        "arabic_cv.pdf": [
            "السيرة الذاتية\nالاسم: أحمد محمد علي\nخبرة في إدارة المشاريع الهندسية والإنشائية",
        ],
        "draft_invoice.txt": [
            "Invoice draft for Ahmed Mansoor - internal copy not final",
        ],
        "report_long.txt": [
            "Quarterly safety inspection and electrical maintenance protocol for Riyadh warehouse facility.\n"
            "All fire extinguishers, emergency exits, and backup generators passed compliance checks.",
            "Budget appendix and travel expenses.",
        ],
    }
    for name, pages in contents.items():
        p = tmp_path / name
        p.write_text("\n".join(pages), encoding="utf-8")
        files.append(p)

    cfg = OcrConfig(enabled=True)
    reader = _counting_reader(contents, [])
    docs = index_files(files, store, cfg, reader_fn=reader)
    pasted = make_pasted_doc("Quick note", "Riyadh warehouse safety inspection backup generators checklist")
    corpus = docs + [pasted]

    # 1. Fuzzy & OCR-tolerant search (rn -> m in Custorner, accent/Arabic folding)
    hits_ocr = search_documents(corpus, "Customer Ahmed Mansour", SearchOptions(mode="fuzzy", threshold=0.72, ocr_tolerant=True))
    assert hits_ocr and hits_ocr[0].name == "scan_ocr.png"
    assert hits_ocr[0].page == 2

    # 2. Exclude filter (-draft) drops draft_invoice.txt
    hits_ex = search_documents(corpus, "Ahmed Mansoor -draft", SearchOptions(mode="fuzzy", threshold=0.65))
    assert all(h.name != "draft_invoice.txt" for h in hits_ex)
    assert any(h.name == "scan_ocr.png" for h in hits_ex)

    # 3. Arabic spelling normalisation (أحمد vs احمد)
    hits_ar = search_documents(corpus, "احمد محمد علي", SearchOptions(mode="smart", threshold=0.75))
    assert hits_ar and hits_ar[0].name == "arabic_cv.pdf"

    # 4. Multi-line AND vs OR
    hits_and = search_documents(
        corpus,
        "Ahmed Mansoor\nwire transfer",
        SearchOptions(mode="fuzzy", threshold=0.75, match_all_lines=True),
    )
    assert [h.name for h in hits_and] == ["scan_ocr.png"]

    # 5. Semantic / long passage query matches both report_long.txt and pasted snippet
    hits_sem = search_documents(
        corpus,
        "safety inspection protocol and backup generators check for Riyadh warehouse",
        SearchOptions(mode="smart", threshold=0.35),
    )
    top_names = {h.name for h in hits_sem[:2]}
    assert "report_long.txt" in top_names and "Quick note" in top_names
    rep_hit = next(h for h in hits_sem if h.name == "report_long.txt")
    assert rep_hit.page == 1

    # 6. Exact / Regex mode + invalid regex error
    hits_rx = search_documents(corpus, r"O0\d{3}\s+USD", SearchOptions(mode="exact"))
    assert len(hits_rx) == 1 and hits_rx[0].name == "scan_ocr.png"
    with pytest.raises(ValueError):
        search_documents(corpus, "[unclosed", SearchOptions(mode="exact"))

    # 7. Document-to-corpus batch search (search_by_documents) excludes self-match
    qdoc = next(d for d in docs if d.name == "report_long.txt")
    batch_hits = search_by_documents(corpus, [qdoc], SearchOptions(mode="smart", threshold=0.25), exclude_self=True)
    assert batch_hits and all(h.name != "report_long.txt" for h in batch_hits)
    assert batch_hits[0].name == "Quick note" and batch_hits[0].query == "report_long.txt"

    # 8. Format & highlight spans helper
    disp, starts = format_doc_pages(next(d for d in docs if d.name == "scan_ocr.png"))
    assert "──── page 2 ────" in disp and dict(starts)[2] > 1
    spans = find_highlight_spans(disp, ["wire transfer", "Ahmed"])
    assert len(spans) >= 2
    store.close()
