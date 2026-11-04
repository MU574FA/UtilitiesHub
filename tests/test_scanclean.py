"""Scan cleaner engine (pure logic).  PDF tests need PyMuPDF and are skipped without it."""
import random

import numpy as np
from PIL import Image, ImageDraw

from utilshub.core import scanclean as sc
from utilshub.core.ocr import detect_skew
from utilshub.core.scanclean import CleanSpec, PageAnalysis, PageEdit

try:
    from tests.test_similarity import write_words
except ImportError:                                      # run from another folder
    from test_similarity import write_words


def text_page(seed=1, size=(850, 1100)) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    for row in range(38):
        if rnd.random() < 0.92:
            write_words(d, rnd, 70, 70 + row * 24, size[0] - 90)
    return img


def scanned(seed=1, tilt=3.0, border=60, shade=True) -> Image.Image:
    """The page, tilted, lying on a dark scanner bed, with a lighting gradient."""
    page = text_page(seed).rotate(tilt, expand=True, resample=Image.BICUBIC, fillcolor="white")
    bed = Image.new("RGB", (page.width + 2 * border, page.height + 2 * border), (35, 35, 35))
    bed.paste(page, (border, border))
    if shade:
        a = np.asarray(bed, dtype=np.float32)
        ramp = np.linspace(1.0, 0.55, bed.width, dtype=np.float32)[None, :, None]
        bed = Image.fromarray((a * ramp).astype(np.uint8))
    return bed


def test_skew_is_found_and_fixed():
    img = text_page().rotate(3.0, expand=True, resample=Image.BICUBIC, fillcolor="white")
    ana = sc.analyse_image(img, CleanSpec())
    assert 2.0 <= abs(ana.skew) <= 4.0, ana.skew
    fixed = sc.process_page(img, CleanSpec(crop="none", enhance="none"), ana)
    assert abs(detect_skew(fixed)) < 0.8


def test_skew_found_on_dark_scanner_bed_with_uneven_light():
    """Real scans: a dark bed around the page and a lighting gradient used to make the detector return 0 degrees."""
    for tilt in (3.0, -2.0):
        ana = sc.analyse_image(scanned(1, tilt, shade=True), CleanSpec())
        assert abs(ana.skew + tilt) < 1.0, (tilt, ana.skew)        # rotate back by the opposite angle
        assert ana.dark_edges
    fixed = sc.process_page(scanned(1, 3.0), CleanSpec(enhance="none"), sc.analyse_image(scanned(1, 3.0), CleanSpec()))
    assert abs(detect_skew(fixed)) < 1.0


def test_straight_page_has_no_skew_and_no_issues():
    ana = sc.analyse_image(text_page(), CleanSpec())
    assert ana.skew == 0.0 and ana.rot == 0 and not ana.blank


def test_dark_border_is_detected_and_trimmed():
    img = scanned(tilt=0.0, shade=False)
    ana = sc.analyse_image(img, CleanSpec())
    assert ana.dark_edges and ana.crop is not None
    out = sc.process_page(img, CleanSpec(enhance="none"), ana)
    assert out.width < img.width * 0.95 and out.height < img.height * 0.95
    corner = np.asarray(out.convert("L"))[:8, :8]
    assert corner.mean() > 200, "the dark scanner bed must be gone from the corners"


def test_one_dark_strip_only():
    page = text_page()
    bed = Image.new("RGB", (page.width + 50, page.height), (20, 20, 20))
    bed.paste(page, (50, 0))                       # shadow only on the left side
    box, dark = sc.content_box(bed)
    assert dark and box and box[0] > 0.04


def test_margins_are_trimmed_but_keep_breathing_room():
    page = Image.new("RGB", (1000, 1400), "white")
    ImageDraw.Draw(page).rectangle([300, 400, 700, 900], fill=(30, 30, 30))
    box, _ = sc.content_box(page, margin=0.02)
    assert box and 0.25 < box[0] < 0.30 and 0.70 < box[2] < 0.75


