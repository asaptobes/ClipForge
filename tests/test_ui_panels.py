"""Tests for the left panel, export dialog and settings dialog (fake back-end jobs)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, QThreadPool, Qt, Signal  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipforge.core.model import ExportSettings, MediaSource  # noqa: E402
from clipforge.core.timecode import format_duration  # noqa: E402
from clipforge.document import Document  # noqa: E402
from clipforge.media import MediaError  # noqa: E402
from clipforge.media import downloader as dl  # noqa: E402
from clipforge.media import export as ex  # noqa: E402
from clipforge.media import ffmpeg  # noqa: E402
from clipforge.settings import Settings  # noqa: E402
from clipforge.ui import export_dialog, left_panel, settings_dialog, theme  # noqa: E402
from clipforge.ui.export_dialog import ExportDialog  # noqa: E402
from clipforge.ui.left_panel import LeftPanel  # noqa: E402
from clipforge.ui.settings_dialog import SettingsDialog  # noqa: E402

URL = "https://www.youtube.com/watch?v=abc12345678"


@pytest.fixture(scope="session")
def app():
    inst = QApplication.instance() or QApplication([])
    theme.apply_theme(inst, "dark")
    return inst


def settle(app, rounds: int = 3) -> None:
    for _ in range(rounds):
        QThreadPool.globalInstance().waitForDone()
        app.processEvents()


# ------------------------------------------------------------------ fakes
def jpeg_bytes() -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray
    img = QImage(64, 36, QImage.Format.Format_RGB32)
    img.fill(QColor("#336699"))
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "JPG")
    return bytes(data)


def make_info(heights=(1080, 720, 480), short=False, live=False, title="A test video"):
    avail = ["best"] + [str(q) for q in (2160, 1440, 1080, 720, 480) if q in heights]
    return SimpleNamespace(url=URL, video_id="abc12345678", title=title, duration=125.0,
                           uploader="Chan", thumbnail_url="", thumbnail_jpeg=jpeg_bytes(),
                           heights=list(heights), is_short=short, is_live=live,
                           available_qualities=lambda: avail)


class FakeMeta(QObject):
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    instances: list = []

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url
        self.started = False
        self.was_cancelled = False
        FakeMeta.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.was_cancelled = True


class FakeDownload(QObject):
    progress = Signal(float, str, str)
    stage = Signal(str)
    finished = Signal(str, object)
    failed = Signal(str)
    cancelled = Signal()
    instances: list = []

    def __init__(self, info, quality, dest_dir, parent=None):
        super().__init__(parent)
        self.info, self.quality, self.dest_dir = info, quality, dest_dir
        self.started = False
        self.was_cancelled = False
        FakeDownload.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.was_cancelled = True

    def is_running(self):
        return self.started


class FakeExportJob(QObject):
    progress = Signal(float, str)
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()
    instances: list = []

    def __init__(self, sources, segments, settings, *, temp_dir, parent=None):
        super().__init__(parent)
        self.sources, self.segments, self.settings, self.temp_dir = sources, segments, settings, temp_dir
        self.started = False
        self.was_cancelled = False
        FakeExportJob.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.was_cancelled = True

    def is_running(self):
        return self.started


class FakeThumbs(QObject):
    thumbnailReady = Signal(str, float)

    def __init__(self):
        super().__init__()
        self.ready: dict[str, QImage] = {}
        self.requests: list[str] = []

    def poster(self, source):
        self.requests.append(source.id)
        return self.ready.get(source.id)


@pytest.fixture
def backend(monkeypatch):
    FakeMeta.instances, FakeDownload.instances, FakeExportJob.instances = [], [], []

    def fake_validate(text):
        text = text.strip()
        if text.startswith("https://www.youtube.com/watch?v="):
            return text
        if text.startswith("https://youtu.be/"):
            return URL
        raise MediaError("That doesn't look like a YouTube link.")

    monkeypatch.setattr(dl, "validate_url", fake_validate)
    monkeypatch.setattr(dl, "MetadataJob", FakeMeta)
    monkeypatch.setattr(dl, "DownloadJob", FakeDownload)
    monkeypatch.setattr(ex, "ExportJob", FakeExportJob)
    monkeypatch.setattr(ex, "can_fast_export", lambda s, seg, es: (True, ""))
    monkeypatch.setattr(ffmpeg, "pick_video_encoder",
                        lambda codec, hw="auto": "libx264" if hw == "off" else "h264_nvenc")
    monkeypatch.setattr(ffmpeg, "version", lambda: "8.1.3")
    return SimpleNamespace(meta=FakeMeta, download=FakeDownload, export=FakeExportJob)


def make_source(tmp_path, sid="s1", name="clip.mp4", w=1920, h=1080, exists=True, **kw):
    path = tmp_path / name
    if exists:
        path.write_bytes(b"x" * 2048)
    fields = dict(id=sid, path=str(path), duration=42.0, width=w, height=h, fps=30.0,
                  video_codec="h264", audio_codec="aac", pix_fmt="yuv420p", sample_rate=48000,
                  channels=2, file_size=2048, title=f"Title {sid}")
    fields.update(kw)
    return MediaSource(**fields)


@pytest.fixture
def settings(tmp_path):
    s = Settings.load(tmp_path / "s.json")
    s.download_dir = str(tmp_path / "dl")
    s.export_dir = str(tmp_path / "out")
    s.cache_dir = str(tmp_path / "cache")
    s.temp_dir = str(tmp_path / "temp")
    return s


@pytest.fixture
def doc():
    return Document()


@pytest.fixture
def panel(app, backend, doc, settings):
    p = LeftPanel(doc, settings, FakeThumbs())
    p.resize(300, 800)
    yield p
    p.shutdown()
    p.deleteLater()


def open_url(panel, text=URL, info=None):
    panel.url_edit.setText(text)
    panel.validate_now()
    if info is not None:
        panel._meta_job.finished.emit(info)


# ================================================================= LeftPanel: URL
def test_url_empty_state(panel):
    assert panel.state() == "idle"
    assert panel.url_error.isHidden()
    assert panel.info_card.isHidden()
    assert not panel.download_button.isEnabled() or panel.quality_row.isHidden()


def test_url_typing_is_debounced(panel, backend):
    panel.url_edit.setText(URL)
    assert panel._debounce.isActive()
    assert backend.meta.instances == []          # nothing fetched before the debounce fires
    panel.validate_now()
    assert len(backend.meta.instances) == 1 and backend.meta.instances[0].started


def test_url_invalid_shows_error(panel, backend):
    open_url(panel, "not a link")
    assert panel.url_edit.property("state") == "error"
    assert not panel.url_error.isHidden()
    assert "YouTube" in panel.url_error.text()
    assert backend.meta.instances == []
    assert panel.state() == "idle"
    open_url(panel, "")                            # emptied: error clears
    assert panel.url_error.isHidden()
    assert panel.url_edit.property("state") == ""


def test_url_valid_starts_fetch_once(panel, backend):
    open_url(panel)
    assert panel.url_edit.property("state") == "ok"
    assert panel.state() == "fetching"
    panel.validate_now()                           # same URL again: no second job
    assert len(backend.meta.instances) == 1


def test_url_change_abandons_old_fetch(panel, backend):
    open_url(panel)
    first = backend.meta.instances[0]
    open_url(panel, "https://www.youtube.com/watch?v=zzzzzzzzzzz")
    assert first.was_cancelled
    first.finished.emit(make_info(title="stale"))   # late result of the old job is ignored
    assert panel.state() == "fetching"
    backend.meta.instances[1].finished.emit(make_info(title="fresh"))
    assert panel.state() == "ready"
    assert panel.info_title.text() == "fresh"


def test_metadata_failure_offers_retry(panel, backend):
    open_url(panel)
    panel._meta_job.failed.emit("Video unavailable")
    assert panel.state() == "message"
    assert panel.message_label.text() == "Video unavailable"
    assert not panel.retry_button.isHidden()
    panel.retry_button.click()
    assert panel.state() == "fetching"
    assert len(backend.meta.instances) == 2


# ============================================================ LeftPanel: metadata
def test_metadata_card_and_disabled_qualities(panel):
    open_url(panel, info=make_info(heights=(1080, 720, 480), short=True, title="Hello"))
    assert panel.state() == "ready"
    assert panel.info_title.text() == "Hello"
    assert panel.info_uploader.text() == "Chan"
    assert panel.info_duration.text() == "2:05"
    assert not panel.info_badge.isHidden()
    assert panel.info_thumb.has_image()
    combo = panel.quality_combo
    enabled = {combo.itemData(i): combo.model().item(i).isEnabled() for i in range(combo.count())}
    assert enabled == {"best": True, "2160": False, "1440": False, "1080": True, "720": True,
                       "480": True}
    assert panel.selected_quality() == "1080"      # settings.preferred_resolution
    assert panel.download_button.isEnabled()


def test_default_quality_falls_back_below_preferred(panel, settings):
    settings.preferred_resolution = "2160"
    open_url(panel, info=make_info(heights=(720, 480)))
    assert panel.selected_quality() == "720"
    settings.preferred_resolution = "best"
    panel.set_default_quality()
    assert panel.selected_quality() == "best"


def test_non_short_hides_badge_and_live_blocks_download(panel):
    open_url(panel, info=make_info(short=False))
    assert panel.info_badge.isHidden()
    panel.validate_now_force()
    panel._meta_job.finished.emit(make_info(live=True))
    assert not panel.download_button.isEnabled()
    assert "Live" in panel.url_error.text()


# ============================================================ LeftPanel: download
def test_download_progress_and_success(panel, backend, settings):
    info = make_info()
    open_url(panel, info=info)
    finished = []
    panel.downloadFinished.connect(lambda p, i: finished.append((p, i)))
    assert not panel.is_downloading()
    panel.start_download()
    job = backend.download.instances[0]
    assert job.started and job.quality == "1080" and job.dest_dir == str(settings.download_path())
    assert panel.is_downloading() and panel.state() == "downloading"
    assert not panel.url_edit.isEnabled()
    assert not panel.progress_box.isHidden() and panel.quality_row.isHidden()
    job.progress.emit(0.42, "3.2 MB/s", "0:14")
    job.stage.emit("Downloading video\u2026")
    assert panel.progress_bar.value() == 420
    assert panel.progress_label.text().replace("\u00b7", "|").split() == \
        ["42%", "|", "3.2", "MB/s", "|", "0:14", "left"]
    assert panel.stage_label.text() == "Downloading video\u2026"
    job.progress.emit(-1.0, "", "")
    assert panel.progress_bar.maximum() == 0       # busy indicator when unknown
    job.finished.emit("C:/x/video.mp4", info)
    assert finished == [("C:/x/video.mp4", info)]
    assert not panel.is_downloading()
    assert panel.state() == "idle"                  # reset
    assert panel.url_edit.text() == "" and panel.url_edit.isEnabled()
    assert panel.info_card.isHidden()


def test_download_failure_retry_and_dismiss(panel, backend):
    info = make_info()
    open_url(panel, info=info)
    panel.start_download()
    backend.download.instances[0].failed.emit("Network error")
    assert panel.state() == "message" and not panel.is_downloading()
    assert panel.message_label.text() == "Network error"
    assert not panel.retry_button.isHidden() and not panel.dismiss_button.isHidden()
    panel.retry_button.click()
    assert len(backend.download.instances) == 2
    again = backend.download.instances[1]
    assert again.started and again.info is info and again.quality == "1080"
    assert panel.is_downloading()
    again.failed.emit("Network error")
    panel.dismiss_button.click()
    assert panel.state() == "ready" and panel.download_button.isEnabled()


def test_download_cancel_shows_retry(panel, backend):
    open_url(panel, info=make_info())
    panel.start_download()
    job = backend.download.instances[0]
    panel.cancel_button.click()
    assert job.was_cancelled
    assert not panel.cancel_button.isEnabled()      # can't double-cancel
    assert panel.is_downloading()                    # until the job confirms
    job.cancelled.emit()
    assert not panel.is_downloading()
    assert panel.state() == "message" and "cancelled" in panel.message_label.text().lower()
    assert not panel.retry_button.isHidden()
    panel.retry_button.click()
    assert len(backend.download.instances) == 2 and panel.is_downloading()


def test_shutdown_cancels_running_download(panel, backend):
    open_url(panel, info=make_info())
    panel.start_download()
    panel.shutdown()
    assert backend.download.instances[0].was_cancelled


def test_quality_choice_is_passed_to_job(panel, backend):
    open_url(panel, info=make_info(heights=(2160, 1080, 720)))
    panel.quality_combo.setCurrentIndex(panel.quality_combo.findData("720"))
    panel.start_download()
    assert backend.download.instances[0].quality == "720"


# ================================================================ LeftPanel: media
def test_media_empty_state_and_import(panel):
    assert not panel.empty_card.isHidden() and panel.media_list.isHidden()
    got = []
    panel.importRequested.connect(lambda: got.append(1))
    panel.import_button.click()
    panel.import_tool.click()
    assert got == [1, 1]
    panel.set_busy_importing(True)
    assert not panel.import_button.isEnabled() and not panel.importing_label.isHidden()
    panel.set_busy_importing(False)
    assert panel.import_button.isEnabled() and panel.importing_label.isHidden()


def test_media_list_rows_and_offline(panel, doc, tmp_path):
    doc.add_source(make_source(tmp_path, "a", "a.mp4"), to_timeline=False)
    doc.add_source(make_source(tmp_path, "b", "b.mp4", w=1080, h=1920, exists=False),
                   to_timeline=False)
    assert panel.empty_card.isHidden() and not panel.media_list.isHidden()
    assert panel.media_list.count() == 2
    assert panel.media_count.text() == "2"
    first = panel.media_list.item(0)
    assert first.data(left_panel.TITLE_ROLE) == "Title a"
    assert first.data(left_panel.META_ROLE) == "1920\u00d71080  \u00b7  30 fps  \u00b7  0:42"
    assert not panel.is_offline("a") and panel.is_offline("b")
    assert panel.media_list.item(1).data(left_panel.OFFLINE_ROLE) is True
    # the file appears later: periodic refresh clears the flag
    (tmp_path / "b.mp4").write_bytes(b"x")
    panel.refresh_offline()
    assert not panel.is_offline("b")


def test_media_add_to_timeline_and_offline_refused(panel, doc, tmp_path):
    doc.add_source(make_source(tmp_path, "a", "a.mp4"), to_timeline=False)
    doc.add_source(make_source(tmp_path, "b", "b.mp4", exists=False), to_timeline=False)
    msgs = []
    panel.statusMessage.connect(msgs.append)
    panel.media_list.itemDoubleClicked.emit(panel.media_list.item(0))
    assert len(doc.timeline.segments) == 1 and doc.timeline.segments[0].source_id == "a"
    panel.media_list.itemDoubleClicked.emit(panel.media_list.item(1))
    assert len(doc.timeline.segments) == 1
    assert any("offline" in m.lower() for m in msgs)


def test_media_remove_and_used_source(panel, doc, tmp_path):
    doc.add_source(make_source(tmp_path, "a", "a.mp4"), to_timeline=True)
    doc.add_source(make_source(tmp_path, "b", "b.mp4"), to_timeline=False)
    panel.remove_source("a")                       # still used on the timeline: refused
    assert "a" in doc.sources
    panel.remove_source("b")
    assert "b" not in doc.sources and panel.media_list.count() == 1
    doc.delete_selected()
    panel.remove_source("a")
    assert panel.media_list.count() == 0 and not panel.empty_card.isHidden()


def test_media_context_menu_actions(panel, doc, tmp_path, monkeypatch):
    doc.add_source(make_source(tmp_path, "a", "a.mp4"), to_timeline=False)
    relinks, shown = [], []
    panel.relinkRequested.connect(relinks.append)
    monkeypatch.setattr(left_panel, "reveal_in_explorer", shown.append)
    row = panel.media_list.visualItemRect(panel.media_list.item(0)).center()

    def choose(label):
        monkeypatch.setattr(panel, "_run_menu", lambda menu, pos: next(
            x for x in menu.actions() if x.text().startswith(label)))
        panel._media_menu(row)

    choose("Relink")
    assert relinks == ["a"]
    choose("Show in folder")
    assert shown == [str(tmp_path / "a.mp4")]
    choose("Add to timeline")
    assert len(doc.timeline.segments) == 1
    doc.delete_selected()
    choose("Remove from project")
    assert "a" not in doc.sources


def test_posters_requested_and_arrive_async(panel, doc, tmp_path):
    thumbs = panel.thumbnails
    doc.add_source(make_source(tmp_path, "a", "a.mp4"), to_timeline=False)
    assert "a" in thumbs.requests and "a" not in panel._posters
    img = QImage(32, 18, QImage.Format.Format_RGB32)
    img.fill(QColor("#aa0000"))
    thumbs.ready["a"] = img
    thumbs.thumbnailReady.emit("a", 0.0)
    assert "a" in panel._posters


def test_project_reset_rebuilds_list(panel, doc, tmp_path):
    doc.add_source(make_source(tmp_path, "a", "a.mp4"), to_timeline=False)
    assert panel.media_list.count() == 1
    doc.new_project()
    assert panel.media_list.count() == 0 and not panel.empty_card.isHidden()


# ================================================================ LeftPanel: details
def test_details_for_selected_media(panel, doc, tmp_path):
    src = make_source(tmp_path, "a", "a.mp4", source_url=URL)
    doc.add_source(src, to_timeline=False)
    assert not panel.details_hint.isHidden() and panel.details == {}
    panel.select_media("a")
    d = panel.details
    assert d["File"] == "a.mp4" and d["Folder"] == str(tmp_path)
    assert d["Resolution"] == "1920\u00d71080  \u00b7  Landscape"
    assert d["Frame rate"] == "30 fps" and d["Duration"] == "0:42"
    assert d["Video"] == "H264  \u00b7  yuv420p"
    assert d["Audio"] == "AAC  \u00b7  48 kHz  \u00b7  Stereo"
    assert d["Size"] == "2.0 KB" and d["Source"] == URL
    assert panel.details_hint.isHidden()
    assert panel.details_grid.count() == 2 * len(d)


def test_details_vertical_no_audio_offline(panel, doc, tmp_path):
    src = make_source(tmp_path, "v", "v.mp4", w=1080, h=1920, has_audio=False, exists=False)
    doc.add_source(src, to_timeline=False)
    panel.select_media("v")
    d = panel.details
    assert d["Resolution"].endswith("Portrait") and d["Audio"] == "No audio"
    assert "Source" not in d and d["Status"].startswith("Offline")


def test_details_follow_selected_clip(panel, doc, tmp_path):
    doc.add_source(make_source(tmp_path, "a", "a.mp4"), to_timeline=True)   # selects the clip
    assert panel.details["File"] == "a.mp4"
    panel.select_media("a")
    doc.add_source(make_source(tmp_path, "b", "b.mp4"), to_timeline=True)
    assert panel.selected_media_id() is None        # clip selection wins over the list
    assert panel.details["File"] == "b.mp4"
    doc.clear_selection()
    assert panel.details == {}


def test_long_texts_never_widen_the_panel(app, backend, doc, settings, tmp_path):
    """Titles, paths and messages are elided/wrapped: the panel's minimum width does not
    depend on them (so it works anywhere between the splitter's min and max width)."""
    def min_width(title: str, url_title: str) -> int:
        d = Document()
        d.add_source(make_source(tmp_path, "a", "a.mp4", title=title, source_url="https://x/" + title),
                     to_timeline=False)
        p = LeftPanel(d, settings, FakeThumbs())
        open_url(p, info=make_info(title=url_title))
        p.select_media("a")
        p.show()
        app.processEvents()
        p._meta_job = None
        width = p.scroll.widget().minimumSizeHint().width()
        p.resize(300, 700)
        app.processEvents()
        assert not p.scroll.horizontalScrollBar().isVisible()
        p.shutdown()
        p.deleteLater()
        return width
    assert min_width("x" * 300, "y " * 150) == min_width("t", "u")


# ============================================================== ExportDialog
@pytest.fixture
def export_doc(doc, tmp_path):
    doc.add_source(make_source(tmp_path, "a", "a.mp4"), to_timeline=True)
    doc.add_source(make_source(tmp_path, "b", "b.mp4", w=1280, h=720), to_timeline=True)
    doc.clear_selection()
    return doc


@pytest.fixture
def dialog(app, backend, export_doc, settings):
    d = ExportDialog(export_doc, settings)
    settle(app)
    yield d
    d.deleteLater()


def test_export_defaults_from_settings(app, backend, export_doc, settings):
    settings.export_codec = "h265"
    settings.hw_accel = "off"
    d = ExportDialog(export_doc, settings)
    cs = d.collect_settings()
    assert cs.codec == "h265" and cs.hw_accel == "off"
    assert cs.folder == os.path.normpath(settings.export_dir)
    assert cs.container == "mp4" and cs.filename == "export"
    assert not d.format_combo.isEnabled()


def test_export_collect_settings_round_trip(app, backend, export_doc, settings, tmp_path):
    want = ExportSettings(filename="My Cut", folder=str(tmp_path / "o"), codec="h265",
                          resolution="720", fps="25", quality="low", audio=False,
                          hw_accel="off", mode="accurate", scope="timeline")
    export_doc.set_export_settings(want)
    d = ExportDialog(export_doc, settings)
    assert d.collect_settings() == ExportSettings(**{**want.to_dict(), "folder": os.path.normpath(want.folder)})
    d.deleteLater()


def test_export_filename_sanitised(dialog):
    dialog.name_edit.setText('a<b>:c"/d\\e|f?g*h')
    assert dialog.name_edit.text() == "abcdefgh"
    dialog.name_edit.setText("  clip.mp4 ")
    assert dialog.collect_settings().filename == "clip"
    dialog.name_edit.setText("name...")
    assert dialog.collect_settings().filename == "name"


def test_export_resolutions_hide_above_source(dialog):
    # first clip is 1920x1080 -> short side 1080
    keys = [dialog.res_combo.itemData(i) for i in range(dialog.res_combo.count())]
    assert keys == ["source", "1080", "720", "480"]
    assert "1920\u00d71080" in dialog.res_combo.itemText(0)
    assert dialog.size_label.text().endswith("1920\u00d71080")
    dialog.res_combo.setCurrentIndex(dialog.res_combo.findData("720"))
    assert dialog.size_label.text().endswith("1280\u00d7720")


def test_export_scope_radio(app, backend, export_doc, settings):
    d = ExportDialog(export_doc, settings)
    assert not d.scope_selection.isEnabled() and d.scope_timeline.isChecked()
    assert len(d.segments()) == 2
    export_doc.select([export_doc.timeline.segments[1].id])
    d2 = ExportDialog(export_doc, settings)
    assert d2.scope_selection.isEnabled()
    d2.scope_selection.setChecked(True)
    assert d2.collect_settings().scope == "selection"
    assert [s.source_id for s in d2.segments()] == ["b"]
    # selection is 1280x720: 1080 option disappears
    keys = [d2.res_combo.itemData(i) for i in range(d2.res_combo.count())]
    assert keys == ["source", "720", "480"]


def test_export_hardware_status_and_mode_info(app, dialog, monkeypatch):
    assert "NVIDIA NVENC" in dialog.hw_status.text()
    dialog.hw_combo.setCurrentIndex(dialog.hw_combo.findData("off"))
    settle(app)
    assert "CPU" in dialog.hw_status.text() and "libx264" in dialog.hw_status.text()
    assert "fast export" in dialog.mode_info.text().lower()
    monkeypatch.setattr(ex, "can_fast_export",
                        lambda s, seg, es: (False, "The clips have different resolutions."))
    dialog.fps_combo.setCurrentIndex(dialog.fps_combo.findData("30"))
    dialog._check_fast()
    settle(app)
    assert "accurate" in dialog.mode_info.text().lower()
    assert "different resolutions" in dialog.mode_info.text()
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("fast"))
    assert "not possible" in dialog.mode_info.text().lower()
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("accurate"))
    assert "frame-exact" in dialog.mode_info.text()


