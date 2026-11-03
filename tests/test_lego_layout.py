"""Pure geometry tests for the brick stack:  python tests/test_lego_layout.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utilshub.ui.lego_layout import StackLayout, mix, reorder, shade

L = StackLayout()


def test_slots():
    n = 3
    h, top = L.height, L.pad + L.stud
    assert L.slot_at(top - 50, n) == 0 and L.slot_at(top + 5, n) == 0          # above / top half of brick 0
    assert L.slot_at(top + h - 3, n) == 1                                      # bottom half of brick 0 -> after it
    assert L.slot_at(top + 1.4 * h, n) == 1 and L.slot_at(top + 1.6 * h, n) == 2
    assert L.slot_at(10_000, n) == 3                                           # far below = append
    assert L.slot_at(100, 0) == 0                                              # empty stack
    assert L.index_at(top - 1, n) is None and L.index_at(top, n) == 0
    assert L.index_at(top + 3 * h - 1, n) == 2 and L.index_at(top + 3 * h, n) is None
    # the marker line of slot k is the top edge of brick k, and the plate follows the last brick
    assert L.slot_y(2) == L.rect(2)[1] and L.slot_y(3) == L.plate_rect(3)[1]
    assert L.rect(1)[1] == L.rect(0)[3]                                        # bricks touch: no gap


def test_reorder():
    a = list("ABCD")
    assert reorder(a, 0, 2) == list("BACD")        # drop A between B and C
    assert reorder(a, 0, 4) == list("BCDA")        # to the end
    assert reorder(a, 3, 0) == list("DABC")        # to the front
    assert reorder(a, 2, 2) == a and reorder(a, 2, 3) == a      # dropping on its own edges changes nothing
    assert reorder(a, 9, 0) == a and a == list("ABCD")


def test_colours():
    assert shade("#000000", 0.5) == "#808080" and shade("#ffffff", -0.5) == "#808080"
    assert mix("#000000", "#ffffff", 0.5) == "#808080"
    assert L.with_width(1000).width == 560 and L.with_width(200).width == 300 and L.with_width(500).width == 460


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
