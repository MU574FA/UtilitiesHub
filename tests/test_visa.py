"""Visa Tracker core: deadlines, follow-up rhythm, storage, import / export, messages, page finder (no display needed)."""
import datetime as dt
import json

import pytest

from utilshub.core import visa as V
from utilshub.core import visa_files as VF
from utilshub.core import visa_io as IO

D = dt.date
TODAY = D(2026, 11, 15)
POL = V.Policy()


def person(**kw) -> V.Person:
    kw.setdefault("name", "Ali Khan")
    return V.Person(**kw)


def contact(p, days_ago, outcome="reached", channel="call"):
    when = dt.datetime.combine(TODAY - dt.timedelta(days=days_ago), dt.time(10, 0))
    p.followups.append(V.make_followup(channel, outcome, "", "me", when))


# --------------------------------------------------------------------------- dates
def test_parse_date_input_formats():
    p = lambda s, **k: V.parse_date_input(s, TODAY, **k)
    assert p("2026-12-01") == D(2026, 12, 1)
    assert p("01/12/2026") == D(2026, 12, 1)
    assert p("01/12/2026", order="mdy") == D(2026, 1, 12)
    assert p("25/12/2026", order="mdy") == D(2026, 12, 25)          # impossible month 25: swapped
    assert p("1 Dec 2026") == D(2026, 12, 1)
    assert p("Dec 1, 2026") == D(2026, 12, 1)
    assert p("today") == TODAY and p("tomorrow") == D(2026, 11, 16) and p("yesterday") == D(2026, 11, 14)
    assert p("+30") == D(2026, 12, 15) and p("-5") == D(2026, 11, 10)
    assert p("+2w") == D(2026, 11, 29) and p("+1m") == D(2026, 12, 15)
    assert p("1/2/26") == D(2026, 2, 1)
    assert p("") is None and p("hello") is None and p("31/02/2026") is None


def test_human_days():
    assert V.human_days(12) == "12 days" and V.human_days(1) == "1 day" and V.human_days(0) == "today"
    assert V.human_days(-3) == "3 days ago" and V.human_days(None) == "-"


# --------------------------------------------------------------------------- deadline and levels
def test_deadline_default_inclusive_override_and_fixed():
    p = person(visa_issue="2026-11-01")
    assert V.deadline_of(p, POL) == D(2026, 1 + 10, 1) + dt.timedelta(days=90) - dt.timedelta(days=0)   # 1 Nov + 90
    assert V.deadline_of(p, POL) == D(2027, 1, 30)
    assert V.deadline_of(p, V.Policy(inclusive=True)) == D(2027, 1, 29)
    assert V.deadline_of(person(visa_issue="2026-11-01", visa_days=60), POL) == D(2026, 12, 31)
    assert V.deadline_of(person(visa_issue="2026-11-01", visa_deadline="2027-02-01"), POL) == D(2027, 2, 1)
    assert V.deadline_of(person(), POL) is None


def test_levels():
    def lvl(days):
        return V.level_of(person(visa_deadline=(TODAY + dt.timedelta(days=days)).isoformat()), TODAY, POL)
    assert lvl(-1) == "overdue" and lvl(0) == "critical" and lvl(7) == "critical"
    assert lvl(8) == "urgent" and lvl(14) == "urgent" and lvl(15) == "soon" and lvl(30) == "soon" and lvl(31) == "ok"
    assert V.level_of(person(), TODAY, POL) == "nodate"
    assert V.level_of(person(status="arrived"), TODAY, POL) == "arrived"
    assert V.days_left(person(visa_deadline="2026-11-20"), TODAY, POL) == 5


# --------------------------------------------------------------------------- rhythm
def test_cadence_parse_and_interval():
    assert V.parse_cadence("60:14, 30:7, 14:3, 0:1") == [(60, 14), (30, 7), (14, 3), (0, 1)]
    assert V.parse_cadence("nonsense") == V.DEFAULT_CADENCE
    assert V.parse_cadence("10=2;30=5") == [(30, 5), (10, 2)]
    assert [POL.interval_for(n) for n in (90, 60, 59, 30, 29, 14, 13, 0, -5)] == [14, 14, 7, 7, 3, 3, 1, 1, 1]
    assert POL.interval_for(None) == 14


