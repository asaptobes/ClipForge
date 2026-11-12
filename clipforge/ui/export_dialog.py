"""Export dialog: choose output options, run the export, show the result."""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from PySide6.QtCore import QTimer, Qt, Slot
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                               QRadioButton, QVBoxLayout, QWidget)

from ..core.model import ExportSettings
from ..core.timecode import format_duration, format_size
from ..media import export as ex
from ..media import ffmpeg
from ..settings import Settings
from . import icons, theme
from .widgets import (DOT, ElidedLabel, PathField, clean_filename, folder_problem, heading,
                      is_reserved_filename, make_label, open_path, plain_button, primary_button,
                      run_task, sanitize_filename, set_prop)
from .winutil import reveal_in_explorer

log = logging.getLogger(__name__)

PAGE_FORM, PAGE_PROGRESS, PAGE_SUCCESS, PAGE_FAILED = range(4)
RESOLUTIONS = ["2160", "1440", "1080", "720", "480"]
FPS_CHOICES = ["24", "25", "30", "50", "60"]
CODEC_ITEMS = [("h264", "H.264 · most compatible"), ("h265", "H.265 · smaller files")]
QUALITY_ITEMS = [("high", "High"), ("medium", "Medium"), ("low", "Low")]
HW_ITEMS = [("auto", "Auto"), ("on", "On"), ("off", "Off")]
MODE_ITEMS = [("auto", "Automatic"), ("fast", "Fast (no re-encoding)"),
              ("accurate", "Accurate (frame-exact)")]
ENCODER_NAMES = {"h264_nvenc": "NVIDIA NVENC", "hevc_nvenc": "NVIDIA NVENC",
                 "h264_qsv": "Intel Quick Sync", "hevc_qsv": "Intel Quick Sync",
                 "h264_amf": "AMD AMF", "hevc_amf": "AMD AMF"}


def _combo(items: list[tuple[str, str]]) -> QComboBox:
    box = QComboBox()
    for key, text in items:
        box.addItem(text, key)
    return box


def _select(box: QComboBox, key: str) -> None:
    i = box.findData(key)
    if i >= 0:
        box.setCurrentIndex(i)


def _fallback_size(width: int, height: int, resolution: str) -> tuple[int, int]:
    """Same rule as ``export.resolve_output_size`` (used only if that is unavailable)."""
    if resolution == "source" or not resolution.isdigit() or not width or not height:
        return width - width % 2, height - height % 2
    short = min(int(resolution), min(width, height))
    scale = short / min(width, height)
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


