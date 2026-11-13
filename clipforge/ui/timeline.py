"""Timeline panel: tool row + custom-painted multi-track timeline + scroll bars.

Everything is read from and edited through :class:`clipforge.document.Document`;
this module never touches the model directly.

Interaction rules (kept deliberately predictable)
-------------------------------------------------
* **Ruler** (top strip) and the **playhead handle**: click seeks, drag scrubs.
  Scrub seeks are throttled to one per ``SCRUB_INTERVAL_MS`` and the final position
  is always sent on release.
* **Clip body**: click *selects only* (it does not move the playhead);
  Ctrl+click toggles the clip in the selection. Dragging the body further than
  ``DRAG_THRESHOLD`` px lifts the clip for reordering (ghost + insertion marker),
  dropping calls ``Document.reorder``. Double-click zooms to the clip.
* **Clip edges** (``EDGE_GRAB`` px): drag to trim, live, as one undo step.
* **Empty track area**: click clears the selection and seeks there (drag scrubs).
* **Esc** while dragging cancels the drag (trim restored, reorder dropped,
  scrub returns to where it started). No other key is consumed here: the main
  window owns every shortcut.
* **Snapping** (magnet button; hold Alt to bypass): trimmed edges snap to other
  clip edges and the playhead, the scrubbed playhead snaps to clip edges.
* **Wheel**: Ctrl+wheel zooms around the cursor, plain/Shift wheel and horizontal
  trackpad deltas scroll. Middle-button drag pans.
* **Right-click** a clip: context menu (selects the clip first if needed).

Multi-track
-----------
With more than one video track the lanes are drawn top to bottom as V<n> ... V1, then the
audio lane, each with a label gutter on the left; the view scrolls vertically when they do
not fit. Dragging a video clip moves it freely in time and between lanes
(``Document.move_clip(id, start, track)``); a ghost shows where it will land, and dropping
on the strip above the top lane creates a new track. A single-track project looks and
behaves exactly as before (reorder drag, no gutter).
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import OrderedDict
from typing import Optional

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath,
                           QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QScrollBar,
                               QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget)

from ..core.model import MAX_VIDEO_TRACKS, MediaSource, Segment, Timeline
from ..core.timecode import format_timecode
from ..document import Document
from . import icons, theme

try:  # the constant lives in the (possibly still stubbed) provider module
    from ..media.waveform import PEAKS_PER_SECOND
except Exception:  # pragma: no cover - defensive
    PEAKS_PER_SECOND = 100

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ geometry
RULER_H = 28               # time ruler height
TRACK_PAD = 10             # space between ruler and clips / below clips
LANE_PAD = 5               # track band extends this far above/below the clips
HEADER_H = 19              # clip title strip
MIN_CLIP_H = 64
MAX_CLIP_H = 150
AUDIO_H = 46               # height of a clip on the audio lane
AUDIO_HEADER_H = 16        # title strip of an audio clip
AUDIO_GAP = 13             # video clip bottom -> audio clip top
WAVE_FRACTION = 0.40       # share of the clip body used by the waveform lane
CLIP_RADIUS = 5.0
CLIP_GAP = 1               # px left free between adjacent clips
NARROW_CLIP_PX = 10        # below this width a clip is drawn as a flat block
LEFT_PAD = 14              # px before t = 0
RIGHT_PAD = 40             # px after the end of the timeline
EDGE_GRAB = 7              # px either side of a clip edge that starts a trim
DRAG_THRESHOLD = 6         # px before a body press becomes a reorder drag
SNAP_PX = 8                # snapping tolerance in pixels
MAX_FRAME_PX = 12.0        # maximum zoom: one frame is this many pixels wide
DEFAULT_PPS = 100.0        # zoom used while the timeline is empty
RULER_MIN_MAJOR_PX = 96    # minimum distance between labelled ruler ticks
RULER_MIN_MINOR_PX = 6
PLAYHEAD_HANDLE_W = 11
PLAYHEAD_HANDLE_H = 15
GUTTER_W = 38              # lane label gutter (multi-track only)
NEWTRACK_H = 18            # drop strip above the top lane (multi-track only)
NEWTRACK_SLACK = 4         # px above the top lane that already count as "new track"
LANE_STEP = 2 * LANE_PAD + 3   # clip bottom of one video lane -> clip top of the next
MIN_LANE_H = 36            # smallest video lane when several are stacked
MAX_LANE_H = 110
GHOST_MAX_W = 260
AUTOSCROLL_MARGIN = 24     # px from the left/right border that auto-scrolls a drag
AUTOSCROLL_MAX_STEP = 28
OFFSCREEN_CLAMP = 64       # clip rectangles are clamped this far outside the view
# -------------------------------------------------------------------- timing
SCRUB_INTERVAL_MS = 30
AUTOSCROLL_INTERVAL_MS = 30
USER_SCROLL_GRACE_S = 1.5  # follow-playhead stays quiet this long after a user scroll
# --------------------------------------------------------------------- misc
ZOOM_STEP = 1.3
WHEEL_ZOOM_STEP = 1.18
ZOOM_SLIDER_STEPS = 1000
THUMB_CACHE_LIMIT = 512
FOLLOW_ANCHOR = 0.12       # where the playhead lands (fraction of width) on a page flip
HINT_TEXT = "Download a video or import a file to start editing"
OFFLINE_TEXT = "Media offline"

_SECOND_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400)
_SECOND_SUBDIV = {1: (10, 5, 2), 2: (4, 2), 5: (5,), 10: (10, 5, 2), 15: (3,), 30: (6, 3, 2),
                  60: (6, 4, 2), 120: (4, 2), 300: (5,), 600: (10, 5, 2), 900: (3,),
                  1800: (6, 3, 2), 3600: (6, 4, 2), 7200: (4, 2), 14400: (4, 2)}
_FRAME_STEPS = (1, 2, 4, 5, 6, 10, 12, 15, 20, 30)


def _mix(a: QColor, b: QColor, f: float) -> QColor:
    """Blend ``a`` towards ``b`` by ``f`` (0..1)."""
    g = 1.0 - f
    return QColor(int(a.red() * g + b.red() * f), int(a.green() * g + b.green() * f),
                  int(a.blue() * g + b.blue() * f))


def _alpha(c: QColor, a: int) -> QColor:
    c = QColor(c)
    c.setAlpha(a)
    return c


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def clip_under_playhead(doc: Document) -> Optional[Segment]:
    """The V1 clip under the playhead, else the top-most overlay clip under it."""
    stack = doc.timeline.segments_at(doc.playhead)
    if not stack:
        return None
    return stack[0] if stack[0].layer == 0 else stack[-1]


def can_detach_audio(doc: Document) -> bool:
    """True when :meth:`Document.detach_audio` would do something right now: a selected
    video clip (or, with no video clip selected, the one under the playhead) has sound
    and nothing already sits on the audio lane behind it."""
    timeline = doc.timeline
    targets = [s for s in doc.selected_segments() if not s.is_audio]
    if not targets:
        under = clip_under_playhead(doc)
        targets = [under] if under is not None else []
    for seg in targets:
        src = doc.sources.get(seg.source_id)
        if src is not None and not src.has_audio:
            continue
        if any(a.timeline_start < seg.timeline_end - 1e-6 and a.timeline_end > seg.timeline_start + 1e-6
               for a in timeline.audio):
            continue
        return True
    return False


class _Drag:
    """State of the mouse interaction in progress."""

    __slots__ = ("kind", "seg_id", "press", "offset", "grab_frac", "clip_w", "pos", "mods",
                 "start_scroll", "start_playhead", "drop_index", "collapse_on_release",
                 "slide_start", "target_track")

    def __init__(self, kind: str, press: QPoint, seg_id: Optional[str] = None) -> None:
        self.kind = kind                 # scrub | press | trim_l | trim_r | reorder | slide | move | pan
        self.seg_id = seg_id
        self.press = QPoint(press)
        self.pos = QPoint(press)
        self.mods = Qt.KeyboardModifier.NoModifier
        self.offset = 0.0                # trim: edge time minus time under the cursor
        self.grab_frac = 0.5             # reorder: where inside the clip it was grabbed
        self.clip_w = 0.0
        self.start_scroll = 0.0
        self.start_playhead = 0.0
        self.drop_index: Optional[int] = None
        self.collapse_on_release = False
        self.slide_start: Optional[float] = None   # slide/move: start of the dropped clip
        self.target_track = 0            # move: video track it will land on (== track_count: new)


class TimelineView(QWidget):
    """The custom-painted ruler + track. Owned by :class:`TimelinePanel`."""

    viewChanged = Signal()     # zoom, scroll offset or scroll range changed
    exportAudioRequested = Signal()   # context menu: "Export audio only…"

    def __init__(self, document: Document, thumbnails, waveforms,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._doc = document
        self._thumbs = thumbnails
        self._waves = waveforms
        self.follow_playhead = True

        self._pps = DEFAULT_PPS            # pixels per second
        self._scroll = 0.0                 # horizontal offset in pixels
        self._vscroll = 0.0                # vertical offset of the lanes in pixels
        self.confirm_remove = self._ask_remove   # (layer, clip_count) -> bool; tests may replace
        self._fitted = True                # zoom tracks "fit" until the user zooms
        self._snapping = True
        self._fps = 30.0
        self._prev_count = len(document.timeline.all_segments())
        self._drag: Optional[_Drag] = None
        self._hover_id: Optional[str] = None
        self._snap_time: Optional[float] = None
        self._last_playhead_x: Optional[int] = None
        self._offline: dict[str, bool] = {}
        self._thumb_cache: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._last_user_scroll = -1e9
        self._user_scrolling = False
        self._provider_warned = False

        self._scrub_pending: Optional[float] = None
        self._scrub_timer = QTimer(self)
        self._scrub_timer.setSingleShot(True)
        self._scrub_timer.setInterval(SCRUB_INTERVAL_MS)
        self._scrub_timer.timeout.connect(self._flush_scrub)
        self._autoscroll_timer = QTimer(self)
        self._autoscroll_timer.setInterval(AUTOSCROLL_INTERVAL_MS)
        self._autoscroll_timer.timeout.connect(self._autoscroll_tick)

        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._sync_min_height()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        document.tracksChanged.connect(self._on_tracks_changed)
        document.timelineChanged.connect(self._on_timeline_changed)
        document.selectionChanged.connect(self._on_selection_changed)
        document.sourcesChanged.connect(self._on_sources_changed)
        document.projectReset.connect(self._on_project_reset)
        document.playheadChanged.connect(self._on_playhead_changed)
        if thumbnails is not None:
            thumbnails.thumbnailReady.connect(self._on_thumbnail_ready)
        if waveforms is not None:
            waveforms.waveformReady.connect(self._on_waveform_ready)
        self._refresh_fps()

    # ----------------------------------------------------------------- mapping
    @property
    def pixels_per_second(self) -> float:
        return self._pps

    @property
    def scroll_offset(self) -> float:
        return self._scroll

    def gutter_width(self) -> int:
        """Width of the lane-label gutter: only shown while there are several video tracks."""
        return GUTTER_W if self._doc.timeline.track_count > 1 else 0

    def _x0(self) -> float:
        """Screen x of timeline time 0 at scroll offset 0."""
        return LEFT_PAD + self.gutter_width()

    def time_to_x(self, t: float) -> float:
        return self._x0() + t * self._pps - self._scroll

    def x_to_time(self, x: float) -> float:
        return (x - self._x0() + self._scroll) / self._pps

    def content_width(self) -> float:
        return self._x0() + self._doc.duration * self._pps + RIGHT_PAD

    def max_scroll(self) -> float:
        return max(0.0, self.content_width() - self.width())

    def fit_pps(self) -> float:
        duration = self._doc.duration
        if duration <= 0:
            return DEFAULT_PPS
        return max(50.0, self.width() - self._x0() - RIGHT_PAD) / duration

    def max_pps(self) -> float:
        return MAX_FRAME_PX * self._fps

    def min_pps(self) -> float:
        return min(self.fit_pps(), self.max_pps())

    def has_audio_lane(self) -> bool:
        return bool(self._doc.timeline.audio)

    def _audio_extra(self) -> int:
        return AUDIO_H + AUDIO_GAP if self.has_audio_lane() else 0

    def _sync_min_height(self) -> None:
        self.setMinimumHeight(RULER_H + 2 * TRACK_PAD + MIN_CLIP_H + self._audio_extra())

    # ---- vertical layout -------------------------------------------------------
    def _n(self) -> int:
        return self._doc.timeline.track_count

    def _top_pad(self) -> int:
        return TRACK_PAD + (NEWTRACK_H if self._n() > 1 else 0)

    def _lane_h(self) -> int:
        """Clip height of one video lane."""
        n = self._n()
        avail = self.height() - RULER_H - self._top_pad() - TRACK_PAD - self._audio_extra()
        if n <= 1:
            return int(_clamp(avail, MIN_CLIP_H, MAX_CLIP_H))
        return int(_clamp((avail - (n - 1) * LANE_STEP) / n, MIN_LANE_H, MAX_LANE_H))

    def content_height(self) -> int:
        n = self._n()
        return (RULER_H + self._top_pad() + n * self._lane_h() + (n - 1) * LANE_STEP
                + self._audio_extra() + TRACK_PAD)

    def max_vscroll(self) -> float:
        return max(0.0, self.content_height() - self.height())

    def vscroll_offset(self) -> float:
        return self._vscroll

    def set_vscroll(self, offset: float) -> None:
        offset = _clamp(offset, 0.0, self.max_vscroll())
        if abs(offset - self._vscroll) < 1e-6:
            return
        self._vscroll = offset
        self._view_changed()

    def lane_top(self, layer: int) -> int:
        """Screen y of the clips of video track ``layer`` (0 = V1, drawn lowest)."""
        n = self._n()
        return int(RULER_H + self._top_pad() + (n - 1 - layer) * (self._lane_h() + LANE_STEP)
                   - self._vscroll)

    def lanes(self, layer: int = 0) -> tuple[int, int, int, int]:
        """``(clip_top, clip_height, thumb_height, wave_height)`` of a video lane (V1 default)."""
        clip_h = self._lane_h()
        body = clip_h - HEADER_H
        wave_h = int(round(body * WAVE_FRACTION))
        return self.lane_top(layer), clip_h, body - wave_h, wave_h

    def audio_lane(self) -> Optional[tuple[int, int]]:
        """``(top, height)`` of the audio lane's clips, or ``None`` while it is hidden."""
        if not self.has_audio_lane():
            return None
        y, h, _, _ = self.lanes(0)
        return y + h + AUDIO_GAP, AUDIO_H

    def lane_at(self, y: float) -> Optional[int]:
        """Video layer whose clip row contains screen ``y``; ``-1`` for the audio lane."""
        if y < RULER_H:
            return None
        audio = self.audio_lane()
        if audio is not None and audio[0] <= y < audio[0] + audio[1]:
            return -1
        h = self._lane_h()
        for layer in range(self._n()):
            top = self.lane_top(layer)
            if top <= y < top + h:
                return layer
        return None

    def target_layer(self, y: float) -> int:
        """Video track a clip dropped with the pointer at ``y`` lands on. ``track_count``
        means "a new track above the top one" (pointer above the top lane)."""
        n = self._n()
        if y < self.lane_top(n - 1) - NEWTRACK_SLACK and n < MAX_VIDEO_TRACKS:
            return n
        step = self._lane_h() + LANE_STEP
        first = self.lane_top(n - 1)
        row = int(math.floor((y - first + LANE_STEP / 2.0) / step))
        return n - 1 - int(_clamp(row, 0, n - 1))

    def lane_label(self, layer: int) -> str:
        return "A" if layer < 0 else f"V{layer + 1}"

    def _lane_geometry(self, seg: Segment) -> tuple[int, int, int, int, int]:
        """``(top, height, header, thumb_height, wave_height)`` for the lane of ``seg``."""
        if seg.is_audio:
            lane = self.audio_lane()
            top = lane[0] if lane else 0
            return top, AUDIO_H, AUDIO_HEADER_H, 0, AUDIO_H - AUDIO_HEADER_H
        y, h, thumb_h, wave_h = self.lanes(seg.layer)
        return y, h, HEADER_H, thumb_h, wave_h

    def clip_rect(self, seg: Segment) -> QRectF:
        """On-screen rectangle of a segment (not clamped to the viewport)."""
        y, h = self._lane_geometry(seg)[:2]
        x1 = round(self.time_to_x(seg.timeline_start))
        x2 = max(x1 + 1, round(self.time_to_x(seg.timeline_end)) - CLIP_GAP)
        return QRectF(x1, y, x2 - x1, h)

    # ------------------------------------------------------------ zoom / scroll
    def set_zoom(self, pps: float, anchor_x: Optional[float] = None) -> None:
        """Set pixels-per-second keeping the time under ``anchor_x`` in place."""
        if anchor_x is None:
            anchor_x = self._default_anchor()
        t = self.x_to_time(anchor_x)
        self._pps = _clamp(pps, self.min_pps(), self.max_pps())
        self._scroll = _clamp(self._x0() + t * self._pps - anchor_x, 0.0, self.max_scroll())
        self._fitted = False
        self._view_changed(cancel_thumbs=True)

    def zoom_by(self, factor: float, anchor_x: Optional[float] = None) -> None:
        self.set_zoom(self._pps * factor, anchor_x)

    def zoom_to_fit(self) -> None:
        self._pps = self.min_pps()
        self._scroll = 0.0
        self._fitted = True
        self._view_changed(cancel_thumbs=True)

    def zoom_to_range(self, start: float, end: float) -> None:
        span = max(end - start, 1.0 / max(self._fps, 1.0))
        margin = 0.08 * self.width()
        self._pps = _clamp((self.width() - 2 * margin) / span, self.min_pps(), self.max_pps())
        centre = (start + end) / 2.0
        self._scroll = _clamp(self._x0() + centre * self._pps - self.width() / 2.0,
                              0.0, self.max_scroll())
        self._fitted = False
        self._view_changed(cancel_thumbs=True)

    def zoom_fraction(self) -> float:
        """Current zoom on a logarithmic 0..1 scale (0 = fit, 1 = frame level)."""
        lo, hi = self.min_pps(), self.max_pps()
        if hi <= lo * 1.0001:
            return 0.0
        return _clamp(math.log(max(self._pps, 1e-9) / lo) / math.log(hi / lo), 0.0, 1.0)

    def set_zoom_fraction(self, fraction: float) -> None:
        lo, hi = self.min_pps(), self.max_pps()
        if hi <= lo * 1.0001:
            return
        self.set_zoom(lo * (hi / lo) ** _clamp(fraction, 0.0, 1.0))

    def set_scroll(self, offset: float, user: bool = False) -> None:
        offset = _clamp(offset, 0.0, max(self.max_scroll(), 0.0))
        if user:
            self.note_user_scroll()
        if abs(offset - self._scroll) < 1e-6:
            return
        self._scroll = offset
        self._view_changed(cancel_thumbs=True)

    def note_user_scroll(self) -> None:
        self._last_user_scroll = time.monotonic()

    def set_user_scrolling(self, active: bool) -> None:
        """Told by the panel while the scroll-bar slider is held down."""
        self._user_scrolling = active
        self.note_user_scroll()

    def scroll_to_playhead(self) -> None:
        """Centre the playhead in the view."""
        self.set_scroll(self._x0() + self._doc.playhead * self._pps - self.width() / 2.0)

    def set_snapping(self, enabled: bool) -> None:
        self._snapping = bool(enabled)

    def snapping(self) -> bool:
        return self._snapping

    def is_dragging(self) -> bool:
        return self._drag is not None

    def refresh_media_status(self) -> None:
        """Forget the cached "file exists" checks (call after files may have moved)."""
        self._offline.clear()
        self.update()

    def _default_anchor(self) -> float:
        x = self.time_to_x(self._doc.playhead)
        return x if 0 <= x <= self.width() else self.width() / 2.0

    def _view_changed(self, cancel_thumbs: bool = False) -> None:
        if cancel_thumbs and self._thumbs is not None:
            try:
                self._thumbs.cancel_pending()
            except Exception:  # a provider problem must never break the view
                self._warn_provider("cancel_pending")
        self._last_playhead_x = None
        self.update()
        self.viewChanged.emit()

    def _warn_provider(self, what: str) -> None:
        if not self._provider_warned:
            self._provider_warned = True
            log.exception("Timeline media provider failed in %s", what)

    # --------------------------------------------------------- document signals
    def _refresh_fps(self) -> None:
        sources = self._doc.sources
        fps = 0.0
        for sid in {s.source_id for s in self._doc.timeline.all_segments()}:
            src = sources.get(sid)
            if src is not None and src.fps > fps:
                fps = src.fps
        self._fps = fps if 1.0 <= fps <= 1000.0 else 30.0

    def _on_tracks_changed(self) -> None:
        """The number of video tracks changed: the gutter and lane heights follow."""
        if self._fitted:
            self._pps = self.min_pps()
            self._scroll = 0.0
        self._scroll = _clamp(self._scroll, 0.0, self.max_scroll())
        self._vscroll = _clamp(self._vscroll, 0.0, self.max_vscroll())
        self._view_changed()

    def _on_timeline_changed(self) -> None:
        segments = self._doc.timeline.all_segments()
        ids = {s.id for s in segments}
        self._sync_min_height()
        self._vscroll = _clamp(self._vscroll, 0.0, self.max_vscroll())
        drag = self._drag
        if drag is not None and drag.seg_id is not None and drag.seg_id not in ids:
            self._abort_drag()
        if self._hover_id not in ids:
            self._hover_id = None
        self._refresh_fps()
        count = len(segments)
        if count and self._prev_count == 0 and self._drag is None:
            self._prev_count = count
            self.zoom_to_fit()
            return
        self._prev_count = count
        if self._drag is None:
            if self._fitted and count:
                self._pps = self.min_pps()
            self._pps = min(self._pps, self.max_pps())
            self._scroll = _clamp(self._scroll, 0.0, self.max_scroll())
        self._view_changed()

    def _on_selection_changed(self) -> None:
        self.update()

    def _on_sources_changed(self) -> None:
        self._offline.clear()
        self._refresh_fps()
        self.update()

    def _on_project_reset(self) -> None:
        self._abort_drag()
        self._offline.clear()
        self._thumb_cache.clear()
        self._hover_id = None
        self._prev_count = 0           # the timelineChanged that follows auto-fits
        self._fitted = True
        self._pps = self.min_pps()
        self._scroll = 0.0
        self._vscroll = 0.0
        self._view_changed(cancel_thumbs=True)

    def _on_playhead_changed(self, t: float) -> None:
        x = int(round(self.time_to_x(t)))
        if (self.follow_playhead and self._drag is None and not self._user_busy()
                and (x < 0 or x > self.width() - 1) and self.width() > 0):
            target = self._x0() + t * self._pps - FOLLOW_ANCHOR * self.width()
            target = _clamp(target, 0.0, self.max_scroll())
            if abs(target - self._scroll) > 0.5:
                self._scroll = target
                self._view_changed(cancel_thumbs=True)
                return
        half = PLAYHEAD_HANDLE_W // 2 + 3
        if self._last_playhead_x is None:
            self.update()
        else:
            for px in (self._last_playhead_x, x):
                self.update(QRect(px - half, 0, 2 * half + 1, self.height()))
        self._last_playhead_x = x

    def _user_busy(self) -> bool:
        return (self._user_scrolling
                or time.monotonic() - self._last_user_scroll < USER_SCROLL_GRACE_S)

    def _source_visible(self, source_id: str) -> bool:
        width = self.width()
        for seg in self._doc.timeline.all_segments():
            if self.time_to_x(seg.timeline_end) < 0:
                continue
            if self.time_to_x(seg.timeline_start) > width:
                continue
            if seg.source_id == source_id:
                return True
        return False

    def _on_thumbnail_ready(self, source_id: str, _time: float) -> None:
        if self._source_visible(source_id):
            self.update()

    def _on_waveform_ready(self, source_id: str) -> None:
        if self._source_visible(source_id):
            self.update()

    # ---------------------------------------------------------------- hit tests
    def _segment_by_id(self, seg_id: Optional[str]) -> Optional[Segment]:
        if seg_id is None:
            return None
        for seg in self._doc.timeline.all_segments():
            if seg.id == seg_id:
                return seg
        return None

    def hit_test(self, pos: QPoint) -> tuple[Optional[Segment], str]:
        """``(segment, zone)`` under ``pos``; zone is ``left``/``right``/``body`` or ``""``."""
        layer = self.lane_at(pos.y())
        if layer is None or pos.x() < self.gutter_width():
            return None, ""
        timeline = self._doc.timeline
        lane = timeline.audio if layer < 0 else timeline.track(layer)
        x = pos.x()
        best: Optional[Segment] = None
        zone = ""
        best_d = 1e9
        for seg in lane:
            x1 = self.time_to_x(seg.timeline_start)
            x2 = self.time_to_x(seg.timeline_end)
            if x2 < x - EDGE_GRAB:
                continue
            if x1 > x + EDGE_GRAB:
                break
            grab = min(EDGE_GRAB, max(2.0, (x2 - x1) / 3.0))
            inside = x1 <= x < x2
            # Edges can be grabbed slightly outside the clip, but where two clips
            # touch, the side of the boundary the cursor is on decides.
            d_left, d_right = abs(x - x1), abs(x - x2)
            if d_left <= grab and (inside or x >= x1 - grab) and d_left <= d_right:
                cand, d = "left", d_left + (0.0 if inside else 0.5)
            elif d_right <= grab and (inside or x < x2 + grab):
                cand, d = "right", d_right + (0.0 if inside else 0.5)
            elif inside:
                cand, d = "body", EDGE_GRAB + 1.0
            else:
                continue
            if d < best_d:
                best, zone, best_d = seg, cand, d
        return best, zone

    def _on_playhead_handle(self, pos: QPoint) -> bool:
        x = self.time_to_x(self._doc.playhead)
        return pos.y() < RULER_H and abs(pos.x() - x) <= PLAYHEAD_HANDLE_W / 2 + 2

    # ----------------------------------------------------------------- snapping
    def _snap(self, t: float, points: list[float], mods) -> tuple[float, bool]:
        if not self._snapping or (mods & Qt.KeyboardModifier.AltModifier):
            return t, False
        tolerance = SNAP_PX / self._pps
        snapped = Timeline.snap(t, points, tolerance)
        if snapped != t:
            return snapped, True
        return t, any(abs(p - t) < 1e-9 for p in points)

    def _set_snap_time(self, t: Optional[float]) -> None:
        if t != self._snap_time:
            self._snap_time = t
            self.update()

    # ------------------------------------------------------------- mouse events
    def mousePressEvent(self, event) -> None:
        pos = event.position().toPoint()
        button = event.button()
        if self._drag is not None:
            event.accept()
            return
        if button == Qt.MouseButton.MiddleButton:
            drag = _Drag("pan", pos)
            drag.start_scroll = self._scroll
            self._begin(drag)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if button != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        event.accept()
        mods = event.modifiers()
        if pos.y() < RULER_H:
            self._start_scrub(pos, mods)
            return
        seg, zone = self.hit_test(pos)
        if seg is None:
            if pos.x() < self.gutter_width():
                return                       # the label gutter is not a seek / clear area
            self._doc.clear_selection()
            self._start_scrub(pos, mods)
            return
        if zone in ("left", "right"):
            if seg.id not in self._doc.selection:
                self._doc.select([seg.id])
            drag = _Drag("trim_l" if zone == "left" else "trim_r", pos, seg.id)
            edge = seg.timeline_start if zone == "left" else seg.timeline_end
            drag.offset = edge - self.x_to_time(pos.x())
            self._doc.begin_drag()
            self._begin(drag)
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            return
        if mods & Qt.KeyboardModifier.ControlModifier:
            self._doc.toggle_select(seg.id)
            return
        drag = _Drag("press", pos, seg.id)
        selection = self._doc.selection
        if seg.id not in selection:
            self._doc.select([seg.id])
        elif len(selection) > 1:
            drag.collapse_on_release = True
        self._begin(drag)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        drag = self._drag
        if drag is None:
            self._update_hover(pos)
            return
        drag.pos = QPoint(pos)
        drag.mods = event.modifiers()
        self._drag_move()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        drag = self._drag
        if drag is None:
            return
        button = event.button()
        if (drag.kind == "pan") != (button == Qt.MouseButton.MiddleButton):
            return
        if drag.kind != "pan" and button != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        drag.pos = QPoint(pos)
        drag.mods = event.modifiers()
        if drag.kind in ("trim_l", "trim_r", "reorder", "slide", "move"):
            self._drag_move()
            drag = self._drag            # the move may have aborted it
            if drag is None:
                return
        self._end_drag_state()
        doc = self._doc
        if drag.kind == "scrub":
            self._scrub_timer.stop()
            self._scrub_pending = None
            doc.seek(self._scrub_time(drag))
        elif drag.kind in ("trim_l", "trim_r"):
            doc.end_drag("Trim clip")
        elif drag.kind == "reorder":
            seg = self._segment_by_id(drag.seg_id)
            if drag.drop_index is not None and seg is not None:
                old_index = doc.timeline.index_of(seg.id)
                if drag.drop_index != old_index:
                    doc.reorder(drag.seg_id, drag.drop_index)
                else:
                    self._drop_in_place(drag, seg)
        elif drag.kind == "slide":
            seg = self._segment_by_id(drag.seg_id)
            if seg is not None and drag.slide_start is not None:
                doc.move_clip(seg.id, drag.slide_start)
        elif drag.kind == "move":
            seg = self._segment_by_id(drag.seg_id)
            if seg is not None and drag.slide_start is not None:
                target = _clamp(drag.target_track, 0, doc.track_count)
                doc.move_clip(seg.id, drag.slide_start, int(target))
        elif drag.kind == "press":
            if drag.collapse_on_release and self._segment_by_id(drag.seg_id) is not None:
                doc.select([drag.seg_id])
        self._update_hover(pos)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            seg, zone = self.hit_test(pos)
            if seg is not None and zone == "body":
                self.zoom_to_range(seg.timeline_start, seg.timeline_end)
                event.accept()
                return
        self.mousePressEvent(event)

    def leaveEvent(self, event) -> None:
        if self._drag is None and self._hover_id is not None:
            self._hover_id = None
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:
        angle = event.angleDelta()
        pixel = event.pixelDelta()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = angle.y() or angle.x()
            if delta:
                self.note_user_scroll()
                self.zoom_by(WHEEL_ZOOM_STEP ** (delta / 120.0), event.position().x())
            event.accept()
            return
        if not pixel.isNull():
            delta = pixel.x() if abs(pixel.x()) > abs(pixel.y()) else pixel.y()
            vertical = abs(pixel.y()) >= abs(pixel.x())
        else:
            raw = angle.x() if abs(angle.x()) > abs(angle.y()) else angle.y()
            delta = raw / 120.0 * 90.0
            vertical = abs(angle.y()) >= abs(angle.x())
        if (vertical and self.max_vscroll() > 0
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.set_vscroll(self._vscroll - delta)    # lanes taller than the view
        elif delta:
            self.set_scroll(self._scroll - delta, user=True)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        if self._drag is not None:
            return
        seg, _zone = self.hit_test(event.pos())
        if seg is None:
            layer = self.lane_at(event.pos().y())
            if self._n() > 1 and layer is not None and layer >= 0:
                menu = self.build_track_menu(layer)
                menu.exec(event.globalPos())
                menu.deleteLater()
                event.accept()
            return
        menu = self.build_context_menu(seg.id)
        menu.exec(event.globalPos())
        menu.deleteLater()
        event.accept()

    def build_context_menu(self, segment_id: str) -> QMenu:
        """Context menu for a clip; selects it first when it is not selected."""
        doc = self._doc
        if segment_id not in doc.selection:
            doc.select([segment_id])
        segs = doc.selected_segments()
        t = doc.playhead
        menu = QMenu(self)
        split = menu.addAction(icons.icon("scissors"), "Split at playhead\tS")
        split.setEnabled(any(s.contains(t) for s in segs))
        split.triggered.connect(lambda: doc.split_at_playhead())
        if any(not s.is_audio for s in segs):
            detach = menu.addAction(icons.icon("detach-audio"), "Detach audio\tCtrl+Shift+D")
            detach.setEnabled(can_detach_audio(doc))
            detach.triggered.connect(lambda: doc.detach_audio())
        export_audio = menu.addAction(icons.icon("export"), "Export audio only…\tCtrl+Shift+E")
        export_audio.triggered.connect(self.exportAudioRequested.emit)
        menu.addSeparator()
        delete = menu.addAction(icons.icon("trash"), "Delete\tDel")
        delete.triggered.connect(lambda: doc.delete_selected(ripple=False))
        ripple = menu.addAction(icons.icon("ripple-delete"), "Ripple delete\tShift+Del")
        ripple.triggered.connect(lambda: doc.delete_selected(ripple=True))
        if self._n() > 1 and any(not s.is_audio for s in segs):
            menu.addSeparator()
            up = menu.addAction("Move to track above\tAlt+Up")
            up.triggered.connect(lambda: self.move_selected_track(1))
            down = menu.addAction("Move to track below\tAlt+Down")
            down.setEnabled(any(s.layer > 0 for s in segs if not s.is_audio))
            down.triggered.connect(lambda: self.move_selected_track(-1))
        menu.addSeparator()
        all_muted = bool(segs) and all(s.muted for s in segs)
        mute = menu.addAction(icons.icon("volume" if all_muted else "volume-x"),
                              "Unmute" if all_muted else "Mute")
        mute.triggered.connect(lambda: doc.toggle_mute_selected())
        return menu

    def build_track_menu(self, layer: int) -> QMenu:
        """Context menu of a video lane: add / remove track."""
        menu = QMenu(self)
        add = menu.addAction(icons.icon("plus"), "Add track\tCtrl+Shift+T")
        add.setEnabled(self._n() < MAX_VIDEO_TRACKS)
        add.triggered.connect(lambda: self.add_track())
        remove = menu.addAction(icons.icon("trash"), f"Remove track {self.lane_label(layer)}")
        remove.setEnabled(layer >= 1)
        remove.triggered.connect(lambda: self.remove_track(layer))
        return menu

    # ------------------------------------------------------------ track editing
    def add_track(self) -> int:
        """Add an empty video track on top. Returns its index, or -1 if refused."""
        return self._doc.add_track()

    def _ask_remove(self, layer: int, clips: int) -> bool:
        noun = "clip" if clips == 1 else "clips"
        box = QMessageBox(QMessageBox.Icon.Question, "Remove track",
                          f"{self.lane_label(layer)} contains {clips} {noun}. "
                          f"Remove the track and delete {'it' if clips == 1 else 'them'}?",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def remove_track(self, layer: int) -> bool:
        """Remove video track ``layer`` (never V1); asks first when it holds clips."""
        timeline = self._doc.timeline
        if not 1 <= layer < timeline.track_count:
            return False
        count = len(timeline.track(layer))
        if count:
            if not self.confirm_remove(layer, count):
                return False
            return self._doc.remove_track(layer, delete_clips=True)
        return self._doc.remove_track(layer)

    def move_selected_track(self, delta: int) -> bool:
        """Move the selected video clips ``delta`` lanes (+1 = up) keeping their start time;
        moving up from the top lane creates a new track. Returns True if anything moved."""
        doc = self._doc
        ids = [s.id for s in doc.selected_segments() if not s.is_audio]
        moved = False
        for sid in sorted(ids, key=lambda i: -(doc.timeline.get(i).layer) * delta):
            try:
                seg = doc.timeline.get(sid)
            except Exception:
                continue
            target = seg.layer + delta
            if target < 0:
                continue
            moved = doc.move_clip(sid, seg.timeline_start, target) or moved
        return moved

    # -------------------------------------------------------------- key events
    def event(self, event) -> bool:
        # Claim Esc before window-level shortcuts see it, but only while dragging.
        if (event.type() == QEvent.Type.ShortcutOverride and self._drag is not None
                and event.key() == Qt.Key.Key_Escape):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._drag is not None:
            self.cancel_drag()
            event.accept()
            return
        event.ignore()                   # everything else belongs to the main window

    def keyReleaseEvent(self, event) -> None:
        event.ignore()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)

    # -------------------------------------------------------------- drag engine
    def _begin(self, drag: _Drag) -> None:
        self._drag = drag
        if drag.kind in ("scrub", "trim_l", "trim_r", "press"):
            self._autoscroll_timer.start()

    def _end_drag_state(self) -> None:
        """Drop the interaction state (does not talk to the document)."""
        self._drag = None
        self._autoscroll_timer.stop()
        self._snap_time = None
        self.unsetCursor()
        self._scroll = _clamp(self._scroll, 0.0, self.max_scroll())
        self._view_changed()

    def _abort_drag(self) -> None:
        """Forget the drag because its clip/project vanished underneath it."""
        if self._drag is None:
            return
        self._scrub_timer.stop()
        self._scrub_pending = None
        self._end_drag_state()

    def cancel_drag(self) -> None:
        """Esc: undo whatever the drag in progress did."""
        drag = self._drag
        if drag is None:
            return
        self._scrub_timer.stop()
        self._scrub_pending = None
        self._end_drag_state()
        if drag.kind in ("trim_l", "trim_r"):
            self._doc.cancel_drag()
        elif drag.kind == "scrub":
            self._doc.seek(drag.start_playhead)
        elif drag.kind == "pan":
            self.set_scroll(drag.start_scroll)

    def _start_scrub(self, pos: QPoint, mods) -> None:
        drag = _Drag("scrub", pos)
        drag.mods = mods
        drag.start_playhead = self._doc.playhead
        self._begin(drag)
        self._scrub_to(self._scrub_time(drag))

    def _scrub_time(self, drag: _Drag) -> float:
        t = _clamp(self.x_to_time(drag.pos.x()), 0.0, self._doc.duration)
        t, snapped = self._snap(t, self._doc.timeline.snap_points(), drag.mods)
        self._set_snap_time(t if snapped else None)
        return t

    def _scrub_to(self, t: float) -> None:
        if self._scrub_timer.isActive():
            self._scrub_pending = t
            return
        self._scrub_pending = None
        self._scrub_timer.start()
        self._doc.seek(t)

    def _flush_scrub(self) -> None:
        t, self._scrub_pending = self._scrub_pending, None
        if t is not None and self._drag is not None and self._drag.kind == "scrub":
            self._scrub_timer.start()
            self._doc.seek(t)

    def _drag_move(self) -> None:
        drag = self._drag
        if drag is None:
            return
        doc = self._doc
        x = drag.pos.x()
        if drag.kind == "scrub":
            self._scrub_to(self._scrub_time(drag))
        elif drag.kind == "pan":
            self.set_scroll(drag.start_scroll - (x - drag.press.x()), user=True)
        elif drag.kind in ("trim_l", "trim_r"):
            seg = self._segment_by_id(drag.seg_id)
            if seg is None:
                self._abort_drag()
                return
            t = self.x_to_time(x) + drag.offset
            points = doc.timeline.snap_points(exclude=[seg.id]) + [doc.playhead]
            target, snapped = self._snap(t, points, drag.mods)
            if drag.kind == "trim_l":
                applied = doc.trim_start(seg.id, target)
            else:
                applied = doc.trim_end(seg.id, target)
            if self._drag is not drag:       # a signal handler aborted the drag
                return
            frame = 1.0 / max(self._fps, 1.0)
            self._set_snap_time(target if snapped and abs(applied - target) <= frame else None)
        elif drag.kind == "press":
            if (drag.pos - drag.press).manhattanLength() >= DRAG_THRESHOLD:
                seg = self._segment_by_id(drag.seg_id)
                if seg is None:
                    self._abort_drag()
                    return
                rect = self.clip_rect(seg)
                if seg.is_audio:
                    drag.kind = "slide"
                    drag.offset = seg.timeline_start - self.x_to_time(drag.press.x())
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                    self._update_slide(drag)
                    return
                if self._n() > 1:
                    drag.kind = "move"
                    drag.offset = seg.timeline_start - self.x_to_time(drag.press.x())
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                    self._update_move(drag)
                    return
                drag.kind = "reorder"
                drag.clip_w = rect.width()
                drag.grab_frac = _clamp((drag.press.x() - rect.left()) / max(rect.width(), 1.0),
                                        0.0, 1.0)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self._update_reorder(drag)
        elif drag.kind == "reorder":
            if self._n() < MAX_VIDEO_TRACKS and self.target_layer(drag.pos.y()) >= self._n():
                seg = self._segment_by_id(drag.seg_id)
                if seg is not None:           # dragged above the lane: make it an overlay move
                    drag.kind = "move"
                    drag.drop_index = None
                    drag.offset = seg.timeline_start - self.x_to_time(drag.press.x())
                    self._update_move(drag)
                    return
            self._update_reorder(drag)
        elif drag.kind == "slide":
            self._update_slide(drag)
        elif drag.kind == "move":
            self._update_move(drag)

    def _update_move(self, drag: _Drag) -> None:
        """Work out where the dragged video clip would land: track from the pointer's
        row, start from the snapped pointer time, then the nearest free slot there
        (the same ``Timeline.fit_start`` the model uses on drop)."""
        seg = self._segment_by_id(drag.seg_id)
        if seg is None or seg.is_audio:
            self._abort_drag()
            return
        timeline = self._doc.timeline
        target = self.target_layer(drag.pos.y())
        left = self.x_to_time(drag.pos.x()) + drag.offset
        points = timeline.snap_points(exclude=[seg.id]) + [self._doc.playhead]
        start, snapped = self._snap(left, points, drag.mods)
        if not snapped:
            end, snapped_end = self._snap(left + seg.duration, points, drag.mods)
            if snapped_end:
                start, snapped = end - seg.duration, True
        start = max(0.0, start)
        if target < timeline.track_count:
            landed = timeline.fit_start(target, start, seg.duration, exclude=[seg.id])
        else:
            landed = start
        drag.target_track = target
        drag.slide_start = landed
        snap_at = None
        if snapped and abs(landed - start) < 1e-6:
            snap_at = next((t for t in (landed, landed + seg.duration)
                            if any(abs(p - t) < 1e-6 for p in points)), None)
        self._set_snap_time(snap_at)
        self.update()

    def _update_slide(self, drag: _Drag) -> None:
        """Work out where the dragged audio clip would land (snapped, then clamped
        between its lane neighbours exactly like ``Timeline.move_to``)."""
        seg = self._segment_by_id(drag.seg_id)
        if seg is None or not seg.is_audio:
            self._abort_drag()
            return
        lane = self._doc.timeline.audio
        try:
            i = next(k for k, c in enumerate(lane) if c.id == seg.id)
        except StopIteration:
            self._abort_drag()
            return
        left = self.x_to_time(drag.pos.x()) + drag.offset
        points = self._doc.timeline.snap_points(exclude=[seg.id]) + [self._doc.playhead]
        start, snapped = self._snap(left, points, drag.mods)
        if not snapped:
            end, snapped_end = self._snap(left + seg.duration, points, drag.mods)
            if snapped_end:
                start, snapped = end - seg.duration, True
        lo = lane[i - 1].timeline_end if i > 0 else 0.0
        hi = lane[i + 1].timeline_start - seg.duration if i + 1 < len(lane) else float("inf")
        start = min(max(start, lo), max(lo, hi))
        drag.slide_start = start
        snap_at = None
        if snapped:
            snap_at = next((t for t in (start, start + seg.duration)
                            if any(abs(p - t) < 1e-6 for p in points)), None)
        self._set_snap_time(snap_at)
        self.update()

    def _ghost_rect(self, drag: _Drag) -> QRectF:
        y, h, _, _ = self.lanes()
        w = min(drag.clip_w, GHOST_MAX_W)
        return QRectF(drag.pos.x() - drag.grab_frac * w, y - 3, w, h)

    def _update_reorder(self, drag: _Drag) -> None:
        if self._segment_by_id(drag.seg_id) is None:
            self._abort_drag()
            return
        centre = self.x_to_time(self._ghost_rect(drag).center().x())
        drag.drop_index = self._doc.timeline.index_for_drop(drag.seg_id, centre)
        self.update()

    def _drop_in_place(self, drag: _Drag, seg: Segment) -> None:
        """The order did not change: slide the clip to where it was dropped (e.g. back to
        the start after deleting the clip in front of it), snapping its edges to 0 and
        neighbouring clips unless snapping is off."""
        doc = self._doc
        # move by the mouse travel (the ghost is width-capped, so its edge is not exact)
        left = seg.timeline_start + (drag.pos.x() - drag.press.x()) / self.pixels_per_second
        points = doc.timeline.snap_points(exclude=[seg.id])
        start, snapped = self._snap(left, points, drag.mods)
        if not snapped:
            end, snapped_end = self._snap(left + seg.duration, points, drag.mods)
            if snapped_end:
                start = end - seg.duration
        doc.move_clip(seg.id, max(0.0, start))

    def _autoscroll_tick(self) -> None:
        drag = self._drag
        if drag is None or drag.kind == "pan" or drag.kind == "press":
            return
        x, w = drag.pos.x(), self.width()
        if x < AUTOSCROLL_MARGIN:
            step = -min(AUTOSCROLL_MAX_STEP, (AUTOSCROLL_MARGIN - x) * 0.6 + 2)
        elif x > w - AUTOSCROLL_MARGIN:
            step = min(AUTOSCROLL_MAX_STEP, (x - (w - AUTOSCROLL_MARGIN)) * 0.6 + 2)
        else:
            step = 0.0
        vstep = 0.0
        if drag.kind == "move" and self.max_vscroll() > 0:
            y, h = drag.pos.y(), self.height()
            if y > h - AUTOSCROLL_MARGIN:
                vstep = min(AUTOSCROLL_MAX_STEP, (y - (h - AUTOSCROLL_MARGIN)) * 0.6 + 2)
            elif y < RULER_H + AUTOSCROLL_MARGIN:
                vstep = -min(AUTOSCROLL_MAX_STEP, (RULER_H + AUTOSCROLL_MARGIN - y) * 0.6 + 2)
        changed = False
        if step:
            target = _clamp(self._scroll + step, 0.0, self.max_scroll())
            if abs(target - self._scroll) >= 0.5:
                self._scroll = target
                changed = True
        if vstep:
            target = _clamp(self._vscroll + vstep, 0.0, self.max_vscroll())
            if abs(target - self._vscroll) >= 0.5:
                self._vscroll = target
                changed = True
        if changed:
            self._view_changed(cancel_thumbs=True)
            self._drag_move()

    def _update_hover(self, pos: QPoint) -> None:
        seg, zone = self.hit_test(pos)
        hover = seg.id if seg is not None else None
        if hover != self._hover_id:
            self._hover_id = hover
            self.update()
        if zone in ("left", "right"):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.unsetCursor()

    # ----------------------------------------------------------------- resizing
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._vscroll = _clamp(self._vscroll, 0.0, self.max_vscroll())
        if self._fitted:
            self._pps = self.min_pps()
            self._scroll = 0.0
        else:
            self._pps = _clamp(self._pps, min(self.min_pps(), self._pps), self.max_pps())
            self._scroll = _clamp(self._scroll, 0.0, self.max_scroll())
        self._view_changed()

    # ------------------------------------------------------------------ painting
    def _is_offline(self, source: Optional[MediaSource]) -> bool:
        if source is None:
            return True
        state = self._offline.get(source.path)
        if state is None:
            try:
                state = not os.path.isfile(source.path)
            except (OSError, TypeError, ValueError):
                state = True
            self._offline[source.path] = state
        return state

    def paintEvent(self, event) -> None:
        pal = theme.current()
        area = event.rect()
        width, height = self.width(), self.height()
        p = QPainter(self)
        p.fillRect(area, QColor(pal.panel_alt))
        p.save()
        p.setClipRect(QRect(0, RULER_H, width, max(0, height - RULER_H)))
        n = self._n()
        clip_h = self._lane_h()
        for layer in range(n):
            top = self.lane_top(layer)
            p.fillRect(QRect(area.left(), top - LANE_PAD, area.width(), clip_h + 2 * LANE_PAD),
                       QColor(pal.track_bg))
        audio_lane = self.audio_lane()
        if audio_lane is not None:
            p.fillRect(QRect(area.left(), audio_lane[0] - LANE_PAD, area.width(),
                             audio_lane[1] + 2 * LANE_PAD), QColor(pal.track_bg))

        doc = self._doc
        segments = doc.timeline.all_segments()
        drag = self._drag
        if not segments:
            font = QFont(self.font())
            font.setPixelSize(13)
            p.setFont(font)
            p.setPen(QColor(pal.text_faint))
            p.drawText(QRect(0, RULER_H, width, height - RULER_H),
                       Qt.AlignmentFlag.AlignCenter, HINT_TEXT)
        else:
            selection = set(doc.selection)
            lifted = (drag.seg_id if drag is not None
                      and drag.kind in ("reorder", "slide", "move") else None)
            left, right = area.left() - 2, area.right() + 2
            for seg in segments:
                x2 = self.time_to_x(seg.timeline_end)
                if x2 < left:
                    continue
                x1 = self.time_to_x(seg.timeline_start)
                if x1 > right:
                    continue
                self._paint_clip(p, pal, seg, area, seg.id in selection,
                                 seg.id == self._hover_id and drag is None, seg.id == lifted)
            if lifted is not None:
                if drag.kind == "slide":
                    self._paint_slide(p, pal, drag)
                elif drag.kind == "move":
                    self._paint_move(p, pal, drag)
                else:
                    self._paint_reorder(p, pal, drag)
        p.restore()

        if n > 1:
            self._paint_gutter(p, pal, area)
        self._paint_ruler(p, pal, area)
        if self._snap_time is not None:
            sx = int(round(self.time_to_x(self._snap_time)))
            snap = QColor(pal.snap_line)
            p.fillRect(QRect(sx - 1, RULER_H, 3, height - RULER_H), _alpha(snap, 70))
            p.fillRect(QRect(sx, 0, 1, height), snap)
        self._paint_playhead(p, pal)
        p.end()

    def _paint_gutter(self, p: QPainter, pal, area: QRect) -> None:
        """Opaque label column: V<n> ... V1 and the audio lane."""
        gw = self.gutter_width()
        if area.left() >= gw:
            return
        p.save()
        p.setClipRect(QRect(0, RULER_H, gw, max(0, self.height() - RULER_H)))
        p.fillRect(QRect(0, RULER_H, gw, self.height() - RULER_H), QColor(pal.panel_alt))
        clip_h = self._lane_h()
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setBold(True)
        p.setFont(font)
        lanes = [(layer, self.lane_top(layer), clip_h) for layer in range(self._n())]
        audio = self.audio_lane()
        if audio is not None:
            lanes.append((-1, audio[0], audio[1]))
        selected_layers = {s.layer for s in self._doc.selected_segments() if not s.is_audio}
        for layer, top, h in lanes:
            band = QRect(0, top - LANE_PAD, gw, h + 2 * LANE_PAD)
            p.fillRect(band, QColor(pal.track_bg))
            if layer in selected_layers:
                p.fillRect(QRect(0, band.top(), 2, band.height()), QColor(pal.accent))
            p.setPen(QColor(pal.text if layer in selected_layers else pal.text_dim))
            p.drawText(QRect(0, top, gw - 1, h), Qt.AlignmentFlag.AlignCenter,
                       self.lane_label(layer))
        p.fillRect(QRect(gw - 1, RULER_H, 1, self.height() - RULER_H), QColor(pal.border))
        p.restore()

    # ruler -----------------------------------------------------------------
    def _ruler_steps(self) -> tuple[float, int, int]:
        """``(major_step_seconds, frames_per_major or 0, minor_subdivisions)``."""
        pps = self._pps
        want = RULER_MIN_MAJOR_PX / pps
        fps = self._fps
        nominal = int(round(fps))
        if nominal >= 2:
            for frames in _FRAME_STEPS:
                if frames >= nominal:
                    break
                if nominal % frames:
                    continue
                step = frames / fps
                if step >= want:
                    minor = frames if frames > 1 and pps / fps >= RULER_MIN_MINOR_PX else 1
                    return step, frames, minor
        for step in _SECOND_STEPS:
            if step >= want:
                break
        minor = 1
        for n in _SECOND_SUBDIV.get(step, (2,)):
            if step / n * pps >= RULER_MIN_MINOR_PX:
                minor = n
                break
        return float(step), 0, minor

    def _paint_ruler(self, p: QPainter, pal, area: QRect) -> None:
        if area.top() >= RULER_H:
            return
        width = self.width()
        p.fillRect(QRect(area.left(), 0, area.width(), RULER_H), QColor(pal.ruler_bg))
        p.fillRect(QRect(area.left(), RULER_H - 1, area.width(), 1), QColor(pal.border))
        step, frames, minor = self._ruler_steps()
        tick = QColor(pal.ruler_tick)
        minor_tick = _alpha(tick, 150)
        font = QFont(self.font())
        font.setPixelSize(11)
        p.setFont(font)
        p.setPen(QColor(pal.text_dim))
        hours = self._doc.duration >= 3600
        fps = self._fps
        first = max(0, int(math.floor(self.x_to_time(area.left() - RULER_MIN_MAJOR_PX * 2) / step)))
        last = int(math.ceil(self.x_to_time(min(area.right(), width) + 2) / step))
        sub = step / minor
        for i in range(first, last + 1):
            t = i * step
            x = int(round(self.time_to_x(t)))
            p.fillRect(QRect(x, RULER_H - 13, 1, 12), tick)
            if frames:
                label = format_timecode(t, fps, hours)
            else:
                label = format_timecode(t, 0.0, hours)[:-4]
            p.drawText(QRect(x + 5, 2, RULER_MIN_MAJOR_PX - 8, 15),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            for j in range(1, minor):
                mx = int(round(self.time_to_x(t + j * sub)))
                tall = minor % 2 == 0 and j * 2 == minor
                p.fillRect(QRect(mx, RULER_H - (8 if tall else 5), 1, 7 if tall else 4), minor_tick)

    # clips -----------------------------------------------------------------
    def _paint_clip(self, p: QPainter, pal, seg: Segment, area: QRect, selected: bool,
                    hovered: bool, lifted: bool) -> None:
        full = self.clip_rect(seg)
        clip_y, clip_h, header_h, thumb_h, wave_h = self._lane_geometry(seg)
        xi1, xi2 = int(full.left()), int(full.right())
        cx1 = max(xi1, -OFFSCREEN_CLAMP)
        cx2 = min(xi2, self.width() + OFFSCREEN_CLAMP)
        if cx2 <= cx1:
            return
        source = self._doc.sources.get(seg.source_id)
        offline = self._is_offline(source)
        body = QColor(pal.clip)
        header = QColor(pal.clip_alt)
        if seg.is_audio:                          # audio lane: same family, tinted green
            wave_colour = QColor(pal.waveform)
            body = _mix(body, wave_colour, 0.16)
            header = _mix(header, wave_colour, 0.34)
        elif seg.is_overlay:                      # overlay tracks: accent tint
            accent = QColor(pal.accent)
            body = _mix(body, accent, 0.20)
            header = _mix(header, accent, 0.46)
        danger = QColor(pal.danger)
        if offline:
            body = _mix(body, danger, 0.38)
            header = _mix(header, danger, 0.50)
        outline = QColor(pal.clip_selected)

        p.save()
        if lifted:
            p.setOpacity(0.32)
        if xi2 - xi1 < NARROW_CLIP_PX:           # too small for detail: flat block
            p.fillRect(QRect(cx1, clip_y, cx2 - cx1, clip_h), body)
            p.fillRect(QRect(cx1, clip_y, cx2 - cx1, header_h), header)
            if seg.muted:
                p.fillRect(QRect(cx1, clip_y + header_h + thumb_h, cx2 - cx1, wave_h),
                           QColor(pal.clip_muted))
            if selected:
                p.setPen(QPen(outline, 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(QRectF(cx1 + 0.5, clip_y + 0.5, max(cx2 - cx1 - 1, 0.01), clip_h - 1))
            p.restore()
            return

        rect = QRectF(cx1, clip_y, cx2 - cx1, clip_h)
        radius = min(CLIP_RADIUS, rect.width() / 2.0)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setClipPath(path, Qt.ClipOperation.IntersectClip)
        p.fillRect(rect, body)
        p.fillRect(QRectF(cx1, clip_y, cx2 - cx1, header_h), header)

        va = max(cx1, area.left() - 1)            # visible, repaint-relevant columns
        vb = min(cx2, area.right() + 2)
        thumb_y = clip_y + header_h
        wave_y = thumb_y + thumb_h
        if vb > va:
            if not offline and source is not None:
                if source.has_video and thumb_h > 0:
                    self._paint_thumbs(p, pal, seg, source, va, vb, thumb_y, thumb_h, body)
                self._paint_wave(p, pal, seg, source, va, vb, wave_y, wave_h)
            elif seg.muted:
                p.fillRect(QRect(va, wave_y, vb - va, wave_h), QColor(pal.clip_muted))
            p.fillRect(QRect(va, wave_y, vb - va, 1), QColor(0, 0, 0, 70))
            p.fillRect(QRect(va, thumb_y - 1, vb - va, 1), QColor(0, 0, 0, 50))
        if offline:
            p.fillRect(QRectF(cx1, thumb_y, cx2 - cx1, thumb_h + wave_h),
                       QBrush(_alpha(danger, 120), Qt.BrushStyle.BDiagPattern))
        self._paint_clip_text(p, pal, seg, source, offline, xi1, xi2, clip_y, thumb_h + wave_h,
                              header_h)
        if hovered:
            p.fillRect(rect, QColor(255, 255, 255, 16))
            grip = QColor(255, 255, 255, 110)
            gy = clip_y + header_h + (clip_h - header_h) / 2.0 - 9
            if xi2 - xi1 > 40:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(grip)
                p.drawRoundedRect(QRectF(xi1 + 3, gy, 2.5, 18), 1.2, 1.2)
                p.drawRoundedRect(QRectF(xi2 - 5.5, gy, 2.5, 18), 1.2, 1.2)
        p.restore()

        p.save()
        if lifted:
            p.setOpacity(0.5)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if selected:
            p.setPen(QPen(outline, 2))
            p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), max(radius - 1, 0.5), max(radius - 1, 0.5))
        else:
            p.setPen(QPen(QColor(0, 0, 0, 110) if pal.name != "light" else QColor(0, 0, 0, 60), 1))
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        p.restore()

    def _paint_clip_text(self, p: QPainter, pal, seg: Segment, source: Optional[MediaSource],
                         offline: bool, xi1: int, xi2: int, clip_y: int, body_h: int,
                         header_h: int = HEADER_H) -> None:
        width = self.width()
        left = max(xi1, 0) + 7
        right = min(xi2, width) - 6
        if right - left < 18:
            return
        font = QFont(self.font())
        font.setPixelSize(11)
        p.setFont(font)
        metrics = QFontMetrics(font)
        text_colour = QColor(pal.text)
        if seg.muted:
            dpr = self.devicePixelRatioF()
            pm = icons.pixmap("volume-x", pal.text, 13, dpr)
            p.drawPixmap(QPointF(left - 1, clip_y + (header_h - 13) / 2.0), pm)
            left += 17
        if right - left >= 18:
            if source is not None:
                title = source.title or os.path.splitext(os.path.basename(source.path))[0]
            else:
                title = "Missing media"
            p.setPen(_alpha(text_colour, 235))
            p.drawText(QRectF(left, clip_y, right - left, header_h - 1),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       metrics.elidedText(title, Qt.TextElideMode.ElideRight, int(right - left)))
        if offline:
            left = max(xi1, 0) + 6
            if right - left >= 30:
                font.setBold(True)
                p.setFont(font)
                label = QFontMetrics(font).elidedText(OFFLINE_TEXT, Qt.TextElideMode.ElideRight,
                                                      int(right - left))
                rect = QRectF(left, clip_y + header_h, right - left, body_h)
                p.setPen(QColor(0, 0, 0, 120))
                p.drawText(rect.translated(0, 1), Qt.AlignmentFlag.AlignCenter, label)
                p.setPen(_mix(QColor(pal.danger), QColor("#ffffff"), 0.55)
                         if pal.name != "light" else _mix(QColor(pal.danger), QColor("#000000"), 0.35))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    def _scaled_thumb(self, image: QImage, tile_w: int, tile_h: int, dpr: float) -> QPixmap:
        key = (image.cacheKey(), tile_w, tile_h, int(round(dpr * 100)))
        cache = self._thumb_cache
        pm = cache.get(key)
        if pm is not None:
            cache.move_to_end(key)
            return pm
        w, h = max(1, int(round(tile_w * dpr))), max(1, int(round(tile_h * dpr)))
        scaled = image.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                              Qt.TransformationMode.SmoothTransformation)
        if scaled.width() > w or scaled.height() > h:
            scaled = scaled.copy((scaled.width() - w) // 2, (scaled.height() - h) // 2, w, h)
        pm = QPixmap.fromImage(scaled)
        pm.setDevicePixelRatio(dpr)
        cache[key] = pm
        while len(cache) > THUMB_CACHE_LIMIT:
            cache.popitem(last=False)
        return pm

    def _paint_thumbs(self, p: QPainter, pal, seg: Segment, source: MediaSource, va: int, vb: int,
                      y: int, h: int, body: QColor) -> None:
        aspect = source.width / source.height if source.width > 0 and source.height > 0 else 16 / 9
        tile_w = max(8, int(round(h * _clamp(aspect, 0.3, 4.0))))
        seconds_per_tile = tile_w / self._pps
        # Tiles sit on a grid anchored at source time 0, so trimming or scrolling
        # never changes which thumbnails a clip asks for.
        base = int(round(self.time_to_x(seg.timeline_start - seg.source_in)))
        k0 = max(0, (va - base) // tile_w)
        k1 = (vb - 1 - base) // tile_w
        dpr = self.devicePixelRatioF()
        shade_a, shade_b = body.darker(128), body.darker(140)
        separator = QColor(0, 0, 0, 55)
        thumbs = self._thumbs
        for k in range(k0, k1 + 1):
            x = base + k * tile_w
            image = None
            if thumbs is not None:
                try:
                    image = thumbs.get(source, k * seconds_per_tile)
                except Exception:
                    self._warn_provider("thumbnails.get")
            if image is not None and not image.isNull():
                p.drawPixmap(x, y, self._scaled_thumb(image, tile_w, h, dpr))
            else:
                p.fillRect(QRect(x, y, tile_w, h), shade_a if k % 2 == 0 else shade_b)
            p.fillRect(QRect(x, y, 1, h), separator)

    def _paint_wave(self, p: QPainter, pal, seg: Segment, source: MediaSource, va: int, vb: int,
                    y: int, h: int) -> None:
        lane = QRect(va, y, vb - va, h)
        if seg.muted:
            p.fillRect(lane, QColor(pal.clip_muted))
            colour = _alpha(QColor(pal.text_faint), 200)
        else:
            p.fillRect(lane, QColor(0, 0, 0, 62))
            colour = QColor(pal.waveform)
        mid = y + h / 2.0
        peaks = None
        if source.has_audio and self._waves is not None:
            try:
                peaks = self._waves.get(source)
            except Exception:
                self._warn_provider("waveforms.get")
        if not source.has_audio or (peaks is not None and len(peaks) == 0):
            pen = QPen(_alpha(QColor(pal.text_faint), 130), 1, Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([2, 4])
            p.save()
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(pen)
            p.drawLine(QPointF(va, math.floor(mid) + 0.5), QPointF(vb, math.floor(mid) + 0.5))
            p.restore()
            return
        if peaks is None:                         # still being extracted
            p.fillRect(QRectF(va, math.floor(mid), vb - va, 1), _alpha(colour, 120))
            return
        n = vb - va
        if n <= 0:
            return
        total = len(peaks)
        # Source time at every visible pixel-column edge -> peak row index.
        edges = (np.arange(va, vb + 1, dtype=np.float64) - self._x0() + self._scroll) / self._pps
        rows = np.floor((seg.source_in + edges - seg.timeline_start) * PEAKS_PER_SECOND)
        rows = np.clip(rows, 0, total).astype(np.int64)
        lo = int(rows[0])
        hi = int(min(max(rows[-1], lo + 1), total))
        if lo >= total or hi <= lo:
            p.fillRect(QRectF(va, math.floor(mid), n, 1), _alpha(colour, 120))
            return
        window = peaks[lo:hi]
        starts = np.clip(rows[:-1] - lo, 0, len(window) - 1)
        mins = np.minimum.reduceat(window[:, 0], starts)
        maxs = np.maximum.reduceat(window[:, 1], starts)
        past_end = rows[:-1] >= total
        if past_end.any():
            mins = np.where(past_end, 0.0, mins)
            maxs = np.where(past_end, 0.0, maxs)
        amp = max(1.0, h / 2.0 - 2.0)
        tops = mid - np.clip(maxs, -1.0, 1.0) * amp
        bottoms = mid - np.clip(mins, -1.0, 1.0) * amp
        bottoms = np.maximum(bottoms, tops + 1.0)
        xs = np.arange(va, vb, dtype=np.float64) + 0.5
        top_list, bottom_list, x_list = tops.tolist(), bottoms.tolist(), xs.tolist()
        points = [QPointF(x_list[i], top_list[i]) for i in range(n)]
        points.extend(QPointF(x_list[i], bottom_list[i]) for i in range(n - 1, -1, -1))
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(colour, 1))
        p.setBrush(colour)
        p.drawPolygon(QPolygonF(points))
        p.restore()

    # overlays --------------------------------------------------------------
    def _paint_reorder(self, p: QPainter, pal, drag: _Drag) -> None:
        seg = self._segment_by_id(drag.seg_id)
        if seg is None:
            return
        clip_y, clip_h, _, _ = self.lanes()
        segments = self._doc.timeline.segments
        rest = [s for s in segments if s.id != seg.id]
        old_index = next((i for i, s in enumerate(segments) if s.id == seg.id), -1)
        idx = drag.drop_index
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if idx is not None and idx != old_index and rest:
            t = rest[idx].timeline_start if idx < len(rest) else rest[-1].timeline_end
            x = round(self.time_to_x(t))
            accent = QColor(pal.accent)
            p.fillRect(QRectF(x - 1, clip_y - LANE_PAD, 2, clip_h + 2 * LANE_PAD), accent)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            top = clip_y - LANE_PAD
            bottom = clip_y + clip_h + LANE_PAD
            p.drawPolygon(QPolygonF([QPointF(x - 5, top), QPointF(x + 5, top), QPointF(x, top + 6)]))
            p.drawPolygon(QPolygonF([QPointF(x - 5, bottom), QPointF(x + 5, bottom),
                                     QPointF(x, bottom - 6)]))
        ghost = self._ghost_rect(drag)
        radius = min(CLIP_RADIUS, ghost.width() / 2.0)
        p.setPen(QPen(QColor(pal.clip_selected), 1.5))
        p.setBrush(_alpha(QColor(pal.clip_alt), 165))
        p.drawRoundedRect(ghost.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)
        source = self._doc.sources.get(seg.source_id)
        if source is not None and ghost.width() > 40:
            font = QFont(self.font())
            font.setPixelSize(11)
            p.setFont(font)
            title = source.title or os.path.splitext(os.path.basename(source.path))[0]
            p.setPen(QColor(pal.text))
            p.drawText(QRectF(ghost.left() + 8, ghost.top(), ghost.width() - 16, HEADER_H),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(font).elidedText(title, Qt.TextElideMode.ElideRight,
                                                     int(ghost.width() - 16)))
        p.restore()

    def _paint_slide(self, p: QPainter, pal, drag: _Drag) -> None:
        """Outline of where the dragged audio clip will land."""
        seg = self._segment_by_id(drag.seg_id)
        if seg is None or drag.slide_start is None:
            return
        y, h = self._lane_geometry(seg)[:2]
        x1 = round(self.time_to_x(drag.slide_start))
        x2 = max(x1 + 1, round(self.time_to_x(drag.slide_start + seg.duration)) - CLIP_GAP)
        rect = QRectF(x1, y, x2 - x1, h)
        radius = min(CLIP_RADIUS, rect.width() / 2.0)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(QColor(pal.clip_selected), 1.5))
        p.setBrush(_alpha(QColor(pal.waveform), 60))
        p.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)
        p.restore()

    def _paint_move(self, p: QPainter, pal, drag: _Drag) -> None:
        """Ghost of where the dragged video clip will land, and the new-track drop marker."""
        seg = self._segment_by_id(drag.seg_id)
        if seg is None or drag.slide_start is None:
            return
        n = self._n()
        target = drag.target_track
        new_track = target >= n
        clip_h = self._lane_h()
        accent = QColor(pal.accent)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if new_track:
            top_lane = self.lane_top(n - 1)
            strip = QRectF(self.gutter_width() + 2, RULER_H + 3,
                           self.width() - self.gutter_width() - 4,
                           top_lane - LANE_PAD - RULER_H - 5)
            if strip.height() >= 10:
                pen = QPen(accent, 1, Qt.PenStyle.CustomDashLine)
                pen.setDashPattern([4, 3])
                p.setPen(pen)
                p.setBrush(_alpha(accent, 40))
                p.drawRoundedRect(strip, 4, 4)
                font = QFont(self.font())
                font.setPixelSize(11)
                p.setFont(font)
                p.setPen(accent)
                p.drawText(strip, Qt.AlignmentFlag.AlignCenter,
                           f"Drop to create track {self.lane_label(n)}")
            else:
                p.fillRect(QRectF(0, top_lane - LANE_PAD - 1, self.width(), 2), accent)
            y = top_lane
        else:
            y = self.lane_top(target)
        x1 = round(self.time_to_x(drag.slide_start))
        x2 = max(x1 + 1, round(self.time_to_x(drag.slide_start + seg.duration)) - CLIP_GAP)
        rect = QRectF(x1, y, x2 - x1, clip_h)
        radius = min(CLIP_RADIUS, rect.width() / 2.0)
        p.setPen(QPen(QColor(pal.clip_selected), 1.5))
        p.setBrush(_alpha(accent, 70))
        p.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)
        source = self._doc.sources.get(seg.source_id)
        if source is not None and rect.width() > 40:
            font = QFont(self.font())
            font.setPixelSize(11)
            p.setFont(font)
            title = source.title or os.path.splitext(os.path.basename(source.path))[0]
            p.setPen(QColor(pal.text))
            p.drawText(QRectF(rect.left() + 8, rect.top(), rect.width() - 16, HEADER_H),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(font).elidedText(title, Qt.TextElideMode.ElideRight,
                                                     int(rect.width() - 16)))
        p.restore()

    def _paint_playhead(self, p: QPainter, pal) -> None:
        x = int(round(self.time_to_x(self._doc.playhead)))
        self._last_playhead_x = x
        half = PLAYHEAD_HANDLE_W / 2.0
        if x < -half - 2 or x > self.width() + half + 2:
            return
        colour = QColor(pal.playhead)
        top = RULER_H - PLAYHEAD_HANDLE_H - 1
        line_bottom = self.height() if x >= self.gutter_width() else RULER_H
        p.fillRect(QRect(x, top + 4, 1, line_bottom - top - 4), colour)
        cx = x + 0.5
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(colour.darker(125), 1))
        p.setBrush(colour)
        shape = QPainterPath()
        shape.moveTo(cx - half + 1.5, top)
        shape.lineTo(cx + half - 1.5, top)
        shape.quadTo(cx + half, top, cx + half, top + 1.5)
        shape.lineTo(cx + half, top + PLAYHEAD_HANDLE_H - 6)
        shape.lineTo(cx, top + PLAYHEAD_HANDLE_H)
        shape.lineTo(cx - half, top + PLAYHEAD_HANDLE_H - 6)
        shape.lineTo(cx - half, top + 1.5)
        shape.quadTo(cx - half, top, cx - half + 1.5, top)
        shape.closeSubpath()
        p.drawPath(shape)
        p.restore()


