"""Editing-model tests: split / trim / delete / reorder / time mapping / invariants."""

from __future__ import annotations

import random

import pytest

from clipforge.core.model import (EPS, MIN_SEGMENT_DURATION, EditError, ExportSettings,
                                  MediaSource, Segment, Timeline, new_id)

NTSC = 30000 / 1001


def src(sid: str = "a", duration: float = 10.0, fps: float = 30.0) -> MediaSource:
    return MediaSource(id=sid, path=f"C:/media/{sid}.mp4", duration=duration, fps=fps,
                       width=1920, height=1080)


def make(*specs) -> Timeline:
    """Timeline with one whole-source segment per ``(id, duration, fps)`` spec."""
    tl = Timeline()
    for spec in specs:
        tl.append_source(src(*spec))
    return tl


def state(tl: Timeline):
    return [(s.id, s.source_id, s.source_in, s.source_out, s.timeline_start, s.muted)
            for s in tl.segments]


def nums_of(st) -> list[float]:
    return [v for row in st for v in row[2:5]]


def nums(tl: Timeline) -> list[float]:
    return nums_of(state(tl))


def check(tl: Timeline) -> None:
    """All structural invariants of a healthy timeline."""
    tl.validate()
    starts = [s.timeline_start for s in tl.segments]
    assert starts == sorted(starts)
    assert len({s.id for s in tl.segments}) == len(tl.segments)
    for a, b in zip(tl.segments, tl.segments[1:]):
        assert b.timeline_start >= a.timeline_end - 1e-6
    for s in tl.segments:
        source = tl.sources[s.source_id]
        assert s.duration > 0
        assert s.duration >= source.frame_duration - 1e-6
        assert s.source_in >= -1e-9
        assert s.source_out <= source.duration + 1e-9
        assert s.timeline_start >= -1e-9


def on_grid(value: float, fps: float) -> bool:
    return abs(value * fps - round(value * fps)) < 1e-6


# ------------------------------------------------------------------------ basics
def test_append_source_places_clips_back_to_back():
    tl = make(("a", 10.0, 30.0), ("b", 4.5, 25.0), ("c", 2.0, NTSC))
    assert [s.timeline_start for s in tl.segments] == [0.0, 10.0, 14.5]
    assert tl.duration == pytest.approx(16.5)
    assert tl.gaps() == []
    assert set(tl.sources) == {"a", "b", "c"}
    check(tl)


def test_empty_timeline_queries():
    tl = Timeline()
    assert tl.is_empty() and tl.duration == 0.0
    assert tl.segment_at(0.0) is None
    assert tl.next_segment_after(0.0) is None
    assert tl.to_source(1.0) is None
    assert tl.gaps() == []
    assert tl.snap_points() == [0.0]
    tl.validate()
    with pytest.raises(EditError):
        tl.get("nope")
    with pytest.raises(EditError):
        tl.index_of("nope")
    with pytest.raises(EditError):
        tl.close_gaps()


def test_constructor_copies_and_sorts_segments():
    a = Segment("s1", "a", 0.0, 2.0, 5.0)
    b = Segment("s2", "a", 2.0, 4.0, 0.0)
    tl = Timeline({"a": src()}, [a, b])
    assert [s.id for s in tl.segments] == ["s2", "s1"]
    tl.segments[0].muted = True
    assert not b.muted                      # the caller's objects are not aliased


def test_snapshot_restore_are_deep_copies():
    tl = make(("a", 10.0, 30.0))
    snap = tl.snapshot()
    tl.split(tl.segments[0].id, 4.0)
    assert len(snap) == 1 and snap[0].source_out == 10.0
    tl.restore(snap)
    assert len(tl.segments) == 1
    tl.segments[0].source_out = 3.0
    assert snap[0].source_out == 10.0


def test_new_id_unique():
    assert len({new_id() for _ in range(2000)}) == 2000


def test_media_source_helpers_and_dict_roundtrip():
    s = MediaSource(id="x", path="p", duration=1.0, width=1080, height=1920, fps=0,
                    keyframes=[0.0, 1.0])
    assert s.is_vertical
    assert s.frame_duration == MIN_SEGMENT_DURATION
    d = s.to_dict()
    assert "keyframes" not in d
    d["some_future_field"] = 1
    again = MediaSource.from_dict(d)
    assert again.id == "x" and again.height == 1920 and again.keyframes == []


def test_segment_and_export_settings_dict_roundtrip():
    seg = Segment("s", "a", 1.25, 3.5, 7.0, True)
    assert Segment.from_dict(seg.to_dict()) == seg
    assert Segment.from_dict({"id": "s", "source_id": "a", "source_in": 1, "source_out": 2,
                              "timeline_start": 0}).muted is False
    es = ExportSettings(filename="clip", codec="h265", audio=False)
    d = es.to_dict()
    d["unknown"] = 5
    assert ExportSettings.from_dict(d) == es
    assert ExportSettings.from_dict(None) == ExportSettings()


# ---------------------------------------------------------------------- splitting
def test_split_middle():
    tl = make(("a", 10.0, 30.0))
    seg = tl.segments[0]
    seg.muted = True
    left, right = tl.split(seg.id, 4.0)
    assert left is tl.segments[0] and right is tl.segments[1]
    assert left.id == seg.id and right.id != left.id
    assert left.duration + right.duration == pytest.approx(10.0)
    assert (left.source_in, left.source_out) == (0.0, pytest.approx(4.0))
    assert right.source_in == left.source_out            # contiguous in the source
    assert right.source_out == 10.0
    assert right.timeline_start == left.timeline_end     # contiguous on the timeline
    assert right.muted is True and left.muted is True
    assert right.source_id == left.source_id
    assert tl.duration == pytest.approx(10.0)
    assert tl.gaps() == []
    check(tl)


@pytest.mark.parametrize("fps", [NTSC, 25.0, 23.976, 60.0, 59.94])
def test_split_snaps_to_source_frame_grid(fps):
    tl = make(("a", 20.0, fps))
    seg = tl.segments[0]
    t = 7.0 + 0.4 / fps                                    # 40 % into a frame
    left, right = tl.split(seg.id, t)
    assert on_grid(right.source_in, fps)
    assert right.source_in == pytest.approx(round(t * fps) / fps)
    assert abs(right.source_in - t) <= 0.5 / fps + 1e-9
    assert left.source_out == right.source_in
    assert right.timeline_start == pytest.approx(right.source_in)
    check(tl)


