"""Tool order / hide logic and journal batches (pure logic)."""
from pathlib import Path

from utilshub.core import toolorder as to
from utilshub.core.fileops import Journal, batch_note, new_batch, transfer

DEFAULT = ["home", "cv", "docs", "picker", "dupes", "scan", "history"]


def test_default_order_when_nothing_saved():
    assert to.ordered_ids(DEFAULT, None) == DEFAULT
    assert to.ordered_ids(DEFAULT, []) == DEFAULT


def test_saved_order_is_respected_and_home_pinned():
    out = to.ordered_ids(DEFAULT, ["history", "scan", "home", "cv"])
    assert out[:4] == ["home", "history", "scan", "cv"]
    assert sorted(out) == sorted(DEFAULT)               # nothing lost, nothing duplicated


def test_new_tool_lands_after_its_default_predecessor():
    saved = ["home", "docs", "cv", "picker", "history"]  # user's order, from before dupes/scan existed
    out = to.ordered_ids(DEFAULT, saved)
    assert out.index("dupes") == out.index("picker") + 1
    assert out.index("scan") == out.index("dupes") + 1


def test_stale_and_duplicate_ids_ignored():
    out = to.ordered_ids(DEFAULT, ["gone", "cv", "cv", "home"])
    assert "gone" not in out and out.count("cv") == 1


def test_move_clamps_and_never_passes_home():
    ids = list(DEFAULT)
    assert to.move(ids, "cv", -5)[0] == "home" and to.move(ids, "cv", -5)[1] == "cv"
    assert to.move(ids, "history", 5)[-1] == "history"
    assert to.move(ids, "home", 3) == ids                 # home cannot move
    assert to.move_to(ids, "scan", 0)[:2] == ["home", "scan"]
    assert to.move(ids, "nope", 1) == ids


def test_hidden_rules():
    h = to.set_hidden([], "cv", True)
    assert h == ["cv"] and to.set_hidden(h, "cv", True) == ["cv"]
    assert to.set_hidden(h, "cv", False) == []
    assert to.set_hidden([], "home", True) == []          # home can never be hidden
    assert to.visible(DEFAULT, ["cv", "home"]) == [i for i in DEFAULT if i != "cv"]
    assert to.prune_hidden(["cv", "gone", "home"], DEFAULT) == ["cv"]


def test_journal_batch_undo(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    j = Journal(tmp_path / "journal.jsonl")
    b1, b2 = new_batch(), new_batch()
    for i, batch in enumerate([b1, b1, b1, b2]):
        f = src / f"f{i}.txt"
        f.write_text(str(i))
        transfer(f, dst, None, "move", "number", j, "duplicates", note=batch_note(batch, f"dup {i}"))
    assert j.last_batch("duplicates") == b2
    assert j.undo_batch(b2, "duplicates") == (1, [])
    assert j.last_batch("duplicates") == b1
    done, problems = j.undo_batch(b1, "duplicates")
    assert done == 3 and not problems
    assert sorted(p.name for p in src.iterdir()) == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]
    assert j.last_batch("duplicates") == ""


def test_journal_batch_reports_blocked_undo(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    j = Journal(tmp_path / "j.jsonl")
    b = new_batch()
    f = src / "a.txt"
    f.write_text("1")
    transfer(f, dst, None, "move", "number", j, "duplicates", note=batch_note(b))
    f.write_text("someone put a new file here")
    done, problems = j.undo_batch(b, "duplicates")
    assert done == 0 and len(problems) == 1