def test_next_due_and_todo():
    p = person(visa_deadline="2027-03-01", added_on="2026-11-10")
    assert V.next_due(p, TODAY, POL) == D(2026, 11, 10)                      # never contacted: due since added
    assert V.todo(p, TODAY, POL) == ["No contact logged yet"]
    contact(p, 3)                                                            # 106 days left -> every 14 days
    assert V.next_due(p, TODAY, POL) == D(2026, 11, 26) and V.todo(p, TODAY, POL) == []
    q = person(visa_deadline="2026-11-20")                                   # 5 days left -> every day
    contact(q, 2)
    assert V.next_due(q, TODAY, POL) == D(2026, 11, 14)
    assert V.todo(q, TODAY, POL) == ["Follow-up overdue by 1 day"]
    contact(q, 0)
    assert V.todo(q, TODAY, POL) == []
    assert V.next_due(q, D(2026, 11, 16), POL) == D(2026, 11, 16) and V.todo(q, D(2026, 11, 16), POL) == ["Follow-up due today"]


def test_no_answer_retries_sooner_and_notes_do_not_count():
    p = person(visa_deadline="2027-03-01")
    contact(p, 1, "no_answer")
    assert V.next_due(p, TODAY, POL) == TODAY                                # retry after 1 day, not after 14
    p2 = person(visa_deadline="2027-03-01", added_on="2026-11-01")
    p2.followups.append(V.make_followup("note", "", "just a note", "me", dt.datetime(2026, 11, 14, 9)))
    p2.followups.append(V.make_followup("system", "", "status", "me", dt.datetime(2026, 11, 14, 9)))
    assert V.last_contact(p2) is None and V.todo(p2, TODAY, POL) == ["No contact logged yet"]


def test_snooze_and_closed_people():
    p = person(visa_deadline="2026-11-20", next_followup="2026-11-18")
    contact(p, 5)
    assert V.next_due(p, TODAY, POL) == D(2026, 11, 18) and V.todo(p, TODAY, POL) == []
    assert V.next_due(person(status="arrived"), TODAY, POL) is None and V.todo(person(status="cancelled"), TODAY, POL) == []


def test_todo_deadline_passed_and_flight_passed():
    p = person(visa_deadline="2026-11-10", flight_date="2026-11-05", next_followup="2026-12-01")
    t = V.todo(p, TODAY, POL)
    assert any("deadline has passed" in x for x in t) and any("Flight date has passed" in x for x in t)


# --------------------------------------------------------------------------- warnings
def test_issues():
    texts = lambda p: [i.text for i in V.issues(p, TODAY, POL)]
    p = person(visa_deadline="2026-12-01", flight_date="2026-12-10", phone="1")
    assert any("AFTER the visa deadline" in t for t in texts(p))
    p = person(visa_deadline="2026-12-01", phone="1")                        # 16 days left, no ticket
    assert any("No ticket booked" in t for t in texts(p))
    assert any("visa file is not stored" in t for t in texts(p))
    assert any("cannot be reached" in t for t in texts(person(visa_deadline="2027-03-01")))
    assert any("No visa date" in t for t in texts(person(phone="1")))
    p = person(visa_deadline="2027-01-01", passport_expiry="2027-02-01", phone="1")
    assert any("passport is valid for less than 6 months" in t for t in texts(p))
    assert V.issues(person(status="arrived"), TODAY, POL) == []


def test_priority_and_filters_and_counts():
    people = [person(name="B", visa_deadline="2026-12-10", phone="1"), person(name="A", visa_deadline="2026-11-10"),
              person(name="C", visa_deadline="2026-11-18"), person(name="D", status="arrived", visa_deadline="2026-11-01"),
              person(name="E", visa_deadline="2027-06-01", ticket_status="sent")]
    order = [p.name for p in sorted(people, key=lambda q: V.priority(q, TODAY, POL))]
    assert order == ["A", "C", "B", "E", "D"]
    c = V.counts(people, TODAY, POL)
    assert (c["active"], c["overdue"], c["d7"], c["d30"], c["closed"], c["all"]) == (4, 1, 1, 1, 1, 5)
    assert c["noticket"] == 3 and c["todo"] == 4                              # nobody was ever contacted