def test_split_25fps_exact_values():
    tl = make(("a", 10.0, 25.0))
    left, right = tl.split(tl.segments[0].id, 3.03)       # nearest 25 fps frame is 3.04
    assert right.source_in == pytest.approx(3.04)
    assert left.duration == pytest.approx(3.04)
    assert right.duration == pytest.approx(6.96)


@pytest.mark.parametrize("fps", [30.0, 25.0, NTSC])
def test_split_refused_at_and_near_edges(fps):
    tl = make(("a", 10.0, fps))
    seg = tl.segments[0]
    frame = 1.0 / fps
    before = state(tl)
    for t in (0.0, 10.0, 0.4 * frame, 10.0 - 0.4 * frame, -1.0, 11.0, 1e-9, 10.0 - 1e-9):
        with pytest.raises(EditError):
            tl.split(seg.id, t)
        assert state(tl) == before
    # exactly one frame inside either edge is fine
    left, right = tl.split(seg.id, frame)
    assert left.duration == pytest.approx(frame)
    # (the source end need not be on the frame grid, e.g. 10 s at 29.97 fps)
    last_cut = (int(10.0 * fps + 1e-6) - 1) / fps
    left2, right2 = tl.split(right.id, last_cut)
    assert frame - 1e-6 <= right2.duration < 2 * frame
    check(tl)


def test_split_outside_clip_and_unknown_id():
    tl = make(("a", 5.0, 30.0), ("b", 5.0, 30.0))
    a, b = tl.segments
    before = state(tl)
    with pytest.raises(EditError):
        tl.split(a.id, 7.0)                 # inside b, not a
    with pytest.raises(EditError):
        tl.split(b.id, 2.0)
    with pytest.raises(EditError):
        tl.split("missing", 2.0)
    assert state(tl) == before


def test_split_one_frame_clip_refused():
    tl = make(("a", 10.0, 30.0))
    _, right = tl.split(tl.segments[0].id, 1 / 30)
    one = tl.segments[0]
    before = state(tl)
    with pytest.raises(EditError):
        tl.split(one.id, one.timeline_start + one.duration / 2)
    assert state(tl) == before


def test_split_segment_not_at_source_or_timeline_zero():
    tl = Timeline({"a": src("a", 20.0, 25.0)},
                  [Segment("x", "a", 4.0, 12.0, 100.0)])
    left, right = tl.split("x", 103.0)
    assert (left.source_in, left.source_out) == (4.0, pytest.approx(7.0))
    assert (right.source_in, right.source_out) == (pytest.approx(7.0), 12.0)
    assert left.timeline_start == 100.0
    assert right.timeline_start == pytest.approx(103.0)
    assert right.timeline_end == pytest.approx(108.0)
    assert tl.gaps() == [(0.0, 100.0)]
    check(tl)


def test_split_uses_timeline_time_not_source_time():
    tl = make(("a", 5.0, 30.0), ("b", 8.0, 30.0))
    b = tl.segments[1]
    left, right = tl.split(b.id, 7.0)       # 2 s into b
    assert left.source_out == pytest.approx(2.0)
    assert right.timeline_start == pytest.approx(7.0)


@pytest.mark.parametrize("fps", [30.0, NTSC, 25.0])
def test_repeated_splits_into_many_pieces(fps):
    tl = make(("a", 60.0, fps))
    rng = random.Random(7)
    for _ in range(300):
        seg = rng.choice(tl.segments)
        t = rng.uniform(seg.timeline_start, seg.timeline_end)
        try:
            tl.split(seg.id, t)
        except EditError:
            pass
    assert len(tl.segments) > 150
    check(tl)
    assert tl.segments[0].source_in == 0.0 and tl.segments[0].timeline_start == 0.0
    assert tl.segments[-1].source_out == 60.0
    for a, b in zip(tl.segments, tl.segments[1:]):
        assert a.source_out == b.source_in                 # bit-exact, no drift
        assert a.timeline_end == pytest.approx(b.timeline_start, abs=1e-9)
        assert on_grid(b.source_in, fps)
    assert sum(s.duration for s in tl.segments) == pytest.approx(60.0, abs=1e-9)
    assert tl.duration == pytest.approx(60.0, abs=1e-9)
    assert tl.gaps() == []
    # every instant still maps to the same source time as before the splits
    for t in (0.0, 0.5, 13.37, 31.0, 59.9):
        seg, st = tl.to_source(t)
        assert st == pytest.approx(t, abs=1e-9)


def test_split_without_known_source_uses_fallback_frame():
    tl = Timeline({}, [Segment("x", "ghost", 0.0, 1.0, 0.0)])
    with pytest.raises(EditError):
        tl.split("x", MIN_SEGMENT_DURATION / 2)
    left, right = tl.split("x", 0.5)
    assert right.source_in == 0.5            # unknown fps -> no quantisation


# ----------------------------------------------------------------------- trimming
def test_trim_start_happy_path():
    tl = make(("a", 10.0, 30.0))
    seg = tl.segments[0]
    applied = tl.trim_start(seg.id, 2.0)
    assert applied == pytest.approx(2.0)
    assert applied == seg.timeline_start
    assert seg.source_in == pytest.approx(2.0)
    assert seg.source_out == 10.0
    assert seg.timeline_end == pytest.approx(10.0)         # the right edge does not move
    check(tl)


def test_trim_end_happy_path():
    tl = make(("a", 10.0, 30.0))
    seg = tl.segments[0]
    applied = tl.trim_end(seg.id, 6.0)
    assert applied == pytest.approx(6.0)
    assert applied == seg.timeline_end
    assert seg.source_out == pytest.approx(6.0)
    assert seg.source_in == 0.0 and seg.timeline_start == 0.0
    check(tl)


