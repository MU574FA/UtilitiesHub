"""Autocomplete UI logic with a fake tkinter: pop-up, keys, shortcuts, right-click menu, settings panel."""
import sys, tempfile, logging
HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parents[1]))
logging.disable(logging.CRITICAL)
from pathlib import Path
import faketk
faketk.install()

# ---- give the fake text widgets real editing semantics (caret, insert, delete, index) -------------------------------
class Edit:
    def _init_edit(self):
        self._s, self._c = "", 0
    def get(self, a=None, b=None):
        if isinstance(self, faketk.Text):
            return self._s if a == "1.0" and b in ("end-1c", "end") else self._s[:self._c] if b == "insert" else self._s
        return self._s
    @staticmethod
    def _n(i):
        return int(str(i).split("+")[1][:-1]) if "+" in str(i) else 0
    def delete(self, a, b=None):
        if isinstance(self, faketk.Text):
            a, b = self._n(a), self._n(b)
        self._s = self._s[:a] + self._s[b:]
        self._c = min(self._c, len(self._s))
    def insert(self, i, s, *tags):
        i = self._n(i) if isinstance(self, faketk.Text) else i
        self._s = self._s[:i] + s + self._s[i:]
        self._c = i + len(s)
    def index(self, i):
        return self._c
    def icursor(self, p): self._c = p
    def mark_set(self, name, idx): self._c = self._n(idx)
    def bbox(self, i): return (0, 0, 8, 16)
    def selection_present(self): return False
for cls in (faketk.Entry, faketk.Text, faketk.Combobox):
    for name in ("get", "delete", "insert", "index", "icursor", "mark_set", "bbox", "selection_present"):
        setattr(cls, name, getattr(Edit, name))
    cls._n = staticmethod(Edit.__dict__["_n"].__func__)
    orig = cls.__init__
    def init(self, master=None, textvariable=None, _o=orig, **kw):
        _o(self, master, **kw) if cls is faketk.Text else _o(self, master, **kw)
        self._s, self._c = "", 0
    cls.__init__ = init
def set_text(w, s):
    w._s, w._c = s, len(s)
faketk.Widget.winfo_rootx = faketk.Widget.winfo_rooty = lambda self: 0
faketk.Widget.winfo_screenwidth = lambda self: 1920
faketk.Widget.winfo_screenheight = lambda self: 1080
faketk.Widget.winfo_reqwidth = lambda self: 200
faketk.Widget.winfo_reqheight = lambda self: 100
faketk.Widget.winfo_height = lambda self: 20
faketk.Widget.wm_overrideredirect = faketk.Widget.wm_geometry = lambda self, *a: None
faketk.Widget.deiconify = lambda self: setattr(self, "_shown", True)
faketk.Widget.withdraw = lambda self: setattr(self, "_shown", False)
faketk.Widget.tk = type("T", (), {"splitlist": staticmethod(lambda v: tuple(v))})()
faketk.Widget.cget = lambda self, k: self._kw.get(k, "")
faketk.Widget.focus_set = lambda self: None
L = faketk.Listbox
L.itemconfigure = lambda self, *a, **k: None
L.selection_clear = lambda self, *a: setattr(self, "_sel", ())
L.see = lambda self, i: None
L.nearest = lambda self, y: 0
L.delete = lambda self, a, b=None: self._items.clear()

import fakeapp
from fakeapp import FakeApp
from utilshub.core import autocomplete as A
from utilshub.core.schema import Opt
import tkinter as tk
from tkinter import ttk

results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))

tmp = Path(tempfile.mkdtemp())
app = FakeApp(tmp)
app.settings.register([Opt("match.job_title_keywords", [], "x", "list"), Opt("visa.visa_types", [], "x", "list"),
                       Opt("visa.custom_fields", [], "x", "list"), Opt("docpacker.custom_fields", [], "x", "list"),
                       Opt("cv_sorter.fields", [], "x", "list"), Opt("screenshot.fields", [], "x", "list")])
