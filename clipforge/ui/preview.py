"""Preview panel: plays the *edited* timeline.

Engine design
-------------
* A small pool of "slots" (``QMediaPlayer`` + ``QAudioOutput`` + ``QVideoSink``), created
  lazily and kept loaded (at most :data:`MAX_PLAYERS`, least-recently-used eviction).
  A source normally owns one slot; while playing towards a cut a second *standby* slot
  is pre-cued (paused) on the first frame of the next clip so the cut is instantaneous
  even when both clips come from the same file.
* Frames are painted by us (``QVideoFrame.toImage()``), never by a ``QVideoWidget``, so
  every frame can be vetted before it reaches the screen: a frame is only shown when it
  comes from the active slot, belongs to the seek that is currently in force and its
  timestamp lies inside the active segment's source window. Anything else is dropped
  and the last valid frame (or black, in a gap) stays up.
* Cuts are frame driven: the first frame whose start time is at/after ``source_out`` is
  not shown and triggers the switch. A 8 ms precise timer is the backup (audio-only
  media, end of file) and drives the playhead between frames, gaps and offline clips.
* Paused seeks ask the player for the *middle* of the wanted frame (measured: asking
  for the exact frame start yields the previous frame; the mid-frame request is exact).
* ``J`` (``shuttle(-1)``) is implemented as "pause and jump back one second per press".
"""

from __future__ import annotations

import logging
import math
import os
from typing import Optional

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QSizePolicy,
                               QSlider, QToolButton, QVBoxLayout, QWidget)

from ..core.model import MediaSource, Segment
from ..core.timecode import format_timecode
from . import icons, theme

log = logging.getLogger(__name__)

MAX_PLAYERS = 4
TICK_MS = 8
RATES = [0.25, 0.5, 1.0, 1.5, 2.0, 4.0]
SHUTTLE_RATES = [1.0, 2.0, 4.0]
GAP_FRAME = 1.0 / 30.0
_TS_EPS = 0.002             # timestamp comparison slack (seconds)
_END_EPS = 1e-4             # "at the end of the timeline"
_SCRUB_COALESCE_MS = 60     # a newer paused seek waits this long for the previous one
_SETTLE_MS = 400            # paused seek: exact frame expected within this time
_PENDING_TIMEOUT_MS = 1500  # playing: stop waiting for the first frame after a seek
_FRAME_CLOCK_MS = 250       # frames younger than this drive the playback clock

_MODE_IDLE, _MODE_MEDIA, _MODE_CLOCK = 0, 1, 2

MSG_CODEC = "This file can't be played in the preview (codec) — export still works"


def _frame_index(t: float, fps: float) -> int:
    return int(math.floor(t * fps + 0.01))


# --------------------------------------------------------------------------- viewport
class _Viewport(QWidget):
    """Custom-painted, letterboxed video surface with placeholder / message states."""

    PLACEHOLDER, BLACK, FRAME = "placeholder", "black", "frame"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._image: Optional[QImage] = None
        self._state = self.PLACEHOLDER
        self._message = ""
        self._detail = ""
        self.setMinimumSize(240, 135)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    # state ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def message(self) -> str:
        return self._message

    def image(self) -> Optional[QImage]:
        return self._image if self._state == self.FRAME else None

    def show_placeholder(self) -> None:
        self._state, self._image, self._message, self._detail = self.PLACEHOLDER, None, "", ""
        self.update()

    def show_black(self, message: str = "", detail: str = "") -> None:
        self._state, self._image = self.BLACK, None
        self._message, self._detail = message, detail
        self.update()

    def show_frame(self, image: QImage) -> None:
        self._state, self._image, self._message, self._detail = self.FRAME, image, "", ""
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return QSize(640, 360)

    # painting ---------------------------------------------------------------
    def video_rect(self) -> QRectF:
        """Where the current image is drawn (letterboxed), in widget coordinates."""
        area = QRectF(self.rect())
        if self._image is None or self._image.isNull():
            return area
        iw, ih = self._image.width(), self._image.height()
        if iw <= 0 or ih <= 0:
            return area
        scale = min(area.width() / iw, area.height() / ih)
        w, h = iw * scale, ih * scale
        return QRectF(area.x() + (area.width() - w) / 2, area.y() + (area.height() - h) / 2, w, h)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        area = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(area.adjusted(0.5, 0.5, -0.5, -0.5), theme.RADIUS + 2, theme.RADIUS + 2)
        painter.fillPath(path, QColor(p.preview_bg))
        painter.setClipPath(path)
        if self._state == self.FRAME and self._image is not None and not self._image.isNull():
            painter.drawImage(self.video_rect(), self._image)
        elif self._state == self.PLACEHOLDER:
            self._paint_notice(painter, area, "film", "No video loaded",
                               "Import a file or download a clip to start editing",
                               p.text_faint, p.text_dim)
        elif self._message:
            self._paint_notice(painter, area, "alert", self._message, self._detail,
                               p.warning, p.text)
        painter.setClipping(False)
        pen_color = QColor(p.border)
        painter.setPen(pen_color)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.end()

    def _paint_notice(self, painter: QPainter, area: QRectF, icon_name: str, title: str,
                      detail: str, icon_color: str, title_color: str) -> None:
        p = theme.current()
        icon_px = 44 if area.height() >= 220 else 30
        title_font = QFont(self.font())
        title_font.setPointSizeF(max(9.5, self.font().pointSizeF() + 1.5))
        title_font.setWeight(QFont.Weight.DemiBold)
        detail_font = QFont(self.font())
        text_w = max(60.0, area.width() - 48)
        flags = int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)
        painter.setFont(title_font)
        title_rect = painter.boundingRect(QRectF(0, 0, text_w, 1000), flags, title)
        detail_rect = QRectF()
        if detail:
            painter.setFont(detail_font)
            detail_rect = painter.boundingRect(QRectF(0, 0, text_w, 1000), flags, detail)
        total = icon_px + 14 + title_rect.height() + ((6 + detail_rect.height()) if detail else 0)
        y = area.center().y() - total / 2
        x0 = area.center().x() - text_w / 2
        pm = icons.pixmap(icon_name, icon_color, icon_px, self.devicePixelRatioF())
        painter.drawPixmap(QPointF(area.center().x() - icon_px / 2, y), pm)
        y += icon_px + 14
        painter.setFont(title_font)
        painter.setPen(QColor(title_color))
        painter.drawText(QRectF(x0, y, text_w, title_rect.height() + 2), flags, title)
        if detail:
            y += title_rect.height() + 6
            painter.setFont(detail_font)
            painter.setPen(QColor(p.text_faint))
            painter.drawText(QRectF(x0, y, text_w, detail_rect.height() + 2), flags, detail)


