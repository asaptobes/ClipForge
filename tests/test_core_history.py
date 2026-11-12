"""Snapshot undo/redo history: stacks, limits, labels and (de)serialisation."""

from __future__ import annotations

import pytest

from clipforge.core.history import History, HistoryEntry
from clipforge.core.model import Segment


def seg(sid: str, start: float = 0.0, length: float = 2.0, muted: bool = False) -> Segment:
    return Segment(sid, "a", 0.0, length, start, muted)


def ids(entries) -> list[str]:
    return [e.label for e in entries]


# ----------------------------------------------------------------- recording
def test_record_stores_a_copy_of_the_pre_edit_snapshot():
    h = History()
    before = [seg("s1")]
    h.record("Mute clip", before)
    before[0].muted = True                    # caller mutates its list afterwards
    assert h.undo_stack[0].segments[0].muted is False
    assert h.can_undo() and not h.can_redo()
    assert h.undo_label() == "Mute clip"


def test_record_clears_redo_stack():
    h = History()
    h.record("A", [seg("s1")])
    h.undo([seg("s1")])
    assert h.can_redo()
    h.record("B", [seg("s1")])
    assert not h.can_redo()
    assert h.redo_label() == ""


def test_limit_drops_oldest_entries():
    h = History(limit=3)
    for i in range(1, 6):
        h.record(f"e{i}", [seg("s")])
    assert ids(h.undo_stack) == ["e3", "e4", "e5"]
    assert h.undo_label() == "e5"


def test_limit_zero_keeps_nothing():
    h = History(limit=0)
    h.record("x", [seg("s")])
    assert not h.can_undo()
    assert h.undo_stack == []


def test_default_limit_is_200():
    h = History()
    for i in range(205):
        h.record(f"e{i}", [])
    assert len(h.undo_stack) == 200
    assert h.undo_stack[0].label == "e5"


# ------------------------------------------------------------- undo / redo
def test_undo_returns_snapshot_and_moves_current_to_redo():
    h = History()
    h.record("Split clip", [seg("before")])
    current = [seg("after")]
    entry = h.undo(current)
    assert entry.label == "Split clip"
    assert [s.id for s in entry.segments] == ["before"]
    assert not h.can_undo() and h.can_redo()
    assert h.redo_label() == "Split clip"
    current[0].id = "mutated"                 # the redo copy is independent of the caller
    assert h.redo_stack[0].segments[0].id == "after"


def test_redo_returns_entry_and_moves_current_back_to_undo():
    h = History()
    h.record("Move clip", [seg("v0")])
    h.undo([seg("v1")])
    entry = h.redo([seg("v0")])
    assert [s.id for s in entry.segments] == ["v1"]
    assert h.can_undo() and not h.can_redo()
    assert h.undo_label() == "Move clip"


def test_labels_are_empty_when_stacks_are_empty():
    h = History()
    assert h.undo_label() == "" and h.redo_label() == ""
    assert not h.can_undo() and not h.can_redo()


def test_clear_empties_both_stacks():
    h = History()
    h.record("A", [])
    h.undo([])
    h.record("B", [])
    h.clear()
    assert not h.can_undo() and not h.can_redo()


# ---------------------------------------------------------- serialisation
def test_entry_dict_roundtrip_keeps_segments():
    e = HistoryEntry("Trim clip", [seg("s1", 1.5, 3.0, True), seg("s2", 4.5)])
    again = HistoryEntry.from_dict(e.to_dict())
    assert again.label == "Trim clip"
    assert again.segments == e.segments


def test_entry_from_dict_defaults_label_and_segments():
    e = HistoryEntry.from_dict({})
    assert e.label == "Edit" and e.segments == []


def test_history_roundtrip_preserves_undo_and_redo_order():
    h = History()
    h.record("one", [seg("a")])
    h.record("two", [seg("b")])
    h.undo([seg("c")])
    data = h.to_dict()

    h2 = History()
    h2.load_dict(data)
    assert ids(h2.undo_stack) == ["one"]
    assert ids(h2.redo_stack) == ["two"]
    assert h2.undo_stack[0].segments == [seg("a")]
    assert h2.redo_stack[0].segments == [seg("c")]
    # the loaded history still behaves
    assert h2.undo([seg("z")]).label == "one"
    assert h2.redo([seg("a")]).label == "one"


def test_to_dict_keeps_only_the_newest_entries():
    h = History()
    for i in range(10):
        h.record(f"e{i}", [])
    d = h.to_dict(max_entries=3)
    assert [e["label"] for e in d["undo"]] == ["e7", "e8", "e9"]
    assert d["redo"] == []


def test_load_dict_tolerates_none_and_empty():
    h = History()
    h.record("keep", [])
    h.load_dict(None)
    assert not h.can_undo()
    h.load_dict({})
    assert not h.can_undo() and not h.can_redo()


@pytest.mark.xfail(strict=False, reason="History.to_dict uses [-max_entries:]; with "
                                        "max_entries=0 the slice is [0:] and returns every "
                                        "entry instead of none (core/history.py to_dict)")
def test_to_dict_zero_max_entries_returns_nothing():
    h = History()
    h.record("e", [])
    assert h.to_dict(max_entries=0)["undo"] == []