def test_export_validation(dialog, export_doc, tmp_path):
    dialog.name_edit.setText("")
    assert dialog.validate() == "Enter a file name."
    assert dialog.name_edit.property("state") == "error"
    dialog.name_edit.setText("CON")
    assert "reserved" in dialog.validate()
    dialog.name_edit.setText("ok")
    dialog.folder_field.set_path("")
    assert dialog.validate()
    dialog.folder_field.set_path("relative/dir")
    assert dialog.validate() and dialog.folder_field.has_error()
    dialog.folder_field.set_path(str(tmp_path))
    dialog.name_edit.setText("a")                  # tmp_path/a.mp4 is a source file
    assert "overwrite one of your source files" in dialog.validate()
    dialog.name_edit.setText("fine")
    assert dialog.validate() is None
    (tmp_path / "b.mp4").unlink()
    assert "offline" in dialog.validate().lower()


def test_export_fast_mode_blocked_when_impossible(app, backend, export_doc, settings, monkeypatch):
    monkeypatch.setattr(ex, "can_fast_export", lambda s, seg, es: (False, "Cuts are not on keyframes."))
    d = ExportDialog(export_doc, settings)
    settle(app)
    d.mode_combo.setCurrentIndex(d.mode_combo.findData("fast"))
    settle(app)
    assert "keyframes" in d.validate()
    assert d.start_export() is False and d.page() == export_dialog.PAGE_FORM
    assert not d.error_label.isHidden()


