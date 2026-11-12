"""Smaller helpers: timecode/size formatting, cancellation tokens, the Task wrapper, paths."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool

from clipforge import paths
from clipforge.core.timecode import format_duration, format_size, format_timecode
from clipforge.workers import Cancelled, CancelToken, Task


def to_ms(text: str) -> int:
    """Parse ``[HH:]MM:SS.mmm`` into milliseconds (for tolerance checks)."""
    parts = text.split(":")
    secs = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[-3]) if len(parts) == 3 else 0
    return round((hours * 3600 + minutes * 60 + secs) * 1000)


# ------------------------------------------------------------ format_timecode
@pytest.mark.parametrize("seconds, expected", [
    (0, "00:00.000"),
    (1.5, "00:01.500"),
    (61.25, "01:01.250"),
    (0.0004, "00:00.000"),
    (0.0006, "00:00.001"),
    (0.9994, "00:00.999"),
    (-3.0, "00:00.000"),                       # negative input is clamped to zero
])
def test_timecode_basic_values(seconds, expected):
    assert format_timecode(seconds) == expected


@pytest.mark.parametrize("seconds, expected", [
    (1.9996, "00:02.000"),                     # 999.6 ms rounds up and carries into seconds
    (59.9996, "01:00.000"),                    # carries into minutes
    (3599.9996, "01:00:00.000"),               # carries into hours
    (0.9996, "00:01.000"),
])
def test_timecode_rounding_carries_into_larger_units(seconds, expected):
    assert format_timecode(seconds) == expected


def test_timecode_near_nine_hundred_ninety_five_ms_is_within_one_ms():
    # 0.9995 sits exactly on a rounding boundary and its float representation decides
    # which side it lands on; both answers are acceptable, but the value must be 1 ms close.
    text = format_timecode(0.9995)
    assert text in ("00:00.999", "00:01.000")
    assert abs(to_ms(text) - 999.5) <= 1


@pytest.mark.parametrize("seconds, expected", [
    (3661.5, "01:01:01.500"),
    (36000.0, "10:00:00.000"),
    (86399.999, "23:59:59.999"),
])
def test_timecode_hours(seconds, expected):
    assert format_timecode(seconds) == expected


def test_show_hours_forces_hour_field():
    assert format_timecode(5.0, show_hours=True) == "00:00:05.000"
    assert format_timecode(65.0, show_hours=False) == "01:05.000"


def test_frame_mode_mm_ss_ff():
    assert format_timecode(1.0, fps=25) == "00:01:00"
    assert format_timecode(0.5, fps=24) == "00:00:12"
    assert format_timecode(0.04, fps=25) == "00:00:01"


def test_frame_mode_carries_frames_into_seconds_and_minutes():
    assert format_timecode(59.999, fps=30) == "01:00:00"
    assert format_timecode(29.999, fps=30) == "00:30:00"


def test_frame_mode_fractional_fps_uses_nominal_frame_count():
    # 29.97 fps: frames are counted against the nominal rate of 30
    assert format_timecode(1.0, fps=29.97) == "00:01:00"


def test_frame_mode_with_show_hours():
    assert format_timecode(1.0, fps=25, show_hours=True) == "00:00:01:00"
    assert format_timecode(3661.0, fps=25, show_hours=True) == "01:01:01:00"


def test_zero_or_negative_fps_falls_back_to_milliseconds():
    assert format_timecode(1.25, fps=0) == "00:01.250"
    assert format_timecode(1.25, fps=-30) == "00:01.250"


# ------------------------------------------------------------ format_duration
@pytest.mark.parametrize("seconds, expected", [
    (0, "0:00"),
    (0.4, "0:00"),
    (59.4, "0:59"),
    (59.5, "1:00"),                            # rounds to whole seconds first
    (61, "1:01"),
    (600, "10:00"),
    (3600, "1:00:00"),
    (3725.2, "1:02:05"),
    (-10, "0:00"),
])
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


# ---------------------------------------------------------------- format_size
@pytest.mark.parametrize("num, expected", [
    (0, "0 B"),
    (512, "512 B"),
    (1023, "1023 B"),
    (1024, "1.0 KB"),
    (1536, "1.5 KB"),
    (1024 ** 2, "1.0 MB"),
    (5 * 1024 ** 3, "5.0 GB"),
    (2 * 1024 ** 4, "2048.0 GB"),              # beyond GB the unit stays GB
])
def test_format_size(num, expected):
    assert format_size(num) == expected


# ------------------------------------------------------------- CancelToken
def test_cancel_token_states():
    tok = CancelToken()
    assert tok.cancelled is False
    tok.raise_if_cancelled()                   # no-op
    assert tok.wait(0.01) is False
    tok.cancel()
    assert tok.cancelled is True
    assert tok.wait(5.0) is True              # returns immediately once cancelled
    with pytest.raises(Cancelled):
        tok.raise_if_cancelled()


def test_cancelled_is_an_exception():
    assert issubclass(Cancelled, Exception)


def test_cancel_token_wakes_a_sleeping_thread():
    tok = CancelToken()
    seen = []
    t = threading.Thread(target=lambda: seen.append(tok.wait(10.0)))
    t.start()
    tok.cancel()
    t.join(5.0)
    assert seen == [True]


# -------------------------------------------------------------------- Task
def test_task_result_signal_carries_return_value(qtbot):
    task = Task(lambda cancel, progress: {"answer": 42}, name="ok")
    with qtbot.waitSignal(task.signals.result, timeout=5000) as blocker:
        task.start()
    assert blocker.args == [{"answer": 42}]


def test_task_finished_is_emitted_after_result(qtbot):
    order: list[str] = []
    task = Task(lambda cancel, progress: "done")
    task.signals.result.connect(lambda _v: order.append("result"))
    with qtbot.waitSignal(task.signals.finished, timeout=5000):
        task.start()
    assert order == ["result"]


def test_task_failure_emits_failed_with_message(qtbot):
    def boom(cancel, progress):
        raise ValueError("bad input file")

    task = Task(boom, name="fail")
    with qtbot.waitSignal(task.signals.failed, timeout=5000) as blocker:
        task.start()
    assert blocker.args == ["bad input file"]


def test_task_failure_without_message_uses_class_name(qtbot):
    def silent(cancel, progress):
        raise KeyError()

    task = Task(silent)
    with qtbot.waitSignal(task.signals.failed, timeout=5000) as blocker:
        task.start()
    assert blocker.args == ["KeyError"]


def test_task_runtime_error_about_deleted_object_is_swallowed(qtbot):
    failures: list[str] = []

    def gone(cancel, progress):
        raise RuntimeError("wrapped C++ object has been deleted")

    task = Task(gone)
    task.signals.failed.connect(failures.append)
    with qtbot.waitSignal(task.signals.finished, timeout=5000):
        task.start()
    assert failures == []


def test_task_other_runtime_error_is_reported(qtbot):
    def broken(cancel, progress):
        raise RuntimeError("ffmpeg exploded")

    task = Task(broken)
    with qtbot.waitSignal(task.signals.failed, timeout=5000) as blocker:
        task.start()
    assert blocker.args == ["ffmpeg exploded"]


def test_task_cancel_before_start_emits_cancelled_not_result(qtbot):
    ran = []

    def work(cancel, progress):
        cancel.raise_if_cancelled()
        ran.append(True)
        return "should not be seen"

    task = Task(work)
    results = []
    task.signals.result.connect(results.append)
    task.cancel()
    with qtbot.waitSignal(task.signals.cancelled, timeout=5000):
        task.start()
    assert results == []


def test_task_returning_after_cancel_still_reports_cancelled(qtbot):
    task = Task(lambda cancel, progress: (cancel.cancel(), "late")[1])
    results = []
    task.signals.result.connect(results.append)
    with qtbot.waitSignal(task.signals.cancelled, timeout=5000):
        task.start()
    assert results == []


def test_task_cancel_while_running_stops_worker(qtbot):
    started = threading.Event()

    def loop(cancel, progress):
        started.set()
        while not cancel.wait(0.01):
            pass
        cancel.raise_if_cancelled()

    task = Task(loop)
    with qtbot.waitSignal(task.signals.cancelled, timeout=5000):
        task.start()
        assert started.wait(5.0)
        task.cancel()


def test_task_progress_signal_reports_fraction_and_text(qtbot):
    updates: list[tuple[float, str]] = []

    def work(cancel, progress):
        progress(0.25, "quarter")
        progress(1.0, "done")
        return 7

    task = Task(work)
    task.signals.progress.connect(lambda f, t: updates.append((f, t)))
    with qtbot.waitSignal(task.signals.finished, timeout=5000):
        task.start()
    assert updates == [(0.25, "quarter"), (1.0, "done")]


def test_task_accepts_an_explicit_pool(qtbot):
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    task = Task(lambda cancel, progress: "pooled")
    with qtbot.waitSignal(task.signals.result, timeout=5000) as blocker:
        task.start(pool)
    assert blocker.args == ["pooled"]
    assert pool.waitForDone(5000)


def test_task_result_still_delivered_after_caller_drops_task(qtbot):
    import time

    task = Task(lambda cancel, progress: (time.sleep(0.05), "still delivered")[1])
    signals = task.signals                     # caller keeps only the signals object
    with qtbot.waitSignal(signals.result, timeout=5000) as blocker:
        task.start()
        del task
    assert blocker.args == ["still delivered"]


# ------------------------------------------------------------------- paths
@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def test_not_frozen_in_tests():
    assert paths.is_frozen() is False


def test_bundle_dir_is_the_project_root(clean_env):
    root = paths.bundle_dir()
    assert (root / "clipforge" / "__init__.py").is_file()
    assert (root / "run_clipforge.py").is_file()


def test_frozen_bundle_dir_uses_meipass(clean_env, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(clean_env / "bundle"), raising=False)
    assert paths.is_frozen() is True
    assert paths.bundle_dir() == clean_env / "bundle"
    assert paths.resource_path("icons", "app.ico") == \
        clean_env / "bundle" / "clipforge" / "resources" / "icons" / "app.ico"


def test_resource_path_points_into_package_resources(clean_env):
    p = paths.resource_path("icons", "x.png")
    assert p.parts[-3:] == ("resources", "icons", "x.png")
    assert p.parent.parent.name == "resources"


def test_app_data_dirs_are_created_under_env_locations(clean_env):
    roaming = paths.app_data_dir()
    local = paths.local_data_dir()
    assert roaming == clean_env / "roaming" / "ClipForge" and roaming.is_dir()
    assert local == clean_env / "local" / "ClipForge" and local.is_dir()
    assert paths.log_dir() == local / "logs" and paths.log_dir().is_dir()


def test_cache_and_temp_live_under_local_data(clean_env):
    assert paths.default_cache_dir() == clean_env / "local" / "ClipForge" / "cache"
    assert paths.default_temp_dir() == clean_env / "local" / "ClipForge" / "temp"


def test_media_defaults_are_under_videos(clean_env):
    assert paths.default_download_dir() == clean_env / "home" / "Videos" / "ClipForge" / "Downloads"
    assert paths.default_export_dir() == clean_env / "home" / "Videos" / "ClipForge" / "Exports"


def test_app_data_falls_back_to_home_without_env(clean_env, monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert paths.app_data_dir() == clean_env / "home" / "AppData" / "Roaming" / "ClipForge"
    assert paths.local_data_dir() == clean_env / "home" / "AppData" / "Local" / "ClipForge"


def test_ensure_dir_creates_nested_folders_idempotently(clean_env):
    target = clean_env / "a" / "b" / "c"
    assert paths.ensure_dir(str(target)) == target
    assert paths.ensure_dir(target) == target
    assert target.is_dir()


def test_ensure_dir_accepts_unicode_and_spaces(clean_env):
    target = clean_env / "Mi carpeta ñ 日本"
    assert paths.ensure_dir(target).is_dir()


def test_find_tool_missing_returns_none(clean_env, monkeypatch):
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    monkeypatch.setattr(paths, "bundle_dir", lambda: clean_env / "empty-bundle")
    assert paths.find_tool("ffmpeg-definitely-not-here") is None


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def test_find_tool_prefers_packaged_copy_over_vendor(clean_env, monkeypatch):
    monkeypatch.setattr(paths, "bundle_dir", lambda: clean_env / "bundle")
    packaged = clean_env / "bundle" / "ffmpeg" / _exe("ffmpeg")
    vendor = clean_env / "bundle" / "vendor" / "ffmpeg" / _exe("ffmpeg")
    vendor.parent.mkdir(parents=True)
    vendor.write_bytes(b"v")
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"p")
    assert paths.find_tool("ffmpeg") == str(packaged)


def test_find_tool_uses_vendor_copy_in_development(clean_env, monkeypatch):
    monkeypatch.setattr(paths, "bundle_dir", lambda: clean_env / "dev")
    vendor = clean_env / "dev" / "vendor" / "ffmpeg" / _exe("ffprobe")
    vendor.parent.mkdir(parents=True)
    vendor.write_bytes(b"v")
    assert paths.find_tool("ffprobe") == str(vendor)


def test_find_tool_falls_back_to_path(clean_env, monkeypatch):
    monkeypatch.setattr(paths, "bundle_dir", lambda: clean_env / "none")
    monkeypatch.setattr(paths.shutil, "which", lambda name: f"C:/on-path/{name}.exe")
    assert paths.find_tool("ffmpeg") == "C:/on-path/ffmpeg.exe"
