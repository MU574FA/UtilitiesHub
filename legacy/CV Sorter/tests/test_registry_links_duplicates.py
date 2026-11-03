import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from cvsorter.config import LinkTemplate, Settings  # noqa: E402
from cvsorter.fileops import predicted_name  # noqa: E402
from cvsorter.links import build_links, render_link  # noqa: E402
from cvsorter.preview import PreviewUnavailable, render_preview  # noqa: E402
from cvsorter.processor import AnalysisResult, Decision, finalize  # noqa: E402
from cvsorter.registry import NameRegistry  # noqa: E402


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _reg(self, content="John Smith\nMaria Garcia\nJosé Núñez\n", name="names.txt", **kw):
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return NameRegistry(str(path), **kw)

    def test_disabled_without_path(self):
        self.assertEqual(NameRegistry("").check("John Smith").status, "off")

    def test_exact_similar_new(self):
        reg = self._reg()
        self.assertEqual(reg.check("john smith").status, "exact")
        self.assertEqual(reg.check("Smith, John").status, "exact")           # word order ignored
        self.assertEqual(reg.check("Jose Nunez").status, "exact")            # accents ignored
        similar = reg.check("Jon Smith")
        self.assertEqual(similar.status, "similar")
        self.assertEqual(similar.best.name, "John Smith")                    # full name reported
        self.assertEqual(reg.check("John A. Smith").status, "similar")       # extra middle name
        self.assertEqual(reg.check("Peter Brown").status, "new")

    def test_add_persists_and_does_not_duplicate(self):
        reg = self._reg("John Smith")                                        # no trailing newline
        self.assertTrue(reg.add("Peter Brown"))
        self.assertFalse(reg.add("peter brown"))
        self.assertEqual((self.dir / "names.txt").read_text().splitlines(), ["John Smith", "Peter Brown"])
        self.assertEqual(NameRegistry(str(self.dir / "names.txt")).check("Peter Brown").status, "exact")

    def test_external_edits_are_picked_up(self):
        reg = self._reg()
        self.assertEqual(reg.check("Anna Lee").status, "new")
        with (self.dir / "names.txt").open("a", encoding="utf-8") as fh:
            fh.write("Anna Lee\n")
        self.assertEqual(reg.check("Anna Lee").status, "exact")

    def test_csv_with_header_column(self):
        reg = self._reg("ID,Full Name,City\n1,John Smith,Paris\n", name="names.csv")
        self.assertEqual(reg.check("John Smith").status, "exact")
        reg.add("Peter Brown")
        rows = list(csv.reader(io.StringIO((self.dir / "names.csv").read_text())))
        self.assertEqual(rows[-1], ["", "Peter Brown", ""])

    def test_missing_file_is_created_on_add(self):
        reg = NameRegistry(str(self.dir / "sub" / "new.txt"))
        self.assertEqual(reg.check("Anna Lee").status, "new")
        self.assertTrue(reg.add("Anna Lee"))
        self.assertTrue((self.dir / "sub" / "new.txt").exists())


