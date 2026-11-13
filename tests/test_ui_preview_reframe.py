"""Preview canvas (social aspects), reframe handle and caption overlay.

No media backend is needed: frames are injected into the viewport directly (a synthetic
left-red / right-blue picture) while the Document, clips and undo stack are real.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipforge.core.captions import CaptionLine, Word, style_for_preset  # noqa: E402
from clipforge.core.model import MediaSource, Segment  # noqa: E402
from clipforge.document import Document  # noqa: E402
from clipforge.ui import preview as pv  # noqa: E402
from clipforge.ui import theme  # noqa: E402


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication([])
    theme.apply_theme(inst, "dark")
    return inst


def split_image(w=320, h=180) -> QImage:
    """Left half red, right half blue."""
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(0, 0, 255))
    p = QPainter(img)
    p.fillRect(0, 0, w // 2, h, QColor(255, 0, 0))
    p.end()
    return img


def px(img: QImage, x: int, y: int) -> tuple[int, int, int]:
    c = img.pixelColor(x, y)
    return c.red(), c.green(), c.blue()


def is_red(c):
    return c[0] > 200 and c[2] < 60


def is_blue(c):
    return c[2] > 200 and c[0] < 60


# ------------------------------------------------------------------------ pure geometry
def test_canvas_ratio_and_fit_for_each_aspect():
    assert pv.canvas_ratio("source") is None
    assert pv.canvas_ratio("9:16") == pytest.approx(9 / 16)
    for aspect, ratio in (("9:16", 9 / 16), ("4:5", 0.8), ("1:1", 1.0), ("16:9", 16 / 9)):
        for w, h in ((800, 450), (300, 600), (240, 135), (1200, 1200)):
            r = pv.fit_rect(QRectF(0, 0, w, h), ratio)
            assert r.width() / r.height() == pytest.approx(ratio, rel=1e-6)
            assert r.left() >= -1e-6 and r.right() <= w + 1e-6
            assert r.top() >= -1e-6 and r.bottom() <= h + 1e-6
            assert r.center().x() == pytest.approx(w / 2) and r.center().y() == pytest.approx(h / 2)
            # as large as possible: touches at least one pair of edges
            assert r.width() == pytest.approx(w) or r.height() == pytest.approx(h)


def test_crop_rect_horizontal_window():
    # 1920x1080 source into 9:16: window is 607.5 wide, full height
    left = pv.crop_rect(1920, 1080, 9 / 16, 0.0)
    right = pv.crop_rect(1920, 1080, 9 / 16, 1.0)
    mid = pv.crop_rect(1920, 1080, 9 / 16, 0.5)
    assert left.x() == 0 and left.width() == pytest.approx(607.5) and left.height() == 1080
    assert right.right() == pytest.approx(1920)
    assert mid.center().x() == pytest.approx(960)
    # 1:1 and 4:5
    assert pv.crop_rect(1920, 1080, 1.0, 0.0).width() == pytest.approx(1080)
    assert pv.crop_rect(1920, 1080, 0.8, 0.0).width() == pytest.approx(864)
    # out-of-range crop is clamped
    assert pv.crop_rect(1920, 1080, 1.0, 7.0).right() == pytest.approx(1920)


def test_crop_rect_narrow_source_crops_vertically_centred():
    r = pv.crop_rect(1080, 1920, 1.0, 0.0)               # portrait source into a square
    assert r.x() == 0 and r.width() == 1080 and r.height() == pytest.approx(1080)
    assert r.center().y() == pytest.approx(960)
    same = pv.crop_rect(1080, 1920, 9 / 16, 0.3)         # identical aspect: whole frame
    assert (same.x(), same.y(), same.width(), same.height()) == pytest.approx((0, 0, 1080, 1920))


def test_pan_interpolation_at_playhead():
    seg = Segment(id="a", source_id="s", source_in=2.0, source_out=6.0, timeline_start=0.0,
                  crop_x=0.2, crop_x_end=0.8)
    assert pv.pan_crop_x(seg, 2.0) == pytest.approx(0.2)
    assert pv.pan_crop_x(seg, 4.0) == pytest.approx(0.5)
    assert pv.pan_crop_x(seg, 6.0) == pytest.approx(0.8)
    assert pv.pan_crop_x(seg, 99.0) == pytest.approx(0.8)
    static = Segment(id="b", source_id="s", source_in=0, source_out=4, timeline_start=0, crop_x=0.3)
    assert pv.pan_crop_x(static, 3.0) == pytest.approx(0.3)


# ---------------------------------------------------------------------------- rendering
def test_fill_render_follows_crop_x(app):
    img = split_image()
    left = pv.render_canvas(img, 90, 160, "fill", 0.0)
    right = pv.render_canvas(img, 90, 160, "fill", 1.0)
    mid = pv.render_canvas(img, 90, 160, "fill", 0.5)
    # window is 101 source px wide: x=0 sees only red, x=1 only blue, centre sees the seam
    assert is_red(px(left, 5, 80)) and is_red(px(left, 85, 80))
    assert is_blue(px(right, 5, 80)) and is_blue(px(right, 85, 80))
    assert is_red(px(mid, 10, 80)) and is_blue(px(mid, 80, 80))


def test_fit_render_has_black_bars(app):
    out = pv.render_canvas(split_image(), 90, 160, "fit", 0.5)
    assert px(out, 45, 5) == (0, 0, 0) and px(out, 45, 155) == (0, 0, 0)
    assert is_red(px(out, 10, 80)) and is_blue(px(out, 80, 80))


def test_blur_render_has_dim_blurred_bars_and_sharp_centre(app):
    out = pv.render_canvas(split_image(), 90, 160, "blur", 0.5)
    top = px(out, 10, 5)
    assert top != (0, 0, 0)                               # blurred copy, not black
    assert max(top) < 160                                 # darkened
    assert is_red(px(out, 10, 80)) and is_blue(px(out, 80, 80))     # sharp, undimmed frame


# ------------------------------------------------------------------------------- rig
class Rig:
    def __init__(self, app, aspect="9:16", mode="fill", w=900, h=600):
        self.doc = Document()
        self.doc.add_source(MediaSource(id="s0", path="C:/nope/s0.mp4", duration=6.0, fps=30.0,
                                        width=1920, height=1080))
        self.doc.history.clear()
        self.doc.clear_selection()
        self.panel = pv.PreviewPanel(self.doc, _Settings())
        self.panel._resync_timer.stop()
        self.panel._enter = lambda t: None          # no media engine: frames are injected
        self.panel.resize(w, h)
        self.panel.show()
        app.processEvents()
        self.panel._resync_timer.stop()
        self.vp = self.panel._viewport
        self.doc.set_canvas(aspect, mode)
        self.seg = self.doc.timeline.segments[0]
        self.show()

    def show(self):
        self.vp.show_frame(split_image(), self.seg, 1.0)

    def close(self):
        self.panel.shutdown()
        self.panel.close()

    def drag(self, p0: QPointF, p1: QPointF, release=True):
        QTest.mousePress(self.vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0.toPoint())
        QTest.mouseMove(self.vp, p1.toPoint())
        if release:
            QTest.mouseRelease(self.vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                               p1.toPoint())


class _Settings:
    volume = 1.0

    def save(self):
        pass


@pytest.fixture()
def rig(app):
    r = Rig(app)
    yield r
    r.close()


def test_canvas_rect_present_only_for_social_aspect(app):
    r = Rig(app, aspect="source")
    try:
        assert r.panel.canvas_rect() is None
        assert not r.vp.handle_active() and not r.vp.pill().isVisible()
        r.doc.set_canvas("9:16", "fill")
        c = r.panel.canvas_rect()
        assert c is not None and c.width() / c.height() == pytest.approx(9 / 16, abs=0.01)
        assert QRectF(r.vp.rect()).contains(c)
        r.doc.set_canvas("1:1")
        assert r.panel.canvas_rect().width() == pytest.approx(r.panel.canvas_rect().height(), abs=1)
        r.doc.set_canvas("source")
        assert r.panel.canvas_rect() is None
    finally:
        r.close()


def test_viewport_paints_fill_crop_and_updates_live(rig):
    c = rig.vp.canvas_rect()
    img = rig.vp.grab().toImage()
    inner_l, inner_r = int(c.left()) + 6, int(c.right()) - 6
    y = int(c.center().y())
    assert is_blue(px(img, inner_l, y)) is False or True    # centre crop: seam in the middle
    rig.doc.set_crop([rig.seg.id], 0.0)
    img = rig.vp.grab().toImage()
    assert is_red(px(img, inner_l, y)) and is_red(px(img, inner_r, y))
    rig.doc.set_crop([rig.seg.id], 1.0)
    img = rig.vp.grab().toImage()
    assert is_blue(px(img, inner_l, y)) and is_blue(px(img, inner_r, y))


def test_blur_and_fit_modes_through_the_viewport(rig):
    rig.doc.set_canvas("9:16", "fit")
    c = rig.vp.canvas_rect()
    img = rig.vp.grab().toImage()
    assert px(img, int(c.center().x()), int(c.top()) + 4) == (0, 0, 0)
    rig.doc.set_canvas("9:16", "blur")
    c = rig.vp.canvas_rect()
    img = rig.vp.grab().toImage()
    assert px(img, int(c.center().x()), int(c.top()) + 4) != (0, 0, 0)
    assert not rig.vp.handle_active() and not rig.vp.pill().isVisible()      # fill only


def test_handle_visibility(rig):
    assert rig.vp.handle_active() and rig.vp.pill().isVisible()
    assert rig.vp.minimap_rect() is not None
    rig.vp.set_interactive(False)                          # playing
    assert not rig.vp.handle_active() and not rig.vp.pill().isVisible()
    rig.vp.set_interactive(True)
    rig.vp.show_black()                                    # gap
    assert not rig.vp.handle_active() and not rig.vp.pill().isVisible()
    rig.show()
    assert rig.vp.pill().isVisible()
    rig.doc.set_canvas("16:9", "fill")                     # source is 16:9: nothing to pan
    assert not rig.vp.handle_active()


# --------------------------------------------------------------------------- drag / undo
def test_canvas_drag_commits_exactly_one_undo_step(rig):
    c = rig.vp.canvas_rect()
    start = QPointF(c.center().x(), c.center().y() + 20)
    n_before = len(rig.doc.history.undo_stack) if hasattr(rig.doc.history, "undo_stack") else None
    rig.drag(start, start + QPointF(30, 0), release=False)
    # live: nothing in the model yet, the picture already follows the override
    assert rig.doc.timeline.segments[0].crop_x == pytest.approx(0.5)
    live = rig.vp.current_crop_x()
    assert live < 0.5                                      # dragged right -> window moves left
    QTest.mouseRelease(rig.vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       (start + QPointF(30, 0)).toPoint())
    seg = rig.doc.timeline.segments[0]
    assert seg.crop_x == pytest.approx(live, abs=1e-6) and seg.crop_x_end is None
    assert rig.doc.undo()
    assert rig.doc.timeline.segments[0].crop_x == pytest.approx(0.5)
    assert not rig.doc.can_undo()                          # exactly one step
    if n_before is not None:
        assert len(rig.doc.history.undo_stack) == n_before


def test_minimap_drag_sets_absolute_position(rig):
    mm = rig.vp.minimap_rect()
    p0 = QPointF(mm.center().x(), mm.center().y())
    rig.drag(p0, QPointF(mm.left() + 1, mm.center().y()))
    assert rig.doc.timeline.segments[0].crop_x == pytest.approx(0.0, abs=0.02)
    assert rig.doc.undo() and not rig.doc.can_undo()


def test_escape_cancels_drag(rig):
    c = rig.vp.canvas_rect()
    p0 = c.center()
    rig.drag(p0, p0 + QPointF(-40, 0), release=False)
    assert rig.vp.is_dragging() and rig.vp.current_crop_x() > 0.5
    QTest.keyClick(rig.vp, Qt.Key.Key_Escape)
    assert not rig.vp.is_dragging()
    assert rig.vp.current_crop_x() == pytest.approx(0.5)
    QTest.mouseRelease(rig.vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0.toPoint())
    assert rig.doc.timeline.segments[0].crop_x == pytest.approx(0.5)
    assert not rig.doc.can_undo()


def test_click_without_moving_does_not_edit(rig):
    c = rig.vp.canvas_rect()
    rig.drag(c.center(), c.center())
    assert not rig.doc.can_undo()


def test_drag_survives_clip_disappearing(rig):
    c = rig.vp.canvas_rect()
    rig.drag(c.center(), c.center() + QPointF(20, 0), release=False)
    rig.doc.select([rig.seg.id])
    rig.doc.delete_selected(ripple=True)
    assert not rig.vp.is_dragging()
    QTest.mouseRelease(rig.vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       c.center().toPoint())                       # must not raise


def test_static_pan_toggle_semantics(rig):
    pill = rig.vp.pill()
    assert not rig.vp.is_panning()
    rig.panel._on_mode_requested("pan")
    seg = rig.doc.timeline.segments[0]
    assert seg.crop_x_end is not None and seg.crop_x_end != seg.crop_x
    assert rig.vp.is_panning() and rig.vp.edit_end() == "end"
    assert pill.isVisible()
    # dragging edits the chosen end only
    x0 = seg.crop_x
    c = rig.vp.canvas_rect()
    rig.drag(c.center(), c.center() + QPointF(-25, 0))
    seg = rig.doc.timeline.segments[0]
    assert seg.crop_x == pytest.approx(x0) and seg.crop_x_end is not None
    end_after = seg.crop_x_end
    rig.vp.set_edit_end("start")
    rig.drag(c.center(), c.center() + QPointF(25, 0))
    seg = rig.doc.timeline.segments[0]
    assert seg.crop_x != pytest.approx(x0) and seg.crop_x_end == pytest.approx(end_after)
    # back to Static: crop_x_end cleared, position = crop at the shown frame
    expected = seg.crop_x_at(1.0)
    rig.panel._on_mode_requested("static")
    seg = rig.doc.timeline.segments[0]
    assert seg.crop_x_end is None and seg.crop_x == pytest.approx(expected)
    assert not rig.vp.is_panning()


def test_pan_live_preview_uses_interpolated_position(rig):
    rig.doc.set_crop([rig.seg.id], 0.0, 1.0)
    rig.vp.show_frame(split_image(), rig.doc.timeline.segments[0], 3.0)     # half way of 6 s
    assert rig.vp.current_crop_x() == pytest.approx(0.5)
    rig.vp.show_frame(split_image(), rig.doc.timeline.segments[0], 6.0)
    assert rig.vp.current_crop_x() == pytest.approx(1.0)


def test_nudge_with_alt_arrows_steps_two_and_ten_percent(rig):
    assert rig.panel.nudge_crop(+1)
    assert rig.doc.timeline.segments[0].crop_x == pytest.approx(0.52)
    assert rig.panel.nudge_crop(-1, big=True)
    assert rig.doc.timeline.segments[0].crop_x == pytest.approx(0.42)
    rig.doc.set_crop([rig.seg.id], 1.0)
    assert not rig.panel.nudge_crop(+1)                    # at the edge: nothing to do
    rig.vp.set_interactive(False)
    assert not rig.panel.nudge_crop(-1)                    # not while playing


def test_targets_are_all_selected_video_clips(rig):
    assert rig.panel.reframe_targets() == [rig.seg.id]
    rig.doc.add_source(MediaSource(id="s1", path="C:/nope/s1.mp4", duration=3.0, fps=30.0,
                                   width=1920, height=1080))
    ids = [s.id for s in rig.doc.timeline.segments]
    rig.doc.select(ids)
    assert rig.panel.reframe_targets() == ids
    rig.doc.select([ids[1]])
    assert rig.panel.reframe_targets() == [rig.seg.id]      # one selected: clip under the playhead


# -------------------------------------------------------------------------------- captions
def _line():
    words = [Word(1.0, 1.4, "hello"), Word(1.5, 2.0, "brave"), Word(2.0, 2.6, "new"),
             Word(2.8, 3.2, "world")]
    return CaptionLine(1.0, 3.2, "hello brave new world", words)


def test_active_caption_and_word_selection():
    a, b = CaptionLine(0.0, 1.0, "one"), CaptionLine(1.0, 2.0, "two")
    caps = [a, b]
    assert pv.active_caption(caps, 0.0) is a
    assert pv.active_caption(caps, 0.999) is a
    assert pv.active_caption(caps, 1.0) is b                # end exclusive
    assert pv.active_caption(caps, 2.0) is None
    assert pv.active_caption([], 0.5) is None
    ln = _line()
    assert pv.active_word(ln, 0.99) == -1
    assert pv.active_word(ln, 1.2) == 0
    assert pv.active_word(ln, 1.5) == 1
    assert pv.active_word(ln, 2.0) == 2
    assert pv.active_word(ln, 1.45) == 0                    # short gap keeps the last word lit
    assert pv.active_word(ln, 3.19) == 3
    assert pv.active_word(CaptionLine(0, 1, "x"), 0.5) == -1


def test_split_rows_wraps_to_at_most_two_balanced_rows():
    assert pv.split_rows([40, 40, 40], 5, 200) == [[0, 1, 2]]
    rows = pv.split_rows([60, 60, 60, 60], 5, 200)
    assert rows == [[0, 1], [2, 3]]
    assert pv.split_rows([100, 100, 100, 100, 100], 5, 200) is None      # needs 3 rows
    assert pv.split_rows([300], 5, 200) is None


def test_layout_has_at_most_two_rows_within_ninety_percent(app):
    style = style_for_preset("pop")
    long = CaptionLine(0, 2, "this is a really quite long caption line to wrap nicely")
    lay = pv.layout_caption(long, style, 270, 480)
    assert 1 <= len(lay.rows) <= 2
    assert lay.width <= 0.9 * 270 + 1
    ln = _line()
    short = pv.layout_caption(ln, style, 1080, 1920)
    assert len(short.rows) <= 2
    one = pv.layout_caption(CaptionLine(0, 1, "hi there"), style, 1080, 1920)
    assert len(one.rows) == 1
    assert pv.layout_caption(ln, style, 1080, 1920) is short                # cached
    other = pv.layout_caption(ln, style, 540, 960)
    assert other is not short and other.px < short.px
    assert short.texts == ["HELLO", "BRAVE", "NEW", "WORLD"]                # uppercase preset


def test_layout_is_stable_between_words(app):
    style = style_for_preset("pop")
    ln = _line()
    a = pv.layout_caption(ln, style, 540, 960)
    b = pv.layout_caption(ln, style, 540, 960)
    assert [p.boundingRect() for p in a.paths] == [p.boundingRect() for p in b.paths]


def _caption_rig(app, preset="pop", position=None):
    r = Rig(app)
    style = style_for_preset(preset)
    if position:
        style.position = position
    r.doc.set_captions([_line()], style)
    r.doc.set_crop([r.seg.id], 0.5)
    return r


def _text_pixels(img: QImage, rect: QRectF, colour: QColor, tol=40) -> int:
    n = 0
    for y in range(int(rect.top()), int(rect.bottom()), 2):
        for x in range(int(rect.left()), int(rect.right()), 2):
            c = img.pixelColor(x, y)
            if (abs(c.red() - colour.red()) < tol and abs(c.green() - colour.green()) < tol
                    and abs(c.blue() - colour.blue()) < tol):
                n += 1
    return n


def test_caption_overlay_draws_only_when_line_active(app):
    r = _caption_rig(app)
    try:
        c = r.vp.canvas_rect()
        yellow = QColor("#FFD400")
        r.doc.seek(0.5)
        r.panel._resync_timer.stop()
        r.show()
        assert r.vp.caption_at(0.5) is None
        before = _text_pixels(r.vp.grab().toImage(), c, yellow)
        r.doc.set_playhead(1.2)
        line, lit = r.vp.caption_at(1.2)
        assert line.text == "hello brave new world" and lit == 0
        img = r.vp.grab().toImage()
        assert _text_pixels(img, c, yellow) > before + 20          # highlighted word is yellow
        assert _text_pixels(img, c, QColor("#FFFFFF"), 20) > 50    # the rest is white
        r.doc.set_playhead(3.5)
        assert r.vp.caption_at(3.5) is None
    finally:
        r.close()


def test_nothing_drawn_without_captions(app):
    r = Rig(app)
    try:
        c = r.vp.canvas_rect()
        a = r.vp.grab().toImage()
        r.doc.set_captions([_line()], style_for_preset("pop"))
        r.doc.set_playhead(0.2)
        b = r.vp.grab().toImage()
        assert a == b                                              # line not active: identical
        r.doc.clear_captions()
        r.doc.set_playhead(1.2)
        assert r.vp.caption_at(1.2) is None
        assert r.vp.grab().toImage() == a
    finally:
        r.close()


def test_caption_position_top_middle_bottom(app):
    ys = {}
    for pos in ("top", "middle", "bottom"):
        r = _caption_rig(app, position=pos)
        try:
            r.doc.set_playhead(1.2)
            c = r.vp.canvas_rect()
            lay = pv.layout_caption(_line(), r.doc.caption_style, c.width(), c.height())
            ys[pos] = pv.caption_origin(r.doc.caption_style, c, lay).y() - c.top()
        finally:
            r.close()
    assert ys["top"] < ys["middle"] < ys["bottom"]


def test_captions_follow_source_aspect_too(app):
    r = _caption_rig(app)
    try:
        r.doc.set_canvas("source")
        r.doc.set_playhead(1.2)
        assert r.vp.caption_at(1.2) is not None
        assert r.panel.canvas_rect() is None and not r.vp.handle_active()
    finally:
        r.close()
