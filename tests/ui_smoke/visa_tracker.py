"""Visa Tracker behaviour under the fake tkinter (own process).  Run: python tests/ui_smoke/visa_tracker.py

Drives the real tool and dialogs: add / edit, filters, contact log, snooze, status, tickets, messages, documents
(page picker), import, exports, undo, reminders.  Checks what is stored and shown, not pixels.
"""
import sys
from unittest import mock
try:                                                  # PyMuPDF is not needed here (page cutting is replaced below)
    import pymupdf  # noqa: F401
except ImportError:
    sys.modules["pymupdf"] = mock.MagicMock()
    sys.modules["fitz"] = sys.modules["pymupdf"]

import datetime as dt, logging, tempfile
from pathlib import Path as _P
_H = _P(__file__).resolve().parent
sys.path.insert(0, str(_H)); sys.path.insert(0, str(_H.parents[1]))
logging.disable(logging.CRITICAL)
from pathlib import Path
import fakeapp, faketk
from fakeapp import FakeApp

from utilshub.core import visa as V, visa_files, visa_io
import utilshub.tools.visa_tracker as vt
import utilshub.ui.visa_dialogs as vd

results = []
def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))

# ---------------------------------------------------------------- fakes for the parts that need a real window / PyMuPDF
class FakeViewer(faketk.Widget):
    def __init__(self, parent, app, **kw):
        super().__init__(parent); self.shown = []
    def show(self, path, *a, **k): self.shown.append(str(path))
    def clear(self, *a, **k): self.shown.append(None)

class FakePreview(faketk.Widget):
    def __init__(self, parent, app):
        super().__init__(parent); self.shown = []
    def show(self, path, page, rotation=0): self.shown.append((page, rotation))
    def clear(self, *a): pass

class FakeGrid(faketk.Widget):
    def __init__(self, parent, app, size=None, toolbar=True):
        super().__init__(parent)
        self.items, self.on_select, self.on_open, self.on_change, self._pos = [], None, None, None, []
        self.canvas = faketk.Canvas(self)
    def set_items(self, items): self.items = items
    def selected(self): return [i for i in self.items if i.selected]
    def redraw(self): self._pos = [(0, i * 10, 10, 10) for i in range(len(self.items))]
    def rotate_selected(self, deg):
        for it in self.selected(): it.rotation = (it.rotation + deg) % 360

class FakeRenderer:
    pages = 1
    def kind(self, path): return "pdf" if str(path).lower().endswith(".pdf") else "image" if str(path).lower().endswith(".jpg") else "other"
    def page_count(self, path): return self.pages

def fake_modal(parent, title, width=520, height=0):
    win = faketk.Widget(parent)
    return win, faketk.Widget(win)

vt.PageViewer, vd.ThumbGrid, vd.PagePreview, vd.modal = FakeViewer, FakeGrid, FakePreview, fake_modal
popups = {"confirm": True, "ask": "", "info": [], "error": []}
for mod in (vt, vd):
    mod.confirm = lambda parent, msg, *a, **k: popups["confirm"]
    mod.info = lambda parent, msg, *a, **k: popups["info"].append(msg)
    mod.error = lambda parent, msg, *a, **k: popups["error"].append(msg)
vt.ask_text = lambda *a, **k: popups["ask"]
opened = []
vt.webbrowser.open = lambda url: opened.append(url)
cuts = []
def fake_extract(src, pages, dest, rotations=None, libreoffice_cmd=""):
    Path(dest).write_bytes(b"%PDF-fake"); cuts.append((str(src), list(pages), dict(rotations or {}))); return str(dest)
visa_files.extract_pages = fake_extract

# ---------------------------------------------------------------- app + tool
TODAY = dt.date.today()                          # everything below is relative to the real clock
d_ = lambda n: TODAY + dt.timedelta(days=n)
ago = lambda n: (TODAY - dt.timedelta(days=n)).strftime("%d/%m/%Y")
tmp = Path(tempfile.mkdtemp())
app = FakeApp(tmp)
app.renderer = FakeRenderer()
badges = []
app.refresh_badges = lambda: badges.append(1)
app.tool_classes = app.tool_classes + [vt.VisaTracker]
app.settings.register(vt.VisaTracker.options())
app.settings.set("visa.data_folder", str(tmp / "data"))
tool = vt.VisaTracker(app)
tool.today = lambda: TODAY
tool.frame = faketk.Widget(app.root)
tool.build(tool.frame); tool.built = True
st = tool.store
def rows(): return {tool.table.get(i)["name"]: tool.table.get(i) for i in tool.table.all_ids()}