def test_trim_start_cannot_extend_before_source_zero():
    tl = Timeline({"a": src()}, [Segment("x", "a", 1.0, 5.0, 20.0)])
    applied = tl.trim_start("x", 0.0)                      # wants 20 s more, only 1 s exists
    seg = tl.get("x")
    assert seg.source_in == pytest.approx(0.0, abs=1e-9)
    assert seg.source_in >= 0.0
    assert applied == pytest.approx(19.0)
    assert seg.timeline_end == pytest.approx(24.0)
    check(tl)


def test_trim_start_at_timeline_zero_cannot_go_negative():
    tl = Timeline({"a": src()}, [Segment("x", "a", 3.0, 5.0, 0.0)])
    applied = tl.trim_start("x", -5.0)
    assert applied == 0.0
    assert tl.get("x").source_in == 3.0                    # no room on the timeline
    check(tl)


def test_trim_end_cannot_extend_beyond_source_duration():
    tl = make(("a", 10.0, 30.0))
    seg = tl.segments[0]
    tl.trim_end(seg.id, 4.0)
    applied = tl.trim_end(seg.id, 500.0)
    assert seg.source_out == 10.0
    assert applied == pytest.approx(10.0)
    check(tl)


@pytest.mark.parametrize("duration,fps", [(10.02, 30.0), (7.3421, NTSC), (3.999, 25.0)])
def test_trim_end_can_return_to_exact_off_grid_source_end(duration, fps):
    """Media durations are rarely a whole number of frames; trimming in and dragging
    back out must restore the original clip exactly (regression: tail was lost)."""
    tl = make(("a", duration, fps))
    seg = tl.segments[0]
    original = state(tl)
    tl.trim_end(seg.id, 2.0)
    assert seg.source_out == pytest.approx(2.0, abs=1 / fps)
    applied = tl.trim_end(seg.id, duration)
    assert seg.source_out == duration
    assert applied == pytest.approx(duration)
    assert state(tl) == original
    # ... and also when dragged far past the end
    tl.trim_end(seg.id, 1.0)
    tl.trim_end(seg.id, 1e9)
    assert state(tl) == original
    # just short of the end still lands on a frame and never exceeds the source
    tl.trim_end(seg.id, duration - 0.004)
    assert seg.source_out <= duration
    check(tl)


def test_trim_start_clamped_by_previous_neighbour():
    tl = make(("a", 10.0, 30.0))
    left, right = tl.split(tl.segments[0].id, 5.0)
    tl.trim_start(right.id, 7.0)                           # gap 5..7
    tl.trim_end(left.id, 4.0)                              # gap 4..7
    applied = tl.trim_start(right.id, 1.0)                 # try to run over `left`
    assert applied == pytest.approx(4.0)
    assert right.timeline_start >= left.timeline_end - 1e-9
    check(tl)
    assert tl.gaps() == []


def test_trim_end_clamped_by_next_neighbour():
    tl = make(("a", 10.0, 30.0), ("b", 5.0, 30.0))
    a, b = tl.segments
    tl.trim_end(a.id, 6.0)                                 # gap 6..10
    applied = tl.trim_end(a.id, 12.0)                      # source has room only to 10 anyway
    assert applied == pytest.approx(10.0)
    assert a.timeline_end <= b.timeline_start + 1e-9
    check(tl)


def test_trim_end_blocked_by_adjacent_neighbour_even_with_source_room():
    tl = make(("a", 10.0, 30.0))
    left, right = tl.split(tl.segments[0].id, 5.0)
    before = state(tl)
    applied = tl.trim_end(left.id, 8.0)                    # source continues, neighbour blocks
    assert applied == pytest.approx(5.0)
    assert nums(tl) == pytest.approx(nums_of(before))
    assert left.timeline_end <= right.timeline_start + 1e-9
    applied = tl.trim_start(right.id, 2.0)
    assert applied == pytest.approx(5.0)
    assert right.source_in == pytest.approx(5.0)
    check(tl)


def test_trim_with_gap_next_to_clip_stops_at_neighbour():
    tl = make(("a", 10.0, 30.0), ("b", 10.0, 30.0), ("c", 10.0, 30.0))
    a, b, c = tl.segments
    tl.trim_start(b.id, 13.0)                              # gap 10..13
    tl.trim_end(b.id, 17.0)                                # gap 17..20
    assert tl.gaps() == [(pytest.approx(10.0), pytest.approx(13.0)),
                         (pytest.approx(17.0), pytest.approx(20.0))]
    assert tl.trim_start(b.id, 11.0) == pytest.approx(11.0)   # partly back into the gap
    assert tl.trim_end(b.id, 19.0) == pytest.approx(19.0)
    assert tl.trim_start(b.id, 0.0) == pytest.approx(10.0)    # stops at a's end
    assert tl.trim_end(b.id, 99.0) == pytest.approx(20.0)     # stops at c's start
    assert tl.gaps() == []
    assert (b.source_in, b.source_out) == (pytest.approx(0.0), pytest.approx(10.0))
    check(tl)


@pytest.mark.parametrize("fps", [30.0, 25.0, NTSC])
def test_trim_minimum_one_frame(fps):
    frame = 1.0 / fps
    tl = make(("a", 10.0, fps))
    seg = tl.segments[0]
    applied = tl.trim_start(seg.id, 50.0)                  # far past the right edge
    assert seg.duration == pytest.approx(frame, abs=1e-6)
    assert applied == seg.timeline_start
    assert seg.timeline_end == pytest.approx(10.0)
    check(tl)

    tl = make(("a", 10.0, fps))
    seg = tl.segments[0]
    applied = tl.trim_end(seg.id, -3.0)                    # far before the left edge
    assert seg.duration == pytest.approx(frame, abs=1e-6)
    assert applied == seg.timeline_end
    assert seg.timeline_start == 0.0
    check(tl)


def test_trim_one_frame_clip_stays_valid():
    tl = make(("a", 10.0, 30.0))
    seg = tl.segments[0]
    tl.trim_end(seg.id, 0.0)
    for op, t in ((tl.trim_end, -1.0), (tl.trim_start, 5.0), (tl.trim_end, 0.0)):
        try:
            op(seg.id, t)
        except EditError:
            pass
        check(tl)
        assert seg.duration == pytest.approx(1 / 30, abs=1e-6)


