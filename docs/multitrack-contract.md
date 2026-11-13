# Multi-track contract (core model + Document)

Authoritative public API for layered video tracks. Export, preview and timeline UI code
against this file. Everything not listed here is unchanged.

Terms: video tracks are numbered by a 0-based **layer** index. Layer 0 is **V1**, the base
track. Layer 1 is V2, and so on; a higher layer is drawn on top of lower ones. There is still
exactly one audio lane (detached, sound-only clips).

## 1. `clipforge.core.model`

### Constants

```python
MAX_VIDEO_TRACKS = 8      # add_track() refuses beyond this
SCALE_MIN = 0.05
SCALE_MAX = 4.0
POS_LIMIT = 1.0           # pos_x / pos_y are clamped to -POS_LIMIT .. +POS_LIMIT
```

### `Segment` (dataclass) - new fields, all with defaults

```python
track: str = "video"      # UNCHANGED: kind of lane, "video" | "audio"
layer: int = 0            # NEW: video track index, 0 = V1 (base). Always 0 for audio clips.
pos_x: float = 0.0        # NEW: clip centre offset from canvas centre, fraction of canvas WIDTH  (+ = right)
pos_y: float = 0.0        # NEW: clip centre offset from canvas centre, fraction of canvas HEIGHT (+ = down)
scale: float = 1.0        # NEW: rendered clip width as a fraction of canvas width
opacity: float = 1.0      # NEW: 0.0 (invisible) .. 1.0 (opaque)
```

`Segment.is_audio`, `duration`, `timeline_end`, `contains`, `to_source`, `to_timeline`,
`crop_x_at`, `copy`, `to_dict`, `from_dict` keep their signatures. New read-only property:

```python
Segment.is_overlay -> bool      # track == "video" and layer > 0
```

`to_dict()` writes the five new keys. `from_dict()` defaults a missing/invalid key to
`layer=0, pos_x=0, pos_y=0, scale=1, opacity=1` and clamps values into range, so old projects
load as a single V1 track.

### Rendering semantics (export and preview MUST agree)

* **V1 (layer 0)** is rendered exactly as today: the existing aspect / reframe / `crop_x`
  pipeline. `pos_x/pos_y/scale/opacity` are **ignored** on layer 0 (they are kept so a clip
  moved back to an overlay track remembers its transform).
* **Overlay clips (layer >= 1)**: keep the source's display aspect ratio, no reframe crop
  (`crop_x` ignored). Rendered width `= scale * canvas_w`, height from the source aspect.
  Centre at `(canvas_w * (0.5 + pos_x), canvas_h * (0.5 + pos_y))`. Blended with `opacity`.
  Parts outside the canvas are clipped. Default (0, 0, 1, 1) = centred, canvas-wide.