app.autocomplete = A.create(app.settings, tmp / "learned.json")
app.autocomplete.register(A.FieldDef("t.job", "Job", "T", keywords=["Software Engineer", "Welder", "Accountant"], shortcuts={"se": "Software Engineer"}),
                          A.FieldDef("t.note", "Note", "T", mode="text", shortcuts={"nr": "No response", "sig": "Best,\n{cursor}"}))
class Toast:
    def __init__(s): s.msgs = []
    def show(s, m, level="info", ms=0): s.msgs.append(m)
app.toast = Toast()
svc = app.autocomplete
from utilshub.ui.autocomplete import attach

class Ev:
    def __init__(self, w, keysym, state=0): self.widget, self.keysym, self.state, self.x_root, self.y_root = w, keysym, state, 0, 0
def fire(w, seq, keysym="", state=0):
    out = None
    for fn in w._bindings.get(seq, []):
        r = fn(Ev(w, keysym, state))
        if r == "break": out = "break"
    faketk.flush()
    return out
def type_text(w, s):
    for ch in s:
        w._s = w._s[:w._c] + ch + w._s[w._c:]; w._c += 1
        fire(w, "<KeyRelease>", ch)

print("\n== pop-up and keys")
root = app.root
e = ttk.Entry(root)
ctl = attach(e, "t.job")
check("attach returns a controller", ctl is not None)
type_text(e, "wel")
check("typing opens the list with the match", ctl.result is not None and [i.text for i in ctl.result.items] == ["Welder"], ctl.result)
check("the pop-up window is shown", ctl.popup._shown is True and ctl.listbox.get(0).strip() == "Welder")
check("Enter without choosing passes through (form can submit)", fire(e, "<KeyPress>", "Return") is None and ctl.result is None and e._s == "wel")
type_text(e, "")
set_text(e, "a"); fire(e, "<KeyRelease>", "a")
check("single letter lists several", ctl.result is not None and len(ctl.result.items) >= 2)
check("Down moves and is swallowed", fire(e, "<KeyPress>", "Down") == "break" and ctl.navigated)
check("Enter after choosing accepts", fire(e, "<KeyPress>", "Return") == "break" and e._s in ("Software Engineer", "Welder", "Accountant"), e._s)
set_text(e, "wel"); fire(e, "<KeyRelease>", "l")
check("Tab accepts the top suggestion", fire(e, "<KeyPress>", "Tab") == "break" and e._s == "Welder" and ctl.result is None, e._s)
set_text(e, "wel"); fire(e, "<KeyRelease>", "l")
check("Escape closes without changing", fire(e, "<KeyPress>", "Escape") == "break" and e._s == "wel" and ctl.result is None)
set_text(e, "wel")
check("no pop-up after Left/Home", (fire(e, "<KeyRelease>", "Left"), ctl.result)[1] is None)
svc.settings.set("autocomplete.auto_popup", False)
type_text(e, "d")
check("auto_popup off: nothing opens while typing", ctl.result is None)
fire(e, "<Control-space>")
check("... but Ctrl+Space opens the list", ctl.result is not None)
fire(e, "<KeyPress>", "Escape")
svc.settings.set("autocomplete.auto_popup", True)

print("\n== shortcuts")
set_text(e, "se")
check("Space expands a shortcut in a value box (space swallowed)", fire(e, "<KeyPress>", "space") == "break" and e._s == "Software Engineer", e._s)
check("Backspace right after puts shortcut + space back", fire(e, "<KeyPress>", "BackSpace") == "break" and e._s == "se ", repr(e._s))
check("... and a second Space does not expand again (a space is already there)", fire(e, "<KeyPress>", "space") is None)
set_text(e, "see")
check("non-shortcut Space is left alone", fire(e, "<KeyPress>", "space") is None and e._s == "see")
svc.settings.set("autocomplete.expand_on_space", False)
set_text(e, "se")
check("expand_on_space off: Space does nothing", fire(e, "<KeyPress>", "space") is None and e._s == "se")
svc.settings.set("autocomplete.expand_on_space", True)