class LinkTests(unittest.TestCase):
    def test_filename_is_url_encoded(self):
        url = render_link("https://x.example/Archive/{filename}", {}, Path("a.pdf"), "John Smith (2).pdf")
        self.assertEqual(url, "https://x.example/Archive/John%20Smith%20%282%29.pdf")

    def test_raw_stem_and_fields(self):
        url = render_link("https://x/{stem_raw}/{company}?d={date_raw}", {"company": ["A&B Co"]},
                          Path("a.pdf"), "Ann Lee.pdf")
        self.assertTrue(url.startswith("https://x/Ann Lee/A%26B%20Co?d="))

    def test_build_links_keys(self):
        s = Settings(link_templates=[LinkTemplate("Archive link", "https://x/{filename}")])
        self.assertEqual(build_links(s, {}, Path("a.pdf"), "Ann.pdf"), {"archive_link": ["https://x/Ann.pdf"]})

    def test_bad_template(self):
        with self.assertRaises(ValueError):
            render_link("https://x/{filename", {}, Path("a.pdf"), "a.pdf")


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.inbox, self.out = root / "in", root / "out"
        self.inbox.mkdir()
        self.settings = Settings(
            watch_folder=str(self.inbox), output_folder=str(self.out),
            link_templates=[LinkTemplate("Archive", "https://x/{filename}")])
        self.registry = NameRegistry(str(root / "names.txt"))

    def tearDown(self):
        self.tmp.cleanup()

    def _cv(self, name="scan.pdf"):
        path = self.inbox / name
        path.write_text("x")
        return AnalysisResult(path)

    def test_save_adds_name_and_builds_link(self):
        out = finalize(self._cv(), Decision("save", {"name": ["Ann Lee"]}), self.settings, self.registry)
        self.assertEqual((out.status, out.dest.name), ("saved", "Ann Lee.pdf"))
        self.assertEqual(out.links["archive"], ["https://x/Ann%20Lee.pdf"])
        self.assertTrue(out.name_added)
        self.assertEqual(self.registry.check("Ann Lee").status, "exact")

    def test_link_uses_numbered_name(self):
        finalize(self._cv("a.pdf"), Decision("save", {"name": ["Ann Lee"]}), self.settings, self.registry)
        out = finalize(self._cv("b.pdf"), Decision("save", {"name": ["Ann Lee"]}), self.settings, self.registry)
        self.assertEqual(out.dest.name, "Ann Lee (2).pdf")
        self.assertEqual(out.links["archive"], ["https://x/Ann%20Lee%20%282%29.pdf"])
        self.assertEqual(predicted_name(self.settings, "Ann Lee", ".pdf"), "Ann Lee (3).pdf")

    def test_duplicate_goes_to_duplicates_folder_and_not_to_list(self):
        cv = self._cv()
        out = finalize(cv, Decision("duplicate", {"name": ["Ann Lee"]}), self.settings, self.registry)
        self.assertEqual(out.status, "duplicate")
        self.assertEqual(out.dest.parent, self.out / "Duplicates")
        self.assertFalse(cv.path.exists())
        self.assertFalse(out.name_added)
        self.assertEqual(self.registry.check("Ann Lee").status, "new")

    def test_record_only_leaves_file(self):
        cv = self._cv()
        out = finalize(cv, Decision("save", {"name": ["Ann Lee"]}, move=False), self.settings, self.registry)
        self.assertEqual(out.status, "recorded")
        self.assertTrue(cv.path.exists())
        self.assertIsNone(out.dest)
        self.assertEqual(out.filename, "Ann Lee.pdf")
        history = (self.out / "cv_history.csv").read_text(encoding="utf-8-sig")
        self.assertIn("recorded", history)

    def test_custom_duplicates_folder_and_original_name(self):
        self.settings.duplicates_folder = str(self.out.parent / "dups")
        self.settings.duplicates_rename = False
        out = finalize(self._cv("keep_me.pdf"), Decision("duplicate", {"name": ["Ann Lee"]}), self.settings)
        self.assertEqual(out.dest, self.out.parent / "dups" / "keep_me.pdf")


class PreviewTests(unittest.TestCase):
    def test_image_preview_returns_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cv.png"
            Image.new("RGB", (1600, 2000), "white").save(path)
            result = render_preview(path, Settings(preview_width=500))
        self.assertEqual(result.total, 1)
        self.assertTrue(result.pages[0].startswith(b"\x89PNG"))
        self.assertLessEqual(Image.open(io.BytesIO(result.pages[0])).width, 500)

    def test_text_has_no_visual_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cv.txt"
            path.write_text("hello")
            with self.assertRaises(PreviewUnavailable):
                render_preview(path, Settings())


if __name__ == "__main__":
    unittest.main()
