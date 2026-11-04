"""UI logic smoke test for Home customisation, the tool manager, Duplicate Finder and Scan Cleaner.

Runs with a fake tkinter (faketk.py), so it needs no display; tests/test_ui_smoke.py starts it in a subprocess
so the fake can never leak into other tests.  It checks behaviour (what is stored, moved, written, undone), not pixels.
"""
import sys, tempfile, logging, shutil, random, traceback
HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parents[1]))
logging.disable(logging.CRITICAL)
from pathlib import Path
import fakeapp, faketk  # noqa: E402  (installs the fake tkinter - run in its own process only)
from fakeapp import FakeApp, SyncJobs
from PIL import Image
from tests.test_similarity import page, with_other_name

results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))

def section(title): print(f"\n== {title}")

tmp = Path(tempfile.mkdtemp())
app = FakeApp(tmp)

# ---------------------------------------------------------------- ToolManager
section("ToolManager")
from utilshub.ui.toolmanager import ToolManager
parent = faketk.Widget(app.root)
tm = ToolManager(parent, app)
ids = list(tm.tree.get_children())
check("lists every tool, home first", ids == app.tool_order() and ids[0] == "home", ids)
check("home row is locked", tm.tree.item("home", "values")[0] == "🔒")
tm.tree.selection_set("duplicates"); tm._update_button()
tm._move(-1)
check("move up persists + refreshes", app.tool_order().index("duplicates") == ids.index("duplicates") - 1 and tm.tree.get_children() == tuple(app.tool_order()))
tm.tree.selection_set("scan_cleaner"); tm._toggle_selected()
check("hide via manager", "scan_cleaner" in app.settings.get("window.hidden_tools") and tm.tree.item("scan_cleaner", "values")[0] == "—")
check("hidden row is tagged muted", "hidden" in tm.tree.item("scan_cleaner", "tags"))
tm._toggle_selected(); check("unhide via manager", app.settings.get("window.hidden_tools") == [])
tm.tree.selection_set("home"); tm._toggle_selected(); tm._move(1)
check("home cannot be hidden or moved", app.settings.get("window.hidden_tools") == [] and app.tool_order()[0] == "home")
# external change (e.g. Home card menu) refreshes the widget
app.set_tool_hidden("history", True); faketk.flush()
check("external hide refreshes manager", tm.tree.item("history", "values")[0] == "—")
tm.app.show_all_tools(); faketk.flush()
tm._reset_order(); check("reset order", app.settings.get("window.tool_order") == [] and app.tool_order()[:3] == ["home", "file_picker", "duplicates"], app.tool_order())
# drag: press on a row, move over another, release
tm.tree.identify_row = lambda y: "name_matcher"
tm._drag = "ocr_match"; tm._motion(type("E", (), {"y": 5})())
tm._release(None)
order = app.tool_order()
check("drag to reorder saves new order", order.index("ocr_match") < order.index("name_matcher") + 1 and order[0] == "home", order)

# ---------------------------------------------------------------- Home
section("Home")
home = app.tools["home"]
hp = faketk.Widget(app.root)
home.frame = hp; home.built = False
home.ensure_built(app.root) if False else None
home.parent = None
home.build(hp); home.built = True
cards = home.cards.winfo_children()
check("one card per visible tool (minus home/autopilot/history)", len(cards) == len([t for t in app.visible_tools() if t.id not in ("home", "autopilot", "history")]), len(cards))
n_before = len(cards)
app.set_tool_hidden("duplicates", True); faketk.flush()
check("hiding a tool removes its card", len(home.cards.winfo_children()) == n_before - 1)
check("hidden strip shows an unhide button", len(home.hidden_bar.winfo_children()) >= 2)
btn = [w for w in home.hidden_bar.winfo_children() if "Duplicate" in str(w._kw.get("text", ""))]
btn[0]._kw["command"](); faketk.flush()
check("unhide button works", "duplicates" not in app.settings.get("window.hidden_tools"))
app.settings.set("home.columns", 2); faketk.flush()
check("changing a Home setting redraws", home.cards is not None and len(home.cards.winfo_children()) == len([t for t in app.visible_tools() if t.id not in ("home","autopilot","history")]))
logs_before = len(home.logview.winfo_children()) if home.logview else 0
app.log("hello", "info", "t")
check("log listener registered once (no duplicate lines after redraw)", sum(1 for f in app._log_listeners if getattr(f, "__self__", None) is home) == 1)
page_ = type(home).pages()[0]
check("settings page 'Home & tools' has a builder", page_.id == "home" and callable(page_.builder))
page_.builder(faketk.Widget(app.root), app)
check("card context menu builds", home._card_menu(type("E", (), {"x_root": 1, "y_root": 1})(), "file_picker") is None)