t = tk.Text(root)
attach(t, "t.note")
set_text(t, "called nr")
check("text box: shortcut expands mid-sentence with a trailing space", fire(t, "<KeyPress>", "space") == "break" and t._s == "called No response ", repr(t._s))
set_text(t, "x sig")
fire(t, "<KeyPress>", "space")
check("multi-line expansion puts the caret at {cursor}", t._s == "x Best,\n " and t._c == 8, (t._s, t._c))

print("\n== per-field switch, master switch, right-click menu")
set_text(e, "wel"); fire(e, "<KeyRelease>", "l")
menu_items = []
class M(faketk.Menu):
    def tk_popup(self, *a): menu_items.append(self)
tk.Menu = M
fire(e, "<Button-3>")
labels = [x[1] for x in menu_items[-1].entries if x[0] == "command"]
check("menu offers on/off, keyword, shortcut, settings", any("ON" in l for l in labels) and any("keyword" in l for l in labels)
      and any("shortcut" in l for l in labels) and any("Keywords & shortcuts" in l for l in labels), labels)
next(x for x in menu_items[-1].entries if "turn off" in x[1])[2]()
check("menu turns this box off", not svc.config("t.job").enabled and ctl.result is None)
set_text(e, "wel"); fire(e, "<KeyRelease>", "l")
check("a disabled box shows nothing", ctl.result is None)
set_text(e, "se")
check("... and does not expand shortcuts", fire(e, "<KeyPress>", "space") is None and e._s == "se")
check("other boxes keep working", attach(ttk.Entry(root), "t.note") is not None and svc.is_enabled("t.note"))
fire(e, "<Button-3>")
next(x for x in menu_items[-1].entries if "turn on" in x[1])[2]()
check("menu turns it back on", svc.config("t.job").enabled)
svc.set_master(False)
set_text(e, "wel"); fire(e, "<KeyRelease>", "l")
check("master off silences every box", ctl.result is None)
svc.set_master(True)
fire(e, "<Button-3>")
next(x for x in menu_items[-1].entries if "Keywords & shortcuts" in x[1])[2]()
check("'Keywords & shortcuts…' opens the settings page on this box", app.opened[-1] == "autocomplete" and svc.pending_select == "t.job", (app.opened, svc.pending_select))
sel_e = ttk.Entry(root); c2 = attach(sel_e, "t.job"); set_text(sel_e, "Pilot")
fire(sel_e, "<Button-3>")
next(x for x in menu_items[-1].entries if "keyword" in x[1].lower() and "Add" in x[1])[2]()
check("'add as keyword' stores the text of the box", "Pilot" in svc.config("t.job").keywords and app.toast.msgs)

print("\n== combobox values, learning, read-only")
cb = ttk.Combobox(root); cb._kw["values"] = ("Agency A", "Agile")
cbc = attach(cb, "visa.group", extra=lambda: ["Batch 12"])
set_text(cb, "ag"); fire(cb, "<KeyRelease>", "g")
check("combobox drop-down values are suggested", cbc.result is not None and {i.text for i in cbc.result.items} == {"Agency A", "Agile"}, cbc.result)
ro = ttk.Combobox(root, state="readonly")
check("read-only boxes get no autocomplete", attach(ro, "visa.group") is None)
no_app = faketk.Widget(None)
no_app.winfo_toplevel = lambda: type("R", (), {})()
check("no app -> harmless no-op", attach(ttk.Entry(faketk.Widget(None)), "x") is None)
g = ttk.Entry(root); gc = attach(g, "visa.group", extra=lambda: ["Batch 12"])
set_text(g, "bat"); fire(g, "<KeyRelease>", "t")
check("extra values (existing groups) are suggested", gc.result and [i.text for i in gc.result.items] == ["Batch 12"])
fire(g, "<KeyPress>", "Escape")
set_text(g, "Night shift"); fire(g, "<FocusOut>")
check("leaving a box remembers its value", svc.learned.get("visa.group")[0] == "Night shift")
pw = ttk.Entry(root); attach(pw, "visa.passport_no"); svc.set_enabled("visa.passport_no", True)
set_text(pw, "AB123456"); fire(pw, "<FocusOut>")
check("passport numbers are never remembered", svc.learned.get("visa.passport_no") == [])
fire(e, "<Destroy>")
check("destroying a box cleans up", ctl._dead and ctl.popup is None)

