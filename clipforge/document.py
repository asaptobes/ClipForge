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

from dataclasses import replace

from .core.captions import CaptionLine, CaptionStyle, timeline_stamp
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
    canvasChanged = Signal()          # output aspect / reframe mode changed
    captionsChanged = Signal()        # caption lines, style or staleness changed
    tracksChanged = Signal()          # the number of video tracks changed (before timelineChanged)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.project = Project()
        self._selection: list[str] = []
        self._dirty = False
        self._drag_snapshot: Optional[list[Segment]] = None
        self._track_count = self.project.timeline.track_count

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
    def track_count(self) -> int:
        """Number of video tracks (V1 plus overlay tracks)."""
        return self.timeline.track_count

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
        self._track_count = project.timeline.track_count
        self.projectReset.emit()
        self.sourcesChanged.emit()
        self.tracksChanged.emit()
        self.timelineChanged.emit()
        self.selectionChanged.emit()
        self.historyChanged.emit()
        self.captionsChanged.emit()
        self.canvasChanged.emit()
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
    def add_source(self, source: MediaSource, to_timeline: bool = True,
                   track: Optional[int] = None, at: Optional[float] = None) -> None:
        """Add media to the library and (by default) put it on the timeline: appended to
        V1, or with ``track`` placed on that video track at ``at`` (default: the playhead)."""
        self.sources[source.id] = source
        self.sourcesChanged.emit()
        if to_timeline and track is None:
            self.append_to_timeline(source.id)
        elif not to_timeline or not self.place_on_timeline(source.id, track, at):
            self._set_dirty(True)             # in the library even if it could not be placed

    def place_on_timeline(self, source_id: str, track: int, at: Optional[float] = None) -> bool:
        """Put library media on a video track at ``at`` (default: the playhead), shifted to
        the nearest free slot if needed. ``track == track_count`` adds a new top track in
        the same undo step. The new clip becomes the selection."""
        source = self.sources[source_id]
        before = self.timeline.snapshot()
        try:
            if track == self.timeline.track_count:
                self.timeline.add_track()
            seg = self.timeline.place_source(source, track, self.playhead if at is None else at)
        except EditError as exc:
            self.timeline.restore(before)
            return self._refuse(exc)
        self._committed("Add clip", before)
        self.select([seg.id])
        return True

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
        self._emit_tracks_if_changed()
        self.timelineChanged.emit()
        self.historyChanged.emit()
        if selection_changed:
            self.selectionChanged.emit()
        if self.project.playhead > self.duration:
            self.seek(self.duration)
        else:
            self.seekRequested.emit(self.project.playhead)   # refresh the preview frame

    def _emit_tracks_if_changed(self) -> None:
        count = self.timeline.track_count
        if count != self._track_count:
            self._track_count = count
            self.tracksChanged.emit()

    def _refuse(self, exc: Exception) -> bool:
        self.message.emit(str(exc))
        return False

    def _under_playhead(self) -> Optional[Segment]:
        """The V1 clip under the playhead, else the top-most overlay clip under it."""
        stack = self.timeline.segments_at(self.playhead)
        if not stack:
            return None
        return stack[0] if stack[0].layer == 0 else stack[-1]

    # ----------------------------------------------------------------- tracks
    def add_track(self, index: Optional[int] = None) -> int:
        """Add an empty video track (default: on top). Returns its index, or -1 if refused."""
        before = self.timeline.snapshot()
        try:
            made = self.timeline.add_track(index)
        except EditError as exc:
            self._refuse(exc)
            return -1
        self._committed("Add track", before)
        return made

    def remove_track(self, index: int, delete_clips: bool = False) -> bool:
        """Remove a video track (never V1). A track with clips needs ``delete_clips``."""
        before = self.timeline.snapshot()
        try:
            self.timeline.remove_track(index, delete_clips=delete_clips)
        except EditError as exc:
            return self._refuse(exc)
        self._committed("Remove track", before)
        return True

    def split_at_playhead(self) -> bool:
        """Split the selected clip(s) under the playhead, or else the clip under it."""
        t = self.playhead
        targets = [s for s in self.selected_segments() if s.contains(t)]
        if not targets:
            under = self._under_playhead()
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

    def move_clip(self, segment_id: str, new_start: float, track: Optional[int] = None) -> bool:
        """Move a clip to ``new_start``. One undo step.

        Without ``track`` it slides on its own lane, clamped between its neighbours. With
        ``track`` it is placed on that video track (nearest free slot); ``track ==
        track_count`` creates a new top track for it in the same undo step."""
        before = self.timeline.snapshot()
        added = False
        try:
            if track is not None and track == self.timeline.track_count:
                if self.timeline.get(segment_id).is_audio:
                    raise EditError("Audio clips stay on the audio lane.")
                self.timeline.add_track()
                added = True
            self.timeline.move_to(segment_id, new_start, track)
        except EditError:
            if added:
                self.timeline.restore(before)
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
            under = self._under_playhead()
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

    def set_crop(self, segment_ids: Iterable[str], x: float, x_end: Optional[float] = None) -> bool:
        """Reframe crop position for video clips (0 left .. 1 right; ``x_end`` pans). One undo step."""
        before = self.timeline.snapshot()
        try:
            self.timeline.set_crop(segment_ids, x, x_end)
        except EditError as exc:
            return self._refuse(exc)
        self._committed("Reframe clip", before)
        return True

    def set_transform(self, segment_ids: Iterable[str], x: Optional[float] = None,
                      y: Optional[float] = None, scale: Optional[float] = None,
                      opacity: Optional[float] = None) -> bool:
        """Position (fractions of the canvas, 0 = centred), scale (fraction of the canvas
        width) and opacity of overlay clips; ``None`` keeps a value.

        Between ``begin_drag()`` and ``end_drag("Transform clip")`` it applies live with no
        undo step of its own; otherwise it is one undo step."""
        live = self._drag_snapshot is not None
        before = None if live else self.timeline.snapshot()
        try:
            self.timeline.set_transform(segment_ids, x, y, scale, opacity)
        except EditError as exc:
            return False if live else self._refuse(exc)
        if live:
            self.timelineChanged.emit()
        else:
            self._committed("Transform clip", before)
        return True

    # ---------------------------------------------------------------- canvas
    @property
    def aspect(self) -> str:
        return self.project.export.aspect

    @property
    def reframe_mode(self) -> str:
        return self.project.export.reframe

    def set_canvas(self, aspect: Optional[str] = None, reframe: Optional[str] = None) -> None:
        """Choose the output shape ("source", "9:16", "4:5", "1:1", "16:9") and how clips
        fill it ("fill", "blur", "fit"). Not an undo step; marks the project dirty."""
        new = self.project.export
        if aspect is not None:
            if aspect not in ("source", "9:16", "4:5", "1:1", "16:9"):
                raise ValueError(f"unknown aspect {aspect!r}")
            new = replace(new, aspect=aspect)
        if reframe is not None:
            if reframe not in ("fill", "blur", "fit"):
                raise ValueError(f"unknown reframe mode {reframe!r}")
            new = replace(new, reframe=reframe)
        if new != self.project.export:
            self.project.export = new
            self._set_dirty(True)
            self.canvasChanged.emit()

    # --------------------------------------------------------------- captions
    @property
    def captions(self) -> list[CaptionLine]:
        return self.project.captions

    @property
    def caption_style(self) -> CaptionStyle:
        return self.project.caption_style

    def captions_stale(self) -> bool:
        """True when captions exist but the timeline changed since they were generated."""
        p = self.project
        return bool(p.captions) and p.captions_stamp != timeline_stamp(self.timeline.all_segments())

    def set_captions(self, lines: Iterable[CaptionLine], style: Optional[CaptionStyle] = None) -> None:
        """Replace all captions (e.g. after transcription) and mark them as matching the
        timeline as it is now. Not an undo step."""
        p = self.project
        p.captions = sorted((ln.copy() for ln in lines), key=lambda ln: ln.start)
        if style is not None:
            p.caption_style = style
        p.captions_stamp = timeline_stamp(self.timeline.all_segments())
        self._set_dirty(True)
        self.captionsChanged.emit()

    def clear_captions(self) -> None:
        if self.project.captions:
            self.project.captions = []
            self.project.captions_stamp = ""
            self._set_dirty(True)
            self.captionsChanged.emit()

    def edit_caption(self, index: int, text: Optional[str] = None, start: Optional[float] = None,
                     end: Optional[float] = None) -> bool:
        """Change one caption's text or timing. Editing text drops its per-word timing
        (the whole line is then shown at once)."""
        try:
            ln = self.project.captions[index]
        except IndexError:
            return False
        if text is not None and text != ln.text:
            ln.text, ln.words = text, []
        if start is not None:
            ln.start = max(0.0, float(start))
        if end is not None:
            ln.end = float(end)
        if ln.end <= ln.start:
            ln.end = ln.start + 0.2
        self.project.captions.sort(key=lambda c: c.start)
        self._set_dirty(True)
        self.captionsChanged.emit()
        return True

    def delete_caption(self, index: int) -> bool:
        if not 0 <= index < len(self.project.captions):
            return False
        del self.project.captions[index]
        self._set_dirty(True)
        self.captionsChanged.emit()
        return True

    def set_caption_style(self, style: CaptionStyle) -> None:
        if style != self.project.caption_style:
            self.project.caption_style = style
            self._set_dirty(True)
            self.captionsChanged.emit()

    def close_gaps(self, track: int = 0) -> bool:
        before = self.timeline.snapshot()
        try:
            self.timeline.close_gaps(track)
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
            self._emit_tracks_if_changed()
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
