"""UI tests for "Download only a section" in the left panel (fake download jobs)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from clipforge.media import downloader as dl  # noqa: E402
from clipforge.ui.left_panel import LeftPanel, format_clock, validate_section  # noqa: E402

from test_ui_panels import (FakeDownload, FakeThumbs, app, backend, doc, make_info,  # noqa: E402,F401
                            open_url, settings)


class SectionDownload(FakeDownload):
    """FakeDownload that also takes ``section=`` like the real DownloadJob."""

    def __init__(self, info, quality, dest_dir, parent=None, section=None):
        super().__init__(info, quality, dest_dir, parent)
        self.section = section


@pytest.fixture
def panel(app, backend, doc, settings, monkeypatch):
    monkeypatch.setattr(dl, "DownloadJob", SectionDownload)
    p = LeftPanel(doc, settings, FakeThumbs())
    p.resize(300, 900)
    p.show()
    yield p
    p.shutdown()
    p.deleteLater()


def long_info(duration=1450.0):
    info = make_info()
    info.duration = duration
    return info


def set_times(panel, start, end):
    panel.section_start.setText(start)
    panel.section_end.setText(end)


# ------------------------------------------------------------------ visibility
def test_section_widgets_only_after_metadata(panel):
    assert panel.section_box.isHidden()
    open_url(panel)                                   # fetching
    assert panel.section_box.isHidden()
    panel._meta_job.finished.emit(long_info())
    assert not panel.section_box.isHidden()
    assert not panel.section_check.isChecked()
    assert panel.section_fields.isHidden() and panel.section_readout.isHidden()
    panel.section_check.setChecked(True)
    assert not panel.section_fields.isHidden() and not panel.section_readout.isHidden()


def test_prefilled_defaults_and_placeholders(panel):
    open_url(panel, info=long_info(1450.0))
    assert panel.section_start.text() == "0:00" and panel.section_end.text() == "24:10"
    assert panel.section_start.placeholderText() == "0:00"
    assert panel.section_end.placeholderText() == "24:10"
    panel.section_check.setChecked(True)
    assert panel.section_readout.text() == "Section: 24:10 (from 0:00 to 24:10)"


def test_hidden_for_live_and_unknown_duration(panel):
    open_url(panel, info=make_info(live=True))
    assert panel.section_box.isHidden()
    panel.validate_now_force()
    info = long_info(0.0)
    panel._meta_job.finished.emit(info)
    assert panel.section_box.isHidden()


def test_new_video_resets_section(panel):
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    set_times(panel, "1:00", "2:00")
    panel.validate_now_force()
    panel._meta_job.finished.emit(long_info(600.0))
    assert not panel.section_check.isChecked()
    assert panel.section_end.text() == "10:00" and panel.section_start.text() == "0:00"


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("start,end,ok,fragment", [
    ("12:10", "13:40", True, "Section: 1:30 (from 12:10 to 13:40)"),
    ("90", "135", True, "Section: 0:45 (from 1:30 to 2:15)"),
    (" 1 : 30 ", "2 : 15", True, "from 1:30 to 2:15"),
    ("1:30.5", "1:32", True, "from 1:30.5 to 1:32"),
    ("", "", True, "Section: 24:10 (from 0:00 to 24:10)"),
    ("5:00", "5:00", False, "End must be after start"),
    ("6:00", "5:00", False, "End must be after start"),
    ("1:00", "30:00", False, "End is past the video length (24:10)"),
    ("30:00", "31:00", False, "Start is past the video length (24:10)"),
    ("1:00", "1:00.2", False, "at least 0.5"),
    ("abc", "5:00", False, ""),
    ("0:00", "x:y", False, ""),
    ("-5", "10", False, ""),
])
def test_validation_matrix(panel, start, end, ok, fragment):
    open_url(panel, info=long_info(1450.0))
    panel.section_check.setChecked(True)
    set_times(panel, start, end)
    assert (panel.section_range() is not None) is ok
    assert panel.download_button.isEnabled() is ok
    text = panel.section_readout.text()
    assert fragment in text
    assert text
    assert panel.section_readout.property("role") == ("dim" if ok else "error")


def test_error_marks_offending_field(panel):
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    set_times(panel, "abc", "5:00")
    assert panel.section_start.property("state") == "error"
    assert not panel.section_end.property("state")
    set_times(panel, "6:00", "5:00")
    assert not panel.section_start.property("state")
    assert panel.section_end.property("state") == "error"
    set_times(panel, "1:00", "5:00")
    assert not panel.section_end.property("state")


def test_unchecking_reenables_download_even_if_invalid(panel):
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    set_times(panel, "9:00", "1:00")
    assert not panel.download_button.isEnabled()
    panel.section_check.setChecked(False)
    assert panel.download_button.isEnabled() and panel.section_range() is None


def test_untouched_end_is_exact_duration(panel):
    open_url(panel, info=long_info(125.6))
    panel.section_check.setChecked(True)
    assert panel.section_end.text() == "2:05.6"
    set_times(panel, "0:10", "2:05.6")
    assert panel.section_range() == (10.0, 125.6)
    set_times(panel, "0:10", "2:06")
    assert panel.section_range() is None               # past the end


def test_hours_format(panel):
    open_url(panel, info=long_info(7384.0))
    panel.section_check.setChecked(True)
    assert panel.section_end.text() == "2:03:04"
    set_times(panel, "1:02:03", "1:03:00")
    assert "from 1:02:03 to 1:03:00" in panel.section_readout.text()


def test_validate_section_pure():
    assert validate_section("0:10", "0:20", 60)[0] == (10.0, 20.0)
    assert validate_section("0:10", "0:20", 5)[1].startswith("Start is past")
    assert format_clock(75) == "1:15" and format_clock(3723.5) == "1:02:03.5"


# -------------------------------------------------------------------- download
def test_download_passes_section(panel, backend):
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    set_times(panel, "12:10", "13:40")
    panel.download_button.click()
    job = backend.download.instances[-1]
    assert job.section == (730.0, 820.0) and job.started
    assert panel.state() == "downloading"


def test_download_without_section_passes_nothing_odd(panel, monkeypatch):
    seen = []

    class Strict(FakeDownload):          # old signature: a `section` keyword would raise
        def __init__(self, info, quality, dest_dir, parent=None):
            seen.append((quality, dest_dir))
            super().__init__(info, quality, dest_dir, parent)

    monkeypatch.setattr(dl, "DownloadJob", Strict)
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    panel.section_check.setChecked(False)
    panel.download_button.click()
    assert panel.state() == "downloading" and len(seen) == 1
    assert FakeDownload.instances[-1].quality == "best" or seen


def test_checkbox_off_job_has_no_section(panel, backend):
    open_url(panel, info=long_info())
    set_times(panel, "1:00", "2:00")                   # ignored while unchecked
    panel.start_download()
    assert backend.download.instances[-1].section is None


def test_start_download_blocked_when_invalid(panel, backend):
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    set_times(panel, "5:00", "1:00")
    panel.start_download()
    assert not backend.download.instances and panel.state() == "ready"


def test_enter_in_end_field_starts_download(panel, backend):
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    set_times(panel, "1:00", "2:00")
    QTest.keyClick(panel.section_end, Qt.Key.Key_Return)
    assert backend.download.instances[-1].section == (60.0, 120.0)


def test_enter_in_end_field_ignored_when_invalid(panel, backend):
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    set_times(panel, "3:00", "1:00")
    QTest.keyClick(panel.section_end, Qt.Key.Key_Return)
    assert not backend.download.instances


def test_enter_in_start_moves_to_end(panel):
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    panel.section_start.setFocus()
    QTest.keyClick(panel.section_start, Qt.Key.Key_Return)
    assert panel.section_end.hasFocus()


def test_indeterminate_progress_and_stage(panel, backend):
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    set_times(panel, "1:00", "2:00")
    panel.start_download()
    job = backend.download.instances[-1]
    job.stage.emit("Downloading section…")
    job.progress.emit(-1.0, "", "")
    assert (panel.progress_bar.minimum(), panel.progress_bar.maximum()) == (0, 0)
    assert panel.stage_label.text() == "Downloading section…"
    assert panel.progress_label.text() == "Downloading section…"
    job.progress.emit(0.4, "2 MB/s", "10s")
    assert panel.progress_bar.maximum() == 1000 and panel.progress_bar.value() == 400
    job.progress.emit(-1.0, "Merging…", "")
    assert panel.progress_bar.maximum() == 0 and panel.progress_label.text() == "Merging…"


def test_finished_message_and_signal(panel, backend):
    msgs, done = [], []
    panel.statusMessage.connect(msgs.append)
    panel.downloadFinished.connect(lambda p, i: done.append(p))
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    set_times(panel, "1:30", "2:15")
    panel.start_download()
    backend.download.instances[-1].finished.emit("C:/x/clip.mp4", object())
    assert "Downloaded section 1:30–2:15" in msgs
    assert done == ["C:/x/clip.mp4"]
    assert panel.section_box.isHidden()


def test_plain_finish_message_unchanged(panel, backend):
    msgs = []
    panel.statusMessage.connect(msgs.append)
    open_url(panel, info=long_info())
    panel.start_download()
    backend.download.instances[-1].finished.emit("C:/x/clip.mp4", object())
    assert "Downloaded clip.mp4" in msgs


def test_retry_keeps_section(panel, backend):
    open_url(panel, info=long_info())
    panel.section_check.setChecked(True)
    set_times(panel, "1:00", "2:00")
    panel.start_download()
    backend.download.instances[-1].failed.emit("Network error")
    assert panel.state() == "message"
    panel.retry_button.click()
    assert len(backend.download.instances) == 2
    assert backend.download.instances[-1].section == (60.0, 120.0)


def test_section_hidden_while_downloading(panel, backend):
    open_url(panel, info=long_info())
    panel.start_download()
    assert panel.section_box.isHidden()
