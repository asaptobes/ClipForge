"""Snapshot-based undo/redo history for the timeline.

Segments are tiny value objects, so each history entry simply stores the full
segment list (and the number of video tracks) as it was *before* the edit. That makes undo/redo exact for every
operation, including ones added later, with no per-operation inverse logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import MAX_VIDEO_TRACKS, Segment, TimelineSnapshot


def _copy(snapshot: list[Segment]) -> TimelineSnapshot:
    """Deep copy that keeps the track count of a ``TimelineSnapshot`` (a plain list of
    clips gets as many tracks as its highest layer needs)."""
    return TimelineSnapshot((s.copy() for s in snapshot), getattr(snapshot, "track_count", 1),
                            getattr(snapshot, "hidden", ()))


@dataclass
class HistoryEntry:
    label: str
    segments: TimelineSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.segments, TimelineSnapshot):
            self.segments = TimelineSnapshot(self.segments)

    def to_dict(self) -> dict:
        return {"label": self.label, "tracks": self.segments.track_count,
                "hidden": list(self.segments.hidden),
                "segments": [s.to_dict() for s in self.segments]}

    @classmethod
    def from_dict(cls, d: dict) -> "HistoryEntry":
        try:
            tracks = min(max(1, int(d.get("tracks", 1))), MAX_VIDEO_TRACKS)
        except (TypeError, ValueError, OverflowError):
            tracks = 1
        try:
            hidden = [int(i) for i in d.get("hidden", [])]
        except (TypeError, ValueError, OverflowError):
            hidden = []
        return cls(d.get("label", "Edit"),
                   TimelineSnapshot((Segment.from_dict(s) for s in d.get("segments", [])), tracks,
                                    hidden))


class History:
    def __init__(self, limit: int = 200) -> None:
        self.limit = limit
        self.undo_stack: list[HistoryEntry] = []
        self.redo_stack: list[HistoryEntry] = []

    def record(self, label: str, before: list[Segment]) -> None:
        """Register a completed edit; ``before`` is the pre-edit snapshot."""
        self.undo_stack.append(HistoryEntry(label, _copy(before)))
        excess = len(self.undo_stack) - max(0, self.limit)
        if excess > 0:                      # (a plain [:-limit] slice breaks for limit == 0)
            del self.undo_stack[:excess]
        self.redo_stack.clear()

    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    def undo_label(self) -> str:
        return self.undo_stack[-1].label if self.undo_stack else ""

    def redo_label(self) -> str:
        return self.redo_stack[-1].label if self.redo_stack else ""

    def undo(self, current: list[Segment]) -> HistoryEntry:
        """Pop the last edit. ``current`` is saved so it can be redone."""
        entry = self.undo_stack.pop()
        self.redo_stack.append(HistoryEntry(entry.label, _copy(current)))
        return entry

    def redo(self, current: list[Segment]) -> HistoryEntry:
        entry = self.redo_stack.pop()
        self.undo_stack.append(HistoryEntry(entry.label, _copy(current)))
        return entry

    def clear(self) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()

    def to_dict(self, max_entries: int = 50) -> dict:
        return {
            "undo": [e.to_dict() for e in self.undo_stack[len(self.undo_stack) - max(0, max_entries):]
                     ] if max_entries > 0 else [],
            "redo": [e.to_dict() for e in self.redo_stack[len(self.redo_stack) - max(0, max_entries):]
                     ] if max_entries > 0 else [],
        }

    def load_dict(self, d: dict) -> None:
        self.undo_stack = [HistoryEntry.from_dict(e) for e in (d or {}).get("undo", [])]
        self.redo_stack = [HistoryEntry.from_dict(e) for e in (d or {}).get("redo", [])]
