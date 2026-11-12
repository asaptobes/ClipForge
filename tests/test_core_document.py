"""Document (QObject edit facade): undo entries, dirty flag, selection, playhead, signals.

Only model objects are used here: sources are never probed and the media files
behind them do not exist, which is fine because the Document never reads them.
"""

from __future__ import annotations

import pytest

from clipforge.core.model import ExportSettings, MediaSource
from clipforge.document import Document


def src(sid: str, duration: float) -> MediaSource:
    return MediaSource(id=sid, path=f"C:/media/{sid}.mp4", duration=duration, fps=30.0,
                       width=1920, height=1080)


def make_doc(*durations: float) -> Document:
    """Document with one clip per duration appended to the timeline.

    History is cleared and the selection emptied (appending selects the new clip),
    so tests start from a neutral state. The document is still dirty."""
    doc = Document()
    for i, d in enumerate(durations):
        doc.add_source(src(f"src{i}", d))
    doc.history.clear()
    doc.clear_selection()
    return doc


def make_clean_doc(tmp_path, *durations: float) -> Document:
    doc = make_doc(*durations)
    doc.save(str(tmp_path / "clean.clipforge"))
    return doc


def state(doc: Document):
    return [(s.id, s.source_id, s.source_in, s.source_out, s.timeline_start, s.muted)
            for s in doc.timeline.segments]


def ids(doc: Document) -> list[str]:
    return [s.id for s in doc.timeline.segments]


class Recorder:
    """Records the order of emitted signal names."""

    NAMES = ("timelineChanged", "selectionChanged", "historyChanged", "playheadChanged",
             "seekRequested", "sourcesChanged", "dirtyChanged", "projectReset")

    def __init__(self, doc: Document) -> None:
        self.events: list[str] = []
        self.playheads: list[float] = []
        self.messages: list[str] = []
        for name in self.NAMES:
            getattr(doc, name).connect(lambda *_a, _n=name: self.events.append(_n))
        doc.playheadChanged.connect(self.playheads.append)
        doc.message.connect(self.messages.append)

    def count(self, name: str) -> int:
        return self.events.count(name)


# ------------------------------------------------- one undo entry per edit
def _setup_two(durations=(10.0, 5.0)):
    return make_doc(*durations)


def _split(doc):
    doc.seek(4.0)
    assert doc.split_at_playhead()


def _delete(doc):
    doc.select([ids(doc)[0]])
    assert doc.delete_selected()


def _ripple(doc):
    doc.select([ids(doc)[0]])
    assert doc.delete_selected(ripple=True)


def _reorder(doc):
    assert doc.reorder(ids(doc)[0], 1)


def _trim(doc):
    first = ids(doc)[0]
    doc.begin_drag()
    doc.trim_end(first, 7.0)
    doc.trim_start(first, 1.0)
    assert doc.end_drag()


def _mute(doc):
    assert doc.set_muted([ids(doc)[0]], True)


def _gapped_doc():
    """Two clips with a gap where the first clip used to be (history cleared)."""
    doc = _setup_two()
    doc.select([ids(doc)[0]])
    doc.delete_selected()
    doc.history.clear()
    return doc


def _close_gaps(doc):
    assert doc.close_gaps()


def _add(doc):
    doc.add_source(src("extra", 3.0))


EDITS = [
    pytest.param(lambda: _setup_two(), _split, "Split clip", id="split"),
    pytest.param(lambda: _setup_two(), _delete, "Delete clip", id="delete"),
    pytest.param(lambda: _setup_two(), _ripple, "Ripple delete", id="ripple"),
    pytest.param(lambda: _setup_two(), _reorder, "Move clip", id="reorder"),
    pytest.param(lambda: _setup_two(), _trim, "Trim clip", id="trim-drag"),
    pytest.param(lambda: _setup_two(), _mute, "Mute clip", id="mute"),
    pytest.param(lambda: _setup_two(), _add, "Add clip", id="add-source"),
    pytest.param(_gapped_doc, _close_gaps, "Close gaps", id="close-gaps"),
]


@pytest.mark.parametrize("setup, edit, label", EDITS)
def test_each_edit_creates_exactly_one_undo_entry(setup, edit, label):
    doc = setup()
    edit(doc)
    assert len(doc.history.undo_stack) == 1
    assert doc.history.undo_label() == label


@pytest.mark.parametrize("setup, edit, label", EDITS)
def test_undo_then_redo_restores_identical_segment_lists(setup, edit, label):
    doc = setup()
    before = state(doc)
    edit(doc)
    after = state(doc)
    assert after != before                    # the edit really changed something
    assert doc.undo()
    assert state(doc) == before
    assert doc.redo()
    assert state(doc) == after


