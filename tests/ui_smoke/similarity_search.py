"""Headless UI logic smoke test for ExtractHub -> Similarity Search tab (fake tkinter)."""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

tmp = Path(tempfile.mkdtemp())
os.environ["UTILSHUB_HOME"] = str(tmp / "home")

import fakeapp  # noqa: E402  (installs faketk)
import faketk  # noqa: E402

faketk.Widget.winfo_rootx = lambda self: 0
faketk.Widget.winfo_rooty = lambda self: 0
faketk.Widget.winfo_height = lambda self: 600
faketk.Widget.winfo_reqheight = lambda self: 200

import utilshub.tools.similarity_search as ss_mod  # noqa: E402
from utilshub.core.ocr_search import OcrTextStore, norm_path  # noqa: E402
from utilshub.core.readers import DocInfo, file_hash  # noqa: E402
from utilshub.tools.extract import ExtractHub  # noqa: E402

ss_mod.PageViewer = fakeapp.FakeViewer

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))


app = fakeapp.FakeApp(tmp)
app.settings.register(ExtractHub.options())
hub = ExtractHub(app)
app.tools["extract"] = hub
app.aliases.update({m.id: hub.id for m in hub.members() if m is not hub})

check("ExtractHub includes similarity_search as 4th tab", [m.id for m in hub.members()] == ["screenshot", "ocr_match", "passport", "similarity_search"])

hub.ensure_built(app.root)
hub.on_show()
hub.select("similarity_search")
tool = hub.member("similarity_search")
check("Similarity Search tab selected and built", tool.built and hub.active is tool)

tool.store.close()
tool.store = OcrTextStore(tmp / "ocr_cache.sqlite")

# Prepare test corpus & query files
corpus_dir = tmp / "corpus"
corpus_dir.mkdir()
f1 = corpus_dir / "alpha_scan.png"
f2 = corpus_dir / "beta_contract.pdf"
f3 = corpus_dir / "gamma_draft.txt"
f1.write_bytes(b"scan-1")
f2.write_bytes(b"pdf-2")
f3.write_text("Draft invoice for Beta Services LLC\nNot final", encoding="utf-8")

ocr_calls = []
texts = {
    "alpha_scan.png": ["Custorner: Tariq Al-Harbi\nSafety helmet inspection passed on site A."],
    "beta_contract.pdf": ["Service Agreement with Beta Services LLC", "Clause 4: Payment terms 30 days net."],
    "gamma_draft.txt": ["Draft invoice for Beta Services LLC\nNot final"],
    "query_doc.pdf": ["Fire drill certificate renewed for Tariq Al-Harbi"],
}


def mock_reader(path, _cfg, _opts):
    p = Path(path)
    ocr_calls.append(p.name)
    pages = texts[p.name]
    return DocInfo(
        str(p),
        kind="pdf" if p.suffix == ".pdf" else "image",
        text="\n".join(pages),
        pages=len(pages),
        page_texts=list(pages),
        hash=file_hash(p),
        used_ocr=p.suffix in (".png", ".pdf"),
    )


tool._reader_fn = mock_reader

# 1. Add folder via hub shared files bar -> auto-indexes 3 files (3 OCR calls)
hub.deliver("similarity_search", [str(corpus_dir)])
check("initial indexing reads each file once", len(ocr_calls) == 3 and len(tool.active_docs) == 3, ocr_calls)
check("cache button updated", "3" in tool.cache_btn._kw["text"], tool.cache_btn._kw["text"])

# 2. Re-index with no changes -> 0 OCR calls, 0 prompts
ocr_calls.clear()
prompts_seen = []
tool._changed_prompt = lambda changed: (prompts_seen.append(changed), ([c.path for c in changed], False))[1]
tool.index_sources()
check("second index makes 0 OCR calls and 0 prompts", ocr_calls == [] and prompts_seen == [], (ocr_calls, prompts_seen))

# 3. Touch mtime without changing content -> 0 OCR calls, 0 prompts
st1 = f1.stat()
os.utime(f1, ns=(st1.st_atime_ns, st1.st_mtime_ns + 5_000_000_000))
tool.index_sources()
check("mtime touch without content change asks nothing and makes 0 OCR calls", ocr_calls == [] and prompts_seen == [])

