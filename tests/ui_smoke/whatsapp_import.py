"""WhatsApp import into DocPacker under the fake tkinter (own process).  Run: python tests/ui_smoke/whatsapp_import.py"""
import sys
from unittest import mock
try:                                                  # PyMuPDF is only needed to import the tool, not by this test
    import pymupdf  # noqa: F401
except ImportError:
    sys.modules["pymupdf"] = mock.MagicMock()
    sys.modules["fitz"] = sys.modules["pymupdf"]

import logging
import os
import tempfile
import zipfile
from pathlib import Path as _P
_H = _P(__file__).resolve().parent
sys.path.insert(0, str(_H)); sys.path.insert(0, str(_H.parent)); sys.path.insert(0, str(_H.parents[1]))
logging.disable(logging.CRITICAL)
from pathlib import Path
import fakeapp, faketk
from fakeapp import FakeApp, FakeViewer
faketk.KNOWN_NOOP |= {"tag_ranges", "tag_names", "search", "selection_add", "tag_delete"}
import tkinter as tk
from utilshub.core import whatsapp as wa
from utilshub.core.readers import DocInfo
import utilshub.tools.docpacker as dpm
import utilshub.ui.whatsapp_import as wim
from utilshub.tools.docpacker import DocPacker

res = []
def check(name, cond, extra=""):
    res.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))

tmp = Path(tempfile.mkdtemp())
os.environ["UTILSHUB_HOME"] = str(tmp)
app = FakeApp(tmp)
app.tool_classes = app.tool_classes + [DocPacker]
app.settings.register(DocPacker.options())
dpm.PageViewer = FakeViewer
wim.PageViewer = FakeViewer
wa.work_root = lambda: (tmp / "wa_work")
(tmp / "wa_work").mkdir(exist_ok=True)

def fake_read(path, cfg, opts):                      # no PyMuPDF / OCR here: a tiny stand-in for the reader
    return DocInfo(path, "pdf" if path.lower().endswith(".pdf") else "image", text="", pages=3 if "bundle" in path else 1,
                   page_texts=["a", "b", "c"] if "bundle" in path else ["a"], hash=Path(path).name)
dpm.read_document = fake_read
dpm.supported_extensions = lambda: [".pdf", ".jpg", ".jpeg", ".png"]
wim_supported = [".pdf", ".jpg", ".jpeg", ".png"]

asked, errors, infos = [], [], []
answers = {}
def fake_ask(parent, title, prompt, initial="", multiline=False):
    asked.append((title, prompt, initial)); return answers.get(title, initial)
wim.ask_text = fake_ask
wim.error = lambda parent, msg, *a: errors.append(msg)
wim.info_box = lambda parent, msg, *a: infos.append(msg)
dpm.confirm = lambda parent, msg, *a: True

CHAT = """31/12/23, 9:45 PM - Messages and calls are end-to-end encrypted.
13/01/24, 9:47 PM - Ahmed Ali: IMG-20240113-WA0001.jpg (file attached)
my passport copy
13/01/24, 9:50 PM - Me Myself: ok
14/01/24, 10:02 AM - Ahmed Ali: bank letter.9s9.pdf (file attached)
14/01/24, 10:03 AM - Ahmed Ali: my number +971 50 123 4567 and I join on 1st March 2026
14/01/24, 10:04 AM - Ahmed Ali: bundle all docs.pdf (file attached)
14/01/24, 10:05 AM - Ahmed Ali: position is Site Engineer
"""
z = tmp / "WhatsApp Chat with Ahmed Ali.zip"
with zipfile.ZipFile(z, "w") as zf:
    zf.writestr("WhatsApp Chat with Ahmed Ali.txt", CHAT)
    zf.writestr("IMG-20240113-WA0001.jpg", b"x" * 30000)
    zf.writestr("bank letter.9s9.pdf", b"%PDF-1.4 bank")
    zf.writestr("bundle all docs.pdf", b"%PDF-1.4 bundle")
    zf.writestr("PTT-20240113-WA0002.opus", b"voice")
    zf.writestr("STK-1.webp", b"s" * 40000)