print("\n== settings panel")
from utilshub.ui.autocomplete_ui import AutocompletePanel
from utilshub.core.fields import default_screenshot_fields
svc.settings.set("screenshot.fields", default_screenshot_fields())
svc.settings.set("visa.custom_fields", ["Agency"])
svc.pending_select = "visa.position"
panel = AutocompletePanel(faketk.Widget(root), app)
check("panel lists every field plus group rows", len(panel._shown) == len(svc.fields()) and panel.tree.exists("g:Visa Tracker"))
check("panel lists Extract group with fields disabled by default",
      panel.tree.exists("g:Extract")
      and panel.tree.exists("badge_extractor.job_title")
      and panel.tree.exists("screenshot.title")
      and panel.tree.item("badge_extractor.job_title", "values")[0] == "—"
      and panel.tree.item("screenshot.title", "values")[0] == "—")
check("pending selection is shown", panel.current == "visa.position" and "Welder" in panel.kw._s, panel.current)
check("custom field appears", "visa.custom.agency" in panel._shown)
panel.tree.identify_region = lambda x, y: "cell"; panel.tree.identify_column = lambda x: "#1"; panel.tree.identify_row = lambda y: "visa.nationality"
e1 = type("E", (), {"x": 1, "y": 1})()
check("click on the ✔ toggles that box", panel._click(e1) == "break" and not svc.config("visa.nationality").enabled and panel.tree.item("visa.nationality", "values")[0] == "—")
panel._bulk(False)
check("Disable all shown", not any(svc.config(f).enabled for f in panel._shown))
panel._bulk(True)
check("Enable all shown", all(svc.config(f).enabled for f in panel._shown))
panel.select("t.job")
set_text(panel.kw, "Alpha\nBeta"); panel._flush_keywords()
check("editing keywords saves", svc.config("t.job").keywords == ["Alpha", "Beta"])
panel.abbr_var.set("al"); panel.text_var.set("Alpha team"); panel._sc_save()
check("adding a shortcut saves and lists it", svc.config("t.job").shortcuts.get("al") == "Alpha team" and panel.sc.exists("al"))
panel.abbr_var.set("al b"); panel.text_var.set("x"); panel._sc_save()
check("an invalid shortcut is rejected with a message", "spaces" in panel.sc_msg._kw.get("text", "") and "al b" not in svc.config("t.job").shortcuts)
panel.sc.selection_set("al"); panel._sc_delete()
check("deleting a shortcut", "al" not in svc.config("t.job").shortcuts)
panel.on_var.set(False); panel._toggle_on()
check("the box switch in the editor works", not svc.config("t.job").enabled)
panel._reset()
check("reset puts the defaults back", svc.config("t.job").enabled and "Welder" in svc.config("t.job").keywords and not svc.is_customised("t.job"))
panel.select("visa.passport_no")
check("sensitive boxes explain why they are never remembered", "Never remembered" in panel.learn_note._kw.get("text", ""), panel.learn_note._kw)
panel.filter.set("nation"); 
check("filter narrows the list", panel._shown == ["screenshot.nationality", "visa.nationality"], panel._shown)
sel_before = len(svc._listeners)
panel._destroy()
check("closing the panel unsubscribes", len(svc._listeners) == sel_before - 1)

print()
bad = [n for n, ok in results if not ok]
print(f"{len(results) - len(bad)}/{len(results)} autocomplete UI checks passed" + (f"; FAILED: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