def test_export_start_saves_settings_and_runs_job(app, dialog, export_doc, settings, backend):
    dialog.name_edit.setText("result")
    dialog.codec_combo.setCurrentIndex(dialog.codec_combo.findData("h265"))
    assert dialog.start_export() is True
    assert dialog.page() == export_dialog.PAGE_PROGRESS and dialog.is_exporting()
    job = backend.export.instances[0]
    assert job.started and job.temp_dir == str(settings.temp_path())
    assert job.settings.filename == "result" and job.settings.codec == "h265"
    assert len(job.segments) == 2 and set(job.sources) == {"a", "b"}
    assert export_doc.export_settings.filename == "result"      # saved into the project
    assert export_doc.dirty
    assert os.path.isdir(settings.export_dir)                    # created on demand
    job.progress.emit(0.5, "Encoding\u2026 50%")
    assert dialog.progress_bar.value() == 500 and dialog.progress_status.text() == "Encoding\u2026 50%"
    assert dialog.progress_target.text().endswith("result.mp4")


def test_export_success_page(app, dialog, tmp_path, backend):
    dialog.name_edit.setText("done")
    dialog.start_export()
    out = tmp_path / "out" / "done.mp4"
    out.write_bytes(b"x" * 3000)
    backend.export.instances[0].finished.emit(str(out))
    assert dialog.page() == export_dialog.PAGE_SUCCESS and not dialog.is_exporting()
    assert dialog.result_path.text() == str(out)
    assert "2.9 KB" in dialog.result_info.text()
    opened, shown = [], []
    export_dialog.open_path = opened.append
    export_dialog.reveal_in_explorer = shown.append
    dialog.open_button.click()
    dialog.show_button.click()
    assert opened == [str(out)] and shown == [str(out)]


