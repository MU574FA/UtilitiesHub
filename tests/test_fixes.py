"""Regression tests for the review fixes (jobs, CSV, journal, triggers, watcher, palette wiring)."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utilshub.core.appendfile import AppendError, append_records, flush_pending, pending_path  # noqa: E402
from utilshub.core.fileops import Journal, transfer  # noqa: E402
from utilshub.core.jobs import JobRunner  # noqa: E402
from utilshub.core.sheets import formula_safe, write_csv  # noqa: E402
from utilshub.core.triggers import Trigger, TriggerRunner  # noqa: E402


class _Bridge:
    root = None

    def call(self, fn, *a, **k):
        fn(*a, **k)


def test_formula_safe():
    assert formula_safe("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert formula_safe("@SUM(1)") == "'@SUM(1)"
    assert formula_safe("-cmd|' /C calc'!A0").startswith("'")
    assert formula_safe("+964 770 123 4567") == "+964 770 123 4567"        # phone numbers stay
    assert formula_safe("-5") == "-5" and formula_safe("Ali") == "Ali"


def test_append_fast_path_new_column_and_injection(tmp_path):
    f = tmp_path / "list.csv"
    assert append_records(f, [{"name": "=1+1", "phone": "+964 7"}]) == 1
    assert append_records(f, [{"name": "Sara", "phone": "2"}]) == 1                    # in-place append
    assert append_records(f, [{"name": "Omar", "city": "Basra"}]) == 1                  # new column -> rewrite
    text = f.read_text(encoding="utf-8-sig")
    assert "'=1+1" in text and "city" in text.splitlines()[0]
    assert len(text.strip().splitlines()) == 4


def test_pending_rows_are_flushed(tmp_path):
    f = tmp_path / "list.csv"
    append_records(pending_path(f), [{"name": "Waiting"}], timestamp=False)
    assert flush_pending(f) == 1
    assert not pending_path(f).exists() and "Waiting" in f.read_text(encoding="utf-8-sig")


def test_overwrite_keeps_old_file_and_undo_restores(tmp_path, monkeypatch):
    monkeypatch.setenv("UTILSHUB_HOME", str(tmp_path / "home"))
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    (src / "a.txt").write_text("new")
    (dest / "a.txt").write_text("old")
    j = Journal(tmp_path / "j.jsonl")
    out = transfer(src / "a.txt", dest, None, "move", "overwrite", j, "t")
    assert out.read_text() == "new"
    assert any((tmp_path / "home" / "replaced").iterdir())                    # the old file was kept aside
    j.undo(j.last_undoable())
    assert (src / "a.txt").read_text() == "new" and (dest / "a.txt").read_text() == "old"


def test_journal_is_trimmed(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    for i in range(30):
        j.record("t", "move", f"a{i}", f"b{i}")
    assert len(Journal(tmp_path / "j.jsonl", max_entries=10).entries()) == 10


def test_serial_jobs_do_not_block_other_work():
    runner = JobRunner(_Bridge(), max_workers=2)
    gate, order = threading.Event(), []
    runner.submit(lambda job: (gate.wait(5), order.append("s1")), serial_key="cv")
    runner.submit(lambda job: order.append("s2"), serial_key="cv")
    runner.submit(lambda job: order.append("s3"), serial_key="cv")
    done = threading.Event()
    runner.submit(lambda job: (order.append("other"), done.set()))              # not stuck behind the queued CV jobs
    assert done.wait(3)
    gate.set()
    for _ in range(50):
        if len(order) == 4:
            break
        time.sleep(0.05)
    assert order == ["other", "s1", "s2", "s3"], order
    runner.shutdown()


def test_progress_listener_does_not_leak():
    runner = JobRunner(_Bridge(), max_workers=2)
    seen = []
    runner.submit(lambda job: job.progress(1, 1), on_progress=seen.append)
    time.sleep(0.3)
    assert runner.listeners == [] and seen
    runner.shutdown()


def test_trigger_ignores_files_made_by_its_own_workflow(tmp_path):
    ran = []
    r = TriggerRunner(lambda trig, path: ran.append(path.name))
    t = Trigger(name="t", folder=str(tmp_path), workflow="w")
    made = tmp_path / "out.pdf"
    made.write_text("x")
    other = tmp_path / "in.pdf"
    other.write_text("x")
    r.ignore(made)
    r._on_file(t, made)
    r._on_file(t, other)
    assert ran == ["in.pdf"] and r.handled_names(t.id) == ["in.pdf"]


def test_write_csv_neutralises_formulas(tmp_path):
    write_csv(tmp_path / "x.csv", [["name"], ["=cmd()"]])
    assert "'=cmd()" in (tmp_path / "x.csv").read_text(encoding="utf-8-sig")


def test_locked_error_type():
    assert issubclass(AppendError, Exception)
