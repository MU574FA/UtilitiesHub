"""GUI smoke test for CV Sorter + Screenshot (needs a display: run under xvfb-run)."""
import os
import shutil
import tempfile
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

CV = """John Smith
Senior Software Engineer at Acme Inc.
Email: john.smith@example.com
Phone: +1 415 555 0134
Python, SQL, Docker
8 years of experience
"""


def pump(app, cond, timeout=20):
    import time
    end = time.time() + timeout
    while time.time() < end:
        app.root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def app():
    home = tempfile.mkdtemp()
    os.environ["UTILSHUB_HOME"] = home
    try:
        from utilshub.app import App
        a = App()
    except tk.TclError:
        pytest.skip("no display")
    yield a
    try:
        a.quit()
    except Exception:
        pass
    shutil.rmtree(home, ignore_errors=True)


def test_cv_sorter_auto_and_review(app):
    work = Path(tempfile.mkdtemp())
    (work / "cv1.txt").write_text(CV, encoding="utf-8")
    (work / "cv2.txt").write_text("just some words without any fields\n", encoding="utf-8")
    app.settings.set("cv_sorter.watch_folder", str(work))
    app.settings.set("cv_sorter.auto_mode", "clean")
    app.settings.set("cv_sorter.popup", False)
    app.show_tool("cv_sorter")
    tool = app.tools["cv_sorter"]
    tool.process_folder()
    assert pump(app, lambda: len(tool.items) == 2 and all(i.status != "queued" for i in tool.items.values())
                and tool.counts["saved"] + sum(i.status == "review" for i in tool.items.values()) == 2)
    saved = [i for i in tool.items.values() if i.status == "saved"]
    assert saved and saved[0].saved_as == "John Smith.txt"
    assert (work / "Sorted" / "John Smith.txt").exists()
    review = [i for i in tool.items.values() if i.status == "review"]
    assert len(review) == 1
    tool.open_review(review[0])
    app.root.update()
    assert tool.dialog.winfo_exists()
    tool.dialog.form.assign("name", "Jane Doe")
    app.settings.set("cv_sorter.folder_template", "{name|last}")
    csv_out = work / "cv_append.csv"
    app.settings.set("cv_sorter.append_file", str(csv_out))
    tool.dialog.save(False)
    assert review[0].status == "saved" and (work / "Sorted" / "Doe" / "Jane Doe.txt").exists()
    assert csv_out.exists() and "Jane Doe" in csv_out.read_text(encoding="utf-8-sig")
    tool.undo_last()
    assert (work / "cv2.txt").exists()


def test_cv_sorter_watcher(app):
    work = Path(tempfile.mkdtemp())
    app.settings.set("cv_sorter.watch_folder", str(work))
    app.settings.set("cv_sorter.popup", False)
    app.settings.set("cv_sorter.watch", True)
    app.show_tool("cv_sorter")
    tool = app.tools["cv_sorter"]
    assert tool.auto_running()
    (work / "new.txt").write_text(CV, encoding="utf-8")
    assert pump(app, lambda: tool.counts["saved"] == 1, 30)
    tool.stop_auto()


def test_screenshot(app):
    from PIL import Image
    app.show_tool("screenshot")
    t = app.tools["screenshot"]
    from utilshub.tools.screenshot import Capture
    cap = Capture(Image.new("RGB", (50, 50), "white"), "test")
    cap.text = "Call me on +964 770 123 4567 or mail a.b@example.org"
    t.extract_into(cap)
    assert cap.values["email"] == ["a.b@example.org"]
    t.add_row(cap, {"phone": "9647701234567", "email": "a.b@example.org"})
    assert len(t.rows) == 1 and len(t.table.tree.get_children()) == 1
    t.delete_selected()
    t.table.tree.selection_set(t.table.tree.get_children())
    t.delete_selected()
    assert not t.rows
    from utilshub.tools.screenshot import CapturePopup
    p = CapturePopup(t, cap, lambda c, f: None)
    app.root.update()
    p.win.destroy()
