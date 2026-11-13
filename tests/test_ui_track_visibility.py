"""Per-track visibility (the eye button): model + undo + project file, preview, export plan and
the timeline gutter. A hidden track is neither shown nor heard in the preview and is left out
of the export; its clips stay on the timeline."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QImage, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import settings_for, sources_of  # noqa: E402

from clipforge.core.history import History  # noqa: E402
from clipforge.core.model import EditError, MediaSource, Segment, Timeline, TimelineSnapshot  # noqa: E402
from clipforge.core.project import Project  # noqa: E402
from clipforge.document import Document  # noqa: E402
from clipforge.media import MediaError  # noqa: E402
from clipforge.media.export import build_plan  # noqa: E402
from clipforge.ui import preview as pv  # noqa: E402
from clipforge.ui import theme  # noqa: E402
from clipforge.ui import timeline as tl  # noqa: E402
from clipforge.ui.timeline import TimelinePanel  # noqa: E402


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication([])
    theme.apply_theme(inst, "dark")
    return inst


def fake(name="a", dur=6.0) -> MediaSource:
    return MediaSource(id=name, path=f"C:/nope/{name}.mp4", duration=dur, fps=30.0,
                       width=1920, height=1080)


def three_tracks() -> Document:
    doc = Document()
    doc.add_source(fake("a"))
    doc.add_source(fake("b"), track=1, at=0.0)
    doc.add_source(fake("c"), track=2, at=1.0)
    doc.history.clear()
    return doc


# ------------------------------------------------------------------------------ model
def test_hidden_defaults_to_visible_and_toggles():
    tlm = Timeline(track_count=3)
    assert tlm.track_hidden == set()
    tlm.set_track_hidden(1, True)
    assert tlm.is_track_hidden(1) and not tlm.is_track_hidden(0)
    tlm.set_track_hidden(1, False)
    assert tlm.track_hidden == set()


def test_cannot_hide_without_a_second_track_or_unknown_track():
    with pytest.raises(EditError):
        Timeline().set_track_hidden(0, True)
    with pytest.raises(EditError):
        Timeline(track_count=2).set_track_hidden(5, True)


def test_segments_at_visible_only():
    doc = three_tracks()
    doc.set_track_hidden(1, True)
    tlm = doc.timeline
    assert [s.layer for s in tlm.segments_at(1.5)] == [0, 1, 2]
    assert [s.layer for s in tlm.segments_at(1.5, visible_only=True)] == [0, 2]


def test_snapshot_carries_hidden_and_equality():
    tlm = Timeline(track_count=3)
    plain = tlm.snapshot()
    tlm.set_track_hidden(2, True)
    snap = tlm.snapshot()
    assert snap.hidden == (2,) and plain.hidden == ()
    assert snap != plain
    tlm.restore(plain)
    assert tlm.track_hidden == set()
    tlm.restore(snap)
    assert tlm.track_hidden == {2}
    assert TimelineSnapshot([], 3, hidden=[7, -1, 1]).hidden == (1,)       # out-of-range dropped


def test_add_and_remove_track_shift_hidden_indices():
    tlm = Timeline(track_count=3)
    tlm.set_track_hidden(2, True)
    tlm.add_track(1)                       # old V3 becomes V4
    assert tlm.track_hidden == {3}
    tlm.remove_track(1)                    # and back
    assert tlm.track_hidden == {2}
    tlm.remove_track(2)                    # removing the hidden track forgets it
    assert tlm.track_hidden == set()
    tlm.set_track_hidden(1, True)
    tlm.remove_track(1)                    # back to a single track: nothing can be hidden
    assert tlm.track_count == 1 and tlm.track_hidden == set()


def test_document_toggle_is_one_undo_step():
    doc = three_tracks()
    changed = []
    doc.timelineChanged.connect(lambda: changed.append(1))
    assert doc.set_track_hidden(1, True)
    assert doc.is_track_hidden(1) and doc.dirty and doc.history.undo_label() == "Hide track"
    assert changed
    assert not doc.set_track_hidden(1, True)           # no change: no undo step
    assert len(doc.history.undo_stack) == 1
    assert doc.undo()
    assert not doc.is_track_hidden(1)
    assert doc.redo()
    assert doc.is_track_hidden(1)
    assert doc.set_track_hidden(1, False) and doc.history.undo_label() == "Show track"
    assert not doc.set_track_hidden(9, True)


def test_hidden_survives_other_edits_undo():
    doc = three_tracks()
    doc.set_track_hidden(2, True)
    doc.select([doc.timeline.track(1)[0].id])
    assert doc.set_muted(doc.selection, True)
    assert doc.undo()                                  # the mute, not the hide
    assert doc.is_track_hidden(2)
    assert doc.undo()
    assert not doc.is_track_hidden(2)


# ------------------------------------------------------------------------- project file
def test_project_roundtrip_and_old_files_load_visible(tmp_path):
    doc = three_tracks()
    doc.set_track_hidden(1, True)
    path = str(tmp_path / "p.clipforge")
    doc.save(path)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["hidden_tracks"] == [1]
    loaded = Project.load(path)
    assert loaded.timeline.track_hidden == {1}
    assert loaded.history.undo_stack[-1].segments.hidden == ()     # state before the hide
    del data["hidden_tracks"]                                       # a file from before this feature
    for entry in data["history"]["undo"]:
        entry.pop("hidden", None)
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    assert Project.load(path).timeline.track_hidden == set()
    data["hidden_tracks"] = "nonsense"
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    assert Project.load(path).timeline.track_hidden == set()


def test_history_entry_roundtrip():
    h = History()
    h.record("x", TimelineSnapshot([], 3, hidden=[1]))
    h2 = History()
    h2.load_dict(h.to_dict())
    assert h2.undo_stack[0].segments.hidden == (1,)


# --------------------------------------------------------------------------- export plan
def clip(src, a, b, start, layer=0, **kw) -> Segment:
    return Segment(uuid.uuid4().hex[:12], src.id, a, b, start, layer=layer, **kw)


def graph_of(plan) -> str:
    a = plan.steps[0].args
    return a[a.index("-filter_complex") + 1]


def plan_of(tmp_path, srcs, segs, **kw):
    hidden = kw.pop("hidden_layers", ())
    return build_plan(sources_of(*srcs), segs, settings_for(tmp_path, **kw),
                      temp_dir=str(tmp_path / "tmp"), video_encoder="libx264",
                      hidden_layers=hidden)


def test_export_nothing_hidden_graph_is_unchanged(tmp_path, video_h, video_v):
    segs = [clip(video_h, 0, 4, 0), clip(video_v, 0, 2, 1.0, layer=1, scale=0.4)]
    base = graph_of(plan_of(tmp_path, [video_h, video_v], segs))
    assert graph_of(plan_of(tmp_path, [video_h, video_v], segs, hidden_layers=())) == base
    assert graph_of(plan_of(tmp_path, [video_h, video_v], segs, hidden_layers=[5])) == base


def test_export_leaves_out_hidden_overlay(tmp_path, video_h, video_v):
    base_seg = clip(video_h, 0, 4, 0)
    ov = clip(video_v, 0, 2, 1.0, layer=1, scale=0.4)
    only_base = plan_of(tmp_path, [video_h], [base_seg])
    hidden = plan_of(tmp_path, [video_h, video_v], [base_seg, ov], hidden_layers=[1])
    assert graph_of(hidden) == graph_of(only_base)
    assert "overlay" not in graph_of(hidden)
    assert hidden.steps[0].args.count("-i") == 1


def test_export_hidden_base_gives_black_under_overlays(tmp_path, video_h, video_v):
    base_seg = clip(video_h, 0, 4, 0)
    ov = clip(video_v, 0, 2, 1.0, layer=1, scale=0.4)
    plan = plan_of(tmp_path, [video_h, video_v], [base_seg, ov], hidden_layers=[0])
    g = graph_of(plan)
    assert "color=c=black" in g and "overlay=" in g
    assert plan.steps[0].args.count("-i") == 1          # the hidden base file is not read
    assert plan.duration == pytest.approx(2.0)          # only the overlay is left (starts at 0)


def test_export_keeps_audio_lane_and_rejects_everything_hidden(tmp_path, video_h):
    segs = [clip(video_h, 0, 3, 0), clip(video_h, 0, 3, 0, layer=1)]
    audio = Segment(uuid.uuid4().hex[:12], video_h.id, 0, 3, 0, track="audio")
    plan = plan_of(tmp_path, [video_h], segs + [audio], hidden_layers=[0, 1])
    assert plan.duration == pytest.approx(3.0)           # black picture with the audio lane's sound
    with pytest.raises(MediaError):
        plan_of(tmp_path, [video_h], segs, hidden_layers=[0, 1])


# ------------------------------------------------------------------------------ preview
class _Settings:
    volume = 1.0

    def save(self):
        pass


def solid(color: str, w=320, h=180) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(color)
    return img


def test_preview_overlay_hidden_is_not_composited(app):
    doc = three_tracks()
    panel = pv.PreviewPanel(doc, _Settings())
    panel._resync_timer.stop()
    panel._enter = lambda t: None                      # no media engine: only the bookkeeping
    panel.resize(700, 500)
    panel.show()
    app.processEvents()
    doc.seek(1.5)
    panel._refresh_overlays()
    vp = panel._viewport
    assert [s.layer for s, _i, _a in vp.overlays()] == [1, 2]
    doc.select([doc.timeline.track(1)[0].id])
    assert panel._selected_overlay_id() is not None
    doc.set_track_hidden(1, True)                      # timelineChanged -> panel refresh
    assert [s.layer for s, _i, _a in vp.overlays()] == [2]
    assert panel._selected_overlay_id() is None        # a hidden clip carries no handles
    doc.set_track_hidden(1, False)
    assert [s.layer for s, _i, _a in vp.overlays()] == [1, 2]
    panel.shutdown()
    panel.close()


def _wait(app, cond, timeout=8.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def test_preview_real_players_hide_and_show(app, video_h, video_v):
    doc = Document()
    doc.add_source(video_h)
    doc.add_source(video_v, track=1, at=0.0)
    doc.history.clear()
    panel = pv.PreviewPanel(doc, _Settings())
    panel.resize(800, 500)
    panel.show()
    try:
        doc.seek(1.0)
        assert _wait(app, lambda: panel.display_state() == "frame"
                     and all(v["has_image"] for v in panel.layer_state().values())
                     and panel.layer_state())
        assert len(panel._viewport.overlays()) == 1
        # hide the overlay track: its picture goes, its player is parked and silent
        doc.set_track_hidden(1, True)
        assert _wait(app, lambda: not panel._viewport.overlays()
                     and all(v["seg_id"] is None for v in panel.layer_state().values()))
        assert not any(v["audible"] for v in panel.layer_state().values())
        assert panel.display_state() == "frame"        # V1 is still there
        # hide the base too: black, no picture
        doc.set_track_hidden(0, True)
        assert _wait(app, lambda: panel.display_state() == "black")
        assert panel.current_image() is None
        doc.seek(2.0)                                  # a new position stays black
        assert _wait(app, lambda: panel.display_state() == "black" and panel._doc.playhead == 2.0)
        assert not any(sl.player.audioOutput() is not None and not sl.audio.isMuted()
                       for sl in panel._slots if sl.audio is not None)
        # show everything again
        doc.set_track_hidden(0, False)
        doc.set_track_hidden(1, False)
        assert _wait(app, lambda: panel.display_state() == "frame"
                     and len(panel._viewport.overlays()) == 1
                     and all(v["has_image"] for v in panel.layer_state().values()))
    finally:
        panel.shutdown()
        panel.close()


# ----------------------------------------------------------------------- timeline gutter
@pytest.fixture
def tlenv(app):
    doc = three_tracks()
    panel = TimelinePanel(doc, None, None)
    panel.resize(1000, 420)
    panel.show()
    app.processEvents()
    yield app, doc, panel
    panel.close()
    panel.deleteLater()
    doc.deleteLater()
    app.processEvents()


def click(view, pos: QPoint) -> None:
    for kind, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                          (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
        ev = QMouseEvent(kind, QPointF(pos), QPointF(view.mapToGlobal(pos)),
                         Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view, ev)


def test_gutter_eye_button_toggles_track(tlenv):
    app, doc, panel = tlenv
    view = panel.view
    assert view.gutter_width() == tl.GUTTER_W
    rect = view.gutter_button_rect("eye", 1)
    assert view.gutter_button_at(rect.center()) == ("eye", 1)
    doc.clear_selection()
    click(view, rect.center())
    assert doc.is_track_hidden(1) and not doc.is_track_hidden(0)
    assert doc.selection == [] and doc.playhead == 0.0   # the click is not a seek / selection
    click(view, rect.center())
    assert not doc.is_track_hidden(1)
    # a second click on the V3 eye is independent
    click(view, view.gutter_button_rect("eye", 2).center())
    assert doc.timeline.track_hidden == {2}
    assert doc.undo() and doc.timeline.track_hidden == set()


def test_gutter_speaker_button_mutes_whole_track(tlenv):
    app, doc, panel = tlenv
    view = panel.view
    mute = view.gutter_button_rect("mute", 2).center()
    click(view, mute)
    assert all(s.muted for s in doc.timeline.track(2))
    assert not any(s.muted for s in doc.timeline.track(1))
    assert view.track_muted(2)
    click(view, mute)
    assert not any(s.muted for s in doc.timeline.track(2))


def test_gutter_buttons_ignore_audio_lane_and_empty_tracks(tlenv):
    app, doc, panel = tlenv
    view = panel.view
    doc.add_track()
    app.processEvents()
    empty = doc.track_count - 1
    n_undo = len(doc.history.undo_stack)
    click(view, view.gutter_button_rect("mute", empty).center())      # nothing to mute
    assert len(doc.history.undo_stack) == n_undo
    click(view, view.gutter_button_rect("eye", empty).center())       # an empty track can hide
    assert doc.is_track_hidden(empty)
    assert view.gutter_button_at(QPoint(view.gutter_width() + 5, 100)) is None


def test_hidden_lane_is_painted_faint_and_tooltips_name_the_action(tlenv):
    app, doc, panel = tlenv
    view = panel.view
    seg = doc.timeline.track(1)[0]
    pos = QPoint(int(view.time_to_x(1.0)), view.lanes(1)[0] + 30)
    before = view.grab().toImage().pixelColor(pos)
    doc.set_track_hidden(1, True)
    app.processEvents()
    after = view.grab().toImage().pixelColor(pos)
    assert before != after and seg.id                  # the clip fades into the lane background
    assert "Show" in view._gutter_tip(("eye", 1))
    assert "Hide" in view._gutter_tip(("eye", 0))
    assert "Mute" in view._gutter_tip(("mute", 0))
    doc.set_track_hidden(1, False)
    app.processEvents()
    assert view.grab().toImage().pixelColor(pos) == before
