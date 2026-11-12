"""The open project as a QObject: the single place the UI reads from and edits through.

Widgets never mutate the timeline directly. They call the edit methods here, which
apply the change to the model, record undo history, mark the project dirty and emit
change signals that the timeline, preview and panels listen to.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

from PySide6.QtCore import QObject, Signal

from .core.history import History
from .core.model import EditError, ExportSettings, MediaSource, Segment, Timeline
from .core.project import Project

log = logging.getLogger(__name__)


class Document(QObject):
    timelineChanged = Signal()        # segments added/removed/moved/trimmed/muted
    selectionChanged = Signal()
    sourcesChanged = Signal()         # media library content changed
    historyChanged = Signal()         # undo/redo availability or labels changed
    dirtyChanged = Signal(bool)
    projectReset = Signal()           # a different project is now loaded (new/open)
    playheadChanged = Signal(float)   # timeline seconds; emitted for every move
    seekRequested = Signal(float)     # user asked to jump: the player must seek
    message = Signal(str)             # short status-bar text (e.g. why an edit was refused)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.project = Project()
        self._selection: list[str] = []
        self._dirty = False
        self._drag_snapshot: Optional[list[Segment]] = None

    # ----------------------------------------------------------------- access
    @property
    def timeline(self) -> Timeline:
        return self.project.timeline

    @property
    def sources(self) -> dict[str, MediaSource]:
        return self.project.sources

    @property
    def history(self) -> History:
        return self.project.history

    @property
    def export_settings(self) -> ExportSettings:
        return self.project.export

    @property
    def path(self) -> Optional[str]:
        return self.project.path

    @property
    def playhead(self) -> float:
        return self.project.playhead

    @property
    def duration(self) -> float:
        return self.timeline.duration

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def selection(self) -> list[str]:
        return list(self._selection)

    def selected_segments(self) -> list[Segment]:
        ids = set(self._selection)
        return [s for s in self.timeline.all_segments() if s.id in ids]

    def source_of(self, segment: Segment) -> Optional[MediaSource]:
        return self.sources.get(segment.source_id)

    def display_name(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0] if self.path else "Untitled"

    # ---------------------------------------------------------------- project
    def _set_dirty(self, dirty: bool) -> None:
        if dirty != self._dirty:
            self._dirty = dirty
            self.dirtyChanged.emit(dirty)

    def mark_dirty(self) -> None:
        self._set_dirty(True)

    def _install(self, project: Project) -> None:
        self.project = project
        self._selection = []
        self._drag_snapshot = None
        self._dirty = False
        self.projectReset.emit()
        self.sourcesChanged.emit()
        self.timelineChanged.emit()
        self.selectionChanged.emit()
        self.historyChanged.emit()
        self.dirtyChanged.emit(False)
        self.playheadChanged.emit(self.project.playhead)
        self.seekRequested.emit(self.project.playhead)

    def new_project(self) -> None:
        self._install(Project())

    def load(self, path: str) -> list[MediaSource]:
        """Open a project (raises ``ProjectError``). Returns sources whose files are missing."""
        project = Project.load(path)
        self._install(project)
        return project.missing_sources()

    def save(self, path: Optional[str] = None) -> None:
        """Save to ``path`` or the current path (raises ``ProjectError``)."""
        target = path or self.project.path
        if not target:
            raise ValueError("No project path")
        self.project.save(target)
        was_dirty = self._dirty
        self._set_dirty(False)
        if not was_dirty:               # title also depends on the (possibly new) path
            self.dirtyChanged.emit(False)

    def relink(self, source_id: str, new_path: str) -> None:
        self.project.relink(source_id, new_path)
        self._set_dirty(True)
        self.sourcesChanged.emit()
        self.timelineChanged.emit()

    def set_export_settings(self, settings: ExportSettings) -> None:
        if settings != self.project.export:
            self.project.export = settings
            self._set_dirty(True)

    # ------------------------------------------------------------------ media
    def add_source(self, source: MediaSource, to_timeline: bool = True) -> None:
        """Add media to the library and (by default) append it to the timeline."""
        self.sources[source.id] = source
        self.sourcesChanged.emit()
        if to_timeline:
            self.append_to_timeline(source.id)
        else:
            self._set_dirty(True)

    def append_to_timeline(self, source_id: str) -> None:
        source = self.sources[source_id]
        before = self.timeline.snapshot()
        seg = self.timeline.append_source(source)
        self._committed("Add clip", before)
        self.select([seg.id])

    def remove_source(self, source_id: str) -> bool:
        """Remove unused media from the library. Returns False if clips still use it."""
        if source_id in self.project.used_source_ids():
            self.message.emit("This media is used on the timeline. Delete its clips first.")
            return False
        for entry in self.history.undo_stack + self.history.redo_stack:
            if any(s.source_id == source_id for s in entry.segments):
                self.history.clear()        # history referenced it; drop to stay consistent
                self.historyChanged.emit()
                break
        self.sources.pop(source_id, None)
        self._set_dirty(True)
        self.sourcesChanged.emit()
        return True

    # -------------------------------------------------------------- selection
    def select(self, ids: Iterable[str]) -> None:
        valid = {s.id for s in self.timeline.all_segments()}
        new = [i for i in dict.fromkeys(ids) if i in valid]
        if new != self._selection:
            self._selection = new
            self.selectionChanged.emit()

    def toggle_select(self, segment_id: str) -> None:
        sel = list(self._selection)
        if segment_id in sel:
            sel.remove(segment_id)
        else:
            sel.append(segment_id)
        self.select(sel)

    def clear_selection(self) -> None:
        self.select([])

    def select_all(self) -> None:
        self.select([s.id for s in self.timeline.all_segments()])

    # --------------------------------------------------------------- playhead
    def seek(self, t: float) -> None:
        """User-initiated jump (ruler click, scrub, frame step). The player follows."""
        t = min(max(0.0, t), self.duration)
        self.project.playhead = t
        self.playheadChanged.emit(t)
        self.seekRequested.emit(t)

    def set_playhead(self, t: float) -> None:
        """Position report from the player during playback (does not trigger a seek)."""
        t = min(max(0.0, t), self.duration)
        if abs(t - self.project.playhead) > 1e-9:
            self.project.playhead = t
            self.playheadChanged.emit(t)

    # ------------------------------------------------------------------ edits
    def _committed(self, label: str, before: list[Segment]) -> None:
        self.history.record(label, before)
        self._after_change()

    def _after_change(self) -> None:
        valid = {s.id for s in self.timeline.all_segments()}
        pruned = [i for i in self._selection if i in valid]
        selection_changed = pruned != self._selection
        self._selection = pruned
        self._set_dirty(True)
        self.timelineChanged.emit()
        self.historyChanged.emit()
        if selection_changed:
            self.selectionChanged.emit()
        if self.project.playhead > self.duration:
            self.seek(self.duration)
        else:
            self.seekRequested.emit(self.project.playhead)   # refresh the preview frame

    def _refuse(self, exc: Exception) -> bool:
        self.message.emit(str(exc))
        return False

    def split_at_playhead(self) -> bool:
        """Split the selected clip(s) under the playhead, or else the clip under it."""
        t = self.playhead
        targets = [s for s in self.selected_segments() if s.contains(t)]
        if not targets:
            under = self.timeline.segment_at(t)
            targets = [under] if under else []
        if not targets:
            return self._refuse(EditError("Move the playhead over a clip to split it."))
        before = self.timeline.snapshot()
        try:
            new_ids = []
            for seg in targets:
                _, right = self.timeline.split(seg.id, t)
                new_ids.append(right.id)
        except EditError as exc:
            self.timeline.restore(before)
            return self._refuse(exc)
        self._committed("Split clip", before)
        self.select(new_ids)
        return True

    def delete_selected(self, ripple: bool = False) -> bool:
        if not self._selection:
            return self._refuse(EditError("Select a clip to delete."))
        before = self.timeline.snapshot()
        try:
            self.timeline.delete(self._selection, ripple=ripple)
        except EditError as exc:
            return self._refuse(exc)
        self._committed("Ripple delete" if ripple else "Delete clip", before)
        return True

    def reorder(self, segment_id: str, new_index: int) -> bool:
        before = self.timeline.snapshot()
        try:
            self.timeline.reorder(segment_id, new_index)
        except EditError:
            return False                      # dropped where it already was: not an error
        self._committed("Move clip", before)
        return True

    def move_clip(self, segment_id: str, new_start: float) -> bool:
        """Slide a clip to ``new_start`` (clamped between its neighbours). One undo step."""
        before = self.timeline.snapshot()
        try:
            self.timeline.move_to(segment_id, new_start)
        except EditError:
            return False                      # dropped where it already was: not an error
        self._committed("Move clip", before)
        return True

    def set_muted(self, segment_ids: Iterable[str], muted: bool) -> bool:
        before = self.timeline.snapshot()
        try:
            self.timeline.set_muted(segment_ids, muted)
        except EditError as exc:
            return self._refuse(exc)
        if before == self.timeline.snapshot():
            return False                      # nothing changed: no undo step, not dirty
        self._committed("Mute clip" if muted else "Unmute clip", before)
        return True

    def toggle_mute_selected(self) -> bool:
        segs = self.selected_segments()
        if not segs:
            return self._refuse(EditError("Select a clip first."))
        return self.set_muted([s.id for s in segs], not all(s.muted for s in segs))

    def detach_audio(self) -> bool:
        """Split the audio of the selected video clips (or the clip under the playhead)
        onto the audio lane. One undo step; the new audio clips become the selection."""
        ids = [s.id for s in self.selected_segments() if not s.is_audio]
        if not ids:
            under = self.timeline.segment_at(self.playhead)
            ids = [under.id] if under else []
        if not ids:
            return self._refuse(EditError("Select a video clip to detach its audio."))
        before = self.timeline.snapshot()
        try:
            made = self.timeline.detach_audio(ids)
        except EditError as exc:
            return self._refuse(exc)
        self._committed("Detach audio", before)
        self.select([a.id for a in made])
        return True

    def close_gaps(self) -> bool:
        before = self.timeline.snapshot()
        try:
            self.timeline.close_gaps()
        except EditError as exc:
            return self._refuse(exc)
        self._committed("Close gaps", before)
        return True

    # interactive drags: many live updates, one undo step ------------------------
    def begin_drag(self) -> None:
        """Call on mouse-press before live ``trim_start``/``trim_end`` updates."""
        self._drag_snapshot = self.timeline.snapshot()

    def trim_start(self, segment_id: str, new_start: float) -> float:
        """Live-trim a clip's left edge; returns the time actually applied."""
        return self._live_trim(self.timeline.trim_start, segment_id, new_start)

    def trim_end(self, segment_id: str, new_end: float) -> float:
        """Live-trim a clip's right edge; returns the time actually applied."""
        return self._live_trim(self.timeline.trim_end, segment_id, new_end)

    def _live_trim(self, op, segment_id: str, t: float) -> float:
        implicit = self._drag_snapshot is None
        if implicit:
            self.begin_drag()
        try:
            applied = op(segment_id, t)
        except EditError as exc:
            if implicit:
                self._drag_snapshot = None
            self._refuse(exc)
            try:                              # nothing was applied: report the real edge
                seg = self.timeline.get(segment_id)
            except EditError:
                return t
            return seg.timeline_start if op == self.timeline.trim_start else seg.timeline_end
        if implicit:
            self.end_drag("Trim clip")
        else:
            self.timelineChanged.emit()
        return applied

    def end_drag(self, label: str = "Trim clip") -> bool:
        """Finish a drag: records a single undo step if anything actually changed."""
        before, self._drag_snapshot = self._drag_snapshot, None
        if before is None or before == self.timeline.snapshot():
            return False
        self._committed(label, before)
        return True

    def cancel_drag(self) -> None:
        """Abort a drag (Esc): restore the pre-drag timeline without touching history."""
        before, self._drag_snapshot = self._drag_snapshot, None
        if before is not None:
            self.timeline.restore(before)
            self.timelineChanged.emit()

    # ---------------------------------------------------------------- history
    def can_undo(self) -> bool:
        return self.history.can_undo()

    def can_redo(self) -> bool:
        return self.history.can_redo()

    def undo(self) -> bool:
        if not self.history.can_undo():
            return False
        entry = self.history.undo(self.timeline.snapshot())
        self.timeline.restore(entry.segments)
        self._after_change()
        self.message.emit(f"Undo: {entry.label}")
        return True

    def redo(self) -> bool:
        if not self.history.can_redo():
            return False
        entry = self.history.redo(self.timeline.snapshot())
        self.timeline.restore(entry.segments)
        self._after_change()
        self.message.emit(f"Redo: {entry.label}")
        return True
