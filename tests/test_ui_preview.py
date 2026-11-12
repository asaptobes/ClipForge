"""Tests for the preview panel using the real Qt Multimedia back-end.

The synthetic clip is 6 s, 640x360, 30 fps, H.264 + AAC whose luma ramps linearly with
time, so any frame the panel puts on screen can be decoded back to the *source* second
it came from (``Rig.src_time``).  All checks therefore compare what is displayed with
what the edit list says should be displayed, independent of the code under test.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipforge.core.model import MediaSource  # noqa: E402
from clipforge.document import Document  # noqa: E402
from clipforge.media import probe  # noqa: E402
from clipforge.ui import theme  # noqa: E402
from clipforge.ui.preview import PreviewPanel  # noqa: E402

from conftest import LUMA_SPAN, _make_video  # noqa: E402

FPS = 30.0
FD = 1.0 / FPS
DUR = 6.0
TOL = 2.2 * FD                 # "within about 2 frames"
#: RGB grey level per source second (limited-range luma 16..216 -> full range 0..255)
_GREY_PER_SEC = 255.0 / 219.0 * LUMA_SPAN / DUR


# ------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication([])
    theme.apply_theme(inst, "dark")
    return inst


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> MediaSource:
    path = str(tmp_path_factory.mktemp("prevmedia") / "ramp.mp4")
    _make_video(path, w=640, h=360, fps=30, dur=int(DUR), audio_rate=48000)
    return probe.probe_source(path)


def src_time(image: QImage) -> float:
    """Decode the source second a displayed frame came from (centre patch brightness)."""
    img = image.convertToFormat(QImage.Format.Format_RGB32)
    cx, cy = img.width() // 2, img.height() // 2
    vals = [img.pixelColor(cx + dx, cy + dy).red() for dx in (-6, 0, 6) for dy in (-6, 0, 6)]
    return (sum(vals) / len(vals)) / _GREY_PER_SEC


class Rig:
    """Document + PreviewPanel + recorders for everything the panel reports."""

    def __init__(self, app, doc, panel, source):
        self.app, self.doc, self.panel, self.src = app, doc, panel, source
        self.frames: list[tuple[float, float, float]] = []   # (source time, timeline t, wall)
        self.playheads: list[float] = []
        self.playing: list[bool] = []
        panel.frameDisplayed.connect(self._on_frame)
        doc.playheadChanged.connect(self.playheads.append)
        panel.playingChanged.connect(self.playing.append)

    def _on_frame(self, image, t):
        self.frames.append((src_time(image), t, time.monotonic()))

    # -- event loop helpers
    def run(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.004)

    def wait_for(self, cond, timeout: float = 6.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if cond():
                return True
            time.sleep(0.004)
        return bool(cond())

    def settle(self, quiet: float = 0.3, timeout: float = 4.0) -> None:
        """Wait until the (paused) panel stops producing frames."""
        mark = len(self.frames)
        t0 = time.monotonic()
        self.wait_for(lambda: len(self.frames) > mark
                      or (time.monotonic() - t0 > 0.5 and self.panel.display_state() != "frame"),
                      timeout)
        last, last_t = len(self.frames), time.monotonic()
        while time.monotonic() - last_t < quiet:
            self.app.processEvents()
            time.sleep(0.004)
            if len(self.frames) != last:
                last, last_t = len(self.frames), time.monotonic()

    # -- editing helpers (always through the Document, like the UI)
    def load(self):
        self.doc.add_source(self.src)
        self.run(0.05)

    def split_at(self, t: float) -> None:
        self.doc.seek(t)
        assert self.doc.split_at_playhead()

    def ripple_delete_at(self, t: float) -> None:
        seg = self.doc.timeline.segment_at(t)
        self.doc.select([seg.id])
        assert self.doc.delete_selected(ripple=True)

    def delete_at(self, t: float) -> None:
        seg = self.doc.timeline.segment_at(t)
        self.doc.select([seg.id])
        assert self.doc.delete_selected(ripple=False)

    # -- what is on screen
    def shown(self) -> float:
        img = self.panel.current_image()
        assert img is not None, f"no frame displayed (state={self.panel.display_state()})"
        return src_time(img)

    def expected_src(self, t: float) -> float:
        hit = self.doc.timeline.to_source(t)
        assert hit is not None
        return hit[1]

    def show(self, t: float) -> float:
        """Seek (paused) to timeline time ``t`` and return the source time on screen."""
        self.doc.seek(t)
        self.settle()
        return self.shown()

    def check_at(self, t: float) -> None:
        got = self.show(t)
        assert got == pytest.approx(self.expected_src(t), abs=TOL), f"timeline {t:.3f}"

    def play_until_stop(self, timeout: float = 15.0, rate: float = 1.0) -> bool:
        self.panel.set_rate(rate)
        self.panel.play()
        assert self.panel.is_playing()
        return self.wait_for(lambda: not self.panel.is_playing(), timeout)


@pytest.fixture
def rig(app, clip):
    doc = Document()
    settings = SimpleNamespace(volume=1.0, saved=0, save=lambda: None)
    panel = PreviewPanel(doc, settings)
    panel.resize(720, 520)
    panel.show()
    r = Rig(app, doc, panel, clip)
    r.settings = settings
    yield r
    panel.shutdown()
    panel.close()
    panel.deleteLater()
    doc.deleteLater()
    for _ in range(5):
        app.processEvents()


# ------------------------------------------------------------------- basic display
def test_empty_document_shows_placeholder(rig):
    assert rig.panel.display_state() == "placeholder"
    assert not rig.panel.is_playing()
    rig.panel.play()                                  # nothing to play
    rig.panel.toggle_play()
    rig.panel.step_frames(1)
    rig.panel.go_to_start()
    rig.panel.go_to_end()
    rig.run(0.1)
    assert not rig.panel.is_playing()
    assert rig.panel.display_state() == "placeholder"
    assert rig.panel.grab().toImage().width() > 0


def test_paused_seek_shows_the_right_frame(rig):
    rig.load()
    assert rig.panel.loaded_source_ids() == [rig.src.id]
    for t in (0.0, 1.5, 3.0, 4.77, 2.2, 0.4, 5.9):
        rig.check_at(t)
    # the final frame is shown when parked at the very end
    got = rig.show(rig.doc.duration)
    assert got == pytest.approx(DUR - FD, abs=TOL)
    assert rig.panel.grab().toImage().width() > 0


def test_frame_timestamps_match_requested_position(rig):
    rig.load()
    rig.show(2.0)
    t_frame = rig.frames[-1][1]
    assert t_frame == pytest.approx(2.0, abs=FD)
    rig.show(0.0)
    assert rig.frames[-1][1] == pytest.approx(0.0, abs=1e-3)


# --------------------------------------------------------- paused seek after edits
def test_seek_after_split_and_ripple_delete_of_first_part(rig):
    rig.load()
    rig.split_at(2.0)
    rig.ripple_delete_at(0.5)                         # keeps source [2, 6] at timeline [0, 4]
    assert rig.doc.duration == pytest.approx(4.0, abs=FD)
    for t in (0.0, 0.7, 1.9, 3.5):
        rig.check_at(t)
    assert rig.show(0.0) == pytest.approx(2.0, abs=TOL)


def test_seek_after_ripple_delete_in_the_middle(rig):
    rig.load()
    rig.split_at(2.0)
    rig.split_at(4.0)
    rig.ripple_delete_at(3.0)                         # removes source [2, 4]
    assert rig.doc.duration == pytest.approx(4.0, abs=FD)
    assert rig.show(1.9) == pytest.approx(1.9, abs=TOL)
    assert rig.show(2.1) == pytest.approx(4.1, abs=TOL)       # first frames after the cut
    assert rig.show(3.5) == pytest.approx(5.5, abs=TOL)
    assert rig.show(0.2) == pytest.approx(0.2, abs=TOL)       # and back across the cut


def test_seek_after_reorder(rig):
    rig.load()
    rig.split_at(2.0)
    rig.split_at(4.0)
    first = rig.doc.timeline.segments[0].id
    assert rig.doc.reorder(first, 2)                  # order: [2,4] [4,6] [0,2]
    assert rig.doc.duration == pytest.approx(DUR, abs=FD)
    assert rig.show(0.5) == pytest.approx(2.5, abs=TOL)
    assert rig.show(3.0) == pytest.approx(5.0, abs=TOL)
    assert rig.show(4.5) == pytest.approx(0.5, abs=TOL)
    assert rig.show(5.9) == pytest.approx(1.9, abs=TOL)
    rig.doc.undo()                                    # back to natural order
    assert rig.show(4.5) == pytest.approx(4.5, abs=TOL)


def test_seek_after_trim_and_undo(rig):
    rig.load()
    seg = rig.doc.timeline.segments[0]
    rig.doc.trim_start(seg.id, 1.0)                   # in-point -> source 1.0
    rig.doc.end_drag()
    start = rig.doc.timeline.segments[0].timeline_start
    got = rig.show(start + 0.5)
    assert got == pytest.approx(1.5, abs=TOL)
    rig.doc.undo()
    assert rig.show(0.5) == pytest.approx(0.5, abs=TOL)


def test_edit_while_paused_refreshes_the_picture(rig):
    rig.load()
    assert rig.show(1.0) == pytest.approx(1.0, abs=TOL)
    rig.doc.seek(1.0)
    rig.split_at(0.5)
    rig.doc.seek(0.0)
    rig.ripple_delete_at(0.2)                         # playhead now shows what used to be 0.5
    rig.settle()
    assert rig.shown() == pytest.approx(0.5, abs=TOL)


# -------------------------------------------------------------------------- stepping
def test_frame_step_moves_exactly_one_frame(rig):
    rig.load()
    rig.show(1.0)
    base_t = rig.doc.playhead
    for direction in (1, 1, 1, -1, -1):
        before_t = rig.doc.playhead
        rig.frames.clear()
        rig.panel.step_frames(direction)
        rig.settle()
        assert rig.doc.playhead == pytest.approx(before_t + direction * FD, abs=1e-3)
        assert rig.frames, "step produced no frame"
        t_frame = rig.frames[-1][1]
        assert t_frame == pytest.approx(rig.doc.playhead, abs=1e-3)
        assert rig.shown() == pytest.approx(rig.doc.playhead, abs=TOL)
    assert rig.doc.playhead == pytest.approx(base_t + FD, abs=2e-3)
    assert not rig.panel.is_playing()


def test_frame_step_pauses_playback(rig):
    rig.load()
    rig.panel.play()
    rig.run(0.3)
    assert rig.panel.is_playing()
    rig.panel.step_frames(1)
    assert not rig.panel.is_playing()
    assert rig.playing[-2:] == [True, False]


def test_frame_step_across_a_cut_and_clamping(rig):
    rig.load()
    rig.split_at(2.0)
    rig.split_at(4.0)
    rig.ripple_delete_at(3.0)                         # cut at timeline 2.0: source 2.0 -> 4.0
    rig.show(2.0 - FD)
    assert rig.shown() == pytest.approx(2.0 - FD, abs=TOL)
    rig.frames.clear()
    rig.panel.step_frames(1)
    rig.settle()
    assert rig.doc.playhead == pytest.approx(2.0, abs=2e-3)
    assert rig.shown() == pytest.approx(4.0, abs=TOL)
    rig.panel.step_frames(-1)
    rig.settle()
    assert rig.shown() == pytest.approx(2.0 - FD, abs=TOL)
    # clamps at both ends
    rig.show(0.0)
    rig.panel.step_frames(-1)
    assert rig.doc.playhead == 0.0
    rig.doc.seek(rig.doc.duration)
    rig.panel.step_frames(1)
    assert rig.doc.playhead == pytest.approx(rig.doc.duration)


def test_frame_step_through_a_gap_uses_thirtieth_second(rig):
    rig.load()
    rig.split_at(2.0)
    rig.split_at(4.0)
    rig.delete_at(3.0)                                # gap [2, 4]
    rig.doc.seek(3.0)
    rig.panel.step_frames(1)
    assert rig.doc.playhead == pytest.approx(3.0 + 1 / 30, abs=1e-6)
    rig.panel.step_frames(-3)
    assert rig.doc.playhead == pytest.approx(3.0 - 2 / 30, abs=1e-6)


# ------------------------------------------------------------------------- playback
def _cut_timeline(rig):
    """A[0,1] @0 and C[3,6] @1: non-adjacent source ranges, 4 s long, one cut at 1.0."""
    rig.load()
    rig.split_at(1.0)
    rig.split_at(3.0)
    rig.ripple_delete_at(2.0)
    assert rig.doc.duration == pytest.approx(4.0, abs=FD)
    rig.doc.seek(0.0)
    rig.settle()
    rig.frames.clear()
    rig.playheads.clear()
    rig.playing.clear()


def _check_playback_frames(rig, allowed, tolerance=TOL):
    assert rig.frames, "no frame was displayed during playback"
    for src_t, t_frame, _wall in rig.frames:
        assert any(a - tolerance <= src_t <= b + tolerance for a, b in allowed), \
            f"frame from source {src_t:.3f}s (timeline {t_frame:.3f}s) outside {allowed}"
        mapped = rig.doc.timeline.to_source(min(t_frame, rig.doc.duration - 1e-3))
        assert mapped is not None
        assert src_t == pytest.approx(mapped[1], abs=tolerance), \
            f"frame shows {src_t:.3f}s but timeline {t_frame:.3f}s maps to {mapped[1]:.3f}s"


def test_play_across_a_cut_shows_only_valid_frames(rig):
    _cut_timeline(rig)
    started = time.monotonic()
    assert rig.play_until_stop(timeout=12)
    elapsed = time.monotonic() - started
    assert 3.0 < elapsed < 6.5                        # ~4 s of real time at 1x
    _check_playback_frames(rig, [(0.0, 1.0), (3.0, 6.0)])
    # both sides of the cut were actually shown, and plenty of frames in between
    assert any(s < 1.0 for s, _, _ in rig.frames) and any(s > 3.0 for s, _, _ in rig.frames)
    assert len(rig.frames) > 60
    assert not any(1.0 + TOL < s < 3.0 - TOL for s, _, _ in rig.frames)
    # frame timeline times never go backwards
    ts = [t for _, t, _ in rig.frames]
    assert all(b >= a - 1e-6 for a, b in zip(ts, ts[1:]))
    # playhead advanced monotonically and stopped at the end
    assert all(b >= a - 1e-6 for a, b in zip(rig.playheads, rig.playheads[1:]))
    assert rig.doc.playhead == pytest.approx(rig.doc.duration, abs=1e-3)
    assert rig.playing == [True, False]
    assert rig.panel.display_state() == "frame"
    assert rig.shown() == pytest.approx(6.0 - FD, abs=TOL * 1.5)       # last frame held


def test_play_across_several_cuts_at_double_speed(rig):
    rig.load()
    for t in (0.5, 1.5, 2.5, 3.5, 4.5):
        rig.split_at(t)
    rig.ripple_delete_at(1.0)                         # [0.5,1.5]
    rig.ripple_delete_at(2.0)                         # recomputed: removes next odd piece
    segs = rig.doc.timeline.segments
    allowed = [(s.source_in, s.source_out) for s in segs]
    rig.doc.seek(0.0)
    rig.settle()
    rig.frames.clear()
    rig.playheads.clear()
    assert rig.play_until_stop(timeout=12, rate=2.0)
    _check_playback_frames(rig, allowed)
    assert all(b >= a - 1e-6 for a, b in zip(rig.playheads, rig.playheads[1:]))
    assert rig.doc.playhead == pytest.approx(rig.doc.duration, abs=1e-3)


def test_play_from_the_middle_and_pause_resume(rig):
    rig.load()
    rig.doc.seek(2.0)
    rig.settle()
    rig.frames.clear()
    rig.panel.play()
    rig.run(0.8)
    assert rig.panel.is_playing()
    rig.panel.toggle_play()
    assert not rig.panel.is_playing()
    rig.settle()
    stopped_at = rig.doc.playhead
    assert 2.4 < stopped_at < 3.6
    assert rig.shown() == pytest.approx(stopped_at, abs=TOL * 1.5)    # paused on the real frame
    _check_playback_frames(rig, [(2.0, DUR)])
    rig.panel.toggle_play()
    rig.run(0.5)
    assert rig.doc.playhead > stopped_at + 0.2
    rig.panel.pause()
    assert rig.playing == [True, False, True, False]


def test_play_at_end_restarts_from_start(rig):
    rig.load()
    rig.doc.seek(rig.doc.duration)
    rig.settle()
    rig.panel.set_rate(4.0)
    rig.panel.play()
    assert rig.panel.is_playing()
    rig.run(0.3)
    assert 0.0 < rig.doc.playhead < DUR
    assert rig.wait_for(lambda: not rig.panel.is_playing(), 8)
    assert rig.doc.playhead == pytest.approx(DUR, abs=1e-3)


def test_continuous_split_plays_through_without_glitch(rig):
    rig.load()
    rig.split_at(2.0)                                 # same file, nothing removed
    rig.doc.seek(1.4)
    rig.settle()
    rig.frames.clear()
    rig.panel.play()
    assert rig.wait_for(lambda: rig.doc.playhead > 2.6, 6)
    rig.panel.pause()
    _check_playback_frames(rig, [(1.4, 2.7)])
    ts = [t for _, t, _ in rig.frames]
    assert all(b >= a - 1e-6 for a, b in zip(ts, ts[1:]))


def test_rate_controls(rig):
    rig.load()
    assert rig.panel.rate() == 1.0
    rig.panel.set_rate(2.0)
    assert rig.panel.rate() == 2.0
    rig.panel.set_rate(100.0)
    assert rig.panel.rate() == 8.0
    rig.panel.set_rate(0.0)
    assert rig.panel.rate() == pytest.approx(0.1)
    rig.panel.set_rate(2.0)
    started = time.monotonic()
    rig.doc.seek(0.0)
    rig.settle()
    rig.panel.play()
    assert rig.wait_for(lambda: not rig.panel.is_playing(), 10)
    assert time.monotonic() - started < 4.5           # 6 s clip at 2x ~ 3 s


def test_shuttle_and_transport(rig):
    rig.load()
    rig.panel.shuttle(1)
    assert rig.panel.is_playing() and rig.panel.rate() == 1.0
    rig.panel.shuttle(1)
    assert rig.panel.rate() == 2.0
    rig.panel.shuttle(1)
    assert rig.panel.rate() == 4.0
    rig.panel.shuttle(1)
    assert rig.panel.rate() == 1.0
    rig.run(0.2)
    rig.panel.shuttle(0)
    assert not rig.panel.is_playing() and rig.panel.rate() == 1.0
    rig.doc.seek(3.0)
    rig.panel.shuttle(-1)
    assert rig.doc.playhead == pytest.approx(2.0)
    rig.panel.shuttle(-1)
    rig.panel.shuttle(-1)
    rig.panel.shuttle(-1)
    assert rig.doc.playhead == 0.0
    rig.panel.go_to_end()
    assert rig.doc.playhead == pytest.approx(rig.doc.duration)
    rig.settle()
    rig.panel.go_to_start()
    assert rig.doc.playhead == 0.0
    rig.settle()
    assert rig.shown() == pytest.approx(0.0, abs=TOL)


def test_transport_buttons(rig):
    rig.load()
    p = rig.panel
    p._btn_play.click()
    assert p.is_playing()
    p._btn_play.click()
    assert not p.is_playing()
    rig.doc.seek(1.0)
    p._btn_fwd.click()
    assert rig.doc.playhead == pytest.approx(1.0 + FD, abs=1e-3)
    p._btn_back.click()
    assert rig.doc.playhead == pytest.approx(1.0, abs=1e-3)
    p._btn_end.click()
    assert rig.doc.playhead == pytest.approx(DUR)
    p._btn_start.click()
    assert rig.doc.playhead == 0.0
    p._rate_combo.setCurrentIndex(p._rate_combo.findData(0.5))
    assert p.rate() == 0.5
    assert "0:06" in p._time_label.text() or "6" in p._time_label.text()


# --------------------------------------------------------------------------- gaps
def _gap_timeline(rig):
    """A[0,2] @0, gap [2,4], C[4,6] @4 (plain delete of the middle clip)."""
    rig.load()
    rig.split_at(2.0)
    rig.split_at(4.0)
    rig.delete_at(3.0)
    assert rig.doc.timeline.gaps() == [(pytest.approx(2.0, abs=FD), pytest.approx(4.0, abs=FD))]
    rig.doc.seek(0.0)
    rig.settle()
    rig.frames.clear()
    rig.playheads.clear()


def test_gap_is_black_when_paused(rig):
    _gap_timeline(rig)
    rig.doc.seek(3.0)
    rig.settle()
    assert rig.panel.display_state() == "black"
    assert rig.panel.current_image() is None
    rig.doc.seek(4.5)
    rig.settle()
    assert rig.panel.display_state() == "frame"
    assert rig.shown() == pytest.approx(4.5, abs=TOL)
    rig.doc.seek(1.0)
    rig.settle()
    assert rig.shown() == pytest.approx(1.0, abs=TOL)


def test_playing_through_a_gap_shows_black_while_time_advances(rig):
    _gap_timeline(rig)
    rig.doc.seek(1.2)
    rig.settle()
    rig.frames.clear()
    rig.playheads.clear()
    rig.panel.play()
    assert rig.wait_for(lambda: rig.doc.playhead > 2.3, 6)
    states = []
    heads = []
    end = time.monotonic() + 1.0
    while time.monotonic() < end and rig.doc.playhead < 3.8:
        rig.app.processEvents()
        if 2.1 < rig.doc.playhead < 3.9:
            states.append(rig.panel.display_state())
            heads.append(rig.doc.playhead)
        time.sleep(0.01)
    assert len(heads) > 20 and heads[-1] > heads[0] + 0.5        # time advances through the gap
    assert set(states) == {"black"}
    assert rig.wait_for(lambda: not rig.panel.is_playing(), 10)
    _check_playback_frames(rig, [(1.2, 2.0), (4.0, 6.0)])
    assert not any(2.0 + TOL < s < 4.0 - TOL for s, _, _ in rig.frames)
    assert all(b >= a - 1e-6 for a, b in zip(rig.playheads, rig.playheads[1:]))
    assert rig.doc.playhead == pytest.approx(rig.doc.duration, abs=1e-3)
    assert rig.panel.display_state() == "frame"


def test_leading_gap_plays_from_black(rig):
    rig.load()
    rig.split_at(1.0)
    rig.delete_at(0.5)                                # gap [0,1], clip [1,6] @1
    rig.doc.seek(0.0)
    rig.settle()
    assert rig.panel.display_state() == "black"
    rig.panel.set_rate(4.0)
    rig.frames.clear()
    rig.panel.play()
    assert rig.wait_for(lambda: not rig.panel.is_playing(), 10)
    assert rig.frames and min(s for s, _, _ in rig.frames) > 1.0 - TOL
    assert rig.doc.playhead == pytest.approx(rig.doc.duration, abs=1e-3)


def test_only_a_gap_left_at_the_end(rig):
    rig.load()
    rig.split_at(4.0)
    rig.delete_at(5.0)                                # trailing clip removed, timeline ends at 4
    assert rig.doc.duration == pytest.approx(4.0, abs=FD)
    rig.doc.seek(rig.doc.duration)
    rig.settle()
    assert rig.panel.display_state() == "frame"       # parked at the end: last frame stays


# --------------------------------------------------- destructive edits mid-playback
def test_removing_all_clips_mid_playback(rig):
    rig.load()
    rig.panel.play()
    rig.run(0.6)
    assert rig.panel.is_playing()
    rig.doc.select_all()
    assert rig.doc.delete_selected(ripple=True)
    assert rig.doc.timeline.is_empty()
    rig.run(0.4)
    assert not rig.panel.is_playing()
    assert rig.panel.display_state() == "placeholder"
    assert rig.doc.playhead == 0.0
    rig.panel.play()                                  # still harmless
    rig.panel.step_frames(1)
    rig.run(0.1)
    rig.doc.undo()                                    # and the clips come back
    rig.settle()
    assert rig.panel.display_state() == "frame"
    assert rig.shown() == pytest.approx(0.0, abs=TOL * 1.5)


def test_plain_delete_of_all_clips_mid_playback_with_cut(rig):
    _cut_timeline(rig)
    rig.panel.play()
    rig.run(0.7)
    rig.doc.select_all()
    rig.doc.delete_selected(ripple=False)
    rig.run(0.5)
    assert not rig.panel.is_playing()
    assert rig.panel.display_state() == "placeholder"


def test_deleting_the_playing_clip_continues_with_the_next(rig):
    _cut_timeline(rig)                                # A[0,1] then C[3,6]
    rig.panel.play()
    rig.run(0.4)
    rig.ripple_delete_at(0.2)                         # drop A while it plays
    assert rig.doc.duration == pytest.approx(3.0, abs=FD)
    rig.frames.clear()
    assert rig.panel.is_playing()
    assert rig.wait_for(lambda: not rig.panel.is_playing(), 8)
    _check_playback_frames(rig, [(3.0, DUR)])
    assert rig.doc.playhead == pytest.approx(rig.doc.duration, abs=1e-3)


def test_edits_during_playback_do_not_crash(rig):
    rig.load()
    rig.panel.play()
    rig.run(0.4)
    rig.split_at(rig.doc.playhead + 1.0)
    rig.doc.undo()
    rig.doc.redo()
    rig.doc.select([rig.doc.timeline.segments[-1].id])
    rig.doc.toggle_mute_selected()
    rig.run(0.3)
    rig.doc.new_project()                             # project replaced during playback
    rig.run(0.4)
    assert not rig.panel.is_playing()
    assert rig.panel.display_state() == "placeholder"
    assert rig.panel.loaded_source_ids() == []
    rig.load()                                        # and it can be used again
    assert rig.show(1.0) == pytest.approx(1.0, abs=TOL)


# ------------------------------------------------------------ offline / bad media
def _missing_source(tmp_path):
    return MediaSource(id="missing1", path=str(tmp_path / "gone.mp4"), duration=3.0,
                       width=640, height=360, fps=30.0, has_video=True, has_audio=True)


def test_missing_file_shows_offline_message(rig, tmp_path):
    rig.doc.add_source(_missing_source(tmp_path))
    rig.run(0.2)
    assert rig.panel.display_state() == "message"
    assert "offline" in rig.panel.status_message().lower()
    assert rig.panel.current_image() is None
    assert rig.panel.loaded_source_ids() == []
    assert rig.panel.grab().toImage().width() > 0
    rig.panel.step_frames(1)
    rig.doc.seek(1.0)
    rig.run(0.1)
    assert rig.panel.display_state() == "message"


def test_playing_an_offline_clip_runs_on_the_clock(rig, tmp_path):
    rig.doc.add_source(_missing_source(tmp_path))
    rig.run(0.1)
    rig.panel.set_rate(4.0)
    rig.panel.play()
    assert rig.panel.is_playing()
    assert rig.wait_for(lambda: not rig.panel.is_playing(), 6)
    assert rig.doc.playhead == pytest.approx(3.0, abs=1e-3)
    assert rig.panel.display_state() == "message"


def test_offline_clip_between_good_clips(rig, tmp_path):
    rig.load()
    rig.split_at(2.0)
    ghost = _missing_source(tmp_path)
    rig.doc.add_source(ghost, to_timeline=False)
    seg = rig.doc.timeline.segments[1]
    rig.doc.sources[ghost.id] = ghost
    seg.source_id = ghost.id                          # second part now points at a missing file
    seg.source_out = seg.source_in + 3.0
    rig.doc.timelineChanged.emit()
    rig.run(0.2)
    rig.doc.seek(1.0)
    rig.settle()
    assert rig.shown() == pytest.approx(1.0, abs=TOL)
    rig.doc.seek(3.0)
    rig.run(0.3)
    assert rig.panel.display_state() == "message"
    rig.doc.seek(0.0)
    rig.settle()
    rig.frames.clear()
    rig.panel.set_rate(2.0)
    rig.panel.play()
    assert rig.wait_for(lambda: not rig.panel.is_playing(), 8)
    assert rig.doc.playhead == pytest.approx(rig.doc.duration, abs=1e-3)
    assert all(s < 2.0 + TOL for s, _, _ in rig.frames)     # nothing from the missing media


def test_relink_after_missing_file_recovers(rig, tmp_path, clip):
    ghost = MediaSource(id=clip.id, path=str(tmp_path / "nope.mp4"), duration=clip.duration,
                        width=640, height=360, fps=30.0)
    rig.doc.add_source(ghost)
    rig.run(0.2)
    assert rig.panel.display_state() == "message"
    rig.doc.relink(ghost.id, clip.path)
    rig.settle()
    assert rig.panel.display_state() == "frame"
    assert rig.show(2.0) == pytest.approx(2.0, abs=TOL)


def test_undecodable_file_does_not_crash(rig, tmp_path):
    bad = tmp_path / "garbage.mp4"
    bad.write_bytes(b"this is not a video" * 100)
    src = MediaSource(id="bad1", path=str(bad), duration=2.0, width=640, height=360, fps=30.0)
    rig.doc.add_source(src)
    rig.run(1.5)
    assert rig.panel.display_state() in ("message", "black")
    rig.panel.step_frames(1)
    rig.panel.set_rate(4.0)
    rig.panel.play()
    assert rig.wait_for(lambda: not rig.panel.is_playing(), 8)
    assert rig.doc.playhead == pytest.approx(rig.doc.duration, abs=1e-3)


# ------------------------------------------------------------------ lifecycle / misc
def test_release_media_and_reuse(rig):
    rig.load()
    rig.show(1.0)
    rig.panel.release_media()
    assert rig.panel.loaded_source_ids() == []
    assert rig.panel.display_state() == "black"
    assert rig.show(2.0) == pytest.approx(2.0, abs=TOL)
    rig.panel.play()
    rig.run(0.3)
    rig.panel.release_media()
    assert not rig.panel.is_playing()
    rig.panel.release_media()                         # idempotent
    assert rig.show(3.0) == pytest.approx(3.0, abs=TOL)


def test_shutdown_is_idempotent_and_final(rig):
    rig.load()
    rig.panel.play()
    rig.run(0.3)
    rig.panel.shutdown()
    rig.panel.shutdown()
    assert not rig.panel.is_playing()
    assert rig.panel.loaded_source_ids() == []
    rig.panel.play()                                  # ignored after shutdown
    rig.doc.seek(1.0)                                 # document signals no longer reach it
    rig.doc.add_source(rig.src)
    rig.run(0.2)
    assert not rig.panel.is_playing()
    assert rig.panel.loaded_source_ids() == []


def test_volume_and_mute(rig):
    rig.load()
    p = rig.panel
    p._vol_slider.setValue(40)
    assert rig.settings.volume == pytest.approx(0.4)
    p.set_muted(True)
    assert p.is_muted()
    p._vol_slider.setValue(60)                        # raising the volume unmutes
    assert not p.is_muted()
    p._btn_mute.click()
    assert p.is_muted()
    p.play()
    rig.run(0.3)
    p.pause()


def test_same_source_shared_slot_and_pool_limit(rig, clip, tmp_path):
    rig.load()
    for t in (1.0, 2.0, 3.0, 4.0):
        rig.split_at(t)
    rig.panel.play()
    rig.run(0.5)
    rig.panel.pause()
    assert len(rig.panel.loaded_source_ids()) <= 2        # one file: active + optional standby
    assert set(rig.panel.loaded_source_ids()) == {clip.id}


# ------------------------------------------------------------------ detached audio lane
def _detach(rig):
    """Load the clip, detach its audio and return the new audio-lane clip."""
    rig.load()
    rig.doc.select([rig.doc.timeline.segments[0].id])
    assert rig.doc.detach_audio()
    aud = rig.doc.timeline.audio[0]
    assert rig.doc.timeline.segments[0].muted
    return aud


def _lane(rig):
    return rig.panel.audio_state()


def _lane_at(rig, src_t, tol=0.06):
    """The audio player is loaded, bound to the clip's file and positioned at ``src_t``."""
    def ok():
        st = _lane(rig)
        return bool(st and st["loaded"] and st["source_id"] == rig.src.id
                    and abs(st["position"] - src_t) < tol)
    return rig.wait_for(ok, 4.0)