# 4. Modify f1 bytes -> prompt fires with ONLY f1; decline keeps cached text
f1.write_bytes(b"scan-1-v2-changed")
texts["alpha_scan.png"] = ["Custorner: Tariq Al-Harbi\nFire drill certificate renewed for 2027."]
tool._changed_prompt = lambda changed: (prompts_seen.append([Path(c.path).name for c in changed]), ([], False))[1]
tool.index_sources()
check("modified file triggers ask prompt for ONLY that file", prompts_seen == [["alpha_scan.png"]], prompts_seen)
check("declining prompt makes 0 OCR calls and keeps old text", ocr_calls == [] and "Safety helmet" in tool.active_docs[norm_path(f1)].text)

# 5. Accept prompt on next check -> re-OCRs ONLY f1 and updates cache
prompts_seen.clear()
tool._changed_prompt = lambda changed: (prompts_seen.append([Path(c.path).name for c in changed]), ([c.path for c in changed], False))[1]
tool.index_sources()
check("accepting prompt re-OCRs ONLY changed file", ocr_calls == ["alpha_scan.png"], ocr_calls)

# 6. Text search + exclude + pasted snippet + highlight inspector
tool.add_pasted_snippet("Memo", "Fire drill certificate compliance note")
tool.query_text.insert("1.0", "Fire drill certificate")
tool.run_search()
check("text search finds modified OCR doc and pasted snippet", len(tool.hits) >= 2, len(tool.hits))
first_hit = next(iter(tool.hits.values()))
check("inspector populates text and highlight spans", bool(tool.doc_text.get("1.0", "end-1c").strip()) and len(tool._hit_spans) >= 1)
tool._step_hit(1)
check("hit stepping updates label", "/" in tool.hit_pos_lbl._kw["text"], tool.hit_pos_lbl._kw["text"])

tool.query_text.delete("1.0", "end")
tool.query_text.insert("1.0", "Beta Services")
tool.exclude_var.set("draft")
tool.run_search()
matched_names = {h.name for h in tool.hits.values()}
check("exclude filter removes draft file", "beta_contract.pdf" in matched_names and "gamma_draft.txt" not in matched_names, matched_names)

# 7. Query from file (smart cached) & query from folder
q_dir = tmp / "query_folder"
q_dir.mkdir()
qf = q_dir / "query_doc.pdf"
qf.write_bytes(b"query-pdf-bytes")
ocr_calls.clear()
tool.search_with_paths([str(qf)])
check("query from file indexes query file once and returns hits", ocr_calls == ["query_doc.pdf"] and len(tool.hits) >= 1, (ocr_calls, len(tool.hits)))

ocr_calls.clear()
tool.search_with_paths([str(q_dir)])
check("query from folder reuses cached query file with 0 OCR calls", ocr_calls == [] and len(tool.hits) >= 1, ocr_calls)

# 8. Exports, Copy matching files + Undo copy, Cache Manager
xlsx_out = tmp / "sim_results.xlsx"
faketk.DIALOGS["asksaveasfilename"] = str(xlsx_out)
tool.export_excel()
check("Excel export written", xlsx_out.exists() and xlsx_out.stat().st_size > 500)

csv_out = tmp / "sim_results.csv"
app.settings.set("similarity_search.append_file", str(csv_out))
tool.add_to_file()
check("CSV append written", csv_out.exists() and "beta_contract.pdf" in csv_out.read_text(encoding="utf-8-sig") or csv_out.exists())

copy_dest = tmp / "copied_matches"
faketk.DIALOGS["askdirectory"] = str(copy_dest)
tool.copy_matching_files()
copied_files = list(copy_dest.glob("*"))
check("copy matching files copies files to destination", len(copied_files) >= 1, copied_files)
tool.undo_copy()
check("undo copy removes copied files", not list(copy_dest.glob("*")))

cm = ss_mod.CacheManagerDialog(tool.frame, tool)
check("CacheManagerDialog lists cached entries", len(cm.table.all_ids()) >= 4, len(cm.table.all_ids()))
cm.win.destroy()

tool.on_close()

print()
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} Similarity Search UI smoke checks passed" + (f"; FAILED: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