* Compositing order at timeline time `t`: `Timeline.segments_at(t)` bottom-to-top.
* Where V1 has a gap, the canvas under the overlays is black.
* The canvas is defined as today (export aspect/resolution; with aspect "source", the shape
  of the V1 clips; if V1 is empty, the shape of the lowest non-empty track's first clip).
* Audio: every un-muted video clip on **any** layer contributes its own sound, mixed (summed)
  with the audio lane. `muted` silences that one clip. Output duration = `Timeline.duration`.

### `TimelineSnapshot`

```python
class TimelineSnapshot(list):       # list[Segment] plus the number of video tracks
    track_count: int
    def __init__(self, segments: Iterable[Segment] = (), track_count: int = 1) -> None
```

It is a real `list` of `Segment` copies (existing code iterating a snapshot keeps working).
Two snapshots are equal only if the clips **and** `track_count` are equal. Empty tracks
survive undo/redo because of `track_count`.

### `Timeline`

```python
Timeline(sources: Optional[dict[str, MediaSource]] = None,
         segments: Optional[Iterable[Segment]] = None,
         track_count: int = 1)
```

Clips are distributed by `Segment.track` / `Segment.layer`; the track count grows to fit the
highest layer present.

Attributes / properties:

```python
video_tracks: list[list[Segment]]   # one list per video track, index == layer, each sorted by timeline_start
tracks       -> list[list[Segment]] # property, same object as video_tracks (bottom-to-top)
track_count  -> int                 # property, len(video_tracks), always >= 1
segments     -> list[Segment]       # property (get/set): video_tracks[0], i.e. V1 ONLY  (was: the only video lane)
audio: list[Segment]                # unchanged: the audio lane
```

Invariant: for every clip in `video_tracks[i]`, `clip.layer == i` and `clip.track == "video"`.
No two clips on the same track overlap in time. Clips on different tracks may overlap freely.

Queries:

```python
duration        -> float                    # end of the last clip on ANY track or the audio lane
video_duration  -> float                    # end of the last clip on any VIDEO track
is_empty() -> bool                          # no clips on any track or the audio lane
all_segments() -> list[Segment]             # V1, V2, ..., Vn, then audio lane
video_segments() -> list[Segment]           # NEW: V1, V2, ..., Vn (no audio)
track(index: int) -> list[Segment]          # NEW: clips of one video track; EditError if out of range
lane_of(segment_id: str) -> list[Segment]   # the list (a video track or `audio`) holding the clip
get(segment_id: str) -> Segment             # any track
index_of(segment_id: str) -> int            # position inside its own lane
segment_at(t: float, track: int = 0) -> Optional[Segment]     # clip under t on ONE video track (default V1)
segments_at(t: float) -> list[Segment]      # NEW: video clips under t on all tracks, bottom-to-top (<= 1 per track)
next_segment_after(t: float, track: int = 0) -> Optional[Segment]
next_start_after(t: float) -> Optional[float]   # NEW: earliest clip start > t on any video track or audio lane
audio_at(t: float) -> Optional[Segment]     # unchanged
to_source(t: float) -> Optional[tuple[Segment, float]]        # V1 only (unchanged)
to_timeline(segment_id: str, source_time: float) -> float
gaps(track: int = 0) -> list[tuple[float, float]]             # empty stretches of one video track
snap_points(exclude: Iterable[str] = ()) -> list[float]       # 0 + clip edges on ALL tracks + audio
fit_start(track: int, start: float, duration: float,
          exclude: Iterable[str] = ()) -> float               # NEW, see below
snapshot() -> TimelineSnapshot
restore(snapshot: Iterable[Segment]) -> None    # track_count taken from a TimelineSnapshot, else derived
validate() -> None
```

`fit_start`: the start time nearest to `start` (never < 0) at which a clip of `duration`
fits on video track `track` without overlapping (clips whose id is in `exclude` are ignored).
Returns `start` itself when that slot is free; when two free slots are equally near, the
later one wins. Use it for drop previews.

Track operations (NEW):

```python
add_track(index: Optional[int] = None) -> int
remove_track(index: int, move_to: Optional[int] = None, delete_clips: bool = False) -> None
```

* `add_track`: inserts an empty video track at `index` (1..track_count; default = on top) and
  returns its index. Clips on tracks at or above `index` get `layer + 1`. `EditError` when
  `index < 1`, `index > track_count`, or `track_count == MAX_VIDEO_TRACKS`.
* `remove_track`: removes video track `index`; higher tracks shift down. V1 (index 0) can never
  be removed. If the track holds clips: with `move_to` they are moved to that track at the same
  times (`EditError` if any would overlap there); with `delete_clips=True` they are deleted;
  with neither, `EditError`. `move_to` refers to the track numbering BEFORE the removal.

Clip operations (all work on a clip's own lane; ripple never touches another track):

```python
append_source(source: MediaSource) -> Segment                 # unchanged: V1, at Timeline.duration
place_source(source: MediaSource, track: int, start: float) -> Segment   # NEW
split(segment_id: str, t: float) -> tuple[Segment, Segment]   # right half inherits layer + transform
delete(segment_ids: Iterable[str], ripple: bool = False) -> None   # ripple closes the gap per lane
trim_start(segment_id: str, new_start: float) -> float        # limited by neighbours on its own lane
trim_end(segment_id: str, new_end: float) -> float
reorder(segment_id: str, new_index: int) -> None              # within its own lane
index_for_drop(segment_id: str, drop_time: float) -> int
move_to(segment_id: str, new_start: float, track: Optional[int] = None) -> float
set_muted(segment_ids: Iterable[str], muted: bool) -> None
detach_audio(segment_ids: Iterable[str]) -> list[Segment]     # video clips of any layer -> audio lane
set_crop(segment_ids: Iterable[str], x: float, x_end: Optional[float] = None) -> None  # any video clip
set_transform(segment_ids: Iterable[str], x: Optional[float] = None, y: Optional[float] = None,
              scale: Optional[float] = None, opacity: Optional[float] = None) -> None   # NEW
close_gaps(track: int = 0) -> None                            # packs ONE video track left
```

* `place_source`: registers the source and puts the whole source on video track `track` at
  `fit_start(track, start, source.duration)`. `EditError` for a bad track.
* `move_to`:
  * `track=None` (legacy): slide on the clip's own lane, clamped between its neighbours; order
    never changes. Works for audio clips too.
  * `track=<int>` (also when it equals the clip's current layer): free placement on that video
    track at `fit_start(track, new_start, clip.duration, exclude=[clip.id])`; the clip may jump
    over other clips and sets `clip.layer = track`. `EditError` for audio clips or a bad track.
  * Returns the applied start. `EditError("The clip is already there.")` if nothing changes.
* `set_transform`: `None` leaves a value as it is; values are clamped (`pos` to +-`POS_LIMIT`,
  `scale` to `SCALE_MIN..SCALE_MAX`, `opacity` to 0..1). Applies to overlay clips
  (`layer >= 1`) among the ids; ids of V1 or audio clips are ignored. `EditError` when the ids
  contain no overlay clip, or when nothing would change.
* Every mutating method either applies completely or raises `EditError` leaving the timeline
  untouched (unchanged rule).

## 2. `clipforge.core.history`

```python
HistoryEntry.segments: TimelineSnapshot      # was list[Segment]; still a list
HistoryEntry.to_dict()  # adds "tracks": <track_count>
```

`History.record/undo/redo` signatures are unchanged; they preserve `track_count` when given a
`TimelineSnapshot` and derive it (highest layer + 1) from a plain list.

## 3. `clipforge.core.project`

* `FORMAT_VERSION = 2` (was 1). Version 1 files load unchanged as one V1 track.
* New top-level key `"video_tracks": <int>`; segments carry the new `Segment` keys.
* `"segments"` still holds every clip (all video tracks, then audio).

## 4. `clipforge.document.Document`

New signal:

```python
tracksChanged = Signal()     # the NUMBER of video tracks changed (add/remove/undo/redo/project load)
```

It is emitted just before the `timelineChanged` of the same edit. Clip moves between existing
tracks emit only `timelineChanged`. All existing signals are unchanged.

New / changed members:

```python
track_count -> int                                        # property
add_track(index: Optional[int] = None) -> int             # new track index, or -1 if refused. Undo step "Add track"
remove_track(index: int, delete_clips: bool = False) -> bool   # undo step "Remove track"
move_clip(segment_id: str, new_start: float, track: Optional[int] = None) -> bool   # CHANGED: optional track
set_transform(segment_ids: Iterable[str], x: Optional[float] = None, y: Optional[float] = None,
              scale: Optional[float] = None, opacity: Optional[float] = None) -> bool   # NEW
add_source(source: MediaSource, to_timeline: bool = True,
           track: Optional[int] = None, at: Optional[float] = None) -> None        # CHANGED: optional track/at
place_on_timeline(source_id: str, track: int, at: Optional[float] = None) -> bool  # NEW
close_gaps(track: int = 0) -> bool                                                 # CHANGED: optional track
```

* `move_clip(..., track=n)`: `Timeline.move_to` semantics. `track == track_count` is allowed
  and creates a new top track in the **same** undo step (drag a clip above the top track).
  Returns False (no undo step) when nothing changes or the move is refused.
* `set_transform`: between `begin_drag()` and `end_drag("Transform clip")` it applies live
  (emits `timelineChanged`, no undo step until `end_drag`). Outside a drag it is one undo
  step "Transform clip". Returns False (with a `message`) when refused.
* `add_source(source)` with `track=None` behaves as before (append to V1). With `track=n` the
  clip is placed on that track at `at` (default: the playhead), shifted to the nearest free
  slot if needed. `track == track_count` creates the track in the same undo step.
* `place_on_timeline`: same placement for media already in the library; the new clip becomes
  the selection. Undo step "Add clip".
* `remove_track(index)`: refuses a non-empty track unless `delete_clips=True`; never removes V1.
* `split_at_playhead()` / `detach_audio()` with no selection: act on the V1 clip under the
  playhead, or, if V1 has none there, on the top-most overlay clip under it.
* `delete_selected`, `set_muted`, `set_crop`, `trim_start`, `trim_end`, `reorder`,
  `begin_drag`, `end_drag`, `cancel_drag`, `undo`, `redo`: same signatures; they work for clips
  on any track. Undo/redo restore clips and the track count.

## 5. Behaviour changes to existing names

| Name | Before | Now |
| --- | --- | --- |
| `Timeline.segments` | the only video lane | V1 (base track) only; a property over `video_tracks[0]` |
| `Timeline.all_segments()` | video + audio | V1..Vn + audio |
| `Timeline.duration`, `snap_points`, `get`, `lane_of`, `is_empty`, `delete`, `set_muted` | both lanes | all video tracks + audio |
| `Timeline.video_duration` | the video lane | all video tracks |
| `Timeline.segment_at`, `next_segment_after`, `gaps`, `close_gaps`, `to_source` | the video lane | V1 by default (`track` argument where listed) |
| `Timeline.detach_audio`, `set_crop` | video-lane clips | video clips on any layer |
| `Timeline.snapshot()` | `list[Segment]` | `TimelineSnapshot` (a `list` subclass) |
| `Project` file | version 1 | version 2, adds `video_tracks` and per-clip `layer/pos_x/pos_y/scale/opacity` |

Code that passes `timeline.all_segments()` to something expecting a single video lane must now
group video clips by `Segment.layer` (or use `timeline.tracks`).