def test_audio_lane_follows_the_playhead_when_paused(rig):
    aud = _detach(rig)
    rig.doc.seek(2.5)
    assert _lane_at(rig, 2.5)
    st = _lane(rig)
    assert st["seg_id"] == aud.id and not st["audible"] and not st["running"]
    rig.doc.seek(4.0)
    assert _lane_at(rig, 4.0)
    rig.doc.seek(0.0)
    assert _lane_at(rig, 0.0)


def test_audio_lane_maps_timeline_to_source_time_of_a_moved_clip(rig):
    aud = _detach(rig)
    assert rig.doc.move_clip(aud.id, 3.0)
    assert rig.doc.duration == pytest.approx(9.0)
    rig.doc.seek(5.0)
    assert _lane_at(rig, 2.0)                         # source_in + t - timeline_start
    rig.doc.seek(1.0)                                 # before the audio clip: nothing to play
    rig.run(0.2)
    st = _lane(rig)
    assert not st["audible"] and not st["running"]


def test_audio_lane_plays_in_sync_with_the_video(rig):
    _detach(rig)
    rig.doc.seek(1.0)
    rig.settle()
    rig.panel.play()
    assert rig.wait_for(lambda: (_lane(rig) or {}).get("running"), 4.0)
    assert _lane(rig)["audible"]
    rig.run(0.8)
    st = _lane(rig)
    assert st["running"] and st["audible"]
    assert abs(st["position"] - rig.doc.playhead) < 0.3
    assert rig.doc.playhead > 1.4
    rig.panel.pause()
    assert rig.wait_for(lambda: not _lane(rig)["running"], 2.0)
    assert not _lane(rig)["audible"]