class TimelinePanel(QWidget):
    """Tool row, timeline view and horizontal scroll bar.

    Public API: :meth:`zoom_in`, :meth:`zoom_out`, :meth:`zoom_to_fit`,
    :meth:`set_snapping`, :meth:`snapping`, :meth:`scroll_to_playhead`, the
    ``follow_playhead`` attribute, :meth:`refresh_theme` and
    :meth:`refresh_media_status`. Signal ``snappingChanged(bool)``.
    """

    snappingChanged = Signal(bool)

    def __init__(self, document: Document, thumbnails, waveforms,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.document = document
        self.view = TimelineView(document, thumbnails, waveforms, self)
        self._syncing = False

        # ---- tool row
        self.undo_button = self._tool("undo", "Undo (Ctrl+Z)", lambda: document.undo())
        self.redo_button = self._tool("redo", "Redo (Ctrl+Y)", lambda: document.redo())
        self.split_button = self._tool("scissors", "Split at playhead (S)",
                                       lambda: document.split_at_playhead())
        self.delete_button = self._tool("trash", "Delete (Del)",
                                        lambda: document.delete_selected(ripple=False))
        self.ripple_button = self._tool("ripple-delete", "Ripple delete (Shift+Del)",
                                        lambda: document.delete_selected(ripple=True))
        self.detach_button = self._tool("detach-audio", "Detach audio (Ctrl+Shift+D)",
                                        lambda: document.detach_audio())
        self.mute_button = self._tool("volume", "Mute clip",
                                      lambda: document.toggle_mute_selected())
        self.gaps_button = self._tool("close-gaps", "Close gaps",
                                      lambda: document.close_gaps(self._gap_track()))
        self.track_button = self._tool("plus", "Add video track (Ctrl+Shift+T)", self.add_track)
        self.track_button.setText("Track")
        self.track_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.snap_button = self._tool("magnet", "Snapping (hold Alt while dragging to bypass)")
        self.snap_button.setCheckable(True)
        self.snap_button.setChecked(True)
        self.snap_button.toggled.connect(self._on_snap_toggled)
        self.zoom_out_button = self._tool("zoom-out", "Zoom out (Ctrl+wheel)", self.zoom_out)
        self.zoom_in_button = self._tool("zoom-in", "Zoom in (Ctrl+wheel)", self.zoom_in)
        self.fit_button = self._tool("fit", "Zoom to fit", self.zoom_to_fit)
        self._icon_names = {
            self.undo_button: "undo", self.redo_button: "redo", self.split_button: "scissors",
            self.delete_button: "trash", self.ripple_button: "ripple-delete",
            self.gaps_button: "close-gaps", self.detach_button: "detach-audio", self.zoom_out_button: "zoom-out",
            self.zoom_in_button: "zoom-in", self.fit_button: "fit",
        }
        self._mute_icon_name = ""

        self.info_label = QLabel(self)
        self.info_label.setTextFormat(Qt.TextFormat.PlainText)
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.info_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.info_label.setMinimumWidth(0)

        self.zoom_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.zoom_slider.setRange(0, ZOOM_SLIDER_STEPS)
        self.zoom_slider.setFixedWidth(120)
        self.zoom_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.zoom_slider.setToolTip("Zoom")
        self.zoom_slider.valueChanged.connect(self._on_slider_changed)

        row = QHBoxLayout()
        row.setContentsMargins(8, 5, 8, 5)
        row.setSpacing(2)
        for widget in (self.undo_button, self.redo_button, self._separator(),
                       self.split_button, self.delete_button, self.ripple_button,
                       self.detach_button, self.mute_button, self.gaps_button, self._separator(),
                       self.track_button, self.snap_button):
            row.addWidget(widget)
        row.addSpacing(10)
        row.addWidget(self.info_label, 1)
        row.addSpacing(10)
        row.addWidget(self._separator())
        row.addWidget(self.zoom_out_button)
        row.addSpacing(2)
        row.addWidget(self.zoom_slider)
        row.addSpacing(2)
        row.addWidget(self.zoom_in_button)
        row.addWidget(self.fit_button)

        self.scrollbar = QScrollBar(Qt.Orientation.Horizontal, self)
        self.scrollbar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scrollbar.valueChanged.connect(self._on_scrollbar_changed)
        self.scrollbar.sliderPressed.connect(lambda: self.view.set_user_scrolling(True))
        self.scrollbar.sliderReleased.connect(lambda: self.view.set_user_scrolling(False))
        self.scrollbar.actionTriggered.connect(lambda _a: self.view.note_user_scroll())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.vscrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        self.vscrollbar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.vscrollbar.valueChanged.connect(self._on_vscrollbar_changed)
        self.vscrollbar.setVisible(False)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.view, 1)
        body.addWidget(self.vscrollbar)

        layout.addLayout(row)
        layout.addLayout(body, 1)
        layout.addWidget(self.scrollbar)

        self.view.viewChanged.connect(self._sync_view_controls)
        document.historyChanged.connect(self._update_actions)
        document.selectionChanged.connect(self._update_actions)
        document.timelineChanged.connect(self._update_actions)
        document.projectReset.connect(self._update_actions)
        document.tracksChanged.connect(self._update_actions)
        document.playheadChanged.connect(self._update_split)
        document.sourcesChanged.connect(self._update_split)
        self.refresh_theme()
        self._update_actions()
        self._sync_view_controls()

    # ------------------------------------------------------------------ public
    @property
    def follow_playhead(self) -> bool:
        return self.view.follow_playhead

    @follow_playhead.setter
    def follow_playhead(self, enabled: bool) -> None:
        self.view.follow_playhead = bool(enabled)

    def zoom_in(self) -> None:
        self.view.zoom_by(ZOOM_STEP)

    def zoom_out(self) -> None:
        self.view.zoom_by(1.0 / ZOOM_STEP)

    def zoom_to_fit(self) -> None:
        self.view.zoom_to_fit()

    def set_snapping(self, enabled: bool) -> None:
        self.snap_button.setChecked(bool(enabled))     # toggled -> view + signal

    def snapping(self) -> bool:
        return self.view.snapping()

    def scroll_to_playhead(self) -> None:
        self.view.scroll_to_playhead()

    def add_track(self) -> int:
        """Add an empty video track on top. Returns its index, or -1 if refused."""
        return self.view.add_track()

    def move_selected_track(self, delta: int) -> bool:
        """Move the selected video clips one lane up (``+1``) or down (``-1``)."""
        return self.view.move_selected_track(delta)

    def refresh_media_status(self) -> None:
        """Re-check which source files exist (the result is cached between calls)."""
        self.view.refresh_media_status()

    def refresh_theme(self) -> None:
        """Rebuild themed icons after ``theme.apply_theme`` switched palettes."""
        for button, name in self._icon_names.items():
            button.setIcon(icons.icon(name))
        self.snap_button.setIcon(icons.icon("magnet", active_color=theme.current().accent))
        self._mute_icon_name = ""
        self._update_actions()
        self.view.update()

    # ----------------------------------------------------------------- helpers
    def _tool(self, icon_name: str, tooltip: str, slot=None) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(icons.icon(icon_name))
        button.setIconSize(icons.icon_size(18))
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setAutoRaise(True)
        if slot is not None:
            button.clicked.connect(lambda _checked=False: slot())
        return button

    def _separator(self) -> QFrame:
        line = QFrame(self)
        line.setProperty("separator", True)
        line.setFixedSize(1, 18)
        holder = QFrame(self)
        box = QHBoxLayout(holder)
        box.setContentsMargins(6, 0, 6, 0)
        box.addWidget(line)
        return holder

    def _on_snap_toggled(self, checked: bool) -> None:
        self.view.set_snapping(checked)
        self.snappingChanged.emit(checked)

    def _on_slider_changed(self, value: int) -> None:
        if not self._syncing:
            self.view.set_zoom_fraction(value / ZOOM_SLIDER_STEPS)

    def _on_scrollbar_changed(self, value: int) -> None:
        if not self._syncing:
            self.view.set_scroll(float(value))

    def _on_vscrollbar_changed(self, value: int) -> None:
        if not self._syncing:
            self.view.set_vscroll(float(value))

    def _gap_track(self) -> int:
        """Track whose gaps the button closes: the lane of the selected video clips when
        they share one, otherwise V1."""
        layers = {s.layer for s in self.document.selected_segments() if not s.is_audio}
        return layers.pop() if len(layers) == 1 else 0

    def _sync_view_controls(self) -> None:
        view = self.view
        self._syncing = True
        try:
            maximum = int(math.ceil(view.max_scroll()))
            value = int(round(view.scroll_offset))
            self.scrollbar.setRange(0, max(maximum, value))
            self.scrollbar.setPageStep(max(1, view.width()))
            self.scrollbar.setSingleStep(40)
            self.scrollbar.setValue(value)
            self.zoom_slider.setValue(int(round(view.zoom_fraction() * ZOOM_SLIDER_STEPS)))
            vmax = int(math.ceil(view.max_vscroll()))
            self.vscrollbar.setRange(0, vmax)
            self.vscrollbar.setPageStep(max(1, view.height()))
            self.vscrollbar.setSingleStep(24)
            self.vscrollbar.setValue(int(round(view.vscroll_offset())))
            self.vscrollbar.setVisible(vmax > 0)
        finally:
            self._syncing = False
        has_clips = not self.document.timeline.is_empty()
        can_zoom = has_clips and view.max_pps() > view.min_pps() * 1.0001
        fraction = view.zoom_fraction()
        self.zoom_slider.setEnabled(can_zoom)
        self.zoom_in_button.setEnabled(can_zoom and fraction < 0.9995)
        self.zoom_out_button.setEnabled(can_zoom and fraction > 0.0005)
        self.fit_button.setEnabled(has_clips)

    def _update_split(self, *_args) -> None:
        doc = self.document
        t = doc.playhead
        self.split_button.setEnabled(clip_under_playhead(doc) is not None
                                     or any(s.contains(t) for s in doc.selected_segments()))
        self.detach_button.setEnabled(can_detach_audio(doc))

    def _update_actions(self) -> None:
        doc = self.document
        history = doc.history
        self.undo_button.setEnabled(doc.can_undo())
        self.redo_button.setEnabled(doc.can_redo())
        undo_label, redo_label = history.undo_label(), history.redo_label()
        self.undo_button.setToolTip(f"Undo {undo_label.lower()} (Ctrl+Z)" if undo_label
                                    else "Undo (Ctrl+Z)")
        self.redo_button.setToolTip(f"Redo {redo_label.lower()} (Ctrl+Y)" if redo_label
                                    else "Redo (Ctrl+Y)")
        segs = doc.selected_segments()
        has_selection = bool(segs)
        self.delete_button.setEnabled(has_selection)
        self.ripple_button.setEnabled(has_selection)
        self.mute_button.setEnabled(has_selection)
        all_muted = has_selection and all(s.muted for s in segs)
        icon_name = "volume-x" if all_muted else "volume"
        if icon_name != self._mute_icon_name:
            self._mute_icon_name = icon_name
            self.mute_button.setIcon(icons.icon(icon_name))
        self.mute_button.setToolTip("Unmute clip" if all_muted else "Mute clip")
        self.gaps_button.setEnabled(bool(doc.timeline.gaps(self._gap_track())))
        self.track_button.setEnabled(doc.track_count < MAX_VIDEO_TRACKS)
        self._update_split()
        self._update_info(segs)
        self._sync_view_controls()

    def _update_info(self, segs: list[Segment]) -> None:
        if not segs:
            text, dim = "No clip selected", True
        elif len(segs) == 1:
            seg = segs[0]
            text = (f"Clip  start {format_timecode(seg.timeline_start)} · "
                    f"end {format_timecode(seg.timeline_end)} · "
                    f"duration {format_timecode(seg.duration)}")
            dim = False
        else:
            total = sum(s.duration for s in segs)
            text = f"{len(segs)} clips · total duration {format_timecode(total)}"
            dim = False
        self.info_label.setText(text)
        self.info_label.setToolTip(text if not dim else "")
        role = "dim" if dim else ""
        if self.info_label.property("role") != role:
            self.info_label.setProperty("role", role)
            style = self.info_label.style()
            style.unpolish(self.info_label)
            style.polish(self.info_label)
