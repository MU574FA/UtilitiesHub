import datetime as dt, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utilshub.core.rules import Rule, evaluate_rule, score_rules, parse_keywords
from utilshub.core.fields import FieldEngine, ExtractContext, parse_specs, default_cv_fields, default_screenshot_fields
from utilshub.core.fuzzy import name_score, prep_name
from utilshub.core.matching import match_lists, one_to_one
from utilshub.core.templates import render_filename, render_clipboard, render_link
from utilshub.core.textutil import parse_page_ranges, format_page_ranges
from utilshub.core.doctypes import DocType, default_doctypes, score_doctype, run_check, CheckContext, Check
from utilshub.core.readers import DocInfo
from utilshub.core.mrz import parse_mrz
from utilshub.core.settings import Settings
from utilshub.core.schema import Opt

CV = """John A. Smith
Senior Software Engineer at Acme Inc.
john.smith@acme.com  +964 770 123 4567
LinkedIn: linkedin.com/in/johnsmith
8 years of experience. Python, SQL, Doker
"""

def test_rules():
    r = Rule(kind="keyword", pattern="Iraqi = iraq, baghdad\nIndian = india", ignore_case=True)
    assert evaluate_rule(r, "from Baghdad").values == ["Iraqi"]
    r = Rule(kind="regex", pattern=r"(\d+)\s*years")
    assert evaluate_rule(r, "8 years").first == "8"
    r = Rule(kind="keyword", pattern="docker", fuzzy=True)
    assert evaluate_rule(r, CV).hit
    assert evaluate_rule(Rule(kind="regex", pattern="("), "x").error
    sc, _ = score_rules([Rule(kind="keyword", pattern="a, b", level="required"),
                         Rule(kind="keyword", pattern="zzz", level="required")], "a b", needed=1)
    assert sc == 0

def test_fields():
    ctx = ExtractContext(["resume","cv"], ["engineer"], ["Inc"], [], 0.8)
    out = FieldEngine(parse_specs(default_cv_fields()), ctx).extract(CV)
    assert out["name"] == ["John A. Smith"], out
    assert out["email"] == ["john.smith@acme.com"]
    assert out["job_title"] == ["Senior Software Engineer"], out
    assert "Python" in out["skills"] and "Docker" in out["skills"], out
    s = FieldEngine(parse_specs(default_screenshot_fields()), ctx).extract(CV)
    assert s["phone"] and s["phone"][0].isdigit()

def test_names():
    assert name_score(prep_name("محمد علي حسن"), prep_name("محمد على حسن")) > 0.95
    assert name_score(prep_name("Mohammed Ali Hassan"), prep_name("Hassan Mohamed Ali")) > 0.85
    res = match_lists(["Jon Smyth", "Zainab Mahdi"], ["John Smith", "Zeinab Mahdy", "Other"])
    assert res[0][0][0] == 0 and res[1][0][0] == 1
    assert one_to_one(res)[0][0] == 0

def test_templates():
    f = {"name": ["John Smith"], "job_title": []}
    assert render_filename("{name} - {job_title}", f, Path("x.pdf")) == "John Smith"
    assert render_clipboard(r"{name|upper}\t{name|initials}", f, None) == "JOHN SMITH\tJS"
    assert render_link("https://x/{filename}", f, Path("a.pdf"), "John Smith.pdf") == "https://x/John%20Smith.pdf"

def test_pages():
    assert parse_page_ranges("1-3, 5, 8-", 9) == [0,1,2,4,7,8]
    assert format_page_ranges([0,1,2,4]) == "1-3, 5"

def test_doctypes():
    types = [DocType.from_dict(d) for d in default_doctypes()]
    info = DocInfo("/tmp/bank_letter.pdf", "pdf", "Account number and IBAN confirmation SWIFT confidential bank IBAN GB82WEST12345698765432")
    scores = {t.id: score_doctype(t, info, "bank_letter.pdf") for t in types}
    assert scores["bank"] == 1.0 and scores["passport"] < 0.35, scores
    bank = types[2]
    res = [run_check(c, info, CheckContext(name="X Y", surname="Y")) for c in bank.checks if c.type in ("regex","manual")]
    assert res[0][0] == "ok"