def test_playback_continues_past_the_video_on_the_audio_lane(rig):
    aud = _detach(rig)
    assert rig.doc.move_clip(aud.id, 3.0)             # audio 3..9, video 0..6
    rig.doc.seek(5.0)
    rig.run(0.1)
    rig.panel.set_rate(4.0)
    rig.panel.play()
    assert rig.wait_for(lambda: rig.doc.playhead > 6.5, 6.0)
    assert rig.panel.is_playing()                     # video is over, audio keeps the clock going
    assert rig.panel.display_state() != "placeholder"
    st = _lane(rig)
    assert st["audible"] and st["seg_id"] == aud.id
    assert rig.wait_for(lambda: not rig.panel.is_playing(), 8.0)
    assert rig.doc.playhead == pytest.approx(9.0, abs=1e-3)
    assert not _lane(rig)["audible"]


def test_playing_in_an_audio_only_region_after_the_video(rig):
    aud = _detach(rig)
    assert rig.doc.move_clip(aud.id, 3.0)
    rig.doc.seek(7.0)
    rig.run(0.1)
    assert rig.panel.display_state() == "black"       # no video there
    assert _lane_at(rig, 4.0)
    rig.panel.set_rate(4.0)
    rig.panel.play()
    assert rig.wait_for(lambda: not rig.panel.is_playing(), 6.0)
    assert rig.doc.playhead == pytest.approx(9.0, abs=1e-3)