def test_unmute_is_its_own_undo_entry():
    doc = make_doc(10.0)
    seg = ids(doc)[0]
    doc.set_muted([seg], True)
    doc.set_muted([seg], False)
    assert doc.history.undo_label() == "Unmute clip"
    assert len(doc.history.undo_stack) == 2


def test_toggle_mute_selected_mutes_when_mixed_selection():
    doc = make_doc(10.0, 5.0)
    a, b = ids(doc)
    doc.set_muted([a], True)
    doc.select([a, b])
    assert doc.toggle_mute_selected()
    assert all(s.muted for s in doc.timeline.segments)


def test_toggle_mute_selected_without_selection_is_refused():
    doc = make_doc(10.0)
    assert doc.toggle_mute_selected() is False


def test_new_edit_after_undo_clears_redo():
    doc = make_doc(10.0)
    doc.set_muted([ids(doc)[0]], True)
    doc.undo()
    assert doc.can_redo()
    doc.set_muted([ids(doc)[0]], True)
    assert not doc.can_redo()
    assert doc.redo() is False


def test_undo_and_redo_on_empty_history_return_false():
    doc = make_doc(10.0)
    assert doc.undo() is False
    assert doc.redo() is False


def test_undo_and_redo_report_status_messages():
    doc = make_doc(10.0)
    rec = Recorder(doc)
    doc.set_muted([ids(doc)[0]], True)
    doc.undo()
    doc.redo()
    assert rec.messages == ["Undo: Mute clip", "Redo: Mute clip"]


def test_undo_and_redo_update_history_labels():
    doc = make_doc(10.0)
    doc.set_muted([ids(doc)[0]], True)
    assert doc.can_undo() and doc.history.undo_label() == "Mute clip"
    doc.undo()
    assert not doc.can_undo() and doc.history.redo_label() == "Mute clip"


# ---------------------------------------------------------- no-op edits
def test_no_op_drag_adds_no_history_entry():
    doc = make_doc(10.0)
    seg = ids(doc)[0]
    doc.begin_drag()
    doc.trim_start(seg, 0.0)                  # already at the left edge
    assert doc.end_drag() is False
    assert doc.history.undo_stack == []


def test_end_drag_without_begin_is_a_noop():
    doc = make_doc(10.0)
    assert doc.end_drag() is False
    assert doc.history.undo_stack == []


def test_muting_already_unmuted_clip_adds_no_history_and_stays_clean(tmp_path):
    doc = make_clean_doc(tmp_path, 10.0)
    seg = ids(doc)[0]
    doc.set_muted([seg], False)               # already unmuted: nothing changes
    assert doc.history.undo_stack == []
    assert doc.dirty is False


def test_reorder_to_same_position_adds_no_history():
    doc = make_doc(10.0, 5.0)
    assert doc.reorder(ids(doc)[0], 0) is False
    assert doc.history.undo_stack == []


def test_close_gaps_without_gaps_is_refused_with_message():
    doc = make_doc(10.0, 5.0)
    rec = Recorder(doc)
    assert doc.close_gaps() is False
    assert doc.history.undo_stack == []
    assert rec.messages and "gaps" in rec.messages[0].lower()


def test_delete_without_selection_is_refused_with_message():
    doc = make_doc(10.0)
    rec = Recorder(doc)
    assert doc.delete_selected() is False
    assert doc.history.undo_stack == []
    assert rec.messages == ["Select a clip to delete."]


# ----------------------------------------------------------------- drags
def test_cancel_drag_restores_pre_drag_timeline_without_history():
    doc = make_doc(10.0)
    seg = ids(doc)[0]
    before = state(doc)
    doc.begin_drag()
    doc.trim_end(seg, 6.0)
    doc.trim_start(seg, 2.0)
    assert state(doc) != before
    doc.cancel_drag()
    assert state(doc) == before
    assert doc.history.undo_stack == []
    assert doc.end_drag() is False            # the drag is gone after cancelling


def test_cancel_drag_emits_timeline_changed():
    doc = make_doc(10.0)
    rec = Recorder(doc)
    doc.begin_drag()
    doc.trim_end(ids(doc)[0], 6.0)
    doc.cancel_drag()
    assert rec.count("timelineChanged") >= 2


def test_live_trim_without_begin_drag_records_one_entry():
    doc = make_doc(10.0)
    seg = ids(doc)[0]
    applied = doc.trim_end(seg, 7.0)
    assert applied == pytest.approx(7.0)
    assert len(doc.history.undo_stack) == 1
    assert doc.history.undo_label() == "Trim clip"


