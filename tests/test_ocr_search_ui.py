"""Headless Similarity Search UI checks (fake tkinter, separate process)."""
import subprocess
import sys
from pathlib import Path


def test_similarity_search_ui_in_subprocess():
    run = Path(__file__).parent / "ui_smoke" / "similarity_search.py"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(run)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(Path(__file__).parents[1]),
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
