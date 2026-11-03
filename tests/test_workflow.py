"""Headless tests for the workflow engine (no Tk needed):  python tests/test_workflow.py

Bricks that need pymupdf (merge / split / compress ...) are tested only when it is installed.
"""
import json, os, sys, tempfile, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utilshub.core.fileops import Journal
from utilshub.core.workflow import (BRICKS, TEMPLATES, RunEnv, Step, Workflow, WorkflowStore, check_workflow,
                                    export_workflow, import_workflow, run_workflow)

try:
    import pymupdf
except ImportError:
    pymupdf = None


def sandbox():
    d = Path(tempfile.mkdtemp())
    src = d / "in"; src.mkdir()
    (src / "b10.txt").write_text("passport No: A1234567 Ahmed")
    (src / "b2.txt").write_text("hello world")
    (src / "copy of b2.txt").write_text("hello world")          # exact duplicate of b2.txt
    (src / "note.md").write_text("nothing here")
    (src / "big.bin").write_bytes(b"x" * 3000)
    return d, src, RunEnv(out_root=d / "out", journal=Journal(d / "journal.jsonl"))


def wf(*steps, **kw):
    return Workflow("t", [Step(b, p) for b, p in steps], **kw)


def names(rep):                       # in flow order
    return [Path(f).name for f in rep.files]


def snames(rep):                      # sorted, when the order does not matter
    return sorted(names(rep))


def test_registry_and_templates():
    assert len(BRICKS) >= 18
    for b in BRICKS.values():
        assert b.group and b.title and b.run, b.id
    for t in TEMPLATES:                                           # every starter only uses real bricks
        assert all(s.brick in BRICKS for s in t.steps), t.name
        assert t.steps[0].brick in ("given", "folder"), t.name


def test_check_workflow():
    assert check_workflow(wf(("keep_types", {"types": "pdf"}))) .get(0)        # nothing flows in
    assert check_workflow(wf(("given", {}), ("keep_types", {"types": ""}))).get(1)   # required empty
    assert not check_workflow(wf(("given", {}), ("keep_types", {"types": "pdf"})))
    assert check_workflow(wf(("nope", {}))).get(0)


def test_filters_and_sort():
    d, src, env = sandbox()
    rep = run_workflow(wf(("folder", {"folder": str(src)}), ("keep_types", {"types": "txt"}),
                          ("sort", {"by": "name"})), env=env)
    assert rep.ok and names(rep) == ["b2.txt", "b10.txt", "copy of b2.txt"], names(rep)   # natural order: 2 before 10
    assert [r.n_out for r in rep.steps] == [5, 3, 3] and rep.steps[1].n_in == 5
    rep = run_workflow(wf(("folder", {"folder": str(src)}), ("name_has", {"words": "copy, note", "mode": "drop"}),
                          ("size", {"max_mb": 0.002})), env=env)
    assert snames(rep) == ["b10.txt", "b2.txt"], names(rep)
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "txt, md"}), ("sort", {"by": "name", "limit": 2, "reverse": True})), env=env)
    assert len(rep.files) == 2
    rep = run_workflow(wf(("folder", {"folder": str(src)}), ("name_has", {"words": r"^b\d+$", "match": "regex"})), env=env)
    assert snames(rep) == ["b10.txt", "b2.txt"], names(rep)


def test_dupes_text_and_grab():
    d, src, env = sandbox()
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "txt"}), ("no_dupes", {})), env=env)
    assert len(rep.files) == 2, names(rep)
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "txt"}), ("text_has", {"words": "passport"})), env=env)
    assert names(rep) == ["b10.txt"], names(rep)
    csv_path = d / "list.csv"
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "txt"}), ("text_has", {"words": "passport"}),
                          ("grab", {"column": "Passport", "pattern": r"No:\s*([A-Z]\d{7})"}),
                          ("to_csv", {"file": str(csv_path)})), env=env)
    assert rep.ok, rep.message
    text = csv_path.read_text(encoding="utf-8-sig")
    assert "A1234567" in text and "b10.txt" in text and "Passport" in text, text


def test_to_folder_rename_undo():
    d, src, env = sandbox()
    dest = d / "sorted"
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "txt, md"}),
                          ("to_folder", {"dest": str(dest), "action": "copy", "by": "type"})), env=env)
    assert rep.ok and (dest / "txt" / "b2.txt").exists() and (dest / "md" / "note.md").exists()
    assert (src / "b2.txt").exists()                               # copy leaves the original
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "md"}),
                          ("to_folder", {"dest": str(dest), "action": "move"})), env=env)
    assert (dest / "note.md").exists() and not (src / "note.md").exists()
    entry = env.journal.last_undoable()                            # moves are undoable from History & Undo
    assert entry and entry.tool == "workflows"
    env.journal.undo(entry)
    assert (src / "note.md").exists()
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "md"}),
                          ("rename", {"pattern": "{n:3}_{name}"})), env=env)
    assert (src / "001_note.md").exists(), list(src.iterdir())
    assert rep.files and Path(rep.files[0]).name == "001_note.md"
    # "skip" policy
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "txt"}),
                          ("to_folder", {"dest": str(dest), "action": "copy", "by": "type", "conflict": "skip"})), env=env)
    assert "skipped" in rep.steps[-1].message and rep.steps[-1].status == "warn"