# ------------------------------------------------------------------------------- slot
class _Slot:
    """One loaded media file: player + audio output + video sink and its seek state."""

    def __init__(self, panel: "PreviewPanel", source: MediaSource) -> None:
        self.panel = panel
        self.source_id = source.id
        self.path = source.path
        self.fps = source.fps if source.fps and source.fps > 0 else 30.0
        self.fd = 1.0 / self.fps
        self.has_video = bool(source.has_video)
        self.dead = False
        self.loaded = False
        self.ended = False
        self.want_play = False
        self.last_used = 0
        # seek state
        self.target: Optional[float] = None      # source seconds asked from the player
        self.pending = False                     # no frame of the current seek seen yet
        self.exact = False                       # paused seek: only the target frame may show
        self.shown = False                       # a frame of the current seek was displayed
        self.retried = False
        self.issue_wall = 0
        self.seg_id: Optional[str] = None        # segment the current seek was issued for
        self.queued: Optional[tuple[float, str]] = None
        # last frame that belongs to the current seek
        self.frame: Optional[QVideoFrame] = None
        self.frame_start = 0.0
        self.frame_end = 0.0

        self.player = QMediaPlayer(panel)
        self.sink = QVideoSink(panel)
        self.audio: Optional[QAudioOutput] = None
        try:
            self.audio = QAudioOutput(panel)
            self.audio.setMuted(True)
            self.player.setAudioOutput(self.audio)
        except Exception:                                    # no audio device / backend
            log.warning("Preview: audio output unavailable", exc_info=True)
            self.audio = None
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)
        self.player.setPlaybackRate(panel.rate())
        self.player.setSource(QUrl.fromLocalFile(self.path))

    # commands ---------------------------------------------------------------
    def seek(self, pos: float, play: bool, exact: bool, seg_id: Optional[str]) -> None:
        self.want_play = play
        self.exact = exact
        self.target = max(0.0, pos)
        self.seg_id = seg_id
        self.pending = self.has_video
        self.shown = False
        self.retried = False
        self.queued = None
        self.frame = None
        self.issue_wall = self.panel._now()
        if self.loaded:
            self._issue()

    def _issue(self) -> None:
        player = self.player
        if self.ended or player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            player.pause()
            self.ended = False
        ms = int(round((self.target or 0.0) * 1000.0))
        dur = player.duration()
        if dur > 0:
            ms = min(ms, max(0, dur - 1))
        player.setPosition(ms)
        if self.want_play:
            player.play()
        elif player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            player.pause()

    def set_playing(self, play: bool) -> None:
        self.want_play = play
        if not self.loaded or self.dead:
            return
        if play:
            self.exact = False
            self.player.play()
        elif self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()

    def set_audio(self, volume: float, muted: bool) -> None:
        if self.audio is not None:
            self.audio.setVolume(volume)
            self.audio.setMuted(muted)

    def at(self, pos: float) -> bool:
        """True when this (paused) slot already shows / is seeking the frame of ``pos``."""
        if self.target is None or self.ended or self.want_play:
            return False
        if self.pending or self.frame is None:
            return (self.queued is None
                    and _frame_index(self.target, self.fps) == _frame_index(pos, self.fps))
        return self.frame_start - _TS_EPS <= pos < self.frame_end - _TS_EPS

    def frame_has_target(self) -> bool:
        if self.frame is None or self.target is None:
            return False
        tol = 0.25 * self.fd
        return self.frame_start - tol <= self.target <= self.frame_end + tol

    def destroy(self) -> None:
        if self.dead:
            return
        self.dead = True
        self.frame = None
        for sig, fn in ((self.sink.videoFrameChanged, self._on_frame),
                        (self.player.mediaStatusChanged, self._on_status),
                        (self.player.errorOccurred, self._on_error)):
            try:
                sig.disconnect(fn)
            except (RuntimeError, TypeError):
                pass
        try:
            self.player.stop()
            self.player.setSource(QUrl())        # closes the file handle
            self.player.setVideoSink(None)
            self.player.setAudioOutput(None)
        except RuntimeError:
            pass
        for obj in (self.player, self.sink, self.audio):
            if obj is not None:
                try:
                    obj.deleteLater()
                except RuntimeError:
                    pass
        # Queued frame / status events of the multimedia threads may still be in flight
        # for this object: keep the Python object alive until they have drained.
        self.panel._bury(self)

    # player callbacks -------------------------------------------------------
    def _on_status(self, status) -> None:
        if getattr(self, "dead", True):
            return
        S = QMediaPlayer.MediaStatus
        if status in (S.LoadedMedia, S.BufferedMedia) and not self.loaded:
            self.loaded = True
            if self.has_video and not self.player.hasVideo():
                self.has_video = False
                self.pending = False
                self.panel._on_slot_no_video(self)
            self.player.pause()
            if self.target is not None:
                self.issue_wall = self.panel._now()
                self._issue()
            elif self.want_play:
                self.player.play()
        elif status == S.EndOfMedia:
            self.ended = True
            self.panel._on_slot_ended(self)
        elif status == S.InvalidMedia:
            self.panel._on_slot_error(self, "invalid media")

    def _on_error(self, error, text) -> None:
        if getattr(self, "dead", True) or error == QMediaPlayer.Error.NoError:
            return
        self.panel._on_slot_error(self, f"{getattr(error, 'name', error)}: {text}")

    def _on_frame(self, frame: QVideoFrame) -> None:
        if getattr(self, "dead", True) or not frame.isValid() or self.target is None:
            return
        start = frame.startTime() / 1e6
        end = frame.endTime() / 1e6
        if start < 0:
            start = self.player.position() / 1000.0
        if end <= start:
            end = start + self.fd
        if self.pending:
            lo = self.target - 1.25 * self.fd
            hi = self.target + (1.0 if self.want_play else 0.25 * self.fd)
            if not lo <= start <= hi:
                return                                      # frame of an older seek
            self.pending = False
        self.frame, self.frame_start, self.frame_end = frame, start, end
        self.panel._on_slot_frame(self)


