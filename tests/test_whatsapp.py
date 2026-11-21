"""WhatsApp export reader: chat formats, attachments, people, phone numbers, safe unzipping (no display needed)."""
import datetime as dt
import zipfile

import pytest

from utilshub.core import whatsapp as W

ANDROID = """31/12/23, 9:45 PM - Messages and calls are end-to-end encrypted. No one outside of this chat can read them.
31/12/23, 9:46 PM - Ahmed Ali: hello
13/01/24, 9:47 PM - Ahmed Ali: IMG-20240113-WA0001.jpg (file attached)
my passport copy
second line
13/01/24, 9:50 PM - Me Myself: ok
14/01/24, 10:02 AM - Ahmed Ali: bank letter.9s9.pdf (file attached)
14/01/24, 10:03 AM - Ahmed Ali: my number +971 50 123 4567 joining 1st March 2026
14/01/24, 10:04 AM - Ahmed Ali: <Media omitted>
"""
IOS = """\u200e[31/12/2023, 21:45:12] Sara: \u200eMessages and calls are end-to-end encrypted.
[01/01/2024, 08:15:00] Sara: \u200e<attached: 00000012-PHOTO-2024-01-01-08-15-00.jpg>
[01/01/2024, 08:16:00] +971 55 000 1111: \u200e<attached: 00000013-contract.pdf>
[01/01/2024, 08:17:00] Sara: hi
there
"""


def test_android_chat_messages_dates_and_attachments():
    chat = W.parse_chat(ANDROID, ["IMG-20240113-WA0001.jpg", "bank letter.9s9.pdf"])
    assert chat.date_order == "dmy" and not chat.ambiguous
    assert chat.messages[0].system and chat.messages[0].when == dt.datetime(2023, 12, 31, 21, 45)
    pic = chat.messages[2]
    assert pic.sender == "Ahmed Ali" and pic.files == ["IMG-20240113-WA0001.jpg"]
    assert pic.caption == "my passport copy\nsecond line"            # the marker and the file name are not the caption
    assert chat.messages[4].files == ["bank letter.9s9.pdf"] and chat.messages[4].caption == ""
    assert chat.messages[-1].omitted and not chat.messages[-1].files
    assert chat.participants()[0] == ("Ahmed Ali", 5)


def test_ios_chat_with_marks_and_multiline():
    chat = W.parse_chat(IOS, ["00000012-PHOTO-2024-01-01-08-15-00.jpg", "00000013-contract.pdf"])
    assert [m.sender for m in chat.messages] == ["Sara", "Sara", "+971 55 000 1111", "Sara"]
    assert chat.messages[1].files == ["00000012-PHOTO-2024-01-01-08-15-00.jpg"]
    assert chat.messages[2].files == ["00000013-contract.pdf"]
    assert chat.messages[3].text == "hi\nthere"
    assert chat.messages[1].when == dt.datetime(2024, 1, 1, 8, 15)


def test_date_order_detection_and_override():
    us = "12/31/23, 9:45 PM - A: x\n01/02/24, 9:45 PM - A: y\n"
    assert W.parse_chat(us).date_order == "mdy"
    assert W.parse_chat(us).messages[0].when == dt.datetime(2023, 12, 31, 21, 45)
    amb = "01/02/24, 9:45 AM - A: x\n"
    c = W.parse_chat(amb)
    assert c.ambiguous and c.messages[0].when.month == 2                # day first by default
    assert W.parse_chat(amb, date_order="mdy").messages[0].when.month == 1
    assert W.parse_chat("2024-03-05, 14:00 - A: x\n").messages[0].when == dt.datetime(2024, 3, 5, 14, 0)


def test_arabic_digits_marks_and_am_pm():
    line = "\u200f\u0663\u0661/\u0661\u0662/\u0662\u0660\u0662\u0663\u060c \u0669:\u0664\u0665 \u0645 - \u0623\u062d\u0645\u062f: \u0645\u0631\u062d\u0628\u0627\n"
    m = W.parse_chat(line).messages[0]
    assert m.sender == "\u0623\u062d\u0645\u062f" and m.text == "\u0645\u0631\u062d\u0628\u0627"
    assert m.when == dt.datetime(2023, 12, 31, 21, 45)                  # \u0645 = PM
    assert W.parse_chat("31/12/23, 12:05 AM - A: x\n").messages[0].when.hour == 0