# --------------------------------------------------------------------------- store
def test_store_roundtrip_unknown_keys_and_undo(tmp_path):
    st = V.Store(tmp_path)
    a = st.add(person(name="Ali", passport_no="X123", visa_issue="2026-11-01"))
    st.add(person(name="Sara"))
    raw = json.loads(st.path.read_text(encoding="utf-8"))
    raw["people"][0]["future_field"] = {"x": 1}
    st.path.write_text(json.dumps(raw), encoding="utf-8")
    st2 = V.Store(tmp_path)
    assert [p.name for p in st2.all()] == ["Ali", "Sara"] and st2.get(a.id).unknown == {"future_field": {"x": 1}}
    st2.log_followup(a.id, "call", "reached", "spoke", "me")
    assert "future_field" in json.loads(st2.path.read_text(encoding="utf-8"))["people"][0]       # written back
    assert len(st2.get(a.id).followups) == 1
    assert st2.undo() == "Log contact" and st2.get(a.id).followups == []
    assert st2.redo() == "Log contact" and len(st2.get(a.id).followups) == 1
    st2.delete([a.id])
    assert st2.get(a.id) is None and st2.undo().startswith("Delete") and st2.get(a.id) is not None
    assert st2.find_passport("x-123").name == "Ali"


def test_status_snooze_and_log_clears_snooze(tmp_path):
    st = V.Store(tmp_path)
    p = st.add(person(name="Ali"))
    st.snooze([p.id], D(2026, 12, 1))
    assert st.get(p.id).next_followup == "2026-12-01"
    st.log_followup(p.id, "whatsapp", "sent", "", "me")
    assert st.get(p.id).next_followup == ""                                   # a real contact ends the snooze
    st.log_followup(p.id, "note", "", "only a note", "me", next_followup=D(2026, 12, 5))
    assert st.get(p.id).next_followup == "2026-12-05"
    st.set_status([p.id], "arrived", D(2026, 11, 20), "landed", "me")
    q = st.get(p.id)
    assert q.status == "arrived" and q.arrived_on == "2026-11-20" and q.next_followup == "" and q.followups[-1]["channel"] == "system"


def test_daily_backup_corrupt_fallback_and_restore(tmp_path):
    st = V.Store(tmp_path, keep_backups=3)
    st.add(person(name="Ali"))
    st.add(person(name="Sara"))                                               # second save: today's backup exists
    assert len(st.backups()) == 1
    (st.backup_dir / "people-20250101.json").write_text(st.path.read_text(encoding="utf-8"), encoding="utf-8")
    st.path.write_text("{ broken", encoding="utf-8")
    again = V.Store(tmp_path)
    assert "backup" in again.warning and len(again.all()) >= 1 and (tmp_path / "people.bad").exists()
    b = again.backup_now()
    again.add(person(name="Third"))
    n = again.restore(b)
    assert n == len(again.all()) and "Third" not in [p.name for p in again.all()]
    assert again.undo().startswith("Restore") and "Third" in [p.name for p in again.all()]
    with pytest.raises(V.VisaError):
        again.restore(tmp_path / "nope.json")


def test_backups_are_pruned(tmp_path):
    st = V.Store(tmp_path, keep_backups=2)
    st.add(person(name="A"))
    st.backup_dir.mkdir(parents=True, exist_ok=True)
    for day in ("20240101", "20240102", "20240103"):
        (st.backup_dir / f"people-{day}.json").write_text("{}", encoding="utf-8")
    (st.backup_dir / f"people-{dt.date.today():%Y%m%d}.json").unlink(missing_ok=True)
    st.add(person(name="B"))
    assert len(st.backups()) == 2


def test_refresh_sees_changes_made_elsewhere(tmp_path):
    a, b = V.Store(tmp_path), V.Store(tmp_path)
    a.add(person(name="Ali"))
    assert b.refresh() is True and [p.name for p in b.all()] == ["Ali"]
    b.add(person(name="Sara"))
    a.update(V.Person.from_dict({**a.all()[0].to_dict(), "phone": "123"}))      # a reloads Sara before it writes
    assert sorted(p.name for p in a.all()) == ["Ali", "Sara"]
    assert sorted(p.name for p in V.Store(tmp_path).all()) == ["Ali", "Sara"]
    assert b.refresh() is True and b.refresh() is False