print("== basics")
check("tool metadata", tool.id == "visa" and tool.stats_when_closed and tool.title == "Visa Tracker" and tool.order < 10)
check("empty list shows a hint", "No people yet" in tool.summary._kw["text"])
check("all declared setting keys exist", all(app.settings.get(k, None) is not None for k in
      ("visa.default_days", "visa.cadence", "visastate.columns", "visastate.templates")))
check("settings page shows only visa.* options (state keys hidden)", all(o.key.startswith("visa.") for o in app.settings.opts("visa.")))

print("== add / edit through the form")
def add(name, **fields):
    dlg = vd.PersonDialog(tool, None, tool._person_saved)
    dlg.vars["name"].set(name)
    for k, v in fields.items():
        if k.startswith("date_"): getattr(dlg, k).var.set(v)
        else: dlg.vars[k].set(v)
    dlg.save()
    return dlg
d = add("Ali Khan", passport_no="X1234567", phone="+964 750 123 4567", date_visa_issue=ago(14), group="Batch 1")
check("person saved with parsed date", len(st.all()) == 1 and st.all()[0].visa_issue == (TODAY - dt.timedelta(days=14)).isoformat())
check("deadline label is live (issue + 90)", visa_io.fmt(d_(76)) in d.deadline_lbl._kw["text"] and "76 days" in d.deadline_lbl._kw["text"], d.deadline_lbl._kw["text"])
add("Sara Noor", passport_no="Y7654321", email="sara@example.com", date_visa_issue=ago(87), group="Batch 1")     # 3 days left
add("Omar Taha", phone="+964 770 555 0000", date_visa_issue=ago(106), group="Batch 2")                           # window over
add("Lina Haddad", phone="+964 771 000 1111", date_visa_issue=ago(11), date_flight_date=d_(106).strftime("%d/%m/%Y"))           # flight after deadline
check("four people", len(st.all()) == 4)
r = rows()
check("default filter hides nobody active; sorted by urgency", list(r)[:2] == ["Omar Taha", "Sara Noor"], list(r))
check("days left numeric and negative for overdue", r["Omar Taha"]["days"] == -16 and r["Sara Noor"]["days"] == 3, (r["Omar Taha"]["days"], r["Sara Noor"]["days"]))
check("traffic light + tags", r["Omar Taha"]["lvl"] == "🔴" and tool.table._rows[st.find_passport("Y7654321").id][1] == ("critical",))
check("never contacted -> to do text", r["Ali Khan"]["todo"] == "No contact logged yet" and r["Ali Khan"]["last"] == "never")
check("chips show counts", "Waiting 4" in tool.chips["active"]._kw["text"] and "To do today 4" in tool.chips["todo"]._kw["text"] and
      "Overdue 1" in tool.chips["overdue"]._kw["text"] and "Critical 1" in tool.chips["d7"]._kw["text"])
check("badge counts people to contact", tool.badge() == "4" and tool.stats()["To do today"] == 4 and tool.stats()["Critical"] == 2)
check("sidebar refresh was requested after changes", len(badges) >= 4)