@pytest.mark.parametrize("fps", [30.0, 25.0, NTSC, 23.976])
def test_trim_is_frame_quantised(fps):
    tl = make(("a", 10.0, fps))
    seg = tl.segments[0]
    applied = tl.trim_start(seg.id, 1.2345)
    assert on_grid(seg.source_in, fps)
    assert abs(applied - 1.2345) <= 0.5 / fps + 1e-9
    assert applied == seg.timeline_start
    applied = tl.trim_end(seg.id, 7.6543)
    assert on_grid(seg.source_out, fps)
    assert abs(applied - 7.6543) <= 0.5 / fps + 1e-9
    assert applied == seg.timeline_end
    check(tl)


def test_trim_returned_time_always_equals_model_state():
    rng = random.Random(3)
    tl = make(("a", 10.0, NTSC), ("b", 6.0, 25.0), ("c", 8.0, 30.0))
    for _ in range(400):
        seg = rng.choice(tl.segments)
        t = rng.uniform(-2.0, tl.duration + 2.0)
        try:
            if rng.random() < 0.5:
                assert tl.trim_start(seg.id, t) == seg.timeline_start
            else:
                assert tl.trim_end(seg.id, t) == seg.timeline_end
        except EditError:
            pass
        check(tl)


def test_trim_extend_previously_trimmed_clip_back_out():
    tl = make(("a", 10.0, 30.0), ("b", 10.0, 25.0))
    a, b = tl.segments
    original = state(tl)
    tl.trim_end(a.id, 3.0)
    tl.trim_start(b.id, 15.0)
    assert tl.gaps() == [(pytest.approx(3.0), pytest.approx(15.0))]
    tl.trim_end(a.id, 10.0)
    tl.trim_start(b.id, 10.0)
    assert nums(tl) == pytest.approx(nums_of(original), abs=1e-9)
    assert tl.gaps() == []
    check(tl)


def test_trim_start_keeps_right_edge_fixed_and_trim_end_left_edge():
    tl = make(("a", 10.0, NTSC), ("b", 10.0, NTSC))
    a, b = tl.segments
    end_before = b.timeline_end
    for t in (11.0, 13.3, 12.1, 18.7, 10.0):
        tl.trim_start(b.id, t)
        assert b.timeline_end == pytest.approx(end_before, abs=1e-9)
    for t in (3.0, 9.9, 0.2):
        tl.trim_end(a.id, t)
        assert a.timeline_start == 0.0 and a.source_in == 0.0


def test_trim_small_gap_never_overlaps_previous():
    # gap of 0.02 s (less than a frame) in front of the clip: quantising must not
    # push the clip over its neighbour
    tl = Timeline({"a": src()}, [Segment("p", "a", 0.0, 4.98, 0.0),
                                 Segment("x", "a", 6.0, 8.0, 5.0)])
    applied = tl.trim_start("x", 0.0)
    assert applied >= 4.98 - 1e-9
    check(tl)
    applied = tl.trim_end("p", 99.0)
    assert tl.get("p").timeline_end <= tl.get("x").timeline_start + 1e-9
    check(tl)


def test_trim_unknown_segment_raises():
    tl = make(("a", 10.0, 30.0))
    before = state(tl)
    with pytest.raises(EditError):
        tl.trim_start("zzz", 1.0)
    with pytest.raises(EditError):
        tl.trim_end("zzz", 1.0)
    assert state(tl) == before


# ----------------------------------------------------------------------- deleting
def four() -> Timeline:
    return make(("a", 4.0, 30.0), ("b", 3.0, 25.0), ("c", 5.0, NTSC), ("d", 2.0, 30.0))


def test_delete_single_leaves_exact_gap():
    tl = four()
    a, b, c, d = tl.segments
    tl.delete([b.id])
    assert [s.id for s in tl.segments] == [a.id, c.id, d.id]
    assert tl.gaps() == [(4.0, 7.0)]
    assert (c.timeline_start, d.timeline_start) == (7.0, 12.0)
    assert tl.duration == pytest.approx(14.0)
    check(tl)


def test_delete_first_leaves_leading_gap():
    tl = four()
    a = tl.segments[0]
    tl.delete([a.id])
    assert tl.gaps() == [(0.0, 4.0)]
    assert tl.duration == pytest.approx(14.0)


def test_delete_multi_leaves_gaps():
    tl = four()
    a, b, c, d = tl.segments
    tl.delete([a.id, c.id])
    assert tl.gaps() == [(0.0, 4.0), (7.0, 12.0)]
    assert [s.id for s in tl.segments] == [b.id, d.id]
    check(tl)


def test_delete_last_shrinks_duration():
    tl = four()
    tl.delete([tl.segments[-1].id])
    assert tl.duration == pytest.approx(12.0)
    assert tl.gaps() == []


def test_ripple_delete_single():
    tl = four()
    a, b, c, d = tl.segments
    tl.delete([b.id], ripple=True)
    assert [s.timeline_start for s in tl.segments] == [0.0, 4.0, 9.0]
    assert tl.gaps() == []
    assert tl.duration == pytest.approx(11.0)
    check(tl)


def test_ripple_delete_non_adjacent_multi_select():
    tl = four()
    a, b, c, d = tl.segments
    tl.delete({a.id, c.id}, ripple=True)
    assert [s.id for s in tl.segments] == [b.id, d.id]
    assert (b.timeline_start, d.timeline_start) == (0.0, 3.0)
    assert tl.duration == pytest.approx(5.0)
    assert tl.gaps() == []
    check(tl)


def test_ripple_delete_preserves_existing_gaps():
    tl = make(("a", 4.0, 30.0), ("b", 3.0, 30.0), ("c", 5.0, 30.0),
              ("d", 2.0, 30.0), ("e", 6.0, 30.0))
    a, b, c, d, e = tl.segments
    tl.delete([b.id])                                       # plain: gap 4..7
    assert tl.gaps() == [(4.0, 7.0)]
    tl.delete([d.id], ripple=True)                          # removes 2 s after the gap
    assert tl.gaps() == [(4.0, 7.0)]                        # old gap untouched
    assert (a.timeline_start, c.timeline_start, e.timeline_start) == (0.0, 7.0, 12.0)
    tl.delete([a.id], ripple=True)                          # removes 4 s before the gap
    assert tl.gaps() == [(0.0, 3.0)]                        # same 3 s gap, moved left
    assert (c.timeline_start, e.timeline_start) == (3.0, 8.0)
    check(tl)