def test_mrz():
    t = "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\nL898902C36UTO7408122F1204159ZE184226B<<<<<10"
    m = parse_mrz(t); assert m and m["surname"] == "ERIKSSON" and m["ok"], m

def test_settings():
    with tempfile.TemporaryDirectory() as d:
        s = Settings(Path(d)/"s.json", autosave=False)
        s.register([Opt("a.b", [1], "x", "list")])
        got=[]; s.subscribe("a.", lambda k,v: got.append(v))
        s.set("a.b", [2]); s.save()
        assert Settings(Path(d)/"s.json").get("a.b", None) == [2] and got == [[2]]


def test_arabic_and_cover():
    import pymupdf as fitz
    from utilshub.core import arabic
    from utilshub.tools.docpacker import draw_cover
    assert arabic.available()
    shaped = arabic.visual("محمد علي")
    assert shaped != "محمد علي"
    doc = fitz.open()
    page = doc.new_page()
    draw_cover(page, {"name": "محمد علي", "passport_no": "A12345"},
               {"title": "ملف المستندات - {name}", "lines": ["رقم الجواز: {passport_no}", "Empty: {missing_key}"],
                "checklist": True, "refs": True, "missing": True},
               [("Passport", "ok"), ("Bank letter", "warn")], {0: 2, 1: 3}, ["Medical"])
    text = page.get_text("text")
    assert "A12345" in text and "Empty:" not in text and "p. 2" in text
    doc.close()


def test_textmatch_and_mrz_summary():
    from utilshub.core.textmatch import find_text, split_patterns
    from utilshub.core.mrz import passport_summary
    pages = ["Invoice 123456789\nCustomer: Mohammed Ali Hassan\nTotal: 500"]
    rx_hits = find_text(pages, split_patterns(r"\b\d{9}\b"), "regex")
    assert len(rx_hits) == 1 and rx_hits[0].text == "123456789" and rx_hits[0].page == 1 and rx_hits[0].line == 1
    sim_hits = find_text(pages, ["Mohamed Aly Hasan"], "similar", threshold=0.8)
    assert sim_hits and "Mohammed Ali Hassan" in sim_hits[0].text
    t = "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\nL898902C36UTO7408122F1204159ZE184226B<<<<<10"
    s = passport_summary(t, min_months=6, today=dt.date(2012, 1, 1))
    assert s["mrz"] == "valid" and s["status"] == "expires soon" and s["surname"] == "ERIKSSON"


def test_append_and_folder_templates():
    from utilshub.core.appendfile import append_records
    from utilshub.core.templates import render_folder, render_line
    from utilshub.tools.docpacker import custom_keys, detect_groups, page_info
    assert render_folder("{surname}/../{year}/", {"surname": "Smith", "year": "2026"}) == Path("Smith/2026")
    assert render_line("Passport: {p}", {"p": ""}) == ""
    assert custom_keys(["Passport no.", "رقم الهوية"]) == {"Passport no.": "passport_no", "رقم الهوية": "field2"}
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "master.csv"
        append_records(csv_path, [{"name": "Alice", "score": 90}], timestamp=False)
        append_records(csv_path, [{"name": "Bob", "extra": "yes"}], timestamp=False)
        text = csv_path.read_text(encoding="utf-8-sig")
        assert "Alice" in text and "Bob" in text and "extra" in text
    types = [DocType.from_dict(d) for d in default_doctypes()]
    multi = DocInfo("/tmp/all.pdf", "pdf", pages=2,
                    page_texts=["Account number and IBAN confirmation SWIFT confidential bank IBAN GB82WEST12345698765432",
                                "Medical report IGRA negative HIV negative HCV negative HBsAg negative Laboratory"])
    groups = detect_groups(types, multi, 0.8)
    assert [g[0] for g in groups] == ["bank", "medical"]
    assert page_info(multi, [1]).pages == 1