def test_documents_are_linked_relatively_and_never_deleted(tmp_path):
    st = V.Store(tmp_path)
    p = st.add(person(name="Ali Khan"))
    f1 = st.new_doc_path(p, "visa")
    f1.write_bytes(b"%PDF-1")
    st.add_doc(p.id, "visa", f1, source="big.pdf", pages=[16])
    f2 = st.new_doc_path(st.get(p.id), "visa")
    assert f2 != f1                                                           # a new name, the first file is untouched
    f2.write_bytes(b"%PDF-2")
    st.add_doc(p.id, "visa", f2, source="big2.pdf", pages=[3])
    q = st.get(p.id)
    assert [d["name"] for d in q.docs] == [f2.name] and q.doc("visa")["pages"] == [4] and f1.exists()
    assert not q.docs[0]["file"].startswith("/") and st.doc_path(q.docs[0]) == f2
    with pytest.raises(V.VisaError):
        st.add_doc(p.id, "visa", tmp_path.parent / "elsewhere.pdf")
    assert st.remove_doc(p.id, q.docs[0]["id"]) and f2.exists() and st.get(p.id).docs == []


def test_merge_people_modes(tmp_path):
    st = V.Store(tmp_path)
    st.add(person(name="Ali", passport_no="X1", phone="111", status="active"))
    new = lambda: [person(name="Ali K", passport_no="x 1", phone="222"), person(name="Sara", passport_no="Y2"),
                   person(name="Nopass")]
    assert st.merge_people(new(), "skip") == (2, 0, 1) and len(st.all()) == 3
    st2 = V.Store(tmp_path / "b")
    st2.add(person(name="Ali", passport_no="X1", phone="111"))
    assert st2.merge_people(new(), "update") == (2, 1, 0)
    assert st2.find_passport("X1").phone == "222" and st2.find_passport("X1").name == "Ali K"
    assert st2.merge_people(new(), "add")[0] == 3
    before = len(st2.all())
    assert st2.merge_people([], "skip") == (0, 0, 0)
    assert len(st2.all()) == before and st2.undo() == "Import" and len(st2.all()) == 3   # the empty import left no undo step


# --------------------------------------------------------------------------- import / export
def test_guess_mapping():
    m = IO.guess_mapping(["Full Name", "Passport No", "Passport Issue Date", "Visa Issued", "Visa Expiry", "Mobile",
                          "Position", "Flight Date", "Ticket", "Nationality", "E-mail", "Remarks"])
    assert m["name"] == "Full Name" and m["passport_no"] == "Passport No" and m["visa_issue"] == "Visa Issued"
    assert m["visa_deadline"] == "Visa Expiry" and m["phone"] == "Mobile" and m["flight_date"] == "Flight Date"
    assert m["ticket_status"] == "Ticket" and m["email"] == "E-mail" and m["notes"] == "Remarks"
    assert IO.guess_mapping(["الاسم", "Phone"])["name"] == "الاسم"


def test_table_to_people_reads_excel_values_and_reports_problems():
    recs = [{"Name": "Ali", "Pass": "X1", "Issued": dt.datetime(2026, 11, 1), "Days": "60 days", "Tick": "sent", "St": ""},
            {"Name": "Sara", "Pass": 55.0, "Issued": 46000, "Days": "", "Tick": "", "St": "Arrived"},
            {"Name": "Bad", "Pass": "", "Issued": "31/02/2026", "Days": "", "Tick": "", "St": ""},
            {"Name": "", "Pass": "Z", "Issued": "", "Days": "", "Tick": "", "St": ""},
            {"Name": "", "Pass": "", "Issued": "", "Days": "", "Tick": "", "St": ""}]
    mp = {"name": "Name", "passport_no": "Pass", "visa_issue": "Issued", "visa_days": "Days", "ticket_status": "Tick", "status": "St"}
    people, problems = IO.table_to_people(recs, mp, TODAY)
    assert [p.name for p in people] == ["Ali", "Sara", "Bad"]
    a, s, b = people
    assert a.visa_issue == "2026-11-01" and a.visa_days == 60 and a.ticket_status == "sent"
    assert s.passport_no == "55" and s.visa_issue == (D(1899, 12, 30) + dt.timedelta(days=46000)).isoformat() and s.status == "arrived"
    assert b.visa_issue == "" and len(problems) == 2 and any("31/02/2026" in x for x in problems) and any("no name" in x for x in problems)


