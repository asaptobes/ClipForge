"""Captions dialog and the main window's captions / canvas controls (fake back-end jobs)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, QThreadPool, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipforge.core.captions import CaptionLine, Word, chunk_words  # noqa: E402
from clipforge.core.model import MediaSource  # noqa: E402
from clipforge.document import Document  # noqa: E402
from clipforge.settings import Settings  # noqa: E402
from clipforge.ui import captions_dialog as cd  # noqa: E402
from clipforge.ui import theme  # noqa: E402
from clipforge.ui.captions_dialog import CaptionsDialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication([])
    theme.apply_theme(inst, "dark")
    return inst


def settle(app, rounds: int = 3) -> None:
    for _ in range(rounds):
        QThreadPool.globalInstance().waitForDone()
        app.processEvents()


class FakeJob(QObject):
    progress = Signal(float, str)
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, *args, parent=None, **kwargs):
        super().__init__(parent)
        self.args, self.kwargs = args, kwargs
        self.started = False
        self.was_cancelled = False
        type(self).instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.was_cancelled = True

    def is_running(self):
        return self.started and not self.was_cancelled


class FakeDownload(FakeJob):
    instances: list = []


class FakeExtract(FakeJob):
    instances: list = []


class FakeTranscribe(FakeJob):
    finished = Signal(list)
    instances: list = []


class Backend:
    def __init__(self):
        self.available = True
        self.downloaded: set[str] = set()
        info = SimpleNamespace(key="small", label="Small", approx_download_mb=480)
        base = SimpleNamespace(key="base", label="Base", approx_download_mb=145)
        self.ns = SimpleNamespace(
            is_available=lambda: self.available,
            MODELS={"base": base, "small": info},
            recommended_model=lambda: "small",
            model_downloaded=lambda k: k in self.downloaded,
            ModelDownloadJob=FakeDownload, TranscribeJob=FakeTranscribe,
            gpu_status=lambda: SimpleNamespace(usable=True, name="RTX 4070", reason=""),
            words_to_lines=lambda words, n: chunk_words(words, n))


def make_source(tmp_path, sid="s1", name="a.mp4"):
    path = tmp_path / name
    path.write_bytes(b"x" * 64)
    return MediaSource(id=sid, path=str(path), duration=20.0, width=1920, height=1080, fps=30.0,
                       video_codec="h264", audio_codec="aac", pix_fmt="yuv420p", sample_rate=48000,
                       channels=2, file_size=64, title=sid)


@pytest.fixture
def backend(monkeypatch):
    FakeDownload.instances, FakeExtract.instances, FakeTranscribe.instances = [], [], []
    b = Backend()
    monkeypatch.setattr(cd, "tr", b.ns)
    monkeypatch.setattr(cd.ae, "ExtractAudioJob", FakeExtract)
    monkeypatch.setattr(CaptionsDialog, "confirm_download", lambda self, label, size: True)
    monkeypatch.setattr(CaptionsDialog, "confirm_replace", lambda self: True)
    monkeypatch.setattr(CaptionsDialog, "confirm_clear", lambda self: True)
    return b


@pytest.fixture
def settings(tmp_path):
    s = Settings.load(tmp_path / "s.json")
    s.temp_dir = str(tmp_path / "temp")
    return s


@pytest.fixture
def doc(tmp_path):
    d = Document()
    d.add_source(make_source(tmp_path), to_timeline=True)
    d.clear_selection()
    return d


@pytest.fixture
def dlg(app, backend, doc, settings):
    d = CaptionsDialog(doc, settings)
    settle(app)
    yield d
    d.shutdown()
    d.deleteLater()


def lines3():
    return [CaptionLine(1.0, 2.0, "hello there", [Word(1.0, 1.4, "hello"), Word(1.5, 2.0, "there")]),
            CaptionLine(2.5, 3.5, "second line", [Word(2.5, 3.0, "second"), Word(3.0, 3.5, "line")]),
            CaptionLine(4.0, 5.0, "third", [Word(4.0, 5.0, "third")])]


def run_to_transcribe(d, backend, words=None):
    backend.downloaded.add("small")
    assert d.start_generate()
    ex = FakeExtract.instances[-1]
    wav = ex.args[2]
    Path(wav).write_bytes(b"RIFF")
    ex.finished.emit(wav)
    return wav


# ------------------------------------------------------------------- generate
def test_defaults_and_device_note(dlg, app):
    assert dlg.model_combo.currentData() == "small"
    assert dlg.lang_combo.itemText(0) == "Auto-detect" and dlg.lang_combo.itemData(0) is None
    assert dlg.lang_combo.count() == 14
    assert "RTX 4070" in dlg.device_label.text()
    assert dlg.generate_button.isEnabled()


def test_unavailable_disables_generate(app, backend, doc, settings):
    backend.available = False
    d = CaptionsDialog(doc, settings)
    assert not d.generate_button.isEnabled()
    assert not d.unavailable_label.isHidden()
    assert d.start_generate() is False
    d.deleteLater()


def test_consent_declined_runs_nothing(dlg, monkeypatch):
    seen = []
    monkeypatch.setattr(CaptionsDialog, "confirm_download",
                        lambda self, label, size: seen.append((label, size)) or False)
    assert dlg.start_generate() is False
    assert seen == [("Small", "480 MB")]
    assert not FakeDownload.instances and not FakeExtract.instances and not dlg.running()


def test_full_flow_download_extract_transcribe(dlg, doc, backend, settings):
    assert dlg.start_generate()
    dl = FakeDownload.instances[-1]
    assert dl.started and dlg.progress_box.isHidden() is False
    dl.progress.emit(0.42, "x")
    assert "Downloading model" in dlg.stage_label.text() and "42 %" in dlg.stage_label.text()
    backend.downloaded.add("small")
    dl.finished.emit("path")
    ex = FakeExtract.instances[-1]
    assert ex.started and ex.args[2].endswith(".wav") and ex.args[3] == "wav"
    assert os.path.dirname(ex.args[2]) == str(settings.temp_path())
    assert "Preparing audio" in dlg.stage_label.text()
    wav = ex.args[2]
    Path(wav).write_bytes(b"RIFF")
    ex.finished.emit(wav)
    tj = FakeTranscribe.instances[-1]
    assert tj.args[0] == wav and tj.kwargs["model"] == "small" and tj.kwargs["language"] is None
    tj.progress.emit(0.5, "")
    assert "Listening" in dlg.stage_label.text() and "50 %" in dlg.stage_label.text()
    tj.finished.emit([Word(0.1, 0.4, "hello"), Word(0.5, 0.9, "world")])
    assert [c.text for c in doc.captions] == ["hello world"]
    assert not dlg.running() and not os.path.exists(wav)
    assert doc.captions_stale() is False


def test_language_is_passed(dlg, backend):
    dlg.lang_combo.setCurrentIndex(dlg.lang_combo.findData("es"))
    run_to_transcribe(dlg, backend)
    assert FakeTranscribe.instances[-1].kwargs["language"] == "es"


def test_words_are_shifted_to_timeline_start(app, backend, settings, tmp_path):
    d = Document()
    d.add_source(make_source(tmp_path), to_timeline=True)
    seg = d.timeline.segments[0]
    d.timeline.move_segment(seg.id, 2.0) if hasattr(d.timeline, "move_segment") else None
    start = min(s.timeline_start for s in d.timeline.all_segments())
    dlg = CaptionsDialog(d, settings)
    run_to_transcribe(dlg, backend)
    FakeTranscribe.instances[-1].finished.emit([Word(0.0, 0.5, "hi")])
    assert d.captions[0].start == pytest.approx(start)
    dlg.deleteLater()


def test_no_speech_message(dlg, doc, backend):
    run_to_transcribe(dlg, backend)
    FakeTranscribe.instances[-1].finished.emit([])
    assert doc.captions == [] and "No speech" in dlg.message_label.text()


def test_silent_timeline_says_so(dlg, doc, backend):
    backend.downloaded.add("small")
    doc.select_all()
    doc.toggle_mute_selected()
    assert dlg.start_generate() is False
    assert "no sound" in dlg.message_label.text() and not FakeExtract.instances


def test_failures_are_friendly_and_clean_up(dlg, backend):
    wav = run_to_transcribe(dlg, backend)
    FakeTranscribe.instances[-1].failed.emit("Model is broken")
    assert not dlg.running() and not os.path.exists(wav)
    assert "Model is broken" in dlg.message_label.text()
    assert dlg.message_label.property("role") == "error"
    assert dlg.generate_button.isEnabled()


def test_download_failure_message(dlg, backend):
    dlg.start_generate()
    FakeDownload.instances[-1].failed.emit("No internet.")
    assert "No internet." in dlg.message_label.text() and not dlg.running()


@pytest.mark.parametrize("stage", ["download", "audio", "transcribe"])
def test_cancel_at_each_stage_ignores_late_signals(dlg, doc, backend, stage):
    wav = None
    if stage == "download":
        dlg.start_generate()
        job = FakeDownload.instances[-1]
    elif stage == "audio":
        backend.downloaded.add("small")
        dlg.start_generate()
        job = FakeExtract.instances[-1]
        wav = job.args[2]
    else:
        wav = run_to_transcribe(dlg, backend)
        job = FakeTranscribe.instances[-1]
    dlg.cancel_button.click()
    assert job.was_cancelled and not dlg.running()
    assert dlg.generate_button.isEnabled() and dlg.generate_button.isVisibleTo(dlg)
    # a late result from the cancelled job must not touch the document or start more jobs
    n = len(FakeExtract.instances) + len(FakeTranscribe.instances)
    job.finished.emit([Word(0, 1, "late")] if stage == "transcribe" else "late.wav")
    assert doc.captions == []
    assert len(FakeExtract.instances) + len(FakeTranscribe.instances) == n


def test_close_mid_run_cancels_and_ignores_late(dlg, doc, backend):
    wav = run_to_transcribe(dlg, backend)
    job = FakeTranscribe.instances[-1]
    dlg.close()
    assert job.was_cancelled and not dlg.running()
    job.finished.emit([Word(0, 1, "late")])
    assert doc.captions == []


def test_escape_closes_and_cancels(dlg, backend):
    dlg.start_generate()
    dlg.reject()
    assert FakeDownload.instances[-1].was_cancelled


def test_timeline_edit_during_listening_cancels(dlg, doc, backend):
    run_to_transcribe(dlg, backend)
    job = FakeTranscribe.instances[-1]
    doc.seek(5.0)
    doc.split_at_playhead()
    assert job.was_cancelled and not dlg.running()
    assert "timeline changed" in dlg.message_label.text()


def test_project_switch_cancels_and_clears(dlg, doc, backend, app):
    doc.set_captions(lines3())
    settle(app)
    assert dlg.table.rowCount() == 3
    backend.downloaded.add("small")
    dlg.start_generate()
    job = FakeExtract.instances[-1]
    doc.new_project()
    settle(app)
    assert job.was_cancelled and not dlg.running()
    assert dlg.table.rowCount() == 0 and not dlg.generate_button.isEnabled()


def test_replace_declined_does_nothing(dlg, doc, backend, monkeypatch):
    doc.set_captions(lines3())
    monkeypatch.setattr(CaptionsDialog, "confirm_replace", lambda self: False)
    backend.downloaded.add("small")
    assert dlg.start_generate() is False and not FakeExtract.instances


# ------------------------------------------------------------------------ table
def test_table_lists_captions_and_stale_banner(dlg, doc, app):
    doc.set_captions(lines3())
    settle(app)
    assert dlg.table.rowCount() == 3
    assert dlg.table.item(0, 0).text() == "00:01.000" and dlg.table.item(1, 2).text() == "second line"
    assert dlg.stale_banner.isHidden()
    doc.seek(10.0)
    doc.split_at_playhead()
    assert not dlg.stale_banner.isHidden()


def test_table_edit_text_and_times_go_through_document(dlg, doc, app):
    doc.set_captions(lines3())
    settle(app)
    dlg.table.item(1, 2).setText("edited  text")
    assert doc.captions[1].text == "edited text" and doc.captions[1].words == []
    dlg.table.item(0, 1).setText("00:01.500")
    assert doc.captions[0].end == pytest.approx(1.5)
    dlg.table.item(2, 0).setText("4.5")
    assert doc.captions[2].start == pytest.approx(4.5)
    settle(app)
    assert dlg.table.item(2, 0).text() == "00:04.500"


def test_table_bad_edits_are_rejected(dlg, doc, app):
    doc.set_captions(lines3())
    settle(app)
    dlg.table.item(0, 0).setText("abc")
    assert doc.captions[0].start == 1.0 and dlg.message_label.property("role") == "warning"
    dlg.table.item(0, 2).setText("   ")
    assert doc.captions[0].text == "hello there"
    dlg.table.item(0, 1).setText("0:00.5")      # end before start
    assert doc.captions[0].end == 2.0
    settle(app)
    assert dlg.table.item(0, 0).text() == "00:01.000"


def test_row_click_seeks_and_delete_removes(dlg, doc, app):
    doc.set_captions(lines3())
    settle(app)
    dlg.table.cellClicked.emit(1, 0)
    assert doc.playhead == pytest.approx(2.5)
    dlg.table.selectRow(0)
    dlg.table.selectRow(2)
    dlg.delete_selected()
    settle(app)
    assert [c.text for c in doc.captions] == ["hello there", "second line"]
    assert len(doc.captions) == 2
    dlg.table.selectRow(0)
    dlg.delete_button.click()
    assert len(doc.captions) == 1


def test_clear_all_confirms(dlg, doc, app, monkeypatch):
    doc.set_captions(lines3())
    settle(app)
    monkeypatch.setattr(CaptionsDialog, "confirm_clear", lambda self: False)
    dlg.clear_button.click()
    assert len(doc.captions) == 3
    monkeypatch.setattr(CaptionsDialog, "confirm_clear", lambda self: True)
    dlg.clear_button.click()
    settle(app)
    assert doc.captions == [] and not dlg.empty_label.isHidden()


def test_regenerate_button_starts_pipeline(dlg, doc, backend, app):
    doc.set_captions(lines3())
    doc.seek(10.0)
    doc.split_at_playhead()
    backend.downloaded.add("small")
    dlg.regenerate_button.click()
    assert FakeExtract.instances


# ------------------------------------------------------------------------- style
def test_style_controls_update_document(dlg, doc, app, monkeypatch):
    dlg.preset_buttons["boxed"].click()
    assert doc.caption_style.preset == "boxed" and doc.caption_style.box is True
    assert dlg.box_check.isChecked()
    dlg.position_buttons["top"].click()
    assert doc.caption_style.position == "top"
    dlg.size_slider.setValue(70)
    assert doc.caption_style.size_pct == pytest.approx(7.0)
    dlg.upper_check.setChecked(False)
    assert doc.caption_style.uppercase is False
    dlg.highlight_check.setChecked(False)
    assert doc.caption_style.highlight_words is False
    dlg.box_check.setChecked(False)
    assert doc.caption_style.box is False
    monkeypatch.setattr(CaptionsDialog, "pick_color", lambda self, initial, title: "#112233")
    dlg.color_buttons["highlight"].click()
    assert doc.caption_style.highlight == "#112233"
    monkeypatch.setattr(CaptionsDialog, "pick_color", lambda self, initial, title: None)
    dlg.color_buttons["primary"].click()
    assert doc.caption_style.primary == "#FFFFFF"


def test_style_follows_document(dlg, doc):
    from clipforge.core.captions import style_for_preset
    doc.set_caption_style(style_for_preset("minimal"))
    assert dlg.preset_buttons["minimal"].isChecked() and dlg.position_buttons["bottom"].isChecked()
    assert dlg.words_spin.value() == 8 and not dlg.upper_check.isChecked()


def test_words_per_line_offers_rechunk(dlg, doc, app, monkeypatch):
    doc.set_captions(lines3())
    asked = []
    monkeypatch.setattr(CaptionsDialog, "confirm_rechunk", lambda self, n: asked.append(n) or True)
    dlg.words_spin.setValue(5)
    assert doc.caption_style.words_per_line == 5 and dlg._rechunk_timer.isActive()
    dlg._offer_rechunk()
    assert asked == [5]
    assert [c.text for c in doc.captions] == ["hello there second line", "third"]


def test_rechunk_declined_keeps_lines(dlg, doc, monkeypatch):
    doc.set_captions(lines3())
    monkeypatch.setattr(CaptionsDialog, "confirm_rechunk", lambda self, n: False)
    dlg.words_spin.setValue(1)
    dlg._offer_rechunk()
    assert len(doc.captions) == 3


# ---------------------------------------------------------------- main window
@pytest.fixture
def window(app, backend, settings, monkeypatch):
    from clipforge.ui import main_window as mw
    monkeypatch.setattr(mw.MainWindow, "_check_dependencies", lambda self: None)
    monkeypatch.setattr(mw.MainWindow, "maybe_save", lambda self: True)
    w = mw.MainWindow(settings)
    yield w
    w._closing = True
    w.close()
    w.deleteLater()


def test_canvas_selector_syncs_with_document(window, app):
    assert window.canvas_combo.currentData() == "source" and window.fill_combo.isHidden()
    window.document.set_canvas(aspect="9:16")
    assert window.canvas_combo.currentData() == "9:16" and not window.fill_combo.isHidden()
    i = window.canvas_combo.findData("1:1")
    window.canvas_combo.setCurrentIndex(i)
    window.canvas_combo.activated.emit(i)
    assert window.document.aspect == "1:1"
    j = window.fill_combo.findData("blur")
    window.fill_combo.setCurrentIndex(j)
    window.fill_combo.activated.emit(j)
    assert window.document.reframe_mode == "blur"


def test_captions_action_and_singleton(window, tmp_path, app):
    assert window.act_captions.shortcut().toString() == "Ctrl+Shift+C"
    assert not window.act_captions.isEnabled()
    window.document.add_source(make_source(tmp_path), to_timeline=True)
    assert window.act_captions.isEnabled()
    window.open_captions()
    first = window.captions_dialog()
    assert first is not None and first.isVisible()
    window.open_captions()
    assert window.captions_dialog() is first
    window.document.new_project()
    settle(app)
    assert window.captions_dialog() is None
    window.document.add_source(make_source(tmp_path, "s2", "b.mp4"), to_timeline=True)
    window.open_captions()
    assert window.captions_dialog() is not None


def test_close_window_closes_captions(window, tmp_path, backend, app):
    window.document.add_source(make_source(tmp_path), to_timeline=True)
    window.open_captions()
    d = window.captions_dialog()
    backend.downloaded.add("small")
    d.start_generate()
    job = FakeExtract.instances[-1]
    window.close()
    assert job.was_cancelled


def test_crop_nudge_shortcuts_are_alt_modified(window, app):
    from PySide6.QtGui import QKeySequence
    acts = {a.text(): a for a in window.actions()}
    keys = {"Nudge crop left": "Alt+Left", "Nudge crop right": "Alt+Right",
            "Nudge crop left (big)": "Alt+Shift+Left", "Nudge crop right (big)": "Alt+Shift+Right"}
    for text, key in keys.items():
        assert acts[text].shortcut() == QKeySequence(key)
    assert window.act_prev_frame.shortcut() == QKeySequence("Left")
    calls = []
    window.preview.nudge_crop = lambda d, big=False: calls.append((d, big)) or True
    for act in (window.act_nudge_left, window.act_nudge_right,
                window.act_nudge_left_big, window.act_nudge_right_big):
        act.trigger()
    assert calls == [(-1, False), (1, False), (-1, True), (1, True)]