def test_export_failure_then_back(app, dialog, backend):
    dialog.name_edit.setText("bad")
    dialog.start_export()
    backend.export.instances[0].failed.emit("Disk full")
    assert dialog.page() == export_dialog.PAGE_FAILED
    assert dialog.failure_label.text() == "Disk full"
    dialog.back_button.click()
    assert dialog.page() == export_dialog.PAGE_FORM and dialog.name_edit.text() == "bad"


def test_export_cancel_returns_to_form_with_note(app, dialog, backend):
    dialog.name_edit.setText("c")
    dialog.start_export()
    job = backend.export.instances[0]
    dialog.progress_cancel.click()
    assert job.was_cancelled and not dialog.progress_cancel.isEnabled()
    job.cancelled.emit()
    assert dialog.page() == export_dialog.PAGE_FORM and not dialog.is_exporting()
    assert "cancelled" in dialog.note_label.text().lower() and not dialog.note_label.isHidden()


def test_export_overwrite_confirmation(app, dialog, tmp_path, monkeypatch):
    (tmp_path / "out").mkdir(exist_ok=True)
    (tmp_path / "out" / "dup.mp4").write_bytes(b"old")
    dialog.name_edit.setText("dup")
    asked = []
    monkeypatch.setattr(dialog, "confirm_overwrite", lambda p: asked.append(p) or False)
    assert dialog.start_export() is False
    assert asked and dialog.page() == export_dialog.PAGE_FORM
    monkeypatch.setattr(dialog, "confirm_overwrite", lambda p: True)
    assert dialog.start_export() is True


