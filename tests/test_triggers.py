"""Folder triggers (core/triggers.py): store, extension parsing, watching a real folder, loop protection."""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utilshub.core.triggers import Trigger, TriggerRunner, TriggerStore, parse_extensions


def wait_for(cond, seconds=8.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def test_parse_extensions():
    assert parse_extensions("pdf, .JPG;png *.tif") == {".pdf", ".jpg", ".png", ".tif"}
    assert parse_extensions("") == set()


def test_store_roundtrip_and_broken_file():
    d = Path(tempfile.mkdtemp())
    store = TriggerStore(d / "t.json")
    t = Trigger(name="Inbox", folder=str(d), workflow="W", extensions="pdf")
    store.put(t)
    t.name = "Renamed"
    store.put(t)                                           # same id: replaced, not duplicated
    again = TriggerStore(d / "t.json")
    assert [x.name for x in again.items] == ["Renamed"] and again.get(t.id).extensions == "pdf"
    again.remove(t.id)
    assert TriggerStore(d / "t.json").items == []
    (d / "bad.json").write_text("{ not json")
    assert TriggerStore(d / "bad.json").items == [] and (d / "bad.broken.json").exists()   # kept, not lost


def test_unknown_fields_are_ignored():
    t = Trigger.from_dict({"name": "x", "folder": "f", "workflow": "w", "from_the_future": 1})
    assert t.name == "x" and not t.problem()
    assert Trigger(name="x", workflow="w").problem() == "no folder"
    assert Trigger(name="x", folder="f").problem() == "no workflow"


def test_runner_fires_once_per_new_file_and_filters_types():
    d = Path(tempfile.mkdtemp())
    (d / "old.pdf").write_bytes(b"already here")           # exists before start: ignored
    got, lock = [], threading.Lock()

    def run(trig, path):
        with lock:
            got.append(path.name)

    runner = TriggerRunner(run)
    trig = Trigger(name="t", folder=str(d), workflow="w", extensions="pdf", quiet_seconds=0.0)
    assert runner.start(trig) and runner.is_running(trig.id)
    (d / "new.pdf").write_bytes(b"x" * 10)
    (d / "skip.txt").write_bytes(b"x")                      # wrong type
    assert wait_for(lambda: "new.pdf" in got)
    time.sleep(0.5)
    assert got == ["new.pdf"], got
    runner.stop_all()
    assert not runner.is_running(trig.id)


def test_process_existing():
    d = Path(tempfile.mkdtemp())
    (d / "a.pdf").write_bytes(b"1")
    got = []
    runner = TriggerRunner(lambda t, p: got.append(p.name))
    runner.start(Trigger(name="t", folder=str(d), workflow="w", quiet_seconds=0.0, process_existing=True))
    assert wait_for(lambda: got == ["a.pdf"])
    runner.stop_all()


def test_loop_protection_pauses_the_trigger():
    d = Path(tempfile.mkdtemp())
    got, said = [], []
    runner = TriggerRunner(lambda t, p: got.append(p.name), say=lambda m, lv="info": said.append((lv, m)), max_per_minute=3)
    trig = Trigger(name="loopy", folder=str(d), workflow="w", quiet_seconds=0.0)
    runner.start(trig)
    for i in range(8):
        (d / f"f{i}.txt").write_bytes(b"x" * (i + 1))
    assert wait_for(lambda: trig.id in runner.paused)
    assert wait_for(lambda: any(lv == "warn" and "paused" in m for lv, m in said))     # said after the watcher stopped
    assert len(got) <= 3
    assert not runner.is_running(trig.id)
    runner.stop_all()


def test_cannot_start_without_folder_or_workflow():
    said = []
    runner = TriggerRunner(lambda t, p: None, say=lambda m, lv="info": said.append(m))
    assert not runner.start(Trigger(name="bad", workflow="w"))
    assert said and "cannot start" in said[0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
