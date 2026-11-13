"""UI tests for multi-site downloads (chip, quality states, error hint) and the Downloader
section of the settings dialog (fake UpdateJob; no network, no real override folder)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, Signal  # noqa: E402

from clipforge.ui import settings_dialog  # noqa: E402
from clipforge.ui.left_panel import LeftPanel  # noqa: E402
from clipforge.ui.settings_dialog import SettingsDialog  # noqa: E402

from test_ui_panels import (FakeThumbs, app, backend, doc, make_info, open_url,  # noqa: E402,F401
                            settings)

HINT = "Something changed on the site. Update the downloader in Settings."


@pytest.fixture
def panel(app, backend, doc, settings):
    p = LeftPanel(doc, settings, FakeThumbs())
    p.resize(300, 800)
    p.show()
    yield p
    p.shutdown()
    p.deleteLater()


def site_info(site="reddit", only_best=True):
    info = make_info(heights=())
    info.site = site
    info.available_qualities = (lambda: ["best"]) if only_best else info.available_qualities
    return info


# ------------------------------------------------------------------ left panel
def test_placeholder_and_footnote(panel):
    assert panel.url_edit.placeholderText() == "Paste a video link"
    for site in ("YouTube", "Twitch", "Kick", "Reddit", "TikTok", "Instagram"):
        assert site in panel.url_edit.toolTip()
    assert "public posts" in panel.footnote.text()
    assert "rights to use" in panel.footnote.text()


def test_youtube_has_no_chip_and_full_quality_list(panel):
    open_url(panel, info=make_info())
    assert panel.info_site.isHidden()
    assert panel.quality_combo.isEnabled()
    assert panel.quality_combo.itemText(0) == "Best"


def test_non_youtube_chip_and_best_available(panel):
    open_url(panel, info=site_info("reddit"))
    assert panel.state() == "ready"
    assert not panel.info_site.isHidden() and panel.info_site.text() == "Reddit"
    assert panel.quality_combo.itemText(0) == "Best available"
    assert not panel.quality_combo.isEnabled()
    assert panel.selected_quality() == "best"
    assert panel.download_button.isEnabled()


@pytest.mark.parametrize("key,name", [("twitch", "Twitch"), ("kick", "Kick")])
def test_twitch_kick_chip(panel, key, name):
    open_url(panel, info=site_info(key))
    assert not panel.info_site.isHidden() and panel.info_site.text() == name


def test_quality_list_restored_after_non_youtube(panel):
    open_url(panel, info=site_info("tiktok"))
    assert panel.info_site.text() == "TikTok"
    panel.validate_now_force()
    panel._meta_job.finished.emit(make_info())
    assert panel.info_site.isHidden()
    assert panel.quality_combo.isEnabled()
    assert panel.quality_combo.itemText(0) == "Best"


def test_non_youtube_download_passes_best(panel, backend):
    open_url(panel, info=site_info("instagram"))
    panel.start_download()
    job = backend.download.instances[-1]
    assert job.quality == "best"


def test_error_hint_button_only_for_update_hint(panel, backend):
    open_url(panel)
    panel._meta_job.failed.emit("The video is private.")
    assert panel.state() == "message"
    assert panel.settings_button.isHidden()
    panel.dismiss_message()
    panel.validate_now_force()
    panel._meta_job.failed.emit(HINT)
    assert not panel.settings_button.isHidden()
    got = []
    panel.openSettingsRequested.connect(lambda: got.append(1))
    panel.settings_button.click()
    assert got == [1]
    panel.dismiss_message()
    assert panel.settings_button.isHidden()


def test_error_hint_on_download_failure(panel, backend):
    open_url(panel, info=site_info("reddit"))
    panel.start_download()
    backend.download.instances[-1].failed.emit(HINT)
    assert panel.state() == "message"
    assert not panel.settings_button.isHidden()
    assert not panel.retry_button.isHidden()


# ------------------------------------------------------------- settings dialog
class FakeUpdateJob(QObject):
    progress = Signal(float, str)
    finished = Signal(str, bool)
    failed = Signal(str)
    instances: list = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.started = False
        self.was_cancelled = False
        FakeUpdateJob.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.was_cancelled = True


@pytest.fixture
def updater(monkeypatch):
    FakeUpdateJob.instances = []
    state = SimpleNamespace(version="2025.1.1", cleared=0)

    def clear():
        state.cleared += 1

    monkeypatch.setattr(settings_dialog, "UpdateJob", FakeUpdateJob)
    monkeypatch.setattr(settings_dialog, "installed_version", lambda: state.version)
    monkeypatch.setattr(settings_dialog, "clear_override", clear)
    return state


@pytest.fixture
def dialog(app, backend, settings, updater):
    d = SettingsDialog(settings)
    yield d
    d.close()
    d.deleteLater()


def test_downloader_section_shows_version(dialog):
    assert dialog.ytdlp_version_label.text() == "2025.1.1"
    assert dialog.update_button.isEnabled()
    assert dialog.update_status.isHidden() and dialog.update_progress.isHidden()


def test_update_progress_then_changed(dialog, updater):
    dialog.update_button.click()
    job = FakeUpdateJob.instances[0]
    assert job.started and dialog.is_updating()
    assert not dialog.update_button.isEnabled()
    assert not dialog.reset_ytdlp_button.isEnabled()
    dialog.update_button.click()                       # no second job while running
    assert len(FakeUpdateJob.instances) == 1
    job.progress.emit(0.5, "Downloading…")
    assert dialog.update_progress.value() == 500
    assert dialog.update_status.text() == "Downloading…"
    updater.version = "2025.2.2"
    job.finished.emit("2025.2.2", True)
    assert not dialog.is_updating() and dialog.update_button.isEnabled()
    assert dialog.update_status.text() == "Updated to 2025.2.2. Restart ClipForge to use it."
    assert dialog.update_status.property("role") == "success"
    assert dialog.ytdlp_version_label.text() == "2025.2.2"


def test_update_already_current(dialog):
    dialog.update_button.click()
    FakeUpdateJob.instances[0].finished.emit("2025.1.1", False)
    assert "up to date" in dialog.update_status.text()
    assert "2025.1.1" in dialog.update_status.text()


def test_update_failure_uses_error_style(dialog):
    dialog.update_button.click()
    FakeUpdateJob.instances[0].failed.emit("No internet connection.")
    assert dialog.update_status.text() == "No internet connection."
    assert dialog.update_status.property("role") == "error"
    assert dialog.update_button.isEnabled() and not dialog.is_updating()


def test_close_during_update_cancels_and_ignores_late_signals(dialog):
    dialog.update_button.click()
    job = FakeUpdateJob.instances[0]
    dialog.reject()
    assert job.was_cancelled and not dialog.is_updating()
    job.finished.emit("9.9.9", True)                   # late result: nothing happens, no crash
    job.failed.emit("late")
    assert dialog.update_status.text() != "late"


def test_close_event_during_update_cancels(dialog):
    dialog.update_button.click()
    job = FakeUpdateJob.instances[0]
    dialog.close()
    assert job.was_cancelled


def test_reset_confirmed(dialog, updater, monkeypatch):
    monkeypatch.setattr(dialog, "confirm_reset_ytdlp", lambda: True)
    dialog.reset_ytdlp_button.click()
    assert updater.cleared == 1
    assert "Restart ClipForge" in dialog.update_status.text()


def test_reset_declined(dialog, updater, monkeypatch):
    monkeypatch.setattr(dialog, "confirm_reset_ytdlp", lambda: False)
    dialog.reset_ytdlp_button.click()
    assert updater.cleared == 0 and dialog.update_status.isHidden()


def test_reset_failure_is_reported(dialog, monkeypatch):
    def boom():
        raise OSError("locked")

    monkeypatch.setattr(settings_dialog, "clear_override", boom)
    monkeypatch.setattr(dialog, "confirm_reset_ytdlp", lambda: True)
    dialog.reset_ytdlp_button.click()
    assert "locked" in dialog.update_status.text()
    assert dialog.update_status.property("role") == "error"


def test_confirm_dialog_uses_message_box(dialog, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes)
    assert dialog.confirm_reset_ytdlp() is True
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.No)
    assert dialog.confirm_reset_ytdlp() is False
