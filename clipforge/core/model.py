"""Non-destructive editing model.

Two time domains are kept strictly apart:

* **source time**  - seconds inside an original media file (``source_in`` / ``source_out``)
* **timeline time** - seconds on the edited timeline (``timeline_start`` / ``timeline_end``)

A :class:`Timeline` is a single track of non-overlapping :class:`Segment` objects kept
sorted by ``timeline_start``; the list index is the segment order. Gaps are allowed
(a plain delete leaves one, a ripple delete closes it). Nothing here touches media
files, so every operation is cheap, deterministic and unit-testable.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Optional

EPS = 1e-6
#: Shortest segment an edit may produce when the source frame rate is unknown.
MIN_SEGMENT_DURATION = 0.04


class EditError(ValueError):
    """An edit operation was not applicable (nothing was changed)."""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class MediaSource:
    """An imported or downloaded media file. Never modified on disk."""

    id: str
    path: str
    duration: float
    width: int = 0            # display width  (rotation already applied)
    height: int = 0           # display height (rotation already applied)
    fps: float = 30.0
    has_video: bool = True
    has_audio: bool = True
    video_codec: str = ""
    audio_codec: str = ""
    pix_fmt: str = ""
    sample_rate: int = 0
    channels: int = 0
    rotation: int = 0
    bit_rate: int = 0
    file_size: int = 0
    title: str = ""
    source_url: Optional[str] = None
    #: Sorted keyframe timestamps (seconds). Filled lazily, only needed for Fast Export.
    keyframes: list[float] = field(default_factory=list, repr=False)

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width

    @property
    def frame_duration(self) -> float:
        return 1.0 / self.fps if self.fps > 0 else MIN_SEGMENT_DURATION

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("keyframes", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MediaSource":
        known = {f for f in cls.__dataclass_fields__}  # tolerate newer project files
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Segment:
    """A piece of one source placed on the timeline."""

    id: str
    source_id: str
    source_in: float
    source_out: float
    timeline_start: float
    muted: bool = False
    #: Which lane the clip lives on: "video" (picture + its own sound) or "audio"
    #: (a detached sound-only clip). Both lanes use the same fields and rules.
    track: str = "video"

    @property
    def is_audio(self) -> bool:
        return self.track == "audio"

    @property
    def duration(self) -> float:
        return self.source_out - self.source_in

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.duration

    def contains(self, t: float) -> bool:
        """True if timeline time ``t`` falls inside this segment (end exclusive)."""
        return self.timeline_start - EPS <= t < self.timeline_end - EPS

    def to_source(self, t: float) -> float:
        return self.source_in + (t - self.timeline_start)

    def to_timeline(self, source_time: float) -> float:
        return self.timeline_start + (source_time - self.source_in)

    def copy(self) -> "Segment":
        return replace(self)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(
            id=d["id"],
            source_id=d["source_id"],
            source_in=float(d["source_in"]),
            source_out=float(d["source_out"]),
            timeline_start=float(d["timeline_start"]),
            muted=bool(d.get("muted", False)),
            track="audio" if d.get("track") == "audio" else "video",
        )


class Timeline:
    """Ordered single-track sequence of segments plus the edit operations on it.

    Every mutating method either applies completely or raises :class:`EditError`
    leaving the timeline untouched.
    """

    def __init__(self, sources: Optional[dict[str, MediaSource]] = None,
                 segments: Optional[Iterable[Segment]] = None) -> None:
        self.sources: dict[str, MediaSource] = sources if sources is not None else {}
        everything = [s.copy() for s in (segments or [])]
        self.segments: list[Segment] = [s for s in everything if not s.is_audio]   # video lane
        self.audio: list[Segment] = [s for s in everything if s.is_audio]          # audio lane
        self._sort()

    # ------------------------------------------------------------------ queries
    def _sort(self) -> None:
        self.segments.sort(key=lambda s: s.timeline_start)
        self.audio.sort(key=lambda s: s.timeline_start)

    @property
    def duration(self) -> float:
        """End of the last clip on either lane."""
        return max((s.timeline_end for s in self.segments + self.audio), default=0.0)

    @property
    def video_duration(self) -> float:
        return max((s.timeline_end for s in self.segments), default=0.0)

    def is_empty(self) -> bool:
        return not self.segments and not self.audio

    def all_segments(self) -> list[Segment]:
        """Video lane followed by audio lane."""
        return self.segments + self.audio

    def lane_of(self, segment_id: str) -> list[Segment]:
        """The lane list (``segments`` or ``audio``) holding this clip."""
        for lane in (self.segments, self.audio):
            if any(s.id == segment_id for s in lane):
                return lane
        raise EditError(f"No such segment: {segment_id}")

    def get(self, segment_id: str) -> Segment:
        for s in self.segments + self.audio:
            if s.id == segment_id:
                return s
        raise EditError(f"No such segment: {segment_id}")

    def index_of(self, segment_id: str) -> int:
        """Position of the clip inside its own lane."""
        for i, s in enumerate(self.lane_of(segment_id)):
            if s.id == segment_id:
                return i
        raise EditError(f"No such segment: {segment_id}")

    def audio_at(self, t: float) -> Optional[Segment]:
        """Detached audio clip under timeline time ``t`` (or ``None``)."""
        for s in self.audio:
            if s.contains(t):
                return s
        return None

    def segment_at(self, t: float) -> Optional[Segment]:
        """Segment under timeline time ``t`` or ``None`` when ``t`` is in a gap."""
        for s in self.segments:
            if s.contains(t):
                return s
        return None

    def next_segment_after(self, t: float) -> Optional[Segment]:
        """First segment starting at or after ``t`` (used to play across gaps)."""
        for s in self.segments:
            if s.timeline_start >= t - EPS:
                return s
        return None

    def to_source(self, t: float) -> Optional[tuple[Segment, float]]:
        """Map timeline time to ``(segment, source_time)``; ``None`` inside a gap."""
        s = self.segment_at(t)
        return (s, s.to_source(t)) if s else None

    def to_timeline(self, segment_id: str, source_time: float) -> float:
        return self.get(segment_id).to_timeline(source_time)

    def gaps(self) -> list[tuple[float, float]]:
        """``(start, end)`` of every empty stretch, including a leading one."""
        out, cursor = [], 0.0
        for s in self.segments:
            if s.timeline_start - cursor > EPS:
                out.append((cursor, s.timeline_start))
            cursor = s.timeline_end
        return out

    def snapshot(self) -> list[Segment]:
        """Copies of every clip on both lanes (each carries its ``track``)."""
        return [s.copy() for s in self.segments + self.audio]

    def restore(self, snapshot: Iterable[Segment]) -> None:
        everything = [s.copy() for s in snapshot]
        self.segments = [s for s in everything if not s.is_audio]
        self.audio = [s for s in everything if s.is_audio]
        self._sort()

    def _frame(self, seg: Segment) -> float:
        src = self.sources.get(seg.source_id)
        return src.frame_duration if src else MIN_SEGMENT_DURATION

    def _source_duration(self, seg: Segment) -> float:
        src = self.sources.get(seg.source_id)
        return src.duration if src else float("inf")

    def quantize_source_time(self, seg: Segment, source_time: float) -> float:
        """Round a source time to the nearest frame boundary of its source."""
        src = self.sources.get(seg.source_id)
        if not src or src.fps <= 0:
            return source_time
        return round(source_time * src.fps) / src.fps

    def snap_points(self, exclude: Iterable[str] = ()) -> list[float]:
        """Clip edges (and 0) that dragged edges/clips may snap to."""
        ex = set(exclude)
        pts = {0.0}
        for s in self.segments + self.audio:
            if s.id not in ex:
                pts.add(s.timeline_start)
                pts.add(s.timeline_end)
        return sorted(pts)

    @staticmethod
    def snap(t: float, points: Iterable[float], tolerance: float) -> float:
        """Return the nearest snap point within ``tolerance`` seconds, else ``t``."""
        best, best_d = t, tolerance
        for p in points:
            d = abs(p - t)
            if d <= best_d:
                best, best_d = p, d
        return best

    # --------------------------------------------------------------- operations
    def append_source(self, source: MediaSource) -> Segment:
        """Add a whole source as a new segment at the end of the timeline."""
        self.sources[source.id] = source
        seg = Segment(new_id(), source.id, 0.0, source.duration, self.duration)
        self.segments.append(seg)
        return seg

    def split(self, segment_id: str, t: float) -> tuple[Segment, Segment]:
        """Split a segment at timeline time ``t`` (snapped to a source frame)."""
        seg = self.get(segment_id)
        cut = self.quantize_source_time(seg, seg.to_source(t))
        min_d = self._frame(seg) - EPS
        if cut - seg.source_in < min_d or seg.source_out - cut < min_d:
            raise EditError("The playhead must be inside the clip to split it.")
        right = Segment(new_id(), seg.source_id, cut, seg.source_out,
                        seg.to_timeline(cut), seg.muted, seg.track)
        seg.source_out = cut
        self.lane_of(seg.id).insert(self.index_of(seg.id) + 1, right)
        return seg, right

    def delete(self, segment_ids: Iterable[str], ripple: bool = False) -> None:
        """Remove segments. With ``ripple`` later segments move left to close each gap."""
        ids = set(segment_ids)
        if not ids or not ids.issubset({s.id for s in self.segments + self.audio}):
            raise EditError("Select a clip to delete.")

        def prune(lane: list[Segment]) -> list[Segment]:
            kept, shift = [], 0.0       # ripple closes gaps within the clip's own lane
            for s in lane:
                if s.id in ids:
                    shift += s.duration
                else:
                    if ripple:
                        s.timeline_start = max(0.0, s.timeline_start - shift)
                    kept.append(s)
            return kept

        self.segments = prune(self.segments)
        self.audio = prune(self.audio)

    def trim_start(self, segment_id: str, new_start: float) -> float:
        """Drag a clip's left edge to timeline time ``new_start``. Returns the applied time."""
        seg = self.get(segment_id)
        lane = self.lane_of(segment_id)
        i = self.index_of(segment_id)
        lo = lane[i - 1].timeline_end if i > 0 else 0.0
        lo = max(lo, seg.timeline_start - seg.source_in)          # source_in >= 0
        hi = seg.timeline_end - self._frame(seg)
        if hi < lo:
            raise EditError("The clip is too short to trim.")
        new_start = min(max(new_start, lo), hi)
        new_in = self.quantize_source_time(seg, seg.source_in + (new_start - seg.timeline_start))
        new_in = min(max(new_in, 0.0), seg.source_out - self._frame(seg))
        applied = seg.timeline_start + (new_in - seg.source_in)
        if applied < lo - EPS:                                    # quantising crossed the limit
            new_in += self._frame(seg)
            applied = seg.timeline_start + (new_in - seg.source_in)
        seg.timeline_start, seg.source_in = applied, new_in
        return applied

    def trim_end(self, segment_id: str, new_end: float) -> float:
        """Drag a clip's right edge to timeline time ``new_end``. Returns the applied time."""
        seg = self.get(segment_id)
        lane = self.lane_of(segment_id)
        i = self.index_of(segment_id)
        hi = lane[i + 1].timeline_start if i + 1 < len(lane) else float("inf")
        src_dur = self._source_duration(seg)
        src_hi = seg.timeline_start + (src_dur - seg.source_in)
        hi = min(hi, src_hi)
        lo = seg.timeline_start + self._frame(seg)
        if hi < lo:
            raise EditError("The clip is too short to trim.")
        new_end = min(max(new_end, lo), hi)
        if src_dur != float("inf") and new_end >= src_hi - EPS:
            # Dragged out to the very end of the media: use the exact source duration
            # (often not a multiple of the frame time) so a trim can be fully undone
            # by dragging back, instead of losing the sub-frame tail to quantisation.
            new_out = src_dur
        else:
            new_out = self.quantize_source_time(seg, seg.source_in + (new_end - seg.timeline_start))
            new_out = max(new_out, seg.source_in + self._frame(seg))
            if seg.timeline_start + (new_out - seg.source_in) > hi + EPS:
                new_out -= self._frame(seg)
            new_out = min(new_out, src_dur)
        seg.source_out = new_out
        return seg.timeline_end

    def reorder(self, segment_id: str, new_index: int) -> None:
        """Move a clip to position ``new_index`` in the segment order.

        The clip is lifted out (its old slot closes) and dropped in front of the
        clip currently at ``new_index``; everything from there on shifts right by
        the moved clip's duration. Gaps elsewhere on the timeline are preserved.
        """
        seg = self.get(segment_id)
        lane = self.lane_of(segment_id)
        old_index = self.index_of(segment_id)
        new_index = max(0, min(new_index, len(lane) - 1))
        if new_index == old_index:
            raise EditError("The clip is already in that position.")
        dur = seg.duration
        rest = [s for s in lane if s.id != segment_id]
        for s in rest[old_index:]:                               # close the old slot
            s.timeline_start = max(0.0, s.timeline_start - dur)
        if new_index < len(rest):
            seg.timeline_start = rest[new_index].timeline_start
            for s in rest[new_index:]:
                s.timeline_start += dur
        else:
            seg.timeline_start = rest[-1].timeline_end if rest else 0.0
        rest.insert(new_index, seg)
        lane[:] = rest

    def move_to(self, segment_id: str, new_start: float) -> float:
        """Slide a clip along the timeline to start at ``new_start`` without changing its
        order: it is clamped between the end of the previous clip (or 0) and the start of
        the next one, so it can be pulled left into a gap or pushed right into free space.
        Returns the applied start; raises ``EditError`` if nothing would change."""
        seg = self.get(segment_id)
        lane = self.lane_of(segment_id)
        i = self.index_of(segment_id)
        lo = lane[i - 1].timeline_end if i > 0 else 0.0
        hi = (lane[i + 1].timeline_start - seg.duration
              if i + 1 < len(lane) else float("inf"))
        new_start = min(max(new_start, lo), max(lo, hi))
        if abs(new_start - seg.timeline_start) < EPS:
            raise EditError("The clip is already there.")
        seg.timeline_start = new_start
        return new_start

    def index_for_drop(self, segment_id: str, drop_time: float) -> int:
        """Order index a dragged clip should take when its centre is at ``drop_time``."""
        idx = 0
        for s in self.lane_of(segment_id):
            if s.id == segment_id:
                continue
            if drop_time > s.timeline_start + s.duration / 2:
                idx += 1
        return idx

    def set_muted(self, segment_ids: Iterable[str], muted: bool) -> None:
        ids = set(segment_ids)
        if not ids:
            raise EditError("Select a clip first.")
        for s in self.segments + self.audio:
            if s.id in ids:
                s.muted = muted

    def detach_audio(self, segment_ids: Iterable[str]) -> list[Segment]:
        """Split the sound of video clips onto the audio lane.

        Each chosen video clip gets a matching audio clip (same source range and
        timeline position) and is muted, so the sound is heard once but can now be
        moved, trimmed, split, muted or deleted independently of the picture.
        Returns the new audio clips.
        """
        ids = set(segment_ids)
        videos = [s for s in self.segments if s.id in ids]
        if not videos:
            raise EditError("Select a video clip to detach its audio.")
        todo = []
        for v in videos:
            src = self.sources.get(v.source_id)
            if src is not None and not src.has_audio:
                continue
            if any(a.timeline_start < v.timeline_end - EPS and a.timeline_end > v.timeline_start + EPS
                   for a in self.audio):
                continue                         # something already sits on the audio lane there
            todo.append(v)
        if not todo:
            raise EditError("Nothing to detach: the clip has no audio, or its audio is already detached.")
        made = []
        for v in todo:
            made.append(Segment(new_id(), v.source_id, v.source_in, v.source_out,
                                v.timeline_start, False, "audio"))
            v.muted = True
        self.audio.extend(made)
        self._sort()
        return made

    def close_gaps(self) -> None:
        """Pack all segments left so the timeline has no gaps."""
        if not self.gaps():
            raise EditError("There are no gaps to close.")
        cursor = 0.0
        for s in self.segments:
            s.timeline_start = cursor
            cursor = s.timeline_end

    def validate(self) -> None:
        """Raise ``EditError`` if the invariants (order, no overlap, bounds) are broken."""
        prev_end = 0.0
        for s in self.segments:
            if s.duration <= 0:
                raise EditError(f"Segment {s.id} has no duration")
            if s.source_in < -EPS:
                raise EditError(f"Segment {s.id} starts before its source")
            if s.source_out > self._source_duration(s) + 1e-3:
                raise EditError(f"Segment {s.id} runs past the end of its source")
            if s.timeline_start < prev_end - 1e-4:
                raise EditError(f"Segment {s.id} overlaps the previous clip")
            prev_end = s.timeline_end
        prev_end = 0.0
        for s in self.audio:
            if s.duration <= 0:
                raise EditError(f"Audio clip {s.id} has no duration")
            if s.source_in < -EPS or s.source_out > self._source_duration(s) + 1e-3:
                raise EditError(f"Audio clip {s.id} is outside its source")
            if s.timeline_start < prev_end - 1e-4:
                raise EditError(f"Audio clip {s.id} overlaps the previous audio clip")
            prev_end = s.timeline_end


@dataclass
class ExportSettings:
    """User choices in the export dialog. Persisted inside the project file."""

    filename: str = "export"
    folder: str = ""
    container: str = "mp4"
    codec: str = "h264"            # "h264" | "h265"
    resolution: str = "source"     # "source" or short-side pixels: "2160","1440","1080","720","480"
    fps: str = "source"            # "source" or "24","25","30","50","60"
    quality: str = "high"          # "high" | "medium" | "low"
    audio: bool = True
    hw_accel: str = "auto"         # "auto" | "on" | "off"
    mode: str = "auto"             # "auto" | "fast" | "accurate"
    scope: str = "timeline"        # "timeline" | "selection"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExportSettings":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})
