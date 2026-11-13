"""Export dialog: presets, canvas shape / fill mode sync with the document, captions burn-in."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, QThreadPool, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipforge.core.captions import CaptionLine  # noqa: E402
from clipforge.core.model import MediaSource  # noqa: E402
from clipforge.document import Document  # noqa: E402
from clipforge.media import export as ex  # noqa: E402
from clipforge.media import ffmpeg  # noqa: E402
from clipforge.settings import Settings  # noqa: E402
from clipforge.ui import theme  # noqa: E402
from clipforge.ui.export_dialog import ExportDialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication([])
    theme.apply_theme(inst, "dark")
    return inst


def settle(app, rounds: int = 3) -> None:
    for _ in range(rounds):
        QThreadPool.globalInstance().waitForDone()
        app.processEvents()


class FakeExportJob(QObject):
    progress = Signal(float, str)
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()
    instances: list = []

    def __init__(self, sources, segments, settings, *, temp_dir, parent=None, **extra):
        super().__init__(parent)
        self.settings, self.extra = settings, extra
        FakeExportJob.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        pass

    def is_running(self):
        return False


@pytest.fixture
def settings(tmp_path):
    s = Settings.load(tmp_path / "s.json")
    s.export_dir = str(tmp_path / "out")
    s.temp_dir = str(tmp_path / "temp")
    return s


@pytest.fixture
def doc(tmp_path):
    d = Document()
    path = tmp_path / "a.mp4"
    path.write_bytes(b"x" * 64)
    d.add_source(MediaSource(id="a", path=str(path), duration=20.0, width=1920, height=1080, fps=30.0,
                             video_codec="h264", audio_codec="aac", pix_fmt="yuv420p",
                             sample_rate=48000, channels=2, file_size=64, title="a"), to_timeline=True)
    d.clear_selection()
    return d


@pytest.fixture
def dialog(app, monkeypatch, doc, settings):
    FakeExportJob.instances = []
    monkeypatch.setattr(ex, "ExportJob", FakeExportJob)
    monkeypatch.setattr(ex, "can_fast_export", lambda s, seg, es: (True, ""))
    monkeypatch.setattr(ffmpeg, "pick_video_encoder", lambda codec, hw="auto": "libx264")
    d = ExportDialog(doc, settings)
    settle(app)
    yield d
    d.deleteLater()


def pick(d, key):
    i = d.preset_combo.findData(key)
    d.preset_combo.setCurrentIndex(i)
    d.preset_combo.activated.emit(i)


def user_pick(box, key):
    i = box.findData(key)
    box.setCurrentIndex(i)
    box.activated.emit(i)


def test_preset_list(dialog):
    texts = [dialog.preset_combo.itemText(i) for i in range(dialog.preset_combo.count())]
    assert texts[0] == "Custom"
    assert "TikTok / Reels / Shorts (9:16, 1080×1920, 30 fps)" in texts
    assert "Instagram feed (4:5, 1080×1350)" in texts
    assert "Square (1:1, 1080×1080)" in texts
    assert "YouTube 1080p (16:9)" in texts
    assert "Original quality (source size)" in texts
    assert dialog.preset_combo.currentData() == "custom"


@pytest.mark.parametrize("key,aspect,fps,reframe,size", [
    ("vertical", "9:16", "30", "fill", (1080, 1920)),
    ("portrait", "4:5", "30", "fill", (1080, 1350)),
    ("square", "1:1", "30", "fill", (1080, 1080)),
])
def test_social_presets_apply_and_sync_document(dialog, doc, key, aspect, fps, reframe, size):
    doc.set_canvas(reframe="blur")
    pick(dialog, key)
    es = dialog.collect_settings()
    assert (es.aspect, es.resolution, es.fps, es.reframe) == (aspect, "1080", fps, reframe)
    assert (es.codec, es.quality, es.audio, es.mode, es.preset) == ("h264", "high", True, "auto", key)
    assert doc.aspect == aspect and doc.reframe_mode == reframe
    assert dialog.output_size() == size
    assert f"{size[0]}×{size[1]}" in dialog.summary_label.text()
    assert f"{size[0]}×{size[1]}" in dialog.size_label.text()


def test_youtube_and_original_presets(dialog, doc):
    pick(dialog, "youtube")
    es = dialog.collect_settings()
    assert (es.aspect, es.resolution, es.fps) == ("16:9", "1080", "source")
    pick(dialog, "original")
    es = dialog.collect_settings()
    assert (es.aspect, es.resolution, es.fps, es.preset) == ("source", "source", "source", "original")
    assert doc.aspect == "source"
    assert dialog.output_size() == (1920, 1080)


def test_editing_a_related_control_switches_to_custom(dialog):
    pick(dialog, "vertical")
    assert dialog.preset_combo.currentData() == "vertical"
    dialog.fps_combo.setCurrentIndex(dialog.fps_combo.findData("60"))
    assert dialog.preset_combo.currentData() == "custom"
    assert dialog.collect_settings().preset == "custom"
    pick(dialog, "square")
    user_pick(dialog.aspect_combo, "16:9")
    assert dialog.preset_combo.currentData() == "custom"
    pick(dialog, "square")
    dialog.audio_check.setChecked(False)
    assert dialog.preset_combo.currentData() == "custom"
    pick(dialog, "square")
    dialog.codec_combo.setCurrentIndex(dialog.codec_combo.findData("h265"))
    assert dialog.preset_combo.currentData() == "custom"
    pick(dialog, "square")
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("accurate"))
    assert dialog.preset_combo.currentData() == "custom"


def test_dialog_canvas_controls_write_to_document(dialog, doc):
    user_pick(dialog.aspect_combo, "4:5")
    assert doc.aspect == "4:5"
    user_pick(dialog.reframe_combo, "fit")
    assert doc.reframe_mode == "fit"
    assert dialog.output_size() == (1080, 1350) or dialog.output_size()[0] < dialog.output_size()[1]


def test_document_canvas_change_updates_dialog(dialog, doc):
    pick(dialog, "vertical")
    doc.set_canvas(aspect="1:1", reframe="blur")
    assert dialog.aspect_combo.currentData() == "1:1" and dialog.reframe_combo.currentData() == "blur"
    assert dialog.preset_combo.currentData() == "custom"
    assert dialog.output_size()[0] == dialog.output_size()[1]


def test_loaded_preset_that_no_longer_matches_is_custom(app, doc, settings, monkeypatch):
    monkeypatch.setattr(ffmpeg, "pick_video_encoder", lambda codec, hw="auto": "libx264")
    monkeypatch.setattr(ex, "can_fast_export", lambda s, seg, es: (True, ""))
    from dataclasses import replace
    doc.set_export_settings(replace(doc.export_settings, preset="vertical", aspect="1:1"))
    d = ExportDialog(doc, settings)
    assert d.preset_combo.currentData() == "custom"
    d.deleteLater()


def test_source_resolution_label_hides_dimensions_with_canvas(dialog):
    assert "1920" in dialog.res_combo.itemText(0)
    user_pick(dialog.aspect_combo, "9:16")
    assert "1920" not in dialog.res_combo.itemText(0)
    assert dialog.res_combo.count() == 6          # all sizes: the canvas is independent of the source


def test_captions_row_visibility_and_stale_warning(app, dialog, doc):
    assert dialog.captions_check.isHidden() and dialog.captions_stale_label.isHidden()
    doc.set_captions([CaptionLine(1.0, 2.0, "hi")])
    assert not dialog.captions_check.isHidden() and dialog.captions_stale_label.isHidden()
    doc.seek(10.0)
    doc.split_at_playhead()
    assert not dialog.captions_stale_label.isHidden()
    assert dialog.captions_stale_label.text() == (
        "Captions are out of date: the timeline changed since they were made. "
        "Regenerate them in Captions.")
    doc.clear_captions()
    assert dialog.captions_check.isHidden()


def test_hint_when_fast_mode_and_reframing(dialog):
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("fast"))
    assert "accurate encoder" not in dialog.mode_info.text()
    user_pick(dialog.aspect_combo, "9:16")
    assert dialog.mode_info.text().startswith("Reframing needs the accurate encoder; "
                                              "ClipForge will use it automatically")


def test_export_passes_captions_only_when_burning(app, dialog, doc, tmp_path):
    dialog.name_edit.setText("clip")
    assert dialog.start_export()
    assert FakeExportJob.instances[-1].extra == {}
    dialog._show_page(0)
    dialog._end_job()
    dialog._exporting = False
    doc.set_captions([CaptionLine(1.0, 2.0, "hi")])
    assert dialog.start_export()
    job = FakeExportJob.instances[-1]
    assert job.extra["captions"][0].text == "hi" and job.extra["caption_style"] is doc.caption_style
    assert doc.export_settings.captions is True


def test_captions_unchecked_not_burned_and_remembered(app, dialog, doc):
    doc.set_captions([CaptionLine(1.0, 2.0, "hi")])
    dialog.captions_check.setChecked(False)
    assert dialog.start_export()
    assert FakeExportJob.instances[-1].extra == {}
    assert doc.export_settings.captions is False


def test_fast_mode_is_upgraded_when_reframing(dialog, doc):
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("fast"))
    user_pick(dialog.aspect_combo, "9:16")
    assert dialog.validate() is None
    assert dialog.start_export()
    job = FakeExportJob.instances[-1]
    assert job.settings.mode == "accurate" and job.settings.aspect == "9:16"
    assert doc.export_settings.mode == "fast"          # the user's own choice is what is saved