def test_audio_reaches_clip_start_during_playback_and_stops_after_clip(rig):
    aud = _detach(rig)
    assert rig.doc.move_clip(aud.id, 1.0)             # audio 1..7
    rig.doc.seek(0.5)
    rig.settle()
    rig.panel.set_rate(2.0)
    rig.panel.play()
    rig.run(0.05)
    assert not _lane(rig) or not _lane(rig)["audible"]
    assert rig.wait_for(lambda: (_lane(rig) or {}).get("audible"), 4.0)
    assert rig.doc.playhead >= 1.0 - 0.1
    assert rig.wait_for(lambda: not rig.panel.is_playing(), 8.0)
    assert not _lane(rig)["audible"]


def test_muted_audio_clip_is_silent_and_unmute_while_playing_takes_effect(rig):
    aud = _detach(rig)
    assert rig.doc.set_muted([aud.id], True)
    rig.doc.seek(1.0)
    rig.panel.play()
    rig.run(0.4)
    st = _lane(rig)
    assert st is not None and not st["audible"] and not st["running"]
    assert rig.doc.set_muted([aud.id], False)
    assert rig.wait_for(lambda: _lane(rig)["audible"] and _lane(rig)["running"], 3.0)
    assert abs(_lane(rig)["position"] - rig.doc.playhead) < 0.35
    assert rig.doc.set_muted([aud.id], True)
    assert rig.wait_for(lambda: not _lane(rig)["audible"] and not _lane(rig)["running"], 3.0)
    rig.panel.pause()


