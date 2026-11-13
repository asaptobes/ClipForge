"""Non-destructive editing model.

Two time domains are kept strictly apart:

* **source time**  - seconds inside an original media file (``source_in`` / ``source_out``)
* **timeline time** - seconds on the edited timeline (``timeline_start`` / ``timeline_end``)

A :class:`Timeline` holds any number of video tracks (V1 is the base, higher tracks are
composited on top) plus one audio lane. Each lane is a list of non-overlapping
:class:`Segment` objects kept sorted by ``timeline_start``; clips on different lanes may
overlap in time. Gaps are allowed (a plain delete leaves one, a ripple delete closes it).
Nothing here touches media files, so every operation is cheap, deterministic and
unit-testable. The public contract is written down in ``docs/multitrack-contract.md``.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Optional

EPS = 1e-6
#: Shortest segment an edit may produce when the source frame rate is unknown.
MIN_SEGMENT_DURATION = 0.04
#: Most video tracks a timeline may hold (V1 plus overlays).
MAX_VIDEO_TRACKS = 8
#: Limits of an overlay clip's transform.
SCALE_MIN = 0.05
SCALE_MAX = 4.0
POS_LIMIT = 1.0


class EditError(ValueError):
    """An edit operation was not applicable (nothing was changed)."""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _unit(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, 0.0), 1.0) if v == v else default


def _clamped(value, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi) if v == v else default


def _layer(value) -> int:
    try:
        return min(max(0, int(value)), MAX_VIDEO_TRACKS - 1)
    except (TypeError, ValueError, OverflowError):
        return 0


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
    #: Horizontal crop position for vertical/square reframing: 0 = left edge of the source
    #: frame, 0.5 = centre, 1 = right edge. If ``crop_x_end`` is set the crop pans linearly
    #: from ``crop_x`` (clip start) to ``crop_x_end`` (clip end).
    crop_x: float = 0.5
    crop_x_end: Optional[float] = None
    #: Which kind of lane the clip lives on: "video" (picture + its own sound) or "audio"
    #: (a detached sound-only clip). Both kinds use the same fields and rules.
    track: str = "video"
    #: Video track index: 0 = V1 (the base track), 1 = V2 ... Higher layers are drawn on
    #: top of lower ones. Always 0 for audio clips.
    layer: int = 0
    #: Overlay transform, used on layers >= 1 only. ``pos_x`` / ``pos_y`` are the offset of
    #: the clip centre from the canvas centre as fractions of the canvas width / height
    #: (+x right, +y down); ``scale`` is the rendered width as a fraction of the canvas
    #: width; ``opacity`` runs from 0 (invisible) to 1 (opaque).
    pos_x: float = 0.0
    pos_y: float = 0.0
    scale: float = 1.0
    opacity: float = 1.0

    @property
    def is_audio(self) -> bool:
        return self.track == "audio"

    @property
    def is_overlay(self) -> bool:
        """True for a video clip above the base track."""
        return self.track != "audio" and self.layer > 0

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

    def crop_x_at(self, source_time: float) -> float:
        """Crop position at a source time inside this clip (linear pan, clamped to 0..1)."""
        if self.crop_x_end is None or self.duration <= 0:
            return min(max(self.crop_x, 0.0), 1.0)
        f = min(max((source_time - self.source_in) / self.duration, 0.0), 1.0)
        return min(max(self.crop_x + (self.crop_x_end - self.crop_x) * f, 0.0), 1.0)

    def to_timeline(self, source_time: float) -> float:
        return self.timeline_start + (source_time - self.source_in)

    def copy(self) -> "Segment":
        return replace(self)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        is_audio = d.get("track") == "audio"
        return cls(
            id=d["id"],
            source_id=d["source_id"],
            source_in=float(d["source_in"]),
            source_out=float(d["source_out"]),
            timeline_start=float(d["timeline_start"]),
            muted=bool(d.get("muted", False)),
            track="audio" if is_audio else "video",
            crop_x=_unit(d.get("crop_x", 0.5), 0.5),
            crop_x_end=None if d.get("crop_x_end") is None else _unit(d.get("crop_x_end"), 0.5),
            layer=0 if is_audio else _layer(d.get("layer", 0)),
            pos_x=_clamped(d.get("pos_x", 0.0), 0.0, -POS_LIMIT, POS_LIMIT),
            pos_y=_clamped(d.get("pos_y", 0.0), 0.0, -POS_LIMIT, POS_LIMIT),
            scale=_clamped(d.get("scale", 1.0), 1.0, SCALE_MIN, SCALE_MAX),
            opacity=_unit(d.get("opacity", 1.0), 1.0),
        )


class TimelineSnapshot(list):
    """Copies of every clip plus the number of video tracks (so empty tracks survive undo).

    It is a plain ``list`` of :class:`Segment` for everything that only reads clips.
    """

    def __init__(self, segments: Iterable[Segment] = (), track_count: int = 1) -> None:
        super().__init__(segments)
        needed = max((s.layer for s in self if not s.is_audio), default=0) + 1
        self.track_count: int = max(1, int(track_count), needed)

    def __eq__(self, other) -> bool:
        if isinstance(other, TimelineSnapshot) and other.track_count != self.track_count:
            return False
        return list.__eq__(self, other)

    def __ne__(self, other) -> bool:
        return not self.__eq__(other)

    __hash__ = None  # type: ignore[assignment]


def _overlaps(a_start: float, a_end: float, b: Segment) -> bool:
    return a_start < b.timeline_end - EPS and a_end > b.timeline_start + EPS


class Timeline:
    """Video tracks V1..Vn plus one audio lane, and the edit operations on them.

    ``video_tracks[0]`` (also reachable as ``segments``) is the base track; higher tracks
    are overlays composited on top. Every mutating method either applies completely or
    raises :class:`EditError` leaving the timeline untouched.
    """

    def __init__(self, sources: Optional[dict[str, MediaSource]] = None,
                 segments: Optional[Iterable[Segment]] = None, track_count: int = 1) -> None:
        self.sources: dict[str, MediaSource] = sources if sources is not None else {}
        self.video_tracks: list[list[Segment]] = [[]]
        self.audio: list[Segment] = []                                             # audio lane
        self._distribute([s.copy() for s in (segments or [])], track_count)

    # ------------------------------------------------------------------- lanes
    def _distribute(self, everything: list[Segment], track_count: int) -> None:
        videos = [s for s in everything if not s.is_audio]
        count = max(1, int(track_count), max((s.layer for s in videos), default=0) + 1)
        self.video_tracks = [[] for _ in range(count)]
        for s in videos:
            self.video_tracks[max(0, s.layer)].append(s)
        self.audio = [s for s in everything if s.is_audio]
        self._sort()

    def _sort(self) -> None:
        for i, lane in enumerate(self.video_tracks):
            lane.sort(key=lambda s: s.timeline_start)
            for s in lane:
                s.layer = i
        self.audio.sort(key=lambda s: s.timeline_start)
        for s in self.audio:
            s.layer = 0

    @property
    def segments(self) -> list[Segment]:
        """The base video track V1."""
        return self.video_tracks[0]

    @segments.setter
    def segments(self, value: list[Segment]) -> None:
        self.video_tracks[0] = value

    @property
    def tracks(self) -> list[list[Segment]]:
        """Video tracks bottom-to-top (index == ``Segment.layer``)."""
        return self.video_tracks

    @property
    def track_count(self) -> int:
        return len(self.video_tracks)

    def track(self, index: int) -> list[Segment]:
        """Clips of one video track."""
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(self.video_tracks):
            raise EditError(f"No such video track: {index}")
        return self.video_tracks[index]

    def add_track(self, index: Optional[int] = None) -> int:
        """Insert an empty video track (default: on top). Returns its index."""
        count = len(self.video_tracks)
        if index is None:
            index = count
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= count:
            raise EditError("A track can only be added above the base track.")
        if count >= MAX_VIDEO_TRACKS:
            raise EditError(f"A project can have at most {MAX_VIDEO_TRACKS} video tracks.")
        self.video_tracks.insert(index, [])
        self._sort()
        return index

    def remove_track(self, index: int, move_to: Optional[int] = None,
                     delete_clips: bool = False) -> None:
        """Remove a video track. Its clips are moved to ``move_to`` (same times, must not
        overlap there), deleted with ``delete_clips``, or else the track must be empty."""
        lane = self.track(index)
        if index == 0:
            raise EditError("The base track cannot be removed.")
        if lane and move_to is not None:
            target = self.track(move_to)
            if move_to == index:
                raise EditError("Choose a different track to move the clips to.")
            if any(_overlaps(s.timeline_start, s.timeline_end, o) for s in lane for o in target):
                raise EditError("The clips do not fit on that track.")
            target.extend(lane)
        elif lane and not delete_clips:
            raise EditError("The track still has clips. Delete or move them first.")
        del self.video_tracks[index]
        self._sort()

    # ------------------------------------------------------------------ queries
    @property
    def duration(self) -> float:
        """End of the last clip on any lane."""
        return max((s.timeline_end for s in self.all_segments()), default=0.0)

    @property
    def video_duration(self) -> float:
        return max((s.timeline_end for s in self.video_segments()), default=0.0)

    def is_empty(self) -> bool:
        return not self.audio and not any(self.video_tracks)

    def video_segments(self) -> list[Segment]:
        """Every video clip: V1, then V2 ... (bottom-to-top)."""
        return [s for lane in self.video_tracks for s in lane]

    def all_segments(self) -> list[Segment]:
        """Video tracks bottom-to-top followed by the audio lane."""
        return self.video_segments() + self.audio

    def lane_of(self, segment_id: str) -> list[Segment]:
        """The lane list (a video track or ``audio``) holding this clip."""
        for lane in (*self.video_tracks, self.audio):
            if any(s.id == segment_id for s in lane):
                return lane
        raise EditError(f"No such segment: {segment_id}")

    def get(self, segment_id: str) -> Segment:
        for s in self.all_segments():
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

    def segment_at(self, t: float, track: int = 0) -> Optional[Segment]:
        """Clip under timeline time ``t`` on one video track (default V1), else ``None``."""
        for s in self.track(track):
            if s.contains(t):
                return s
        return None

    def segments_at(self, t: float) -> list[Segment]:
        """Video clips under ``t`` on every track, bottom-to-top (compositing order)."""
        out = []
        for lane in self.video_tracks:
            for s in lane:
                if s.contains(t):
                    out.append(s)
                    break
        return out

    def next_segment_after(self, t: float, track: int = 0) -> Optional[Segment]:
        """First segment starting at or after ``t`` (used to play across gaps)."""
        for s in self.track(track):
            if s.timeline_start >= t - EPS:
                return s
        return None

    def next_start_after(self, t: float) -> Optional[float]:
        """Earliest clip start later than ``t`` on any lane (``None`` if there is none)."""
        return min((s.timeline_start for s in self.all_segments() if s.timeline_start > t + EPS),
                   default=None)

    def to_source(self, t: float) -> Optional[tuple[Segment, float]]:
        """Map timeline time to ``(segment, source_time)`` on V1; ``None`` inside a gap."""
        s = self.segment_at(t)
        return (s, s.to_source(t)) if s else None

    def to_timeline(self, segment_id: str, source_time: float) -> float:
        return self.get(segment_id).to_timeline(source_time)

    def gaps(self, track: int = 0) -> list[tuple[float, float]]:
        """``(start, end)`` of every empty stretch of a video track, including a leading one."""
        out, cursor = [], 0.0
        for s in self.track(track):
            if s.timeline_start - cursor > EPS:
                out.append((cursor, s.timeline_start))
            cursor = s.timeline_end
        return out

    def fit_start(self, track: int, start: float, duration: float,
                  exclude: Iterable[str] = ()) -> float:
        """Start time nearest to ``start`` where a clip of ``duration`` fits on a video
        track without overlapping (``start`` itself when that slot is free)."""
        ex = set(exclude)
        others = [s for s in self.track(track) if s.id not in ex]
        start = max(0.0, float(start))

        def free(c: float) -> bool:
            return not any(_overlaps(c, c + duration, o) for o in others)

        if free(start):
            return start
        candidates = [0.0]
        for o in others:
            candidates.append(o.timeline_end)
            if o.timeline_start - duration >= 0.0:
                candidates.append(o.timeline_start - duration)
        fitting = [c for c in candidates if free(c)]      # never empty: after the last clip
        return min(fitting, key=lambda c: (abs(c - start), -c))

    def snapshot(self) -> TimelineSnapshot:
        """Copies of every clip on all lanes plus the number of video tracks."""
        return TimelineSnapshot((s.copy() for s in self.all_segments()), len(self.video_tracks))

    def restore(self, snapshot: Iterable[Segment]) -> None:
        count = getattr(snapshot, "track_count", 1)
        self._distribute([s.copy() for s in snapshot], count)

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
        """Clip edges on every lane (and 0) that dragged edges/clips may snap to."""
        ex = set(exclude)
        pts = {0.0}
        for s in self.all_segments():
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
        """Add a whole source as a new segment on V1 at the end of the timeline."""
        self.sources[source.id] = source
        seg = Segment(new_id(), source.id, 0.0, source.duration, self.duration)
        self.segments.append(seg)
        return seg

    def place_source(self, source: MediaSource, track: int, start: float) -> Segment:
        """Put a whole source on a video track at ``start`` (or the nearest free slot)."""
        lane = self.track(track)
        at = self.fit_start(track, start, source.duration)
        self.sources[source.id] = source
        seg = Segment(new_id(), source.id, 0.0, source.duration, at, layer=track)
        lane.append(seg)
        self._sort()
        return seg

    def split(self, segment_id: str, t: float) -> tuple[Segment, Segment]:
        """Split a segment at timeline time ``t`` (snapped to a source frame)."""
        seg = self.get(segment_id)
        cut = self.quantize_source_time(seg, seg.to_source(t))
        min_d = self._frame(seg) - EPS
        if cut - seg.source_in < min_d or seg.source_out - cut < min_d:
            raise EditError("The playhead must be inside the clip to split it.")
        mid = seg.crop_x_at(cut)
        right = replace(seg, id=new_id(), source_in=cut, timeline_start=seg.to_timeline(cut),
                        crop_x=mid if seg.crop_x_end is not None else seg.crop_x)
        if seg.crop_x_end is not None:
            seg.crop_x_end = mid
        seg.source_out = cut
        self.lane_of(seg.id).insert(self.index_of(seg.id) + 1, right)
        return seg, right

    def delete(self, segment_ids: Iterable[str], ripple: bool = False) -> None:
        """Remove segments. With ``ripple`` later segments on the same lane move left."""
        ids = set(segment_ids)
        if not ids or not ids.issubset({s.id for s in self.all_segments()}):
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

        self.video_tracks = [prune(lane) for lane in self.video_tracks]
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
        """Move a clip to position ``new_index`` in the order of its own lane.

        The clip is lifted out (its old slot closes) and dropped in front of the
        clip currently at ``new_index``; everything from there on shifts right by
        the moved clip's duration. Gaps elsewhere on the lane are preserved.
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

    def move_to(self, segment_id: str, new_start: float, track: Optional[int] = None) -> float:
        """Move a clip to start at ``new_start``.

        Without ``track`` the clip slides along its own lane without changing its order:
        it is clamped between the end of the previous clip (or 0) and the start of the
        next one. With ``track`` it is placed freely on that video track (which may be the
        one it is on) at ``new_start`` or, if that would overlap, the nearest free slot.
        Returns the applied start; raises ``EditError`` if nothing would change."""
        seg = self.get(segment_id)
        lane = self.lane_of(segment_id)
        if track is not None:
            if seg.is_audio:
                raise EditError("Audio clips stay on the audio lane.")
            target = self.track(track)
            applied = self.fit_start(track, new_start, seg.duration, exclude=[seg.id])
            if target is lane and abs(applied - seg.timeline_start) < EPS:
                raise EditError("The clip is already there.")
            lane[:] = [s for s in lane if s.id != seg.id]
            seg.timeline_start = applied
            target.append(seg)
            self._sort()
            return applied
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
        for s in self.all_segments():
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
        videos = [s for s in self.video_segments() if s.id in ids]
        if not videos:
            raise EditError("Select a video clip to detach its audio.")
        todo: list[Segment] = []
        for v in videos:
            src = self.sources.get(v.source_id)
            if src is not None and not src.has_audio:
                continue
            if any(_overlaps(v.timeline_start, v.timeline_end, a) for a in self.audio + todo):
                continue                         # something already sits on the audio lane there
            todo.append(v)
        if not todo:
            raise EditError("Nothing to detach: the clip has no audio, or its audio is already detached.")
        made = []
        for v in todo:
            made.append(Segment(new_id(), v.source_id, v.source_in, v.source_out,
                                v.timeline_start, False, track="audio"))
            v.muted = True
        self.audio.extend(made)
        self._sort()
        return made

    def set_crop(self, segment_ids: Iterable[str], x: float, x_end: Optional[float] = None) -> None:
        """Set the reframe crop position (0..1) on video clips; ``x_end`` makes it pan."""
        ids = set(segment_ids)
        targets = [s for s in self.video_segments() if s.id in ids]
        if not targets:
            raise EditError("Select a video clip to reframe.")
        x = _unit(x, 0.5)
        x_end = None if x_end is None else _unit(x_end, x)
        if all(abs(s.crop_x - x) < 1e-6 and
               ((s.crop_x_end is None and x_end is None) or
                (s.crop_x_end is not None and x_end is not None and abs(s.crop_x_end - x_end) < 1e-6))
               for s in targets):
            raise EditError("The crop is already there.")
        for s in targets:
            s.crop_x, s.crop_x_end = x, x_end

    def set_transform(self, segment_ids: Iterable[str], x: Optional[float] = None,
                      y: Optional[float] = None, scale: Optional[float] = None,
                      opacity: Optional[float] = None) -> None:
        """Set position / scale / opacity of overlay clips (``None`` keeps a value)."""
        ids = set(segment_ids)
        targets = [s for s in self.video_segments() if s.id in ids and s.layer > 0]
        if not targets:
            raise EditError("Select a clip on an overlay track to transform it.")
        wanted = []
        for s in targets:
            wanted.append((
                s.pos_x if x is None else _clamped(x, s.pos_x, -POS_LIMIT, POS_LIMIT),
                s.pos_y if y is None else _clamped(y, s.pos_y, -POS_LIMIT, POS_LIMIT),
                s.scale if scale is None else _clamped(scale, s.scale, SCALE_MIN, SCALE_MAX),
                s.opacity if opacity is None else _unit(opacity, s.opacity),
            ))
        if all(max(abs(a - b) for a, b in zip(w, (s.pos_x, s.pos_y, s.scale, s.opacity))) < 1e-9
               for s, w in zip(targets, wanted)):
            raise EditError("The clip already looks like that.")
        for s, w in zip(targets, wanted):
            s.pos_x, s.pos_y, s.scale, s.opacity = w

    def close_gaps(self, track: int = 0) -> None:
        """Pack all segments of one video track left so it has no gaps."""
        if not self.gaps(track):
            raise EditError("There are no gaps to close.")
        cursor = 0.0
        for s in self.track(track):
            s.timeline_start = cursor
            cursor = s.timeline_end

    def validate(self) -> None:
        """Raise ``EditError`` if the invariants (order, no overlap, bounds) are broken."""
        if not self.video_tracks:
            raise EditError("The timeline has no base track")
        for index, lane in enumerate(self.video_tracks):
            prev_end = 0.0
            for s in lane:
                if s.is_audio or s.layer != index:
                    raise EditError(f"Segment {s.id} is on the wrong track")
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
    #: Output canvas shape. "source" keeps each source's own shape; otherwise the canvas is
    #: this aspect ratio, with ``resolution`` (short side) setting its size.
    aspect: str = "source"         # "source" | "9:16" | "4:5" | "1:1" | "16:9"
    #: How a clip that is not the canvas shape is placed on it:
    #: "fill" = crop to fill (see Segment.crop_x), "blur" = fit over a blurred copy,
    #: "fit" = fit with black bars.
    reframe: str = "fill"          # "fill" | "blur" | "fit"
    captions: bool = True          # burn the project's captions into the export if present
    preset: str = "custom"         # name of the export preset last chosen in the dialog

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExportSettings":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})
