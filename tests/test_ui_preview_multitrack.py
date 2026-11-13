"""Preview of multi-track timelines: overlay geometry, handle hit-testing and drags, compositing.

No media backend is needed: pictures are injected into the viewport while the Document, clips
and undo stack are real (the source files do not exist, so no players are created).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipforge.core.model import POS_LIMIT, SCALE_MAX, SCALE_MIN, MediaSource  # noqa: E402
from clipforge.document import Document  # noqa: E402
from clipforge.ui import preview as pv  # noqa: E402
from clipforge.ui import theme  # noqa: E402


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication([])
    theme.apply_theme(inst, "dark")
    return inst


def solid(color: str, w=320, h=180) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    return img


def px(img: QImage, x: int, y: int) -> tuple[int, int, int]:
    c = img.pixelColor(int(x), int(y))
    return c.red(), c.green(), c.blue()


# ------------------------------------------------------------------------ pure geometry
def test_overlay_rect_default_is_canvas_wide_and_centred():
    canvas = QRectF(100, 50, 400, 300)
    r = pv.overlay_rect(canvas, 16 / 9, 0.0, 0.0, 1.0)
    assert r.width() == pytest.approx(400)
    assert r.height() == pytest.approx(400 * 9 / 16)
    assert r.center().x() == pytest.approx(canvas.center().x())
    assert r.center().y() == pytest.approx(canvas.center().y())


def test_overlay_rect_scale_position_and_aspect():
    canvas = QRectF(0, 0, 900, 1600)                       # 9:16 canvas
    r = pv.overlay_rect(canvas, 1.0, 0.25, -0.1, 0.5)      # square clip, half width
    assert r.width() == pytest.approx(450) and r.height() == pytest.approx(450)
    assert r.center().x() == pytest.approx(900 * 0.75)
    assert r.center().y() == pytest.approx(1600 * 0.4)


def test_overlay_rect_may_leave_the_canvas():
    r = pv.overlay_rect(QRectF(0, 0, 100, 100), 1.0, 1.0, 1.0, 1.0)
    assert r.left() == pytest.approx(100) and r.right() == pytest.approx(200)


def test_source_aspect_falls_back_to_16_9():
    assert pv.source_aspect(None) == pytest.approx(16 / 9)
    assert pv.source_aspect(MediaSource(id="a", path="x", duration=1, width=1080, height=1920)) \
        == pytest.approx(9 / 16)
    assert pv.source_aspect(MediaSource(id="a", path="x", duration=1)) == pytest.approx(16 / 9)


# ------------------------------------------------------------------------ hit testing
def test_hit_test_corners_body_and_outside():
    r = QRectF(100, 100, 200, 100)
    assert pv.hit_test_overlay(r, QPointF(101, 102)) == "tl"
    assert pv.hit_test_overlay(r, QPointF(299, 101)) == "tr"
    assert pv.hit_test_overlay(r, QPointF(103, 198)) == "bl"
    assert pv.hit_test_overlay(r, QPointF(300, 200)) == "br"
    assert pv.hit_test_overlay(r, QPointF(200, 150)) == "body"
    assert pv.hit_test_overlay(r, QPointF(50, 50)) is None
    assert pv.hit_test_overlay(r, QPointF(310 + pv.HANDLE_HIT, 150)) is None


def test_corner_wins_over_body_on_tiny_frames():
    r = QRectF(100, 100, 6, 6)
    assert pv.hit_test_overlay(r, QPointF(103, 103)) in pv.CORNERS


# ------------------------------------------------------------------------ drag results
def test_moved_position_is_relative_and_clamped():
    canvas = QRectF(0, 0, 400, 200)
    assert pv.moved_position(0.0, 0.0, 100, -50, canvas) == pytest.approx((0.25, -0.25))
    assert pv.moved_position(0.9, 0.9, 1000, 1000, canvas) == (POS_LIMIT, POS_LIMIT)
    assert pv.moved_position(-0.9, -0.9, -1000, -1000, canvas) == (-POS_LIMIT, -POS_LIMIT)
    assert pv.moved_position(0.3, 0.2, 10, 10, QRectF()) == (0.3, 0.2)


def test_resized_scale_follows_distance_from_centre_and_clamps():
    r = pv.overlay_rect(QRectF(0, 0, 400, 400), 1.0, 0, 0, 0.5)       # 200x200 at the centre
    br = pv.corner_point(r, "br")
    assert br == QPointF(300, 300)
    assert pv.resized_scale(0.5, r, "br", br) == pytest.approx(0.5)
    far = QPointF(200 + 200, 200 + 200)                               # twice as far
    assert pv.resized_scale(0.5, r, "br", far) == pytest.approx(1.0)
    near = QPointF(250, 250)
    assert pv.resized_scale(0.5, r, "br", near) == pytest.approx(0.25)
    assert pv.resized_scale(0.5, r, "tl", QPointF(200, 200)) == SCALE_MIN
    assert pv.resized_scale(0.5, r, "tl", QPointF(-5000, -5000)) == SCALE_MAX


# ------------------------------------------------------------------------------- rig
class _Settings:
    volume = 1.0

    def save(self):
        pass


class Rig:
    def __init__(self, app, aspect="16:9", with_v1=True):
        self.doc = Document()
        self.v1 = MediaSource(id="v1", path="C:/nope/v1.mp4", duration=6.0, fps=30.0,
                              width=1920, height=1080)
        self.ov = MediaSource(id="ov", path="C:/nope/ov.mp4", duration=4.0, fps=30.0,
                              width=1920, height=1080)
        if with_v1:
            self.doc.add_source(self.v1)
        else:
            self.doc.add_source(self.v1, to_timeline=False)
        self.doc.add_source(self.ov, track=1, at=1.0)
        self.doc.set_canvas(aspect, "fill")
        self.doc.history.clear()
        self.overlay = self.doc.timeline.track(1)[0]
        self.panel = pv.PreviewPanel(self.doc, _Settings())
        self.panel._resync_timer.stop()
        self.panel._enter = lambda t: None            # no media engine: pictures are injected
        self.panel.resize(900, 600)
        self.panel.show()
        app.processEvents()
        self.panel._resync_timer.stop()
        self.vp = self.panel._viewport
        self.doc.seek(2.0)
        self.doc.select([self.overlay.id])
        self.panel._refresh_overlays()
        self.vp.show_frame(solid("#ff0000"), self.doc.timeline.segments[0] if with_v1 else None, 0.0) \
            if with_v1 else self.vp.show_black()
        self.set_overlay_image(solid("#0000ff"))

    def set_overlay_image(self, img):
        items = [(s, img, a) for s, _i, a in self.vp.overlays()]
        self.vp.set_overlays(items, self.panel._selected_overlay_id(), self.panel._canvas_hint())

    def close(self):
        self.panel.shutdown()
        self.panel.close()

    def frame(self) -> QRectF:
        return self.vp.overlay_frame()

    def drag(self, p0: QPointF, p1: QPointF, release=True):
        QTest.mousePress(self.vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0.toPoint())
        QTest.mouseMove(self.vp, p1.toPoint())
        if release:
            QTest.mouseRelease(self.vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                               p1.toPoint())

    def seg(self):
        return self.doc.timeline.get(self.overlay.id)


@pytest.fixture()
def rig(app):
    r = Rig(app)
    yield r
    r.close()


def test_overlay_is_selected_and_framed(rig):
    assert rig.vp.transform_active()
    comp = rig.vp.compose_rect()
    f = rig.frame()
    assert comp is not None and f is not None
    assert f.width() == pytest.approx(comp.width())             # default: canvas wide


def test_no_handles_when_not_selected_or_not_under_playhead(rig):
    rig.doc.clear_selection()
    assert not rig.vp.transform_active()
    rig.doc.select([rig.overlay.id])
    assert rig.vp.transform_active()
    rig.doc.seek(0.5)                                            # before the overlay starts
    rig.panel._refresh_overlays()
    assert not rig.vp.transform_active()


def test_no_handles_for_v1_clip(rig):
    rig.doc.select([rig.doc.timeline.segments[0].id])
    assert not rig.vp.transform_active()


def test_body_drag_moves_and_is_one_undo_step(rig):
    rig.doc.set_transform([rig.overlay.id], scale=0.5)
    rig.doc.history.clear()
    rig.panel._refresh_overlays()
    rig.set_overlay_image(solid("#0000ff"))
    f = rig.frame()
    comp = rig.vp.compose_rect()
    start = f.center()
    rig.drag(start, start + QPointF(comp.width() * 0.2, comp.height() * 0.1))
    s = rig.seg()
    assert s.pos_x == pytest.approx(0.2, abs=0.02) and s.pos_y == pytest.approx(0.1, abs=0.02)
    assert rig.doc.can_undo()
    rig.doc.undo()
    assert rig.seg().pos_x == 0.0 and rig.seg().pos_y == 0.0
    assert rig.seg().scale == pytest.approx(0.5)
    assert not rig.doc.can_undo()                          # exactly one step was recorded


def test_corner_drag_resizes_keeping_aspect_and_centre(rig):
    rig.doc.set_transform([rig.overlay.id], scale=0.5)
    rig.doc.history.clear()
    rig.panel._refresh_overlays()
    rig.set_overlay_image(solid("#0000ff"))
    f = rig.frame()
    br = pv.corner_point(f, "br")
    c = f.center()
    rig.drag(br - QPointF(1, 1), c + (br - c) * 1.5)
    s = rig.seg()
    assert s.scale == pytest.approx(0.75, abs=0.03)
    assert (s.pos_x, s.pos_y) == (0.0, 0.0)
    assert rig.doc.can_undo()


def test_drag_is_clamped_by_the_model_limits(rig):
    f = rig.frame()
    comp = rig.vp.compose_rect()
    rig.drag(f.center(), f.center() + QPointF(comp.width() * 20, 0))
    assert rig.seg().pos_x == pytest.approx(POS_LIMIT)
    c = rig.frame().center()
    rig.drag(pv.corner_point(rig.frame(), "br") - QPointF(1, 1), c + QPointF(0.5, 0.5))
    assert rig.seg().scale >= SCALE_MIN


def test_escape_cancels_the_drag_and_restores_the_clip(rig):
    f = rig.frame()
    rig.drag(f.center(), f.center() + QPointF(60, 20), release=False)
    assert rig.vp.is_transforming() and rig.seg().pos_x != 0.0
    QTest.keyClick(rig.vp, Qt.Key.Key_Escape)
    assert not rig.vp.is_transforming()
    assert rig.seg().pos_x == 0.0 and rig.seg().pos_y == 0.0
    assert not rig.doc.can_undo()


def test_drag_survives_the_clip_disappearing(rig):
    f = rig.frame()
    rig.drag(f.center(), f.center() + QPointF(40, 0), release=False)
    rig.doc.delete_selected()
    rig.panel._refresh_overlays()
    assert not rig.vp.is_transforming()
    QTest.mouseRelease(rig.vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       f.center().toPoint())                    # must not raise


def test_handles_hidden_and_drag_blocked_while_playing(rig):
    rig.vp.set_interactive(False)
    assert not rig.vp.transform_active()
    f = rig.frame()
    rig.drag(f.center(), f.center() + QPointF(50, 0))
    assert rig.seg().pos_x == 0.0
    rig.vp.set_interactive(True)


def test_opacity_slider_follows_selection_and_edits_the_model(rig):
    slider = rig.panel._opacity_slider
    assert not slider.isHidden()
    slider.setValue(40)
    assert rig.seg().opacity == pytest.approx(0.4)
    assert rig.doc.can_undo()
    rig.doc.clear_selection()
    assert slider.isHidden()


# ------------------------------------------------------------------------ compositing
def render(rig) -> QImage:
    return rig.vp.grab().toImage()


def test_overlay_is_composited_over_v1_with_export_geometry(rig):
    rig.doc.set_transform([rig.overlay.id], scale=0.5, x=0.25, y=0.0)
    rig.panel._refresh_overlays()
    rig.set_overlay_image(solid("#0000ff"))
    rig.vp.set_interactive(False)                                # no handle frame in the picture
    img = render(rig)
    comp = rig.vp.compose_rect()
    r = pv.overlay_rect(comp, 16 / 9, 0.25, 0.0, 0.5)
    cx, cy = r.center().x(), r.center().y()
    assert px(img, cx, cy)[2] > 200 and px(img, cx, cy)[0] < 60            # blue overlay
    assert px(img, comp.left() + 5, comp.top() + 5)[0] > 200              # red V1 elsewhere
    rig.vp.set_interactive(True)


def test_opacity_blends_with_the_layer_below(rig):
    rig.doc.set_transform([rig.overlay.id], opacity=0.5)
    rig.panel._refresh_overlays()
    rig.set_overlay_image(solid("#0000ff"))
    rig.vp.set_interactive(False)
    img = render(rig)
    c = rig.vp.compose_rect().center()
    r, g, b = px(img, c.x(), c.y())
    assert 90 < r < 165 and 90 < b < 165
    rig.vp.set_interactive(True)


def test_overlay_without_picture_draws_nothing(rig):
    rig.set_overlay_image(None)
    rig.vp.set_interactive(False)
    img = render(rig)
    c = rig.vp.compose_rect().center()
    assert px(img, c.x(), c.y())[0] > 200                                  # V1 red still there
    rig.vp.set_interactive(True)


def test_v1_gap_shows_black_base_under_overlay_with_source_aspect(app):
    r = Rig(app, aspect="source", with_v1=False)
    try:
        r.vp.set_interactive(False)
        comp = r.vp.compose_rect()
        assert comp is not None and comp.width() / comp.height() == pytest.approx(16 / 9, rel=0.02)
        r.doc.set_transform([r.overlay.id], scale=0.5)
        r.panel._refresh_overlays()
        r.set_overlay_image(solid("#0000ff"))
        img = r.vp.grab().toImage()
        assert px(img, comp.left() + 3, comp.top() + 3) == (0, 0, 0)       # black base
        assert px(img, comp.center().x(), comp.center().y())[2] > 200
    finally:
        r.close()


def test_single_track_project_has_no_overlays_or_handles(app):
    doc = Document()
    doc.add_source(MediaSource(id="a", path="C:/nope/a.mp4", duration=3.0, fps=30.0,
                               width=1920, height=1080))
    panel = pv.PreviewPanel(doc, _Settings())
    panel._resync_timer.stop()
    try:
        panel._refresh_overlays()
        assert panel._viewport.overlays() == []
        assert not panel._viewport.transform_active()
        assert panel._opacity_slider.isHidden()
        assert panel.layer_state() == {}
    finally:
        panel.shutdown()


def test_layer_sync_with_offline_media_is_safe(rig):
    rig.panel._sync_layers()
    rig.panel._sync_layers(tick=True)
    assert rig.panel.layer_state() == {}                         # offline file: no player made