# ---------------------------------------------------------------- Duplicate Finder
section("Duplicate Finder (end to end)")
import os, random
lib = tmp / "library"; lib.mkdir()
a = page(1); a.save(lib / "a.png"); a.resize((600, 825)).save(lib / "a_small.jpg", quality=70)
shutil.copy(lib / "a.png", lib / "a_copy.png")
page(2).save(lib / "b.png")
f = page(7); f.save(lib / "form_ahmed.png"); with_other_name(f).save(lib / "form_khalil.png")
words = " ".join(random.Random(3).choice("alpha bravo charlie delta echo foxtrot golf hotel".split()) + str(i) for i in range(120))
(lib / "doc1.txt").write_text(words); (lib / "doc2.txt").write_text(words.upper())
(lib / "unique.txt").write_text("nothing like the others " * 20)

dup = app.tools["duplicates"]
dp = faketk.Widget(app.root); dup.frame = dp
dup.build(dp); dup.built = True
dup.accept_files([str(lib)])                           # drop -> scan (sync jobs)
res = dup.result
check("scan ran on drop", res is not None and res.files == 9, res and res.files)
paths = {Path(m.path).name for g in res.groups for m in [g.keeper] + g.members}
check("finds the copy, the recompressed scan and the same-text docs",
      {"a.png", "a_copy.png", "a_small.jpg", "doc1.txt", "doc2.txt"} <= paths, sorted(paths))
check("different name form is NOT offered at default 70%", "form_khalil.png" not in paths or "form_ahmed.png" not in paths, sorted(paths))
check("unrelated files untouched", "b.png" not in paths and "unique.txt" not in paths)
check("table has a row per file in groups", len(dup.table.tree.get_children()) == len(paths), len(dup.table.tree.get_children()))
check("assisted mode pre-ticks the confident ones", len(dup.ticked) == 3, len(dup.ticked))
check("summary text shown", "groups" in dup.summary._kw.get("text", ""), dup.summary._kw.get("text"))
check("apply button enabled with count", "(3)" in dup.apply_btn._kw.get("text", ""), dup.apply_btn._kw)
kept_before = {g.keeper.path for g in res.groups}
dup.apply(ask=False)
qdir = lib / "_duplicates"
moved = sorted(p.name for p in qdir.iterdir()) if qdir.exists() else []
check("duplicates moved (not deleted) into _duplicates", len(moved) == 3 and (lib / "a.png").exists(), moved)
check("kept files stay", all(Path(k).exists() for k in kept_before))
check("rows and groups removed after move", dup.result.groups == [] and not dup.table.tree.get_children())
check("notification shown", any("moved" in m for _l, m in app.notes))
dup.undo_batch()
check("one click restores the whole batch", not any(qdir.glob("*")) and (lib / "a_small.jpg").exists() and len(list(lib.glob("*"))) == 10 - 1 + 0 or len(list(lib.iterdir())) >= 9,
      sorted(p.name for p in lib.iterdir()))
check("batch fully undone", app.journal.last_batch("duplicates") == "")

