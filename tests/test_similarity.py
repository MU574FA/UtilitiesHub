"""Duplicate finder engine (pure logic, no display needed)."""
import random
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

from utilshub.core import similarity as sim
from utilshub.core.similarity import DupOptions, FingerprintCache, collect_files, scan


def write_words(d: ImageDraw.ImageDraw, rnd: random.Random, x: int, y: int, x_end: int) -> None:
    """Thin 2-px strokes grouped into 'letters' and 'words' - far closer to real text than solid bars."""
    while x < x_end:
        for _letter in range(rnd.randint(2, 9)):
            for _stroke in range(rnd.randint(1, 3)):
                sx, sy = x + rnd.randint(0, 4), y + rnd.randint(0, 4)
                d.rectangle([sx, sy, sx + 1, sy + rnd.randint(5, 11)], fill=(20, 20, 20))
            x += 7
        x += rnd.randint(8, 14)


def page(seed: int, size=(800, 1100)) -> Image.Image:
    """A fake scanned page: white with random 'text lines'."""
    rnd = random.Random(seed)
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    for row in range(40):
        if rnd.random() < 0.9:
            write_words(d, rnd, 60, 60 + row * 24, size[0] - 80)
    return img


def with_other_name(img: Image.Image, seed: int = 99) -> Image.Image:
    """The same page, but one name-sized field (about 240 x 26 px) holds other words."""
    out = img.copy()
    d = ImageDraw.Draw(out)
    d.rectangle([58, 196, 300, 224], fill="white")
    write_words(d, random.Random(seed), 62, 200, 290)
    return out


def run(files, **kw):
    return scan([Path(f) for f in files], DupOptions(workers=2, **kw))


WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec "
         "romeo sierra tango uniform victor whiskey xray yankee zulu").split()


def article(seed: int, n=220) -> str:
    rnd = random.Random(seed)
    return " ".join(rnd.choice(WORDS) + str(rnd.randint(0, 40)) for _ in range(n))


# --------------------------------------------------------------------------- exact
def test_identical_files_group(tmp_path):
    a, b, c = tmp_path / "a.bin", tmp_path / "b.bin", tmp_path / "c.bin"
    a.write_bytes(b"hello world" * 100)
    shutil.copy(a, b)
    c.write_bytes(b"something else" * 100)
    res = run([a, b, c])
    assert len(res.groups) == 1
    g = res.groups[0]
    assert len(g.members) == 1 and g.members[0].score == 1.0 and g.members[0].why == "Identical file"


def test_three_copies_one_group(tmp_path):
    files = []
    for n in "abc":
        f = tmp_path / f"{n}.dat"
        f.write_bytes(b"x" * 500)
        files.append(f)
    res = run(files)
    assert len(res.groups) == 1 and len(res.groups[0].members) == 2


def test_empty_files_are_ignored(tmp_path):
    for n in "ab":
        (tmp_path / f"{n}.txt").write_bytes(b"")
    assert collect_files([tmp_path]) == []          # min_size=1


# --------------------------------------------------------------------------- pictures
def test_recompressed_and_resized_image_matches(tmp_path):
    img = page(1)
    a, b = tmp_path / "scan.png", tmp_path / "scan_small.jpg"
    img.save(a)
    img.resize((600, 825)).save(b, quality=70)
    res = run([a, b])
    assert len(res.groups) == 1
    m = res.groups[0].members[0]
    assert m.score >= 0.9, m
    assert res.groups[0].keeper.path == str(a)       # 'largest' keeps the bigger one


def test_different_pages_do_not_match(tmp_path):
    a, b = tmp_path / "p1.png", tmp_path / "p2.png"
    page(1).save(a)
    page(2).save(b)
    assert run([a, b]).groups == []


def test_turned_scan_matches_only_when_rotation_allowed(tmp_path):
    a, b = tmp_path / "up.png", tmp_path / "side.png"
    img = page(3)
    img.save(a)
    img.rotate(90, expand=True).save(b)
    on = run([a, b], rotation_invariant=True)
    assert len(on.groups) == 1 and "turned" in on.groups[0].members[0].why
    assert run([a, b], rotation_invariant=False).groups == []


def test_blank_pages_are_not_called_duplicates(tmp_path):
    a, b = tmp_path / "w1.png", tmp_path / "w2.png"
    Image.new("RGB", (500, 700), "white").save(a)
    Image.new("RGB", (640, 900), (252, 252, 252)).save(b)
    assert run([a, b]).groups == []


def test_strict_check_lowers_score_when_one_area_differs(tmp_path):
    base = page(5)
    other = base.copy()
    ImageDraw.Draw(other).rectangle([100, 200, 330, 330], fill="black")        # one field filled in
    a, b = tmp_path / "blank.png", tmp_path / "filled.png"
    base.save(a)
    other.save(b)
    lenient = run([a, b], strict=False, review_threshold=0.3)
    strict = run([a, b], strict=True, review_threshold=0.3)
    assert lenient.groups, "the look-alike should be found without the strict check"
    s_len = lenient.groups[0].members[0].score
    s_str = strict.groups[0].members[0].score if strict.groups else 0.0
    assert s_str < s_len


def test_form_with_another_name_is_not_certain(tmp_path):
    """The dangerous case: the same blank form filled in for two different people."""
    base = page(7)
    a, b = tmp_path / "ahmed.png", tmp_path / "khalil.png"
    base.save(a)
    with_other_name(base).save(b)
    lenient = run([a, b], strict=False, review_threshold=0.3)
    assert lenient.groups and lenient.groups[0].members[0].score >= 0.95      # hashes alone cannot tell
    strict = run([a, b], strict=True, review_threshold=0.3)
    s = strict.groups[0].members[0].score if strict.groups else 0.0
    assert s < DupOptions().auto_threshold, s                                  # ... the strict check must