def test_export_close_during_export_confirms_and_cancels(app, dialog, backend, monkeypatch):
    dialog.name_edit.setText("x")
    dialog.start_export()
    job = backend.export.instances[0]
    monkeypatch.setattr(dialog, "confirm_cancel_export", lambda: False)
    dialog.reject()                                  # user says "no": keeps exporting
    assert dialog.is_exporting() and not job.was_cancelled
    monkeypatch.setattr(dialog, "confirm_cancel_export", lambda: True)
    results = []
    dialog.finished.connect(results.append)
    dialog.reject()
    assert job.was_cancelled and results == []       # waits for the job to wind down
    job.cancelled.emit()
    assert results == [0] and not dialog.is_exporting()


def test_export_cancel_button_not_confirmed_when_idle(dialog):
    results = []
    dialog.finished.connect(results.append)
    dialog.cancel_button.click()
    assert results == [0]


# ============================================================== SettingsDialog
def test_settings_save_writes_and_persists(app, backend, settings, tmp_path):
    d = SettingsDialog(settings)
    settle(app)
    d.download_field.set_path(str(tmp_path / "newdl"))
    d.export_field.set_path(str(tmp_path / "newout"))
    d.resolution_combo.setCurrentIndex(d.resolution_combo.findData("720"))
    d.codec_combo.setCurrentIndex(d.codec_combo.findData("h265"))
    d.hw_combo.setCurrentIndex(d.hw_combo.findData("off"))
    d.theme_combo.setCurrentIndex(d.theme_combo.findData("light"))
    assert settings.theme == "dark"                  # nothing written before Save
    results = []
    d.finished.connect(results.append)
    d.save_button.click()
    assert results == [1]
    reloaded = Settings.load(tmp_path / "s.json")
    assert reloaded.download_dir == os.path.normpath(str(tmp_path / "newdl"))
    assert reloaded.export_dir == os.path.normpath(str(tmp_path / "newout"))
    assert (reloaded.preferred_resolution, reloaded.export_codec, reloaded.hw_accel,
            reloaded.theme) == ("720", "h265", "off", "light")
    assert settings.theme == "light" and os.path.isdir(tmp_path / "newdl")