def test_ripple_delete_leaves_clips_before_untouched_and_sources_intact():
    tl = four()
    before = {s.id: (s.source_in, s.source_out) for s in tl.segments}
    a, b, c, d = tl.segments
    tl.delete([c.id], ripple=True)
    assert (a.timeline_start, b.timeline_start, d.timeline_start) == (0.0, 4.0, 7.0)
    for s in tl.segments:
        assert (s.source_in, s.source_out) == before[s.id]


@pytest.mark.parametrize("ripple", [False, True])
def test_delete_unknown_ids_raises_and_changes_nothing(ripple):
    tl = four()
    before = state(tl)
    with pytest.raises(EditError):
        tl.delete(["nope"], ripple=ripple)
    with pytest.raises(EditError):
        tl.delete([tl.segments[0].id, "nope"], ripple=ripple)   # partly unknown
    with pytest.raises(EditError):
        tl.delete([], ripple=ripple)
    assert state(tl) == before


@pytest.mark.parametrize("ripple", [False, True])
def test_delete_everything(ripple):
    tl = four()
    tl.delete([s.id for s in tl.segments], ripple=ripple)
    assert tl.is_empty() and tl.duration == 0.0 and tl.gaps() == []
    tl.validate()
    seg = tl.append_source(src("z", 1.0))
    assert seg.timeline_start == 0.0


def test_delete_accepts_generator_and_duplicates():
    tl = four()
    first = tl.segments[0].id
    tl.delete(i for i in [first, first])
    assert len(tl.segments) == 3


# --------------------------------------------------------------------- reordering
def order(tl: Timeline) -> list[str]:
    return [s.source_id for s in tl.segments]


def test_reorder_first_to_last():
    tl = four()
    tl.reorder(tl.segments[0].id, 3)
    assert order(tl) == ["b", "c", "d", "a"]
    assert [s.timeline_start for s in tl.segments] == pytest.approx([0.0, 3.0, 8.0, 10.0])
    assert tl.duration == pytest.approx(14.0)
    assert tl.gaps() == []
    check(tl)


def test_reorder_last_to_first():
    tl = four()
    tl.reorder(tl.segments[3].id, 0)
    assert order(tl) == ["d", "a", "b", "c"]
    assert [s.timeline_start for s in tl.segments] == pytest.approx([0.0, 2.0, 6.0, 9.0])
    assert tl.duration == pytest.approx(14.0)
    check(tl)


def test_reorder_middle_moves():
    tl = four()
    tl.reorder(tl.segments[1].id, 2)
    assert order(tl) == ["a", "c", "b", "d"]
    assert [s.timeline_start for s in tl.segments] == pytest.approx([0.0, 4.0, 9.0, 12.0])
    tl.reorder(tl.segments[2].id, 1)
    assert order(tl) == ["a", "b", "c", "d"]
    assert [s.timeline_start for s in tl.segments] == pytest.approx([0.0, 4.0, 7.0, 12.0])
    check(tl)


def test_reorder_same_index_raises_and_changes_nothing():
    tl = four()
    before = state(tl)
    for i, s in enumerate(list(tl.segments)):
        with pytest.raises(EditError):
            tl.reorder(s.id, i)
    with pytest.raises(EditError):
        tl.reorder(tl.segments[0].id, -5)       # clamps to 0 == current
    with pytest.raises(EditError):
        tl.reorder(tl.segments[-1].id, 99)      # clamps to last == current
    with pytest.raises(EditError):
        tl.reorder("nope", 0)
    assert state(tl) == before


def test_reorder_index_is_clamped():
    tl = four()
    tl.reorder(tl.segments[0].id, 99)
    assert order(tl) == ["b", "c", "d", "a"]
    tl.reorder(tl.segments[-1].id, -3)
    assert order(tl) == ["a", "b", "c", "d"]
    check(tl)


def test_reorder_single_clip_raises():
    tl = make(("a", 4.0, 30.0))
    with pytest.raises(EditError):
        tl.reorder(tl.segments[0].id, 0)


def test_reorder_every_pair_preserves_duration_and_content():
    for i in range(4):
        for j in range(4):
            if i == j:
                continue
            tl = four()
            content = {s.id: (s.source_id, s.source_in, s.source_out, s.muted)
                       for s in tl.segments}
            ids = [s.id for s in tl.segments]
            moved = ids[i]
            tl.reorder(moved, j)
            check(tl)
            assert tl.index_of(moved) == j
            assert tl.gaps() == []
            assert tl.duration == pytest.approx(14.0)
            expected = [x for x in ids if x != moved]
            expected.insert(j, moved)
            assert [s.id for s in tl.segments] == expected
            assert {s.id: (s.source_id, s.source_in, s.source_out, s.muted)
                    for s in tl.segments} == content


def test_reorder_with_gaps_is_sane():
    tl = make(("a", 4.0, 30.0), ("b", 3.0, 30.0), ("c", 5.0, 30.0),
              ("d", 2.0, 30.0), ("e", 6.0, 30.0))
    a, b, c, d, e = tl.segments
    tl.delete([b.id])                                        # gap 4..7 (3 s)
    content = {s.id: (s.source_in, s.source_out, s.duration) for s in tl.segments}
    total_gap = sum(g[1] - g[0] for g in tl.gaps())
    for seg_id, idx in ((e.id, 0), (a.id, 3), (c.id, 0), (d.id, 1), (e.id, 3), (a.id, 1)):
        tl.reorder(seg_id, idx)
        check(tl)
        assert tl.index_of(seg_id) == idx
        assert {s.id: (s.source_in, s.source_out, s.duration) for s in tl.segments} == content
        # gaps elsewhere are preserved: total empty time is unchanged
        assert sum(g[1] - g[0] for g in tl.gaps()) == pytest.approx(total_gap)
        assert tl.duration == pytest.approx(17.0 + total_gap)


