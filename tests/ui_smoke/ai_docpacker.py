"""DocPacker AI behaviour under the fake tkinter (own process).  Run: python tests/ui_smoke/ai_docpacker.py"""
import sys
from unittest import mock
try:                                                  # PyMuPDF is only needed to import the tool, not by this test
    import pymupdf  # noqa: F401
except ImportError:
    sys.modules["pymupdf"] = mock.MagicMock()
    sys.modules["fitz"] = sys.modules["pymupdf"]

import sys, tempfile, logging
from pathlib import Path as _P
_H = _P(__file__).resolve().parent
sys.path.insert(0, str(_H)); sys.path.insert(0, str(_H.parent)); sys.path.insert(0, str(_H.parents[1]))
logging.disable(logging.CRITICAL)
from pathlib import Path
import fakeapp, faketk
from fakeapp import FakeApp
from fake_ollama import FakeOllama
from utilshub.tools.docpacker import DocPacker, Entry, UNASSIGNED
from utilshub.core import aidocs, llm
from utilshub.core.readers import DocInfo
import tkinter as tk

def handler(path, payload):
    u = payload["messages"][-1]["content"]
    if "Which kind of document" in u: return {"type":"bank","confidence":0.9,"reason":"bank letter"}
    if "Extract these fields" in u: return {"name":["Jane Roe"],"surname":["Roe"],"passport_no":["X1234567"]}
    return {}

tmp = Path(tempfile.mkdtemp())
class App(FakeApp):
    pass
app = FakeApp(tmp)
app.tool_classes = app.tool_classes + [DocPacker]
app.settings.register(DocPacker.options())
dp = DocPacker(app)
dp.built = True
dp.entries = {}; dp._types=[]; dp._load_types()
dp.name_var, dp.surname_var, dp.join_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
dp.ai_note_var = tk.StringVar(); dp.ai_filled = {}; dp._ai_after=None; dp._ai_warned=set()
dp.status_var = tk.StringVar(); dp.custom_vars = {"Passport no.": tk.StringVar()}
app.settings.set("docpacker.custom_fields", ["Passport no."], save=False)
dp.frame = faketk.Widget(app.root)
dp._refresh_summary = lambda: None
msgs=[]
import utilshub.tools.docpacker as m
m.info_box = lambda parent, text: msgs.append(text)
res=[]
def check(n,c): res.append(c); print(("PASS " if c else "FAIL ")+n)

with FakeOllama(handler) as srv:
    s = app.settings
    s.set("ai.host", srv.url, save=False)
    # AI off: button explains
    dp.autofill_cover()
    check("fill with AI off explains how to enable", msgs and "AI filling is off" in msgs[-1])
    s.set("ai.enabled", True, save=False); s.set("ai.cover_mode","ai",save=False); s.set("ai.doctype_mode","ai",save=False)
    # an entry recognised by AI (SyncJobs run in place; recognise blocks through the cache)
    info = DocInfo(str(tmp/"letter.pdf"), "pdf", text="Surname: ROE\nPassport No: X1234567\nJane Roe", pages=1, page_texts=["x"])
    e = Entry(str(tmp/"letter.pdf"), info=info, busy=False); dp.entries[e.key]=e
    dp.table = type("T",(),{"update_row":lambda *a,**k:None, "selection":lambda s:[]})()
    dp._recognise(e)
    check("first look: AI answer is on its way (async in the real app)", e.pending and e.dtype=="")
    dp._recognise(e)             # the real app re-checks when the background job reports back
    check("AI recognised the document", e.dtype=="bank" and e.src=="ai" and not e.pending)
    dp.autofill_cover()
    check("name filled by AI", dp.name_var.get()=="Jane Roe")
    check("surname filled", dp.surname_var.get()=="Roe")
    check("custom field filled", dp.custom_vars["Passport no."].get()=="X1234567")
    check("AI-filled values are tracked", set(dp.ai_filled)=={"name","surname","passport_no"})
    check("note shown", "Filled by the AI" in dp.ai_note_var.get())
    dp.name_var.set("Jane Q Roe"); dp._ai_prune()
    check("editing a value removes its AI flag", "name" not in dp.ai_filled and "Full name" not in dp.ai_note_var.get())
    check("report mentions AI", "recognised by the AI" in dp.report_text() and "filled by the AI" in dp.report_text())
    # typed values are never overwritten
    dp.surname_var.set("Typed"); dp.autofill_cover()
    check("typed value kept", dp.surname_var.get()=="Typed")
print(f"{sum(res)}/{len(res)} DocPacker AI checks passed")
sys.exit(0 if all(res) else 1)