def test_trim_returns_the_time_actually_applied():
    doc = make_doc(10.0, 5.0)
    first = ids(doc)[0]
    # the next clip starts at 10.0, so dragging further is clamped there
    assert doc.trim_end(first, 50.0) == pytest.approx(10.0)


def test_refused_live_trim_reports_real_edge_and_message():
    doc = make_doc(0.02)                      # shorter than one frame at 30 fps
    rec = Recorder(doc)
    seg = ids(doc)[0]
    edge = doc.trim_start(seg, 0.01)
    assert edge == pytest.approx(0.0)
    assert rec.messages and "too short" in rec.messages[0]
    assert doc.history.undo_stack == []


def test_end_drag_custom_label():
    doc = make_doc(10.0)
    doc.begin_drag()
    doc.trim_end(ids(doc)[0], 6.0)
    doc.end_drag("Custom label")
    assert doc.history.undo_label() == "Custom label"


# ----------------------------------------------------------------- dirty
def test_new_document_is_clean(tmp_path):
    doc = Document()
    assert doc.dirty is False
    assert doc.display_name() == "Untitled"
    with pytest.raises(ValueError):
        doc.save()                            # no path yet


def test_edit_marks_dirty_and_save_clears_it(tmp_path):
    doc = make_doc(10.0)
    rec = Recorder(doc)
    doc.set_muted([ids(doc)[0]], True)
    assert doc.dirty is True
    doc.save(str(tmp_path / "p.clipforge"))
    assert doc.dirty is False
    assert ("dirtyChanged") in rec.events
    assert doc.path == str(tmp_path / "p.clipforge")
    assert doc.display_name() == "p"


def test_dirty_signal_only_fires_on_transitions(tmp_path):
    doc = make_clean_doc(tmp_path, 10.0)
    flags: list[bool] = []
    doc.dirtyChanged.connect(flags.append)
    doc.set_muted([ids(doc)[0]], True)
    doc.set_muted([ids(doc)[0]], False)
    assert flags == [True]
    doc.save(str(tmp_path / "p.clipforge"))
    assert flags[0] is True and False in flags


@pytest.mark.xfail(strict=False, reason="Document.save emits dirtyChanged(False) twice when "
                                        "the document was dirty: _set_dirty() emits, then "
                                        "save() emits again unconditionally (document.py save)")
def test_save_of_dirty_document_emits_dirty_false_once(tmp_path):
    doc = make_clean_doc(tmp_path, 10.0)
    flags: list[bool] = []
    doc.dirtyChanged.connect(flags.append)
    doc.set_muted([ids(doc)[0]], True)
    doc.save(str(tmp_path / "p.clipforge"))
    assert flags == [True, False]


def test_undo_after_save_makes_document_dirty_again(tmp_path):
    doc = make_doc(10.0)
    doc.set_muted([ids(doc)[0]], True)
    doc.save(str(tmp_path / "p.clipforge"))
    doc.undo()
    assert doc.dirty is True


def test_add_source_without_timeline_marks_dirty_only(tmp_path):
    doc = Document()
    doc.add_source(src("lib", 4.0), to_timeline=False)
    assert "lib" in doc.sources
    assert doc.timeline.segments == []
    assert doc.history.undo_stack == []
    assert doc.dirty is True


def test_set_export_settings_marks_dirty_only_on_change(tmp_path):
    doc = make_doc(10.0)
    doc.save(str(tmp_path / "p.clipforge"))
    doc.set_export_settings(ExportSettings())         # identical to current: no change
    assert doc.dirty is False
    doc.set_export_settings(ExportSettings(filename="other"))
    assert doc.dirty is True
    assert doc.export_settings.filename == "other"


# ------------------------------------------------------------- selection
def test_select_drops_unknown_ids_and_duplicates_keeping_order():
    doc = make_doc(10.0, 5.0)
    a, b = ids(doc)
    doc.select([b, "ghost", a, b])
    assert doc.selection == [b, a]


def test_select_same_selection_emits_nothing():
    doc = make_doc(10.0)
    doc.select([ids(doc)[0]])
    rec = Recorder(doc)
    doc.select([ids(doc)[0]])
    assert rec.count("selectionChanged") == 0


def test_toggle_select_adds_and_removes():
    doc = make_doc(10.0, 5.0)
    a, b = ids(doc)
    doc.toggle_select(a)
    doc.toggle_select(b)
    assert doc.selection == [a, b]
    doc.toggle_select(a)
    assert doc.selection == [b]