def test_reorder_with_leading_gap():
    tl = four()
    a, b, c, d = tl.segments
    tl.delete([a.id])                                        # leading gap 0..4
    tl.reorder(d.id, 0)
    assert tl.gaps() == [(0.0, 4.0)]
    assert [s.id for s in tl.segments] == [d.id, b.id, c.id]
    assert [s.timeline_start for s in tl.segments] == pytest.approx([4.0, 6.0, 9.0])
    check(tl)


def test_index_for_drop():
    tl = four()                                              # a 0-4, b 4-7, c 7-12, d 12-14
    a, b, c, d = tl.segments
    # dragging `a`: others' centres are b 5.5, c 9.5, d 13
    assert tl.index_for_drop(a.id, -10.0) == 0
    assert tl.index_for_drop(a.id, 2.0) == 0
    assert tl.index_for_drop(a.id, 5.4) == 0
    assert tl.index_for_drop(a.id, 5.6) == 1
    assert tl.index_for_drop(a.id, 9.6) == 2
    assert tl.index_for_drop(a.id, 13.1) == 3
    assert tl.index_for_drop(a.id, 1000.0) == 3
    # dragging `d`: centres a 2, b 5.5, c 9.5
    assert tl.index_for_drop(d.id, 0.0) == 0
    assert tl.index_for_drop(d.id, 3.0) == 1
    assert tl.index_for_drop(d.id, 6.0) == 2
    assert tl.index_for_drop(d.id, 13.0) == 3
    # dragging `b` back onto its own slot keeps its index
    assert tl.index_for_drop(b.id, 5.5) == 1
    # exactly on a centre does not pass it
    assert tl.index_for_drop(a.id, 5.5) == 0


def test_index_for_drop_feeds_reorder():
    tl = four()
    a, b, c, d = tl.segments
    idx = tl.index_for_drop(a.id, 10.0)                       # past b and c
    tl.reorder(a.id, idx)
    assert order(tl) == ["b", "c", "a", "d"]
    with pytest.raises(EditError):
        tl.reorder(a.id, tl.index_for_drop(a.id, a.timeline_start + a.duration / 2))


# ------------------------------------------------------------------- mute / gaps
def test_set_muted():
    tl = four()
    a, b, c, d = tl.segments
    tl.set_muted([a.id, c.id], True)
    assert [s.muted for s in tl.segments] == [True, False, True, False]
    tl.set_muted([a.id], False)
    assert [s.muted for s in tl.segments] == [False, False, True, False]
    with pytest.raises(EditError):
        tl.set_muted([], True)


def test_close_gaps():
    tl = four()
    a, b, c, d = tl.segments
    tl.delete([a.id, c.id])
    tl.close_gaps()
    assert [s.timeline_start for s in tl.segments] == [0.0, 3.0]
    assert tl.gaps() == []
    assert tl.duration == pytest.approx(5.0)
    before = state(tl)
    with pytest.raises(EditError):
        tl.close_gaps()
    assert state(tl) == before
    check(tl)


def test_gaps_reporting():
    tl = Timeline({"a": src()}, [Segment("x", "a", 0.0, 1.0, 2.0),
                                 Segment("y", "a", 1.0, 2.0, 3.0),
                                 Segment("z", "a", 2.0, 3.0, 6.5)])
    assert tl.gaps() == [(0.0, 2.0), (4.0, 6.5)]
    # sub-EPS slivers are not gaps
    tl2 = Timeline({"a": src()}, [Segment("x", "a", 0.0, 1.0, 0.0),
                                  Segment("y", "a", 1.0, 2.0, 1.0 + 1e-9)])
    assert tl2.gaps() == []


# ------------------------------------------------------------------- time mapping
def test_segment_at_boundaries():
    tl = four()                                              # cuts at 4, 7, 12; end 14
    a, b, c, d = tl.segments
    assert tl.segment_at(0.0) is a                           # start inclusive
    assert tl.segment_at(3.9999) is a
    assert tl.segment_at(4.0) is b                           # a cut belongs to the right clip
    assert tl.segment_at(7.0) is c
    assert tl.segment_at(12.0) is d
    assert tl.segment_at(13.999) is d
    assert tl.segment_at(14.0) is None                       # end exclusive
    assert tl.segment_at(14.5) is None
    assert tl.segment_at(-0.5) is None
    assert a.contains(0.0) and not a.contains(4.0) and b.contains(4.0)


def test_segment_at_cut_after_float_noise():
    tl = make(("a", 10.0, NTSC))
    seg = tl.segments[0]
    pieces = [seg]
    for t in (1.1, 2.3, 3.7, 5.9, 8.2):
        _, right = tl.split(pieces[-1].id, t)
        pieces.append(right)
    for left, right in zip(pieces, pieces[1:]):
        assert tl.segment_at(right.timeline_start) is right
        assert tl.segment_at(left.timeline_end) is right
        assert tl.segment_at(right.timeline_start - 0.001) is left


def test_segment_at_and_to_source_in_gap():
    tl = four()
    a, b, c, d = tl.segments
    tl.delete([b.id])                                        # gap 4..7
    assert tl.segment_at(4.0) is None
    assert tl.segment_at(5.5) is None
    assert tl.segment_at(6.99) is None
    assert tl.segment_at(7.0) is c
    assert tl.to_source(5.0) is None
    assert tl.to_source(3.0) == (a, pytest.approx(3.0))


def test_next_segment_after():
    tl = four()
    a, b, c, d = tl.segments
    tl.delete([b.id])                                        # gap 4..7
    assert tl.next_segment_after(0.0) is a
    assert tl.next_segment_after(0.5) is c                   # a already started
    assert tl.next_segment_after(4.0) is c
    assert tl.next_segment_after(7.0) is c                   # "at or after"
    assert tl.next_segment_after(7.01) is d
    assert tl.next_segment_after(12.0) is d
    assert tl.next_segment_after(12.5) is None
    assert tl.next_segment_after(-3.0) is a


