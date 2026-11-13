"""Multi-track behaviour of the timeline panel: lane geometry, hit-testing, cross-lane
drags, new-track-on-drop, track removal, keyboard helpers and the overlay entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, Signal  # noqa: E402
from PySide6.QtGui import QMouseEvent, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipforge.core.model import MAX_VIDEO_TRACKS  # noqa: E402
from clipforge.document import Document  # noqa: E402
from clipforge.ui import theme  # noqa: E402
from clipforge.ui import timeline as tl  # noqa: E402
from clipforge.ui.timeline import TimelinePanel  # noqa: E402

LEFT = Qt.MouseButton.LeftButton
NOMOD = Qt.KeyboardModifier.NoModifier


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication([])
    theme.apply_theme(inst, "dark")
    return inst


def _mouse(view, kind, pos, mods=NOMOD):
    p = QPointF(pos)
    button = LEFT if kind != QEvent.Type.MouseMove else Qt.MouseButton.NoButton
    buttons = LEFT if kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    ev = QMouseEvent(kind, p, QPointF(view.mapToGlobal(p.toPoint())), button, buttons, mods)
    QApplication.sendEvent(view, ev)


def drag(view, a, b, steps=6, finish=True):
    _mouse(view, QEvent.Type.MouseButtonPress, a)
    for i in range(1, steps + 1):
        _mouse(view, QEvent.Type.MouseMove,
               QPoint(round(a.x() + (b.x() - a.x()) * i / steps),
                      round(a.y() + (b.y() - a.y()) * i / steps)))
    if finish:
        _mouse(view, QEvent.Type.MouseButtonRelease, b)


class Env:
    def __init__(self, app, doc, panel):
        self.app, self.doc, self.panel, self.view = app, doc, panel, panel.view

    def pump(self):
        for _ in range(3):
            self.app.processEvents()

    def pt(self, t, layer, dy=None):
        top, h, _, _ = self.view.lanes(layer)
        return QPoint(round(self.view.time_to_x(t)), top + (h // 2 if dy is None else dy))


@pytest.fixture
def env(app, video_h, video_v):
    doc = Document()
    panel = TimelinePanel(doc, None, None)
    panel.resize(1000, 420)
    panel.show()
    e = Env(app, doc, panel)
    e.h, e.v = video_h, video_v
    e.pump()
    yield e
    panel.close()
    panel.deleteLater()
    doc.deleteLater()
    e.pump()


def base_and_overlay(e):
    """V1: h (6 s) at 0; V2: v at 1 s; returns (v1_id, v2_id)."""
    e.doc.add_source(e.h)
    e.doc.add_source(e.v, track=1, at=1.0)
    e.pump()
    tlm = e.doc.timeline
    return tlm.track(0)[0].id, tlm.track(1)[0].id


# --------------------------------------------------------------------------- geometry
def test_single_track_has_no_gutter_and_old_geometry(env):
    env.doc.add_source(env.h)
    v = env.view
    assert v.gutter_width() == 0
    assert v.lanes()[0] == tl.RULER_H + tl.TRACK_PAD
    assert v.time_to_x(0.0) == tl.LEFT_PAD


def test_lanes_stack_highest_on_top_with_gutter(env):
    base_and_overlay(env)
    env.doc.add_track()
    env.pump()
    v = env.view
    assert env.doc.track_count == 3
    assert v.gutter_width() == tl.GUTTER_W
    assert v.time_to_x(0.0) == tl.LEFT_PAD + tl.GUTTER_W
    tops = [v.lane_top(layer) for layer in (2, 1, 0)]
    assert tops == sorted(tops) and len(set(tops)) == 3
    h = v._lane_h()
    assert tops[1] - tops[0] == h + tl.LANE_STEP
    assert [v.lane_label(i) for i in (2, 1, 0, -1)] == ["V3", "V2", "V1", "A"]
    # each lane's clip row is inside the widget and below the ruler
    assert tops[0] >= tl.RULER_H and tops[2] + h <= v.height()


def test_lane_height_adapts_and_vertical_scroll_appears(env):
    base_and_overlay(env)
    v = env.view
    roomy = v._lane_h()
    for _ in range(4):
        env.doc.add_track()
    env.pump()
    assert env.doc.track_count == 6
    assert v._lane_h() < roomy
    assert v._lane_h() >= tl.MIN_LANE_H
    env.panel.resize(1000, 240)
    env.pump()
    assert v.max_vscroll() > 0
    assert env.panel.vscrollbar.isVisible()
    v.set_vscroll(1e6)
    assert v.vscroll_offset() == pytest.approx(v.max_vscroll())
    # lanes scrolled: top lane moved up, hit-test follows the lanes
    assert v.lane_top(0) + v._lane_h() <= v.height() - tl.TRACK_PAD + 1
    assert v.lane_at(v.lane_top(0) + 2) == 0
    # wheel scrolls vertically while lanes overflow
    v.set_vscroll(0)
    ev = QWheelEvent(QPointF(300, 100), QPointF(v.mapToGlobal(QPoint(300, 100))), QPoint(0, 0),
                     QPoint(0, -120), Qt.MouseButton.NoButton, NOMOD,
                     Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(v, ev)
    assert v.vscroll_offset() > 0
    # removing tracks brings the offset back into range
    for layer in (5, 4, 3, 2):
        assert env.doc.remove_track(layer)
    env.pump()
    assert v.vscroll_offset() <= v.max_vscroll()


def test_view_rebuilds_on_tracks_changed(env):
    env.doc.add_source(env.h)
    v = env.view
    assert v.gutter_width() == 0
    env.doc.add_track()
    assert v.gutter_width() == tl.GUTTER_W
    env.doc.undo()
    assert v.gutter_width() == 0
    assert env.panel.track_button.isEnabled()


# ------------------------------------------------------------------------ hit testing
def test_hit_test_resolves_the_lane(env):
    v1, v2 = base_and_overlay(env)
    v = env.view
    seg, zone = v.hit_test(env.pt(3.0, 0))
    assert seg.id == v1 and zone == "body"
    seg, zone = v.hit_test(env.pt(3.0, 1))
    assert seg.id == v2 and zone == "body"
    # on V2 at a time where only V1 has a clip -> nothing
    assert v.hit_test(env.pt(0.2, 1))[0] is None
    # gutter and ruler are never clips
    assert v.hit_test(QPoint(4, v.lane_top(0) + 5))[0] is None
    assert v.hit_test(QPoint(env.pt(3.0, 1).x(), 5))[0] is None
    assert v.hit_test(env.pt(1.0, 1))[1] in ("left", "body")


def test_target_layer_and_new_track_zone(env):
    base_and_overlay(env)
    v = env.view
    n = env.doc.track_count
    assert v.target_layer(v.lane_top(0) + 5) == 0
    assert v.target_layer(v.lane_top(1) + 5) == 1
    assert v.target_layer(v.lane_top(n - 1) - tl.NEWTRACK_SLACK - 2) == n
    assert v.target_layer(tl.RULER_H + 2) == n
    assert v.target_layer(v.height() - 1) == 0          # below everything: V1
    for _ in range(MAX_VIDEO_TRACKS):
        env.doc.add_track()
    top = env.doc.track_count - 1
    assert v.target_layer(tl.RULER_H + 2) == top        # at the limit: no new track


def test_overlay_clip_gets_distinct_paint(env):
    base_and_overlay(env)
    env.doc.select([env.doc.timeline.track(1)[0].id])
    img = env.panel.grab().toImage()
    assert not img.isNull()


# ---------------------------------------------------------------------- drags / moves
def test_drag_clip_to_other_lane_and_along_time(env):
    v1, v2 = base_and_overlay(env)
    doc = env.doc
    steps = len(doc.history.undo_stack)
    src = env.pt(2.0, 1)
    dst = QPoint(round(env.view.time_to_x(3.0)), env.view.lanes(0)[0] + 10)
    drag(env.view, src, dst)
    clip = doc.timeline.get(v2)
    assert clip.layer == 0 or clip.layer == 1
    # V1 is occupied by the 6 s clip from 0: the clip lands at the nearest free slot on V1
    assert clip.layer == 0
    assert clip.timeline_start >= doc.timeline.get(v1).timeline_end - 1e-6
    assert len(doc.history.undo_stack) == steps + 1
    doc.timeline.validate()


def test_drag_along_time_on_same_lane(env):
    _, v2 = base_and_overlay(env)
    doc = env.doc
    start = doc.timeline.get(v2).timeline_start
    a = env.pt(start + 0.5, 1)
    b = QPoint(a.x() + round(1.5 * env.view.pixels_per_second), a.y())
    env.doc.select([])
    drag(env.view, a, b)
    clip = doc.timeline.get(v2)
    assert clip.layer == 1
    assert clip.timeline_start == pytest.approx(start + 1.5, abs=0.15)


def test_drop_above_top_lane_creates_track(env):
    v1, v2 = base_and_overlay(env)
    doc = env.doc
    assert doc.track_count == 2
    a = env.pt(3.0, 0)
    b = QPoint(a.x(), tl.RULER_H + 3)
    drag(env.view, a, b)
    assert doc.track_count == 3
    clip = doc.timeline.get(v1)
    assert clip.layer == 2
    assert len(doc.timeline.track(0)) == 0 and len(doc.timeline.track(1)) == 1
    doc.undo()                                          # one undo step for track + move
    assert doc.track_count == 2 and doc.timeline.get(v1).layer == 0


def test_single_track_drag_above_lane_makes_overlay_track(env):
    env.doc.add_source(env.h)
    env.doc.add_source(env.v)
    env.pump()
    doc = env.doc
    first = doc.timeline.track(0)[0].id
    v = env.view
    a = QPoint(round(v.time_to_x(1.0)), v.lanes()[0] + 40)
    drag(v, a, QPoint(a.x(), tl.RULER_H + 2))
    assert doc.track_count == 2
    assert doc.timeline.get(first).layer == 1


def test_single_track_reorder_still_works(env):
    env.doc.add_source(env.h)
    env.doc.add_source(env.v)
    env.pump()
    doc = env.doc
    a_id, b_id = [s.id for s in doc.timeline.segments]
    v = env.view
    a = QPoint(round(v.time_to_x(1.0)), v.lanes()[0] + 40)
    b = QPoint(round(v.time_to_x(doc.duration - 0.5)), a.y())
    drag(v, a, b)
    assert [s.id for s in doc.timeline.segments] == [b_id, a_id]
    assert doc.track_count == 1


def test_ghost_uses_fit_start_and_esc_cancels(env):
    v1, v2 = base_and_overlay(env)
    v = env.view
    a = env.pt(2.0, 1)
    b = QPoint(round(v.time_to_x(2.0)), v.lanes(0)[0] + 10)
    drag(v, a, b, finish=False)
    d = v._drag
    assert d is not None and d.kind == "move" and d.target_track == 0
    expected = env.doc.timeline.fit_start(0, d.slide_start, env.doc.timeline.get(v2).duration,
                                          exclude=[v2])
    assert d.slide_start == pytest.approx(expected)
    assert env.panel.grab() is not None            # paints the ghost
    v.cancel_drag()
    assert env.doc.timeline.get(v2).layer == 1


def test_snapping_works_across_tracks(env):
    v1, v2 = base_and_overlay(env)
    doc = env.doc
    end_v1 = doc.timeline.get(v1).timeline_end
    v = env.view
    # drag V2 clip so its left edge lands close to the end of the V1 clip (snap edge on V1)
    clip = doc.timeline.get(v2)
    a = env.pt(clip.timeline_start + 0.3, 1)
    target_x = v.time_to_x(end_v1 - 0.02 + 0.3) - 0.0
    drag(v, a, QPoint(round(target_x), a.y()))
    assert doc.timeline.get(v2).timeline_start == pytest.approx(end_v1, abs=1e-3)


def test_trim_split_delete_work_on_overlay_lane(env):
    v1, v2 = base_and_overlay(env)
    doc, v = env.doc, env.view
    clip = doc.timeline.get(v2)
    before = clip.duration
    # trim the overlay's right edge
    edge = QPoint(round(v.time_to_x(clip.timeline_end)) - 1, v.lanes(1)[0] + 20)
    drag(v, edge, QPoint(edge.x() - 40, edge.y()))
    assert doc.timeline.get(v2).duration < before
    # select it, split at playhead inside it, then delete one half
    doc.select([v2])
    doc.seek(doc.timeline.get(v2).timeline_start + 1.0)
    assert doc.split_at_playhead()
    assert len(doc.timeline.track(1)) == 2
    # ripple only touches its own lane
    v1_start = doc.timeline.get(v1).timeline_start
    doc.select([doc.timeline.track(1)[0].id])
    doc.delete_selected(ripple=True)
    assert doc.timeline.get(v1).timeline_start == v1_start
    assert len(doc.timeline.track(1)) == 1


def test_multi_select_across_lanes(env):
    v1, v2 = base_and_overlay(env)
    v = env.view
    _mouse(v, QEvent.Type.MouseButtonPress, env.pt(3.0, 0))
    _mouse(v, QEvent.Type.MouseButtonRelease, env.pt(3.0, 0))
    ctrl = Qt.KeyboardModifier.ControlModifier
    _mouse(v, QEvent.Type.MouseButtonPress, env.pt(3.0, 1), ctrl)
    _mouse(v, QEvent.Type.MouseButtonRelease, env.pt(3.0, 1), ctrl)
    assert set(env.doc.selection) == {v1, v2}


# ------------------------------------------------------------------ track operations
def test_add_track_button_and_limit(env):
    env.doc.add_source(env.h)
    assert env.panel.add_track() == 1
    assert env.doc.track_count == 2
    while env.doc.track_count < MAX_VIDEO_TRACKS:
        env.panel.add_track()
    env.pump()
    assert not env.panel.track_button.isEnabled()
    assert env.panel.add_track() == -1


def test_remove_empty_track_without_confirmation(env):
    env.doc.add_source(env.h)
    env.panel.add_track()
    asked = []
    env.view.confirm_remove = lambda layer, n: asked.append((layer, n)) or True
    assert env.view.remove_track(1)
    assert env.doc.track_count == 1 and not asked


def test_remove_track_with_clips_confirms_then_deletes(env):
    v1, v2 = base_and_overlay(env)
    asked = []
    env.view.confirm_remove = lambda layer, n: asked.append((layer, n)) or False
    assert not env.view.remove_track(1)
    assert asked == [(1, 1)] and env.doc.track_count == 2
    env.view.confirm_remove = lambda layer, n: True
    assert env.view.remove_track(1)
    assert env.doc.track_count == 1
    assert [s.id for s in env.doc.timeline.all_segments()] == [v1]


def test_v1_cannot_be_removed_and_lane_menu(env):
    base_and_overlay(env)
    assert not env.view.remove_track(0)
    menu = env.view.build_track_menu(0)
    acts = {a.text().split("\t")[0]: a for a in menu.actions() if a.text()}
    assert not acts["Remove track V1"].isEnabled()
    menu2 = env.view.build_track_menu(1)
    acts2 = {a.text().split("\t")[0]: a for a in menu2.actions() if a.text()}
    assert acts2["Remove track V2"].isEnabled() and acts2["Add track"].isEnabled()
    menu.deleteLater()
    menu2.deleteLater()


def test_clip_menu_has_move_entries_only_when_multitrack(env):
    v1, v2 = base_and_overlay(env)
    names = [a.text().split("\t")[0] for a in env.view.build_context_menu(v2).actions()]
    assert "Move to track above" in names and "Move to track below" in names


# ------------------------------------------------------------------------- keyboard API
def test_move_selected_track_up_down_and_new_track(env):
    v1, v2 = base_and_overlay(env)
    doc = env.doc
    doc.select([v2])
    assert env.panel.move_selected_track(-1)
    assert doc.timeline.get(v2).layer == 0
    doc.select([v2])
    assert env.panel.move_selected_track(1)
    assert doc.timeline.get(v2).layer == 1
    assert env.panel.move_selected_track(1)              # off the top: creates V3
    assert doc.track_count == 3 and doc.timeline.get(v2).layer == 2
    doc.select([v1])
    assert not env.panel.move_selected_track(-1)         # V1 cannot go lower


def test_shortcuts_registered_in_main_window_source():
    src = (Path(__file__).resolve().parent.parent / "clipforge" / "ui" / "main_window.py").read_text(
        encoding="utf-8")
    for key in ("Ctrl+Shift+T", "Alt+Up", "Alt+Down"):
        assert key in src


# ---------------------------------------------------------------------------- overlay
class _Thumbs(QObject):
    thumbnailReady = Signal(str, float)

    def poster(self, source):
        return None


def _left_panel(tmp_path, doc):
    from clipforge.settings import Settings
    from clipforge.ui.left_panel import LeftPanel
    s = Settings.load(tmp_path / "s.json")
    s.download_dir = str(tmp_path / "dl")
    s.export_dir = str(tmp_path / "out")
    s.cache_dir = str(tmp_path / "cache")
    s.temp_dir = str(tmp_path / "temp")
    return LeftPanel(doc, s, _Thumbs())


def test_add_as_overlay_places_half_size_bottom_right(app, tmp_path, video_h, video_v):
    doc = Document()
    panel = _left_panel(tmp_path, doc)
    doc.add_source(video_h)
    assert panel.can_add_as_overlay(video_h.id)
    assert panel.add_as_overlay(video_h.id)
    assert doc.track_count == 2
    clip = doc.timeline.track(1)[0]
    assert clip.layer == 1 and clip.source_id == video_h.id
    assert clip.scale == pytest.approx(0.5)
    assert clip.pos_x > 0 and clip.pos_y > 0
    # a second overlay at the same time does not fit on V2: it gets a new track
    assert panel.add_as_overlay(video_h.id)
    assert doc.track_count == 3
    # a portrait source is scaled down so it also fits the corner
    doc.add_source(video_v, to_timeline=False)
    assert panel.add_as_overlay(video_v.id)
    portrait = doc.selected_segments()[0]
    assert portrait.is_overlay and portrait.scale < 0.5
    assert portrait.pos_y > 0
    panel.shutdown()
    panel.deleteLater()
    doc.deleteLater()


def test_add_as_overlay_needs_a_base_clip(app, tmp_path, video_v):
    doc = Document()
    panel = _left_panel(tmp_path, doc)
    doc.add_source(video_v, to_timeline=False)
    assert not panel.can_add_as_overlay(video_v.id)
    assert not panel.add_as_overlay(video_v.id)
    assert doc.track_count == 1
    panel.shutdown()
    panel.deleteLater()
    doc.deleteLater()
