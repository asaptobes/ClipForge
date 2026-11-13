"""Layered video tracks: track management, per-track rules, transforms, undo, project files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipforge.core.history import History, HistoryEntry
from clipforge.core.model import (MAX_VIDEO_TRACKS, POS_LIMIT, SCALE_MAX, SCALE_MIN, EditError,
                                  MediaSource, Segment, Timeline, TimelineSnapshot)
from clipforge.core.project import FORMAT_NAME, FORMAT_VERSION, Project, ProjectError
from clipforge.document import Document


def src(sid: str = "a", duration: float = 10.0, fps: float = 30.0, has_audio: bool = True) -> MediaSource:
    return MediaSource(id=sid, path=f"C:/media/{sid}.mp4", duration=duration, fps=fps,
                       width=1920, height=1080, has_audio=has_audio)


def layered() -> Timeline:
    """V1: a 0-10, b 10-15.   V2: c 2-6, d 8-11.   V3: e 3-5."""
    tl = Timeline()
    tl.append_source(src("a", 10.0))
    tl.append_source(src("b", 5.0))
    tl.add_track()
    tl.add_track()
    tl.place_source(src("c", 4.0), 1, 2.0)
    tl.place_source(src("d", 3.0), 1, 8.0)
    tl.place_source(src("e", 2.0), 2, 3.0)
    tl.validate()
    return tl


def by_source(tl: Timeline, sid: str) -> Segment:
    return next(s for s in tl.all_segments() if s.source_id == sid)


def layout(tl: Timeline):
    return [[(s.source_id, round(s.timeline_start, 6), round(s.timeline_end, 6)) for s in lane]
            for lane in tl.tracks]


class Signals:
    def __init__(self, doc: Document) -> None:
        self.log: list[str] = []
        self.messages: list[str] = []
        doc.tracksChanged.connect(lambda: self.log.append("tracks"))
        doc.timelineChanged.connect(lambda: self.log.append("timeline"))
        doc.message.connect(self.messages.append)


def layered_doc() -> Document:
    doc = Document()
    doc.add_source(src("a", 10.0))
    doc.add_source(src("b", 5.0))
    assert doc.add_track() == 1
    doc.add_source(src("c", 4.0), track=1, at=2.0)
    return doc


# ---------------------------------------------------------------- segment fields
def test_segment_defaults_are_base_track_centred_opaque():
    s = Segment("s", "a", 0.0, 1.0, 0.0)
    assert (s.track, s.layer, s.pos_x, s.pos_y, s.scale, s.opacity) == ("video", 0, 0.0, 0.0, 1.0, 1.0)
    assert not s.is_overlay and not s.is_audio


def test_segment_dict_round_trip_keeps_layer_and_transform():
    s = Segment("s", "a", 1.0, 3.0, 4.0, layer=2, pos_x=-0.25, pos_y=0.4, scale=0.3, opacity=0.6)
    back = Segment.from_dict(json.loads(json.dumps(s.to_dict())))
    assert back == s and back.is_overlay
    assert s.copy() == s and s.copy() is not s


def test_segment_from_old_dict_defaults_to_v1():
    old = {"id": "s", "source_id": "a", "source_in": 0, "source_out": 2, "timeline_start": 1,
           "muted": True, "track": "video", "crop_x": 0.2, "crop_x_end": None}
    s = Segment.from_dict(old)
    assert (s.layer, s.pos_x, s.pos_y, s.scale, s.opacity) == (0, 0.0, 0.0, 1.0, 1.0)
    assert s.muted and s.crop_x == 0.2


def test_segment_from_dict_clamps_bad_values():
    d = {"id": "s", "source_id": "a", "source_in": 0, "source_out": 2, "timeline_start": 0,
         "layer": "x", "pos_x": 9, "pos_y": float("nan"), "scale": 0, "opacity": 7}
    s = Segment.from_dict(d)
    assert (s.layer, s.pos_x, s.pos_y, s.scale, s.opacity) == (0, POS_LIMIT, 0.0, SCALE_MIN, 1.0)
    assert Segment.from_dict({**d, "layer": 10 ** 9}).layer == MAX_VIDEO_TRACKS - 1
    assert Segment.from_dict({**d, "layer": 3, "track": "audio"}).layer == 0


# ------------------------------------------------------------------ track list
def test_new_timeline_has_one_base_track():
    tl = Timeline()
    assert tl.track_count == 1 and tl.tracks == [[]] and tl.segments is tl.tracks[0]
    assert tl.is_empty()


def test_segments_is_the_base_track_only():
    tl = layered()
    assert [s.source_id for s in tl.segments] == ["a", "b"]
    assert [s.source_id for s in tl.video_segments()] == ["a", "b", "c", "d", "e"]
    assert [s.source_id for s in tl.all_segments()] == ["a", "b", "c", "d", "e"]
    assert [s.layer for s in tl.all_segments()] == [0, 0, 1, 1, 2]
    assert tl.track(1) is tl.tracks[1]
    assert tl.lane_of(by_source(tl, "e").id) is tl.tracks[2]
    assert tl.index_of(by_source(tl, "d").id) == 1
    assert by_source(tl, "e").is_overlay and not by_source(tl, "a").is_overlay


def test_constructor_distributes_by_layer_and_grows_track_count():
    segs = [Segment("x", "a", 0, 2, 0), Segment("y", "a", 0, 2, 1, layer=2),
            Segment("z", "a", 0, 2, 0, track="audio")]
    tl = Timeline({"a": src("a")}, segs)
    assert tl.track_count == 3
    assert [[s.id for s in lane] for lane in tl.tracks] == [["x"], [], ["y"]]
    assert [s.id for s in tl.audio] == ["z"]
    assert Timeline(track_count=4).track_count == 4


def test_add_track_on_top_and_in_the_middle():
    tl = layered()
    assert tl.add_track() == 3 and tl.track_count == 4 and tl.tracks[3] == []
    assert tl.add_track(1) == 1
    assert [len(lane) for lane in tl.tracks] == [2, 0, 2, 1, 0]
    assert by_source(tl, "c").layer == 2 and by_source(tl, "e").layer == 3
    tl.validate()


@pytest.mark.parametrize("index", [0, -1, 9, True, 1.0])
def test_add_track_rejects_bad_index(index):
    tl = layered()
    with pytest.raises(EditError):
        tl.add_track(index)
    assert tl.track_count == 3


def test_add_track_stops_at_the_limit():
    tl = Timeline()
    for _ in range(MAX_VIDEO_TRACKS - 1):
        tl.add_track()
    with pytest.raises(EditError):
        tl.add_track()
    assert tl.track_count == MAX_VIDEO_TRACKS


def test_remove_empty_track_shifts_higher_tracks_down():
    tl = layered()
    tl.add_track(1)
    tl.remove_track(1)
    assert layout(tl) == layout(layered())
    assert by_source(tl, "e").layer == 2


def test_remove_track_rules():
    tl = layered()
    before = layout(tl)
    with pytest.raises(EditError):
        tl.remove_track(0)                                   # never the base track
    with pytest.raises(EditError):
        tl.remove_track(1)                                   # still has clips
    with pytest.raises(EditError):
        tl.remove_track(5)
    with pytest.raises(EditError):
        tl.remove_track(1, move_to=1)
    with pytest.raises(EditError):
        tl.remove_track(1, move_to=0)                        # would overlap a and b
    with pytest.raises(EditError):
        tl.remove_track(2, move_to=1)                        # e 3-5 overlaps c 2-6
    assert layout(tl) == before
    tl.remove_track(1, delete_clips=True)
    assert layout(tl) == [[("a", 0, 10), ("b", 10, 15)], [("e", 3, 5)]]
    assert by_source(tl, "e").layer == 1
    tl.validate()


def test_remove_track_moving_clips_to_another_track():
    tl = layered()
    tl.move_to(by_source(tl, "e").id, 6.0, track=2)          # e 6-8 fits between c and d
    tl.remove_track(2, move_to=1)
    assert layout(tl) == [[("a", 0, 10), ("b", 10, 15)], [("c", 2, 6), ("e", 6, 8), ("d", 8, 11)]]
    assert by_source(tl, "e").layer == 1
    tl.validate()


# --------------------------------------------------------------------- queries
def test_duration_spans_every_track():
    tl = Timeline()
    tl.append_source(src("a", 4.0))
    tl.add_track()
    tl.place_source(src("c", 5.0), 1, 20.0)
    assert tl.duration == 25.0 and tl.video_duration == 25.0
    assert tl.gaps() == [] and tl.gaps(1) == [(0.0, 20.0)]


def test_segment_at_per_track_and_segments_at_bottom_to_top():
    tl = layered()
    a, b, c, d, e = (by_source(tl, k) for k in "abcde")
    assert tl.segment_at(4.0) is a
    assert tl.segment_at(4.0, 1) is c
    assert tl.segment_at(4.0, 2) is e
    assert tl.segment_at(7.0, 1) is None                     # gap on V2
    assert tl.segments_at(4.0) == [a, c, e]
    assert tl.segments_at(7.0) == [a]
    assert tl.segments_at(10.5) == [b, d]
    assert tl.segments_at(99.0) == []
    assert tl.next_segment_after(6.5, 1) is d
    assert tl.next_segment_after(6.5) is b
    assert tl.to_source(4.0) == (a, 4.0)
    with pytest.raises(EditError):
        tl.segment_at(1.0, 7)


def test_segments_at_skips_tracks_with_a_gap():
    tl = Timeline()
    tl.add_track()
    tl.place_source(src("c", 4.0), 1, 2.0)
    assert tl.segment_at(3.0) is None
    assert [s.source_id for s in tl.segments_at(3.0)] == ["c"]


def test_next_start_after_looks_at_every_lane():
    tl = layered()
    assert tl.next_start_after(0.0) == 2.0
    assert tl.next_start_after(2.0) == 3.0
    assert tl.next_start_after(8.0) == 10.0
    assert tl.next_start_after(10.0) is None


def test_snap_points_cover_all_tracks():
    tl = layered()
    assert tl.snap_points() == [0.0, 2.0, 3.0, 5.0, 6.0, 8.0, 10.0, 11.0, 15.0]
    assert 3.0 not in tl.snap_points(exclude=[by_source(tl, "e").id])


def test_fit_start_returns_nearest_free_slot():
    tl = layered()                                           # V2: c 2-6, d 8-11
    assert tl.fit_start(1, 6.0, 2.0) == 6.0                  # exactly fills the gap
    assert tl.fit_start(1, 0.0, 2.0) == 0.0
    assert tl.fit_start(1, 1.0, 2.0) == 0.0                  # pushed left in front of c
    assert tl.fit_start(1, 5.5, 2.0) == 6.0
    assert tl.fit_start(1, 7.0, 2.0) == 6.0
    assert tl.fit_start(1, 9.0, 5.0) == 11.0                 # too long for any gap: after d
    assert tl.fit_start(1, 3.0, 3.0) == 11.0                 # 3 s fits nowhere before the end
    assert tl.fit_start(1, -4.0, 1.0) == 0.0
    assert tl.fit_start(1, 3.0, 2.0, exclude=[by_source(tl, "c").id]) == 3.0
    assert tl.fit_start(2, 3.0, 9.0) == 5.0
    assert tl.fit_start(2, 3.0, 2.0) == 5.0                  # tie between 1.0 and 5.0: later wins


# ------------------------------------------------------------------ operations
def test_place_source_adjusts_instead_of_overlapping():
    tl = layered()
    seg = tl.place_source(src("f", 2.0), 1, 3.0)             # wanted inside c
    assert (seg.layer, seg.timeline_start) == (1, 6.0)
    assert "f" in tl.sources
    tl.validate()
    with pytest.raises(EditError):
        tl.place_source(src("g", 2.0), 6, 0.0)
    assert "g" not in tl.sources


def test_append_source_goes_to_v1_after_everything():
    tl = Timeline()
    tl.add_track()
    tl.place_source(src("c", 5.0), 1, 20.0)
    seg = tl.append_source(src("a", 4.0))
    assert (seg.layer, seg.timeline_start) == (0, 25.0)


def test_move_between_tracks_free_slot_and_adjusted():
    tl = layered()
    e = by_source(tl, "e")
    assert tl.move_to(e.id, 6.0, track=1) == 6.0
    assert e.layer == 1 and tl.tracks[2] == []
    assert [s.source_id for s in tl.tracks[1]] == ["c", "e", "d"]
    assert tl.move_to(e.id, 4.0, track=2) == 4.0             # back up, free placement
    assert e.layer == 2
    assert tl.move_to(e.id, 3.0, track=1) == 6.0             # overlap rejected -> nearest slot
    tl.validate()


def test_move_overlay_clip_down_to_base_track():
    tl = layered()
    e = by_source(tl, "e")
    assert tl.move_to(e.id, 4.0, track=0) == 15.0            # V1 is full until 15
    assert not e.is_overlay and [s.source_id for s in tl.segments] == ["a", "b", "e"]
    tl.validate()


def test_move_with_explicit_own_track_may_jump_over_neighbours():
    tl = layered()
    c = by_source(tl, "c")
    assert tl.move_to(c.id, 12.0, track=1) == 12.0
    assert [s.source_id for s in tl.tracks[1]] == ["d", "c"]
    with pytest.raises(EditError):
        tl.move_to(c.id, 12.0, track=1)                      # already there
    tl.validate()


def test_move_without_track_is_still_clamped_to_neighbours():
    tl = layered()
    c = by_source(tl, "c")
    assert tl.move_to(c.id, 12.0) == 4.0                     # stops at d (8) minus its 4 s
    assert [s.source_id for s in tl.tracks[1]] == ["c", "d"]
    assert layout(tl)[0] == [("a", 0, 10), ("b", 10, 15)]    # other tracks untouched


def test_move_to_track_rejects_audio_clips_and_bad_tracks():
    tl = layered()
    audio = tl.detach_audio([by_source(tl, "a").id])[0]
    before = layout(tl)
    with pytest.raises(EditError):
        tl.move_to(audio.id, 0.0, track=1)
    with pytest.raises(EditError):
        tl.move_to(by_source(tl, "e").id, 0.0, track=3)
    assert layout(tl) == before
    assert tl.move_to(audio.id, 20.0) == 20.0                # sliding on the audio lane still works


def test_split_on_overlay_keeps_layer_and_transform():
    tl = layered()
    c = by_source(tl, "c")
    tl.set_transform([c.id], 0.2, -0.1, 0.4, 0.5)
    c.muted = True
    left, right = tl.split(c.id, 4.0)
    assert [s.source_id for s in tl.tracks[1]] == ["c", "c", "d"]
    for part in (left, right):
        assert (part.layer, part.pos_x, part.pos_y, part.scale, part.opacity, part.muted) == \
               (1, 0.2, -0.1, 0.4, 0.5, True)
    assert (left.timeline_end, right.timeline_start, right.timeline_end) == (4.0, 4.0, 6.0)
    assert layout(tl)[0] == [("a", 0, 10), ("b", 10, 15)]
    tl.validate()


def test_delete_and_ripple_only_touch_the_clips_own_track():
    tl = layered()
    tl.delete([by_source(tl, "c").id], ripple=True)
    assert layout(tl) == [[("a", 0, 10), ("b", 10, 15)], [("d", 4, 7)], [("e", 3, 5)]]
    tl.delete([by_source(tl, "a").id, by_source(tl, "e").id])
    assert layout(tl) == [[("b", 10, 15)], [("d", 4, 7)], []]
    assert tl.track_count == 3                               # empty tracks stay
    with pytest.raises(EditError):
        tl.delete(["nope"])


def test_trim_is_limited_by_neighbours_on_the_same_track_only():
    tl = layered()
    c, d, e = (by_source(tl, k) for k in "cde")
    tl.trim_end(c.id, 3.0)                                   # c is now 2-3 (source 0-1 of 4)
    assert tl.trim_end(c.id, 100.0) == pytest.approx(6.0)    # back out to the end of its source
    tl.trim_start(d.id, 9.0)                                 # d is now 9-11
    assert tl.trim_start(d.id, 0.0) == pytest.approx(8.0)    # cannot go before its source start
    tl.trim_start(e.id, 4.0)
    assert e.timeline_start == pytest.approx(4.0)            # V3 trims freely above V1/V2 clips
    tl.trim_end(c.id, 5.0)
    tl.move_to(d.id, 5.0)
    assert tl.trim_end(c.id, 9.0) == pytest.approx(5.0)      # blocked by d on its own track
    assert layout(tl)[0] == [("a", 0, 10), ("b", 10, 15)]
    tl.validate()


def test_reorder_and_close_gaps_are_per_track():
    tl = layered()
    tl.close_gaps(1)
    assert layout(tl) == [[("a", 0, 10), ("b", 10, 15)], [("c", 0, 4), ("d", 4, 7)], [("e", 3, 5)]]
    with pytest.raises(EditError):
        tl.close_gaps(1)
    with pytest.raises(EditError):
        tl.close_gaps()                                      # V1 never had gaps
    tl.close_gaps(2)
    assert layout(tl)[2] == [("e", 0, 2)]
    tl.reorder(by_source(tl, "d").id, 0)
    assert layout(tl)[1] == [("d", 0, 3), ("c", 3, 7)]
    assert layout(tl)[0] == [("a", 0, 10), ("b", 10, 15)]
    tl.validate()


def test_set_muted_works_on_any_track():
    tl = layered()
    tl.set_muted([by_source(tl, "e").id, by_source(tl, "a").id], True)
    assert [s.source_id for s in tl.all_segments() if s.muted] == ["a", "e"]


def test_detach_audio_from_overlay_clip():
    tl = layered()
    c = by_source(tl, "c")
    made = tl.detach_audio([c.id])
    assert len(made) == 1 and made[0].is_audio and made[0].layer == 0
    assert (made[0].timeline_start, made[0].timeline_end) == (2.0, 6.0)
    assert c.muted and tl.audio == made
    tl.validate()


def test_detach_audio_never_overlaps_the_single_audio_lane():
    tl = layered()                                           # a 0-10 and c 2-6 overlap in time
    made = tl.detach_audio([by_source(tl, "a").id, by_source(tl, "c").id, by_source(tl, "b").id])
    assert [(m.source_id, m.timeline_start) for m in made] == [("a", 0.0), ("b", 10.0)]
    assert not by_source(tl, "c").muted                      # kept its own sound
    tl.validate()


def test_set_crop_accepts_video_clips_on_any_track():
    tl = layered()
    tl.set_crop([by_source(tl, "e").id], 0.2)
    assert by_source(tl, "e").crop_x == 0.2


# ------------------------------------------------------------------- transform
def test_set_transform_sets_and_clamps():
    tl = layered()
    c, d = by_source(tl, "c"), by_source(tl, "d")
    tl.set_transform([c.id, d.id], x=0.25, y=-0.3, scale=0.5, opacity=0.4)
    for s in (c, d):
        assert (s.pos_x, s.pos_y, s.scale, s.opacity) == (0.25, -0.3, 0.5, 0.4)
    tl.set_transform([c.id], x=5, y=-5, scale=100, opacity=-1)
    assert (c.pos_x, c.pos_y, c.scale, c.opacity) == (POS_LIMIT, -POS_LIMIT, SCALE_MAX, 0.0)
    tl.set_transform([c.id], scale=0)
    assert c.scale == SCALE_MIN
    assert (d.pos_x, d.scale) == (0.25, 0.5)


def test_set_transform_none_keeps_each_clips_own_value():
    tl = layered()
    c, d = by_source(tl, "c"), by_source(tl, "d")
    tl.set_transform([c.id], x=0.1, scale=0.3)
    tl.set_transform([c.id, d.id], opacity=0.5)
    assert (c.pos_x, c.scale, c.opacity) == (0.1, 0.3, 0.5)
    assert (d.pos_x, d.scale, d.opacity) == (0.0, 1.0, 0.5)


def test_set_transform_refusals_change_nothing():
    tl = layered()
    a, c = by_source(tl, "a"), by_source(tl, "c")
    with pytest.raises(EditError):
        tl.set_transform([a.id], x=0.3)                      # base-track clips are not transformed
    with pytest.raises(EditError):
        tl.set_transform([], x=0.3)
    with pytest.raises(EditError):
        tl.set_transform([c.id])                             # nothing to change
    with pytest.raises(EditError):
        tl.set_transform([c.id], x=0.0, scale=1.0, opacity=1.0)
    tl.set_transform([a.id, c.id], x=0.3)                    # base clip in a mixed selection is skipped
    assert (a.pos_x, c.pos_x) == (0.0, 0.3)


def test_transform_survives_a_trip_to_the_base_track():
    tl = layered()
    e = by_source(tl, "e")
    tl.set_transform([e.id], x=0.2, scale=0.25)
    tl.move_to(e.id, 20.0, track=0)
    tl.move_to(e.id, 3.0, track=2)
    assert (e.pos_x, e.scale, e.layer) == (0.2, 0.25, 2)


# ------------------------------------------------------------ snapshot / validate
def test_snapshot_restore_keeps_tracks_including_empty_ones():
    tl = layered()
    tl.add_track()
    tl.set_transform([by_source(tl, "c").id], x=0.1, opacity=0.5)
    snap = tl.snapshot()
    assert isinstance(snap, TimelineSnapshot) and isinstance(snap, list)
    assert snap.track_count == 4 and len(snap) == 5
    want = layout(tl)
    tl.remove_track(3)
    tl.remove_track(1, delete_clips=True)
    tl.delete([by_source(tl, "a").id])
    tl.restore(snap)
    assert layout(tl) == want and tl.track_count == 4
    assert by_source(tl, "c").opacity == 0.5
    assert tl.snapshot() == snap
    snap[0].timeline_start = 99.0                            # snapshots are copies
    assert tl.segments[0].timeline_start == 0.0


def test_snapshot_equality_includes_track_count():
    tl = layered()
    before = tl.snapshot()
    tl.add_track()
    after = tl.snapshot()
    assert list(before) == list(after)
    assert before != after and not (before == after)
    assert before == list(before)                            # a plain list compares by clips only


def test_restore_from_plain_list_derives_the_track_count():
    tl = layered()
    plain = [s.copy() for s in tl.all_segments()]
    other = Timeline(dict(tl.sources))
    other.restore(plain)
    assert layout(other) == layout(tl)


def test_validate_detects_overlap_within_an_overlay_track():
    tl = layered()
    by_source(tl, "d").timeline_start = 5.0                  # into c (2-6)
    with pytest.raises(EditError):
        tl.validate()


def test_validate_detects_a_clip_on_the_wrong_track():
    tl = layered()
    by_source(tl, "e").layer = 0
    with pytest.raises(EditError):
        tl.validate()


def test_overlap_across_tracks_is_valid():
    tl = Timeline()
    tl.append_source(src("a", 10.0))
    tl.add_track()
    tl.place_source(src("c", 10.0), 1, 0.0)
    tl.validate()
    assert len(tl.segments_at(5.0)) == 2


# --------------------------------------------------------------------- history
def test_history_keeps_track_count_through_undo_redo_and_dicts():
    h = History()
    tl = Timeline()
    before = tl.snapshot()
    tl.add_track()
    h.record("Add track", before)
    assert h.undo_stack[0].segments.track_count == 1
    entry = h.undo(tl.snapshot())
    assert entry.segments.track_count == 1 and h.redo_stack[0].segments.track_count == 2
    h2 = History()
    h2.load_dict(json.loads(json.dumps(h.to_dict())))
    assert h2.redo_stack[0].segments.track_count == 2
    assert HistoryEntry.from_dict({"label": "x", "segments": []}).segments.track_count == 1
    assert HistoryEntry("x", [Segment("s", "a", 0, 1, 0, layer=2)]).segments.track_count == 3


# -------------------------------------------------------------------- document
def test_document_add_and_remove_track_are_undoable():
    doc = Document()
    doc.add_source(src("a", 10.0))
    sig = Signals(doc)
    assert doc.add_track() == 1 and doc.track_count == 2
    assert sig.log[:2] == ["tracks", "timeline"]
    assert doc.history.undo_label() == "Add track" and doc.dirty
    assert doc.undo() and doc.track_count == 1
    assert doc.redo() and doc.track_count == 2
    assert doc.remove_track(1) and doc.track_count == 1
    assert doc.history.undo_label() == "Remove track"
    assert doc.undo() and doc.track_count == 2
    assert sig.log.count("tracks") == 5


def test_document_track_refusals_emit_a_message_and_no_history():
    doc = layered_doc()
    depth = len(doc.history.undo_stack)
    sig = Signals(doc)
    assert doc.remove_track(0) is False
    assert doc.remove_track(1) is False                      # holds clip c
    assert doc.add_track(0) == -1
    assert len(sig.messages) == 3 and sig.log == []
    assert len(doc.history.undo_stack) == depth and doc.track_count == 2
    assert doc.remove_track(1, delete_clips=True)
    assert doc.track_count == 1 and [s.source_id for s in doc.timeline.all_segments()] == ["a", "b"]
    assert doc.undo()
    assert layout(doc.timeline) == [[("a", 0, 10), ("b", 10, 15)], [("c", 2, 6)]]


def test_document_add_source_onto_a_track_at_the_playhead():
    doc = Document()
    doc.add_source(src("a", 10.0))
    doc.add_track()
    doc.seek(3.0)
    doc.add_source(src("c", 4.0), track=1)
    seg = doc.timeline.tracks[1][0]
    assert (seg.timeline_start, seg.layer) == (3.0, 1)
    assert doc.selection == [seg.id] and doc.history.undo_label() == "Add clip"
    doc.add_source(src("d", 2.0), track=1, at=3.5)           # occupied: nearest free slot
    assert layout(doc.timeline)[1] == [("d", 1, 3), ("c", 3, 7)]
    assert doc.undo() and layout(doc.timeline)[1] == [("c", 3, 7)]
    assert "d" in doc.sources


def test_document_add_source_to_a_new_top_track_is_one_undo_step():
    doc = Document()
    doc.add_source(src("a", 10.0))
    depth = len(doc.history.undo_stack)
    doc.add_source(src("c", 4.0), track=1, at=0.0)
    assert doc.track_count == 2 and len(doc.history.undo_stack) == depth + 1
    assert doc.undo() and doc.track_count == 1 and doc.timeline.tracks == [doc.timeline.segments]


def test_document_place_on_timeline_refuses_a_bad_track():
    doc = Document()
    doc.add_source(src("a", 10.0), to_timeline=False)
    sig = Signals(doc)
    assert doc.place_on_timeline("a", 5) is False
    assert sig.messages and doc.timeline.is_empty() and not doc.can_undo()
    assert doc.place_on_timeline("a", 0, at=2.0) is True
    assert doc.timeline.segments[0].timeline_start == 2.0


def test_document_move_clip_between_tracks_and_undo():
    doc = layered_doc()
    c = doc.timeline.tracks[1][0]
    sig = Signals(doc)
    assert doc.move_clip(c.id, 20.0, track=0)
    assert layout(doc.timeline) == [[("a", 0, 10), ("b", 10, 15), ("c", 20, 24)], []]
    assert sig.log == ["timeline"]                           # track count unchanged
    assert doc.move_clip(c.id, 1.0, track=1)
    assert doc.move_clip(c.id, 1.0, track=1) is False        # no change: no undo step
    assert doc.history.undo_label() == "Move clip"
    assert doc.undo() and doc.undo()
    assert layout(doc.timeline) == [[("a", 0, 10), ("b", 10, 15)], [("c", 2, 6)]]


def test_document_move_clip_above_the_top_track_creates_it_in_one_step():
    doc = layered_doc()
    b = doc.timeline.segments[1]
    depth = len(doc.history.undo_stack)
    sig = Signals(doc)
    assert doc.move_clip(b.id, 3.0, track=2)
    assert doc.track_count == 3 and layout(doc.timeline)[2] == [("b", 3, 8)]
    assert sig.log[:2] == ["tracks", "timeline"]
    assert len(doc.history.undo_stack) == depth + 1
    assert doc.undo() and doc.track_count == 2


def test_document_refused_move_does_not_leave_a_new_track_behind():
    doc = layered_doc()
    assert doc.detach_audio()
    audio = doc.timeline.audio[0]
    depth = len(doc.history.undo_stack)
    assert doc.move_clip(audio.id, 0.0, track=2) is False
    assert doc.move_clip(audio.id, 0.0, track=1) is False
    assert doc.move_clip(doc.timeline.segments[0].id, 0.0, track=7) is False
    assert doc.track_count == 2 and len(doc.history.undo_stack) == depth


def test_document_set_transform_is_one_undo_step():
    doc = layered_doc()
    c = doc.timeline.tracks[1][0]
    assert doc.set_transform([c.id], x=0.2, y=0.1, scale=0.5, opacity=0.8)
    assert doc.history.undo_label() == "Transform clip"
    assert doc.undo()
    c = doc.timeline.tracks[1][0]
    assert (c.pos_x, c.pos_y, c.scale, c.opacity) == (0.0, 0.0, 1.0, 1.0)
    assert doc.redo()
    c = doc.timeline.tracks[1][0]
    assert (c.pos_x, c.pos_y, c.scale, c.opacity) == (0.2, 0.1, 0.5, 0.8)


def test_document_set_transform_refusal():
    doc = layered_doc()
    depth = len(doc.history.undo_stack)
    sig = Signals(doc)
    assert doc.set_transform([doc.timeline.segments[0].id], x=0.2) is False
    assert sig.messages and len(doc.history.undo_stack) == depth


def test_document_live_transform_drag_is_a_single_undo_step():
    doc = layered_doc()
    c = doc.timeline.tracks[1][0]
    depth = len(doc.history.undo_stack)
    sig = Signals(doc)
    doc.begin_drag()
    for x in (0.1, 0.2, 0.3):
        assert doc.set_transform([c.id], x=x)
    assert doc.set_transform([c.id], x=0.3) is False         # no movement: silent
    assert sig.log == ["timeline"] * 3 and sig.messages == []
    assert len(doc.history.undo_stack) == depth
    assert doc.end_drag("Transform clip")
    assert len(doc.history.undo_stack) == depth + 1 and doc.history.undo_label() == "Transform clip"
    assert doc.undo() and doc.timeline.tracks[1][0].pos_x == 0.0


def test_document_cancelled_transform_drag_restores():
    doc = layered_doc()
    c = doc.timeline.tracks[1][0]
    depth = len(doc.history.undo_stack)
    doc.begin_drag()
    doc.set_transform([c.id], scale=0.3)
    doc.cancel_drag()
    assert doc.timeline.tracks[1][0].scale == 1.0 and len(doc.history.undo_stack) == depth


def test_document_split_falls_back_to_the_top_overlay_when_v1_has_a_gap():
    doc = Document()
    doc.add_source(src("a", 4.0))
    doc.add_source(src("c", 6.0), track=1, at=10.0)
    doc.clear_selection()
    doc.seek(12.0)
    assert doc.split_at_playhead()
    assert layout(doc.timeline) == [[("a", 0, 4)], [("c", 10, 12), ("c", 12, 16)]]
    doc.clear_selection()
    doc.seek(2.0)
    assert doc.split_at_playhead()                           # V1 wins when it has a clip there
    assert layout(doc.timeline)[0] == [("a", 0, 2), ("a", 2, 4)]


def test_document_split_selected_overlay_clip_leaves_v1_alone():
    doc = layered_doc()
    c = doc.timeline.tracks[1][0]
    doc.select([c.id])
    doc.seek(4.0)
    assert doc.split_at_playhead()
    assert layout(doc.timeline) == [[("a", 0, 10), ("b", 10, 15)], [("c", 2, 4), ("c", 4, 6)]]


def test_document_ripple_delete_on_overlay_track_only():
    doc = layered_doc()
    doc.add_source(src("d", 3.0), track=1, at=8.0)
    doc.select([doc.timeline.tracks[1][0].id])
    assert doc.delete_selected(ripple=True)
    assert layout(doc.timeline) == [[("a", 0, 10), ("b", 10, 15)], [("d", 4, 7)]]
    assert doc.undo()
    assert layout(doc.timeline)[1] == [("c", 2, 6), ("d", 8, 11)]


def test_document_close_gaps_on_a_chosen_track():
    doc = layered_doc()
    assert doc.close_gaps(1)
    assert layout(doc.timeline) == [[("a", 0, 10), ("b", 10, 15)], [("c", 0, 4)]]
    assert doc.close_gaps() is False


def test_document_select_all_and_duration_include_overlays():
    doc = layered_doc()
    doc.add_source(src("d", 3.0), track=1, at=30.0)
    assert doc.duration == 33.0
    doc.select_all()
    assert len(doc.selection) == 4
    doc.seek(31.0)
    assert doc.playhead == 31.0


# --------------------------------------------------------------------- project
def media(folder: Path, sid: str, duration: float) -> MediaSource:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.mp4"
    path.write_bytes(b"not really a video")
    return MediaSource(id=sid, path=str(path.resolve()), duration=duration, fps=30.0,
                       width=1920, height=1080)


def full_state(doc: Document):
    return [s.to_dict() for s in doc.timeline.all_segments()]


def test_project_round_trip_with_tracks_transforms_and_history(tmp_path):
    doc = Document()
    doc.add_source(media(tmp_path, "a", 10.0))
    doc.add_source(media(tmp_path, "c", 4.0), track=1, at=2.0)
    doc.add_source(media(tmp_path, "e", 2.0), track=2, at=3.0)
    doc.set_transform([doc.timeline.tracks[1][0].id], x=0.25, y=-0.2, scale=0.4, opacity=0.7)
    doc.set_muted([doc.timeline.tracks[2][0].id], True)
    doc.add_track()                                          # an empty top track
    path = str(tmp_path / "p.clipforge")
    doc.save(path)

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    assert raw["version"] == FORMAT_VERSION == 2 and raw["video_tracks"] == 4
    assert [s["layer"] for s in raw["segments"]] == [0, 1, 2]

    doc2 = Document()
    sig = Signals(doc2)
    assert doc2.load(path) == []
    assert sig.log[:2] == ["tracks", "timeline"]
    assert doc2.track_count == 4 and full_state(doc2) == full_state(doc)
    c = doc2.timeline.tracks[1][0]
    assert (c.pos_x, c.pos_y, c.scale, c.opacity) == (0.25, -0.2, 0.4, 0.7)
    assert doc2.undo() and doc2.track_count == 3             # "Add track" survived the save
    assert doc2.undo() and not doc2.timeline.tracks[2][0].muted
    assert doc2.undo() and doc2.timeline.tracks[1][0].scale == 1.0
    assert doc2.undo() and doc2.track_count == 2             # clip e and its track are gone
    assert doc2.redo() and doc2.track_count == 3


def test_old_single_track_project_loads_as_v1(tmp_path):
    a = media(tmp_path, "a", 10.0)
    old = {
        "format": FORMAT_NAME, "version": 1,
        "sources": [a.to_dict()],
        "segments": [
            {"id": "s1", "source_id": "a", "source_in": 0.0, "source_out": 4.0, "timeline_start": 0.0,
             "muted": True, "crop_x": 0.3, "crop_x_end": None, "track": "video"},
            {"id": "s2", "source_id": "a", "source_in": 4.0, "source_out": 10.0, "timeline_start": 5.0,
             "muted": False, "crop_x": 0.5, "crop_x_end": None, "track": "video"},
            {"id": "s3", "source_id": "a", "source_in": 0.0, "source_out": 4.0, "timeline_start": 0.0,
             "muted": False, "crop_x": 0.5, "crop_x_end": None, "track": "audio"},
        ],
        "playhead": 2.0,
        "history": {"undo": [{"label": "Split clip", "segments": [
            {"id": "s0", "source_id": "a", "source_in": 0.0, "source_out": 10.0, "timeline_start": 0.0,
             "muted": False, "track": "video"}]}], "redo": []},
    }
    path = tmp_path / "old.clipforge"
    path.write_text(json.dumps(old), encoding="utf-8")
    doc = Document()
    assert doc.load(str(path)) == []
    tl = doc.timeline
    assert tl.track_count == 1 and [s.id for s in tl.segments] == ["s1", "s2"]
    assert [s.id for s in tl.audio] == ["s3"]
    for s in tl.all_segments():
        assert (s.layer, s.pos_x, s.pos_y, s.scale, s.opacity) == (0, 0.0, 0.0, 1.0, 1.0)
    assert tl.segments[0].muted and tl.segments[0].crop_x == 0.3
    assert doc.undo() and [s.id for s in tl.segments] == ["s0"] and tl.track_count == 1
    doc.save(str(path))                                      # re-saved in the new format
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 2 and raw["video_tracks"] == 1


def test_project_with_overlapping_clips_on_one_overlay_track_is_rejected(tmp_path):
    a = media(tmp_path, "a", 10.0)
    seg = {"id": "s1", "source_id": "a", "source_in": 0.0, "source_out": 4.0, "timeline_start": 0.0,
           "layer": 1}
    data = {"format": FORMAT_NAME, "version": 2, "video_tracks": 2, "sources": [a.to_dict()],
            "segments": [seg, {**seg, "id": "s2", "timeline_start": 2.0}]}
    path = tmp_path / "bad.clipforge"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError):
        Project.load(str(path))
    data["segments"][1]["layer"] = 0                         # same times on different tracks: fine
    path.write_text(json.dumps(data), encoding="utf-8")
    assert Project.load(str(path)).timeline.track_count == 2


def test_project_tolerates_a_bad_track_count(tmp_path):
    a = media(tmp_path, "a", 10.0)
    seg = {"id": "s1", "source_id": "a", "source_in": 0.0, "source_out": 4.0, "timeline_start": 0.0,
           "layer": 2}
    data = {"format": FORMAT_NAME, "version": 2, "video_tracks": 10 ** 9, "sources": [a.to_dict()],
            "segments": [seg]}
    path = tmp_path / "p.clipforge"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert Project.load(str(path)).timeline.track_count == MAX_VIDEO_TRACKS
    data["video_tracks"] = 0                                 # grows to fit the clip on layer 2
    path.write_text(json.dumps(data), encoding="utf-8")
    assert Project.load(str(path)).timeline.track_count == 3
    data["video_tracks"] = "many"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError):
        Project.load(str(path))