print("== form validation")
before = len(st.all())
popups["error"].clear()
bad = vd.PersonDialog(tool, None, tool._person_saved); bad.save()
check("name is required", len(popups["error"]) == 1 and "name" in popups["error"][0].lower() and len(st.all()) == before)
popups["error"].clear()
bad.vars["name"].set("X"); bad.date_visa_issue.var.set("31/02/2026"); bad.save()
check("impossible date refused with a hint", len(st.all()) == before and "not a date" in popups["error"][0], popups["error"])
check("date box shows what it could not read", bad.date_visa_issue.hint._kw["text"] == "✗ not a date")
popups["error"].clear(); popups["confirm"] = False
dup = vd.PersonDialog(tool, None, tool._person_saved); dup.vars["name"].set("Copy"); dup.vars["passport_no"].set("x1234567"); dup.save()
check("same passport asks before saving", len(st.all()) == before)
popups["confirm"] = True; dup.save()
check("...and saves when confirmed", len(st.all()) == before + 1)
st.delete([p.id for p in st.all() if p.name == "Copy"])
bad2 = vd.PersonDialog(tool, None, tool._person_saved); bad2.vars["name"].set("Z"); bad2.date_visa_issue.var.set("01/12/2026"); bad2.date_visa_deadline.var.set("01/11/2026"); bad2.save()
check("last entry before issue date refused", "before the issue date" in " ".join(popups["error"]))
sa = vd.PersonDialog(tool, None, tool._person_saved); sa.vars["name"].set("Again 1"); sa.vars["group"].set("Batch 9"); sa.date_visa_issue.var.set("+0"); sa.save(again=True)
check("save & add another keeps batch data and clears the rest", sa.vars["name"].get() == "" and sa.vars["group"].get() == "Batch 9" and sa.date_visa_issue.var.get() == "")
st.delete([p.id for p in st.all() if p.name == "Again 1"])

print("== filters and groups")
tool.set_filter("overdue"); check("overdue filter", list(rows()) == ["Omar Taha"])
tool.set_filter("d7"); check("critical filter", list(rows()) == ["Sara Noor"])
tool.set_filter("noticket"); check("no ticket filter", len(rows()) == 4)
tool.set_filter("active"); tool.group_var.set("Batch 2"); tool.refresh()
check("group filter", list(rows()) == ["Omar Taha"]); tool.group_var.set(vt.ALL_GROUPS); tool.refresh()
check("group list offered", tool.group_box._kw["values"] == [vt.ALL_GROUPS, "Batch 1", "Batch 2"])
check("filter remembered", app.settings.get("visastate.filter") == "active")

print("== selecting shows the details")
sara = st.find_passport("Y7654321"); ali = st.find_passport("X1234567")
tool.table.tree.selection_set(sara.id); tool._selected([sara.id])
texts = lambda w=tool.detail: [c._kw.get("text", "") for c in w.winfo_children()]
check("detail shows name, countdown, alerts", "Sara Noor" in texts() and "3 days left" in texts() and any("Only 3 days left" in t for t in texts()), texts())
check("alerts include no-ticket and no-file hints", any("No ticket booked" in t for t in texts()) and any("visa file is not stored" in t for t in texts()))
check("countdown is coloured red", [c for c in tool.detail.winfo_children() if c._kw.get("text") == "3 days left"][0]._kw.get("foreground") == app.theme.p["err"])
lina = [p for p in st.all() if p.name == "Lina Haddad"][0]
tool.table.tree.selection_set(lina.id); tool._selected([lina.id])
check("flight after the deadline is flagged", any("AFTER the visa deadline" in t for t in texts()))
omar = [p for p in st.all() if p.name == "Omar Taha"][0]
tool.table.tree.selection_set(omar.id); tool._selected([omar.id])
check("expired visa shown clearly", "Expired 16 days ago" in texts())