def test_exports(tmp_path):
    st = V.Store(tmp_path)
    p = st.add(person(name="Ali", passport_no="X1", visa_issue="2026-11-01", flight_date="2026-12-01", ticket_status="booked"))
    st.log_followup(p.id, "whatsapp", "sent", "Sent the reminder", "Sara", dt.datetime(2026, 11, 14, 9, 30))
    rows = IO.people_table(st.all(), TODAY, POL, custom_fields=["Agency"])
    assert rows[0][0] == "Name" and rows[0][-1] == "Agency" and len(rows) == 2
    r = dict(zip(rows[0], rows[1]))
    assert r["Visa deadline"] == "30-Jan-2027" and r["Days left"] == 76 and r["Ticket"] == "Ticket booked" and r["Last contact by"] == "Sara"
    back = IO.table_to_people([dict(zip(rows[0], rows[1]))], IO.guess_mapping(rows[0]), TODAY)[0][0]      # round trip
    assert back.visa_deadline == "2027-01-30" and back.passport_no == "X1" and back.ticket_status == "booked"
    log = IO.followup_table(st.all())
    assert log[1] == ["Ali", "X1", "14-Nov-2026", "09:30", "WhatsApp", "Message sent", "Sara", "Sent the reminder"]
    text = IO.summary_text(st.all(), TODAY, POL)
    assert "Visa status - 15-Nov-2026" in text and "Nothing urgent today." in text and IO.fmt(D(2026, 11, 5), "%d/%m/%Y") == "05/11/2026"


def test_ics():
    p = person(id="abc", name="Ali, Khan", visa_issue="2026-11-01", next_followup="2026-11-20", flight_date="2026-12-01",
               passport_no="X1", flight_no="QR 1")
    gone = person(id="zzz", name="Arrived", visa_issue="2026-11-01", status="arrived")
    ics = IO.make_ics([p, gone], TODAY, POL)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert ics.count("BEGIN:VEVENT") == 3 and "zzz" not in ics
    assert "DTSTART;VALUE=DATE:20270130" in ics and "UID:abc-deadline@utilshub" in ics
    assert ics.count("BEGIN:VALARM") == 4 and "TRIGGER:-P14D" in ics and "Ali\\, Khan" in ics
    assert all(len(line.encode("utf-8")) <= 75 for line in ics.split("\r\n"))
    assert "\n" not in ics.replace("\r\n", "")


def test_templates_and_links():
    p = person(name="Ali Khan", visa_deadline="2026-12-01", flight_date="2026-11-28", flight_no="QR 1", ticket_status="booked",
               phone="+964 750 123 4567", extra={"Agency": "Blue Sky"})
    out = IO.render_template("Hi {first_name}, {days_text} left until {deadline}. {agency} {unknown} {my_name}", p, TODAY, POL,
                             my_name="Sara")
    assert out == "Hi Ali, 16 days left until 01-Dec-2026. Blue Sky {unknown} Sara"
    tpls = IO.DEFAULT_TEMPLATES
    assert IO.pick_template(tpls, p, TODAY, POL)["auto"] == "ticket"
    assert IO.pick_template(tpls, person(visa_deadline="2026-11-18"), TODAY, POL)["auto"] == "urgent"
    assert IO.pick_template(tpls, person(visa_deadline="2027-05-01"), TODAY, POL)["auto"] == "check"
    assert IO.clean_phone("+964 (750) 123-4567") == "9647501234567" and IO.clean_phone("00964 750 123 4567") == "9647501234567"
    assert IO.clean_phone("0750 123 4567", "964") == "9647501234567" and IO.clean_phone("0750 123 4567") == "07501234567"
    assert IO.clean_phone("12") == "" and IO.clean_phone("") == ""
    url = IO.whatsapp_url("+964 750 123 4567", "Hello there & more")
    assert url == "https://wa.me/9647501234567?text=Hello%20there%20%26%20more" and IO.whatsapp_url("x", "hi") == ""
    assert IO.mailto_url("a@b.c", "Visa", "Line1\nLine2") == "mailto:a@b.c?subject=Visa&body=Line1%0ALine2" and IO.mailto_url("", "s", "b") == ""


# --------------------------------------------------------------------------- page finder
VISA_PAGE = """REPUBLIC OF EXAMPLE  VISA
Name: ALI KHAN   Passport No: X1234567
Date of issue: 01/11/2026
Valid until: 30/01/2027
Number of entries: Single entry"""
TICKET_PAGE = """E-TICKET ITINERARY  Booking reference ABC123
Passenger: ALI KHAN
Issued 01 Nov 2026
Departure: 28 Nov 2026  Flight QR 1"""