def test_settings_cancel_discards(app, backend, settings):
    d = SettingsDialog(settings)
    d.theme_combo.setCurrentIndex(d.theme_combo.findData("midnight"))
    d.cancel_button.click()
    assert settings.theme == "dark"
    assert not (Path(settings.cache_dir).parent / "s.json").exists()


def test_settings_restore_defaults(app, backend, settings):
    settings.preferred_resolution, settings.export_codec = "480", "h265"
    settings.theme, settings.hw_accel = "midnight", "off"
    d = SettingsDialog(settings)
    assert d.resolution_combo.currentData() == "480"
    d.restore_defaults()
    defaults = Settings()
    assert d.resolution_combo.currentData() == defaults.preferred_resolution
    assert d.codec_combo.currentData() == "h264" and d.theme_combo.currentData() == "dark"
    assert d.hw_combo.currentData() == "auto"
    assert d.download_field.path() == os.path.normpath(defaults.download_dir)
    assert settings.theme == "midnight"              # only applied on Save


def test_settings_invalid_folder_blocks_save(app, backend, settings):
    d = SettingsDialog(settings)
    d.download_field.set_path("not\\absolute")
    results = []
    d.finished.connect(results.append)
    assert d.save() is False and results == []
    assert d.download_field.has_error() and not d.error_label.isHidden()
    assert settings.download_dir != "not\\absolute"