def test_blank_page_detection():
    blank = Image.new("RGB", (800, 1100), (250, 250, 248))
    noisy = Image.fromarray(np.clip(np.random.default_rng(1).normal(245, 3, (1100, 800, 3)), 0, 255).astype(np.uint8))
    assert sc.analyse_image(blank).blank and sc.analyse_image(noisy).blank
    assert not sc.analyse_image(text_page()).blank
    photo = Image.fromarray(np.random.default_rng(2).integers(0, 255, (600, 400, 3), dtype=np.uint8))
    assert not sc.analyse_image(photo).blank, "a picture is never a blank page"


def test_enhance_modes():
    img = scanned(tilt=0.0, shade=True, border=0)
    clean = sc.enhance_image(img, "clean")
    left = np.asarray(clean.convert("L"))[100:160, 5:60].mean()
    right = np.asarray(clean.convert("L"))[100:160, -60:-5].mean()
    assert abs(left - right) < 25, "lighting gradient should be evened out"
    raw = np.asarray(img.convert("L"))
    assert raw[100:160, 5:60].mean() - raw[100:160, -60:-5].mean() > 40
    bw = sc.enhance_image(img, "bw")
    assert sc.is_binary(bw) and bw.mode == "L"
    assert sc.enhance_image(img, "gray").mode == "L"
    assert sc.enhance_image(img, "none").mode == "RGB"


def test_manual_overrides_beat_detection():
    spec = CleanSpec()
    ana = PageAnalysis(rot=90, skew=1.5)
    assert sc.effective(spec, ana, None)["rot"] == 90 and sc.effective(spec, ana, None)["skew"] == 1.5
    v = sc.effective(spec, ana, PageEdit(rot=0, skew=-2.0, crop="none", enhance="bw"))
    assert v["rot"] == 0 and v["skew"] == -2.0 and v["crop"] == "none" and v["enhance"] == "bw"
    assert sc.effective(spec, ana, PageEdit(rot=270))["skew"] == 1.5          # upside-down vs detected: tilt still valid
    assert sc.effective(spec, ana, PageEdit(rot=0))["skew"] == 0.0            # other axis: the detected tilt is void
    assert sc.effective(spec, ana, PageEdit(rot=180))["skew"] == 0.0
    assert sc.effective(CleanSpec(rotate=False, deskew=False), ana, None)["rot"] == 0
    assert PageEdit().is_empty() and not PageEdit(keep=True).is_empty()


def test_presets():
    spec = sc.apply_preset(CleanSpec(), "bw")
    assert spec.enhance == "bw" and spec.denoise == 1
    assert sc.apply_preset(CleanSpec(), "nonexistent") == CleanSpec()
    assert sc.apply_preset(CleanSpec(), "photo_id").enhance == "none"


def test_clean_image_file_roundtrip(tmp_path):
    src, out = tmp_path / "scan.png", tmp_path / "scan_clean.png"
    scanned(tilt=2.5).save(src)
    res = sc.clean_file(src, out, CleanSpec())
    assert out.exists() and res.pages_out == 1
    with Image.open(out) as im:
        assert im.width < 1150 and abs(detect_skew(im.convert("RGB"))) < 1.0


def test_original_is_never_modified(tmp_path):
    src, out = tmp_path / "a.png", tmp_path / "b.png"
    scanned().save(src)
    before = src.read_bytes()
    sc.clean_file(src, out, CleanSpec())
    assert src.read_bytes() == before