def test_to_source_to_timeline_roundtrip_after_edits():
    tl = make(("a", 10.0, NTSC), ("b", 8.0, 25.0), ("c", 6.0, 30.0))
    a, b, c = tl.segments
    _, a2 = tl.split(a.id, 3.3)
    _, a3 = tl.split(a2.id, 6.6)
    _, b2 = tl.split(b.id, 14.2)
    tl.delete([a2.id])                                       # gap
    tl.delete([b.id], ripple=True)
    tl.reorder(c.id, 0)
    tl.reorder(a3.id, 1)
    tl.trim_start(b2.id, b2.timeline_start + 0.5)
    check(tl)
    rng = random.Random(11)
    hits = 0
    for _ in range(500):
        t = rng.uniform(0.0, tl.duration)
        mapped = tl.to_source(t)
        if mapped is None:
            assert tl.segment_at(t) is None
            assert any(g0 - 1e-6 <= t <= g1 + 1e-6 for g0, g1 in tl.gaps())
            continue
        hits += 1
        seg, st = mapped
        assert seg.source_in - 1e-6 <= st < seg.source_out
        assert tl.to_timeline(seg.id, st) == pytest.approx(t, abs=1e-9)
        assert seg.to_timeline(seg.to_source(t)) == pytest.approx(t, abs=1e-9)
    assert hits > 300
    for seg in tl.segments:                                  # edges
        assert tl.to_source(seg.timeline_start) == (seg, pytest.approx(seg.source_in))
        assert tl.to_timeline(seg.id, seg.source_in) == seg.timeline_start
        assert tl.to_timeline(seg.id, seg.source_out) == pytest.approx(seg.timeline_end)
    with pytest.raises(EditError):
        tl.to_timeline("nope", 1.0)


def test_snap_points_and_snap():
    tl = four()
    a, b, c, d = tl.segments
    tl.delete([b.id])
    assert tl.snap_points() == [0.0, 4.0, 7.0, 12.0, 14.0]
    assert tl.snap_points(exclude=[c.id]) == [0.0, 4.0, 12.0, 14.0]
    assert tl.snap_points(exclude=[a.id, c.id, d.id]) == [0.0]
    pts = tl.snap_points()
    assert Timeline.snap(4.08, pts, 0.1) == 4.0
    assert Timeline.snap(3.93, pts, 0.1) == 4.0
    assert Timeline.snap(4.2, pts, 0.1) == 4.2               # out of tolerance
    assert Timeline.snap(6.8, [6.0, 7.0, 6.9], 0.5) == 6.9   # nearest wins, not first
    assert Timeline.snap(5.0, [], 1.0) == 5.0
    assert Timeline.snap(5.0, pts, 0.0) == 5.0
    assert Timeline.snap(7.0, pts, 0.0) == 7.0
    assert Timeline.snap(-0.05, pts, 0.1) == 0.0


def test_quantize_source_time():
    tl = make(("a", 10.0, 25.0))
    seg = tl.segments[0]
    assert tl.quantize_source_time(seg, 1.019) == pytest.approx(1.0)
    assert tl.quantize_source_time(seg, 1.021) == pytest.approx(1.04)
    ghost = Segment("g", "missing", 0.0, 1.0, 0.0)
    assert tl.quantize_source_time(ghost, 0.123456) == 0.123456


def test_validate_detects_broken_invariants():
    s = src("a", 10.0)
    with pytest.raises(EditError):
        Timeline({"a": s}, [Segment("x", "a", 2.0, 2.0, 0.0)]).validate()       # zero length
    with pytest.raises(EditError):
        Timeline({"a": s}, [Segment("x", "a", 3.0, 2.0, 0.0)]).validate()       # negative
    with pytest.raises(EditError):
        Timeline({"a": s}, [Segment("x", "a", -1.0, 2.0, 0.0)]).validate()
    with pytest.raises(EditError):
        Timeline({"a": s}, [Segment("x", "a", 0.0, 11.0, 0.0)]).validate()      # past source
    with pytest.raises(EditError):
        Timeline({"a": s}, [Segment("x", "a", 0.0, 5.0, 0.0),
                            Segment("y", "a", 5.0, 9.0, 4.0)]).validate()       # overlap
    with pytest.raises(EditError):
        Timeline({"a": s}, [Segment("x", "a", 0.0, 5.0, -1.0)]).validate()      # before t=0
    Timeline({"a": s}, [Segment("x", "a", 0.0, 5.0, 0.0),
                        Segment("y", "a", 5.0, 9.0, 5.0)]).validate()


# ---------------------------------------------------------- randomised invariants
def _random_op(rng: random.Random, tl: Timeline, pool: list[MediaSource]) -> None:
    segs = tl.segments
    roll = rng.random()
    if not segs or roll < 0.04:
        tl.append_source(rng.choice(pool))
        return
    seg = rng.choice(segs)
    span = tl.duration + 2.0
    if roll < 0.30:
        # mostly inside the clip, sometimes anywhere (must then be refused cleanly)
        t = (rng.uniform(seg.timeline_start, seg.timeline_end) if rng.random() < 0.8
             else rng.uniform(-1.0, span))
        tl.split(seg.id, t)
    elif roll < 0.45:
        near = rng.random() < 0.7
        t = seg.timeline_start + rng.uniform(-2.0, 2.0) if near else rng.uniform(-1.0, span)
        assert tl.trim_start(seg.id, t) == seg.timeline_start
    elif roll < 0.60:
        near = rng.random() < 0.7
        t = seg.timeline_end + rng.uniform(-2.0, 2.0) if near else rng.uniform(-1.0, span)
        assert tl.trim_end(seg.id, t) == seg.timeline_end
    elif roll < 0.68:
        k = rng.randint(1, min(3, len(segs)))
        ids = [s.id for s in rng.sample(segs, k)]
        if rng.random() < 0.1:
            ids.append("unknown-id")
        tl.delete(ids, ripple=False)
    elif roll < 0.76:
        k = rng.randint(1, min(3, len(segs)))
        tl.delete([s.id for s in rng.sample(segs, k)], ripple=True)
    elif roll < 0.90:
        tl.reorder(seg.id, rng.randint(-1, len(segs)))
    elif roll < 0.96:
        ids = [s.id for s in rng.sample(segs, rng.randint(0, min(3, len(segs))))]
        tl.set_muted(ids, rng.random() < 0.5)
    else:
        tl.close_gaps()