def test_system_line_with_colon_is_not_a_person():
    chat = W.parse_chat('12/01/24, 9:00 AM - Ahmed changed the subject from "a" to "b: c"\n12/01/24, 9:01 AM - Bob: hi\n')
    assert chat.messages[0].system and chat.messages[1].sender == "Bob"


def test_phone_helpers():
    assert W.looks_like_phone("+971 50 123 4567") and W.looks_like_phone("\u202a+44 7700 900123\u202c")
    assert not W.looks_like_phone("Ahmed Ali") and not W.looks_like_phone("12345")
    assert W.clean_phone("\u202a+971\u00a050 123  4567\u202c") == "+971 50 123 4567"
    assert W.find_phone(["call me +971 50 123 4567"]) == "+971 50 123 4567"
    assert W.find_phone(["whatsapp 0501234567 please"]) == "0501234567"      # beside a phone word
    assert W.find_phone(["account 123456789012"]) == ""                      # a bare number is not a phone
    assert W.find_phone(["on 12.03.2024 at noon"]) == ""


def test_person_info_and_guess():
    chat = W.parse_chat(ANDROID, [])
    assert W.guess_person(chat, "Ahmed Ali") == "Ahmed Ali"
    info = W.person_info(chat, "Ahmed Ali")
    assert (info.name, info.phone, info.phone_from) == ("Ahmed Ali", "+971 50 123 4567", "message")
    unsaved = W.person_info(W.parse_chat(IOS, []), "+971 55 000 1111")
    assert (unsaved.name, unsaved.phone, unsaved.phone_from) == ("", "+971 55 000 1111", "sender")
    # a group: the person who sends the files, not the one who talks most
    group = "01/01/24, 9:00 AM - Boss: a\n01/01/24, 9:01 AM - Boss: b\n01/01/24, 9:02 AM - Boss: c\n" \
            "01/01/24, 9:03 AM - Hr: x\n01/01/24, 9:04 AM - Zed: f.pdf (file attached)\n"
    assert W.guess_person(W.parse_chat(group, ["f.pdf"]), "Team") == "Zed"
    assert W.title_from_name("WhatsApp Chat with Ahmed Ali.zip") == "Ahmed Ali"
    assert W.title_from_name("WhatsApp Chat - Sara.zip") == "Sara"


def make_zip(path, chat_name="WhatsApp Chat with Ahmed Ali.txt", chat=ANDROID, extra=()):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(chat_name, chat)
        zf.writestr("IMG-20240113-WA0001.jpg", b"x" * 30000)
        zf.writestr("IMG-20240114-WA0009.jpg", b"x" * 30000)             # a copy of the same bytes
        zf.writestr("bank letter.9s9.pdf", b"%PDF-1.4 test")
        zf.writestr("PTT-20240113-WA0002.opus", b"voice")
        zf.writestr("STK-20240113-WA0003.webp", b"s" * 40000)
        zf.writestr("tiny.jpg", b"t" * 100)
        for name, data in extra:
            zf.writestr(name, data)
    return path


def test_package_lists_links_classifies(tmp_path):
    z = make_zip(tmp_path / "WhatsApp Chat with Ahmed Ali.zip")
    pkg = W.WaPackage(z, root=tmp_path / "work")
    by = {f.name: f for f in pkg.files}
    assert pkg.title == "Ahmed Ali" and pkg.chat and len(pkg.chat.messages) == 7
    assert by["IMG-20240113-WA0001.jpg"].sender == "Ahmed Ali" and by["IMG-20240113-WA0001.jpg"].caption.startswith("my passport")
    assert by["bank letter.9s9.pdf"].when == dt.datetime(2024, 1, 14, 10, 2)
    assert by["PTT-20240113-WA0002.opus"].kind == "voice" and by["PTT-20240113-WA0002.opus"].noise
    assert by["STK-20240113-WA0003.webp"].kind == "sticker" and by["tiny.jpg"].small and by["tiny.jpg"].noise
    assert by["IMG-20240114-WA0009.jpg"].dup_of == "IMG-20240113-WA0001.jpg"
    assert not by["bank letter.9s9.pdf"].noise
    assert [f.name for f in pkg.files][:2] == ["IMG-20240113-WA0001.jpg", "bank letter.9s9.pdf"]   # chat order first