# ------------------------------------------------------------------------- audio lane
class _AudioLane:
    """Audio-only player for the clips of the timeline's audio lane.

    One ``QMediaPlayer`` + ``QAudioOutput`` owned by the panel. The source is switched only
    when the clip under the playhead comes from a different file; ``target`` is the source
    second last commanded, ``seek_wall`` the wall time of that command (used to estimate the
    position while the player is still starting up).
    """

    def __init__(self, panel: "PreviewPanel") -> None:
        self.panel = panel
        self.dead = False
        self.source_id: Optional[str] = None
        self.path: Optional[str] = None
        self.seg_id: Optional[str] = None
        self.loaded = False
        self.ended = False
        self.want_play = False
        self.audible = False
        self.target: Optional[float] = None
        self.seek_wall = 0
        self.player = QMediaPlayer(panel)
        self.out: Optional[QAudioOutput] = None
        try:
            self.out = QAudioOutput(panel)
            self.out.setMuted(True)
            self.player.setAudioOutput(self.out)
        except Exception:                                    # no audio device / backend
            log.warning("Preview: audio-lane output unavailable", exc_info=True)
            self.out = None
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)
        self.player.setPlaybackRate(panel.rate())

    # source -----------------------------------------------------------------
    def bind(self, src: MediaSource) -> bool:
        """Load ``src`` unless it already is the bound file. False when it cannot be used."""
        if self.source_id == src.id and self.path == src.path:
            return True
        self.unbind()
        if not src.path or not os.path.exists(src.path):
            return False
        self.source_id, self.path = src.id, src.path
        self.player.setSource(QUrl.fromLocalFile(src.path))
        return True

    def unbind(self) -> None:
        self.source_id = self.path = self.seg_id = None
        self.loaded = self.ended = self.want_play = self.audible = False
        self.target = None
        if not self.dead:
            try:
                self.player.stop()
                self.player.setSource(QUrl())
            except RuntimeError:
                pass

    # commands ---------------------------------------------------------------
    def seek(self, pos: float, play: bool, now: int) -> None:
        self.target = max(0.0, pos)
        self.seek_wall = now
        self.ended = False
        self.want_play = play
        if self.loaded:
            self._issue()

    def _issue(self) -> None:
        player = self.player
        if player.playbackState() == QMediaPlayer.PlaybackState.StoppedState:
            player.pause()
        ms = int(round((self.target or 0.0) * 1000.0))
        dur = player.duration()
        if dur > 0:
            ms = min(ms, max(0, dur - 1))
        player.setPosition(ms)
        if self.want_play:
            player.play()
        elif player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            player.pause()

    def set_play(self, play: bool, now: int) -> None:
        if play == self.want_play:
            return
        if play:
            self.seek_wall = now
        elif self.loaded and self.target is not None:
            self.target = self.player.position() / 1000.0     # where it actually stopped
        self.want_play = play
        if not self.loaded:
            return
        if play:
            if not self.ended:
                self.player.play()
        elif self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()

    def silence(self) -> None:
        self.seg_id = None
        self.audible = False
        if self.out is not None:
            self.out.setMuted(True)
        if self.want_play:
            self.set_play(False, 0)

    def set_audio(self, volume: float, muted: bool) -> None:
        if self.out is not None:
            self.out.setVolume(volume)
            self.out.setMuted(muted)

    def estimate(self, now: int, rate: float) -> Optional[float]:
        """Best guess of the current source second; ``None`` while the file is opening."""
        if not self.loaded or self.target is None:
            return None
        if not self.want_play:
            return self.target
        if now - self.seek_wall < 400 or self.ended:
            return self.target + max(0, now - self.seek_wall) / 1000.0 * rate
        return self.player.position() / 1000.0

    def rebase(self, now: int, rate: float) -> None:
        """Called before the playback rate changes."""
        est = self.estimate(now, rate)
        if est is not None and self.want_play:
            self.target, self.seek_wall = est, now

    def destroy(self) -> None:
        if self.dead:
            return
        self.dead = True
        for sig, fn in ((self.player.mediaStatusChanged, self._on_status),
                        (self.player.errorOccurred, self._on_error)):
            try:
                sig.disconnect(fn)
            except (RuntimeError, TypeError):
                pass
        try:
            self.player.stop()
            self.player.setSource(QUrl())
            self.player.setAudioOutput(None)
        except RuntimeError:
            pass
        for obj in (self.player, self.out):
            if obj is not None:
                try:
                    obj.deleteLater()
                except RuntimeError:
                    pass
        self.panel._bury(self)

    # player callbacks -------------------------------------------------------
    def _on_status(self, status) -> None:
        if self.dead:
            return
        S = QMediaPlayer.MediaStatus
        if status in (S.LoadedMedia, S.BufferedMedia) and not self.loaded and self.source_id:
            self.loaded = True
            self.player.pause()
            self.target = None                  # let the panel re-issue a fresh position
            self.panel._on_lane_loaded()
        elif status == S.EndOfMedia:
            self.ended = True
        elif status == S.InvalidMedia:
            self.panel._on_lane_error(self, "invalid media")

    def _on_error(self, error, text) -> None:
        if self.dead or error == QMediaPlayer.Error.NoError:
            return
        self.panel._on_lane_error(self, f"{getattr(error, 'name', error)}: {text}")


