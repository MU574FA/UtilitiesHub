"""Headless UI logic tests (fake tkinter, separate process).  See tests/ui_smoke/run.py."""
import subprocess
import sys
from pathlib import Path


def test_ui_smoke_in_subprocess():
    run = Path(__file__).parent / "ui_smoke" / "run.py"
    proc = subprocess.run([sys.executable, str(run)], capture_output=True, text=True, timeout=600,
                          cwd=str(Path(__file__).parents[1]))
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