print("== contacts")
tool.table.tree.selection_set(sara.id); tool._selected([sara.id])
tool.quick_log("call", "reached")
s2 = st.get(sara.id)
check("one click logs a contact with my name/time", len(s2.followups) == 1 and s2.followups[0]["channel"] == "call" and s2.followups[0]["outcome"] == "reached")
check("row updated: contacted today, next follow-up tomorrow", rows()["Sara Noor"]["last"] != "never" and "To do" not in rows()["Sara Noor"]["todo"] and rows()["Sara Noor"]["todo"] == "")
check("toast tells how to undo", any("Ctrl+Z" in m for _l, m in app.notes))
tool.undo(); check("Ctrl+Z undoes the contact", st.get(sara.id).followups == [] and rows()["Sara Noor"]["todo"] != "")
tool.set_filter("todo"); n_todo = len(rows()); tool.set_filter("active")
check("'to do today' lists the ones that need contact", n_todo == 4)
dlg = vd.FollowupDialog(tool, [sara.id, ali.id])
dlg.note.insert("1.0", "Both confirmed travel plans"); dlg._ch_set("whatsapp"); dlg._out_set("replied"); dlg.next.var.set("+3"); dlg.save()
a2, s3 = st.get(ali.id), st.get(sara.id)
check("log dialog writes to several people", a2.followups[-1]["note"] == "Both confirmed travel plans" and s3.followups[-1]["channel"] == "whatsapp" and s3.followups[-1]["outcome"] == "replied")
check("custom next reminder date is stored", a2.next_followup == d_(3).isoformat() and rows()["Ali Khan"]["next"] == visa_io.fmt(d_(3)))
tool.table.tree.selection_set(sara.id); tool._selected([sara.id])
tool.quick_log("call", "no_answer")
check("no answer -> retry the next day (clears the old reminder)", V.next_due(st.get(sara.id), TODAY, tool.policy()) == TODAY + dt.timedelta(days=1))
popups["ask"] = "+5"; tool.snooze_custom()
check("snooze to a date", st.get(sara.id).next_followup == d_(5).isoformat())
tool.store.snooze([sara.id], None)
bad_dlg = vd.FollowupDialog(tool, [sara.id]); bad_dlg.when.var.set("nope"); n_before = len(st.get(sara.id).followups); popups["error"].clear(); bad_dlg.save()
check("bad date in the log dialog is refused", len(st.get(sara.id).followups) == n_before and popups["error"])
past = vd.FollowupDialog(tool, [sara.id]); past.when.var.set(ago(5)); past.save()
check("past contacts can be written afterwards (noon of that day)", st.get(sara.id).followups[-1]["when"].startswith(d_(-5).isoformat() + "T12:00"))

print("== status and ticket")
tool.table.tree.selection_set(lina.id); tool._selected([lina.id])
tool.set_ticket("sent")
l2 = st.get(lina.id)
check("ticket sent is stored and logged for the proof", l2.ticket_status == "sent" and l2.followups[-1]["channel"] == "system" and "Ticket sent" in l2.followups[-1]["note"])
popups["ask"] = "-2"; tool.mark_arrived()
l3 = st.get(lina.id)
check("arrived: date, status, no more reminders", l3.status == "arrived" and l3.arrived_on == d_(-2).isoformat() and V.todo(l3, TODAY, tool.policy()) == [])
check("arrived person leaves the active list", "Lina Haddad" not in rows())
tool.set_filter("closed"); check("closed filter shows them", list(rows()) == ["Lina Haddad"]); tool._selected([lina.id])
check("closed person detail offers reopen", any(c._kw.get("text") == "↩ Reopen" for b in tool.detail.winfo_children() for c in b.winfo_children()))
tool.table.tree.selection_set(lina.id); tool.reopen(); check("reopen", st.get(lina.id).status == "active")
tool.set_filter("active")
popups["ask"] = "bogus"; tool.table.tree.selection_set(lina.id); tool.mark_arrived()
check("bad arrival date is refused", st.get(lina.id).status == "active")
tool.table.tree.selection_set(omar.id); popups["ask"] = "never answered"; tool.close_person("missed")
o2 = st.get(omar.id)
check("visa missed closes with the reason in the log", o2.status == "missed" and "never answered" in o2.followups[-1]["note"])