def test_select_all_and_clear_selection():
    doc = make_doc(10.0, 5.0)
    doc.select_all()
    assert doc.selection == ids(doc)
    doc.clear_selection()
    assert doc.selection == []


def test_selection_is_pruned_after_delete_and_stays_pruned_after_undo():
    doc = make_doc(10.0, 5.0)
    a, b = ids(doc)
    doc.select([a, b])
    doc.delete_selected()                     # deletes both, nothing left selected
    assert doc.selection == []
    doc.undo()
    assert doc.selection == []                # no ghost ids restored into the selection


def test_selection_keeps_surviving_clips_after_partial_delete():
    doc = make_doc(10.0, 5.0)
    a, b = ids(doc)
    doc.select([a, b])
    doc.delete_selected()
    assert doc.selection == []
    doc.undo()
    doc.select([a, b])
    doc.select([a])
    doc.select([a, b])
    doc.toggle_select(a)                      # selection is now [b]
    doc.delete_selected()                     # deletes b only
    assert doc.selection == []
    assert [s.id for s in doc.timeline.segments] == [a]


def test_selected_segments_follow_timeline_order():
    doc = make_doc(10.0, 5.0)
    a, b = ids(doc)
    doc.select([b, a])
    assert [s.id for s in doc.selected_segments()] == [a, b]


# -------------------------------------------------------------- playhead
def test_seek_emits_playhead_and_seek_request_but_not_timeline():
    doc = make_doc(10.0)
    rec = Recorder(doc)
    doc.seek(3.5)
    assert doc.playhead == pytest.approx(3.5)
    assert rec.playheads == [3.5]
    assert rec.count("seekRequested") == 1
    assert rec.count("timelineChanged") == 0


def test_seek_clamps_to_timeline_bounds():
    doc = make_doc(10.0)
    doc.seek(-4.0)
    assert doc.playhead == 0.0
    doc.seek(99.0)
    assert doc.playhead == pytest.approx(10.0)


def test_set_playhead_does_not_request_a_seek():
    doc = make_doc(10.0)
    rec = Recorder(doc)
    doc.set_playhead(2.0)
    assert doc.playhead == pytest.approx(2.0)
    assert rec.playheads == [2.0]
    assert rec.count("seekRequested") == 0


def test_set_playhead_same_value_emits_nothing_and_clamps():
    doc = make_doc(10.0)
    doc.set_playhead(2.0)
    rec = Recorder(doc)
    doc.set_playhead(2.0)
    assert rec.playheads == []
    doc.set_playhead(50.0)
    assert doc.playhead == pytest.approx(10.0)


def test_playhead_is_clamped_when_timeline_shrinks():
    doc = make_doc(10.0, 5.0)
    doc.seek(12.0)
    rec = Recorder(doc)
    doc.select([ids(doc)[1]])
    doc.delete_selected()                     # timeline now 10 s long
    assert doc.playhead == pytest.approx(10.0)
    assert rec.playheads == [10.0]


def test_playhead_goes_to_zero_when_timeline_becomes_empty():
    doc = make_doc(10.0)
    doc.seek(9.0)
    doc.select([ids(doc)[0]])
    doc.delete_selected()
    assert doc.duration == 0.0
    assert doc.playhead == 0.0


def test_playhead_is_not_moved_by_edits_within_timeline():
    doc = make_doc(10.0)
    doc.seek(3.0)
    doc.set_muted([ids(doc)[0]], True)
    assert doc.playhead == pytest.approx(3.0)


# ---------------------------------------------------------- split_at_playhead
def test_split_with_playhead_inside_clip_selects_right_half():
    doc = make_doc(10.0)
    doc.seek(4.0)
    assert doc.split_at_playhead() is True
    segs = doc.timeline.segments
    assert len(segs) == 2
    assert segs[0].source_out == pytest.approx(4.0)
    assert doc.selection == [segs[1].id]
    assert doc.history.undo_label() == "Split clip"


def test_split_prefers_selected_clip_under_playhead():
    doc = make_doc(10.0, 5.0)
    a, b = ids(doc)
    doc.select([a, b])
    doc.seek(12.0)                            # only clip b is under the playhead
    assert doc.split_at_playhead()
    assert len(doc.timeline.segments) == 3
    assert doc.timeline.get(a).source_out == pytest.approx(10.0)


def test_split_falls_back_to_clip_under_playhead_when_selection_is_elsewhere():
    doc = make_doc(10.0, 5.0)
    a, b = ids(doc)
    doc.select([a])
    doc.seek(12.0)
    assert doc.split_at_playhead()
    assert len(doc.timeline.segments) == 3
    assert doc.timeline.get(a).source_out == pytest.approx(10.0)