def test_page_scoring_finds_the_visa_page():
    texts = ["Curriculum vitae of Ali Khan  Education skills", TICKET_PAGE, VISA_PAGE, "", "Bank statement for Sara"]
    scores = VF.rank_pages(texts, "visa", "X1234567", "Ali Khan")
    best = VF.likely_pages(scores)
    assert best[0].page == 2 and "passport number" in best[0].reasons and "visa" in best[0].reasons
    assert 1 not in [s.page for s in best] and scores[3].score == 0
    assert VF.likely_pages(VF.rank_pages(texts, "ticket", "", "Ali Khan"))[0].page == 1
    assert VF.likely_pages(VF.rank_pages(["nothing here at all", "random text of a letter"], "visa")) == []
    assert VF.score_page("تأشيرة دخول  رقم الجواز", "visa").score >= 5


def test_suggest_dates():
    d = VF.suggest_dates(VISA_PAGE, "visa", TODAY)
    assert d["issue"] == D(2026, 11, 1) and d["expiry"] == D(2027, 1, 30) and D(2026, 11, 1) in d["all"]
    assert VF.suggest_dates(TICKET_PAGE, "ticket", TODAY)["flight"] == D(2026, 11, 28)
    swapped = VF.suggest_dates("Valid until 01/11/2026\nDate of issue 30/01/2027", "visa", TODAY)
    assert swapped["issue"] < swapped["expiry"]
    assert VF.suggest_dates("no dates", "visa")["all"] == []


def test_read_page_texts_of_a_text_file(tmp_path):
    from utilshub.core.ocr import OcrConfig
    f = tmp_path / "x.txt"
    f.write_text(VISA_PAGE, encoding="utf-8")
    texts, err = VF.read_page_texts(f, OcrConfig(enabled=False), 5)
    assert err == "" and "VISA" in texts[0]


def test_extract_pages_cuts_one_page(tmp_path):
    fitz = pytest.importorskip("pymupdf")
    big = fitz.open()
    for i in range(5):
        big.new_page().insert_text((72, 72), f"page {i}")
    src = tmp_path / "big.pdf"
    big.save(str(src))
    out = VF.extract_pages(src, [3], tmp_path / "visa.pdf")
    with fitz.open(out) as doc:
        assert len(doc) == 1 and "page 3" in doc[0].get_text()
    with pytest.raises(VF.VisaFileError):
        VF.extract_pages(src, [], tmp_path / "none.pdf")
    with pytest.raises(VF.VisaFileError):
        VF.extract_pages(tmp_path / "missing.pdf", [0], tmp_path / "x.pdf")


# --------------------------------------------------------------------------- Iraq e-Visa extractor & candidate matching
IRAQ_EVISA_SAME_LINE = """REPUBLIC OF IRAQ - MINISTRY OF INTERIOR - DIRECTORATE OF NATIONALITY AND PASSPORTS AND RESIDENCE
معلومات الزائر / زانیاری سەردانکەران / Visa Information
RAJESH KUMAR SHARMA  الاســـــم الكامل / ناوى ته‌واو / Full Name:
Indian  هندي  الجنسيـــــــــة / ڕه‌گه‌زنامه‌ / Nationality:
1988-05-28  تاريخ الميلاد / ڕۆژی له‌ دایک بوون / Date of Birth:
V7790175  رقم الجواز / ژماره‌ی پاسپۆرت / Passport No.:
عادي  نوع الجواز / جۆری پاسپۆرت / Passport Type:
معلومات سمة الدخول الالكترونية / Information of Electronic Visa
1938660  رقم سمة الدخول / ژماره‌ی ڤیزه‌ی چوونه‌ ژووره‌وه‌ / Entry Visa No.:
202601-0001265830  رقـــــم الطلــــب / ژماره‌ی داواكردن / Application No.:
Multiple One Year  متعددة سنة واحدة  نـــــــــــوع الســــــمة / جۆری ڤیزه‌ / Visa Type:
Directorate of Residence Affairs  مديرية شؤون الإقامة  جـــــهة منــــح السمـــــة / شوێنی ده‌رچوون / Issuing Authority:
2026-11-18  تاريخ إصدار سمة الدخول / به‌رواری ده‌رچوونی ڤیزه‌ / Date of Issue:
90  صــــــلاحيــــــة الدخـــــول / مۆڵه‌ت بۆ ده‌ستگه‌یشتن / Entry Validity:
365  مـــــــــــدة الإقـــــــامـــــــة / ماوه‌ی مانه‌وه‌ / Duration of stay:
معلومات جهة الطلب / Requester Information
العتبة العباسية المقدسة/العتبة العباسية المقدسة  جهة الطلب / لایه‌نی داواکار / Requester:
كربلاء  العنـــــــوان / نـــــــاونیشان / Address:
البريد الإلكتروني / ئیمه‌یل / Email:
رقم الهاتف / ژماره‌ی مۆبایل / Mobile:"""

