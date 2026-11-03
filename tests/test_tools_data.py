"""Smoke tests for file_picker / name_matcher / sheet_sync (run under xvfb)."""
import os
import tempfile
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture(scope="module")
def app():
    os.environ["UTILSHUB_HOME"] = tempfile.mkdtemp()
    from utilshub.app import App
    try:
        a = App()
    except tk.TclError:
        pytest.skip("no display")
    yield a
    a.quit()


def pump(app, n=40):
    for _ in range(n):
        app.root.update()
        app.root.after(20)


def test_find_hits():
    from utilshub.tools.file_picker import find_hits, find_duplicates
    files = [Path("/x/John Smith CV.pdf"), Path("/x/ahmed_ali.docx"), Path("/x/other.txt")]
    assert len(find_hits(["john smith"], files, "contains")["john smith"]) == 1
    assert find_hits(["Smith John CV"], files, "fuzzy", threshold=0.8)["Smith John CV"]
    assert find_hits([r"ali\b"], files, "regex")[r"ali\b"]
    assert find_hits(["nobody"], files, "exact")["nobody"] == []


def test_file_picker_ui(app, tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    for n in ("John Smith.pdf", "Mary Jones.pdf", "x.txt"):
        (src / n).write_text("hi")
    app.show_tool("file_picker")
    t = app.tools["file_picker"]
    t.vars["source"].set(str(src))
    t.vars["dest"].set(str(dst))
    t.set_names(["john smith", "nobody"])
    t.preview(); pump(app)
    assert t.missing == ["nobody"] and len(t.rows) == 1
    t.apply(); pump(app)
    assert (dst / "John Smith.pdf").exists()
    t.undo()
    assert not (dst / "John Smith.pdf").exists()


def test_name_matcher_and_sync(app, tmp_path):
    import openpyxl
    a, b = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    for p, rows in ((a, [["Name", "Phone"], ["Ahmed Ali", "1"], ["John Smith", "2"], ["Zed", "3"]]),
                    (b, [["Name", "Phone"], ["Ali Ahmed", "11"], ["Smith John", "2"], ["New Guy", "9"]])):
        wb = openpyxl.Workbook()
        for r in rows:
            wb.active.append(r)
        wb.save(p)
    app.show_tool("name_matcher")
    nm = app.tools["name_matcher"]
    nm.pa.path.set(str(a)); nm.pb.path.set(str(b))
    nm.pa.load(); nm.pb.load()
    nm.compare(); pump(app)
    assert [r.chosen for r in nm.rows][:2] == [0, 1] and nm.rows[2].chosen is None
    # Single-file mode (typed names in A against file B) + append to CSV
    nm.a_mode.set("text"); nm._a_mode_changed()
    nm.a_text.delete("1.0", "end"); nm.a_text.insert("1.0", "Ahmed Ali\nNobody")
    nm.compare(); pump(app)
    assert len(nm.rows) == 2 and nm.rows[0].chosen == 0 and nm.rows[1].chosen is None
    csv_out = tmp_path / "matches.csv"
    app.settings.set("name_matcher.append_file", str(csv_out))
    nm.add_to_file()
    assert csv_out.exists() and "Ahmed Ali" in csv_out.read_text(encoding="utf-8-sig")
    # OCR & Match and Passport tools
    txt_file = tmp_path / "sample.txt"
    txt_file.write_text("P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\nL898902C36UTO7408122F1204159ZE184226B<<<<<10\n", encoding="utf-8")
    app.show_tool("ocr_match")
    om = app.tools["ocr_match"]
    om.add_paths([str(txt_file)]); pump(app)
    om.patterns.delete("1.0", "end"); om.patterns.insert("1.0", "ERIKSSON")
    om.find(); pump(app)
    assert len(om.results.all_ids()) == 1
    app.show_tool("passport")
    pt = app.tools["passport"]
    pt.add_paths([str(txt_file)]); pump(app)
    assert len(pt.rows) == 1 and next(iter(pt.rows.values()))["surname"] == "ERIKSSON"
    nm.choose  # callable
    app.show_tool("sheet_sync")
    wb = openpyxl.Workbook()
    for r in [["Name", "Phone"], ["ahmed ali", "11"], ["John Smith", "2"], ["New Guy", "9"]]:
        wb.active.append(r)
    wb.save(b)
    ss = app.tools["sheet_sync"]
    ss.old.path.set(str(a)); ss.new.path.set(str(b))
    ss.old.load(); ss.new.load()
    ss.key.set("Name")
    ss.preview()
    st = ss.plan.stats()
    assert st["added"] == 1 and st["cells"] == 1 and st["missing"] == 1
    out = tmp_path / "out.xlsx"
    from utilshub.core.syncing import apply_plan
    apply_plan(ss.plan, ss.old.book, out)
    assert out.exists()
