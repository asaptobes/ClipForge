"""Application settings dialog."""

from __future__ import annotations

import logging
import os
import shutil
from typing import Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QMessageBox,
                               QProgressBar, QVBoxLayout, QWidget)

from .. import __app_name__, __version__, paths
from ..media import ffmpeg
from ..core.timecode import format_size
try:  # the downloader updater lives in the media package; the dialog works without it
    from ..media.ytdlp_update import UpdateJob, clear_override, installed_version
except ImportError:  # pragma: no cover - only while the module is missing
    UpdateJob = clear_override = installed_version = None
from ..settings import Settings
from .support_dialog import open_support_page
from .widgets import (DOT, PathField, folder_problem, heading, hseparator, make_label,
                      open_path, plain_button, primary_button, run_task, set_prop)

log = logging.getLogger(__name__)

CACHE_SUBDIRS = ("thumbnails", "waveforms")
RESOLUTION_ITEMS = [("best", "Best available"), ("2160", "2160p (4K)"), ("1440", "1440p"),
                    ("1080", "1080p (Full HD)"), ("720", "720p"), ("480", "480p")]
CODEC_ITEMS = [("h264", "H.264 (most compatible)"), ("h265", "H.265 (smaller files)")]
HW_ITEMS = [("auto", "Auto"), ("on", "On"), ("off", "Off")]
THEME_ITEMS = [("dark", "Dark"), ("midnight", "Midnight"), ("light", "Light")]