# ------------------------------------------------------------------------------ panel
class PreviewPanel(QWidget):
    """Video viewport + transport bar. Plays the edited timeline of a ``Document``."""

    playingChanged = Signal(bool)
    #: test / diagnostics hook: every video frame put on screen with its timeline time
    frameDisplayed = Signal(QImage, float)

    def __init__(self, document, settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._doc = document
        self._settings = settings
        self._slots: list[_Slot] = []
        self._graveyard: list = []
        self._audio_lane: Optional[_AudioLane] = None
        self._audio_bad: set[tuple[str, str]] = set()      # (source id, path) the lane can't play
        self._audio_check_wall = 0
        self._active: Optional[_Slot] = None
        self._standby: Optional[_Slot] = None
        self._seg: Optional[Segment] = None
        self._mode = _MODE_IDLE
        self._playing = False
        self._rate = 1.0
        self._t = 0.0
        self._use = 0
        self._errors: dict[str, tuple[str, str]] = {}     # source id -> (path, message)
        self._no_video: set[str] = set()
        self._muted = False
        self._volume = min(1.0, max(0.0, float(getattr(settings, "volume", 1.0))))
        self._shut = False
        self._in_enter = False
        # playback clock
        self._wall = QElapsedTimer()
        self._wall.start()
        self._anchor_src = 0.0
        self._anchor_wall = 0
        self._frame_wall = -10_000
        self._last_pos = -1
        self._clock_t0 = 0.0
        self._clock_wall = 0
        self._clock_boundary = 0.0

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._scrub_timer = QTimer(self)
        self._scrub_timer.setSingleShot(True)
        self._scrub_timer.timeout.connect(self._flush_queued)
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.timeout.connect(self._on_settle_timeout)
        self._resync_timer = QTimer(self)
        self._resync_timer.setSingleShot(True)
        self._resync_timer.setInterval(0)
        self._resync_timer.timeout.connect(self._resync)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self._save_volume)

        self._build_ui()

        document.seekRequested.connect(self._on_seek_requested)
        document.timelineChanged.connect(self._on_timeline_changed)
        document.sourcesChanged.connect(self._on_sources_changed)
        document.projectReset.connect(self._on_project_reset)
        document.playheadChanged.connect(self._update_time_label)

        self._t = document.playhead
        self._update_time_label()
        self._update_enabled()
        self._resync_timer.start()

    # ================================================================== UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        self._viewport = _Viewport(self)
        root.addWidget(self._viewport, 1)

        bar = QWidget(self)
        bar.setFixedHeight(52)
        grid = QGridLayout(bar)
        grid.setContentsMargins(6, 4, 6, 4)
        grid.setHorizontalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)

        self._time_label = QLabel(bar)
        self._time_label.setProperty("role", "mono")
        self._time_label.setTextFormat(Qt.TextFormat.RichText)
        self._time_label.setMinimumWidth(190)
        self._time_label.setToolTip("Current time / total duration")
        grid.addWidget(self._time_label, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        centre = QHBoxLayout()
        centre.setContentsMargins(0, 0, 0, 0)
        centre.setSpacing(6)
        self._btn_start = self._transport_button("skip-start", "Go to start (Home)", self.go_to_start)
        self._btn_back = self._transport_button("step-back", "Previous frame (←)",
                                                lambda: self.step_frames(-1), repeat=True)
        self._btn_play = QToolButton(bar)
        self._btn_play.setProperty("variant", "transport-main")
        self._btn_play.setFixedSize(40, 40)
        self._btn_play.setIconSize(QSize(20, 20))
        self._btn_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_play.clicked.connect(self.toggle_play)
        self._btn_fwd = self._transport_button("step-forward", "Next frame (→)",
                                               lambda: self.step_frames(1), repeat=True)
        self._btn_end = self._transport_button("skip-end", "Go to end (End)", self.go_to_end)
        for i, b in enumerate((self._btn_start, self._btn_back, self._btn_play,
                               self._btn_fwd, self._btn_end)):
            if i in (2, 3):
                centre.addSpacing(4)
            centre.addWidget(b)
        grid.addLayout(centre, 0, 1, Qt.AlignmentFlag.AlignCenter)

        right = QHBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(6)
        self._rate_combo = QComboBox(bar)
        for r in RATES:
            self._rate_combo.addItem(f"{r:g}x", r)
        self._rate_combo.setCurrentIndex(RATES.index(1.0))
        self._rate_combo.setFixedWidth(82)
        self._rate_combo.setToolTip("Playback speed")
        self._rate_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._rate_combo.currentIndexChanged.connect(self._on_rate_combo)
        right.addWidget(self._rate_combo)
        right.addSpacing(6)
        self._btn_mute = QToolButton(bar)
        self._btn_mute.setFixedSize(32, 32)
        self._btn_mute.setIconSize(QSize(18, 18))
        self._btn_mute.setToolTip("Mute")
        self._btn_mute.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_mute.clicked.connect(self._toggle_mute)
        right.addWidget(self._btn_mute)
        self._vol_slider = QSlider(Qt.Orientation.Horizontal, bar)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setFixedWidth(88)
        self._vol_slider.setValue(int(round(self._volume * 100)))
        self._vol_slider.setToolTip("Volume")
        self._vol_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._vol_slider.valueChanged.connect(self._on_volume_changed)
        self._vol_slider.sliderReleased.connect(self._save_volume)
        right.addWidget(self._vol_slider)
        grid.addLayout(right, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        root.addWidget(bar)
        self._bar = bar
        self.refresh_theme()

    def _transport_button(self, icon_name: str, tip: str, slot, repeat: bool = False) -> QToolButton:
        b = QToolButton(self)
        b.setProperty("variant", "transport")
        b.setProperty("icon_name", icon_name)
        b.setFixedSize(36, 36)
        b.setIconSize(QSize(18, 18))
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if repeat:
            b.setAutoRepeat(True)
            b.setAutoRepeatDelay(350)
            b.setAutoRepeatInterval(70)
        b.clicked.connect(slot)
        return b

    def refresh_theme(self) -> None:
        """Rebuild themed icons (call after ``theme.apply_theme``)."""
        for b in (self._btn_start, self._btn_back, self._btn_fwd, self._btn_end):
            b.setIcon(icons.icon(b.property("icon_name"), size=18))
        self._update_play_icon()
        self._update_mute_icon()
        self._update_time_label()
        self._viewport.update()

    def _update_play_icon(self) -> None:
        p = theme.current()
        name = "pause" if self._playing else "play"
        self._btn_play.setIcon(icons.icon(name, p.accent_text, 20))
        self._btn_play.setToolTip("Pause (Space)" if self._playing else "Play (Space)")

    def _update_mute_icon(self) -> None:
        silent = self._muted or self._volume <= 0.0
        self._btn_mute.setIcon(icons.icon("volume-x" if silent else "volume", size=18))
        self._btn_mute.setToolTip("Unmute" if self._muted else "Mute")

    def _update_time_label(self, *_args) -> None:
        p = theme.current()
        dur = self._doc.duration
        hours = dur >= 3600
        cur = format_timecode(min(self._doc.playhead, dur) if dur > 0 else 0.0, show_hours=hours)
        total = format_timecode(dur, show_hours=hours)
        self._time_label.setText(
            f'<span style="color:{p.text}">{cur}</span>'
            f'<span style="color:{p.text_faint}"> / {total}</span>')

    def _update_enabled(self) -> None:
        has = not self._doc.timeline.is_empty()
        for b in (self._btn_start, self._btn_back, self._btn_play, self._btn_fwd, self._btn_end):
            b.setEnabled(has)

    # volume ---------------------------------------------------------------
    def _linear_volume(self) -> float:
        v = self._volume
        try:
            from PySide6.QtMultimedia import QtAudio
            return float(QtAudio.convertVolume(v, QtAudio.VolumeScale.LogarithmicVolumeScale,
                                               QtAudio.VolumeScale.LinearVolumeScale))
        except Exception:
            return v

    def _apply_audio(self) -> None:
        vol = self._linear_volume()
        for slot in self._slots:
            if slot is self._active and self._mode == _MODE_MEDIA and self._seg is not None:
                slot.set_audio(vol, self._muted or self._seg.muted or not self._playing)
            else:
                slot.set_audio(vol, True)
        self._sync_audio()

    def _on_volume_changed(self, value: int) -> None:
        self._volume = value / 100.0
        self._settings.volume = self._volume
        if value > 0 and self._muted:
            self._muted = False
        self._update_mute_icon()
        self._apply_audio()
        if not self._vol_slider.isSliderDown():
            self._save_timer.start()            # wheel / keyboard change: save a bit later

    def _save_volume(self) -> None:
        self._save_timer.stop()
        self._settings.volume = self._volume
        try:
            self._settings.save()
        except Exception:
            log.warning("Preview: could not save settings", exc_info=True)

    def _toggle_mute(self) -> None:
        self.set_muted(not self._muted)

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self._update_mute_icon()
        self._apply_audio()

    def is_muted(self) -> bool:
        return self._muted

    # ================================================================== public API
    def is_playing(self) -> bool:
        return self._playing

    def rate(self) -> float:
        return self._rate

    def play(self) -> None:
        if self._playing or self._shut:
            return
        dur = self._doc.duration
        if self._doc.timeline.is_empty() or dur <= 0:
            return
        self._resync_timer.stop()
        t = min(max(0.0, self._doc.playhead), dur)
        if t >= dur - _END_EPS:
            t = 0.0                              # play at the end restarts from the top
        self._set_playing(True)
        self._set_time(t, force=True)
        self._enter(t)

    def pause(self) -> None:
        if not self._playing:
            return
        self._set_playing(False)
        slot, seg = self._active, self._seg
        if self._mode == _MODE_MEDIA and slot is not None and seg is not None:
            slot.set_playing(False)
            if slot.frame is not None and not slot.pending and slot.shown:
                # keep the playhead on the frame that is actually on screen
                self._set_time(seg.to_timeline(min(max(slot.frame_start, seg.source_in),
                                                   seg.source_out)), force=True)
        self._apply_audio()

    def toggle_play(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()

    def step_frames(self, n: int) -> None:
        """Pause and move the playhead by ``n`` frames of the clip under it (1/30 s in a gap)."""
        self.pause()
        tl = self._doc.timeline
        dur = tl.duration
        if tl.is_empty() or n == 0:
            return
        t = min(max(0.0, self._doc.playhead), dur)
        seg, src_t = self._resolve(t, paused=True)
        if seg is None:
            new_t = t + n * GAP_FRAME
        else:
            src = self._doc.sources.get(seg.source_id)
            fps = src.fps if src and src.fps > 0 else 30.0
            k = _frame_index(src_t, fps)
            new_t = seg.to_timeline((k + n) / fps)
            if n > 0:
                new_t = max(new_t, t)
        self._doc.seek(min(max(0.0, new_t), dur))

    def set_rate(self, rate: float) -> None:
        rate = min(max(float(rate), 0.1), 8.0)
        if abs(rate - self._rate) < 1e-9:
            return
        now = self._now()
        if self._playing:                         # re-base both clocks at the old rate
            if self._mode == _MODE_CLOCK:
                self._clock_t0 = self._clock_time(now)
                self._clock_wall = now
            elif self._mode == _MODE_MEDIA:
                est = self._media_estimate(now)
                self._anchor_src, self._anchor_wall = est, now
        if self._audio_lane is not None and not self._audio_lane.dead:
            self._audio_lane.rebase(now, self._rate)
            self._audio_lane.player.setPlaybackRate(rate)
        self._rate = rate
        for slot in self._slots:
            slot.player.setPlaybackRate(rate)
        idx = self._rate_combo.findData(rate)
        self._rate_combo.blockSignals(True)
        if idx >= 0:
            self._rate_combo.setCurrentIndex(idx)
        else:
            self._rate_combo.setCurrentIndex(-1)
            self._rate_combo.setPlaceholderText(f"{rate:g}x")
        self._rate_combo.blockSignals(False)

    def shuttle(self, direction: int) -> None:
        """J/K/L. L (>0): play, repeated presses cycle 1x→2x→4x. K (0): pause, back to 1x.
        J (<0): pause and jump back one second per press (no true reverse playback)."""
        if direction > 0:
            if self._playing and self._rate in SHUTTLE_RATES:
                self.set_rate(SHUTTLE_RATES[(SHUTTLE_RATES.index(self._rate) + 1) % len(SHUTTLE_RATES)])
            else:
                self.set_rate(1.0)
                self.play()
        elif direction == 0:
            self.pause()
            self.set_rate(1.0)
        else:
            self.pause()
            self._doc.seek(max(0.0, self._doc.playhead - 1.0))

    def go_to_start(self) -> None:
        self._doc.seek(0.0)

    def go_to_end(self) -> None:
        self._doc.seek(self._doc.duration)

    def release_media(self) -> None:
        """Stop and unload every player so the files can be relinked / moved / deleted."""
        if self._playing:
            self._set_playing(False)
        self._settle_timer.stop()
        self._scrub_timer.stop()
        for slot in list(self._slots):
            slot.destroy()
        self._slots.clear()
        if self._audio_lane is not None:
            self._audio_lane.destroy()
            self._audio_lane = None
        self._audio_bad.clear()
        self._active = self._standby = None
        self._seg = None
        self._mode = _MODE_IDLE
        self._errors.clear()
        self._no_video.clear()
        if self._doc.timeline.is_empty():
            self._viewport.show_placeholder()
        else:
            self._viewport.show_black()

    def shutdown(self) -> None:
        if self._shut:
            return
        self._shut = True
        for timer in (self._timer, self._scrub_timer, self._settle_timer, self._resync_timer):
            timer.stop()
        if self._save_timer.isActive():
            self._save_volume()
        doc = self._doc
        for sig, fn in ((doc.seekRequested, self._on_seek_requested),
                        (doc.timelineChanged, self._on_timeline_changed),
                        (doc.sourcesChanged, self._on_sources_changed),
                        (doc.projectReset, self._on_project_reset),
                        (doc.playheadChanged, self._update_time_label)):
            try:
                sig.disconnect(fn)
            except (RuntimeError, TypeError):
                pass
        self.release_media()

    # diagnostics (also used by the tests) ------------------------------------
    def display_state(self) -> str:
        """``"placeholder"``, ``"black"``, ``"frame"`` or ``"message"``."""
        if self._viewport.state == _Viewport.BLACK and self._viewport.message:
            return "message"
        return self._viewport.state

    def status_message(self) -> str:
        return self._viewport.message

    def current_image(self) -> Optional[QImage]:
        return self._viewport.image()

    def loaded_source_ids(self) -> list[str]:
        return [s.source_id for s in self._slots]

    def audio_state(self) -> Optional[dict]:
        """State of the audio-lane player (``None`` when it does not exist): bound source,
        clip, commanded and actual source position (s), whether it is loaded / running and
        whether it is meant to be audible right now."""
        lane = self._audio_lane
        if lane is None or lane.dead:
            return None
        return {"source_id": lane.source_id, "seg_id": lane.seg_id, "target": lane.target,
                "position": lane.player.position() / 1000.0, "loaded": lane.loaded,
                "running": lane.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState,
                "audible": lane.audible}

    # ================================================================== document signals
    def _on_seek_requested(self, t: float) -> None:
        if self._shut:
            return
        self._resync_timer.stop()
        self._update_enabled()
        self._set_time(t, force=True, report=False)
        self._enter(t)

    def _on_timeline_changed(self) -> None:
        if self._shut:
            return
        self._update_time_label()
        self._update_enabled()
        self._resync_timer.start()

    def _on_sources_changed(self) -> None:
        if self._shut:
            return
        sources = self._doc.sources
        for slot in list(self._slots):
            src = sources.get(slot.source_id)
            if src is None or src.path != slot.path:
                self._drop_slot(slot)
        for sid in list(self._errors):
            src = sources.get(sid)
            if src is None or src.path != self._errors[sid][0]:
                del self._errors[sid]
        self._no_video &= set(sources)
        lane = self._audio_lane
        if lane is not None and lane.source_id is not None:
            src = sources.get(lane.source_id)
            if src is None or src.path != lane.path:
                lane.unbind()
        self._audio_bad = {(sid, p) for sid, p in self._audio_bad
                           if sid in sources and sources[sid].path == p}
        self._resync_timer.start()

    def _on_project_reset(self) -> None:
        if self._shut:
            return
        self.release_media()
        self._t = self._doc.playhead
        self._update_time_label()
        self._update_enabled()
        self._resync_timer.start()

    def _resync(self) -> None:
        """Timeline / sources changed under us: show what now belongs at the playhead."""
        if self._shut:
            return
        t = self._t if self._playing else self._doc.playhead
        self._enter(min(max(0.0, t), self._doc.duration))

    # ================================================================== engine
    def _now(self) -> int:
        return self._wall.elapsed()

    def _set_playing(self, playing: bool) -> None:
        if playing == self._playing:
            return
        self._playing = playing
        if playing:
            self._settle_timer.stop()
            self._scrub_timer.stop()
            self._timer.start()
        else:
            self._timer.stop()
            for slot in self._slots:
                slot.set_playing(False)
            self._apply_audio()
        self._update_play_icon()
        self.playingChanged.emit(playing)

    def _set_time(self, t: float, force: bool = False, report: bool = True) -> None:
        if not force and t < self._t:
            return
        self._t = t
        if report:
            self._doc.set_playhead(t)

    def _source_problem(self, src: Optional[MediaSource]) -> Optional[tuple[str, str]]:
        """``(message, detail)`` when ``src`` cannot be previewed, else ``None``."""
        if src is None:
            return ("Media offline", "The source of this clip is missing from the project")
        name = os.path.basename(src.path) or src.title or "file"
        cached = self._errors.get(src.id)
        if cached and cached[0] == src.path and os.path.exists(src.path):
            return (cached[1], name)
        if not src.path or not os.path.exists(src.path):
            return (f"Media offline: {name}", "Relink the file to see it in the preview")
        return None

    def _resolve(self, t: float, paused: bool) -> tuple[Optional[Segment], float]:
        """Segment + source time for timeline time ``t`` (``(None, 0)`` in a gap)."""
        tl = self._doc.timeline
        seg = tl.segment_at(t)
        if seg is None and paused and tl.segments and t >= tl.duration - _END_EPS:
            last = max(tl.segments, key=lambda s: s.timeline_end)   # parked at the very end:
            seg = last                                              # show the last frame
        if seg is None:
            return None, 0.0
        src = self._doc.sources.get(seg.source_id)
        fd = src.frame_duration if src else GAP_FRAME
        src_t = seg.to_source(t)
        src_t = min(src_t, seg.source_out - min(0.5 * fd, 0.5 * seg.duration))
        return seg, max(src_t, seg.source_in)

    @staticmethod
    def _frame_pos(src: MediaSource, src_t: float) -> float:
        """Player position that reliably yields the frame containing ``src_t``."""
        if not src.has_video or src.fps <= 0:
            return src_t
        return (_frame_index(src_t, src.fps) + 0.5) / src.fps

    def _touch(self, slot: _Slot) -> None:
        self._use += 1
        slot.last_used = self._use

    def _bury(self, slot: _Slot) -> None:
        """Hold a destroyed slot for a moment so late queued callbacks find a live object."""
        self._graveyard.append(slot)
        QTimer.singleShot(2000, lambda s=slot: self._graveyard.remove(s)
                          if s in self._graveyard else None)

    def _drop_slot(self, slot: _Slot) -> None:
        if slot in self._slots:
            self._slots.remove(slot)
        if slot is self._active:
            self._active = None
        if slot is self._standby:
            self._standby = None
        slot.destroy()

    def _obtain_slot(self, src: MediaSource, pos: float, keep: tuple) -> Optional[_Slot]:
        """A slot for ``src`` other than those in ``keep``: cued > same file > new > LRU."""
        same = [s for s in self._slots if s.source_id == src.id and s not in keep]
        for s in same:
            if s.at(pos):
                return s
        if same:
            return max(same, key=lambda s: (s is self._active, s.last_used))
        if len(self._slots) >= MAX_PLAYERS:
            victims = [s for s in self._slots if s not in keep and s is not self._active
                       and s is not self._standby] or [s for s in self._slots if s not in keep]
            if not victims:
                return None
            self._drop_slot(min(victims, key=lambda s: s.last_used))
        try:
            slot = _Slot(self, src)
        except Exception:
            log.exception("Preview: could not create a player for %s", src.path)
            self._errors[src.id] = (src.path, MSG_CODEC)
            return None
        self._slots.append(slot)
        return slot

    def _deactivate(self) -> None:
        slot = self._active
        self._active = None
        if slot is not None and not slot.dead:
            slot.set_audio(self._linear_volume(), True)
            slot.set_playing(False)

    def _enter(self, t: float) -> None:
        """Show (and, when playing, play from) timeline time ``t``."""
        if self._in_enter:                          # re-entered from a player callback
            QTimer.singleShot(0, self._resync)
            return
        self._in_enter = True
        try:
            self._enter_impl(t)
        finally:
            self._in_enter = False
        self._sync_audio()

    # audio lane -------------------------------------------------------------
    def _sync_audio(self, tick: bool = False) -> None:
        """Make the audio-lane player match the audio clip under the playhead.

        ``tick`` marks the periodic call from the playback timer: it only reacts to clip
        changes and (throttled) drift, never re-seeks right after its own seek."""
        if self._shut:
            return
        lane = self._audio_lane
        seg = self._doc.timeline.audio_at(self._t)
        src = self._doc.sources.get(seg.source_id) if seg is not None else None
        if seg is None or src is None or (src.id, src.path) in self._audio_bad:
            if lane is not None and not lane.dead and (lane.want_play or lane.audible):
                lane.silence()
            return
        audible = self._playing and not self._muted and not seg.muted
        now = self._now()
        check = True
        if tick:
            check = now - self._audio_check_wall >= 120
            if check:
                self._audio_check_wall = now
            if lane is not None and not check and lane.seg_id == seg.id and lane.audible == audible:
                return
        if lane is None or lane.dead:
            try:
                lane = _AudioLane(self)
            except Exception:
                log.exception("Preview: could not create the audio-lane player")
                self._audio_bad.add((src.id, src.path))
                return
            self._audio_lane = lane
        if not lane.bind(src):
            self._audio_bad.add((src.id, src.path))
            lane.silence()
            return
        target = min(max(seg.to_source(self._t), seg.source_in), seg.source_out)
        est = lane.estimate(now, self._rate)
        if lane.target is None:
            seek = True
        elif est is None:
            seek = False                                  # file still opening
        else:
            seek = abs(est - target) > (0.08 if self._playing else 0.004)
            if seek and tick and lane.seg_id == seg.id and (
                    lane.ended or now - lane.seek_wall < 500):
                seek = False                              # don't fight a seek in progress
        if seek:
            lane.seek(target, audible, now)
        else:
            lane.set_play(audible, now)
        lane.seg_id, lane.audible = seg.id, audible
        lane.set_audio(self._linear_volume(), not audible)

    def _on_lane_loaded(self) -> None:
        if not self._shut:
            self._sync_audio()

    def _on_lane_error(self, lane: _AudioLane, text: str) -> None:
        if lane.dead:
            return
        log.error("Preview: cannot play audio of %s (%s)", lane.path, text)
        if lane.source_id and lane.path:
            self._audio_bad.add((lane.source_id, lane.path))
        lane.unbind()

    def _enter_impl(self, t: float) -> None:
        tl = self._doc.timeline
        self._settle_timer.stop()
        self._scrub_timer.stop()
        if tl.is_empty():
            self._deactivate()
            self._seg, self._mode = None, _MODE_IDLE
            if self._playing:
                self._set_playing(False)
            self._set_time(0.0, force=True)
            self._viewport.show_placeholder()
            return
        dur = tl.duration
        t = min(max(0.0, t), dur)
        if self._playing and t >= dur - _END_EPS:
            self._finish(dur)
            return

        seg, src_t = self._resolve(t, paused=not self._playing)
        src = self._doc.sources.get(seg.source_id) if seg is not None else None
        problem = self._source_problem(src) if seg is not None else None

        slot: Optional[_Slot] = None
        pos = 0.0
        if seg is not None and problem is None:
            pos = self._frame_pos(src, src_t)
            current = self._active
            if (self._playing and current is not None and current.source_id == src.id
                    and self._mode == _MODE_MEDIA and current.want_play and current.loaded
                    and not current.ended and not current.pending
                    and abs(self._media_estimate(self._now()) - src_t) < 0.06):
                # same picture at the same moment (clip was split / muted / moved under
                # the running player): just re-bind, no seek, no hiccup
                self._seg = seg
                self._set_time(t, force=True)
                self._apply_audio()
                self._cue_next()
                return
            slot = self._obtain_slot(src, pos, keep=())
            if slot is None:
                problem = self._source_problem(src) or (MSG_CODEC, os.path.basename(src.path))

        if slot is None:                              # gap, offline or unplayable clip
            self._deactivate()
            self._seg, self._mode = seg, _MODE_CLOCK
            if problem:
                self._viewport.show_black(problem[0], problem[1])
            else:
                self._viewport.show_black()
            if seg is not None:
                boundary = seg.timeline_end
            else:
                nxt = tl.next_segment_after(t)
                boundary = nxt.timeline_start if nxt is not None else dur
            self._clock_t0, self._clock_wall = t, self._now()
            self._clock_boundary = max(boundary, t)
            self._set_time(t, force=True)
            if self._playing:
                self._cue_next()
            return

        if slot is not self._active:
            self._deactivate()
            self._active = slot
        if slot is self._standby:
            self._standby = None
        self._touch(slot)
        self._seg, self._mode = seg, _MODE_MEDIA
        self._set_time(t, force=True)
        now = self._now()
        self._anchor_src, self._anchor_wall = src_t, now
        self._frame_wall = -10_000
        self._last_pos = -1
        if src.id in self._no_video:
            self._viewport.show_black(MSG_CODEC, os.path.basename(src.path))

        if slot.at(pos):                               # cued / already there
            slot.seg_id = seg.id
            if self._playing:
                if not slot.pending and slot.frame is not None:
                    self._frame_wall = now
                    self._present(slot)                # first frame of the clip, right now
                slot.set_playing(True)
            elif not slot.pending and slot.frame is not None:
                slot.exact = True
                self._present(slot)
            else:
                self._settle_timer.start(_SETTLE_MS)
        elif (not self._playing and slot.loaded and slot.pending and not slot.want_play
              and now - slot.issue_wall < _SCRUB_COALESCE_MS):
            slot.queued = (pos, seg.id)                # a seek is in flight: latest wins
            self._scrub_timer.start(max(1, _SCRUB_COALESCE_MS - (now - slot.issue_wall)))
            self._settle_timer.start(_SETTLE_MS + _SCRUB_COALESCE_MS)
        else:
            slot.seek(pos, play=self._playing, exact=not self._playing, seg_id=seg.id)
            if not self._playing:
                self._settle_timer.start(_SETTLE_MS)
        self._apply_audio()
        if self._playing:
            self._cue_next()

    def _finish(self, dur: float) -> None:
        """Playback reached the end of the timeline: stop there, keep the last frame."""
        self._set_playing(False)
        self._set_time(dur, force=True)
        if self._mode != _MODE_MEDIA:
            self._enter_impl(dur)

    def _cue_next(self) -> None:
        """Pre-seek a paused standby player to the first frame of the upcoming clip."""
        tl = self._doc.timeline
        if self._mode == _MODE_MEDIA and self._seg is not None:
            after = self._seg.timeline_end
        elif self._mode == _MODE_CLOCK:
            after = self._clock_boundary
        else:
            return
        nxt = tl.segment_at(after) or tl.next_segment_after(after)
        cur = self._seg
        if nxt is None or (cur is not None and nxt.id == cur.id):
            return
        if (self._mode == _MODE_MEDIA and cur is not None and self._continues(cur, nxt)):
            return
        src = self._doc.sources.get(nxt.source_id)
        if src is None or self._source_problem(src) is not None:
            return
        pos = self._frame_pos(src, nxt.source_in)
        keep = (self._active,) if self._active is not None and self._mode == _MODE_MEDIA else ()
        slot = self._obtain_slot(src, pos, keep=keep)
        if slot is None:
            return
        self._touch(slot)
        if not slot.at(pos):
            slot.seek(pos, play=False, exact=True, seg_id=nxt.id)
        slot.set_audio(self._linear_volume(), True)
        self._standby = slot

    @staticmethod
    def _continues(cur: Segment, nxt: Segment) -> bool:
        """``nxt`` carries straight on in the same file (a split with nothing removed)."""
        return (nxt.source_id == cur.source_id
                and abs(nxt.source_in - cur.source_out) < 1e-4
                and abs(nxt.timeline_start - cur.timeline_end) < 1e-4)

    def _advance(self) -> None:
        """The active clip reached its out point while playing."""
        seg = self._seg
        if seg is None or not self._playing:
            return
        t_end = seg.timeline_end
        nxt = self._doc.timeline.segment_at(t_end)
        if (nxt is not None and nxt.id != seg.id and self._mode == _MODE_MEDIA
                and self._active is not None and not self._active.ended
                and self._continues(seg, nxt)):
            self._seg = nxt
            self._active.seg_id = nxt.id
            self._set_time(t_end)
            self._apply_audio()
            self._cue_next()
            slot = self._active
            if slot.frame is not None and slot.frame_start < nxt.source_out - _TS_EPS:
                self._present(slot)
            return
        self._set_time(t_end)
        self._enter(t_end)

    # clocks ---------------------------------------------------------------
    def _clock_time(self, now: int) -> float:
        return self._clock_t0 + (now - self._clock_wall) / 1000.0 * self._rate

    def _media_estimate(self, now: int) -> float:
        """Best guess of the active player's current source time."""
        slot = self._active
        if slot is None:
            return self._anchor_src
        if slot.pending or not slot.loaded:
            if slot.loaded and now - slot.issue_wall > _PENDING_TIMEOUT_MS:
                slot.pending = False                     # no picture is coming; move on
                self._anchor_wall = now
            else:
                self._anchor_wall = now                  # hold the clock while seeking
                return self._anchor_src
        if now - self._frame_wall <= _FRAME_CLOCK_MS:
            return self._anchor_src + (now - self._anchor_wall) / 1000.0 * self._rate
        # no recent video frame (audio-only, still picture, stall): follow the player
        pos = slot.player.position()
        if pos != self._last_pos:
            self._last_pos = pos
            p = pos / 1000.0
            if p >= self._anchor_src - 0.5:
                self._anchor_src, self._anchor_wall = max(p, self._anchor_src), now
        return self._anchor_src + min(now - self._anchor_wall, 150) / 1000.0 * self._rate

    def _tick(self) -> None:
        if not self._playing:
            return
        now = self._now()
        if self._mode == _MODE_MEDIA and self._seg is not None and self._active is not None:
            seg = self._seg
            est = self._media_estimate(now)
            self._set_time(min(max(seg.to_timeline(est), seg.timeline_start), seg.timeline_end))
            if est >= seg.source_out - 1e-4:
                self._advance()
        elif self._mode == _MODE_CLOCK:
            t = self._clock_time(now)
            if t >= self._clock_boundary - 1e-6:
                self._set_time(self._clock_boundary)
                self._enter(self._clock_boundary)
            else:
                self._set_time(t)
        else:
            self._enter(self._t)
        self._sync_audio(tick=True)

    # slot callbacks ---------------------------------------------------------
    def _on_slot_frame(self, slot: _Slot) -> None:
        if slot is self._active and self._mode == _MODE_MEDIA:
            self._present(slot)
            if not self._playing and slot.queued is not None:
                self._flush_queued()

    def _present(self, slot: _Slot) -> None:
        """Vet the slot's current frame against the active segment and show it."""
        seg = self._seg
        if seg is None or slot.frame is None or slot.seg_id != seg.id:
            return
        start, end = slot.frame_start, slot.frame_end
        if self._playing:
            if start >= seg.source_out - _TS_EPS:
                self._advance()                           # first frame past the out point
                return
            if end <= seg.source_in + _TS_EPS:
                return                                    # pre-roll frame before the in point
        else:
            if end <= seg.source_in + _TS_EPS or start >= seg.source_out - _TS_EPS:
                return
            if slot.exact and not slot.frame_has_target():
                return                                    # not the frame that was asked for
        image = slot.frame.toImage()
        if image.isNull():
            return
        slot.shown = True
        t_frame = seg.to_timeline(max(start, seg.source_in))
        self._viewport.show_frame(image)
        if self._playing:
            now = self._now()
            self._anchor_src, self._anchor_wall, self._frame_wall = max(start, seg.source_in), now, now
            self._set_time(t_frame)
        else:
            self._settle_timer.stop()
        self.frameDisplayed.emit(image, t_frame)

    def _flush_queued(self) -> None:
        slot = self._active
        if slot is None or slot.queued is None or self._playing:
            return
        pos, seg_id = slot.queued
        slot.seek(pos, play=False, exact=True, seg_id=seg_id)
        self._settle_timer.start(_SETTLE_MS)

    def _on_settle_timeout(self) -> None:
        """A paused seek did not produce its exact frame in time: degrade gracefully."""
        slot = self._active
        if self._playing or slot is None or self._mode != _MODE_MEDIA or slot.shown:
            return
        if slot.queued is not None:
            self._flush_queued()
            return
        if not slot.loaded:
            self._settle_timer.start(_SETTLE_MS)          # still opening the file
            return
        if slot.frame is not None and not slot.pending:
            slot.exact = False                            # nearest frame is better than none
            self._present(slot)
            if slot.shown:
                return
        if not slot.retried and slot.target is not None and slot.has_video:
            target, seg_id = max(0.0, slot.target - slot.fd), slot.seg_id
            log.debug("Preview: seek to %.3f s produced no frame, retrying one frame earlier",
                      slot.target)
            slot.seek(target, play=False, exact=False, seg_id=seg_id)
            slot.retried = True
            self._settle_timer.start(_SETTLE_MS)
            return
        if self._viewport.state == _Viewport.FRAME:
            self._viewport.show_black()                   # never leave a wrong frame up

    def _on_slot_ended(self, slot: _Slot) -> None:
        """The file ran out (before or exactly at the clip's out point)."""
        if slot is self._standby:
            self._standby = None
        if slot is not self._active or not self._playing or self._mode != _MODE_MEDIA:
            return
        seg = self._seg
        if seg is None:
            return
        # finish the clip on the wall clock, holding the last frame
        self._active = None
        slot.set_audio(self._linear_volume(), True)
        self._mode = _MODE_CLOCK
        self._clock_t0, self._clock_wall = self._t, self._now()
        self._clock_boundary = max(seg.timeline_end, self._t)

    def _on_slot_no_video(self, slot: _Slot) -> None:
        log.warning("Preview: no decodable video stream in %s", slot.path)
        self._no_video.add(slot.source_id)
        if slot is self._active:
            self._viewport.show_black(MSG_CODEC, os.path.basename(slot.path))

    def _on_slot_error(self, slot: _Slot, text: str) -> None:
        if slot.dead:
            return
        log.error("Preview: cannot play %s (%s)", slot.path, text)
        was_active = slot is self._active
        self._errors[slot.source_id] = (slot.path, MSG_CODEC)
        self._drop_slot(slot)
        if was_active and not self._shut:
            QTimer.singleShot(0, self._resync)

    def _on_rate_combo(self, index: int) -> None:
        rate = self._rate_combo.itemData(index)
        if rate:
            self.set_rate(float(rate))