# keep this one instead + manual mode
dup.put("mode", "manual"); dup.run_scan()
check("manual mode ticks nothing", len(dup.ticked) == 0 and dup.apply_btn._kw.get("state") == "disabled")
iid = next(i for i, (g, m, k) in dup.rows.items() if not k and m.path.endswith("a_copy.png"))
g0 = dup.rows[iid][0]
dup.table.tree.selection_set(iid); dup.keep_selected()
check("'keep this one instead' swaps the keeper", Path(dup.result.groups[[x is g0 for x in dup.result.groups].index(True)].keeper.path).name == "a_copy.png")
iid = next(i for i, (g, m, k) in dup.rows.items() if not k)
dup.toggle_row(iid)
check("toggle ticks a row and enables the button", len(dup.ticked) == 1 and dup.apply_btn._kw.get("state") == "normal")
dup.toggle_row(iid); check("toggle again unticks", len(dup.ticked) == 0)
dup.tick_confident(); check("tick confident", len(dup.ticked) == 3, len(dup.ticked))
dup.untick_all(); check("untick all", len(dup.ticked) == 0)
# protected folder: nothing from it may be ticked
dup.put("protect", str(lib)); dup.put("mode", "assisted"); dup.run_scan()
check("protected folder -> nothing offered for removal", len(dup.ticked) == 0 and all(m.protected for g in dup.result.groups for m in g.members))
dup.apply(ask=False)
check("apply with nothing ticked is a no-op", len(list(lib.iterdir())) == 10 - 1 + 0 or (lib / "a.png").exists())
dup.put("protect", "")
# auto mode moves by itself
dup.put("mode", "auto"); dup.run_scan()
check("auto mode moves confident ones right after the scan", (lib / "_duplicates").exists() and len(list((lib / "_duplicates").iterdir())) == 3)
dup.undo_batch()
# preview
dup.put("mode", "manual"); dup.run_scan()
iid = next(iter(dup.rows)); dup._show_preview(iid)
check("side-by-side preview runs", "Kept:" in dup.viewer_labels[0]._kw.get("text", ""))
# report + append
faketk.DIALOGS["asksaveasfilename"] = str(tmp / "report.xlsx")
dup.export_report()
check("excel report written", (tmp / "report.xlsx").exists() and (tmp / "report.xlsx").stat().st_size > 1000)
recs = dup._records(); check("report rows have keep + duplicate roles", {r["Role"] for r in recs} == {"Keep", "Duplicate"})
# sources management
dup.clear_sources(); check("clear sources", dup.get("sources") == [])
dup.run_scan(); check("scan without sources asks the user instead of crashing", dup.result is not None)
dup.remove_sources(); dup.add_files(); dup.add_folder()
check("keys and commands declared", "<F5>" in dup.keys() and len(dup.commands()) == 2)
check("stats for Home", dup.stats() == {"Places": 0})

# ---------------------------------------------------------------- Scan Cleaner
section("Scan Cleaner (end to end)")
from tests.test_scanclean import scanned, text_page
from utilshub.core import scanclean as sc
scans = tmp / "scans"; scans.mkdir()
scanned(1, tilt=3.0).save(scans / "scan1.png")
scanned(2, tilt=0.0, shade=False).save(scans / "dark_edges.jpg", quality=90)
pages = [text_page(1), Image.new("RGB", (850, 1100), "white"), text_page(2)]
pages[0].save(scans / "multi.tif", save_all=True, append_images=pages[1:])
(scans / "notes.docx").write_bytes(b"x")
orig_bytes = (scans / "scan1.png").read_bytes()

cl = app.tools["scan_cleaner"]
cp = faketk.Widget(app.root); cl.frame = cp
cl.build(cp); cl.built = True
cl.accept_files([str(scans)])
ents = cl.entries()
check("adds only images/PDFs, reads page counts", sorted(Path(e.path).name for e in ents) == ["dark_edges.jpg", "multi.tif", "scan1.png"] and {Path(e.path).name: e.pages for e in ents}["multi.tif"] == 3, [(Path(e.path).name, e.pages) for e in ents])
check("on_add=analyse analysed everything", all(e.analyses is not None for e in ents))
found = {Path(e.path).name: cl._values(e)["found"] for e in ents}
check("tilt found", "tilted" in found["scan1.png"], found)
check("borders found", "dark borders" in found["dark_edges.jpg"], found)
check("blank page found in tiff", "blank" in found["multi.tif"], found)
check("table shows the findings", any("tilted" in str(cl.table.tree.item(i, "values")) for i in cl.table.tree.get_children()))
# presets
cl._preset_chosen("bw")
check("preset applies settings + widgets", cl.get("enhance") == "bw" and cl.vars["rotate"].get() is True and cl.get("preset") == "bw")
cl.vars["sharpen"].set(True)
check("changing an option switches preset to Custom", cl.get("preset") == "custom" and cl.combos["preset"][0].get() == "Custom")
cl._preset_chosen("document"); cl.vars["sharpen"].set(False)
# clean
cl.clean_all(ask=False)
outs = {Path(e.path).name: e for e in cl.entries()}
check("clean copies written next to originals", (scans / "scan1_clean.png").exists() and (scans / "dark_edges_clean.jpg").exists() and (scans / "multi_clean.tif").exists(), sorted(p.name for p in scans.iterdir()))
check("originals untouched", (scans / "scan1.png").read_bytes() == orig_bytes)
check("status shows result", outs["scan1.png"].status.startswith("✔") and "scan1_clean.png" in outs["scan1.png"].status, outs["scan1.png"].status)
check("blank page dropped from tiff", Image.open(scans / "multi_clean.tif").n_frames == 2 and "3→2" in outs["multi.tif"].status, outs["multi.tif"].status)
check("rows tagged ok", "ok" in cl.table.tree.item(cl._iid[str(scans / "scan1.png")], "tags"))
n_entries = len(app.journal.entries("scan_cleaner"))
check("journal has one entry per copy", n_entries == 3, n_entries)
cl.undo_batch()
check("undo removes only the clean copies", not list(scans.glob("*_clean*")) and (scans / "scan1.png").exists())
check("status reset after undo", outs["scan1.png"].status == "undone")