def cache_size_of_tree(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def cache_size(cache_dir: str) -> int:
    """Bytes used by the thumbnail and waveform caches under ``cache_dir``."""
    return sum(cache_size_of_tree(os.path.join(cache_dir, sub)) for sub in CACHE_SUBDIRS)


def clear_cache_dirs(cache_dir: str) -> int:
    """Delete everything inside ``<cache_dir>/thumbnails`` and ``/waveforms`` (the folders
    themselves stay) and nothing else. Returns the bytes freed."""
    freed = 0
    for sub in CACHE_SUBDIRS:
        folder = os.path.join(cache_dir, sub)
        if not os.path.isdir(folder) or os.path.islink(folder):
            continue
        for entry in os.scandir(folder):
            try:
                if entry.is_dir(follow_symlinks=False):
                    freed += cache_size_of_tree(entry.path)
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    freed += entry.stat(follow_symlinks=False).st_size
                    os.remove(entry.path)
            except OSError as exc:
                log.warning("Could not delete cache entry %s: %s", entry.path, exc)
    return freed


def _combo(items: list[tuple[str, str]]) -> QComboBox:
    box = QComboBox()
    for key, text in items:
        box.addItem(text, key)
    return box


def _select(box: QComboBox, key: str) -> None:
    i = box.findData(key)
    if i >= 0:
        box.setCurrentIndex(i)


class SettingsDialog(QDialog):
    """Edits a :class:`Settings` object; writes into it and saves only on Save."""

    def __init__(self, settings: Settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(580)
        self._cache_token = 0
        self._update_job = None
        self._update_running = False
        self._build()
        self.load_from(settings)
        self._refresh_cache_size()
        run_task(lambda cancel, progress: ffmpeg.version(), self._on_ffmpeg_version,
                 name="ffmpeg-version")

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)
        root.addWidget(make_label("Settings", "title"))

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        self._grid, self._row = grid, 0

        self._section("Folders")
        self.download_field = PathField(dialog_title="Download folder")
        self._add_row("Downloads", self.download_field)
        self.export_field = PathField(dialog_title="Export folder")
        self._add_row("Exports", self.export_field)
        self.cache_field = PathField(dialog_title="Cache folder")
        self._add_row("Cache", self.cache_field)
        cache_row = QHBoxLayout()
        self.cache_size_label = make_label("Calculating…", "faint")
        self.clear_cache_button = plain_button("Clear cache", "trash")
        cache_row.addWidget(self.cache_size_label, 1)
        cache_row.addWidget(self.clear_cache_button)
        self._add_row("", cache_row)
        self.temp_field = PathField(dialog_title="Temporary folder")
        self._add_row("Temporary files", self.temp_field)

        self._section("Defaults")
        self.resolution_combo = _combo(RESOLUTION_ITEMS)
        self._add_row("Download quality", self.resolution_combo)
        self.codec_combo = _combo(CODEC_ITEMS)
        self._add_row("Export codec", self.codec_combo)
        self.hw_combo = _combo(HW_ITEMS)
        self._add_row("Hardware acceleration", self.hw_combo)

        self._section("Appearance")
        self.theme_combo = _combo(THEME_ITEMS)
        self._add_row("Theme", self.theme_combo)
        self._add_row("", make_label("The theme is applied everywhere after ClipForge restarts.",
                                     "faint", wrap=True, shrinkable=False))
        self._section("Downloader")
        self.ytdlp_version_label = make_label("", "dim", selectable=True)
        self._add_row("yt-dlp version", self.ytdlp_version_label)
        update_row = QHBoxLayout()
        self.update_button = plain_button("Check for updates", "retry")
        self.reset_ytdlp_button = plain_button("Reset to bundled version", variant="ghost")
        self.reset_ytdlp_button.setCursor(Qt.CursorShape.PointingHandCursor)
        update_row.addWidget(self.update_button)
        update_row.addWidget(self.reset_ytdlp_button)
        update_row.addStretch(1)
        self._add_row("", update_row)
        status_col = QVBoxLayout()
        status_col.setSpacing(6)
        self.update_progress = QProgressBar()
        self.update_progress.setRange(0, 1000)
        self.update_progress.setTextVisible(False)
        self.update_progress.setVisible(False)
        self.update_status = make_label("", "dim", wrap=True, shrinkable=False)
        self.update_status.setVisible(False)
        status_col.addWidget(self.update_progress)
        status_col.addWidget(self.update_status)
        self._add_row("", status_col)
        self._add_row("", make_label("Sites change often; updating the downloader fixes most "
                                     "“can’t download” problems.",
                                     "faint", wrap=True, shrinkable=False))
        self._refresh_ytdlp_version()

        self._section("Support")
        self.support_check = QCheckBox("Show the thank-you message at startup")
        self._add_row("", self.support_check)
        self.support_button = plain_button("Support ClipForge ☕", variant="ghost")
        self.support_button.setCursor(Qt.CursorShape.PointingHandCursor)
        support_row = QHBoxLayout()
        support_row.addWidget(self.support_button)
        support_row.addStretch(1)
        self._add_row("", support_row)
        root.addLayout(grid)

        self.error_label = make_label("", "error", wrap=True, shrinkable=False)
        self.error_label.setVisible(False)
        root.addWidget(self.error_label)

        buttons = QHBoxLayout()
        self.restore_button = plain_button("Restore defaults")
        self.cancel_button = plain_button("Cancel")
        self.save_button = primary_button("Save")
        self.save_button.setDefault(True)
        buttons.addWidget(self.restore_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        root.addLayout(buttons)
        root.addWidget(hseparator())

        footer = QHBoxLayout()
        self.version_label = make_label(f"{__app_name__} {__version__}", "faint")
        self.ffmpeg_label = make_label("FFmpeg …", "faint")
        self.log_button = plain_button("Open log folder", "folder", variant="ghost")
        footer.addWidget(self.version_label)
        footer.addWidget(make_label(DOT.strip(), "faint"))
        footer.addWidget(self.ffmpeg_label)
        footer.addStretch(1)
        footer.addWidget(self.log_button)
        root.addLayout(footer)

        self.save_button.clicked.connect(self.save)
        self.cancel_button.clicked.connect(self.reject)
        self.restore_button.clicked.connect(self.restore_defaults)
        self.clear_cache_button.clicked.connect(self.clear_cache)
        self.log_button.clicked.connect(self._open_logs)
        self.update_button.clicked.connect(self.check_for_updates)
        self.reset_ytdlp_button.clicked.connect(self.reset_ytdlp)
        self.support_button.clicked.connect(open_support_page)
        self.cache_field.changed.connect(lambda _p: self._refresh_cache_size())
        for field in self.path_fields():
            field.changed.connect(lambda _p: self.error_label.setVisible(False))

    def _section(self, text: str) -> None:
        h = heading(text)
        h.setContentsMargins(0, 6 if self._row else 0, 0, 0)
        self._grid.addWidget(h, self._row, 0, 1, 2)
        self._row += 1

    def _add_row(self, label: str, content) -> None:
        lab = make_label(label, "dim")
        lab.setMinimumWidth(140)
        self._grid.addWidget(lab, self._row, 0)
        if isinstance(content, QWidget):
            self._grid.addWidget(content, self._row, 1)
        else:
            self._grid.addLayout(content, self._row, 1)
        self._row += 1

    def path_fields(self) -> list[PathField]:
        return [self.download_field, self.export_field, self.cache_field, self.temp_field]

    # ---------------------------------------------------------------- values
    def load_from(self, s: Settings) -> None:
        """Show the values of ``s`` in the form (does not touch ``self.settings``)."""
        self.download_field.set_path(s.download_dir)
        self.export_field.set_path(s.export_dir)
        self.cache_field.set_path(s.cache_dir)
        self.temp_field.set_path(s.temp_dir)
        _select(self.resolution_combo, s.preferred_resolution)
        _select(self.codec_combo, s.export_codec)
        _select(self.hw_combo, s.hw_accel)
        _select(self.theme_combo, s.theme)
        self.support_check.setChecked(s.show_support_prompt)

    def restore_defaults(self) -> None:
        """Reset the form to the factory defaults (applied only when saved)."""
        self.load_from(Settings())
        self.error_label.setVisible(False)

    def validate(self, create: bool = False) -> Optional[str]:
        names = ["Downloads", "Exports", "Cache", "Temporary files"]
        for name, field in zip(names, self.path_fields()):
            problem = folder_problem(field.path(), create=create)
            if problem:
                field.set_error(problem)
                return f"{name} folder: {problem}"
        return None

    def save(self) -> bool:
        """Validate, write into the settings object, ``settings.save()`` and close."""
        error = self.validate(create=True)
        if error:
            self.error_label.setText(error)
            self.error_label.setVisible(True)
            return False
        s = self.settings
        s.download_dir = self.download_field.path()
        s.export_dir = self.export_field.path()
        s.cache_dir = self.cache_field.path()
        s.temp_dir = self.temp_field.path()
        s.preferred_resolution = str(self.resolution_combo.currentData())
        s.export_codec = str(self.codec_combo.currentData())
        s.hw_accel = str(self.hw_combo.currentData())
        s.theme = str(self.theme_combo.currentData())
        s.show_support_prompt = self.support_check.isChecked()
        s.save()
        self.accept()
        return True

    # ----------------------------------------------------------------- cache
    def _refresh_cache_size(self) -> None:
        self._cache_token += 1
        token = self._cache_token
        folder = self.cache_field.path()
        self.cache_size_label.setText("Calculating…")
        if not folder:
            self.cache_size_label.setText("")
            return
        run_task(lambda cancel, progress: (token, cache_size(folder)), self._on_cache_size,
                 name="cache-size")

    @Slot(object)
    def _on_cache_size(self, result) -> None:
        token, size = result
        if token != self._cache_token:
            return
        self.cache_size_label.setText(
            f"Thumbnails and waveforms use {format_size(size)}." if size else "The cache is empty.")

    def confirm_clear_cache(self) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "Clear cache?",
                          "Delete cached thumbnails and waveforms? They are regenerated "
                          "automatically when needed. Your media and projects are not touched.",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def clear_cache(self) -> None:
        folder = self.cache_field.path()
        if not folder or not self.confirm_clear_cache():
            return
        self.clear_cache_button.setEnabled(False)
        self.cache_size_label.setText("Clearing…")
        run_task(lambda cancel, progress: clear_cache_dirs(folder), self._on_cache_cleared,
                 self._on_cache_clear_failed, name="clear-cache")

    @Slot(object)
    def _on_cache_cleared(self, freed) -> None:
        self.clear_cache_button.setEnabled(True)
        self._refresh_cache_size()

    @Slot(str)
    def _on_cache_clear_failed(self, message: str) -> None:
        self.clear_cache_button.setEnabled(True)
        self.cache_size_label.setText(f"Could not clear the cache: {message}")

    # ------------------------------------------------------------ downloader
    def focus_downloader(self) -> None:
        """Put the keyboard focus on the downloader update button."""
        self.update_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _refresh_ytdlp_version(self) -> None:
        version = ""
        if installed_version is not None:
            try:
                version = installed_version() or ""
            except Exception:  # noqa: BLE001 - cosmetic
                log.debug("installed_version failed", exc_info=True)
        self.ytdlp_version_label.setText(version or "unknown")
        self.update_button.setEnabled(UpdateJob is not None and not self._update_running)
        self.reset_ytdlp_button.setEnabled(clear_override is not None
                                           and not self._update_running)

    def _set_update_status(self, text: str, role: str = "dim") -> None:
        set_prop(self.update_status, "role", role)
        self.update_status.setText(text)
        self.update_status.setVisible(bool(text))

    def is_updating(self) -> bool:
        return self._update_running

    def check_for_updates(self) -> None:
        if self._update_running or UpdateJob is None:
            return
        try:
            job = UpdateJob(self)
            job.progress.connect(self._on_update_progress)
            job.finished.connect(self._on_update_finished)
            job.failed.connect(self._on_update_failed)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not start the downloader update")
            self._set_update_status(f"Could not check for updates: {exc}", "error")
            return
        self._update_job = job
        self._update_running = True
        self.update_button.setEnabled(False)
        self.reset_ytdlp_button.setEnabled(False)
        self.update_progress.setRange(0, 0)
        self.update_progress.setVisible(True)
        self._set_update_status("Checking for updates…")
        job.start()

    def _end_update(self) -> None:
        job, self._update_job = self._update_job, None
        self._update_running = False
        self.update_progress.setVisible(False)
        if job is not None:
            job.deleteLater()
        self._refresh_ytdlp_version()

    @Slot(float, str)
    def _on_update_progress(self, fraction: float, text: str) -> None:
        if self.sender() is not self._update_job:
            return
        if fraction is None or fraction < 0:
            self.update_progress.setRange(0, 0)
        else:
            self.update_progress.setRange(0, 1000)
            self.update_progress.setValue(int(min(1.0, fraction) * 1000))
        if text:
            self._set_update_status(text)

    @Slot(str, bool)
    def _on_update_finished(self, version: str, changed: bool) -> None:
        if self.sender() is not self._update_job:
            return
        self._end_update()
        if changed:
            self._set_update_status(f"Updated to {version}. Restart ClipForge to use it.",
                                    "success")
        else:
            self._set_update_status(f"You’re up to date (version {version}).", "success")

    @Slot(str)
    def _on_update_failed(self, message: str) -> None:
        if self.sender() is not self._update_job:
            return
        self._end_update()
        self._set_update_status(message or "The update failed.", "error")

    def _stop_update(self) -> None:
        """Cancel a running update and forget it (the dialog is closing)."""
        job, self._update_job = self._update_job, None
        self._update_running = False
        if job is None:
            return
        for sig in (job.progress, job.finished, job.failed):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        try:
            job.cancel()
        except Exception:  # noqa: BLE001
            log.debug("update cancel failed", exc_info=True)

    def done(self, result: int) -> None:  # noqa: D102 - QDialog override
        self._stop_update()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_update()
        super().closeEvent(event)

    def confirm_reset_ytdlp(self) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "Reset downloader?",
                          "Go back to the downloader version that came with ClipForge? "
                          "Downloaded updates are removed.",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def reset_ytdlp(self) -> None:
        if self._update_running or clear_override is None or not self.confirm_reset_ytdlp():
            return
        try:
            clear_override()
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not reset the downloader")
            self._set_update_status(f"Could not reset the downloader: {exc}", "error")
            return
        self._set_update_status("Reset to the bundled version. Restart ClipForge to use it.",
                                "success")

    # ---------------------------------------------------------------- footer
    @Slot(object)
    def _on_ffmpeg_version(self, version) -> None:
        self.ffmpeg_label.setText(f"FFmpeg {version}" if version else "FFmpeg not found")

    def _open_logs(self) -> None:
        open_path(str(paths.log_dir()))