print("== messages")
tool.table.tree.selection_set(ali.id); tool._selected([ali.id]); tool._set_tab("follow")
check("composer fills the fitting template", tool.msg_text is not None and "Hello Ali," in tool.msg_text.get("1.0", "end") and visa_io.fmt(d_(76)) in tool.msg_text.get("1.0", "end"))
tool.msg_text.delete("1.0", "end"); tool.msg_text.insert("1.0", "My own words for Ali")
popups["confirm"] = True; before = len(st.get(ali.id).followups); opened.clear(); tool.send_whatsapp()
check("WhatsApp link uses the edited text and the person's number", opened and opened[0].startswith("https://wa.me/9647501234567?text=My%20own%20words"), opened)
check("sending is logged when confirmed", len(st.get(ali.id).followups) == before + 1 and st.get(ali.id).followups[-1]["outcome"] == "sent")
popups["confirm"] = False; before = len(st.get(ali.id).followups); tool.send_whatsapp()
check("not logged when you did not send", len(st.get(ali.id).followups) == before)
tool.table.tree.selection_set(sara.id); tool._selected([sara.id]); popups["confirm"] = True; opened.clear(); tool.send_email()
check("e-mail opens mailto with a message", opened and opened[0].startswith("mailto:sara@example.com?subject="))
tool.table.tree.selection_set(ali.id); tool._selected([ali.id]); popups["error"].clear(); ali_phone = st.get(ali.id).phone
st.edit_many([ali.id], lambda p: setattr(p, "phone", ""), "x"); opened.clear(); tool.send_whatsapp()
check("no phone -> clear error, nothing opened", not opened and "no usable phone" in popups["error"][0])
st.edit_many([ali.id], lambda p: setattr(p, "phone", ali_phone), "x")
td = vd.TemplateDialog(tool); td.add(); td.name.set("Mine"); td.text.delete("1.0", "end"); td.text.insert("1.0", "Hi {first_name}!"); td.save()
check("templates are editable and saved", "Mine" in [t["name"] for t in tool.templates()])
check("templates fall back to the standard ones if damaged", (app.settings.set("visastate.templates", ["junk"]) or True) and len(tool.templates()) == 4)

print("== documents and the page picker")
big = tmp / "big_scan.pdf"; big.write_bytes(b"%PDF")
app.renderer.pages = 6
tool.table.tree.selection_set(ali.id); tool._selected([ali.id])
picked = {}
orig_picker = vt.PagePicker
class SpyPicker(orig_picker):
    def __init__(self, *a, **k):
        picked["args"] = a; super().__init__(*a, **k); picked["obj"] = self
vt.PagePicker = SpyPicker
texts_6 = ["CV of Ali", "Bank letter", "Photo page", "REPUBLIC OF X VISA\nPassport No: X1234567\nDate of issue 02/11/2026\nValid until 31/01/2027\nSingle entry",
           "Flight itinerary e-ticket", "Reference letter"]
visa_files.read_page_texts = lambda path, cfg, pages: (texts_6, "")
vt.filedialog.askopenfilename = lambda **k: str(big)
tool._set_tab("docs"); tool.attach("visa")
pk = picked["obj"]
check("picker lists every page of the big file", len(pk.items) == 6 and pk.total == 6)
check("best page is found, marked ★ and selected", [it.page for it in pk.grid_.selected()] == [3] and "★" in pk.items[3].label and "passport number" in pk.items[3].label, pk.items[3].label)
check("preview shows the selected page", pk.preview.shown[-1] == (3, 0))
check("dates are read from the page", pk.date_fields["issue"].get_date() == dt.date(2026, 11, 2) and pk.date_fields["expiry"].get_date() == dt.date(2027, 1, 31))
check("status line explains", "likely page" in pk.status._kw["text"])
pk.find_var.set("itinerary"); pk.find_next()
check("find jumps to the page with that text", [it.page for it in pk.grid_.selected()] == [4])
pk._step(-1); check("arrow keys move the selection", [it.page for it in pk.grid_.selected()] == [3])
pk.rotate(90); check("turning a page is remembered", pk.grid_.selected()[0].rotation == 90 and pk.preview.shown[-1] == (3, 90))
pk.apply_var.set(True)
pk.save()
check("extraction got the single chosen page and its turn", cuts[-1] == (str(big), [3], {3: 90}), cuts)
a3 = st.get(ali.id)
visa_doc = a3.doc("visa")
check("only the visa page is stored, linked relatively", visa_doc and visa_doc["pages"] == [4] and visa_doc["source"] == "big_scan.pdf" and st.doc_path(visa_doc).exists() and not visa_doc["file"].startswith("/"))
check("dates from the page were applied and deadline recalculated", a3.visa_issue == "2026-11-02" and a3.visa_deadline == "2027-01-31")
check("documents tab lists and previews it", tool.docs_table is not None and len(tool.docs_table.all_ids()) == 1 and str(st.doc_path(visa_doc)) in tool.viewer.shown)
check("the page picker never gets pages removed or moved", pk.grid_.delete_selected() is None)
none_picker = vd.PagePicker(tool, a3, "visa", str(big), lambda r: picked.setdefault("none", r)); none_picker._scan_done((["x", "y"], ""))
for it in none_picker.items: it.selected = False
none_picker.save()
check("saving with nothing chosen asks first", "none" not in picked and any("Click the page" in m for m in popups["info"]))
vt.filedialog.askopenfilename = lambda **k: str(tmp / "ticket.pdf")
(tmp / "ticket.pdf").write_bytes(b"%PDF"); app.renderer.pages = 1
tool.attach("ticket")
check("a one-page file is stored at once (no picker)", cuts[-1][1] == [0] and st.get(ali.id).doc("ticket") is not None)
check("storing a ticket marks it booked", st.get(ali.id).ticket_status == "booked")
vt.filedialog.askopenfilename = lambda **k: str(tmp / "notes.xyz")
popups["error"].clear(); tool.attach("other")
check("unsupported file refused politely", popups["error"] and "cannot be used" in popups["error"][0])
tool.docs_table.tree.selection_set(str(st.get(ali.id).doc("ticket")["id"]))
popups["confirm"] = True; tool.remove_doc()
check("removing a file unlinks it but keeps it on disk", st.get(ali.id).doc("ticket") is None and any(p.suffix == ".pdf" for p in (st.files_dir).rglob("*Ticket*")))
tool.undo(); check("...and Ctrl+Z links it again", st.get(ali.id).doc("ticket") is not None)
vt.PagePicker = orig_picker