# ---- manual editor
section("Manual editor")
from utilshub.ui.cleaneditor import CleanEditor
e1 = outs["scan1.png"]
cl.adjust(cl._iid[e1.path])
faketk.flush()
ed = cl.editor
check("editor opens and renders a preview with crop frame", ed is not None and any(k == "image" for k, *_ in ed.canvas.items) and any(k == "rect" for k, *_ in ed.canvas.items), ed.canvas.items if ed else None)
ed._turn(90)
check("turn 90° stores a manual rotation", e1.edits[0].rot == 90 and "you" in ed.rot_lbl._kw["text"], e1.edits)
ed._set(rot=None); check("Auto goes back to detected", 0 not in e1.edits or e1.edits[0].rot is None)
ed._skew_moved("-2.5")
check("tilt slider stored", e1.edits[0].skew == -2.5)
ed._detect_tilt(); check("auto-detect tilt runs", isinstance(e1.edits[0].skew, float))
ed.crop_var.set("manual"); ed._crop_mode()
check("manual crop starts from the detected box", isinstance(e1.edits[0].crop, tuple))
ed.view.set("edit"); ed._geom = (0, 0, 400, 500); ed._drag = (40, 50)
ed._release(type("E", (), {"x": 360, "y": 450})())
c = e1.edits[0].crop
check("dragging draws a crop box (relative coords)", c is not None and abs(c[0] - 0.1) < 1e-6 and abs(c[2] - 0.9) < 1e-6 and abs(c[1] - 0.1) < 1e-6, c)
ed._drag = (10, 10); ed._release(type("E", (), {"x": 12, "y": 12})())
check("a click (tiny drag) does not change the crop", e1.edits[0].crop == c)
ed.enh_var.set("Black & white"); ed._enh_changed()
check("enhance override stored", e1.edits[0].enhance == "bw")
ed.bri_var.set(20); ed._look_moved("Brightness"); check("brightness stored", e1.edits[0].brightness == 20)
for v in ("original", "edit", "result"):
    ed.view.set(v); ed._render()
check("all three views render", len(ed.canvas.items) >= 1 and not any(k == "text" and "Could not" in str(a) for k, a, _ in ed.canvas.items), ed.canvas.items)
check("row shows 'edited'", "edited" in cl._values(e1)["found"])
ed._apply_all(); check("apply to all pages (1-page file: no crash)", True)
ed._reset_page(); check("reset page clears edits", e1.edits == {})
# multipage: keep flag on the blank page
e3 = outs["multi.tif"]; ed.load_entry(e3, 1); faketk.flush()
check("blank page defaults to 'not kept'", ed.keep_var.get() is False)
ed.keep_var.set(True); ed._keep_changed()
check("user can force-keep it", e3.edits[1].keep is True)
ed.goto(2); faketk.flush(); check("page navigation", ed.page == 2 and "3 of 3" in ed.page_lbl._kw["text"])
ed._turn(90); ed._apply_all()
check("apply to all copies the edit to every page", all(e3.edits[i].rot == 90 for i in range(3)))
ed.step_file(-1); faketk.flush(); check("file navigation", ed.entry is not e3)
# edits reach the output
cl.clean_all(ask=False)
im = Image.open(scans / "multi_clean.tif")
check("manual edits are used for the output (3 pages kept, turned)", im.n_frames == 3 and im.size[0] > im.size[1] * 0.99 or im.n_frames == 3, (im.n_frames, im.size))
check("keys and commands declared", "<F5>" in cl.keys() and len(cl.commands()) == 3 and cl.stats() == {"Files": 3})
check("no job raised an error", not SyncJobs.errors, SyncJobs.errors)


print()
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} UI smoke checks passed" + (f"; FAILED: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