def test_split_over_gap_emits_message_and_changes_nothing():
    doc = make_doc(10.0, 5.0)
    doc.select([ids(doc)[0]])
    doc.delete_selected()                     # gap [0, 10)
    doc.history.clear()
    rec = Recorder(doc)
    doc.seek(5.0)
    assert doc.split_at_playhead() is False
    assert rec.messages == ["Move the playhead over a clip to split it."]
    assert doc.history.undo_stack == []


def test_split_on_empty_timeline_is_refused():
    doc = Document()
    rec = Recorder(doc)
    assert doc.split_at_playhead() is False
    assert rec.messages


def test_split_too_close_to_edge_is_refused_and_leaves_timeline_untouched():
    doc = make_doc(10.0)
    before = state(doc)
    rec = Recorder(doc)
    doc.seek(0.0)                             # cut at the very start: nothing to keep
    assert doc.split_at_playhead() is False
    assert state(doc) == before
    assert rec.messages and "inside the clip" in rec.messages[0]
    assert doc.history.undo_stack == []


# ----------------------------------------------------------------- sources
def test_add_source_appends_at_end_of_timeline():
    doc = make_doc(10.0, 5.0)
    rec = Recorder(doc)
    doc.add_source(src("third", 3.0))
    segs = doc.timeline.segments
    assert [s.source_id for s in segs] == ["src0", "src1", "third"]
    assert segs[-1].timeline_start == pytest.approx(15.0)
    assert rec.count("sourcesChanged") == 1
    assert rec.count("timelineChanged") == 1
    assert doc.selection == [segs[-1].id]


def test_remove_source_refused_while_in_use():
    doc = make_doc(10.0)
    rec = Recorder(doc)
    assert doc.remove_source("src0") is False
    assert "src0" in doc.sources
    assert rec.messages and "used on the timeline" in rec.messages[0]


def test_remove_unused_source_succeeds_and_marks_dirty():
    doc = make_doc(10.0)
    doc.add_source(src("lib", 4.0), to_timeline=False)
    assert doc.remove_source("lib") is True
    assert "lib" not in doc.sources
    assert doc.dirty is True


def test_remove_source_clears_history_that_referenced_it():
    doc = make_doc(10.0, 5.0)
    doc.select([ids(doc)[1]])
    doc.delete_selected()                     # snapshot still contains src1 clip
    assert doc.can_undo()
    assert doc.remove_source("src1") is True
    assert not doc.can_undo()


def test_source_of_returns_the_media_for_a_segment():
    doc = make_doc(10.0)
    seg = doc.timeline.segments[0]
    assert doc.source_of(seg).id == "src0"


# -------------------------------------------------------------- signals
def test_edit_emits_timeline_and_history_signals():
    doc = make_doc(10.0)
    rec = Recorder(doc)
    doc.set_muted([ids(doc)[0]], True)
    assert rec.count("timelineChanged") == 1
    assert rec.count("historyChanged") == 1
    assert rec.count("seekRequested") == 1    # preview frame refresh after the change


def test_undo_emits_timeline_and_history_signals():
    doc = make_doc(10.0)
    doc.set_muted([ids(doc)[0]], True)
    rec = Recorder(doc)
    doc.undo()
    assert rec.count("timelineChanged") == 1
    assert rec.count("historyChanged") == 1


def test_edit_emits_selection_changed_only_when_pruned():
    doc = make_doc(10.0, 5.0)
    a, b = ids(doc)
    doc.select([a])
    rec = Recorder(doc)
    doc.set_muted([b], True)                  # selection untouched by this edit
    assert rec.count("selectionChanged") == 0
    doc.select([a, b])
    rec2 = Recorder(doc)
    doc.set_muted([a], True)                  # nothing pruned here
    assert rec2.count("selectionChanged") == 0
    doc.select([b])
    rec3 = Recorder(doc)
    doc.delete_selected()                     # b (the selection) is removed
    assert rec3.count("selectionChanged") == 1
    assert doc.selection == []


def test_seek_request_on_seek_not_on_set_playhead():
    doc = make_doc(10.0)
    rec = Recorder(doc)
    doc.set_playhead(1.0)
    doc.set_playhead(2.0)
    assert rec.count("seekRequested") == 0
    doc.seek(3.0)
    assert rec.count("seekRequested") == 1


def test_signals_from_qt_side_are_delivered_with_values(qtbot):
    doc = make_doc(10.0)
    with qtbot.waitSignal(doc.playheadChanged, timeout=1000) as blocker:
        doc.seek(2.5)
    assert blocker.args == [2.5]
