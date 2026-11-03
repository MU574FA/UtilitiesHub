"""Run with:  python -m unittest discover -s tests -v   (no extra dependencies needed)"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvsorter.config import CustomField, Settings  # noqa: E402
from cvsorter.extractor import CVExtractor, extract_custom  # noqa: E402
from cvsorter.fileops import render_filename, transfer  # noqa: E402

SAMPLE = """JOHN A. SMITH
Senior Software Engineer
john.smith@example.com | +1 (415) 555-2671
linkedin.com/in/johnsmith

Experience
Senior Software Engineer at Acme Technologies Inc. 2019 - 2023
Built services in Python and Docker. 7 years of experience.
"""


class ExtractorTests(unittest.TestCase):
    def setUp(self):
        self.result = CVExtractor(Settings()).extract(SAMPLE, Path("scan_001.pdf"))

    def test_name(self):
        self.assertEqual(self.result["name"], ["John A. Smith"])

    def test_email_phone(self):
        self.assertEqual(self.result["email"], ["john.smith@example.com"])
        self.assertEqual(self.result["phone"], ["+1 (415) 555-2671"])  # year range ignored

    def test_title_company(self):
        self.assertEqual(self.result["job_title"], ["Senior Software Engineer"])
        self.assertEqual(self.result["company"], ["Acme Technologies Inc."])

    def test_custom_fields(self):
        self.assertEqual(self.result["linkedin"], ["linkedin.com/in/johnsmith"])
        self.assertEqual(self.result["years_of_experience"], ["7"])
        self.assertEqual(self.result["skills"], ["Python", "Docker"])

    def test_name_fallback_to_email(self):
        res = CVExtractor(Settings()).extract("jane_doe@mail.org", Path("x.txt"))
        self.assertEqual(res["name"], ["Jane Doe"])

    def test_bad_regex_raises_for_gui_validation(self):
        with self.assertRaises(Exception):
            extract_custom(CustomField("x", "regex", "(unclosed"), "text")


class FileOpsTests(unittest.TestCase):
    def test_template(self):
        fields = {"name": ["John Smith"], "company": [], "job_title": ["Dev/Ops"]}
        out = render_filename("{name} - {company} - {job_title}", fields, Path("a.pdf"))
        self.assertEqual(out, "John Smith - DevOps")

    def test_bad_template_falls_back(self):
        self.assertEqual(render_filename("{name", {"name": ["Ann Lee"]}, Path("a.pdf")), "Ann Lee")

    def test_duplicates_are_numbered(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / "out"
            for i in range(2):
                src = tmp / f"cv{i}.txt"
                src.write_text("x")
                transfer(src, out, "John Smith")
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["John Smith (2).txt", "John Smith.txt"])


if __name__ == "__main__":
    unittest.main()
