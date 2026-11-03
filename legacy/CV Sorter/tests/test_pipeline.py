"""End-to-end: watcher -> read -> extract -> rename/move -> history CSV."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import docx  # noqa: E402
from pptx import Presentation  # noqa: E402

from cvsorter.config import Settings  # noqa: E402
from cvsorter.processor import Decision, analyze, finalize  # noqa: E402
from cvsorter.watcher import FolderWatcher  # noqa: E402

LINES = ["Maria Garcia", "Data Analyst", "maria.garcia@mail.com", "+44 20 7946 0958"]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(watch_folder=str(root / "in"), output_folder=str(root / "out"),
                                 ocr_enabled=False, stable_seconds=0.5)
        self.inbox = root / "in"
        self.inbox.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _make(self, kind):
        path = self.inbox / f"scan.{kind}"
        if kind == "txt":
            path.write_text("\n".join(LINES), encoding="utf-8")
        elif kind == "docx":
            d = docx.Document()
            for line in LINES:
                d.add_paragraph(line)
            d.save(path)
        else:
            prs = Presentation()
            slide = prs.slides.add_slide(prs.slide_layouts[5])
            slide.shapes.title.text = LINES[0]
            box = slide.shapes.add_textbox(0, 0, 100, 100)
            box.text_frame.text = "\n".join(LINES[1:])
            prs.save(path)
        return path

    def test_each_format_is_read_renamed_and_moved(self):
        for kind in ("txt", "docx", "pptx"):
            with self.subTest(kind=kind):
                path = self._make(kind)
                result = analyze(path, self.settings)
                self.assertIsNone(result.error)
                self.assertEqual(result.fields["name"], ["Maria Garcia"])
                self.assertEqual(result.fields["email"], ["maria.garcia@mail.com"])
                dest = finalize(result, Decision("save", result.fields), self.settings).dest
                self.assertEqual(dest.name, f"Maria Garcia.{kind}")
                self.assertFalse(path.exists())
        self.assertTrue((Path(self.settings.output_folder) / "cv_history.csv").exists())

    def test_watcher_detects_dropped_file(self):
        seen, done = [], threading.Event()
        watcher = FolderWatcher(self.inbox, [".txt"], 0.5, lambda p: (seen.append(p), done.set()))
        watcher.start()
        try:
            (self.inbox / "drop.txt").write_text("hello")
            self.assertTrue(done.wait(10), "watcher did not report the file")
        finally:
            watcher.stop()
        self.assertEqual(seen[0].name, "drop.txt")


if __name__ == "__main__":
    unittest.main()