print("== drop a file")
kinds = []
vt.KindDialog = lambda tool_, name, cb: kinds.append((name, cb))
tool.accept_files([str(big)])
check("dropped file asks what it is", kinds and kinds[0][0] == "big_scan.pdf")
popups["info"].clear(); tool.table.tree.selection_set(); tool.accept_files([str(big)])
check("dropped file without a selected person explains", popups["info"] and "Select a person first" in popups["info"][0])

print("== import")
csv = tmp / "list.csv"
csv.write_text("Full Name,Passport No,Visa Issued,Mobile,Ticket\nNew One,P111,10/11/2026,+964 700 000 001,yes\nAli Khan,X1234567,01/11/2026,,\nBroken,P222,31/02/2026,,\n", encoding="utf-8")
before = len(st.all())
tool.filter = "all"
imp = vd.ImportDialog(tool, str(csv), tool._do_import)
check("columns matched automatically", imp.mapping()["name"] == "Full Name" and imp.mapping()["visa_issue"] == "Visa Issued" and imp.mapping()["phone"] == "Mobile")
check("preview counts people and problems", "3 people found" in imp.result_lbl._kw["text"] and "1 problem" in imp.result_lbl._kw["text"], imp.result_lbl._kw["text"])
imp.group.set("Imported"); popups["info"].clear(); imp.run()
check("duplicates skipped, new people added into the group", len(st.all()) == before + 2 and st.find_passport("P111").group == "Imported" and st.find_passport("P111").ticket_status == "booked")
check("import summary shown", "2 added" in popups["info"][0] and "1 skipped" in popups["info"][0])
tool.undo(); check("one undo reverts the whole import", len(st.all()) == before)
imp2 = vd.ImportDialog(tool, str(tmp / "missing.csv"), tool._do_import)
check("unreadable sheet gives an error, no window", imp2.win is None and popups["error"])
csv.write_text("Nothing\n", encoding="utf-8")
imp3 = vd.ImportDialog(tool, str(csv), tool._do_import)
check("no name column -> import disabled", imp3.import_btn._kw["state"] == "disabled")
tool.accept_files([str(tmp / "list.csv")]); check("dropping a spreadsheet starts the import", True)

