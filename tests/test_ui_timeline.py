"""Tests for the timeline panel (mouse interaction, zoom, toolbar, painting).

Uses fake thumbnail / waveform providers with the real signal and method names, so no
media decoding is involved; the clips only need MediaSource metadata (the conftest
synthetic videos are used so the "file exists" check passes).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, Signal  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipforge.core.model import MediaSource, Segment  # noqa: E402
from clipforge.document import Document  # noqa: E402
from clipforge.ui import theme  # noqa: E402
from clipforge.ui import timeline as tl  # noqa: E402
from clipforge.ui.timeline import TimelinePanel  # noqa: E402

LEFT = Qt.MouseButton.LeftButton
NOMOD = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
ALT = Qt.KeyboardModifier.AltModifier


# ------------------------------------------------------------------------- fakes
class FakeThumbs(QObject):
    thumbnailReady = Signal(str, float)

    def __init__(self):
        super().__init__()
        self.gets = 0
        self.cancels = 0
        self.mode = "image"            # image | none | raise

    def get(self, source, time):
        self.gets += 1
        if self.mode == "raise":
            raise RuntimeError("boom")
        if self.mode == "none":
            return None
        img = QImage(64, 36, QImage.Format.Format_RGB32)
        img.fill(QColor(40 + int(time * 20) % 200, 90, 140))
        return img

    def cancel_pending(self):
        self.cancels += 1


class FakeWaves(QObject):
    waveformReady = Signal(str)

    def __init__(self):
        super().__init__()
        self.mode = "peaks"            # peaks | none | empty | raise

    def get(self, source):
        if self.mode == "raise":
            raise RuntimeError("boom")
        if self.mode == "none":
            return None
        if self.mode == "empty":
            return np.zeros((0, 2), np.float32)
        n = int(source.duration * 100)
        x = np.sin(np.arange(n) / 7.0).astype(np.float32)
        return np.stack([-np.abs(x), np.abs(x)], axis=1)


# ---------------------------------------------------------------------- helpers
@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication([])
    theme.apply_theme(inst, "dark")
    return inst


def mouse(view, kind, pos, button=LEFT, buttons=None, mods=NOMOD):
    """Deliver a synthetic mouse event (with modifiers) straight to the view."""
    p = QPointF(pos)
    if buttons is None:
        buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
    if kind == QEvent.Type.MouseMove:
        button, buttons = Qt.MouseButton.NoButton, LEFT
    ev = QMouseEvent(kind, p, QPointF(view.mapToGlobal(p.toPoint())), button, buttons, mods)
    QApplication.sendEvent(view, ev)


def press(view, pos, mods=NOMOD):
    mouse(view, QEvent.Type.MouseButtonPress, pos, mods=mods)


def move(view, pos, mods=NOMOD):
    mouse(view, QEvent.Type.MouseMove, pos, mods=mods)


def release(view, pos, mods=NOMOD):
    mouse(view, QEvent.Type.MouseButtonRelease, pos, mods=mods)


def click(view, pos, mods=NOMOD):
    press(view, pos, mods)
    release(view, pos, mods)


def drag(view, a, b, mods=NOMOD, steps=6, finish=True):
    press(view, a, mods)
    for i in range(1, steps + 1):
        move(view, QPoint(round(a.x() + (b.x() - a.x()) * i / steps),
                          round(a.y() + (b.y() - a.y()) * i / steps)), mods)
    if finish:
        release(view, b, mods)


def wheel(view, pos, delta, mods=NOMOD):
    ev = QWheelEvent(QPointF(pos), QPointF(view.mapToGlobal(pos)), QPoint(0, 0),
                     QPoint(0, delta), Qt.MouseButton.NoButton, mods,
                     Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view, ev)


def seg_for(src, a, b, start, muted=False):
    return Segment(uuid.uuid4().hex[:12], src.id, a, b, start, muted)


def pump(app, n=3):
    for _ in range(n):
        app.processEvents()


class Env:
    def __init__(self, app, doc, panel, thumbs, waves):
        self.app, self.doc, self.panel, self.thumbs, self.waves = app, doc, panel, thumbs, waves
        self.view = panel.view

    def x(self, t):
        return round(self.view.time_to_x(t))

    def body_pt(self, t, y=None):
        return QPoint(self.x(t), y if y is not None else self.view.lanes()[0] + 40)

    def ruler_pt(self, t):
        return QPoint(self.x(t), 10)

    def ids(self):
        return [s.id for s in self.doc.timeline.segments]

    def undo_count(self):
        return len(self.doc.history.undo_stack)


@pytest.fixture
def env(app, video_h, video_v):
    doc = Document()
    thumbs, waves = FakeThumbs(), FakeWaves()
    panel = TimelinePanel(doc, thumbs, waves)
    panel.resize(1000, 320)
    panel.show()
    pump(app)
    e = Env(app, doc, panel, thumbs, waves)
    e.h, e.v = video_h, video_v
    yield e
    panel.close()
    panel.deleteLater()
    doc.deleteLater()
    pump(app)


def add(e, *sources):
    for src in sources:
        e.doc.add_source(src)
    pump(e.app)


def three(e):
    """Clips A (h, 6 s), B (v, 4 s), C (h, 6 s) back to back = 16 s."""
    add(e, e.h, e.v)
    e.doc.append_to_timeline(e.h.id)
    pump(e.app)
    return e.ids()


# ------------------------------------------------------------------------- mapping
def test_time_x_roundtrip_at_various_zooms_and_scrolls(env):
    three(env)
    v = env.view
    for pps in (v.min_pps(), 80.0, 150.0, 333.3, v.max_pps()):
        v.set_zoom(pps)
        for scroll in (0.0, 123.0, 777.7, 1e9):
            v.set_scroll(scroll)
            assert 0.0 <= v.scroll_offset <= v.max_scroll() + 1e-9
            for t in (0.0, 0.001, 3.14159, 8.0, 15.999, 16.0):
                assert v.x_to_time(v.time_to_x(t)) == pytest.approx(t, abs=1e-9)
            for x in (0, 17.5, 500, 999):
                assert v.time_to_x(v.x_to_time(x)) == pytest.approx(x, abs=1e-9)
    assert v.pixels_per_second == pytest.approx(v.max_pps())


def test_zoom_is_clamped_and_fit_resets(env):
    three(env)
    v = env.view
    v.set_zoom(1e9)
    assert v.pixels_per_second == pytest.approx(v.max_pps())
    v.set_zoom(1e-9)
    assert v.pixels_per_second == pytest.approx(v.min_pps())
    v.set_zoom(200.0)
    env.panel.zoom_to_fit()
    assert v.pixels_per_second == pytest.approx(v.min_pps())
    assert v.scroll_offset == 0.0
    # the whole timeline fits in the view when fitted
    assert v.time_to_x(env.doc.duration) <= v.width()


def test_ctrl_wheel_zoom_keeps_time_under_cursor(env):
    three(env)
    v = env.view
    v.set_zoom(150.0, anchor_x=300)
    v.set_scroll(400.0)
    for cursor_x, delta in ((400, 120), (250, 240), (700, -120), (123, 120)):
        t0 = v.x_to_time(cursor_x)
        pps0 = v.pixels_per_second
        wheel(v, QPoint(cursor_x, 100), delta, CTRL)
        assert v.x_to_time(cursor_x) == pytest.approx(t0, abs=1e-6)
        assert (v.pixels_per_second > pps0) == (delta > 0)
        assert v.pixels_per_second == pytest.approx(pps0 * tl.WHEEL_ZOOM_STEP ** (delta / 120.0))


def test_plain_wheel_scrolls_not_zooms(env):
    three(env)
    v = env.view
    v.set_zoom(200.0)
    v.set_scroll(300.0)
    pps = v.pixels_per_second
    wheel(v, QPoint(400, 100), -120)
    assert v.pixels_per_second == pps
    assert v.scroll_offset > 300.0
    wheel(v, QPoint(400, 100), 120)
    assert v.scroll_offset == pytest.approx(300.0)


def test_scrollbar_and_slider_stay_in_sync(env):
    three(env)
    p, v = env.panel, env.view
    assert not p.zoom_out_button.isEnabled()          # fitted: cannot zoom out further
    assert p.zoom_in_button.isEnabled()
    p.zoom_in()
    assert p.zoom_out_button.isEnabled()
    assert p.zoom_slider.value() == round(v.zoom_fraction() * tl.ZOOM_SLIDER_STEPS) > 0
    p.scrollbar.setValue(120)
    assert v.scroll_offset == pytest.approx(120.0)
    p.zoom_slider.setValue(tl.ZOOM_SLIDER_STEPS)
    assert v.pixels_per_second == pytest.approx(v.max_pps())
    p.fit_button.click()
    assert v.pixels_per_second == pytest.approx(v.min_pps())
    assert p.scrollbar.value() == 0


def test_double_click_zooms_to_clip(env):
    ids = three(env)
    v = env.view
    seg = env.doc.timeline.get(ids[1])
    pt = env.body_pt((seg.timeline_start + seg.timeline_end) / 2)
    mouse(v, QEvent.Type.MouseButtonDblClick, pt)
    assert v.pixels_per_second > v.min_pps() * 2
    mid = (seg.timeline_start + seg.timeline_end) / 2
    assert v.x_to_time(v.width() / 2) == pytest.approx(mid, abs=0.2)


# ------------------------------------------------------------------- seek / select
def test_ruler_click_seeks_and_drag_scrubs(env):
    three(env)
    v, doc = env.view, env.doc
    click(v, env.ruler_pt(3.3))
    assert doc.playhead == pytest.approx(3.3, abs=1.5 / v.pixels_per_second)
    seeks = []
    doc.seekRequested.connect(seeks.append)
    drag(v, env.ruler_pt(2.0), env.ruler_pt(12.5))
    assert doc.playhead == pytest.approx(12.5, abs=1.5 / v.pixels_per_second)
    assert len(seeks) >= 2 and seeks[-1] == pytest.approx(doc.playhead)
    # clicking left of 0 / right of the end clamps
    click(v, QPoint(2, 10))
    assert doc.playhead == 0.0
    click(v, QPoint(v.width() - 2, 10))
    assert doc.playhead == pytest.approx(doc.duration)


def test_ruler_scrub_snaps_to_clip_edge_unless_alt(env):
    three(env)
    v, doc = env.view, env.doc
    near = QPoint(env.x(6.0) + 3, 10)
    click(v, near)
    assert doc.playhead == pytest.approx(6.0)
    click(v, near, ALT)
    assert doc.playhead == pytest.approx(v.x_to_time(near.x()), abs=1e-6)
    assert doc.playhead != pytest.approx(6.0, abs=1e-4)
    env.panel.set_snapping(False)
    click(v, near)
    assert doc.playhead == pytest.approx(v.x_to_time(near.x()), abs=1e-6)


def test_click_selects_without_moving_playhead(env):
    ids = three(env)
    v, doc = env.view, env.doc
    doc.seek(1.0)
    click(v, env.body_pt(8.0))                       # inside B (6..10)
    assert doc.selection == [ids[1]]
    assert doc.playhead == 1.0
    click(v, env.body_pt(13.0))
    assert doc.selection == [ids[2]]


def test_ctrl_click_multi_select_and_toggle(env):
    ids = three(env)
    v, doc = env.view, env.doc
    click(v, env.body_pt(2.0))
    click(v, env.body_pt(13.0), CTRL)
    assert doc.selection == [ids[0], ids[2]]
    click(v, env.body_pt(8.0), CTRL)
    assert set(doc.selection) == set(ids)
    click(v, env.body_pt(2.0), CTRL)
    assert set(doc.selection) == {ids[1], ids[2]}
    # plain click on a member of a multi-selection collapses it on release
    click(v, env.body_pt(13.0))
    assert doc.selection == [ids[2]]


def test_click_empty_track_clears_selection_and_seeks(env):
    three(env)
    v, doc = env.view, env.doc
    click(v, env.body_pt(2.0))
    assert doc.selection
    lane_y, clip_h, _, _ = v.lanes()
    click(v, QPoint(env.x(4.0), lane_y + clip_h + 20))
    assert doc.selection == []
    assert doc.playhead == pytest.approx(4.0, abs=1.5 / v.pixels_per_second)


# --------------------------------------------------------------------------- trim
def test_right_edge_trim_is_one_undo_step(env):
    add(env, env.h)
    v, doc = env.view, env.doc
    seg = doc.timeline.segments[0]
    before_end = seg.timeline_end
    undo0 = env.undo_count()
    a = QPoint(env.x(before_end) - 2, v.lanes()[0] + 40)
    b = QPoint(a.x() - 240, a.y())
    expected = before_end - 240 / v.pixels_per_second     # (the fitted zoom changes afterwards)
    drag(v, a, b, steps=12)
    seg = doc.timeline.segments[0]
    assert seg.timeline_end < before_end - 1.0
    assert seg.timeline_end == pytest.approx(expected, abs=0.05)
    assert env.undo_count() == undo0 + 1
    assert doc.history.undo_label() == "Trim clip"
    doc.undo()
    assert doc.timeline.segments[0].timeline_end == pytest.approx(before_end)
    assert env.undo_count() == undo0


def test_left_edge_trim_moves_source_in(env):
    add(env, env.h)
    v, doc = env.view, env.doc
    undo0 = env.undo_count()
    a = QPoint(env.x(0.0) + 2, v.lanes()[0] + 40)
    drag(v, a, QPoint(a.x() + 150, a.y()))
    seg = doc.timeline.segments[0]
    assert seg.source_in > 0.5 and seg.timeline_start == pytest.approx(seg.source_in, abs=1e-6)
    assert seg.source_out == pytest.approx(env.h.duration)
    assert env.undo_count() == undo0 + 1


def test_trim_without_movement_creates_no_undo_step(env):
    add(env, env.h)
    v = env.view
    undo0 = env.undo_count()
    a = QPoint(env.x(env.doc.duration) - 2, v.lanes()[0] + 40)
    press(v, a)
    release(v, a)
    assert env.undo_count() == undo0
    assert not v.is_dragging()


def test_trim_cannot_exceed_source(env):
    ids = three(env)
    v, doc = env.view, env.doc
    seg_b = doc.timeline.get(ids[1])
    # pull B's right edge far to the right: blocked by clip C starting right there
    a = QPoint(env.x(seg_b.timeline_end) - 2, v.lanes()[0] + 40)
    drag(v, a, QPoint(a.x() + 200, a.y()))
    doc.timeline.validate()
    assert doc.timeline.get(ids[1]).timeline_end <= doc.timeline.get(ids[2]).timeline_start + 1e-6


def test_escape_cancels_trim(env):
    add(env, env.h)
    v, doc = env.view, env.doc
    before = [s.copy() for s in doc.timeline.segments]
    undo0 = env.undo_count()
    a = QPoint(env.x(6.0) - 2, v.lanes()[0] + 40)
    drag(v, a, QPoint(a.x() - 200, a.y()), finish=False)
    assert v.is_dragging()
    assert doc.timeline.segments[0].timeline_end < 5.0
    QTest.keyClick(v, Qt.Key.Key_Escape)
    assert not v.is_dragging()
    assert doc.timeline.segments == before
    release(v, QPoint(a.x() - 200, a.y()))           # late release is harmless
    assert doc.timeline.segments == before
    assert env.undo_count() == undo0


def test_snap_trim_edge_to_playhead_and_alt_disables(env):
    add(env, env.h)
    v, doc = env.view, env.doc
    doc.seek(3.0)
    y = v.lanes()[0] + 40
    grab = QPoint(env.x(6.0) - 2, y)
    # target 6 px right of the playhead (snap tolerance is 8 px)
    target_x = env.x(3.0) + 6
    # compensate the grab offset so the *edge* lands where we want it
    dx = target_x - (v.time_to_x(6.0))
    drag(v, grab, QPoint(round(grab.x() + (target_x - v.time_to_x(6.0))), y))
    seg = doc.timeline.segments[0]
    assert seg.timeline_end == pytest.approx(3.0, abs=1e-6)
    assert dx < -100
    doc.undo()
    drag(v, grab, QPoint(round(grab.x() + (target_x - v.time_to_x(6.0))), y), mods=ALT)
    seg = doc.timeline.segments[0]
    assert seg.timeline_end > 3.0 + 0.5 / v.pixels_per_second
    doc.undo()
    env.panel.set_snapping(False)
    assert env.panel.snapping() is False and v.snapping() is False
    drag(v, grab, QPoint(round(grab.x() + (target_x - v.time_to_x(6.0))), y))
    assert doc.timeline.segments[0].timeline_end > 3.0 + 0.5 / v.pixels_per_second


def test_snap_trim_edge_to_other_clip_edge(env):
    ids = three(env)
    v, doc = env.view, env.doc
    # shrink B's right edge so a gap opens, then drag it back near C's start: must snap there
    seg_b = doc.timeline.get(ids[1])
    y = v.lanes()[0] + 40
    a = QPoint(env.x(seg_b.timeline_end) - 2, y)
    drag(v, a, QPoint(a.x() - 80, y), mods=ALT)
    shrunk_end = doc.timeline.get(ids[1]).timeline_end
    assert shrunk_end < 10.0 - 0.5
    a2 = QPoint(env.x(shrunk_end) - 2, y)
    drag(v, a2, QPoint(a2.x() + 80 - 4, y))               # ends 4 px short of C's start
    assert doc.timeline.get(ids[1]).timeline_end == pytest.approx(10.0, abs=1e-6)


# ------------------------------------------------------------------------ reorder
def test_body_drag_reorders_with_one_undo_step(env):
    a, b, c = three(env)
    v, doc = env.view, env.doc
    undo0 = env.undo_count()
    y = v.lanes()[0] + 40
    drag(v, QPoint(env.x(3.0), y), QPoint(env.x(15.0), y), steps=15)
    assert env.ids() == [b, c, a]
    doc.timeline.validate()
    assert env.undo_count() == undo0 + 1
    assert doc.history.undo_label() == "Move clip"
    doc.undo()
    assert env.ids() == [a, b, c]


def test_body_drag_back_to_same_place_is_noop(env):
    a, b, c = three(env)
    v = env.view
    undo0 = env.undo_count()
    y = v.lanes()[0] + 40
    drag(v, QPoint(env.x(3.0), y), QPoint(env.x(3.0) + 30, y))
    assert env.ids() == [a, b, c]
    assert env.undo_count() == undo0


def test_small_body_move_is_a_click_not_a_drag(env):
    a, b, c = three(env)
    v = env.view
    undo0 = env.undo_count()
    y = v.lanes()[0] + 40
    drag(v, QPoint(env.x(3.0), y), QPoint(env.x(3.0) + 2, y))
    assert env.ids() == [a, b, c] and env.undo_count() == undo0
    assert env.doc.selection == [a]


def test_escape_cancels_reorder(env):
    a, b, c = three(env)
    v = env.view
    undo0 = env.undo_count()
    y = v.lanes()[0] + 40
    drag(v, QPoint(env.x(3.0), y), QPoint(env.x(15.0), y), finish=False)
    assert v.is_dragging()
    v.grab()                                        # paints ghost + insertion marker
    QTest.keyClick(v, Qt.Key.Key_Escape)
    assert not v.is_dragging()
    release(v, QPoint(env.x(15.0), y))
    assert env.ids() == [a, b, c] and env.undo_count() == undo0


def test_escape_cancels_scrub(env):
    three(env)
    v, doc = env.view, env.doc
    doc.seek(2.0)
    press(v, env.ruler_pt(9.0))
    move(v, env.ruler_pt(11.0))
    QTest.keyClick(v, Qt.Key.Key_Escape)
    assert doc.playhead == pytest.approx(2.0)
    assert not v.is_dragging()


def test_other_keys_are_not_consumed(env):
    three(env)
    v = env.view
    seen = []
    ev = QTest  # noqa: F841  (keep QTest import used)
    from PySide6.QtGui import QKeyEvent
    key = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, NOMOD)
    v.keyPressEvent(key)
    seen.append(key.isAccepted())
    esc = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, NOMOD)
    v.keyPressEvent(esc)                             # not dragging: not consumed
    seen.append(esc.isAccepted())
    assert seen == [False, False]


# ------------------------------------------------------------- robustness in drags
def test_undo_during_trim_drag_aborts_cleanly(env):
    add(env, env.h)                                  # history: [Add clip]
    v, doc = env.view, env.doc
    a = QPoint(env.x(6.0) - 2, v.lanes()[0] + 40)
    drag(v, a, QPoint(a.x() - 100, a.y()), finish=False)
    assert v.is_dragging()
    assert doc.undo()                                # clip vanishes under the drag
    assert doc.timeline.is_empty()
    assert not v.is_dragging()
    move(v, QPoint(a.x() - 150, a.y()))
    release(v, QPoint(a.x() - 150, a.y()))
    v.grab()
    assert doc.timeline.is_empty()


def test_undo_during_trim_drag_where_clip_survives(env):
    ids = three(env)
    v, doc = env.view, env.doc
    a = QPoint(env.x(10.0) - 2, v.lanes()[0] + 40)   # B's right edge
    drag(v, a, QPoint(a.x() - 60, a.y()), finish=False)
    assert doc.undo()                                # removes clip C; B still exists
    move(v, QPoint(a.x() - 90, a.y()))
    release(v, QPoint(a.x() - 90, a.y()))
    assert not v.is_dragging()
    doc.timeline.validate()
    v.grab()


def test_undo_during_reorder_drag(env):
    ids = three(env)
    v, doc = env.view, env.doc
    y = v.lanes()[0] + 40
    drag(v, QPoint(env.x(13.0), y), QPoint(env.x(1.0), y), finish=False)   # lift C
    assert doc.undo()                                # C removed while lifted
    assert not v.is_dragging()
    v.grab()
    release(v, QPoint(env.x(1.0), y))
    assert env.ids() == ids[:2]


def test_new_project_during_drags(env):
    ids = three(env)
    v, doc = env.view, env.doc
    y = v.lanes()[0] + 40
    for start, end in ((env.x(13.0), env.x(1.0)), (env.x(10.0) - 2, env.x(8.0))):
        drag(v, QPoint(start, y), QPoint(end, y), finish=False)
        assert v.is_dragging()
        doc.new_project()
        assert not v.is_dragging()
        move(v, QPoint(end + 20, y))
        release(v, QPoint(end + 20, y))
        v.grab()
        assert doc.timeline.is_empty()
        three(env)
    press(v, env.ruler_pt(5.0))
    doc.new_project()
    release(v, env.ruler_pt(7.0))
    v.grab()


def test_delete_clip_during_reorder_drag(env):
    a, b, c = three(env)
    v, doc = env.view, env.doc
    y = v.lanes()[0] + 40
    drag(v, QPoint(env.x(13.0), y), QPoint(env.x(2.0), y), finish=False)
    doc.select([c])
    doc.delete_selected(ripple=True)
    assert not v.is_dragging()
    release(v, QPoint(env.x(2.0), y))
    assert env.ids() == [a, b]


def test_middle_button_pan(env):
    three(env)
    v = env.view
    v.set_zoom(250.0)
    v.set_scroll(0.0)
    mouse(v, QEvent.Type.MouseButtonPress, QPoint(500, 100), button=Qt.MouseButton.MiddleButton)
    mouse(v, QEvent.Type.MouseMove, QPoint(300, 100))
    assert v.scroll_offset == pytest.approx(200.0)
    mouse(v, QEvent.Type.MouseButtonRelease, QPoint(300, 100), button=Qt.MouseButton.MiddleButton)
    assert not v.is_dragging()


# ----------------------------------------------------------------------- toolbar
def test_toolbar_enabled_state_follows_selection_and_history(env):
    p, doc = env.panel, env.doc
    for b in (p.undo_button, p.redo_button, p.split_button, p.delete_button,
              p.ripple_button, p.mute_button, p.gaps_button, p.fit_button):
        assert not b.isEnabled()
    add(env, env.h, env.v)                           # last added clip is selected
    assert p.undo_button.isEnabled() and not p.redo_button.isEnabled()
    assert p.delete_button.isEnabled() and p.ripple_button.isEnabled() and p.mute_button.isEnabled()
    assert p.fit_button.isEnabled()
    assert not p.gaps_button.isEnabled()
    doc.seek(1.0)
    assert p.split_button.isEnabled()                # playhead over a clip
    doc.clear_selection()
    assert not p.delete_button.isEnabled() and not p.ripple_button.isEnabled()
    assert not p.mute_button.isEnabled()
    assert p.split_button.isEnabled()                # split does not need a selection
    doc.undo()
    assert p.redo_button.isEnabled()
    doc.redo()
    assert not p.redo_button.isEnabled()
    doc.select([doc.timeline.segments[0].id])
    doc.delete_selected(ripple=False)                # leaves a gap
    assert p.gaps_button.isEnabled()
    p.gaps_button.click()
    assert not p.gaps_button.isEnabled()
    doc.new_project()
    assert not p.undo_button.isEnabled() and not p.delete_button.isEnabled()


def test_split_disabled_when_playhead_in_gap(env):
    ids = three(env)
    doc, p = env.doc, env.panel
    doc.select([ids[1]])
    doc.delete_selected(ripple=False)                # gap 6..10
    doc.seek(8.0)
    assert not p.split_button.isEnabled()
    doc.seek(3.0)
    assert p.split_button.isEnabled()


def test_toolbar_buttons_perform_edits(env):
    a, b, c = three(env)
    p, doc = env.panel, env.doc
    doc.seek(3.0)
    doc.select([a])
    p.split_button.click()
    assert len(doc.timeline.segments) == 4
    assert doc.timeline.segments[0].timeline_end == pytest.approx(3.0, abs=0.04)
    assert doc.selection == [doc.timeline.segments[1].id]

    p.mute_button.click()
    assert doc.timeline.segments[1].muted
    assert p.mute_button.toolTip() == "Unmute clip"
    p.mute_button.click()
    assert not doc.timeline.segments[1].muted
    assert p.mute_button.toolTip() == "Mute clip"

    dur = doc.duration
    right = doc.timeline.segments[1]
    p.ripple_button.click()
    assert len(doc.timeline.segments) == 3
    assert doc.duration == pytest.approx(dur - right.duration)
    assert not doc.timeline.gaps()

    doc.select([doc.timeline.segments[0].id])
    p.delete_button.click()
    assert len(doc.timeline.segments) == 2
    assert doc.timeline.gaps()

    p.gaps_button.click()
    assert not doc.timeline.gaps()

    n = len(doc.history.undo_stack)
    p.undo_button.click()
    assert len(doc.history.undo_stack) == n - 1 and doc.timeline.gaps()
    assert p.redo_button.isEnabled()
    p.redo_button.click()
    assert not doc.timeline.gaps() and len(doc.history.undo_stack) == n


def test_tooltips_name_the_undo_action(env):
    add(env, env.h)
    assert "add clip" in env.panel.undo_button.toolTip().lower()
    env.doc.undo()
    assert "add clip" in env.panel.redo_button.toolTip().lower()


def test_info_label(env):
    ids = three(env)
    p, doc = env.panel, env.doc
    doc.clear_selection()
    assert p.info_label.text() == "No clip selected"
    doc.select([ids[0]])
    assert "duration" in p.info_label.text() and "clips" not in p.info_label.text()
    doc.select(ids[:2])
    assert p.info_label.text().startswith("2 clips")


def test_snap_button_toggles_view(env):
    p = env.panel
    seen = []
    p.snappingChanged.connect(seen.append)
    p.snap_button.click()
    assert p.snapping() is False and seen == [False]
    p.set_snapping(True)
    assert p.snapping() is True and seen == [False, True]


# ------------------------------------------------------------------- context menu
def _actions(menu):
    return {a.text().split("\t")[0]: a for a in menu.actions() if not a.isSeparator()}


def test_context_menu_actions(env):
    a, b, c = three(env)
    doc, v = env.doc, env.view
    doc.clear_selection()
    doc.seek(3.0)
    menu = v.build_context_menu(a)
    assert doc.selection == [a]                      # selected first
    acts = _actions(menu)
    assert set(acts) == {"Split at playhead", "Detach audio", "Export audio only…", "Delete",
                         "Ripple delete", "Mute"}
    assert acts["Split at playhead"].isEnabled()
    acts["Split at playhead"].trigger()
    assert len(doc.timeline.segments) == 4

    segs = doc.timeline.segments
    menu = v.build_context_menu(segs[3].id)          # playhead (3 s) is not inside it
    assert not _actions(menu)["Split at playhead"].isEnabled()
    _actions(menu)["Mute"].trigger()
    assert doc.timeline.get(segs[3].id).muted
    menu = v.build_context_menu(segs[3].id)
    assert "Unmute" in _actions(menu)
    _actions(menu)["Unmute"].trigger()
    assert not doc.timeline.get(segs[3].id).muted

    dur = doc.duration
    last = segs[3]
    _actions(v.build_context_menu(last.id))["Ripple delete"].trigger()
    assert doc.duration == pytest.approx(dur - last.duration)
    first = doc.timeline.segments[0].id
    _actions(v.build_context_menu(first))["Delete"].trigger()
    assert first not in env.ids() and doc.timeline.gaps()


def test_context_menu_keeps_existing_multi_selection(env):
    a, b, c = three(env)
    env.doc.select([a, c])
    env.view.build_context_menu(c)
    assert env.doc.selection == [a, c]


# ----------------------------------------------------------------------- painting
def _grab_ok(widget):
    img = widget.grab().toImage()
    assert not img.isNull() and img.width() > 0
    return img


def test_paint_empty(env):
    img = _grab_ok(env.panel)
    assert img.width() == env.panel.width()
    env.view.set_zoom(50.0)
    _grab_ok(env.view)


def test_paint_single_clip_selected_and_playhead(env):
    add(env, env.h)
    env.doc.seek(2.5)
    img = _grab_ok(env.view)
    # the playhead line is drawn in the playhead colour somewhere below the ruler
    x = env.x(2.5)
    col = QColor(img.pixel(x, env.view.height() - 5))
    assert col == QColor(theme.current().playhead)


def test_paint_many_clips_all_zooms(env):
    add(env, env.h)
    segs, t = [], 0.0
    for i in range(200):
        s = seg_for(env.h if i % 2 else env.v, 0.5, 0.5 + 0.2 + (i % 7) * 0.1, t, muted=i % 5 == 0)
        segs.append(s)
        t = s.timeline_end + (0.05 if i % 3 == 0 else 0.0)
    env.doc.sources[env.v.id] = env.v
    env.doc.timeline.segments[:] = segs
    env.doc.timelineChanged.emit()
    env.doc.select([s.id for s in segs[::10]])
    pump(env.app)
    v = env.view
    _grab_ok(v)                                       # fitted: ~1000 clips narrower than a pixel
    for pps in (v.min_pps() * 3, 100.0, v.max_pps()):
        v.set_zoom(pps)
        for scroll in (0.0, v.max_scroll() / 2, v.max_scroll()):
            v.set_scroll(scroll)
            _grab_ok(v)
    env.panel.scroll_to_playhead()
    _grab_ok(env.panel)


def test_paint_muted_and_offline_and_missing_source(env):
    add(env, env.h)
    ghost = MediaSource(id="gone", path=os.path.join(os.getcwd(), "definitely-missing.mp4"),
                        duration=5.0, width=640, height=360, fps=30.0, has_audio=True)
    env.doc.sources[ghost.id] = ghost
    orphan = seg_for(ghost, 0, 5.0, 6.0)
    nosrc = Segment("orphan", "no-such-source", 0.0, 3.0, 11.0)
    muted = seg_for(env.h, 0.0, 2.0, 14.0, muted=True)
    env.doc.timeline.segments.extend([orphan, nosrc, muted])
    env.doc.timelineChanged.emit()
    env.doc.select([orphan.id])
    pump(env.app)
    _grab_ok(env.view)
    env.panel.refresh_media_status()
    env.view.set_zoom(env.view.max_pps())
    for t in (0.0, 7.0, 12.0, 14.5):
        env.view.set_scroll(env.view.time_to_x(t) + env.view.scroll_offset - 100)
        _grab_ok(env.view)
    # offline clips are tinted differently from healthy ones
    env.view.zoom_to_fit()
    img = _grab_ok(env.view)
    y = env.view.lanes()[0] + 5
    healthy = QColor(img.pixel(env.x(3.0), y))
    offline = QColor(img.pixel(env.x(8.0), y))
    assert healthy != offline


@pytest.mark.parametrize("thumb_mode", ["image", "none", "raise"])
@pytest.mark.parametrize("wave_mode", ["peaks", "none", "empty", "raise"])
def test_paint_with_degraded_providers(env, thumb_mode, wave_mode):
    env.thumbs.mode, env.waves.mode = thumb_mode, wave_mode
    three(env)
    _grab_ok(env.view)
    env.thumbs.thumbnailReady.emit(env.h.id, 1.0)
    env.waves.waveformReady.emit(env.h.id)
    env.thumbs.thumbnailReady.emit("unknown", 1.0)
    pump(env.app)
    _grab_ok(env.panel)


def test_provider_signals_and_cancel_on_zoom(env):
    three(env)
    before = env.thumbs.cancels
    env.view.zoom_by(1.5)
    assert env.thumbs.cancels > before
    gets = env.thumbs.gets
    _grab_ok(env.view)
    assert env.thumbs.gets > gets


def test_none_providers_are_tolerated(app, video_h):
    doc = Document()
    panel = TimelinePanel(doc, None, None)
    panel.resize(800, 300)
    panel.show()
    doc.add_source(video_h)
    pump(app)
    _grab_ok(panel)
    panel.zoom_in()
    _grab_ok(panel)
    panel.close()


def test_theme_refresh_and_resize(env):
    three(env)
    env.panel.refresh_theme()
    for size in ((300, 140), (1600, 500), (400, 112)):
        env.panel.resize(*size)
        pump(env.app)
        _grab_ok(env.panel)
        assert env.view.time_to_x(env.doc.duration) <= env.view.width() + 1   # still fitted


def test_follow_playhead_scrolls_view(env):
    three(env)
    v = env.view
    v.set_zoom(300.0)
    v.set_scroll(0.0)
    v._last_user_scroll = -1e9
    env.doc.set_playhead(14.0)
    assert v.scroll_offset > 0
    assert 0 <= v.time_to_x(14.0) <= v.width()
    env.panel.follow_playhead = False
    v.set_scroll(0.0)
    env.doc.set_playhead(15.0)
    assert v.scroll_offset == 0.0


# ------------------------------------------------------- slide a clip into a gap
def test_drag_single_clip_left_into_leading_gap_snaps_to_start(env):
    a, b, c = three(env)
    v, doc = env.view, env.doc
    doc.select([a, b])
    doc.delete_selected(ripple=False)                    # leaves C alone at 10-15
    assert doc.timeline.segments[0].timeline_start == pytest.approx(10.0)
    undo0 = env.undo_count()
    y = v.lanes()[0] + 40
    # drag C left by (almost) its distance from 0: ends 4 px short of time 0, so it snaps there
    start_x = env.x(12.0)
    end_x = start_x - (v.time_to_x(10.0) - v.time_to_x(0.0)) + 4
    drag(v, QPoint(start_x, y), QPoint(round(end_x), y), steps=15)
    seg = doc.timeline.segments[0]
    assert seg.timeline_start == pytest.approx(0.0, abs=1e-6)
    assert env.undo_count() == undo0 + 1
    doc.undo()
    assert doc.timeline.segments[0].timeline_start == pytest.approx(10.0)


# ------------------------------------------------------------------ audio lane
def detached(e):
    """One 6 s clip with its audio detached; returns (video_id, audio_id)."""
    add(e, e.h)
    pump(e.app)
    vid = e.ids()[0]
    e.doc.select([vid])
    assert tl.can_detach_audio(e.doc)
    assert e.doc.detach_audio()
    pump(e.app)
    return vid, e.doc.timeline.audio[0].id


def audio_pt(e, t, dy=0):
    top, h = e.view.audio_lane()
    return QPoint(e.x(t), top + h // 2 + dy)


def test_audio_lane_hidden_until_detach_then_rendered(env):
    add(env, env.h)
    pump(env.app)
    v = env.view
    assert v.audio_lane() is None
    base_min = v.minimumHeight()
    vid, aid = detached(env)
    top, h = v.audio_lane()
    vy, vh, _, _ = v.lanes()
    assert top >= vy + vh and top + h <= v.height()
    assert v.minimumHeight() > base_min
    assert env.doc.selection == [aid]
    img = _grab_ok(env.view)
    pal = theme.current()
    px = QColor(img.pixelColor(env.x(3.0), top + 4))
    assert px != QColor(pal.track_bg) and px != QColor(pal.panel_alt)
    env.doc.undo()
    pump(env.app)
    assert v.audio_lane() is None
    assert v.minimumHeight() == base_min


def test_detach_button_and_state(env):
    add(env, env.h)
    pump(env.app)
    btn = env.panel.detach_button
    env.doc.clear_selection()
    env.doc.seek(1.0)
    pump(env.app)
    assert btn.isEnabled()                       # clip under the playhead
    btn.click()
    pump(env.app)
    assert len(env.doc.timeline.audio) == 1
    assert not btn.isEnabled()                   # already detached
    assert env.doc.timeline.segments[0].muted
    env.doc.undo()
    pump(env.app)
    assert not env.doc.timeline.audio and btn.isEnabled()


def test_audio_hit_test_selection_and_background_seek(env):
    vid, aid = detached(env)
    v, doc = env.view, env.doc
    seg, zone = v.hit_test(audio_pt(env, 3.0))
    assert seg is not None and seg.id == aid and zone == "body"
    assert v.hit_test(env.body_pt(3.0))[0].id == vid
    seg, zone = v.hit_test(audio_pt(env, 0.0))
    assert seg.id == aid and zone == "left"
    assert v.hit_test(audio_pt(env, 6.0))[1] == "right"
    doc.clear_selection()
    click(v, audio_pt(env, 3.0))
    assert doc.selection == [aid]
    click(v, env.body_pt(3.0), CTRL)
    assert set(doc.selection) == {aid, vid}
    click(v, env.body_pt(3.0), CTRL)
    assert doc.selection == [aid]
    # the audio lane background beyond the clip clears the selection and seeks
    doc.trim_end(aid, 3.0)
    pump(env.app)
    pt = audio_pt(env, 4.5)
    click(v, pt, ALT)
    assert doc.selection == []
    assert doc.playhead == pytest.approx(v.x_to_time(pt.x()), abs=0.05)


def test_audio_trim_handles_single_undo_step(env):
    vid, aid = detached(env)
    v, doc = env.view, env.doc
    undo0 = env.undo_count()
    drag(v, audio_pt(env, 6.0), audio_pt(env, 4.0), mods=ALT, steps=8)
    assert doc.timeline.get(aid).timeline_end == pytest.approx(4.0, abs=0.1)
    assert doc.timeline.segments[0].timeline_end == pytest.approx(6.0)
    assert env.undo_count() == undo0 + 1
    drag(v, audio_pt(env, 0.0), audio_pt(env, 1.0), mods=ALT, steps=8)
    assert doc.timeline.get(aid).timeline_start == pytest.approx(1.0, abs=0.1)
    assert env.undo_count() == undo0 + 2
    doc.undo()
    doc.undo()
    assert doc.timeline.audio[0].timeline_start == 0.0
    assert doc.timeline.audio[0].timeline_end == pytest.approx(6.0)


def test_audio_trim_esc_restores(env):
    vid, aid = detached(env)
    v, doc = env.view, env.doc
    undo0 = env.undo_count()
    drag(v, audio_pt(env, 6.0), audio_pt(env, 3.0), steps=5, finish=False)
    assert v.is_dragging()
    v.cancel_drag()
    assert doc.timeline.get(aid).timeline_end == pytest.approx(6.0)
    assert env.undo_count() == undo0


def test_audio_clip_slides_with_neighbour_clamp_and_snap(env):
    vid, aid = detached(env)
    v, doc = env.view, env.doc
    doc.trim_end(aid, 3.0)
    pump(env.app)
    undo0 = env.undo_count()
    drag(v, audio_pt(env, 1.5), audio_pt(env, 3.5), mods=ALT, steps=10)
    a = doc.timeline.get(aid)
    assert a.timeline_start == pytest.approx(2.0, abs=0.1)
    assert a.timeline_end == pytest.approx(5.0, abs=0.1)
    assert env.undo_count() == undo0 + 1
    assert doc.timeline.segments[0].timeline_start == 0.0
    # sliding left clamps at 0 (and snaps to it)
    drag(v, audio_pt(env, 3.5), audio_pt(env, 0.2), steps=10)
    assert doc.timeline.get(aid).timeline_start == pytest.approx(0.0, abs=1e-6)
    doc.undo()
    assert doc.timeline.get(aid).timeline_start == pytest.approx(2.0, abs=0.1)
    # an audio clip never reorders past a neighbour
    doc.seek(3.0)
    doc.select([aid])
    assert doc.split_at_playhead()
    pump(env.app)
    first, second = doc.timeline.audio
    mid = (second.timeline_start + second.timeline_end) / 2
    drag(v, audio_pt(env, mid), audio_pt(env, -3.0), mods=ALT, steps=10)
    assert [s.id for s in doc.timeline.audio] == [first.id, second.id]
    assert doc.timeline.get(second.id).timeline_start >= first.timeline_end - 1e-6


def test_audio_slide_esc_does_nothing(env):
    vid, aid = detached(env)
    v, doc = env.view, env.doc
    doc.trim_end(aid, 3.0)
    undo0 = env.undo_count()
    drag(v, audio_pt(env, 1.0), audio_pt(env, 4.0), steps=6, finish=False)
    v.cancel_drag()
    assert doc.timeline.get(aid).timeline_start == 0.0
    assert env.undo_count() == undo0


def test_audio_drag_survives_clip_vanishing(env):
    vid, aid = detached(env)
    v, doc = env.view, env.doc
    drag(v, audio_pt(env, 1.0), audio_pt(env, 2.0), steps=3, finish=False)
    doc.undo()
    pump(env.app)
    assert not v.is_dragging()
    release(v, QPoint(env.x(2.0), 100))


def test_audio_trim_snaps_to_video_edge(env):
    add(env, env.h, env.v)
    pump(env.app)
    env.doc.select([env.ids()[0]])
    env.doc.detach_audio()
    pump(env.app)
    aid = env.doc.timeline.audio[0].id
    env.doc.trim_end(aid, 5.0)
    y = audio_pt(env, 5.0).y()
    drag(env.view, QPoint(env.x(5.0), y), QPoint(env.x(6.0) - 3, y), steps=6)
    assert env.doc.timeline.get(aid).timeline_end == pytest.approx(6.0, abs=0.04)


def test_audio_delete_split_mute_and_context_menu(env):
    vid, aid = detached(env)
    v, doc = env.view, env.doc
    doc.select([aid])
    doc.seek(2.0)
    assert env.panel.split_button.isEnabled()
    env.panel.split_button.click()
    assert len(doc.timeline.audio) == 2 and len(doc.timeline.segments) == 1
    sel = list(doc.selection)
    env.panel.mute_button.click()
    assert all(doc.timeline.get(i).muted for i in sel)
    menu = v.build_context_menu(sel[0])
    labels = [a.text().split("\t")[0] for a in menu.actions() if a.text()]
    assert "Detach audio" not in labels
    menu.deleteLater()
    env.panel.ripple_button.click()
    assert len(doc.timeline.audio) == 1
    doc.select([doc.timeline.audio[0].id])
    env.panel.delete_button.click()
    assert doc.timeline.audio == [] and v.audio_lane() is None
    assert len(doc.timeline.segments) == 1


def test_audio_lane_scroll_extent_uses_full_duration(env):
    vid, aid = detached(env)
    doc, v = env.doc, env.view
    doc.move_clip(aid, 4.0)
    pump(env.app)
    assert doc.duration == pytest.approx(10.0)
    assert v.content_width() == pytest.approx(tl.LEFT_PAD + 10.0 * v.pixels_per_second + tl.RIGHT_PAD)
    assert v.time_to_x(10.0) <= v.width()


def test_audio_lane_paint_variants(env):
    vid, aid = detached(env)
    for mode in ("peaks", "none", "empty", "raise"):
        env.waves.mode = mode
        _grab_ok(env.panel)
    env.waves.mode = "peaks"
    env.doc.set_muted([aid], True)
    env.view.set_zoom(300.0)
    _grab_ok(env.panel)
    env.panel.refresh_theme()
    for size in ((300, 200), (1200, 420)):
        env.panel.resize(*size)
        pump(env.app)
        _grab_ok(env.panel)