class ExportDialog(QDialog):
    """Modal export dialog. ``exec()`` returns when the user closes it."""

    def __init__(self, document, settings: Settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.document = document
        self.settings = settings
        self.setWindowTitle("Export")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._job = None
        self._exporting = False
        self._cancelling = False
        self._close_after_cancel = False
        self._started = 0.0
        self._output_path = ""
        self._encoder_token = 0
        self._fast_token = 0
        self._fast_result: Optional[tuple[bool, str]] = None
        self._encoder: Optional[str] = None

        self._loading = True
        self._fast_timer = QTimer(self)
        self._fast_timer.setSingleShot(True)
        self._fast_timer.setInterval(250)
        self._fast_timer.timeout.connect(self._check_fast)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(500)
        self._elapsed_timer.timeout.connect(self._tick)

        self._build()
        self._load(document.export_settings)
        self._refresh_scope()
        self._loading = False
        self._refresh_all()

        self._check_encoder()
        self._check_fast()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(0)
        # Pages are shown/hidden instead of using a QStackedWidget, whose size hint is
        # that of its largest page: the dialog then fits each page snugly.
        self._pages = [self._build_form(), self._build_progress(), self._build_success(),
                       self._build_failed()]
        for page in self._pages:
            root.addWidget(page)
        self._page = -1
        self._show_page(PAGE_FORM)

    def _build_form(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        title = make_label("Export video", "title")
        lay.addWidget(title)

        self.note_label = make_label("", "warning", wrap=True, shrinkable=False)
        self.note_label.setVisible(False)
        lay.addWidget(self.note_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        self._grid = grid
        self._row = 0

        # file name + folder
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("File name")
        name_row.addWidget(self.name_edit, 1)
        name_row.addWidget(make_label(".mp4", "dim"))
        self._add_row("File name", name_row)
        self.folder_field = PathField("", dialog_title="Export folder")
        self._add_row("Folder", self.folder_field)

        self.format_combo = _combo([("mp4", "MP4")])
        self.format_combo.setEnabled(False)
        self._add_row("Format", self.format_combo)
        self.codec_combo = _combo(CODEC_ITEMS)
        self._add_row("Codec", self.codec_combo)
        res_row = QHBoxLayout()
        res_row.setSpacing(10)
        self.res_combo = _combo([("source", "Source")])
        res_row.addWidget(self.res_combo, 1)
        self.size_label = make_label("", "dim")
        res_row.addWidget(self.size_label)
        self._add_row("Resolution", res_row)
        self.fps_combo = _combo([("source", "Source")] + [(f, f"{f} fps") for f in FPS_CHOICES])
        self._add_row("Frame rate", self.fps_combo)
        self.quality_combo = _combo(QUALITY_ITEMS)
        self._add_row("Quality", self.quality_combo)
        self.audio_check = QCheckBox("Include audio")
        self.audio_check.setChecked(True)
        self._add_row("Audio", self.audio_check)

        hw_col = QVBoxLayout()
        hw_col.setSpacing(4)
        self.hw_combo = _combo(HW_ITEMS)
        self.hw_status = make_label("Checking available encoders…", "faint", wrap=True, shrinkable=False)
        hw_col.addWidget(self.hw_combo)
        hw_col.addWidget(self.hw_status)
        self._add_row("Hardware acceleration", hw_col, top=True)

        mode_col = QVBoxLayout()
        mode_col.setSpacing(4)
        self.mode_combo = _combo(MODE_ITEMS)
        self.mode_info = make_label("", "faint", wrap=True, shrinkable=False)
        mode_col.addWidget(self.mode_combo)
        mode_col.addWidget(self.mode_info)
        self._add_row("Export mode", mode_col, top=True)

        scope_col = QVBoxLayout()
        scope_col.setSpacing(6)
        self.scope_timeline = QRadioButton("Entire timeline")
        self.scope_selection = QRadioButton("Selected clips")
        self.scope_timeline.setChecked(True)
        scope_col.addWidget(self.scope_timeline)
        scope_col.addWidget(self.scope_selection)
        self._add_row("Range", scope_col, top=True)
        lay.addLayout(grid)

        self.summary_label = make_label("", "dim", wrap=True, shrinkable=False)
        lay.addWidget(self.summary_label)
        self.error_label = make_label("", "error", wrap=True, shrinkable=False)
        self.error_label.setVisible(False)
        lay.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = plain_button("Cancel")
        self.export_button = primary_button("Export", "export")
        self.export_button.setDefault(True)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.export_button)
        lay.addLayout(buttons)

        self.name_edit.textChanged.connect(self._on_name_edited)
        self.name_edit.textChanged.connect(lambda _t: self._clear_error())
        self.folder_field.changed.connect(lambda _p: self._clear_error())
        self.codec_combo.currentIndexChanged.connect(self._on_codec_or_hw)
        self.hw_combo.currentIndexChanged.connect(self._on_codec_or_hw)
        self.res_combo.currentIndexChanged.connect(self._on_output_changed)
        self.fps_combo.currentIndexChanged.connect(self._on_output_changed)
        self.audio_check.toggled.connect(self._on_output_changed)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.scope_timeline.toggled.connect(self._on_scope_changed)
        self.cancel_button.clicked.connect(self.reject)
        self.export_button.clicked.connect(self.start_export)
        return page

    def _add_row(self, label: str, content, top: bool = False) -> None:
        lab = make_label(label, "dim")
        lab.setMinimumWidth(130)
        lab.setAlignment(Qt.AlignmentFlag.AlignLeft |
                         (Qt.AlignmentFlag.AlignTop if top else Qt.AlignmentFlag.AlignVCenter))
        if top:
            lab.setContentsMargins(0, 6, 0, 0)
        self._grid.addWidget(lab, self._row, 0)
        if isinstance(content, QWidget):
            self._grid.addWidget(content, self._row, 1)
        else:
            self._grid.addLayout(content, self._row, 1)
        self._row += 1

    def _build_progress(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(make_label("Exporting…", "title"))
        self.progress_status = make_label("Preparing…", "dim", wrap=True, shrinkable=False)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_target = ElidedLabel("", role="faint", mode=Qt.TextElideMode.ElideMiddle)
        row = QHBoxLayout()
        self.elapsed_label = make_label("Elapsed 0:00", "faint")
        self.progress_cancel = plain_button("Cancel export", "x")
        row.addWidget(self.elapsed_label, 1)
        row.addWidget(self.progress_cancel)
        lay.addWidget(self.progress_status)
        lay.addWidget(self.progress_bar)
        lay.addWidget(self.progress_target)
        lay.addSpacing(8)
        lay.addLayout(row)
        self.progress_cancel.clicked.connect(self.cancel_export)
        return page

    def _result_header(self, icon_name: str, color: str, text: str) -> tuple[QHBoxLayout, QLabel]:
        row = QHBoxLayout()
        row.setSpacing(10)
        ic = QLabel()
        ic.setPixmap(icons.pixmap(icon_name, getattr(theme.current(), color), 26))
        ic.setFixedSize(26, 26)
        title = make_label(text, "title")
        row.addWidget(ic)
        row.addWidget(title, 1)
        return row, title

    def _build_success(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        head, _ = self._result_header("check", "success", "Export complete")
        lay.addLayout(head)
        self.result_path = ElidedLabel("", mode=Qt.TextElideMode.ElideMiddle)
        self.result_info = make_label("", "dim")
        lay.addWidget(self.result_path)
        lay.addWidget(self.result_info)
        lay.addSpacing(8)
        row = QHBoxLayout()
        self.open_button = primary_button("Open file", "play")
        self.show_button = plain_button("Show in folder", "folder")
        self.close_button = plain_button("Close")
        row.addWidget(self.open_button)
        row.addWidget(self.show_button)
        row.addStretch(1)
        row.addWidget(self.close_button)
        lay.addLayout(row)
        self.open_button.clicked.connect(lambda: open_path(self._output_path))
        self.show_button.clicked.connect(lambda: reveal_in_explorer(self._output_path))
        self.close_button.clicked.connect(self.accept)
        return page

    def _build_failed(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        head, _ = self._result_header("alert", "danger", "Export failed")
        lay.addLayout(head)
        self.failure_label = make_label("", None, wrap=True, shrinkable=False, selectable=True)
        lay.addWidget(self.failure_label)
        lay.addSpacing(8)
        row = QHBoxLayout()
        self.back_button = primary_button("Back")
        self.failed_close_button = plain_button("Close")
        row.addStretch(1)
        row.addWidget(self.failed_close_button)
        row.addWidget(self.back_button)
        lay.addLayout(row)
        self.back_button.clicked.connect(lambda: self._show_page(PAGE_FORM))
        self.failed_close_button.clicked.connect(self.accept)
        return page

    def _shrink(self) -> None:
        if self.isVisible():
            self.layout().activate()
            self.resize(self.width(), self.sizeHint().height())

    def _show_page(self, page: int) -> None:
        self._page = page
        for i, w in enumerate(self._pages):
            w.setVisible(i == page)
        self.layout().activate()
        QTimer.singleShot(0, self._shrink)

    def page(self) -> int:
        return self._page

    # ----------------------------------------------------------- load / collect
    def _load(self, es: ExportSettings) -> None:
        untouched = es == ExportSettings()
        name = es.filename
        if name in ("", "export") and self.document.path:
            name = self.document.display_name()
        self.name_edit.setText(clean_filename(name) or "export")
        self.folder_field.set_path(es.folder or self.settings.export_dir)
        _select(self.codec_combo, self.settings.export_codec if untouched else es.codec)
        _select(self.hw_combo, self.settings.hw_accel if untouched else es.hw_accel)
        self._fill_resolutions(es.resolution)
        _select(self.fps_combo, es.fps)
        _select(self.quality_combo, es.quality)
        self.audio_check.setChecked(es.audio)
        _select(self.mode_combo, es.mode)
        if es.scope == "selection" and self.document.selected_segments():
            self.scope_selection.setChecked(True)

    def collect_settings(self) -> ExportSettings:
        """Current dialog values as an :class:`ExportSettings`."""
        return ExportSettings(
            filename=clean_filename(self.name_edit.text()),
            folder=self.folder_field.path(),
            container="mp4",
            codec=str(self.codec_combo.currentData()),
            resolution=str(self.res_combo.currentData()),
            fps=str(self.fps_combo.currentData()),
            quality=str(self.quality_combo.currentData()),
            audio=self.audio_check.isChecked(),
            hw_accel=str(self.hw_combo.currentData()),
            mode=str(self.mode_combo.currentData()),
            scope="selection" if self.scope_selection.isChecked() else "timeline",
        )

    def segments(self) -> list:
        """Clips (video lane, then audio lane) the current scope exports."""
        if self.scope_selection.isChecked():
            return self.document.selected_segments()
        return list(self.document.timeline.all_segments())

    def _video_segments(self) -> list:
        """The picture clips of the current scope (these decide the output size)."""
        segs = self.segments()
        return [s for s in segs if not s.is_audio] or segs

    def output_path(self) -> str:
        es = self.collect_settings()
        return os.path.join(es.folder, f"{es.filename}.mp4")

    # ----------------------------------------------------------------- scope
    def _refresh_scope(self) -> None:
        n = len(self.document.selected_segments())
        self.scope_selection.setEnabled(n > 0)
        self.scope_selection.setText(f"Selected clips ({n})" if n else "Selected clips (none selected)")
        if n == 0 and self.scope_selection.isChecked():
            self.scope_timeline.setChecked(True)

    def _on_scope_changed(self, _checked: bool) -> None:
        if self._loading:
            return
        self._fill_resolutions(str(self.res_combo.currentData()))
        self._on_output_changed()

    # ------------------------------------------------------------ resolution
    def _first_source(self):
        segs = self._video_segments()
        return self.document.sources.get(segs[0].source_id) if segs else None

    def _fill_resolutions(self, wanted: str) -> None:
        src = self._first_source()
        short = min(src.width, src.height) if src and src.width and src.height else 0
        box = self.res_combo
        box.blockSignals(True)
        box.clear()
        label = "Source" + (f" ({src.width}×{src.height})" if short else "")
        box.addItem(label, "source")
        for r in RESOLUTIONS:
            if not short or int(r) <= short:
                box.addItem(f"{r}p", r)
        _select(box, wanted)
        box.blockSignals(False)

    def output_size(self) -> tuple[int, int]:
        """Resulting even ``(width, height)``; ``(0, 0)`` if unknown."""
        segs = self.segments()
        if not segs:
            return 0, 0
        es = self.collect_settings()
        sources = [self.document.sources[s.source_id] for s in self._video_segments()
                   if s.source_id in self.document.sources]
        try:
            return ex.resolve_output_size(sources, es)
        except Exception:  # noqa: BLE001 - back end not available: same rule locally
            src = sources[0] if sources else None
            return _fallback_size(src.width, src.height, es.resolution) if src else (0, 0)

    def _refresh_all(self) -> None:
        w, h = self.output_size()
        self.size_label.setText(f"→ {w}×{h}" if w and h else "")
        segs = self.segments()
        dur = 0.0
        if segs and not self.scope_selection.isChecked():
            dur = self.document.duration
        elif segs:
            dur = max(s.timeline_end for s in segs) - min(s.timeline_start for s in segs)
        parts = []
        if w and h:
            parts.append(f"{w}×{h}")
        if dur:
            parts.append(format_duration(dur))
        name = self.collect_settings().filename
        parts.append(f"{len(segs)} clip{'s' if len(segs) != 1 else ''}")
        self.summary_label.setText("Output: " + DOT.join(parts) +
                                   (f"\n{self.output_path()}" if name else ""))
        self._update_mode_info()

    # ------------------------------------------------------------- name field
    def _on_name_edited(self, text: str) -> None:
        if self._loading:
            return
        clean = sanitize_filename(text)
        if clean != text:
            pos = self.name_edit.cursorPosition() - (len(text) - len(clean))
            self.name_edit.setText(clean)
            self.name_edit.setCursorPosition(max(0, pos))
        self._refresh_all()

    def _clear_error(self) -> None:
        self.error_label.setVisible(False)
        set_prop(self.name_edit, "state", "")

    # ----------------------------------------------------------- async checks
    def _on_codec_or_hw(self) -> None:
        if self._loading:
            return
        self._check_encoder()
        self._on_output_changed()

    def _on_output_changed(self, *_args) -> None:
        if self._loading:
            return
        self._fast_result = None
        self._refresh_all()
        self._fast_timer.start()

    def _on_mode_changed(self, *_args) -> None:
        if not self._loading:
            self._update_mode_info()

    def _check_encoder(self) -> None:
        self._encoder_token += 1
        token = self._encoder_token
        codec, hw = str(self.codec_combo.currentData()), str(self.hw_combo.currentData())
        self.hw_status.setText("Checking available encoders…")
        set_prop(self.hw_status, "role", "faint")

        def work(cancel, progress):
            return token, ffmpeg.pick_video_encoder(codec, hw)
        run_task(work, self._on_encoder, self._on_encoder_failed, name="pick-encoder")

    @Slot(object)
    def _on_encoder(self, result) -> None:
        token, name = result
        if token != self._encoder_token:
            return
        self._encoder = name
        hw = str(self.hw_combo.currentData())
        if ffmpeg.is_hardware_encoder(name):
            text = f"Using {ENCODER_NAMES.get(name, 'hardware encoder')} ({name})."
            set_prop(self.hw_status, "role", "success")
        elif hw == "off":
            text = f"Hardware acceleration is off. Using the CPU encoder ({name})."
            set_prop(self.hw_status, "role", "faint")
        else:
            text = f"No hardware encoder available. Using the CPU encoder ({name})."
            set_prop(self.hw_status, "role", "faint")
        self.hw_status.setText(text)

    @Slot(str)
    def _on_encoder_failed(self, message: str) -> None:
        self._encoder = None
        self.hw_status.setText("Could not check encoders. The CPU encoder will be used.")

    def _check_fast(self) -> None:
        self._fast_token += 1
        token = self._fast_token
        segs = self.segments()
        if not segs:
            self._fast_result = (False, "There is nothing to export.")
            self._update_mode_info()
            return
        es = self.collect_settings()
        sources = dict(self.document.sources)
        self._update_mode_info()

        def work(cancel, progress):
            return token, tuple(ex.can_fast_export(sources, segs, es))
        run_task(work, self._on_fast, self._on_fast_failed, name="can-fast-export")

    @Slot(object)
    def _on_fast(self, result) -> None:
        token, value = result
        if token != self._fast_token:
            return
        self._fast_result = (bool(value[0]), str(value[1] or ""))
        self._update_mode_info()

    @Slot(str)
    def _on_fast_failed(self, message: str) -> None:
        self._fast_result = (False, "Fast export could not be checked for this timeline.")
        self._update_mode_info()

    def _update_mode_info(self) -> None:
        mode = str(self.mode_combo.currentData())
        res = self._fast_result
        role = "faint"
        if mode == "accurate":
            text = ("Re-encodes the video so every cut is frame-exact and any resolution or "
                    "frame rate works. Takes longer.")
        elif res is None:
            text = "Checking whether fast export is possible…"
        elif mode == "fast":
            if res[0]:
                text = "Copies the video without re-encoding: very fast and no quality loss."
            else:
                text = f"Fast export is not possible here. {res[1]}"
                role = "warning"
        else:
            if res[0]:
                text = ("Automatic will use fast export: the video is copied without "
                        "re-encoding, so it finishes in seconds with no quality loss.")
            else:
                reason = res[1].rstrip(".")
                text = f"Automatic will use accurate export (re-encode). {reason}." if reason \
                    else "Automatic will use accurate export (re-encode)."
        self.mode_info.setText(text)
        set_prop(self.mode_info, "role", role)

    # ------------------------------------------------------------ validation
    def validate(self, create_folder: bool = False) -> Optional[str]:
        """Why the export cannot start (``None`` when everything is fine). Marks the
        offending field."""
        es = self.collect_settings()
        if not es.filename:
            set_prop(self.name_edit, "state", "error")
            return "Enter a file name."
        if is_reserved_filename(es.filename):
            set_prop(self.name_edit, "state", "error")
            return f"“{es.filename}” is a reserved Windows name. Choose another file name."
        problem = folder_problem(es.folder, create=create_folder)
        if problem:
            self.folder_field.set_error(problem)
            return problem
        segs = self.segments()
        if not segs:
            return "There are no clips to export in the selected range."
        used = {os.path.normcase(os.path.abspath(self.document.sources[s.source_id].path))
                for s in segs if s.source_id in self.document.sources}
        out = os.path.normcase(os.path.abspath(self.output_path()))
        if out in used:
            set_prop(self.name_edit, "state", "error")
            return "That would overwrite one of your source files. Choose a different name or folder."
        missing = [self.document.sources[sid] for sid in
                   dict.fromkeys(s.source_id for s in segs if s.source_id in self.document.sources)
                   if not os.path.isfile(self.document.sources[sid].path)]
        if missing:
            return f"Media is offline: {os.path.basename(missing[0].path)}. Relink it first."
        if es.mode == "fast" and self._fast_result is not None and not self._fast_result[0]:
            return f"Fast export is not possible: {self._fast_result[1]} Choose Automatic or Accurate."
        return None

    def confirm_overwrite(self, path: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "Replace file?",
                          f"{os.path.basename(path)} already exists in this folder.\n\n"
                          "Do you want to replace it?",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def confirm_cancel_export(self) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "Cancel export?",
                          "An export is still running. Cancel it and close this window?",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    # ----------------------------------------------------------------- export
    def start_export(self) -> bool:
        """Validate, save the settings into the project and start the export job."""
        if self._exporting:
            return False
        self.note_label.setVisible(False)
        error = self.validate(create_folder=True)
        if error:
            self.error_label.setText(error)
            self.error_label.setVisible(True)
            return False
        self._clear_error()
        es = self.collect_settings()
        out = self.output_path()
        if os.path.exists(out) and not self.confirm_overwrite(out):
            return False
        self.document.set_export_settings(es)
        segs = self.segments()
        try:
            job = ex.ExportJob(dict(self.document.sources), segs, es,
                               temp_dir=str(self.settings.temp_path()), parent=self)
            job.progress.connect(self._on_progress)
            job.finished.connect(self._on_finished)
            job.failed.connect(self._on_failed)
            job.cancelled.connect(self._on_cancelled)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not create export job")
            self.error_label.setText(f"Could not start the export. {exc}")
            self.error_label.setVisible(True)
            return False
        self._job = job
        self._exporting = True
        self._cancelling = False
        self._output_path = out
        self._started = time.monotonic()
        self.progress_bar.setRange(0, 0)
        self.progress_status.setText("Preparing…")
        self.progress_target.setText(out)
        self.elapsed_label.setText("Elapsed 0:00")
        self.progress_cancel.setEnabled(True)
        self._show_page(PAGE_PROGRESS)
        self._elapsed_timer.start()
        job.start()
        return True

    def is_exporting(self) -> bool:
        return self._exporting

    def _tick(self) -> None:
        self.elapsed_label.setText(f"Elapsed {format_duration(time.monotonic() - self._started)}")

    def _end_job(self) -> None:
        self._exporting = False
        self._cancelling = False
        self._elapsed_timer.stop()
        job, self._job = self._job, None
        if job is not None:
            job.deleteLater()

    @Slot(float, str)
    def _on_progress(self, fraction: float, text: str) -> None:
        if self.sender() is not self._job:
            return
        if fraction is None or fraction < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(min(1.0, fraction) * 1000))
        if text and not self._cancelling:
            self.progress_status.setText(text)

    @Slot(str)
    def _on_finished(self, path: str) -> None:
        if self.sender() is not self._job:
            return
        self._end_job()
        if self._close_after_cancel:           # finished while we were closing: just close
            self._close_after_cancel = False
            super().accept()
            return
        self._output_path = path or self._output_path
        try:
            size = os.path.getsize(self._output_path)
        except OSError:
            size = 0
        self.result_path.setText(self._output_path)
        info = [format_size(size)] if size else []
        info.append(f"Finished in {format_duration(time.monotonic() - self._started)}")
        self.result_info.setText(DOT.join(info))
        self._show_page(PAGE_SUCCESS)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        if self.sender() is not self._job:
            return
        self._end_job()
        if self._close_after_cancel:
            self._close_after_cancel = False
            super().reject()
            return
        self.failure_label.setText(message or "The export failed for an unknown reason.")
        self._show_page(PAGE_FAILED)

    @Slot()
    def _on_cancelled(self) -> None:
        if self.sender() is not self._job:
            return
        self._end_job()
        if self._close_after_cancel:
            self._close_after_cancel = False
            super().reject()
            return
        self.note_label.setText("Export cancelled. Nothing was written.")
        self.note_label.setVisible(True)
        self._show_page(PAGE_FORM)

    def cancel_export(self) -> None:
        """Ask the running job to stop; the dialog reacts when it reports back."""
        if self._job is None or self._cancelling:
            return
        self._cancelling = True
        self.progress_cancel.setEnabled(False)
        self.progress_status.setText("Cancelling…")
        try:
            self._job.cancel()
        except Exception:  # noqa: BLE001
            log.exception("export cancel failed")

    def reject(self) -> None:  # Cancel button, Esc, window close
        if self._exporting:
            if self._cancelling:
                self._close_after_cancel = True
                return
            if not self.confirm_cancel_export():
                return
            self._close_after_cancel = True
            self.cancel_export()
            return
        super().reject()

    def done(self, result: int) -> None:
        self._fast_timer.stop()
        super().done(result)
