import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvsorter.clipboard import render_clipboard  # noqa: E402
from cvsorter.config import CustomField, Settings  # noqa: E402
from cvsorter.extractor import CVExtractor, extract_custom  # noqa: E402
from cvsorter.fuzzy import FuzzyIndex, KeywordMatcher  # noqa: E402


class FuzzyTests(unittest.TestCase):
    def test_ocr_typos_are_found(self):
        idx = FuzzyIndex("Skills: Pyth0n, Dockr and postgresql")
        self.assertTrue(idx.find("Python", 0.8))
        self.assertTrue(idx.find("Docker", 0.8))
        self.assertIsNone(idx.find("Kubernetes", 0.8))

    def test_short_terms_must_match_exactly(self):
        self.assertIsNone(FuzzyIndex("I know SQI").find("SQL", 0.5))
        self.assertTrue(FuzzyIndex("I know SQL").find("SQL", 0.8))

    def test_keyword_field_fuzzy_switch(self):
        spec = CustomField("Skills", "keywords", "Python, Docker", fuzzy=True)
        self.assertEqual(extract_custom(spec, "Pyth0n and Dockr"), ["Python", "Docker"])
        spec.fuzzy = False
        self.assertEqual(extract_custom(spec, "Pyth0n and Dockr"), [])

    def test_job_title_matcher(self):
        words = ["engineer", "developer"]
        self.assertFalse(KeywordMatcher(words, fuzzy=False).search("Software Enginer"))
        self.assertTrue(KeywordMatcher(words, fuzzy=True).search("Software Enginer"))

    def test_known_company_lookup_and_snap(self):
        s = Settings(known_companies=["Google", "Acme Corp"])
        res = CVExtractor(s).extract("Jane Doe\nWorked at Gogle LLC for years", Path("x.txt"))
        self.assertEqual(res["company"], ["Google"])
        res = CVExtractor(s).extract("Acmee Corp\nthen Google", Path("x.txt"))
        self.assertEqual(res["company"], ["Acme Corp", "Google"])   # order of appearance

    def test_company_snap_can_be_disabled(self):
        s = Settings(known_companies=["Google"], company_snap=False)
        res = CVExtractor(s).extract("Jane Doe\nWorked at Gogle LLC", Path("x.txt"))
        self.assertIn("Google", res["company"])        # found via the list
        self.assertTrue(any("Gogle" in c for c in res["company"]))  # raw detection kept


class ClipboardTests(unittest.TestCase):
    FIELDS = {"name": ["Ann Lee"], "job_title": [], "phone": ["+1 555 0100", "+1 555 0199"]}

    def test_tab_layout_keeps_empty_columns(self):
        out = render_clipboard(r"{name}\t{job_title}\t{phone}", self.FIELDS, Path("a.pdf"))
        self.assertEqual(out, "Ann Lee\t\t+1 555 0100")

    def test_all_values_and_newlines(self):
        out = render_clipboard(r"{phone_all}\n{filename}", self.FIELDS, Path("a.pdf"), "Ann Lee.pdf")
        self.assertEqual(out, "+1 555 0100; +1 555 0199\nAnn Lee.pdf")

    def test_custom_order(self):
        out = render_clipboard("{phone} | {name}", self.FIELDS, Path("a.pdf"))
        self.assertEqual(out, "+1 555 0100 | Ann Lee")

    def test_bad_template(self):
        with self.assertRaises(ValueError):
            render_clipboard("{name", self.FIELDS, Path("a.pdf"))


if __name__ == "__main__":
    unittest.main()
