"""Main window: toolbar, the three resizable panels, shortcuts, project lifecycle."""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional, Sequence

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QIcon, QKeySequence
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
                               QMessageBox, QSplitter, QToolButton, QVBoxLayout, QWidget)

from .. import __app_name__, paths
from ..core.model import MediaSource
from ..core.project import PROJECT_EXTENSION, ProjectError
from ..document import Document
from ..media import MediaError, ffmpeg
from ..media.probe import SUPPORTED_EXTENSIONS, probe_source
from ..media.thumbnails import ThumbnailProvider
from ..media.waveform import WaveformProvider
from ..settings import Settings
from ..workers import Task
from . import icons, theme
from .export_dialog import ExportDialog
from .left_panel import LeftPanel
from .preview import PreviewPanel
from .settings_dialog import SettingsDialog
from .timeline import TimelinePanel, can_detach_audio

log = logging.getLogger(__name__)

VIDEO_FILTER = "Video files (*.mp4 *.mov *.mkv *.webm *.m4v);;All files (*.*)"
PROJECT_FILTER = f"ClipForge project (*{PROJECT_EXTENSION})"


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.document = Document(self)
        self.thumbnails = ThumbnailProvider(str(settings.thumbnail_cache_dir()), self)
        self.waveforms = WaveformProvider(str(settings.waveform_cache_dir()), self)
        self._tasks: set[Task] = set()
        self._import_count = 0
        self._closing = False

        self.setWindowTitle(__app_name__)
        self.setMinimumSize(1100, 680)
        self.resize(1480, 900)
        self.setAcceptDrops(True)
        icon_file = paths.resource_path("icon.ico")
        if icon_file.is_file():
            self.setWindowIcon(QIcon(str(icon_file)))

        self._build_ui()
        self._build_actions()
        self._connect()
        self._restore_window_state()
        self._refresh_title()
        self._refresh_actions()
        QTimer.singleShot(0, self._check_dependencies)

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.top_bar = QFrame(objectName="TopBar")
        self.top_bar.setFixedHeight(52)
        self.top_layout = QHBoxLayout(self.top_bar)
        self.top_layout.setContentsMargins(14, 0, 14, 0)
        self.top_layout.setSpacing(4)
        root.addWidget(self.top_bar)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(8, 8, 8, 8)
        body_layout.setSpacing(0)
        root.addWidget(body, 1)

        self.left_panel = LeftPanel(self.document, self.settings, self.thumbnails)
        self.preview = PreviewPanel(self.document, self.settings)
        self.timeline_panel = TimelinePanel(self.document, self.thumbnails, self.waveforms)

        self.left_frame = self._panel(self.left_panel)
        self.preview_frame = self._panel(self.preview)
        self.timeline_frame = self._panel(self.timeline_panel)
        self.left_frame.setMinimumWidth(270)
        self.preview_frame.setMinimumWidth(420)
        self.preview_frame.setMinimumHeight(240)
        self.timeline_frame.setMinimumHeight(190)

        self.h_split = QSplitter(Qt.Orientation.Horizontal)
        self.h_split.addWidget(self.left_frame)
        self.h_split.addWidget(self.preview_frame)
        self.h_split.setStretchFactor(0, 0)
        self.h_split.setStretchFactor(1, 1)
        self.h_split.setSizes([340, 1100])
        self.h_split.setChildrenCollapsible(False)

        self.v_split = QSplitter(Qt.Orientation.Vertical)
        self.v_split.addWidget(self.h_split)
        self.v_split.addWidget(self.timeline_frame)
        self.v_split.setStretchFactor(0, 1)
        self.v_split.setStretchFactor(1, 0)
        self.v_split.setSizes([560, 300])
        self.v_split.setChildrenCollapsible(False)
        body_layout.addWidget(self.v_split)

        self.setCentralWidget(central)

        self.status_label = QLabel("Ready")
        self.statusBar().addWidget(self.status_label, 1)
        self.encoder_label = QLabel("")
        self.statusBar().addPermanentWidget(self.encoder_label)
        self.statusBar().setSizeGripEnabled(False)
        self._status_timer = QTimer(self, singleShot=True, interval=6000)
        self._status_timer.timeout.connect(lambda: self.status_label.setText("Ready"))

    @staticmethod
    def _panel(widget: QWidget) -> QFrame:
        frame = QFrame()
        frame.setProperty("panel", True)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        lay.addWidget(widget)
        return frame

    def _action(self, text: str, slot: Callable, shortcuts: Sequence[str | QKeySequence] = (),
                icon_name: str = "", tip: str = "") -> QAction:
        act = QAction(text, self)
        if icon_name:
            act.setIcon(icons.icon(icon_name))
        if shortcuts:
            act.setShortcuts([QKeySequence(s) for s in shortcuts])
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        hint = f" ({QKeySequence(shortcuts[0]).toString()})" if shortcuts else ""
        act.setToolTip((tip or text) + hint)
        act.triggered.connect(lambda _checked=False: slot())
        self.addAction(act)
        return act

    def _build_actions(self) -> None:
        d, pv, tl = self.document, self.preview, self.timeline_panel
        a = self._action
        self.act_new = a("New Project", self.new_project, ["Ctrl+N"], "new")
        self.act_open = a("Open Project", self.open_project, ["Ctrl+O"], "open")
        self.act_save = a("Save Project", self.save_project, ["Ctrl+S"], "save")
        self.act_save_as = a("Save Project As…", self.save_project_as, ["Ctrl+Shift+S"])
        self.act_import = a("Import Video", self.import_dialog, ["Ctrl+I"], "import")
        self.act_settings = a("Settings", self.open_settings, ["Ctrl+,"], "settings")
        self.act_export = a("Export", self.open_export, ["Ctrl+E"], "export")
        self.act_export_audio = a("Export audio only…", self.export_audio, ["Ctrl+Shift+E"], None,
                                  "Save the sound of the selected clips (or the whole timeline) "
                                  "as mp3, m4a or wav")

        # editing / transport shortcuts (buttons for these live in the panels)
        self.act_undo = a("Undo", d.undo, ["Ctrl+Z"])
        self.act_redo = a("Redo", d.redo, ["Ctrl+Y", "Ctrl+Shift+Z"])
        self.act_split = a("Split at playhead", d.split_at_playhead, ["S", "Ctrl+B"])
        self.act_delete = a("Delete", lambda: d.delete_selected(ripple=False), ["Del", "Backspace"])
        self.act_ripple = a("Ripple delete", lambda: d.delete_selected(ripple=True),
                            ["Shift+Del", "Shift+Backspace"])
        self.act_mute = a("Mute clip", d.toggle_mute_selected, ["M"])
        self.act_detach = a("Detach audio", d.detach_audio, ["Ctrl+Shift+D"], "detach-audio",
                            "Detach audio from the selected video clip onto the audio lane")
        tl.view.exportAudioRequested.connect(self.export_audio)
        self.act_select_all = a("Select all clips", d.select_all, ["Ctrl+A"])
        self.act_deselect = a("Deselect", d.clear_selection, ["Ctrl+D"])
        self.act_play = a("Play / Pause", pv.toggle_play, ["Space"])
        self.act_prev_frame = a("Previous frame", lambda: pv.step_frames(-1), ["Left"])
        self.act_next_frame = a("Next frame", lambda: pv.step_frames(1), ["Right"])
        self.act_prev_10 = a("Back 10 frames", lambda: pv.step_frames(-10), ["Shift+Left"])
        self.act_next_10 = a("Forward 10 frames", lambda: pv.step_frames(10), ["Shift+Right"])
        self.act_j = a("Shuttle back", lambda: pv.shuttle(-1), ["J"])
        self.act_k = a("Shuttle stop", lambda: pv.shuttle(0), ["K"])
        self.act_l = a("Shuttle forward", lambda: pv.shuttle(1), ["L"])
        self.act_home = a("Go to start", pv.go_to_start, ["Home"])
        self.act_end = a("Go to end", pv.go_to_end, ["End"])
        self.act_prev_cut = a("Previous cut", lambda: self._jump_cut(-1), ["Up"])
        self.act_next_cut = a("Next cut", lambda: self._jump_cut(1), ["Down"])
        self.act_zoom_in = a("Zoom in", tl.zoom_in, ["Ctrl+=", "Ctrl++"])
        self.act_zoom_out = a("Zoom out", tl.zoom_out, ["Ctrl+-"])
        self.act_zoom_fit = a("Zoom to fit", tl.zoom_to_fit, ["Shift+Z", "Ctrl+0"])
        self.act_paste_url = a("Paste link", self._focus_url, ["Ctrl+L"])

        self._build_top_bar()

    def _build_top_bar(self) -> None:
        lay = self.top_layout
        p = theme.current()
        logo = QLabel()
        icon_png = paths.resource_path("icon.png")
        if icon_png.is_file():
            logo.setPixmap(QIcon(str(icon_png)).pixmap(26, 26))
        else:
            logo.setPixmap(icons.pixmap("film", p.accent, 24))
        brand = QLabel(__app_name__)
        brand.setProperty("role", "brand")
        lay.addWidget(logo)
        lay.addSpacing(6)
        lay.addWidget(brand)
        lay.addSpacing(14)
        lay.addWidget(self._vline())
        lay.addSpacing(8)

        def button(action: QAction, text: bool = True, primary: bool = False) -> QToolButton:
            b = QToolButton()
            b.setDefaultAction(action)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setIconSize(icons.icon_size(18))
            if text:
                b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                b.setProperty("text-beside", True)
            if primary:
                b.setProperty("variant", "primary")
            return b

        self.btn_new = button(self.act_new)
        self.btn_open = button(self.act_open)
        self.btn_save = button(self.act_save)
        save_menu = QMenu(self)
        save_menu.addAction(self.act_save)
        save_menu.addAction(self.act_save_as)
        self.btn_save.setMenu(save_menu)
        self.btn_save.setPopupMode(QToolButton.ToolButtonPopupMode.DelayedPopup)
        self.btn_save.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_save.customContextMenuRequested.connect(
            lambda pos: save_menu.exec(self.btn_save.mapToGlobal(pos)))
        self.recent_menu = QMenu(self)
        self.btn_open.setMenu(self.recent_menu)
        self.btn_open.setPopupMode(QToolButton.ToolButtonPopupMode.DelayedPopup)
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        self.btn_import = button(self.act_import)
        for b in (self.btn_new, self.btn_open, self.btn_save):
            lay.addWidget(b)
        lay.addSpacing(8)
        lay.addWidget(self._vline())
        lay.addSpacing(8)
        lay.addWidget(self.btn_import)

        lay.addStretch(1)
        self.project_label = QLabel("Untitled")
        self.project_label.setProperty("role", "dim")
        lay.addWidget(self.project_label)
        lay.addStretch(1)

        self.btn_settings = button(self.act_settings)
        lay.addWidget(self.btn_settings)
        lay.addSpacing(6)
        self.act_export.setIcon(icons.icon("export", theme.current().accent_text))
        self.btn_export = button(self.act_export, primary=True)
        lay.addWidget(self.btn_export)

    @staticmethod
    def _vline() -> QFrame:
        line = QFrame()
        line.setProperty("separator", True)
        line.setFixedSize(1, 22)
        return line

    def _connect(self) -> None:
        d = self.document
        d.dirtyChanged.connect(lambda _d: self._refresh_title())
        d.projectReset.connect(self._refresh_title)
        d.timelineChanged.connect(self._refresh_actions)
        d.sourcesChanged.connect(self._refresh_actions)
        d.selectionChanged.connect(self._refresh_actions)
        d.playheadChanged.connect(self._refresh_actions)
        d.message.connect(self.show_status)
        self.left_panel.downloadFinished.connect(self._on_download_finished)
        self.left_panel.importRequested.connect(self.import_dialog)
        self.left_panel.relinkRequested.connect(self.relink_source)
        self.left_panel.statusMessage.connect(self.show_status)
        self.h_split.splitterMoved.connect(lambda *_: None)

    # ----------------------------------------------------------------- status
    def show_status(self, text: str, timeout_ms: int = 6000) -> None:
        self.status_label.setText(text)
        if timeout_ms > 0:
            self._status_timer.start(timeout_ms)
        else:
            self._status_timer.stop()

    def _refresh_title(self) -> None:
        name = self.document.display_name()
        dirty = self.document.dirty
        self.project_label.setText(f"{name}{'  •' if dirty else ''}")
        self.project_label.setToolTip(self.document.path or "Project not saved yet")
        self.setWindowTitle(f"{name}{'*' if dirty else ''} — {__app_name__}")

    def _refresh_actions(self) -> None:
        has_clips = not self.document.timeline.is_empty()
        self.act_export.setEnabled(has_clips)
        self.act_detach.setEnabled(can_detach_audio(self.document))

    def _check_dependencies(self) -> None:
        try:
            missing = ffmpeg.missing_dependencies()
        except Exception as exc:  # noqa: BLE001
            log.exception("Dependency check failed")
            missing = [str(exc)]
        if missing:
            self.encoder_label.setText("FFmpeg missing")
            QMessageBox.warning(
                self, "FFmpeg not available",
                "ClipForge could not find or run: " + ", ".join(missing) + ".\n\n"
                "Downloading, importing and exporting need FFmpeg and FFprobe. Reinstall "
                "ClipForge, or put ffmpeg.exe and ffprobe.exe in a folder named 'ffmpeg' "
                "next to ClipForge.exe (or on your PATH), then restart.")
            return
        task = Task(lambda cancel, progress: (ffmpeg.version(),
                                              ffmpeg.pick_video_encoder(self.settings.export_codec,
                                                                        self.settings.hw_accel)),
                    name="encoder-probe")
        task.signals.result.connect(self._on_encoder_probe)
        self._run(task)

    def _on_encoder_probe(self, result: tuple) -> None:
        version, encoder = result
        kind = "GPU" if ffmpeg.is_hardware_encoder(encoder) else "CPU"
        self.encoder_label.setText(f"FFmpeg {version}  ·  {kind} encoder: {encoder}")

    def _run(self, task: Task) -> Task:
        self._tasks.add(task)
        task.signals.finished.connect(lambda t=task: self._tasks.discard(t))
        return task.start()

    # ---------------------------------------------------------------- project
    def maybe_save(self) -> bool:
        """Offer to save unsaved changes. Returns False if the user cancelled."""
        if not self.document.dirty:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"Save changes to “{self.document.display_name()}”?")
        box.setInformativeText("Your edits will be lost if you don't save them.")
        save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Don't Save", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel or clicked is None:
            return False
        if clicked is save:
            return self.save_project()
        return True

    def new_project(self) -> None:
        if not self.maybe_save():
            return
        self.preview.pause()
        self.document.new_project()
        self.show_status("New project")

    def open_project(self, path: Optional[str] = None) -> None:
        if not self.maybe_save():
            return
        if not path:
            start = os.path.dirname(self.document.path) if self.document.path else \
                str(paths.ensure_dir(os.path.dirname(self.settings.export_dir) or "."))
            path, _ = QFileDialog.getOpenFileName(self, "Open Project", start, PROJECT_FILTER)
            if not path:
                return
        self.preview.pause()
        try:
            missing = self.document.load(path)
        except ProjectError as exc:
            log.warning("Open project failed: %s", exc)
            QMessageBox.critical(self, "Can't open project", str(exc))
            return
        self.settings.add_recent(path)
        self.settings.save()
        self.show_status(f"Opened {os.path.basename(path)}")
        if missing:
            QTimer.singleShot(0, lambda: self._resolve_missing(missing))

    def _resolve_missing(self, missing: list[MediaSource]) -> None:
        names = "\n".join(f"  •  {os.path.basename(s.path)}" for s in missing[:8])
        more = f"\n  … and {len(missing) - 8} more" if len(missing) > 8 else ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Missing media")
        box.setText(f"{len(missing)} media file(s) used by this project can't be found:")
        box.setInformativeText(names + more + "\n\nLocate them now? You can also relink later "
                               "by right-clicking the media in the library.")
        locate = box.addButton("Locate…", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not locate:
            return
        folder_hint = ""
        for src in missing:
            if os.path.isfile(src.path):
                continue
            # a file with the same name in a folder the user already pointed at
            if folder_hint:
                candidate = os.path.join(folder_hint, os.path.basename(src.path))
                if os.path.isfile(candidate) and self._relink_to(src.id, candidate, quiet=True):
                    continue
            chosen = self.relink_source(src.id)
            if chosen is None:
                break
            folder_hint = os.path.dirname(chosen)

    def relink_source(self, source_id: str) -> Optional[str]:
        """Ask for the new location of a source. Returns the chosen path or None."""
        src = self.document.sources.get(source_id)
        if src is None:
            return None
        start = os.path.dirname(src.path) if os.path.isdir(os.path.dirname(src.path)) else ""
        path, _ = QFileDialog.getOpenFileName(
            self, f"Locate “{os.path.basename(src.path)}”",
            os.path.join(start, os.path.basename(src.path)), VIDEO_FILTER)
        if not path:
            return None
        return path if self._relink_to(source_id, path) else None

    def _relink_to(self, source_id: str, path: str, quiet: bool = False) -> bool:
        src = self.document.sources[source_id]
        try:
            probed = probe_source(path)
        except MediaError as exc:
            if not quiet:
                QMessageBox.warning(self, "Can't use this file", str(exc))
            return False
        if abs(probed.duration - src.duration) > max(0.5, src.frame_duration * 3):
            if quiet:
                return False
            answer = QMessageBox.question(
                self, "Different media",
                f"The selected file is {probed.duration:.2f} s long but the project expects "
                f"{src.duration:.2f} s. Clips may no longer line up.\n\nUse it anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return False
            needed = max((s.source_out for s in self.document.timeline.all_segments()
                          if s.source_id == source_id), default=0.0)
            if probed.duration + 1e-3 < needed:
                QMessageBox.warning(self, "File too short",
                                    "This file is shorter than the clips that use it.")
                return False
        self.preview.release_media()
        try:
            self.document.relink(source_id, path)
        except ProjectError as exc:
            QMessageBox.warning(self, "Relink failed", str(exc))
            return False
        self.show_status(f"Relinked {os.path.basename(path)}")
        return True

    def save_project(self) -> bool:
        if not self.document.path:
            return self.save_project_as()
        return self._save_to(self.document.path)

    def save_project_as(self) -> bool:
        start_dir = os.path.dirname(self.document.path) if self.document.path else \
            str(paths.ensure_dir(os.path.join(os.path.dirname(self.settings.export_dir) or ".",
                                              "Projects")))
        suggested = os.path.join(start_dir, self.document.display_name() + PROJECT_EXTENSION)
        path, _ = QFileDialog.getSaveFileName(self, "Save Project As", suggested, PROJECT_FILTER)
        if not path:
            return False
        if not path.lower().endswith(PROJECT_EXTENSION):
            path += PROJECT_EXTENSION
        return self._save_to(path)

    def _save_to(self, path: str) -> bool:
        try:
            self.document.save(path)
        except ProjectError as exc:
            log.warning("Save failed: %s", exc)
            QMessageBox.critical(self, "Can't save project", str(exc))
            return False
        self.settings.add_recent(path)
        self.settings.save()
        self._refresh_title()
        self.show_status(f"Saved {os.path.basename(path)}")
        return True

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        self.recent_menu.addAction(self.act_open)
        recents = [p for p in self.settings.recent_projects if os.path.isfile(p)]
        if recents:
            self.recent_menu.addSeparator()
        for p in recents:
            act = self.recent_menu.addAction(os.path.basename(p))
            act.setToolTip(p)
            act.triggered.connect(lambda _c=False, path=p: self.open_project(path))

    # ----------------------------------------------------------------- import
    def import_dialog(self) -> None:
        start = self.settings.download_dir if os.path.isdir(self.settings.download_dir) else ""
        files, _ = QFileDialog.getOpenFileNames(self, "Import Video", start, VIDEO_FILTER)
        if files:
            self.import_files(files)

    def import_files(self, files: Sequence[str], source_url: Optional[str] = None,
                     title: Optional[str] = None) -> None:
        """Probe files in the background, then add them to the library and timeline."""
        files = [os.path.abspath(f) for f in files]
        existing = {os.path.normcase(s.path): s for s in self.document.sources.values()}
        to_probe: list[str] = []
        for f in files:
            already = existing.get(os.path.normcase(f))
            if already is not None:                      # same file again → just add a clip
                self.document.append_to_timeline(already.id)
            else:
                to_probe.append(f)
        if not to_probe:
            return

        def work(cancel, progress):
            results: list = []
            for f in to_probe:
                cancel.raise_if_cancelled()
                try:
                    results.append(probe_source(f, source_url=source_url, title=title))
                except MediaError as exc:
                    log.warning("Import failed for %s: %s | %s", f, exc, exc.detail)
                    results.append((f, str(exc)))
            return results

        self._import_count += 1
        self.left_panel.set_busy_importing(True)
        self.show_status("Importing…", 0)
        task = Task(work, name="import")
        task.signals.result.connect(self._on_imported)
        task.signals.failed.connect(lambda msg: QMessageBox.warning(self, "Import failed", msg))
        task.signals.finished.connect(self._on_import_done)
        self._run(task)

    def _on_import_done(self) -> None:
        self._import_count = max(0, self._import_count - 1)
        if self._import_count == 0:
            self.left_panel.set_busy_importing(False)

    def _on_imported(self, results: list) -> None:
        if self._closing:
            return
        errors, added = [], 0
        was_empty = self.document.timeline.is_empty()
        for item in results:
            if isinstance(item, MediaSource):
                self.document.add_source(item, to_timeline=True)
                added += 1
            else:
                path, message = item
                errors.append(f"{os.path.basename(path)}\n{message}")
        if added:
            self.show_status(f"Imported {added} file{'s' if added != 1 else ''}")
            if was_empty:
                self.document.seek(0.0)
                self.document.clear_selection()
        else:
            self.show_status("Nothing imported")
        if errors:
            QMessageBox.warning(self, "Some files couldn't be imported", "\n\n".join(errors))

    def _on_download_finished(self, path: str, info: object) -> None:
        self.import_files([path], source_url=getattr(info, "url", None),
                          title=getattr(info, "title", None))

    # ----------------------------------------------------------------- dialogs
    def open_export(self) -> None:
        if self.document.timeline.is_empty():
            self.show_status("Add a clip to the timeline before exporting.")
            return
        self.preview.pause()
        missing = [s for s in self.document.project.missing_sources()
                   if s.id in self.document.project.used_source_ids()]
        if missing:
            QMessageBox.warning(self, "Media offline",
                                "Some clips use media that can't be found. Relink it (right-click "
                                "the media in the library) or delete those clips before exporting.")
            return
        dialog = ExportDialog(self.document, self.settings, self)
        dialog.exec()
        dialog.deleteLater()

    def export_audio(self) -> None:
        """Save the sound of the selection (or the whole timeline) as an audio file."""
        import os
        from PySide6.QtWidgets import QFileDialog
        from ..media.audio_extract import ExtractAudioJob
        doc = self.document
        if doc.timeline.is_empty():
            self.show_status("Add a clip to the timeline first.")
            return
        clips = doc.selected_segments() or doc.timeline.all_segments()
        if any(doc.sources[c.source_id].path and not os.path.isfile(doc.sources[c.source_id].path)
               for c in clips if c.source_id in doc.sources):
            QMessageBox.warning(self, "Media offline", "Some clips use media that can't be found.")
            return
        self.preview.pause()
        start = os.path.join(self.settings.default_export_dir() if hasattr(self.settings, "default_export_dir")
                             else os.path.expanduser("~"), f"{doc.display_name()}.mp3")
        path, chosen = QFileDialog.getSaveFileName(
            self, "Export audio", start, "MP3 (*.mp3);;AAC / M4A (*.m4a);;WAV (*.wav)")
        if not path:
            return
        fmt = {"MP3": "mp3", "AAC": "m4a", "WAV": "wav"}.get(chosen.split(" ")[0], None)
        job = ExtractAudioJob(dict(doc.sources), clips, path, fmt,
                              temp_dir=str(self.settings.temp_path()), parent=self)
        self._audio_job = job
        job.progress.connect(lambda f, t: self.show_status(f"Exporting audio… {int(f * 100)}%"))
        job.finished.connect(lambda out: self.show_status(f"Audio saved: {out}", 8000))
        job.failed.connect(lambda msg: QMessageBox.warning(self, "Audio export failed", msg))
        job.start()

    def open_settings(self) -> None:
        old_theme, old_cache = self.settings.theme, self.settings.cache_dir
        dialog = SettingsDialog(self.settings, self)
        accepted = dialog.exec()
        dialog.deleteLater()
        if not accepted:
            return
        if self.settings.cache_dir != old_cache:
            self.thumbnails.set_cache_dir(str(self.settings.thumbnail_cache_dir()))
            self.waveforms.set_cache_dir(str(self.settings.waveform_cache_dir()))
        if self.settings.theme != old_theme:
            self.show_status("Theme saved — restart ClipForge to apply it everywhere.", 10000)
        self._check_dependencies()

    def _focus_url(self) -> None:
        self.left_panel.focus_url()

    def _jump_cut(self, direction: int) -> None:
        t = self.document.playhead
        points = self.document.timeline.snap_points() + [self.document.duration]
        if direction > 0:
            later = [p for p in points if p > t + 1e-4]
            target = min(later) if later else self.document.duration
        else:
            earlier = [p for p in points if p < t - 1e-4]
            target = max(earlier) if earlier else 0.0
        self.preview.pause()
        self.document.seek(target)

    # --------------------------------------------------------------- drag-drop
    @staticmethod
    def _dropped_paths(event) -> list[str]:
        if not event.mimeData().hasUrls():
            return []
        return [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        ok = (SUPPORTED_EXTENSIONS + (PROJECT_EXTENSION,))
        if any(p.lower().endswith(ok) for p in self._dropped_paths(event)):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        dropped = self._dropped_paths(event)
        projects = [p for p in dropped if p.lower().endswith(PROJECT_EXTENSION)]
        videos = [p for p in dropped if p.lower().endswith(SUPPORTED_EXTENSIONS)]
        event.acceptProposedAction()
        if projects:
            QTimer.singleShot(0, lambda: self.open_project(projects[0]))
        elif videos:
            QTimer.singleShot(0, lambda: self.import_files(videos))

    def open_path(self, path: str) -> None:
        """Open a project or import a video given on the command line."""
        if path.lower().endswith(PROJECT_EXTENSION):
            self.open_project(path)
        elif os.path.isfile(path):
            self.import_files([path])

    # ------------------------------------------------------------ window state
    def _restore_window_state(self) -> None:
        s = self.settings
        try:
            if s.window_geometry:
                self.restoreGeometry(QByteArray.fromBase64(s.window_geometry.encode()))
            if s.splitter_main:
                self.h_split.restoreState(QByteArray.fromBase64(s.splitter_main.encode()))
            if s.splitter_vertical:
                self.v_split.restoreState(QByteArray.fromBase64(s.splitter_vertical.encode()))
        except Exception:  # noqa: BLE001 - stale state must never block start-up
            log.exception("Could not restore window layout")

    def _save_window_state(self) -> None:
        s = self.settings
        s.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        s.splitter_main = bytes(self.h_split.saveState().toBase64()).decode()
        s.splitter_vertical = bytes(self.v_split.saveState().toBase64()).decode()
        s.save()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.left_panel.is_downloading():
            answer = QMessageBox.question(self, "Download in progress",
                                          "A download is still running. Cancel it and quit?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if not self.maybe_save():
            event.ignore()
            return
        self._closing = True
        self._save_window_state()
        for task in list(self._tasks):
            task.cancel()
        for closer in (self.preview.shutdown, self.left_panel.shutdown,
                       self.thumbnails.shutdown, self.waveforms.shutdown):
            try:
                closer()
            except Exception:  # noqa: BLE001
                log.exception("Error during shutdown")
        event.accept()