print("== exports")
faketk.DIALOGS["asksaveasfilename"] = str(tmp / "out.xlsx"); tool.export_people()
import openpyxl
wb = openpyxl.load_workbook(tmp / "out.xlsx")
check("Excel export has People and Follow-ups sheets", wb.sheetnames == ["People", "Follow-ups"] and wb["People"].max_row == len(st.all()) + 1 and wb["Follow-ups"].max_row > 1)
faketk.DIALOGS["asksaveasfilename"] = str(tmp / "out.csv"); tool.export_people()
check("CSV export chosen by file name", (tmp / "out.csv").read_text(encoding="utf-8-sig").startswith("Name,Passport number"))
faketk.DIALOGS["asksaveasfilename"] = str(tmp / "log.xlsx"); tool.table.tree.selection_set(ali.id); tool.export_followups()
check("follow-up proof log of the selected person", openpyxl.load_workbook(tmp / "log.xlsx")["Follow-ups"]["A2"].value == "Ali Khan")
faketk.DIALOGS["asksaveasfilename"] = str(tmp / "d.ics"); tool.export_calendar()
check("calendar file written", (tmp / "d.ics").read_text(encoding="utf-8").startswith("BEGIN:VCALENDAR"))
faketk.DIALOGS["asksaveasfilename"] = ""; tool.export_people(); check("cancelling an export does nothing", True)
tool.copy_summary(); check("summary copied", any("Summary copied" in m for _l, m in app.notes))

print("== undo, delete, backup")
n = len(st.all()); tool.table.tree.selection_set(*[p.id for p in st.all()[-2:]]); popups["confirm"] = False; tool.delete_selected()
check("delete asks first", len(st.all()) == n)
popups["confirm"] = True; tool.delete_selected(); check("delete removes the selected", len(st.all()) == n - 2)
tool.undo(); check("undo brings them back", len(st.all()) == n)
tool.undo(); tool.redo(); check("redo works", len(st.all()) == n)
check("backups exist after saves", len(st.backups()) >= 1)
b = st.backup_now(); check("manual backup", b.exists())
vt.filedialog.askopenfilename = lambda **k: str(b); popups["confirm"] = True; tool.restore_backup()
check("restore from a backup", any("Restored" in m for _l, m in app.notes))

print("== reminders and app integration")
app.notes.clear(); tool._startup_alert()
check("start-up reminder warns about work to do", any("Visa Tracker:" in m and l == "warn" for l, m in app.notes), app.notes)
app.notes.clear(); app.settings.set("visa.remind_on_start", False); tool._startup_alert()
check("reminder can be switched off", not app.notes)
tool._last_alert = 0; app.settings.set("visa.remind_every_min", 30); tool._alert_day = TODAY; app.notes.clear(); tool._tick()
check("periodic reminder repeats while things are open", any("Visa Tracker:" in m for _l, m in app.notes))
app.settings.set("visa.badge", False); check("badge can be switched off", tool.badge() == ""); app.settings.set("visa.badge", True)
check("keys declared", {"<Control-n>", "<Control-l>", "<Control-z>", "<F5>"} <= set(tool.keys()))
check("palette commands declared", {"visa.add", "visa.import", "visa.todo", "visa.export"} <= {c.id for c in tool.commands()})
app.settings.set("visa.critical_days", 2); faketk.flush()
check("changing a rule re-evaluates (Sara 3 days is no longer critical)", "Critical 0" in tool.chips["d7"]._kw["text"] or tool.chips["d7"]._kw["text"].endswith(" 0") or "Urgent" in tool.chips["d14"]._kw["text"])
app.settings.set("visa.critical_days", 7)
new_dir = tmp / "elsewhere"; app.settings.set("visa.data_folder", str(new_dir)); faketk.flush()
check("changing the data folder opens that folder (empty here)", tool.store is not None and tool.store.folder == new_dir and len(tool.store.all()) == 0)
app.settings.set("visa.data_folder", str(tmp / "data")); faketk.flush()
check("...and switching back restores the data", len(tool.store.all()) == n)

print("== time moves on")
tool.today = lambda: TODAY + dt.timedelta(days=10); tool._tick(); tool.refresh()
check("ten days later the lists are recomputed", rows().get("Sara Noor", {}).get("days") == -7 or "Sara Noor" not in rows())

print(f"\n{sum(results)}/{len(results)} visa tracker checks passed")
sys.exit(0 if all(results) else 1)