def test_master_mute_silences_the_audio_lane(rig):
    _detach(rig)
    rig.panel.set_muted(True)
    rig.doc.seek(1.0)
    rig.panel.play()
    rig.run(0.4)
    assert not _lane(rig)["audible"]
    rig.panel.set_muted(False)
    assert rig.wait_for(lambda: _lane(rig)["audible"], 3.0)
    rig.panel.pause()


def test_deleting_and_trimming_the_audio_clip_while_playing(rig):
    aud = _detach(rig)
    rig.doc.seek(0.5)
    rig.panel.play()
    assert rig.wait_for(lambda: (_lane(rig) or {}).get("audible"), 4.0)
    rig.doc.begin_drag()
    rig.doc.trim_start(aud.id, 1.0)
    rig.doc.end_drag()
    rig.run(0.3)
    assert rig.panel.is_playing()
    rig.doc.select([aud.id])
    assert rig.doc.delete_selected(ripple=False)
    rig.run(0.3)
    st = _lane(rig)
    assert not st["audible"] and not st["running"]
    assert rig.doc.undo()                              # clip is back
    rig.run(0.3)
    rig.panel.pause()
    rig.doc.seek(2.0)
    assert _lane_at(rig, 2.0)


def test_audio_lane_after_undo_redo_and_split(rig):
    aud = _detach(rig)
    rig.doc.seek(3.0)
    assert _lane_at(rig, 3.0)
    assert rig.doc.split_at_playhead()
    rig.doc.seek(4.5)
    assert _lane_at(rig, 4.5)
    assert rig.doc.undo()
    rig.doc.seek(5.0)
    assert _lane_at(rig, 5.0)
    assert rig.doc.redo()
    rig.doc.seek(1.0)
    assert _lane_at(rig, 1.0)


def test_audio_lane_is_torn_down_with_the_panel(rig):
    _detach(rig)
    rig.doc.seek(1.0)
    rig.panel.play()
    assert rig.wait_for(lambda: (_lane(rig) or {}).get("running"), 4.0)
    rig.panel.release_media()
    assert _lane(rig) is None and not rig.panel.is_playing()
    rig.doc.seek(2.0)
    assert _lane_at(rig, 2.0)                          # lazily recreated
    rig.panel.shutdown()
    assert _lane(rig) is None
    rig.doc.seek(3.0)
    rig.run(0.2)
    assert _lane(rig) is None


def test_missing_audio_source_does_not_crash(rig):
    aud = _detach(rig)
    rig.doc.sources[rig.src.id].path = rig.src.path + ".missing"
    rig.doc.sourcesChanged.emit()
    rig.doc.seek(1.0)
    rig.panel.play()
    rig.run(0.4)
    st = _lane(rig)
    assert st is None or (not st["audible"] and st["source_id"] is None)
    rig.panel.pause()
    rig.doc.sources[rig.src.id].path = rig.src.path
    rig.doc.sourcesChanged.emit()