dp = DocPacker(app)
dp.frame = faketk.Widget(app.root)
dp.build(dp.frame)
split_calls = []
dp.split_dialog = lambda iid: split_calls.append(iid)          # the real one needs a real window
check("DocPacker builds with the Phone box and the WhatsApp button", hasattr(dp, "phone_var"))
check("phone is a placeholder value", dp.values()["phone"] == "")
dp.phone_var.set(" +1 555 ")
check("{phone} is available for cover and file names", dp.values()["phone"] == "+1 555")
dp.phone_var.set("")

# ------------------------------------------------------------------ open through a ZIP drop
dp.add_paths([str(z)])
dlg = dp._wa_dialog
check("a dropped ZIP opens the WhatsApp window, not the file list", dlg is not None and not dp.entries)
check("documents and photos are listed; voice notes and stickers hidden by default",
      sorted(i.f.name for i in dlg.items.values() if dlg._visible(i)) ==
      ["IMG-20240113-WA0001.jpg", "bank letter.9s9.pdf", "bundle all docs.pdf"])
check("noise is still known (shown with the checkbox)", len(dlg.items) == 5)
check("person guessed from the chat title", dlg.person[0] == "Ahmed Ali")
check("name filled automatically", dp.name_var.get() == "Ahmed Ali")
check("phone filled automatically (found in a message)", dp.phone_var.get() == "+971 50 123 4567")
check("WhatsApp-filled values are flagged for checking", set(dp.wa_filled) == {"name", "phone"} and "WhatsApp" in dp.wa_note_var.get())
dp.name_var.set("Ahmed A. Ali")
check("editing a filled value removes its flag", "name" not in dp.wa_filled and "Full name" not in dp.wa_note_var.get())

# typed values are never overwritten
dp.phone_var.set("999")
check("typed phone kept by the quiet fill", dp.wa_fill_person(wa.PersonInfo("Ahmed Ali", "Ahmed Ali", "+971 50 123 4567"), replace=False) == [])
check("typed values replaced only when asked", dp.wa_fill_person(wa.PersonInfo("Ahmed Ali", "Ahmed Ali", "+971 50 123 4567"), replace=True) == ["name", "phone"]
      and dp.phone_var.get() == "+971 50 123 4567" and dp.name_var.get() == "Ahmed Ali")

# ------------------------------------------------------------------ preview, choose, several documents
bank = dlg.items["0:bank letter.9s9.pdf"]
dlg._selected([bank.iid])
check("selecting a file extracts it and shows it in the viewer", dlg.viewer.shown and dlg.viewer.shown[-1].endswith("bank letter.9s9.pdf")
      and Path(dlg.viewer.shown[-1]).read_bytes() == b"%PDF-1.4 bank")
check("only the opened file was unzipped", sorted(p.name for p in Path(dlg.viewer.shown[-1]).parent.iterdir()) == ["bank letter.9s9.pdf"])
types = {t.name: t.id for t in dlg.types}
bank_name = next(n for n in types if "bank" in n.lower())
dlg.type_var.set(bank_name); dlg._type_picked()
check("choosing a type marks the file and sets it", bank.use and bank.type_id == types[bank_name])
bundle = dlg.items["0:bundle all docs.pdf"]
dlg._selected([bundle.iid]); dlg.several_var.set(True); dlg._several_toggled()
check("'several documents' marks the file and remembers it", bundle.use and bundle.several)
photo = dlg.items["0:IMG-20240113-WA0001.jpg"]
dlg._toggle(photo)
dlg._toggle(photo)
check("clicking the box toggles the mark", not photo.use)