def test_shifted_rescan_is_offered_but_not_auto(tmp_path):
    base = page(8)
    a, b = tmp_path / "scan1.png", tmp_path / "scan2.png"
    base.save(a)
    base.transform(base.size, Image.AFFINE, (1, 0, 3, 0, 1, 2), fillcolor="white").save(b)
    res = run([a, b])
    assert res.groups, "a re-scan of the same page should be found"
    assert DupOptions().review_threshold <= res.groups[0].members[0].score < DupOptions().auto_threshold


def test_strict_check_keeps_clean_recompression(tmp_path):
    img = page(6)
    a, b = tmp_path / "a.png", tmp_path / "b.jpg"
    img.save(a)
    img.save(b, quality=60)
    res = run([a, b], strict=True)
    assert res.groups and res.groups[0].members[0].score >= 0.9


# --------------------------------------------------------------------------- text
def test_same_text_different_formatting(tmp_path):
    t = article(1)
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text(t, encoding="utf-8")
    b.write_text("  " + t.upper().replace(" ", "\n\n", 5) + "!!", encoding="utf-8")
    res = run([a, b])
    assert len(res.groups) == 1
    m = res.groups[0].members[0]
    assert m.score >= 0.99 and m.why == "Same text"


def test_similar_text_is_never_auto_certain(tmp_path):
    """Two forms that differ by a name must stay below the default auto threshold."""
    words = article(2).split()
    a_txt = " ".join(words)
    words[100] = "AHMED"
    words[101] = "KHALIL"
    a, b = tmp_path / "form1.txt", tmp_path / "form2.txt"
    a.write_text(a_txt, encoding="utf-8")
    b.write_text(" ".join(words), encoding="utf-8")
    res = run([a, b], review_threshold=0.3)
    assert res.groups, "should still be offered for review"
    assert res.groups[0].members[0].score <= sim.AUTO_CAP_SIMILAR_TEXT < DupOptions().auto_threshold


def test_different_texts_do_not_match(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text(article(10), encoding="utf-8")
    b.write_text(article(11), encoding="utf-8")
    assert run([a, b]).groups == []


def test_arabic_text_is_compared(tmp_path):
    ar = "هذا نص عربي طويل لاختبار المقارنة بين الملفات " * 12
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text(ar, encoding="utf-8")
    b.write_text(ar.replace("ا", "أ", 3), encoding="utf-8")
    assert run([a, b]).groups


# --------------------------------------------------------------------------- grouping rules
def test_keep_rules_and_protected_folder(tmp_path):
    lib, dl = tmp_path / "library", tmp_path / "downloads"
    lib.mkdir()
    dl.mkdir()
    a, b = dl / "copy.bin", lib / "orig.bin"
    a.write_bytes(b"z" * 300)
    shutil.copy(a, b)
    old = 1_000_000_000
    import os
    os.utime(a, (old, old))                                    # the download is the OLDER file
    res = run([a, b], keep="oldest")
    assert res.groups[0].keeper.path == str(a)
    res = run([a, b], keep="oldest", protect=str(lib))         # but the library copy is protected
    g = res.groups[0]
    assert g.keeper.path == str(b) and g.keeper.protected and g.members[0].path == str(a)


def test_make_keeper_rescores(tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"q" * 200)
    shutil.copy(a, b)
    res = run([a, b])
    g = res.groups[0]
    other = g.members[0].path
    res.make_keeper(g, other)
    assert g.keeper.path == other and g.members[0].path != other and g.members[0].score == 1.0


# --------------------------------------------------------------------------- housekeeping
def test_cache_roundtrip_and_invalidates_on_change(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text(article(3), encoding="utf-8")
    cache_file = tmp_path / "cache.json"
    opts = DupOptions()
    c1 = FingerprintCache(cache_file, opts.signature())
    fp = sim.fingerprint(f, opts)
    c1.put(fp)
    c1.save()
    c2 = FingerprintCache(cache_file, opts.signature())
    st = f.stat()
    hit = c2.get(str(f), st.st_size, st.st_mtime_ns)
    assert hit and hit.minhash == fp.minhash and hit.text_hash == fp.text_hash
    f.write_text(article(4), encoding="utf-8")                  # changed -> miss
    st = f.stat()
    assert c2.get(str(f), st.st_size, st.st_mtime_ns) is None
    assert FingerprintCache(cache_file, "other-signature").get(str(f), st.st_size, st.st_mtime_ns) is None


def test_collect_files_skips_quarantine_and_temp_files(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / sim.QUARANTINE_NAME).mkdir()
    for p in ("a.pdf", "sub/b.pdf", "~$c.docx", f"{sim.QUARANTINE_NAME}/d.pdf", "e.txt"):
        (tmp_path / p).write_bytes(b"x")
    names = sorted(p.name for p in collect_files([tmp_path]))
    assert names == ["a.pdf", "b.pdf", "e.txt"]
    assert sorted(p.name for p in collect_files([tmp_path], recursive=False)) == ["a.pdf", "e.txt"]
    assert sorted(p.name for p in collect_files([tmp_path], exts={".pdf"})) == ["a.pdf", "b.pdf"]


def test_cancel_returns_quickly(tmp_path):
    for i in range(6):
        (tmp_path / f"{i}.txt").write_text(article(i), encoding="utf-8")
    res = scan(collect_files([tmp_path]), DupOptions(workers=1), cancelled=lambda: True)
    assert res.cancelled and res.groups == []


def test_unreadable_image_is_reported_not_fatal(tmp_path):
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not really a png" * 20)
    good = tmp_path / "good.txt"
    good.write_text("x" * 50)
    res = run([bad, good])
    assert any("broken.png" in p for p, _ in res.problems)