@pytest.mark.parametrize("seed", [1, 2, 3, 20240229, 987654321])
def test_random_operations_keep_invariants(seed):
    rng = random.Random(seed)
    pool = [src("a", 12.0, NTSC), src("b", 9.52, 25.0), src("c", 7.3421, 60.0)]
    tl = Timeline()
    for s in pool:
        tl.append_source(s)
    applied = refused = 0
    for step in range(600):
        before = tl.snapshot()
        before_order = [id(s) for s in tl.segments]
        try:
            _random_op(rng, tl, pool)
            applied += 1
        except EditError:
            refused += 1
            assert tl.segments == before, f"step {step}: failed op modified the timeline"
            assert [id(s) for s in tl.segments] == before_order
        try:
            check(tl)
        except (EditError, AssertionError) as exc:      # pragma: no cover - diagnostics
            pytest.fail(f"seed {seed} step {step}: {exc}\nbefore={before}\nafter={tl.segments}")
        # time mapping stays consistent with the segment list
        if tl.segments:
            probe = rng.uniform(0.0, tl.duration)
            hit = tl.segment_at(probe)
            if hit is not None:
                assert hit.timeline_start - EPS <= probe < hit.timeline_end
    assert applied > 200 and refused > 20


def test_random_no_gap_timeline_keeps_total_duration_under_reorder():
    rng = random.Random(5)
    tl = make(("a", 12.0, NTSC), ("b", 9.52, 25.0), ("c", 7.3421, 60.0))
    for _ in range(40):
        seg = rng.choice(tl.segments)
        try:
            tl.split(seg.id, rng.uniform(seg.timeline_start, seg.timeline_end))
        except EditError:
            pass
    total = tl.duration
    content = sorted((s.source_id, s.source_in, s.source_out) for s in tl.segments)
    for _ in range(300):
        seg = rng.choice(tl.segments)
        try:
            tl.reorder(seg.id, rng.randrange(len(tl.segments)))
        except EditError:
            continue
        check(tl)
        assert tl.gaps() == []
    assert tl.duration == pytest.approx(total, abs=1e-6)
    assert tl.segments[0].timeline_start == pytest.approx(0.0, abs=1e-6)
    assert sorted((s.source_id, s.source_in, s.source_out) for s in tl.segments) == content


# ---------------------------------------------------------------- move_to (slide)
def _two_with_gaps():
    src = MediaSource("s", "x.mp4", 20.0, 640, 360, 30.0)
    tl = Timeline({"s": src})
    a = Segment("a", "s", 0.0, 4.0, 2.0)      # 2-6
    b = Segment("b", "s", 4.0, 8.0, 10.0)     # 10-14
    tl.segments = [a, b]
    return tl


def test_move_to_pulls_first_clip_to_zero():
    tl = _two_with_gaps()
    assert tl.move_to("a", -5.0) == 0.0
    assert tl.get("a").timeline_start == 0.0
    tl.validate()


def test_move_to_is_clamped_between_neighbours():
    tl = _two_with_gaps()
    assert tl.move_to("a", 50.0) == pytest.approx(6.0)     # b starts at 10, a is 4 long
    with pytest.raises(EditError):                         # a now ends at 10 = b's start: no room
        tl.move_to("b", 3.0)
    tl.validate()


def test_move_to_pushes_last_clip_right_without_limit():
    tl = _two_with_gaps()
    assert tl.move_to("b", 50.0) == pytest.approx(50.0)
    tl.validate()


def test_move_to_same_position_raises_and_changes_nothing():
    tl = _two_with_gaps()
    with pytest.raises(EditError):
        tl.move_to("a", 2.0)
    assert tl.get("a").timeline_start == 2.0


# ----------------------------------------------------------------- audio lane
def _av_timeline():
    from clipforge.core.model import MediaSource, Timeline
    src = MediaSource(id="s1", path="x.mp4", duration=10.0, has_audio=True, fps=25.0)
    tl = Timeline({"s1": src})
    seg = tl.append_source(src)
    return tl, seg


def test_detach_audio_creates_matching_muted_pair():
    tl, seg = _av_timeline()
    made = tl.detach_audio([seg.id])
    assert len(made) == 1 and made[0].track == "audio"
    a = made[0]
    assert (a.source_id, a.source_in, a.source_out, a.timeline_start) == ("s1", 0.0, 10.0, 0.0)
    assert seg.muted is True and not a.muted
    tl.validate()


def test_detach_audio_twice_or_without_audio_is_refused():
    import pytest
    from clipforge.core.model import EditError, MediaSource, Timeline
    tl, seg = _av_timeline()
    tl.detach_audio([seg.id])
    with pytest.raises(EditError):
        tl.detach_audio([seg.id])
    silent = MediaSource(id="s2", path="y.mp4", duration=5.0, has_audio=False)
    tl2 = Timeline({"s2": silent})
    s2 = tl2.append_source(silent)
    with pytest.raises(EditError):
        tl2.detach_audio([s2.id])


def test_audio_clip_edits_are_independent_of_video():
    tl, seg = _av_timeline()
    a = tl.detach_audio([seg.id])[0]
    left, right = tl.split(a.id, 4.0)
    assert left.track == right.track == "audio" and len(tl.audio) == 2 and len(tl.segments) == 1
    tl.delete([left.id])
    assert [s.id for s in tl.audio] == [right.id]
    tl.move_to(right.id, 2.0)
    assert right.timeline_start == 2.0 and seg.timeline_start == 0.0
    tl.trim_end(right.id, 5.0)
    assert tl.duration >= seg.timeline_end        # audio may not shorten the video
    tl.validate()


def test_snapshot_restore_round_trip_keeps_both_lanes():
    tl, seg = _av_timeline()
    before = tl.snapshot()
    tl.detach_audio([seg.id])
    assert len(tl.snapshot()) == 2
    tl.restore(before)
    assert tl.audio == [] and tl.segments[0].muted is False


def test_audio_lane_serialises_with_track():
    from clipforge.core.model import Segment
    tl, seg = _av_timeline()
    a = tl.detach_audio([seg.id])[0]
    assert Segment.from_dict(a.to_dict()).track == "audio"
    old = seg.to_dict(); old.pop("track")
    assert Segment.from_dict(old).track == "video"       # older project files
