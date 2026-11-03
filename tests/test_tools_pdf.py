"""GUI smoke tests for DocPacker and PDF Desk (run with xvfb: xvfb-run -a python tests/test_tools_pdf.py)."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["UTILSHUB_HOME"] = tempfile.mkdtemp()

import pymupdf as fitz
from PIL import Image, ImageDraw


def make_samples(d: Path):
    def pdf(name, lines, n=1):
        doc = fitz.open()
        for i in range(n):
            pg = doc.new_page()
            for k, l in enumerate(lines):
                pg.insert_text((60, 100 + 20 * k), l, fontsize=12)
        doc.save(d / name); doc.close()
    pdf("bank_letter.pdf", ["Account number and IBAN confirmation", "IBAN GB82WEST12345698765432", "SWIFT: WESTGB2L",
                            "Account number 12345678", "Confidential", "Bank statement"], 2)
    pdf("medical.pdf", ["Medical report", "IGRA negative", "HIV negative", "HCV negative", "HBsAg negative", "Laboratory"])
    im = Image.new("RGB", (600, 800), "white"); ImageDraw.Draw(im).ellipse((150, 150, 450, 450), fill="gray")
    im.save(d / "photo.jpg")


def pump(app, cond, timeout=60):
    import time
    end = time.time() + timeout
    while time.time() < end:
        app.root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def main():
    from utilshub.app import App
    d = Path(tempfile.mkdtemp()); make_samples(d)
    app = App(); app.root.update()
    assert "docpacker" in app.tools and "pdf_desk" in app.tools, list(app.tools)

    # ---- PDF Desk
    app.show_tool("pdf_desk"); t = app.tools["pdf_desk"]
    t.add_paths([str(d)])
    assert pump(app, lambda: len(t.grid.items) == 4), len(t.grid.items)
    t.grid.items[0].selected = True; t.grid.rotate_selected(90)
    out = d / "merged.pdf"
    t._ask_save = lambda initial: str(out)
    t.put("open_after", False); t.put("page_numbers", "{n}/{total}"); t.put("compression", 1)
    t.merge_save()
    assert pump(app, lambda: out.exists() and not app.jobs.active()), "merge"
    with fitz.open(out) as m:
        assert len(m) == 4, len(m)
    for it in t.grid.items: it.selected = it.page == 0
    t.every_var.set("2"); app.settings.set("pdf_desk.output_folder", str(d / "parts"))
    t.split_every()
    assert pump(app, lambda: (d / "parts").exists() and len(list((d / "parts").glob("*.pdf"))) == 2 and not app.jobs.active()), "split"
    print("pdf_desk ok")

    # ---- DocPacker
    app.show_tool("docpacker"); p = app.tools["docpacker"]
    p.name_var.set("John Smith")
    p.add_paths([str(d / "bank_letter.pdf"), str(d / "medical.pdf"), str(d / "photo.jpg")])
    assert pump(app, lambda: len(p.entries) == 3 and not any(e.busy for e in p.entries.values())), "read"
    types = {Path(k).name: e.dtype for k, e in p.entries.items()}
    print("recognised:", types)
    assert types["bank_letter.pdf"] == "bank" and types["medical.pdf"] == "medical", types
    p.table.tree.selection_set(str(d / "bank_letter.pdf")); app.root.update()
    p.last_output = ""
    p.settings.set("docpacker.output_folder", str(d / "pack")); p.put("open_after", False)
    p.settings.set("docpacker.custom_fields", ["Passport no"])
    p.settings.set("docpacker.output_subfolder", "{surname}")
    p.settings.set("docpacker.parts_folder", "{surname}/parts")
    p.settings.set("docpacker.cover_title", "ملف المستندات - {name}")
    p.settings.set("docpacker.cover_lines", ["Passport: {passport_no}", "{date}"])
    p.settings.set("docpacker.append_file", str(d / "packages.csv"))
    p.custom_vars["Passport no"].set("P998877")
    import utilshub.tools.docpacker as dp
    dp.confirm = lambda *a, **k: True
    dp.error = lambda parent, msg, *a: (_ for _ in ()).throw(AssertionError(msg))
    p.build_package()
    ok = pump(app, lambda: p.last_output and not app.jobs.active(), 20)
    if not ok: print([(j.status, j.error) for j in app.jobs.jobs.values()][-3:])
    assert ok, "pack"
    with fitz.open(p.last_output) as m:
        print("package pages:", len(m)); assert len(m) >= 4
    assert (d / "pack" / "Smith" / "parts").exists()
    assert (d / "packages.csv").exists() and "P998877" in (d / "packages.csv").read_text(encoding="utf-8-sig")
    assert "MISSING" in p.report_text() and "P998877" in p.report_text()
    # Split a multi-page file into parts and unsplit it
    bank_entry = p.entries[str(d / "bank_letter.pdf")]
    p.split_entry(bank_entry, [("bank", [0]), ("medical", [1])])
    p.recheck()
    assert sum(1 for e in p.entries.values() if e.path == str(d / "bank_letter.pdf")) == 2
    p.unsplit(str(d / "bank_letter.pdf"))
    assert sum(1 for e in p.entries.values() if e.path == str(d / "bank_letter.pdf")) == 1
    p.assign([str(d / "photo.jpg")], "passport"); app.root.update()
    print("docpacker ok")
    app.quit()


if __name__ == "__main__":
    main()