dlg.add_to_docpacker(False)
paths = {Path(p).name: e for p, e in dp.entries.items()}
check("marked files reached DocPacker", set(paths) == {"bank letter.9s9.pdf", "bundle all docs.pdf"})
check("the type chosen by hand is kept (not re-guessed)", not paths["bank letter.9s9.pdf"].auto and paths["bank letter.9s9.pdf"].dtype == types[bank_name])
check("a file without a chosen type is left to recognition", paths["bundle all docs.pdf"].auto)
check("added files are flagged in the list", bank.added and not bank.use and "added" in dlg._row(bank)["note"])
check("the name is not taken from the private folder name", dp.name_var.get() == "Ahmed Ali")
check("the split view opened for the combined file only", split_calls == ["%s" % next(p for p in dp.entries if p.endswith("bundle all docs.pdf"))] and dp._wa_split == [])

# only the selected file
dlg._selected([photo.iid]); dlg.add_to_docpacker(True)
check("'add only the selected file' adds just that one", any(Path(p).name == "IMG-20240113-WA0001.jpg" for p in dp.entries) and len(dp.entries) == 3)

# ------------------------------------------------------------------ message -> field
chat = dlg.cur.chat
num_msg = next(m for m in chat.messages if "my number" in m.text)
dlg.use_for("joining", "Joining date", "1st March 2026")
check("a date is parsed and shown before it is applied", "2026-03-01" in asked[-1][1] + asked[-1][2] and dp.join_var.get() == "2026-03-01")
errors.clear(); dlg.use_for("joining", "Joining date", "no date here")
check("an unreadable date is refused with a message", errors and dp.join_var.get() == "2026-03-01")
dlg.use_for("surname", "Surname", "  Ali \n")
check("text is cleaned and applied", dp.surname_var.get() == "Ali")
dlg.use_for("phone", "Phone", "call me on +971 50 123 4567 please")
check("a phone is picked out of a sentence", asked[-1][2] == "+971 50 123 4567")
made = dp.wa_add_field("Position")
check("a new field can be created from the window", made == ("position", "Position") and "Position" in dp.custom_vars)
dlg.use_for("position", "Position", "position is Site Engineer")
check("a custom field is filled from a message", dp.custom_vars["Position"].get() == "position is Site Engineer")
check("every WhatsApp-filled value is listed", {"joining", "surname", "phone", "position"} <= set(dp.wa_filled))
choices = [k for k, _l in dp.wa_field_choices()]
check("name, surname, joining, phone and custom fields can be filled", choices[:4] == ["name", "surname", "joining", "phone"] and "position" in choices)

# ------------------------------------------------------------------ chat rendering, search, person choice
dlg.render_chat()
check("the chat renders in the Text widget with the message text", "my passport copy" in dlg.chat.get("1.0", "end") and "Site Engineer" in dlg.chat.get("1.0", "end"))
check("attachments appear as clickable chips", "📎 bank letter.9s9.pdf" in dlg.chat.get("1.0", "end") and len(dlg._chip_files) == 3)
dlg.from_var.set("Me Myself"); dlg.render_chat()
check("sender filter limits the chat", "Site Engineer" not in dlg.chat.get("1.0", "end") and "ok" in dlg.chat.get("1.0", "end"))
dlg.from_var.set("Everyone")
dlg._set_person("Me Myself")
check("choosing another person is remembered for this chat", dp.wa_people_get(wa.chat_key("Ahmed Ali")) == "Me Myself")
check("person summary follows the choice", "Me Myself" in dlg.person_info_var.get())

# ------------------------------------------------------------------ close, clear, cleanup
workdir = Path(next(iter(dp._wa_dirs)))
dlg.close()
check("closing keeps the files DocPacker still uses", workdir.exists() and dp._wa_dialog is None)
dp.clear()
check("Clear removes the unzipped copies and the WhatsApp values", not workdir.exists() and not dp.wa_filled and dp.phone_var.get() == "")

# a ZIP nobody can read does not break the window
bad = tmp / "bad.zip"; bad.write_bytes(b"nope")
dp.open_whatsapp([str(bad)])
check("an unreadable ZIP is reported, no crash", errors and "not a readable ZIP" in errors[-1])

print(f"{sum(res)}/{len(res)} WhatsApp import checks passed")
sys.exit(0 if all(res) else 1)