IRAQ_EVISA_SPLIT_LINES = """REPUBLIC OF IRAQ - MINISTRY OF INTERIOR
Full Name:
AHMED HASSAN AL-TAEE
Nationality:
Iraqi  عراقي
Date of Birth:
1992-03-14
Passport No.:
A9988776
Entry Visa No.:
2044551
Application No.:
202602-0009988776
Visa Type:
Single Entry
Date of Issue:
2026-11-20
Entry Validity:
60
Duration of stay:
30"""


def test_extract_iraq_evisa_same_line_and_split_lines():
    v = VF.extract_visa_page_info(IRAQ_EVISA_SAME_LINE, page=0)
    assert v.is_visa is True
    assert v.full_name == "RAJESH KUMAR SHARMA"
    assert v.nationality == "Indian"
    assert v.date_of_birth == "1988-05-28"
    assert v.passport_no == "V7790175"
    assert v.visa_no == "1938660"
    assert v.application_no == "202601-0001265830"
    assert v.visa_type == "Multiple One Year"
    assert v.issuing_authority == "Directorate of Residence Affairs"
    assert v.visa_issue == "2026-11-18"
    assert v.entry_validity == 90
    assert v.duration_of_stay == 365
    assert "العتبة العباسية" in v.requester
    assert v.address == "كربلاء"
    # suggest_dates ignores Date of Birth and reads issue + entry_validity
    sd = VF.suggest_dates(IRAQ_EVISA_SAME_LINE, "visa", TODAY)
    assert sd["issue"] == D(2026, 11, 18) and sd["entry_validity"] == 90 and D(1988, 5, 28) not in sd["all"]

    v2 = VF.extract_visa_page_info(IRAQ_EVISA_SPLIT_LINES, page=1)
    assert v2.is_visa is True and v2.full_name == "AHMED HASSAN AL-TAEE" and v2.passport_no == "A9988776"
    assert v2.visa_issue == "2026-11-20" and v2.entry_validity == 60 and v2.duration_of_stay == 30


def test_candidate_matching_for_passport_and_different_names(tmp_path):
    from utilshub.core.candidate_lookup import find_candidate, match_candidate, match_status
    sheet = tmp_path / "candidates.csv"
    sheet.write_text(
        "Candidate Name,Passport No,Date of Birth,Phone,Email,Position\n"
        "Rajesh Sharma,V7790175,1988-05-28,+91 98765 43210,rajesh@example.com,Technician\n"
        "Ali Al-Taee,,1992-03-14,+964 770 111 2222,ali@example.com,Supervisor\n"
        "Completely Different Name,P5554443,1990-01-01,+964 780 999 8888,diff@example.com,Operator\n",
        encoding="utf-8",
    )
    # 1. Exact passport match even when visa name differs completely from candidate list name
    h1 = match_candidate(str(sheet), name="MOHAMMED KAREEM JASIM", passport_no="P5554443")
    assert h1 and h1[0].matched_by == "passport" and h1[0].score == 1.0 and match_status(h1) == "match"
    assert h1[0].field_map()["phone"] == "+964 780 999 8888"

    # 2. Passport full name with middle name matches shorter candidate list name
    h2 = match_candidate(str(sheet), name="RAJESH KUMAR SHARMA", passport_no="")
    assert h2 and match_status(h2) == "match" and h2[0].candidate_name == "Rajesh Sharma"

    # 3. Non-existent candidate returns empty
    h3 = match_candidate(str(sheet), name="UNKNOWN VISITOR NAME", passport_no="Z0000001")
    assert h3 == [] and match_status(h3) == "none"

    # 4. Backwards-compatible find_candidate still works
    h4 = find_candidate(str(sheet), "Rajesh Sharma")
    assert h4 and h4[0].score >= 0.95