def test_multipage_tiff_drops_blank_pages(tmp_path):
    src, out = tmp_path / "doc.tif", tmp_path / "doc_clean.tif"
    pages = [text_page(1), Image.new("RGB", (850, 1100), "white"), text_page(2)]
    pages[0].save(src, save_all=True, append_images=pages[1:])
    res = sc.clean_file(src, out, CleanSpec())
    assert (res.pages_in, res.pages_out, res.dropped) == (3, 2, [1])
    with Image.open(out) as im:
        assert im.n_frames == 2
    keep = sc.clean_file(src, tmp_path / "all.tif", CleanSpec(), edits={1: PageEdit(keep=True)})
    assert keep.pages_out == 3 and keep.dropped == []
    force = sc.clean_file(src, tmp_path / "less.tif", CleanSpec(drop_blank=False), edits={0: PageEdit(keep=False)})
    assert force.pages_out == 2 and force.dropped == [0]


def test_single_blank_page_is_kept(tmp_path):
    src = tmp_path / "blank.png"
    Image.new("RGB", (800, 1000), "white").save(src)
    res = sc.clean_file(src, tmp_path / "out.png", CleanSpec())
    assert res.pages_out == 1


def test_all_pages_dropped_is_reported(tmp_path):
    src = tmp_path / "x.png"
    text_page().save(src)
    try:
        sc.clean_file(src, tmp_path / "o.png", CleanSpec(), edits={0: PageEdit(keep=False)})
    except sc.CleanError as exc:
        assert "blank" in str(exc).lower()
    else:
        raise AssertionError("expected CleanError")


def test_output_formats_and_suffixes(tmp_path):
    assert sc.output_suffix("a.JPEG") == ".jpg" and sc.output_suffix("a.gif", 3) == ".tif"
    assert sc.output_suffix("a.pdf") == ".pdf" and sc.output_suffix("a.bmp") == ".bmp"
    src = tmp_path / "s.jpg"
    scanned(tilt=0).convert("RGB").save(src, quality=92)
    for spec_kw, ext in (({}, ".jpg"), ({"enhance": "bw"}, ".png"), ({"enhance": "bw"}, ".tif")):
        out = tmp_path / f"o{ext}"
        sc.clean_file(src, out, CleanSpec(**spec_kw))
        with Image.open(out) as im:
            im.load()
    data, fmt = sc.encode_page(sc.enhance_image(text_page(), "bw"))
    assert fmt == "png"
    assert sc.encode_page(text_page())[1] == "jpeg"


def test_bw_is_much_smaller(tmp_path):
    src = tmp_path / "s.png"
    scanned(tilt=0).save(src)
    colour, bw = tmp_path / "c.png", tmp_path / "b.png"
    sc.clean_file(src, colour, CleanSpec(enhance="none"))
    sc.clean_file(src, bw, CleanSpec(enhance="bw"))
    assert bw.stat().st_size < colour.stat().st_size / 3


def test_cancel(tmp_path):
    src = tmp_path / "s.png"
    text_page().save(src)
    try:
        sc.clean_file(src, tmp_path / "o.png", CleanSpec(), cancelled=lambda: True)
    except sc.CleanError as exc:
        assert "cancel" in str(exc).lower()
    else:
        raise AssertionError("expected CleanError")


def test_pdf_roundtrip_if_pymupdf_present(tmp_path):
    if sc.fitz is None:
        return                                          # PyMuPDF not installed here: nothing to test
    import fitz
    src, out = tmp_path / "s.pdf", tmp_path / "c.pdf"
    doc = fitz.open()
    for img in (scanned(1), Image.new("RGB", (850, 1100), "white")):
        buf = __import__("io").BytesIO()
        img.save(buf, "PNG")
        pg = doc.new_page(width=612, height=792)
        pg.insert_image(pg.rect, stream=buf.getvalue())
    native = doc.new_page()
    native.insert_text((72, 100), "A page with real text. " * 10)
    doc.save(str(src))
    res = sc.clean_file(src, out, CleanSpec(dpi=100))
    assert res.pages_in == 3 and res.pages_out == 2 and res.dropped == [1]
    with fitz.open(out) as d:
        assert "real text" in d[1].get_text()           # the text page was copied untouched