def test_settings_cache_size_and_clear_only_cache_subdirs(app, backend, settings, tmp_path, monkeypatch):
    cache = Path(settings.cache_dir)
    (cache / "thumbnails" / "sub").mkdir(parents=True)
    (cache / "waveforms").mkdir()
    (cache / "thumbnails" / "a.jpg").write_bytes(b"x" * 1024)
    (cache / "thumbnails" / "sub" / "b.jpg").write_bytes(b"x" * 1024)
    (cache / "waveforms" / "w.npy").write_bytes(b"x" * 2048)
    (cache / "keep.txt").write_bytes(b"keep")
    assert settings_dialog.cache_size(str(cache)) == 4096
    d = SettingsDialog(settings)
    settle(app)
    assert "4.0 KB" in d.cache_size_label.text()
    monkeypatch.setattr(d, "confirm_clear_cache", lambda: False)
    d.clear_cache()
    assert (cache / "thumbnails" / "a.jpg").exists()
    monkeypatch.setattr(d, "confirm_clear_cache", lambda: True)
    d.clear_cache()
    settle(app)
    assert not any((cache / "thumbnails").iterdir()) and not any((cache / "waveforms").iterdir())
    assert (cache / "thumbnails").is_dir() and (cache / "keep.txt").exists()
    assert "empty" in d.cache_size_label.text()
    assert d.clear_cache_button.isEnabled()


def test_settings_footer_versions(app, backend, settings):
    d = SettingsDialog(settings)
    settle(app)
    assert "1.0.0" in d.version_label.text()
    assert d.ffmpeg_label.text() == "FFmpeg 8.1.3"


def test_export_includes_audio_lane_and_uses_full_duration(app, backend, export_doc, settings):
    first = export_doc.timeline.segments[0]
    export_doc.select([first.id])
    assert export_doc.detach_audio()
    export_doc.clear_selection()
    d = ExportDialog(export_doc, settings)
    settle(app)
    segs = d.segments()
    assert len(segs) == len(export_doc.timeline.all_segments())
    assert any(s.is_audio for s in segs)
    assert d.size_label.text().endswith("1920×1080")      # size comes from picture clips
    assert format_duration(export_doc.duration) in d.summary_label.text()
    # selection scope passes the chosen clips from both lanes
    export_doc.select([first.id, export_doc.timeline.audio[0].id])
    d._refresh_scope()
    d.scope_selection.setChecked(True)
    assert {s.id for s in d.segments()} == {first.id, export_doc.timeline.audio[0].id}
    d.deleteLater()
