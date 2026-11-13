"""Captions dialog: generate captions from the timeline's speech, edit them, style them.

The dialog is non-modal and lives next to the main window. Everything it shows comes from
the :class:`~clipforge.document.Document` (``captions``, ``caption_style``) and every edit
goes back through the document, so the preview, the timeline and the export follow along.

Generating is a three-stage pipeline run by the media job objects (never on the GUI
thread): download the speech model once, mix the timeline audio to a temporary wav, then
transcribe it. Cancelling, closing the dialog or switching project retires the running job
(it is told to stop, its signals are cut, its temp file is deleted when it is done), so late
results can never reach a document they do not belong to.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import uuid
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QKeyEvent, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QColorDialog, QComboBox,
                               QDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QProgressBar, QPushButton, QSlider, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.captions import (CaptionLine, CaptionStyle, Word, chunk_words, style_for_preset)
from ..core.timecode import format_timecode, parse_timecode
from ..media import audio_extract as ae
from ..settings import Settings
from . import icons, theme
from .widgets import (hseparator, heading, make_label, plain_button, primary_button, run_task,
                      set_prop)

try:  # the speech back end is optional at import time; ``is_available`` decides at run time
    from ..media import transcribe as tr
except ImportError:  # pragma: no cover - only while the module is missing
    tr = None

log = logging.getLogger(__name__)

LANGUAGES = [(None, "Auto-detect"), ("en", "English"), ("es", "Spanish"), ("fr", "French"),
             ("de", "German"), ("pt", "Portuguese"), ("it", "Italian"), ("nl", "Dutch"),
             ("ja", "Japanese"), ("ko", "Korean"), ("zh", "Chinese"), ("hi", "Hindi"),
             ("ar", "Arabic"), ("ru", "Russian")]
PRESET_LABELS = [("pop", "Pop"), ("classic", "Classic"), ("boxed", "Boxed"), ("minimal", "Minimal")]
POSITION_LABELS = [("top", "Top"), ("middle", "Middle"), ("bottom", "Bottom")]
SIZE_MIN, SIZE_MAX = 2.0, 9.0
STAGE_NONE, STAGE_DOWNLOAD, STAGE_AUDIO, STAGE_TRANSCRIBE = "", "download", "audio", "transcribe"
STAGE_TEXT = {STAGE_DOWNLOAD: "Downloading model", STAGE_AUDIO: "Preparing audio",
              STAGE_TRANSCRIBE: "Listening"}
COL_START, COL_END, COL_TEXT = range(3)
NO_CLIPS_TEXT = "Add a clip to the timeline to generate captions."


# ------------------------------------------------------------------ job retirement
class _Reaper(QObject):
    """Keeps a retired job alive until it reports back, then deletes it and its temp file."""

    _live: set = set()

    def __init__(self, job: QObject, temp_path: Optional[str]) -> None:
        super().__init__()
        self.job, self.temp_path = job, temp_path
        _Reaper._live.add(self)
        for name in ("finished", "failed", "cancelled"):
            sig = getattr(job, name, None)
            if sig is not None:
                sig.connect(self._reap)
        running = False
        try:
            running = bool(job.is_running())
        except Exception:  # noqa: BLE001
            running = False
        if not running:
            self._reap()

    def _reap(self, *_args) -> None:
        if self not in _Reaper._live:
            return
        _Reaper._live.discard(self)
        _remove_file(self.temp_path)
        self.job.deleteLater()
        self.deleteLater()


def _remove_file(path: Optional[str]) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def retire_job(job: Optional[QObject], temp_path: Optional[str] = None) -> None:
    """Tell ``job`` to stop, cut its signals and clean up after it once it is done."""
    if job is None:
        return
    for name in ("progress", "finished", "failed", "cancelled"):
        try:
            getattr(job, name).disconnect()
        except (RuntimeError, TypeError, AttributeError):
            pass
    try:
        job.setParent(None)
    except Exception:  # noqa: BLE001
        pass
    try:
        job.cancel()
    except Exception:  # noqa: BLE001
        log.debug("captions job cancel failed", exc_info=True)
    _Reaper(job, temp_path)


# ------------------------------------------------------------------ small widgets
class CaptionTable(QTableWidget):
    """Caption list; Delete removes the selected rows."""

    deleteRequested = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and \
                self.state() != QAbstractItemView.State.EditingState:
            self.deleteRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


def swatch_icon(color: str) -> QIcon:
    """A small rounded colour chip with a 1 px theme border."""
    p = theme.current()
    dpr, size = 2, 16
    pm = QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(dpr)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(QColor(p.border_strong))
    painter.drawRoundedRect(0.5, 0.5, size - 1, size - 1, 4, 4)
    painter.end()
    return QIcon(pm)


def _model_size_text(mb: float) -> str:
    return f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{int(round(mb))} MB"


def _segment_row(items: list[tuple[str, str]], group: QButtonGroup) -> tuple[QHBoxLayout, dict]:
    row = QHBoxLayout()
    row.setSpacing(6)
    buttons: dict[str, QPushButton] = {}
    for key, text in items:
        b = plain_button(text, variant="segment")
        b.setCheckable(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        group.addButton(b)
        row.addWidget(b)
        buttons[key] = b
    row.addStretch(1)
    return row, buttons


# ---------------------------------------------------------------------- dialog
class CaptionsDialog(QDialog):
    """Non-modal captions editor. Construct once per use and ``show()`` it."""

    def __init__(self, document, settings: Settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.document = document
        self.settings = settings
        self.setWindowTitle("Captions")
        self.setModal(False)
        self.setMinimumSize(500, 780)
        self.resize(520, 820)

        self._stage = STAGE_NONE
        self._job: Optional[QObject] = None
        self._temp_wav: Optional[str] = None
        self._offset = 0.0
        self._model_key = ""
        self._lang: Optional[str] = None
        self._refreshing = False
        self._syncing_style = False
        self._keep_line: Optional[CaptionLine] = None
        self._gpu_token = 0

        self._table_timer = QTimer(self)
        self._table_timer.setSingleShot(True)
        self._table_timer.setInterval(0)
        self._table_timer.timeout.connect(self._refresh_table)
        self._rechunk_timer = QTimer(self)
        self._rechunk_timer.setSingleShot(True)
        self._rechunk_timer.setInterval(700)
        self._rechunk_timer.timeout.connect(self._offer_rechunk)

        self._build()
        d = document
        d.captionsChanged.connect(self._on_captions_changed)
        d.timelineChanged.connect(self._on_timeline_changed)
        d.sourcesChanged.connect(self._refresh_generate_state)
        d.projectReset.connect(self._on_project_reset)

        self._fill_models()
        self._refresh_all()
        self._check_gpu()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        root.addWidget(make_label("Captions", "title"))

        # ---- generate -----------------------------------------------------------
        root.addWidget(heading("Generate"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        self.model_combo = QComboBox()
        self.lang_combo = QComboBox()
        for code, text in LANGUAGES:
            self.lang_combo.addItem(text, code)
        grid.addWidget(make_label("Model", "dim"), 0, 0)
        grid.addWidget(self.model_combo, 0, 1)
        grid.addWidget(make_label("Language", "dim"), 1, 0)
        grid.addWidget(self.lang_combo, 1, 1)
        root.addLayout(grid)

        self.device_label = make_label("Checking your computer…", "faint", wrap=True, shrinkable=False)
        root.addWidget(self.device_label)
        self.unavailable_label = make_label("", "warning", wrap=True, shrinkable=False)
        self.unavailable_label.setVisible(False)
        root.addWidget(self.unavailable_label)
        self.message_label = make_label("", "dim", wrap=True, shrinkable=False)
        self.message_label.setVisible(False)
        root.addWidget(self.message_label)

        self.generate_button = primary_button("Generate captions", "captions")
        root.addWidget(self.generate_button)

        self.progress_box = QWidget()
        pl = QVBoxLayout(self.progress_box)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(8)
        self.stage_label = make_label("", "dim")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.stage_label, 1)
        self.cancel_button = plain_button("Cancel", "x")
        row.addWidget(self.cancel_button)
        pl.addLayout(row)
        pl.addWidget(self.progress_bar)
        self.progress_box.setVisible(False)
        root.addWidget(self.progress_box)

        root.addWidget(hseparator())

        # ---- captions list ------------------------------------------------------
        head = QHBoxLayout()
        head.addWidget(heading("Captions"))
        head.addStretch(1)
        self.count_label = make_label("", "faint")
        head.addWidget(self.count_label)
        root.addLayout(head)

        self.stale_banner = QFrame()
        self.stale_banner.setProperty("card", True)
        bl = QHBoxLayout(self.stale_banner)
        bl.setContentsMargins(12, 8, 8, 8)
        bl.setSpacing(10)
        self.stale_label = make_label("Captions are out of date: the timeline changed since they "
                                      "were made.", "warning", wrap=True, shrinkable=False)
        self.regenerate_button = plain_button("Regenerate", "retry")
        bl.addWidget(self.stale_label, 1)
        bl.addWidget(self.regenerate_button)
        self.stale_banner.setVisible(False)
        root.addWidget(self.stale_banner)

        self.table = CaptionTable(0, 3)
        self.table.setHorizontalHeaderLabels(["Start", "End", "Text"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                   QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setMinimumHeight(150)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_START, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_END, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_TEXT, QHeaderView.ResizeMode.Stretch)
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setSectionsClickable(False)
        self.table.setColumnWidth(COL_START, 104)
        self.table.setColumnWidth(COL_END, 104)
        root.addWidget(self.table, 1)

        self.empty_label = make_label("No captions yet. Generate them above, or after the "
                                      "timeline has clips.", "faint", wrap=True, shrinkable=False)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.empty_label)

        list_row = QHBoxLayout()
        list_row.setSpacing(8)
        self.delete_button = plain_button("Delete", "trash")
        self.clear_button = plain_button("Clear all", variant="danger")
        list_row.addWidget(self.delete_button)
        list_row.addWidget(self.clear_button)
        list_row.addStretch(1)
        self.hint_label = make_label("Double-click to edit", "faint")
        list_row.addWidget(self.hint_label)
        root.addLayout(list_row)

        root.addWidget(hseparator())

        # ---- style --------------------------------------------------------------
        root.addWidget(heading("Style"))
        sg = QGridLayout()
        sg.setHorizontalSpacing(14)
        sg.setVerticalSpacing(10)
        sg.setColumnStretch(1, 1)

        self.preset_group = QButtonGroup(self)
        self.preset_group.setExclusive(True)
        prow, self.preset_buttons = _segment_row(PRESET_LABELS, self.preset_group)
        sg.addWidget(make_label("Look", "dim"), 0, 0)
        sg.addLayout(prow, 0, 1)

        self.position_group = QButtonGroup(self)
        self.position_group.setExclusive(True)
        posrow, self.position_buttons = _segment_row(POSITION_LABELS, self.position_group)
        sg.addWidget(make_label("Position", "dim"), 1, 0)
        sg.addLayout(posrow, 1, 1)

        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(int(SIZE_MIN * 10), int(SIZE_MAX * 10))
        self.size_label = make_label("", "dim")
        self.size_label.setMinimumWidth(44)
        self.size_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        srow = QHBoxLayout()
        srow.setSpacing(10)
        srow.addWidget(self.size_slider, 1)
        srow.addWidget(self.size_label)
        sg.addWidget(make_label("Size", "dim"), 2, 0)
        sg.addLayout(srow, 2, 1)

        self.words_spin = QSpinBox()
        self.words_spin.setRange(1, 8)
        self.words_spin.setSuffix(" words")
        self.words_spin.setFixedWidth(110)
        wrow = QHBoxLayout()
        wrow.addWidget(self.words_spin)
        wrow.addStretch(1)
        sg.addWidget(make_label("Per line", "dim"), 3, 0)
        sg.addLayout(wrow, 3, 1)

        self.color_buttons: dict[str, QPushButton] = {}
        crow = QHBoxLayout()
        crow.setSpacing(8)
        for field, text in (("primary", "Text"), ("highlight", "Highlight"), ("outline", "Outline")):
            b = plain_button(text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(f"{text} colour")
            self.color_buttons[field] = b
            crow.addWidget(b)
        crow.addStretch(1)
        sg.addWidget(make_label("Colours", "dim"), 4, 0)
        sg.addLayout(crow, 4, 1)

        self.upper_check = QCheckBox("Uppercase")
        self.highlight_check = QCheckBox("Highlight word")
        self.box_check = QCheckBox("Box")
        trow = QHBoxLayout()
        trow.setSpacing(16)
        for c in (self.upper_check, self.highlight_check, self.box_check):
            trow.addWidget(c)
        trow.addStretch(1)
        sg.addWidget(make_label("Options", "dim"), 5, 0)
        sg.addLayout(trow, 5, 1)
        root.addLayout(sg)

        # ---- wiring -------------------------------------------------------------
        self.generate_button.clicked.connect(self.start_generate)
        self.regenerate_button.clicked.connect(self.start_generate)
        self.cancel_button.clicked.connect(self.cancel_generate)
        self.delete_button.clicked.connect(self.delete_selected)
        self.table.deleteRequested.connect(self.delete_selected)
        self.clear_button.clicked.connect(self.clear_all)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.currentCellChanged.connect(self._on_current_cell)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.itemSelectionChanged.connect(self._refresh_list_buttons)
        for key, b in self.preset_buttons.items():
            b.clicked.connect(lambda _c=False, k=key: self._apply_preset(k))
        for key, b in self.position_buttons.items():
            b.clicked.connect(lambda _c=False, k=key: self._edit_style(position=k))
        self.size_slider.valueChanged.connect(self._on_size)
        self.words_spin.valueChanged.connect(self._on_words)
        for field, b in self.color_buttons.items():
            b.clicked.connect(lambda _c=False, f=field: self._pick_color(f))
        self.upper_check.toggled.connect(lambda v: self._edit_style(uppercase=v))
        self.highlight_check.toggled.connect(lambda v: self._edit_style(highlight_words=v))
        self.box_check.toggled.connect(lambda v: self._edit_style(box=v))

    # ------------------------------------------------------------- availability
    def _available(self) -> bool:
        try:
            return bool(tr is not None and tr.is_available())
        except Exception:  # noqa: BLE001
            log.exception("transcribe.is_available failed")
            return False

    def _fill_models(self) -> None:
        self.model_combo.blockSignals(True)
        current = self.model_combo.currentData()
        self.model_combo.clear()
        models = dict(getattr(tr, "MODELS", {}) or {}) if tr is not None else {}
        for key, info in models.items():
            label = getattr(info, "label", str(key))
            mb = float(getattr(info, "approx_download_mb", 0) or 0)
            try:
                have = bool(tr.model_downloaded(key))
            except Exception:  # noqa: BLE001
                have = False
            suffix = "downloaded" if have else (_model_size_text(mb) if mb else "")
            self.model_combo.addItem(f"{label}  ·  {suffix}" if suffix else label, key)
        want = current
        if want is None and tr is not None:
            try:
                want = tr.recommended_model()
            except Exception:  # noqa: BLE001
                want = None
        i = self.model_combo.findData(want)
        if i >= 0:
            self.model_combo.setCurrentIndex(i)
        self.model_combo.blockSignals(False)

    def _check_gpu(self) -> None:
        if tr is None:
            self.device_label.setText("")
            return
        self._gpu_token += 1
        token = self._gpu_token

        def work(cancel, progress):
            return token, tr.gpu_status()
        run_task(work, self._on_gpu, self._on_gpu_failed, name="gpu-status")

    @Slot(object)
    def _on_gpu(self, result) -> None:
        token, status = result
        if token != self._gpu_token:
            return
        if getattr(status, "usable", False):
            name = getattr(status, "name", "") or "graphics card"
            self.device_label.setText(f"Using your NVIDIA GPU ({name})")
            set_prop(self.device_label, "role", "success")
        else:
            reason = getattr(status, "reason", "") or "No supported GPU found: captions run on the CPU."
            self.device_label.setText(reason)
            set_prop(self.device_label, "role", "faint")

    @Slot(str)
    def _on_gpu_failed(self, _message: str) -> None:
        self.device_label.setText("Captions will run on the CPU.")

    # ------------------------------------------------------------------ refresh
    def _refresh_all(self) -> None:
        available = self._available()
        self.unavailable_label.setVisible(not available)
        if not available:
            self.unavailable_label.setText(
                "Auto-captions are not available in this copy of ClipForge: the speech "
                "recognition component is missing.")
        self._refresh_generate_state()
        self._sync_style()
        self._refresh_table()
        self._refresh_stale()

    def running(self) -> bool:
        return self._stage != STAGE_NONE

    def _refresh_generate_state(self) -> None:
        has_clips = not self.document.timeline.is_empty()
        busy = self.running()
        self.generate_button.setVisible(not busy)
        self.progress_box.setVisible(busy)
        self.model_combo.setEnabled(not busy)
        self.lang_combo.setEnabled(not busy)
        self.generate_button.setEnabled(self._available() and has_clips)
        self.generate_button.setText("Regenerate captions" if self.document.captions
                                     else "Generate captions")
        if not has_clips and not busy and self._available() and not self.message_label.text():
            self._say(NO_CLIPS_TEXT, "faint")
        elif has_clips and self.message_label.text() == NO_CLIPS_TEXT:
            self._say("")
        self.regenerate_button.setEnabled(self._available() and has_clips and not busy)

    def _say(self, text: str, role: str = "dim") -> None:
        self.message_label.setText(text)
        self.message_label.setVisible(bool(text))
        set_prop(self.message_label, "role", role)

    def _refresh_stale(self) -> None:
        self.stale_banner.setVisible(bool(self.document.captions) and self.document.captions_stale())

    def _refresh_list_buttons(self) -> None:
        has = bool(self.document.captions)
        self.delete_button.setEnabled(bool(self._selected_rows()))
        self.clear_button.setEnabled(has)
        self.hint_label.setVisible(has)

    @Slot()
    def _on_captions_changed(self) -> None:
        self._refresh_stale()
        self._sync_style()
        self._refresh_generate_state()
        self._table_timer.start()

    @Slot()
    def _on_timeline_changed(self) -> None:
        self._refresh_stale()
        self._refresh_generate_state()
        if self._stage in (STAGE_AUDIO, STAGE_TRANSCRIBE):
            self._abort("Cancelled because the timeline changed. Generate again when you are ready.")

    @Slot()
    def _on_project_reset(self) -> None:
        if self.running():
            self._abort("")
        self._say("")
        self._fill_models()
        self._refresh_all()

    # -------------------------------------------------------------------- table
    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.table.selectedIndexes()})

    @Slot()
    def _refresh_table(self) -> None:
        lines = self.document.captions
        keep, self._keep_line = self._keep_line, None
        rows = self._selected_rows()
        selected = {id(lines[r]) for r in rows if r < len(lines)} if lines else set()
        if keep is not None:
            selected = {id(keep)}
        self._refreshing = True
        try:
            self.table.blockSignals(True)
            self.table.setRowCount(len(lines))
            sel_model = self.table.selectionModel()
            sel_model.clearSelection()
            flags_ro = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            for r, ln in enumerate(lines):
                for col, text in ((COL_START, format_timecode(ln.start)),
                                  (COL_END, format_timecode(ln.end)), (COL_TEXT, ln.text)):
                    item = self.table.item(r, col)
                    if item is None:
                        item = QTableWidgetItem()
                        self.table.setItem(r, col, item)
                    item.setText(text)
                    item.setFlags(flags_ro | Qt.ItemFlag.ItemIsEditable)
                    if col != COL_TEXT:
                        item.setForeground(QColor(theme.current().text_dim))
            self.table.blockSignals(False)
            for r, ln in enumerate(lines):
                if id(ln) in selected:
                    self.table.selectRow(r)
        finally:
            self.table.blockSignals(False)
            self._refreshing = False
        n = len(lines)
        self.count_label.setText(f"{n} caption{'s' if n != 1 else ''}" if n else "")
        self.table.setVisible(bool(n))
        self.empty_label.setVisible(not n)
        self._refresh_list_buttons()

    @Slot(QTableWidgetItem)
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._refreshing:
            return
        row, col = item.row(), item.column()
        lines = self.document.captions
        if not 0 <= row < len(lines):
            return
        line = lines[row]
        text = item.text()
        ok = True
        if col == COL_TEXT:
            text = " ".join(text.split())
            if not text:
                ok = False
                self._say("A caption can't be empty. Use Delete to remove it.", "warning")
            elif text != line.text:
                self._keep_line = line
                ok = self.document.edit_caption(row, text=text)
        else:
            try:
                t = parse_timecode(text)
            except ValueError as exc:
                ok = False
                self._say(str(exc), "warning")
            else:
                self._keep_line = line
                if col == COL_START:
                    ok = self.document.edit_caption(row, start=t)
                else:
                    if t <= line.start:
                        ok = False
                        self._say("The end must be after the start.", "warning")
                        self._keep_line = None
                    else:
                        ok = self.document.edit_caption(row, end=t)
        if ok:
            if self.message_label.property("role") == "warning":
                self._say("")
        self._table_timer.start()          # rewrite the cell with the normalised value

    @Slot(int, int, int, int)
    def _on_current_cell(self, row: int, _col: int, prev_row: int, _prev_col: int) -> None:
        if self._refreshing or row < 0 or row == prev_row:
            return
        lines = self.document.captions
        if row < len(lines):
            self.document.seek(lines[row].start)

    @Slot(int, int)
    def _on_cell_clicked(self, row: int, _col: int) -> None:
        lines = self.document.captions
        if 0 <= row < len(lines):
            self.document.seek(lines[row].start)

    def delete_selected(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        for r in reversed(rows):
            self.document.delete_caption(r)
        self._table_timer.start()

    def confirm_clear(self) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "Clear captions?",
                          "Remove all captions from this project?",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def clear_all(self) -> None:
        if self.document.captions and self.confirm_clear():
            self.document.clear_captions()

    # -------------------------------------------------------------------- style
    def _sync_style(self) -> None:
        st = self.document.caption_style
        self._syncing_style = True
        try:
            for key, b in self.preset_buttons.items():
                b.setChecked(key == st.preset)
            if st.preset not in self.preset_buttons:
                self.preset_group.setExclusive(False)
                for b in self.preset_buttons.values():
                    b.setChecked(False)
                self.preset_group.setExclusive(True)
            for key, b in self.position_buttons.items():
                b.setChecked(key == st.position)
            for w, v in ((self.size_slider, int(round(st.size_pct * 10))),
                         (self.words_spin, int(st.words_per_line))):
                w.blockSignals(True)
                w.setValue(v)
                w.blockSignals(False)
            self.size_label.setText(f"{st.size_pct:.1f} %")
            for field, b in self.color_buttons.items():
                b.setIcon(swatch_icon(getattr(st, field)))
            for chk, v in ((self.upper_check, st.uppercase), (self.highlight_check, st.highlight_words),
                           (self.box_check, st.box)):
                chk.blockSignals(True)
                chk.setChecked(bool(v))
                chk.blockSignals(False)
        finally:
            self._syncing_style = False

    def _edit_style(self, **changes) -> None:
        if self._syncing_style:
            return
        self.document.set_caption_style(dataclasses.replace(self.document.caption_style, **changes))

    def _apply_preset(self, name: str) -> None:
        if self._syncing_style:
            return
        self.document.set_caption_style(style_for_preset(name))
        self._sync_style()

    def _on_size(self, value: int) -> None:
        if self._syncing_style:
            return
        pct = round(min(max(value / 10.0, SIZE_MIN), SIZE_MAX), 1)
        self.size_label.setText(f"{pct:.1f} %")
        self._edit_style(size_pct=pct)

    def _on_words(self, value: int) -> None:
        if self._syncing_style:
            return
        self._edit_style(words_per_line=int(value))
        self._rechunk_timer.start()

    def pick_color(self, initial: str, title: str) -> Optional[str]:
        color = QColorDialog.getColor(QColor(initial), self, title)
        return color.name().upper() if color.isValid() else None

    def _pick_color(self, field: str) -> None:
        current = getattr(self.document.caption_style, field)
        chosen = self.pick_color(current, {"primary": "Text colour", "highlight": "Highlight colour",
                                           "outline": "Outline colour"}.get(field, "Colour"))
        if chosen:
            self._edit_style(**{field: chosen})

    def confirm_rechunk(self, words_per_line: int) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "Regroup captions?",
                          f"Regroup the existing captions into lines of up to {words_per_line} "
                          f"word{'s' if words_per_line != 1 else ''}?\n\n"
                          "Captions you edited by hand are left as they are.",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _offer_rechunk(self) -> None:
        """Offer to regroup existing lines from their timed words after the per-line count
        changed. Not offered when the captions are out of date (regenerate instead)."""
        doc = self.document
        n = doc.caption_style.words_per_line
        if not doc.captions or doc.captions_stale() or self.running():
            return
        words: list[Word] = [w for ln in doc.captions for w in ln.words]
        if not words or not self.confirm_rechunk(n):
            return
        kept = [ln for ln in doc.captions if not ln.words]
        lines = chunk_words(words, n) + [ln.copy() for ln in kept]
        lines.sort(key=lambda ln: ln.start)
        doc.set_captions(lines)

    # ----------------------------------------------------------------- generate
    def confirm_download(self, label: str, size_text: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "Download speech model?",
                          f"Auto-captions run on your computer. ClipForge needs to download the "
                          f"{label} model (about {size_text}) once from Hugging Face. Download?",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        return box.exec() == QMessageBox.StandardButton.Yes

    def confirm_replace(self) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "Replace captions?",
                          "Generating new captions replaces the current ones, including any "
                          "edits you made. Continue?",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def start_generate(self) -> bool:
        """Run the pipeline. Returns False when nothing was started."""
        if self.running():
            return False
        doc = self.document
        if not self._available():
            self._refresh_all()
            return False
        if doc.timeline.is_empty():
            self._say(NO_CLIPS_TEXT, "faint")
            return False
        if self._has_missing_media():
            self._say("Some clips use media that can't be found. Relink it first.", "error")
            return False
        key = self.model_combo.currentData()
        if key is None:
            return False
        if doc.captions and not self.confirm_replace():
            return False
        self._model_key = str(key)
        self._lang = self.lang_combo.currentData()
        self._say("")
        try:
            have = bool(tr.model_downloaded(self._model_key))
        except Exception:  # noqa: BLE001
            have = False
        if not have:
            info = tr.MODELS.get(self._model_key)
            label = getattr(info, "label", self._model_key)
            size = _model_size_text(float(getattr(info, "approx_download_mb", 0) or 0)) \
                if getattr(info, "approx_download_mb", 0) else "a few hundred MB"
            if not self.confirm_download(label, size):
                return False
            return self._start_download()
        return self._start_audio()

    def _has_missing_media(self) -> bool:
        doc = self.document
        for seg in doc.timeline.all_segments():
            src = doc.sources.get(seg.source_id)
            if src is not None and src.path and not os.path.isfile(src.path):
                return True
        return False

    def _set_stage(self, stage: str) -> None:
        self._stage = stage
        self.progress_bar.setRange(0, 0)
        self.stage_label.setText(STAGE_TEXT.get(stage, "") + "…" if stage else "")
        self.cancel_button.setEnabled(True)
        self._refresh_generate_state()

    def _attach(self, job: QObject) -> None:
        self._job = job
        job.progress.connect(self._on_progress)
        job.finished.connect(self._on_finished)
        job.failed.connect(self._on_failed)
        job.cancelled.connect(self._on_cancelled)

    def _start_download(self) -> bool:
        self._set_stage(STAGE_DOWNLOAD)
        try:
            job = tr.ModelDownloadJob(self._model_key, parent=self)
            self._attach(job)
            job.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not start the model download")
            self._fail(f"Could not start the download. {exc}")
            return False
        return True

    def _audible_clips(self) -> list:
        doc = self.document
        out = []
        for seg in doc.timeline.all_segments():
            src = doc.sources.get(seg.source_id)
            if not seg.muted and src is not None and getattr(src, "has_audio", True):
                out.append(seg)
        return out

    def _start_audio(self) -> bool:
        doc = self.document
        if not self._audible_clips():
            self._finish_run()
            self._say("There is no sound to caption: every clip is muted or has no audio.", "warning")
            return False
        segs = list(doc.timeline.all_segments())
        self._offset = min(s.timeline_start for s in segs)
        self._set_stage(STAGE_AUDIO)
        temp_dir = str(self.settings.temp_path())
        dest = os.path.join(temp_dir, f"clipforge-captions-{uuid.uuid4().hex[:10]}.wav")
        try:
            job = ae.ExtractAudioJob(dict(doc.sources), segs, dest, "wav", temp_dir=temp_dir,
                                     parent=self)
            self._attach(job)
            self._temp_wav = dest
            job.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not start the audio extraction")
            self._fail(f"Could not prepare the audio. {exc}")
            return False
        return True

    def _start_transcribe(self, wav_path: str) -> bool:
        self._temp_wav = wav_path
        self._set_stage(STAGE_TRANSCRIBE)
        try:
            job = tr.TranscribeJob(wav_path, model=self._model_key, language=self._lang,
                                   device="auto", parent=self)
            self._attach(job)
            job.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not start the transcription")
            self._fail(f"Could not start listening. {exc}")
            return False
        return True

    # -- job signals
    @Slot(float, str)
    def _on_progress(self, fraction: float, _text: str) -> None:
        if self.sender() is not self._job:
            return
        base = STAGE_TEXT.get(self._stage, "")
        if fraction is None or fraction < 0:
            self.progress_bar.setRange(0, 0)
            self.stage_label.setText(f"{base}…")
            return
        f = min(1.0, max(0.0, float(fraction)))
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(int(f * 1000))
        self.stage_label.setText(f"{base}… {int(round(f * 100))} %")

    def _end_job(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            job.deleteLater()

    def _on_finished(self, *args) -> None:
        if self.sender() is not self._job:
            return
        stage = self._stage
        self._end_job()
        if stage == STAGE_DOWNLOAD:
            self._fill_models()
            self._start_audio()
        elif stage == STAGE_AUDIO:
            path = str(args[0]) if args and args[0] else (self._temp_wav or "")
            if not path:
                self._fail("The audio could not be prepared.")
                return
            self._start_transcribe(path)
        elif stage == STAGE_TRANSCRIBE:
            self._apply_words(list(args[0]) if args and args[0] else [])

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        if self.sender() is not self._job:
            return
        stage = self._stage
        self._end_job()
        prefix = {STAGE_DOWNLOAD: "The model could not be downloaded. ",
                  STAGE_AUDIO: "The audio could not be prepared. ",
                  STAGE_TRANSCRIBE: "Captions could not be generated. "}.get(stage, "")
        self._fail(prefix + (message or "Something went wrong."))

    @Slot()
    def _on_cancelled(self) -> None:
        if self.sender() is not self._job:
            return
        self._end_job()
        self._finish_run()
        self._say("Cancelled.", "faint")

    def _apply_words(self, words: list[Word]) -> None:
        wav, self._temp_wav = self._temp_wav, None
        _remove_file(wav)
        self._finish_run()
        if not words:
            self._say("No speech was found in the timeline's audio.", "warning")
            return
        shifted = [Word(w.start + self._offset, w.end + self._offset, w.text) for w in words]
        n = self.document.caption_style.words_per_line
        try:
            lines = tr.words_to_lines(shifted, n)
        except Exception:  # noqa: BLE001
            log.exception("words_to_lines failed")
            lines = chunk_words(shifted, n)
        if not lines:
            self._say("No speech was found in the timeline's audio.", "warning")
            return
        self.document.set_captions(lines)
        self._say(f"Done: {len(lines)} captions added.", "success")

    def _fail(self, message: str) -> None:
        retire_job(self._job)
        self._job = None
        self._finish_run()
        self._say(message, "error")

    def _finish_run(self) -> None:
        """Back to idle: the temp file goes away and the form is usable again."""
        _remove_file(self._temp_wav)
        self._temp_wav = None
        self._stage = STAGE_NONE
        self.stage_label.setText("")
        self._refresh_generate_state()

    def cancel_generate(self) -> None:
        """Stop whatever stage is running; late signals from it are ignored."""
        if not self.running():
            return
        self._abort("Cancelled.")

    def _abort(self, message: str) -> None:
        job, self._job = self._job, None
        temp, self._temp_wav = self._temp_wav, None
        retire_job(job, temp)
        self._finish_run()
        self._say(message, "faint")

    # ------------------------------------------------------------------ closing
    def shutdown(self) -> None:
        """Cancel running jobs and stop timers (window close, project switch, app quit)."""
        self._rechunk_timer.stop()
        self._table_timer.stop()
        self._gpu_token += 1
        if self.running():
            self._abort("")

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)

    def done(self, result: int) -> None:  # Esc, window close button, accept/reject
        self.shutdown()
        super().done(result)