def test_preview_changes_nothing():
    d, src, env = sandbox()
    dest = d / "never"
    before = sorted(p.name for p in src.iterdir())
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "txt"}), ("rename", {"pattern": "X_{name}"}),
                          ("to_folder", {"dest": str(dest), "action": "move"}), ("zip", {"name": "z"}),
                          ("to_csv", {"file": str(d / "p.csv")})), env=env, preview=True)
    assert rep.ok, rep.message
    assert sorted(p.name for p in src.iterdir()) == before
    assert not dest.exists() and not (d / "p.csv").exists() and not (d / "out").exists()
    assert not env.journal.entries()
    assert rep.steps[1].message.endswith("(preview)")


def test_zip_given_and_stop():
    d, src, env = sandbox()
    ins = [src / "b2.txt", src / "b10.txt", src / "b2.txt"]                  # duplicate input is collapsed
    rep = run_workflow(wf(("given", {}), ("sort", {}), ("zip", {"name": "pack"})), inputs=ins, env=env)
    assert rep.ok and len(rep.files) == 1 and rep.made == rep.files
    with zipfile.ZipFile(rep.files[0]) as z:
        assert z.namelist() == ["b2.txt", "b10.txt"], z.namelist()
    assert Path(rep.files[0]).parent.parent.parent == d / "out"              # <out>/<workflow>/<timestamp>/
    rep = run_workflow(wf(("given", {}), ("zip", {})), inputs=[], env=env)   # nothing given -> polite stop, not a crash
    assert rep.ok and "No files" in rep.message and len(rep.steps) == 1
    rep = run_workflow(wf(("folder", {"folder": str(src)}), ("keep_types", {"types": "pdf"}),
                          ("stop_unless", {"min": 1}), ("zip", {})), env=env)
    assert rep.ok and rep.steps[-1].status == "stopped" and len(rep.steps) == 3
    rep = run_workflow(wf(("folder", {"folder": str(d / "nope")})), env=env)
    assert "not found" in rep.message


def test_errors_cancel_disabled():
    d, src, env = sandbox()
    rep = run_workflow(wf(("folder", {"folder": str(src)}), ("name_has", {"words": "(", "match": "regex"}),
                          ("zip", {})), env=env)
    assert not rep.ok and "Bad regular expression" in rep.message and rep.steps[-1].status == "error"
    flag = {"stop": False}
    env2 = RunEnv(out_root=d / "out2", cancelled=lambda: flag["stop"], progress=lambda *a: flag.update(stop=True))
    rep = run_workflow(wf(("folder", {"folder": str(src)}), ("zip", {})), env=env2)
    assert not rep.ok and rep.message == "Cancelled."
    w = wf(("folder", {"folder": str(src)}), ("keep_types", {"types": "pdf"}), ("zip", {}))
    w.steps[1].enabled = False                                               # a switched-off brick is skipped
    rep = run_workflow(w, env=env)
    assert rep.ok and len(rep.steps) == 2 and len(rep.files) == 1
    assert not run_workflow(Workflow("empty"), env=env).ok


def test_store_and_files():
    d, src, env = sandbox()
    store = WorkflowStore(d / "wf.json")
    w = wf(("given", {}), ("zip", {"name": "a"}), description="hi")
    w.name = "Mine"
    store.put(w)
    w.steps[1].params["name"] = "b"
    store.put(w)                                                             # same name = replace
    again = WorkflowStore(d / "wf.json")
    assert again.names() == ["Mine"] and again.get("Mine").steps[1].params["name"] == "b"
    assert again.unique_name("Mine") == "Mine (2)"
    export_workflow(w, d / "x.json")
    assert import_workflow(d / "x.json").to_dict() == w.to_dict()
    (d / "bad.json").write_text("[1,2]")
    try:
        import_workflow(d / "bad.json"); raise SystemExit("should fail")
    except ValueError:
        pass
    (d / "wf.json").write_text("{ not json")                                  # damaged file is kept aside, not fatal
    assert WorkflowStore(d / "wf.json").items == [] and (d / "wf.broken.json").exists()
    store.remove("Mine")


def test_pdf_bricks():
    if pymupdf is None:
        print("  (pymupdf missing: PDF bricks skipped)")
        return
    d, src, env = sandbox()
    for n, pages in (("a.pdf", 2), ("b.pdf", 3)):
        doc = pymupdf.open()
        for i in range(pages):
            doc.new_page().insert_text((72, 100), f"{n} page {i + 1}")
        doc.save(src / n); doc.close()
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "pdf"}), ("sort", {}),
                          ("merge_pdf", {"name": "M", "numbers": "{n}"})), env=env)
    assert rep.ok and pymupdf.open(rep.files[0]).page_count == 5, rep.message
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "pdf"}), ("split_pdf", {"every": 1})), env=env)
    assert len(rep.files) == 5
    rep = run_workflow(wf(("folder", {"folder": str(src), "types": "pdf"}), ("keep_pages", {"pages": "2-"}),
                          ("stamp_pdf", {"watermark": "DRAFT"}), ("compress_pdf", {})), env=env)
    assert rep.ok and sorted(pymupdf.open(f).page_count for f in rep.files) == [1, 2], rep.message


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("workflow engine ok")