def test_extract_one_file_and_cleanup(tmp_path):
    pkg = W.WaPackage(make_zip(tmp_path / "a.zip"), root=tmp_path / "work")
    p = pkg.extract("bank letter.9s9.pdf")
    assert p.read_bytes() == b"%PDF-1.4 test" and p.parent.parent == tmp_path / "work"
    assert pkg.extract("bank letter.9s9.pdf") == p                      # once
    assert not (p.parent / "IMG-20240113-WA0001.jpg").exists()          # only what you opened
    pkg.cleanup()
    assert not p.exists()
    assert pkg.extract("bank letter.9s9.pdf").exists()                  # and it can come back


def test_zip_slip_is_flattened(tmp_path):
    z = make_zip(tmp_path / "a.zip", extra=[("../../evil.pdf", b"evil"), ("sub/dir/ok.pdf", b"ok")])
    pkg = W.WaPackage(z, root=tmp_path / "work")
    names = {f.name for f in pkg.files}
    assert {"evil.pdf", "ok.pdf"} <= names
    out = pkg.extract("evil.pdf")
    assert out.parent == pkg.workdir and not (tmp_path.parent / "evil.pdf").exists()


def test_bad_zip_is_a_friendly_error(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(W.WaError):
        W.WaPackage(bad, root=tmp_path / "work")


def test_zip_without_chat_and_reparse(tmp_path):
    z = tmp_path / "files.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.pdf", b"%PDF")
    pkg = W.WaPackage(z, root=tmp_path / "work")
    assert pkg.chat is None and [f.name for f in pkg.files] == ["a.pdf"]
    amb = tmp_path / "WhatsApp Chat - X.zip"
    with zipfile.ZipFile(amb, "w") as zf:
        zf.writestr("_chat.txt", "[01/02/2024, 10:00:00] X: a.pdf\n")
        zf.writestr("a.pdf", b"%PDF")
    p2 = W.WaPackage(amb, root=tmp_path / "work")
    assert p2.chat.ambiguous and p2.files[0].when.month == 2
    p2.reparse("mdy")
    assert p2.files[0].when.month == 1


def test_purge_old(tmp_path):
    import os
    import time
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    os.utime(old, (time.time() - 9 * 86400,) * 2)
    assert W.purge_old(3, tmp_path) == 1 and not old.exists() and new.exists()


def test_date_from_text_understands_chat_wording():
    D = dt.date
    assert W.date_from_text("I join on the 1st of March 2026") == D(2026, 3, 1)
    assert W.date_from_text("March 3rd, 2026") == D(2026, 3, 3)
    assert W.date_from_text("starting 22nd Mar 2026") == D(2026, 3, 22)
    assert W.date_from_text("2026-03-01") == D(2026, 3, 1)
    assert W.date_from_text("\u0661\u0665/\u0663/\u0662\u0660\u0662\u0666") == D(2026, 3, 15)
    assert W.date_from_text("see you soon") is None


def test_merge_files_into_one_pdf(tmp_path):
    fitz = pytest.importorskip("pymupdf")
    from PIL import Image
    import io
    z = tmp_path / "m.zip"
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), "white").save(buf, "JPEG")
    pdf = fitz.open()
    pdf.new_page()
    pdf.new_page()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("_chat.txt", "13/01/24, 9:47 PM - Ahmed Ali: IMG-1.jpg (file attached)\nfront\n"
                                 "13/01/24, 9:48 PM - Ahmed Ali: scan.pdf (file attached)\nback\n")
        zf.writestr("IMG-1.jpg", buf.getvalue() * 40)
        zf.writestr("scan.pdf", pdf.tobytes())
    pkg = W.WaPackage(z, root=tmp_path / "work")
    merged, skipped = pkg.merge(["IMG-1.jpg", "scan.pdf"], "Passport pages")
    assert merged.name == "Passport pages.pdf" and not skipped
    assert merged.sender == "Ahmed Ali" and merged.caption == "front / back"
    with fitz.open(pkg.extract(merged.name)) as out:
        assert len(out) == 3                                           # 1 photo page + 2 PDF pages, in the order given
    again, _ = pkg.merge(["IMG-1.jpg", "scan.pdf"], "Passport pages")
    assert again.name == "Passport pages (2).pdf"                      # never overwrites
    with pytest.raises(W.WaError):
        pkg.merge(["IMG-1.jpg"], "x")
