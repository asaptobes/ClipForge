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

from PySide6.QtCore import QElapsedTimer, QEvent, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QPainter,
                           QPainterPath, QPen)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QSizePolicy,
                               QSlider, QToolButton, QToolTip, QVBoxLayout, QWidget)

from ..core.model import POS_LIMIT, SCALE_MAX, SCALE_MIN, MediaSource, Segment
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


# ------------------------------------------------------------------ canvas geometry (pure)
ASPECT_RATIOS = {"9:16": 9 / 16, "4:5": 4 / 5, "1:1": 1.0, "16:9": 16 / 9}
CROP_STEP = 0.02            # Alt+Left/Right nudge
CROP_STEP_BIG = 0.10        # with Shift
_CANVAS_MARGIN = 10         # space around the canvas inside the viewport
_PILL_STRIP = 44            # room reserved above the canvas for the reframe pill (fill mode)
_BLUR_DIV = 16              # blurred background is built at 1/16 of the canvas size
_BLUR_DIM = 115             # darkening (0..255) over the blurred background

REFRAME_TIP = ("Reframe the clip under the playhead (or every selected video clip when "
               "several are selected). Drag the picture or the mini-map; Alt+Left/Right nudges, "
               "Shift for bigger steps, Esc cancels a drag.")


def canvas_ratio(aspect: str) -> Optional[float]:
    """Width / height of an output aspect (``None`` for ``"source"`` or unknown names)."""
    return ASPECT_RATIOS.get(aspect)


def fit_rect(area: QRectF, ratio: float) -> QRectF:
    """Largest rectangle of ``ratio`` (w/h) centred inside ``area``."""
    if area.width() <= 0 or area.height() <= 0 or ratio <= 0:
        return QRectF(area.center(), area.center())
    w = min(area.width(), area.height() * ratio)
    h = w / ratio
    return QRectF(area.x() + (area.width() - w) / 2, area.y() + (area.height() - h) / 2, w, h)


def contain_rect(src_w: float, src_h: float, dest: QRectF) -> QRectF:
    """``src_w`` x ``src_h`` scaled to fit entirely inside ``dest`` and centred."""
    if src_w <= 0 or src_h <= 0:
        return QRectF(dest)
    return fit_rect(dest, src_w / src_h)


def crop_rect(src_w: float, src_h: float, ratio: float, crop_x: float) -> QRectF:
    """The part of a ``src_w`` x ``src_h`` frame that fills a canvas of ``ratio`` (w/h).

    A source wider than the canvas is cropped horizontally with its left edge at
    ``crop_x`` (0..1) of the free travel; a narrower one is cropped vertically, centred."""
    if src_w <= 0 or src_h <= 0 or ratio <= 0:
        return QRectF(0, 0, max(0.0, src_w), max(0.0, src_h))
    cx = min(max(crop_x, 0.0), 1.0)
    if src_w / src_h >= ratio:
        w = src_h * ratio
        return QRectF((src_w - w) * cx, 0.0, w, src_h)
    h = src_w / ratio
    return QRectF(0.0, (src_h - h) / 2, src_w, h)


def pan_crop_x(seg: Segment, source_time: float) -> float:
    """Crop position of ``seg`` at a source time (static, or linearly interpolated pan)."""
    return seg.crop_x_at(source_time)


# ------------------------------------------------------- overlay geometry (pure)
HANDLE_SIZE = 9.0           # painted corner handle (px)
HANDLE_HIT = 8.0            # half-size of the corner hit area (px)
CORNERS = ("tl", "tr", "bl", "br")


def overlay_rect(canvas: QRectF, aspect: float, pos_x: float, pos_y: float,
                 scale: float) -> QRectF:
    """Where an overlay clip lands on ``canvas`` (same geometry as the export): width is
    ``scale`` x canvas width, the height follows the source ``aspect`` (w/h) and the centre
    sits at ``(0.5 + pos_x, 0.5 + pos_y)`` of the canvas."""
    aspect = aspect if aspect and aspect > 0 else 16 / 9
    w = scale * canvas.width()
    h = w / aspect
    cx = canvas.x() + canvas.width() * (0.5 + pos_x)
    cy = canvas.y() + canvas.height() * (0.5 + pos_y)
    return QRectF(cx - w / 2, cy - h / 2, w, h)


def corner_point(rect: QRectF, corner: str) -> QPointF:
    return QPointF(rect.left() if corner in ("tl", "bl") else rect.right(),
                   rect.top() if corner in ("tl", "tr") else rect.bottom())


def hit_test_overlay(rect: QRectF, pos: QPointF, tol: float = HANDLE_HIT) -> Optional[str]:
    """``"tl" | "tr" | "bl" | "br"`` for a corner handle, ``"body"`` inside the frame,
    ``None`` elsewhere. Corners win over the body."""
    best, best_d = None, None
    for c in CORNERS:
        p = corner_point(rect, c)
        dx, dy = abs(pos.x() - p.x()), abs(pos.y() - p.y())
        if dx <= tol and dy <= tol:
            d = dx * dx + dy * dy
            if best_d is None or d < best_d:
                best, best_d = c, d
    if best is not None:
        return best
    return "body" if rect.contains(pos) else None


def moved_position(base_x: float, base_y: float, dx_px: float, dy_px: float,
                   canvas: QRectF) -> tuple[float, float]:
    """New ``(pos_x, pos_y)`` after dragging the body by a pixel delta (clamped)."""
    if canvas.width() <= 0 or canvas.height() <= 0:
        return base_x, base_y
    x = base_x + dx_px / canvas.width()
    y = base_y + dy_px / canvas.height()
    return (min(max(x, -POS_LIMIT), POS_LIMIT), min(max(y, -POS_LIMIT), POS_LIMIT))


def resized_scale(base_scale: float, rect: QRectF, corner: str, pos: QPointF) -> float:
    """New scale after dragging ``corner`` of ``rect`` to ``pos``. The centre and aspect stay;
    the scale follows the cursor's distance from the centre (clamped to the model limits)."""
    c = rect.center()
    p0 = corner_point(rect, corner)
    d0 = math.hypot(p0.x() - c.x(), p0.y() - c.y())
    if d0 < 1e-6:
        return base_scale
    d1 = math.hypot(pos.x() - c.x(), pos.y() - c.y())
    return min(max(base_scale * d1 / d0, SCALE_MIN), SCALE_MAX)


def source_aspect(src: Optional[MediaSource]) -> float:
    """Display aspect (w/h) of a source, 16:9 when unknown."""
    if src is not None and src.width > 0 and src.height > 0:
        return src.width / src.height
    return 16 / 9


class _BlurCache:
    """Tiny blurred copy of the last frame (cheap blur: down to 1/16 and smooth back up)."""

    def __init__(self) -> None:
        self._key: Optional[tuple] = None
        self._img: Optional[QImage] = None

    def get(self, image: QImage, ratio: float, canvas_w: float, canvas_h: float) -> QImage:
        sw = max(8, int(round(canvas_w / _BLUR_DIV)))
        sh = max(8, int(round(canvas_h / _BLUR_DIV)))
        key = (image.cacheKey(), image.width(), image.height(), sw, sh)
        if key != self._key or self._img is None:
            r = crop_rect(image.width(), image.height(), ratio, 0.5).toAlignedRect()
            r = r.intersected(image.rect())
            self._img = image.copy(r).scaled(sw, sh, Qt.AspectRatioMode.IgnoreAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation)
            self._key = key
        return self._img


def paint_canvas_frame(painter: QPainter, canvas: QRectF, image: QImage, mode: str,
                       crop_x: float, blur: Optional[_BlurCache] = None) -> None:
    """Draw ``image`` into ``canvas`` the way the export frames it (fill / blur / fit)."""
    painter.fillRect(canvas, QColor(0, 0, 0))
    if image.isNull() or canvas.width() <= 0 or canvas.height() <= 0:
        return
    iw, ih = image.width(), image.height()
    ratio = canvas.width() / canvas.height()
    painter.save()
    painter.setClipRect(canvas, Qt.ClipOperation.IntersectClip)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if mode == "fill":
        painter.drawImage(canvas, image, crop_rect(iw, ih, ratio, crop_x))
    elif mode == "blur":
        bg = (blur or _BlurCache()).get(image, ratio, canvas.width(), canvas.height())
        painter.drawImage(canvas, bg)
        painter.fillRect(canvas, QColor(0, 0, 0, _BLUR_DIM))
        painter.drawImage(contain_rect(iw, ih, canvas), image)
    else:                                                    # "fit"
        painter.drawImage(contain_rect(iw, ih, canvas), image)
    painter.restore()


def render_canvas(image: QImage, width: int, height: int, mode: str, crop_x: float = 0.5) -> QImage:
    """The canvas picture as an image of ``width`` x ``height`` (used by tests / tools)."""
    out = QImage(max(1, width), max(1, height), QImage.Format.Format_RGB32)
    painter = QPainter(out)
    paint_canvas_frame(painter, QRectF(0, 0, out.width(), out.height()), image, mode, crop_x)
    painter.end()
    return out


# ------------------------------------------------------------------ captions (pure + paint)
def active_caption(captions, t: float):
    """The caption line shown at timeline time ``t`` (``start <= t < end``; the latest
    starting one when lines overlap), or ``None``."""
    found = None
    for ln in captions:
        if ln.start <= t < ln.end:
            found = ln
    return found


def active_word(line, t: float, hold: float = 0.3) -> int:
    """Index into ``line.words`` of the word being spoken at ``t`` (``-1`` when none).
    A short silence between two words keeps the previous one lit so the colour never blinks."""
    best = -1
    for i, w in enumerate(line.words):
        if w.start <= t < w.end:
            return i
        if w.end <= t < w.end + hold and (i + 1 >= len(line.words) or t < line.words[i + 1].start):
            best = i
    return best


def split_rows(widths: list[float], space: float, max_w: float) -> Optional[list[list[int]]]:
    """Split word widths into at most two balanced rows no wider than ``max_w``.
    ``None`` when that is impossible."""
    n = len(widths)
    if n == 0:
        return [[]]

    def span(a: int, b: int) -> float:
        return sum(widths[a:b]) + space * max(0, b - a - 1)

    if span(0, n) <= max_w:
        return [list(range(n))]
    best, best_w = None, 0.0
    for k in range(1, n):
        w1, w2 = span(0, k), span(k, n)
        if w1 <= max_w and w2 <= max_w and (best is None or max(w1, w2) < best_w):
            best, best_w = k, max(w1, w2)
    if best is None:
        return None
    return [list(range(best)), list(range(best, n))]


class CaptionLayout:
    """Everything needed to paint one caption: row boxes and per-word glyph paths, laid out
    once so only colours change from word to word (no jitter)."""

    def __init__(self) -> None:
        self.width = 0.0
        self.height = 0.0
        self.px = 0.0
        self.rows: list[QRectF] = []
        self.paths: list[QPainterPath] = []
        self.timing: list[int] = []            # token -> index into line.words (-1 = untimed)
        self.stroke = QPainterPath()
        self.texts: list[str] = []
        self.row_of: list[int] = []


_CAPTION_CACHE: dict[tuple, CaptionLayout] = {}


def caption_font(style, px: float) -> QFont:
    family = style.font if style.font and QFontDatabase.hasFamily(style.font) else "Segoe UI"
    font = QFont(family)
    font.setPixelSize(max(4, int(round(px))))
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def layout_caption(line, style, canvas_w: float, canvas_h: float) -> CaptionLayout:
    """Cached layout of ``line`` for a canvas of the given size (<= 2 rows within 90 % width)."""
    key = (line.text, tuple(w.text for w in line.words), style.font, round(style.size_pct, 3),
           bool(style.uppercase), int(round(canvas_w)), int(round(canvas_h)))
    hit = _CAPTION_CACHE.get(key)
    if hit is not None:
        return hit
    if len(_CAPTION_CACHE) > 96:
        _CAPTION_CACHE.clear()
    tokens: list[str] = []
    timing: list[int] = []
    if line.words:
        for i, w in enumerate(line.words):
            if w.text.strip():
                tokens.append(w.text.strip())
                timing.append(i)
    else:
        tokens = line.text.split()
        timing = [-1] * len(tokens)
    if style.uppercase:
        tokens = [t.upper() for t in tokens]
    lay = CaptionLayout()
    lay.timing, lay.texts = timing, tokens
    base_px = max(4.0, style.size_pct / 100.0 * canvas_h)
    max_w = 0.9 * canvas_w
    scale = 1.0
    while True:
        font = caption_font(style, base_px * scale)
        fm = QFontMetricsF(font)
        widths = [fm.horizontalAdvance(t) for t in tokens]
        space = fm.horizontalAdvance(" ")
        rows = split_rows(widths, space, max_w)
        if rows is not None or base_px * scale < 7:
            break
        scale *= 0.92
    if rows is None:                                           # cannot fit: balanced best effort
        half = max(1, (len(tokens) + 1) // 2)
        rows = [list(range(half)), list(range(half, len(tokens)))] if len(tokens) > 1 else [[0]]
        rows = [r for r in rows if r]
    lay.px = base_px * scale
    pitch = fm.height()
    row_w = [sum(widths[i] for i in r) + space * max(0, len(r) - 1) for r in rows]
    lay.width = max(row_w, default=0.0)
    lay.height = pitch * len(rows)
    lay.paths = [QPainterPath() for _ in tokens]
    lay.row_of = [0] * len(tokens)
    for ri, r in enumerate(rows):
        x = (lay.width - row_w[ri]) / 2
        lay.rows.append(QRectF(x, ri * pitch, row_w[ri], pitch))
        for i in r:
            lay.paths[i].addText(QPointF(x, ri * pitch + fm.ascent()), font, tokens[i])
            lay.row_of[i] = ri
            x += widths[i] + space
    for path in lay.paths:
        lay.stroke.addPath(path)
    _CAPTION_CACHE[key] = lay
    return lay


def caption_origin(style, rect: QRectF, lay: CaptionLayout) -> QPointF:
    """Top-left of the caption block inside ``rect`` for the style's position / margin."""
    margin = style.margin_pct / 100.0 * rect.height()
    if style.position == "top":
        y = rect.top() + margin
    elif style.position == "middle":
        y = rect.center().y() - lay.height / 2
    else:
        y = rect.bottom() - margin - lay.height
    return QPointF(rect.center().x() - lay.width / 2, y)


def paint_caption(painter: QPainter, rect: QRectF, line, style, t: float) -> None:
    """Draw ``line`` (as it looks at time ``t``) over ``rect`` (the canvas / video area)."""
    if rect.width() < 8 or rect.height() < 8 or not (line.text.strip() or line.words):
        return
    lay = layout_caption(line, style, rect.width(), rect.height())
    if not lay.paths:
        return
    origin = caption_origin(style, rect, lay)
    primary, highlight = QColor(style.primary), QColor(style.highlight)
    outline = QColor(style.outline)
    if not primary.isValid():
        primary = QColor("#FFFFFF")
    if not highlight.isValid():
        highlight = primary
    if not outline.isValid():
        outline = QColor("#000000")
    lit = -1
    if style.highlight_words and line.words:
        lit = active_word(line, t)
    painter.save()
    painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(origin)
    if style.box:
        box = QColor(outline)
        box.setAlpha(170)
        padx, pady, rad = 0.45 * lay.px, 0.05 * lay.px, 0.3 * lay.px
        boxes = QPainterPath()
        for r in lay.rows:
            one = QPainterPath()
            one.addRoundedRect(r.adjusted(-padx, -pady, padx, pady), rad, rad)
            boxes = boxes.united(one)
        painter.fillPath(boxes, box)
    out_px = style.outline_pct / 100.0 * rect.height()
    if out_px > 0.3:
        pen = QPen(outline, 2.0 * out_px, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                   Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(lay.stroke)
    painter.setPen(Qt.PenStyle.NoPen)
    for i, path in enumerate(lay.paths):
        painter.fillPath(path, highlight if (lit >= 0 and lay.timing[i] == lit) else primary)
    painter.restore()


# ------------------------------------------------------------------------- reframe pill
class _ReframePill(QWidget):
    """Floating segmented control: ``Static | Pan`` (+ ``Start | End`` while panning)."""

    modeRequested = Signal(str)        # "static" | "pan"
    editEndChanged = Signal(str)       # "start" | "end"
    HEIGHT = 28

    _TIPS = {"static": "One fixed crop position for the whole clip",
             "pan": "Pan the crop from a start to an end position over the clip",
             "start": "Drag edits where the pan starts", "end": "Drag edits where the pan ends"}

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._pan = False
        self._end = "start"
        self._hover: Optional[str] = None
        self._cells: list[tuple[str, QRectF, str]] = []
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(REFRAME_TIP)
        self.hide()
        self._relayout()

    def set_state(self, pan: bool, end: str) -> None:
        if pan != self._pan or end != self._end:
            self._pan, self._end = pan, end
            self._relayout()
        self.update()

    def _pill_font(self) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, self.font().pointSizeF() - 0.5))
        f.setWeight(QFont.Weight.DemiBold)
        return f

    def _relayout(self) -> None:
        fm = QFontMetricsF(self._pill_font())
        items = [("static", "Static"), ("pan", "Pan")]
        if self._pan:
            items += [("start", "Start"), ("end", "End")]
        x, self._cells = 4.0, []
        for i, (key, label) in enumerate(items):
            if i == 2:
                x += 8.0                                   # gap between the two groups
            w = fm.horizontalAdvance(label) + 20
            self._cells.append((key, QRectF(x, 3, w, self.HEIGHT - 6), label))
            x += w + 2
        self.resize(int(math.ceil(x + 2)), self.HEIGHT)

    def _active(self, key: str) -> bool:
        if key in ("static", "pan"):
            return (key == "pan") == self._pan
        return key == self._end

    def _cell_at(self, pos: QPointF) -> Optional[str]:
        for key, rect, _ in self._cells:
            if rect.contains(pos):
                return key
        return None

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QColor(p.panel)
        body.setAlpha(235)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(p.border_strong), 1))
        painter.setBrush(body)
        painter.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        painter.setFont(self._pill_font())
        for key, rect, label in self._cells:
            active = self._active(key)
            if active:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(p.accent))
                painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            elif key == self._hover:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(p.raised_hover))
                painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            painter.setPen(QColor(p.accent_text if active else p.text_dim if key != self._hover
                                  else p.text))
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), label)
        painter.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        hover = self._cell_at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._hover = None
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        key = self._cell_at(event.position())
        if event.button() != Qt.MouseButton.LeftButton or key is None:
            event.accept()
            return
        if key in ("static", "pan"):
            if not self._active(key):
                self.modeRequested.emit(key)
        elif key != self._end:
            self.editEndChanged.emit(key)
        event.accept()

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.ToolTip:
            key = self._cell_at(e.pos().toPointF())
            QToolTip.showText(e.globalPos(), self._TIPS.get(key, "") + (
                "\n\n" + REFRAME_TIP if key in ("static", "pan") else ""), self)
            return True
        return super().event(e)


# --------------------------------------------------------------------------- viewport
class _Viewport(QWidget):
    """Custom-painted, letterboxed video surface with placeholder / message states.

    With a social canvas (``aspect != "source"``) the frame is drawn into a canvas rectangle
    exactly like the export frames it, with the reframe handle and the caption overlay on top."""

    PLACEHOLDER, BLACK, FRAME = "placeholder", "black", "frame"

    #: a reframe edit was finished: (``"start"`` | ``"end"``, crop position 0..1)
    cropCommitted = Signal(str, float)
    modeRequested = Signal(str)          # "static" | "pan" (from the pill)
    #: overlay handle drag: began / live (clip id, pos_x, pos_y, scale) / finished / aborted
    transformBegan = Signal()
    transformChanged = Signal(str, float, float, float)
    transformEnded = Signal()
    transformCancelled = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._overlays: list[tuple[Segment, Optional[QImage], float]] = []
        self._selected_overlay: Optional[str] = None
        self._ratio_hint: Optional[float] = None
        self._tdrag: Optional[dict] = None
        self._image: Optional[QImage] = None
        self._state = self.PLACEHOLDER
        self._message = ""
        self._detail = ""
        self._doc = None
        self._aspect = "source"
        self._mode = "fill"
        self._blur = _BlurCache()
        self._seg: Optional[Segment] = None          # copy of the clip the frame belongs to
        self._src_t = 0.0
        self._interactive = True
        self._edit_end = "start"
        self._drag: Optional[dict] = None
        self._caption_key: Optional[tuple] = None
        self.setMinimumSize(240, 135)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setMouseTracking(True)
        self._pill = _ReframePill(self)
        self._pill.modeRequested.connect(self.modeRequested)
        self._pill.editEndChanged.connect(self._set_edit_end)

    # state ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def message(self) -> str:
        return self._message

    def image(self) -> Optional[QImage]:
        return self._image if self._state == self.FRAME else None

    def set_document(self, doc) -> None:
        self._doc = doc
        self.set_canvas(doc.aspect, doc.reframe_mode)

    def show_placeholder(self) -> None:
        self._state, self._image, self._message, self._detail = self.PLACEHOLDER, None, "", ""
        self._clear_clip()
        self.update()

    def show_black(self, message: str = "", detail: str = "") -> None:
        self._state, self._image = self.BLACK, None
        self._message, self._detail = message, detail
        self._clear_clip()
        self.update()

    def show_frame(self, image: QImage, seg: Optional[Segment] = None, src_t: float = 0.0) -> None:
        """Show ``image``; ``seg`` / ``src_t`` say which clip and source time it belongs to
        (used for the crop position of a social canvas)."""
        self._state, self._image, self._message, self._detail = self.FRAME, image, "", ""
        if self._drag is not None and (seg is None or self._seg is None or seg.id != self._seg.id):
            self.cancel_drag()
        if self._drag is None:
            self._seg = seg.copy() if seg is not None else None
        self._src_t = src_t
        self._update_handle_ui()
        self.update()

    def _clear_clip(self) -> None:
        self.cancel_drag()
        self._seg = None
        self._update_handle_ui()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return QSize(640, 360)

    # canvas ------------------------------------------------------------------
    def set_canvas(self, aspect: str, mode: str) -> None:
        if (aspect, mode) != (self._aspect, self._mode):
            self.cancel_drag()
            self.cancel_transform()
            self._aspect, self._mode = aspect, mode
        self._update_handle_ui()
        self.update()

    def canvas_rect(self) -> Optional[QRectF]:
        """The canvas rectangle in widget coordinates (``None`` for the source aspect, an
        empty project or the placeholder)."""
        ratio = canvas_ratio(self._aspect)
        if ratio is None or self._state == self.PLACEHOLDER:
            return None
        top = _PILL_STRIP if self._mode == "fill" else _CANVAS_MARGIN
        area = QRectF(self.rect()).adjusted(_CANVAS_MARGIN, top, -_CANVAS_MARGIN, -_CANVAS_MARGIN)
        r = fit_rect(area, ratio)
        if r.width() < 4 or r.height() < 4:
            return None
        # snap to whole pixels so the frame line is crisp
        return QRectF(round(r.x()), round(r.y()), max(1.0, round(r.width())),
                      max(1.0, round(r.height())))

    def video_rect(self) -> QRectF:
        """Where the current picture is drawn, in widget coordinates (the canvas when a
        social aspect is active, otherwise the letterboxed frame)."""
        canvas = self.canvas_rect()
        if canvas is not None:
            return canvas
        area = QRectF(self.rect())
        if self._image is None or self._image.isNull():
            return area
        iw, ih = self._image.width(), self._image.height()
        if iw <= 0 or ih <= 0:
            return area
        scale = min(area.width() / iw, area.height() / ih)
        w, h = iw * scale, ih * scale
        return QRectF(area.x() + (area.width() - w) / 2, area.y() + (area.height() - h) / 2, w, h)

    def current_crop_x(self) -> float:
        seg = self._seg
        return pan_crop_x(seg, self._src_t) if seg is not None else 0.5

    # overlays ------------------------------------------------------------------
    def set_overlays(self, items: list[tuple[Segment, Optional[QImage], float]],
                     selected: Optional[str] = None, ratio_hint: Optional[float] = None) -> None:
        """Overlay clips to composite, bottom-to-top: ``(clip copy, picture or None, aspect)``.
        ``selected`` is the overlay that carries the transform handles."""
        self._overlays = list(items)
        ids = {seg.id for seg, _img, _a in self._overlays}
        self._selected_overlay = selected if selected in ids else None
        self._ratio_hint = ratio_hint
        if self._tdrag is not None and self._tdrag["id"] not in ids:
            self.cancel_transform()
        self.update()

    def overlays(self) -> list[tuple[Segment, Optional[QImage], float]]:
        return list(self._overlays)

    def compose_rect(self) -> Optional[QRectF]:
        """The rectangle overlays are composited on: the canvas, or with the source aspect
        the picture's own letterboxed rectangle (black base of the right shape in a gap)."""
        canvas = self.canvas_rect()
        if canvas is not None or not self._overlays or self._state == self.PLACEHOLDER:
            return canvas
        if self._state == self.FRAME and self._image is not None and not self._image.isNull():
            return self.video_rect()
        ratio = self._ratio_hint or self._overlays[0][2]
        area = QRectF(self.rect()).adjusted(_CANVAS_MARGIN, _CANVAS_MARGIN,
                                            -_CANVAS_MARGIN, -_CANVAS_MARGIN)
        r = fit_rect(area, ratio)
        if r.width() < 4 or r.height() < 4:
            return None
        return QRectF(round(r.x()), round(r.y()), max(1.0, round(r.width())),
                      max(1.0, round(r.height())))

    def overlay_frame(self, overlay_id: Optional[str] = None) -> Optional[QRectF]:
        """Widget-space rectangle of an overlay (default: the selected one)."""
        want = overlay_id or self._selected_overlay
        comp = self.compose_rect()
        if want is None or comp is None:
            return None
        for seg, _img, aspect in self._overlays:
            if seg.id == want:
                return overlay_rect(comp, aspect, seg.pos_x, seg.pos_y, seg.scale)
        return None

    def transform_active(self) -> bool:
        return (self._interactive and self._selected_overlay is not None
                and self.overlay_frame() is not None)

    def is_transforming(self) -> bool:
        return self._tdrag is not None

    def cancel_transform(self) -> None:
        """Abort an overlay drag (the panel restores the model)."""
        if self._tdrag is None:
            return
        self._tdrag = None
        self.releaseKeyboard()
        self.unsetCursor()
        self.transformCancelled.emit()
        self.update()

    def _overlay_cursor(self, pos: Optional[QPointF]) -> bool:
        """Set the cursor for the overlay handles; False when the pointer is not on them."""
        if not self.transform_active():
            return False
        if self._tdrag is not None:
            kind = self._tdrag["kind"]
        else:
            frame = self.overlay_frame()
            kind = hit_test_overlay(frame, pos) if (pos is not None and frame is not None) else None
        if kind is None:
            return False
        shapes = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
                  "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
                  "body": Qt.CursorShape.SizeAllCursor}
        self.setCursor(shapes[kind])
        return True

    def _transform_press(self, event) -> bool:
        if event.button() != Qt.MouseButton.LeftButton or not self.transform_active():
            return False
        frame, comp = self.overlay_frame(), self.compose_rect()
        kind = hit_test_overlay(frame, event.position()) if frame is not None else None
        if kind is None or comp is None:
            return False
        seg = next((s for s, _i, _a in self._overlays if s.id == self._selected_overlay), None)
        if seg is None:
            return False
        self._tdrag = {"id": seg.id, "kind": kind, "origin": QPointF(event.position()),
                       "frame": QRectF(frame), "comp": QRectF(comp),
                       "base": (seg.pos_x, seg.pos_y, seg.scale)}
        self.grabKeyboard()                       # so that Esc reaches us while the button is down
        self.transformBegan.emit()
        self._overlay_cursor(event.position())
        event.accept()
        return True

    def transform_values(self, pos: QPointF) -> Optional[tuple[float, float, float]]:
        """``(pos_x, pos_y, scale)`` the running drag gives at pointer ``pos``."""
        d = self._tdrag
        if d is None:
            return None
        bx, by, bs = d["base"]
        if d["kind"] == "body":
            x, y = moved_position(bx, by, pos.x() - d["origin"].x(), pos.y() - d["origin"].y(),
                                  d["comp"])
            return x, y, bs
        return bx, by, resized_scale(bs, d["frame"], d["kind"], pos)

    def _transform_move(self, event) -> bool:
        if self._tdrag is None:
            return False
        vals = self.transform_values(event.position())
        if vals is not None:
            self.transformChanged.emit(self._tdrag["id"], *vals)
        event.accept()
        return True

    def _transform_release(self, event) -> bool:
        d = self._tdrag
        if d is None or event.button() != Qt.MouseButton.LeftButton:
            return False
        vals = self.transform_values(event.position())
        if vals is not None:
            self.transformChanged.emit(d["id"], *vals)
        self._tdrag = None
        self.releaseKeyboard()
        self.transformEnded.emit()
        self._overlay_cursor(event.position()) or self.unsetCursor()
        self.update()
        event.accept()
        return True

    # reframe handle ------------------------------------------------------------
    def set_interactive(self, on: bool) -> None:
        """Handle / pill are only offered while paused."""
        if on != self._interactive:
            self._interactive = on
            if not on:
                self.cancel_drag()
                self.cancel_transform()
            self._update_handle_ui()
            self.update()

    def update_clip(self, seg: Optional[Segment]) -> None:
        """The model's current version of the clip behind the frame (``None`` = gone)."""
        if seg is None:
            if self._seg is not None:
                self.cancel_drag()
                self._seg = None
        elif self._seg is not None and seg.id == self._seg.id and self._drag is None:
            self._seg = seg.copy()
        self._update_handle_ui()
        self.update()

    def edit_end(self) -> str:
        return self._edit_end if self.is_panning() else "start"

    def is_panning(self) -> bool:
        return self._seg is not None and self._seg.crop_x_end is not None

    def set_edit_end(self, end: str) -> None:
        self._set_edit_end(end)

    def _set_edit_end(self, end: str) -> None:
        if end in ("start", "end") and end != self._edit_end:
            self.cancel_drag()
            self._edit_end = end
            self._update_handle_ui()
            self.update()

    def _can_pan(self) -> bool:
        ratio = canvas_ratio(self._aspect)
        img = self._image
        return (ratio is not None and img is not None and not img.isNull()
                and img.height() > 0 and img.width() / img.height() > ratio + 1e-3)

    def handle_active(self) -> bool:
        """Is the reframe handle (pill + mini-map + dragging) on offer right now?"""
        return (self._interactive and self._state == self.FRAME and self._mode == "fill"
                and self._seg is not None and self._can_pan()
                and self.canvas_rect() is not None)

    def pill(self) -> _ReframePill:
        return self._pill

    def _update_handle_ui(self) -> None:
        on = self.handle_active()
        if on:
            self._pill.set_state(self.is_panning(), self.edit_end())
            self._pill.move(int((self.width() - self._pill.width()) / 2), 8)
        if self._pill.isVisible() != on:
            self._pill.setVisible(on)
        if not on:
            self.unsetCursor()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._update_handle_ui()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self.cancel_drag()
        self.cancel_transform()
        super().hideEvent(event)

    def minimap_rect(self) -> Optional[QRectF]:
        canvas = self.canvas_rect()
        img = self._image
        if canvas is None or img is None or img.isNull() or not self.handle_active():
            return None
        w = min(max(canvas.width() * 0.34, 96.0), 200.0, canvas.width() - 16)
        if w < 40:
            return None
        h = w * img.height() / img.width()
        return QRectF(round(canvas.center().x() - w / 2), round(canvas.top() + 8), round(w), round(h))

    def _window_rect(self, mm: QRectF, x: float) -> QRectF:
        """Crop window for position ``x`` in mini-map coordinates."""
        img, canvas = self._image, self.canvas_rect()
        c = crop_rect(img.width(), img.height(), canvas.width() / canvas.height(), x)
        k = mm.width() / img.width()
        return QRectF(mm.x() + c.x() * k, mm.y() + c.y() * k, c.width() * k, c.height() * k)

    def _edited_value(self) -> float:
        seg = self._seg
        if seg is None:
            return 0.5
        if self.is_panning() and self.edit_end() == "end":
            return seg.crop_x_end if seg.crop_x_end is not None else seg.crop_x
        return seg.crop_x

    def _apply_edit(self, x: float) -> None:
        seg = self._seg
        if seg is None:
            return
        x = min(max(x, 0.0), 1.0)
        if self.is_panning() and self.edit_end() == "end":
            seg.crop_x_end = x
        else:
            seg.crop_x = x

    def _drag_value(self, pos: QPointF) -> float:
        d = self._drag
        img, canvas = self._image, self.canvas_rect()
        if d is None or img is None or canvas is None:
            return self._edited_value()
        ratio = canvas.width() / canvas.height()
        cw = img.height() * ratio
        travel = img.width() - cw
        if travel <= 0:
            return d["base"]
        if d["kind"] == "map":
            mm = d["mm"]
            win_w = mm.width() * cw / img.width()
            span = mm.width() - win_w
            return min(max((pos.x() - mm.left() - win_w / 2) / span, 0.0), 1.0) if span > 0 else d["base"]
        dx = pos.x() - d["origin"].x()
        # content follows the finger: dragging right reveals more of the left of the source
        return min(max(d["base"] - dx * (cw / canvas.width()) / travel, 0.0), 1.0)

    def cancel_drag(self) -> None:
        """Abort a drag in progress and restore the clip's real crop (Esc does this)."""
        d = self._drag
        if d is None:
            return
        self._drag = None
        if self._seg is not None:
            self._seg.crop_x, self._seg.crop_x_end = d["saved"]
        self.releaseKeyboard()
        self._update_cursor(None)
        self.update()

    def is_dragging(self) -> bool:
        return self._drag is not None

    def _update_cursor(self, pos: Optional[QPointF]) -> None:
        if self._overlay_cursor(pos):
            return
        if not self.handle_active():
            self.unsetCursor()
        elif self._drag is not None:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        else:
            mm = self.minimap_rect()
            if pos is not None and mm is not None and mm.contains(pos):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif pos is not None and self.canvas_rect() is not None and self.canvas_rect().contains(pos):
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.unsetCursor()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._transform_press(event):
            return
        if event.button() != Qt.MouseButton.LeftButton or not self.handle_active():
            event.ignore()
            return
        pos = event.position()
        mm = self.minimap_rect()
        canvas = self.canvas_rect()
        kind = "map" if mm is not None and mm.contains(pos) else (
            "pan" if canvas is not None and canvas.contains(pos) else None)
        if kind is None:
            event.ignore()
            return
        seg = self._seg
        self._drag = {"kind": kind, "origin": pos, "mm": mm, "base": self._edited_value(),
                      "saved": (seg.crop_x, seg.crop_x_end), "start": self._edited_value()}
        self.grabKeyboard()                 # so that Esc reaches us while the button is down
        if kind == "map":
            self._apply_edit(self._drag_value(pos))
        self._update_cursor(pos)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._transform_move(event):
            return
        if self._drag is not None:
            self._apply_edit(self._drag_value(event.position()))
            self.update()
            event.accept()
        else:
            self._update_cursor(event.position())
            event.ignore()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._transform_release(event):
            return
        d = self._drag
        if d is None or event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        x = self._drag_value(event.position())
        end = self.edit_end()
        start = d["start"]
        self._apply_edit(x)
        self._drag = None
        self.releaseKeyboard()
        self._update_cursor(event.position())
        self.update()
        if abs(x - start) > 1e-4:
            self.cropCommitted.emit(end, x)          # the panel commits it as ONE undo step
        else:
            if self._seg is not None:
                self._seg.crop_x, self._seg.crop_x_end = d["saved"]
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._tdrag is not None and event.key() == Qt.Key.Key_Escape:
            self.cancel_transform()
            event.accept()
            return
        if self._drag is not None and event.key() == Qt.Key.Key_Escape:
            self.cancel_drag()
            event.accept()
            return
        super().keyPressEvent(event)

    # captions ------------------------------------------------------------------
    def sync_caption_time(self) -> None:
        """Repaint only when the caption line / lit word at the playhead changed."""
        doc = self._doc
        if doc is None:
            return
        caps = doc.captions
        if not caps:
            if self._caption_key is not None:
                self._caption_key = None
                self.update()
            return
        t = doc.playhead
        line = active_caption(caps, t)
        key = None
        if line is not None:
            key = (id(line), active_word(line, t) if doc.caption_style.highlight_words else -1)
        if key != self._caption_key:
            self._caption_key = key
            self.update()

    def caption_at(self, t: float):
        """``(line, lit word index)`` shown at ``t`` or ``None`` (nothing is drawn)."""
        doc = self._doc
        if doc is None or self._state == self.PLACEHOLDER:
            return None
        line = active_caption(doc.captions, t)
        if line is None:
            return None
        lit = active_word(line, t) if (doc.caption_style.highlight_words and line.words) else -1
        return line, lit

    # painting ---------------------------------------------------------------
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
        canvas = self.canvas_rect()
        have_image = self._state == self.FRAME and self._image is not None and not self._image.isNull()
        if have_image and canvas is not None:
            paint_canvas_frame(painter, canvas, self._image, self._mode, self.current_crop_x(),
                               self._blur)
        elif have_image:
            painter.drawImage(self.video_rect(), self._image)
        elif self._state == self.PLACEHOLDER:
            self._paint_notice(painter, area, "film", "No video loaded",
                               "Import a file or download a clip to start editing",
                               p.text_faint, p.text_dim)
        else:
            comp = canvas if canvas is not None else (self.compose_rect() if self._overlays else None)
            if comp is not None:
                painter.fillRect(comp, QColor(0, 0, 0))
            if self._message:
                self._paint_notice(painter, canvas if canvas is not None else area, "alert",
                                   self._message, self._detail, p.warning, p.text)
        if canvas is not None:
            painter.setPen(QPen(QColor(p.border_strong), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.drawRect(canvas.adjusted(-0.5, -0.5, 0.5, 0.5))
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_overlays(painter)
        self._paint_captions(painter, canvas)
        if self.handle_active() and not self.transform_active():
            self._paint_minimap(painter)
        self._paint_transform_handles(painter)
        painter.setClipping(False)
        painter.setPen(QColor(p.border))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.end()

    def _paint_overlays(self, painter: QPainter) -> None:
        """Composite the overlay pictures bottom-to-top onto the canvas."""
        if not self._overlays or self._state == self.PLACEHOLDER:
            return
        comp = self.compose_rect()
        if comp is None:
            return
        painter.save()
        painter.setClipRect(comp, Qt.ClipOperation.IntersectClip)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for seg, img, aspect in self._overlays:
            if img is None or img.isNull():
                continue
            painter.setOpacity(min(max(seg.opacity, 0.0), 1.0))
            painter.drawImage(overlay_rect(comp, aspect, seg.pos_x, seg.pos_y, seg.scale), img)
        painter.restore()

    def _paint_transform_handles(self, painter: QPainter) -> None:
        if not self.transform_active():
            return
        frame = self.overlay_frame()
        if frame is None:
            return
        p = theme.current()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(p.accent), 1))
        painter.drawRect(QRectF(round(frame.x()) + 0.5, round(frame.y()) + 0.5,
                                round(frame.width()), round(frame.height())))
        half = HANDLE_SIZE / 2
        for c in CORNERS:
            pt = corner_point(frame, c)
            r = QRectF(round(pt.x() - half) + 0.5, round(pt.y() - half) + 0.5,
                       HANDLE_SIZE, HANDLE_SIZE)
            painter.setPen(QPen(QColor(p.accent), 1))
            painter.setBrush(QColor(p.accent_text))
            painter.drawRect(r)
        painter.restore()

    def _paint_captions(self, painter: QPainter, canvas: Optional[QRectF]) -> None:
        doc = self._doc
        if doc is None or self._state == self.PLACEHOLDER or not doc.captions:
            return
        shown = self.caption_at(doc.playhead)
        if shown is None:
            return
        rect = canvas if canvas is not None else self.video_rect()
        paint_caption(painter, rect, shown[0], doc.caption_style, doc.playhead)

    def _paint_minimap(self, painter: QPainter) -> None:
        mm = self.minimap_rect()
        if mm is None or self._seg is None or self._image is None:
            return
        p = theme.current()
        seg = self._seg
        accent = QColor(p.accent_hover)
        pan = self.is_panning()
        editing = self.edit_end()
        wins = [("start", self._window_rect(mm, seg.crop_x))]
        if pan and seg.crop_x_end is not None:
            wins.append(("end", self._window_rect(mm, seg.crop_x_end)))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shadow = QPainterPath()
        shadow.addRoundedRect(mm.adjusted(-3, -3, 3, 3), 6, 6)
        painter.fillPath(shadow, QColor(0, 0, 0, 150))
        clip = QPainterPath()
        clip.addRoundedRect(mm, 4, 4)
        painter.setClipPath(clip)
        painter.drawImage(mm, self._image)
        veil = QPainterPath()
        veil.addRect(mm)
        lit = QPainterPath()
        for _, w in wins:
            lit.addRoundedRect(w, 3, 3)
        painter.fillPath(veil.subtracted(lit), QColor(0, 0, 0, 150))
        painter.setClipping(False)
        for key, w in wins:
            solid = (key == editing) if pan else True
            pen = QPen(accent if solid else QColor(255, 255, 255, 170), 2 if solid else 1.5)
            if not solid:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(w.adjusted(1, 1, -1, -1), 3, 3)
        if pan and len(wins) == 2:
            a, b = wins[0][1].center(), wins[1][1].center()
            if abs(b.x() - a.x()) > 6:
                painter.setPen(QPen(accent, 1.5))
                y = mm.center().y()
                d = 1 if b.x() > a.x() else -1
                x0, x1 = a.x() + d * 3, b.x() - d * 3
                painter.drawLine(QPointF(x0, y), QPointF(x1, y))
                painter.drawLine(QPointF(x1, y), QPointF(x1 - d * 5, y - 4))
                painter.drawLine(QPointF(x1, y), QPointF(x1 - d * 5, y + 4))
        painter.setPen(QPen(QColor(p.border_strong), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(mm.adjusted(-0.5, -0.5, 0.5, 0.5), 4, 4)
        painter.restore()

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


# ----------------------------------------------------------------------- overlay layer
class _LayerPlayer:
    """Player of one overlay track (V2, V3, ...): video sink + audio output, reused for every
    clip that passes on that layer and kept sync-seeked to the timeline clock by the panel.

    A frame is kept only when it belongs to the current seek and lies inside the source
    window of the clip the layer is bound to, so a stale picture never survives a cut."""

    def __init__(self, panel: "PreviewPanel", layer: int) -> None:
        self.panel = panel
        self.layer = layer
        self.dead = False
        self.source_id: Optional[str] = None
        self.path: Optional[str] = None
        self.fps = 30.0
        self.has_video = True
        self.loaded = False
        self.ended = False
        self.want_play = False
        self.seg_id: Optional[str] = None
        self.window = (0.0, 0.0)                 # source window of the bound clip
        self.target: Optional[float] = None      # source seconds last commanded
        self.pending = False
        self.seek_wall = 0
        self.check_wall = 0
        self.image: Optional[QImage] = None
        self.player = QMediaPlayer(panel)
        self.sink = QVideoSink(panel)
        self.out: Optional[QAudioOutput] = None
        try:
            self.out = QAudioOutput(panel)
            self.out.setMuted(True)
            self.player.setAudioOutput(self.out)
        except Exception:                                    # no audio device / backend
            log.warning("Preview: overlay audio output unavailable", exc_info=True)
            self.out = None
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)
        self.player.setPlaybackRate(panel.rate())

    @property
    def fd(self) -> float:
        return 1.0 / self.fps if self.fps > 0 else 1.0 / 30.0

    # source -----------------------------------------------------------------
    def bind(self, src: MediaSource) -> bool:
        if self.source_id == src.id and self.path == src.path:
            return True
        self.unbind()
        if not src.path or not os.path.exists(src.path):
            return False
        self.source_id, self.path = src.id, src.path
        self.fps = src.fps if src.fps and src.fps > 0 else 30.0
        self.has_video = bool(src.has_video)
        self.player.setSource(QUrl.fromLocalFile(src.path))
        return True

    def unbind(self) -> None:
        self.source_id = self.path = self.seg_id = None
        self.loaded = self.ended = self.want_play = self.pending = False
        self.target = None
        self.image = None
        if not self.dead:
            try:
                self.player.stop()
                self.player.setSource(QUrl())
            except RuntimeError:
                pass

    def park(self) -> None:
        """No clip on this layer right now: stop, mute and drop the picture."""
        self.seg_id = None
        self.image = None
        self.pending = False
        self.target = None
        if self.out is not None:
            self.out.setMuted(True)
        if not self.dead and self.want_play:
            self.set_play(False, 0)
        self.want_play = False

    def bind_clip(self, seg: Segment) -> None:
        """Tie the layer to ``seg``; a different clip starts without any picture."""
        if self.seg_id != seg.id:
            self.image = None
            self.target = None
        self.seg_id = seg.id
        self.window = (seg.source_in, seg.source_out)

    # commands ---------------------------------------------------------------
    def seek(self, pos: float, play: bool, now: int) -> None:
        self.target = max(0.0, pos)
        self.seek_wall = now
        self.ended = False
        self.want_play = play
        self.pending = self.has_video
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
            self.target = self.player.position() / 1000.0
        self.want_play = play
        if not self.loaded:
            return
        if play:
            if not self.ended:
                self.player.play()
        elif self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()

    def set_audio(self, volume: float, muted: bool) -> None:
        if self.out is not None:
            self.out.setVolume(volume)
            self.out.setMuted(muted)

    def estimate(self, now: int, rate: float) -> Optional[float]:
        if not self.loaded or self.target is None:
            return None
        if not self.want_play:
            return self.target
        if now - self.seek_wall < 400 or self.ended:
            return self.target + max(0, now - self.seek_wall) / 1000.0 * rate
        return self.player.position() / 1000.0

    def rebase(self, now: int, rate: float) -> None:
        est = self.estimate(now, rate)
        if est is not None and self.want_play:
            self.target, self.seek_wall = est, now

    def destroy(self) -> None:
        if self.dead:
            return
        self.dead = True
        self.image = None
        for sig, fn in ((self.sink.videoFrameChanged, self._on_frame),
                        (self.player.mediaStatusChanged, self._on_status),
                        (self.player.errorOccurred, self._on_error)):
            try:
                sig.disconnect(fn)
            except (RuntimeError, TypeError):
                pass
        try:
            self.player.stop()
            self.player.setSource(QUrl())
            self.player.setVideoSink(None)
            self.player.setAudioOutput(None)
        except RuntimeError:
            pass
        for obj in (self.player, self.sink, self.out):
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
            if self.has_video and not self.player.hasVideo():
                self.has_video = False
                self.pending = False
            self.player.pause()
            self.target = None                  # let the panel re-issue a fresh position
            self.panel._on_layer_loaded()
        elif status == S.EndOfMedia:
            self.ended = True
        elif status == S.InvalidMedia:
            self.panel._on_layer_error(self, "invalid media")

    def _on_error(self, error, text) -> None:
        if self.dead or error == QMediaPlayer.Error.NoError:
            return
        self.panel._on_layer_error(self, f"{getattr(error, 'name', error)}: {text}")

    def _on_frame(self, frame: QVideoFrame) -> None:
        if self.dead or not frame.isValid() or self.seg_id is None or self.target is None:
            return
        start = frame.startTime() / 1e6
        end = frame.endTime() / 1e6
        if start < 0:
            start = self.player.position() / 1000.0
        if end <= start:
            end = start + self.fd
        lo, hi = self.window
        if start >= hi - _TS_EPS or end <= lo + _TS_EPS:
            return                                           # outside the clip's source window
        if self.pending:
            tol = 0.25 * self.fd
            if self.want_play:
                if not self.target - 1.25 * self.fd <= start <= self.target + 1.0:
                    return                                   # frame of an older seek
            elif not start - tol <= self.target <= end + tol:
                return                                       # not the frame that was asked for
            self.pending = False
        image = frame.toImage()
        if image.isNull():
            return
        self.image = image
        self.panel._on_layer_frame(self)


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
        self._layers: dict[int, _LayerPlayer] = {}         # one player per overlay track
        self._layer_bad: set[tuple[str, str]] = set()      # (source id, path) a layer can't play
        self._layer_sig: tuple = ()
        self._opacity_busy = False
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
        self._layer_timer = QTimer(self)               # retry a paused overlay seek that was held back
        self._layer_timer.setSingleShot(True)
        self._layer_timer.timeout.connect(self._sync_layers)

        self._build_ui()

        document.seekRequested.connect(self._on_seek_requested)
        document.timelineChanged.connect(self._on_timeline_changed)
        document.sourcesChanged.connect(self._on_sources_changed)
        document.projectReset.connect(self._on_project_reset)
        document.playheadChanged.connect(self._update_time_label)
        document.playheadChanged.connect(self._on_playhead_for_captions)
        document.canvasChanged.connect(self._on_canvas_changed)
        document.captionsChanged.connect(self._on_captions_changed)
        document.selectionChanged.connect(self._refresh_reframe)
        document.selectionChanged.connect(self._refresh_overlays)
        document.tracksChanged.connect(self._refresh_overlays)

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
        self._viewport.set_document(self._doc)
        self._viewport.cropCommitted.connect(self._on_crop_committed)
        self._viewport.modeRequested.connect(self._on_mode_requested)
        self._viewport.transformBegan.connect(self._doc.begin_drag)
        self._viewport.transformChanged.connect(self._on_transform_changed)
        self._viewport.transformEnded.connect(self._on_transform_ended)
        self._viewport.transformCancelled.connect(self._doc.cancel_drag)
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
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal, bar)
        self._opacity_slider.setRange(0, 100)
        self._opacity_slider.setValue(100)
        self._opacity_slider.setFixedWidth(72)
        self._opacity_slider.setToolTip("Opacity of the selected overlay clip")
        self._opacity_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        self._opacity_slider.sliderPressed.connect(self._doc.begin_drag)
        self._opacity_slider.sliderReleased.connect(self._on_transform_ended)
        self._opacity_slider.hide()
        right.addWidget(self._opacity_slider)
        right.addSpacing(6)
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
        self._sync_layers()

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
        for lp in self._layers.values():
            if not lp.dead:
                lp.rebase(now, self._rate)
                lp.player.setPlaybackRate(rate)
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
        self._release_layers()
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
        for timer in (self._timer, self._scrub_timer, self._settle_timer, self._resync_timer,
                      self._layer_timer):
            timer.stop()
        if self._save_timer.isActive():
            self._save_volume()
        doc = self._doc
        for sig, fn in ((doc.seekRequested, self._on_seek_requested),
                        (doc.timelineChanged, self._on_timeline_changed),
                        (doc.sourcesChanged, self._on_sources_changed),
                        (doc.projectReset, self._on_project_reset),
                        (doc.playheadChanged, self._update_time_label),
                        (doc.playheadChanged, self._on_playhead_for_captions),
                        (doc.canvasChanged, self._on_canvas_changed),
                        (doc.captionsChanged, self._on_captions_changed),
                        (doc.selectionChanged, self._refresh_reframe),
                        (doc.selectionChanged, self._refresh_overlays),
                        (doc.tracksChanged, self._refresh_overlays)):
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

    # ================================================================== canvas / reframe / captions
    def canvas_rect(self) -> Optional[QRectF]:
        """Canvas rectangle inside the viewport (``None`` when the aspect is "source")."""
        return self._viewport.canvas_rect()

    def _clip(self, seg_id: Optional[str]) -> Optional[Segment]:
        if seg_id is None:
            return None
        for s in self._doc.timeline.segments:
            if s.id == seg_id:
                return s
        return None

    def _shown_clip(self) -> Optional[Segment]:
        """The model's version of the clip the displayed frame belongs to."""
        seg = self._viewport._seg
        return self._clip(seg.id) if seg is not None else None

    def reframe_targets(self) -> list[str]:
        """Clips a reframe edit applies to: every selected video clip when several are
        selected, otherwise the clip under the playhead."""
        picked = [s.id for s in self._doc.selected_segments() if not s.is_audio]
        if len(picked) > 1:
            return picked
        seg = self._shown_clip() or self._doc.timeline.segment_at(self._doc.playhead)
        return [seg.id] if seg is not None and not seg.is_audio else []

    def _on_canvas_changed(self) -> None:
        if self._shut:
            return
        self._viewport.set_canvas(self._doc.aspect, self._doc.reframe_mode)
        self._refresh_reframe()

    def _on_captions_changed(self) -> None:
        if self._shut:
            return
        self._viewport._caption_key = None
        self._viewport.sync_caption_time()
        self._viewport.update()

    def _on_playhead_for_captions(self, *_args) -> None:
        if not self._shut:
            self._viewport.sync_caption_time()

    def _refresh_reframe(self, *_args) -> None:
        """Re-read the displayed clip from the model (undo, new crop, deleted clip...)."""
        if self._shut:
            return
        self._viewport.update_clip(self._shown_clip())

    def _commit_crop(self, x: float, x_end: Optional[float]) -> bool:
        ids = self.reframe_targets()
        ok = bool(ids) and self._doc.set_crop(ids, x, x_end)
        self._refresh_reframe()
        return ok

    def _on_crop_committed(self, end: str, x: float) -> None:
        """A drag in the viewport finished: one ``Document.set_crop`` = one undo step."""
        seg = self._shown_clip()
        if seg is None:
            self._refresh_reframe()
            return
        if end == "end" and seg.crop_x_end is not None:
            self._commit_crop(seg.crop_x, x)
        else:
            self._commit_crop(x, seg.crop_x_end)

    def _on_mode_requested(self, mode: str) -> None:
        """``Static`` / ``Pan`` pressed in the pill."""
        seg = self._shown_clip()
        if seg is None:
            return
        if mode == "pan" and seg.crop_x_end is None:
            x = seg.crop_x
            x_end = min(1.0, x + 0.3) if x < 0.5 else max(0.0, x - 0.3)
            self._viewport.set_edit_end("end")
            self._commit_crop(x, x_end)
        elif mode == "static" and seg.crop_x_end is not None:
            self._viewport.set_edit_end("start")
            self._commit_crop(seg.crop_x_at(self._viewport._src_t), None)

    def nudge_crop(self, direction: int, big: bool = False) -> bool:
        """Alt+Left / Alt+Right (the main window binds them): move the crop window by 2 %
        (10 % with Shift) of its travel. In Pan mode the end chosen in the pill moves."""
        vp = self._viewport
        seg = self._shown_clip()
        if seg is None or not vp.handle_active() or direction == 0:
            return False
        step = (CROP_STEP_BIG if big else CROP_STEP) * (1 if direction > 0 else -1)
        if vp.is_panning() and vp.edit_end() == "end" and seg.crop_x_end is not None:
            new = min(max(seg.crop_x_end + step, 0.0), 1.0)
            return new != seg.crop_x_end and self._commit_crop(seg.crop_x, new)
        new = min(max(seg.crop_x + step, 0.0), 1.0)
        return new != seg.crop_x and self._commit_crop(new, seg.crop_x_end)

    def cancel_reframe_drag(self) -> None:
        self._viewport.cancel_drag()

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
        self._refresh_reframe()
        self._refresh_overlays()
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
        self._layer_bad = {(sid, p) for sid, p in self._layer_bad
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
        self._viewport.set_interactive(not playing)
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
        self._sync_layers()

    # overlay layers ---------------------------------------------------------
    def _release_layers(self) -> None:
        self._layer_timer.stop()
        for lp in list(self._layers.values()):
            lp.destroy()
        self._layers.clear()
        self._layer_bad.clear()
        self._layer_sig = ()
        self._viewport.set_overlays([], None, None)

    def layer_state(self) -> dict[int, dict]:
        """Diagnostics: per overlay layer the bound clip / source, audibility and picture."""
        return {n: {"seg_id": lp.seg_id, "source_id": lp.source_id, "target": lp.target,
                    "loaded": lp.loaded, "has_image": lp.image is not None,
                    "audible": bool(lp.out is not None and not lp.out.isMuted() and lp.want_play)}
                for n, lp in self._layers.items() if not lp.dead}

    def _selected_overlay_id(self) -> Optional[str]:
        """The selected overlay clip under the playhead that carries the handles."""
        doc = self._doc
        picked = {s.id for s in doc.selected_segments() if s.is_overlay}
        if not picked:
            return None
        for seg in reversed(doc.timeline.segments_at(self._t)):
            if seg.id in picked:
                return seg.id
        return None

    def _canvas_hint(self) -> Optional[float]:
        """Aspect of the canvas for the source aspect: the first clip of the lowest track."""
        for track in self._doc.timeline.tracks:
            if track:
                src = self._doc.sources.get(track[0].source_id)
                if src is not None and src.width > 0 and src.height > 0:
                    return src.width / src.height
                return None
        return None

    def _refresh_overlays(self, *_args) -> None:
        """Hand the viewport the overlay clips under the playhead with their pictures."""
        if self._shut:
            return
        tl = self._doc.timeline
        items = []
        for seg in tl.segments_at(self._t):
            if seg.layer < 1 or seg.is_audio:
                continue
            lp = self._layers.get(seg.layer)
            img = lp.image if (lp is not None and not lp.dead and lp.seg_id == seg.id) else None
            src = self._doc.sources.get(seg.source_id)
            if src is not None and src.width > 0 and src.height > 0:
                aspect = src.width / src.height
            elif img is not None and img.height() > 0:
                aspect = img.width() / img.height()
            else:
                aspect = 16 / 9
            items.append((seg.copy(), img, aspect))
        self._viewport.set_overlays(items, self._selected_overlay_id(), self._canvas_hint())
        self._update_opacity_ui()

    def _sync_layers(self, tick: bool = False) -> None:
        """Make every overlay player match the clip of its track under the playhead: bind,
        seek (exact frame while paused, drift-corrected while playing), audio, and park the
        players of tracks that have no clip right now."""
        if self._shut:
            return
        doc = self._doc
        tl = doc.timeline
        now = self._now()
        active = {s.layer: s for s in tl.segments_at(self._t) if s.layer >= 1 and not s.is_audio}
        for layer in list(self._layers):
            lp = self._layers[layer]
            if layer >= tl.track_count:
                del self._layers[layer]
                lp.destroy()
            elif layer not in active:
                lp.park()
        vol = self._linear_volume()
        for layer, seg in active.items():
            lp = self._layers.get(layer)
            src = doc.sources.get(seg.source_id)
            if (src is None or (src.id, src.path) in self._layer_bad
                    or self._source_problem(src) is not None):
                if lp is not None:
                    lp.park()
                continue
            if lp is None or lp.dead:
                try:
                    lp = _LayerPlayer(self, layer)
                except Exception:
                    log.exception("Preview: could not create the player of layer %d", layer)
                    self._layer_bad.add((src.id, src.path))
                    continue
                self._layers[layer] = lp
            if not lp.bind(src):
                self._layer_bad.add((src.id, src.path))
                lp.park()
                continue
            lp.bind_clip(seg)
            fd = src.frame_duration if src.frame_duration > 0 else GAP_FRAME
            target = min(max(seg.to_source(self._t), seg.source_in),
                         max(seg.source_in, seg.source_out - min(0.5 * fd, 0.5 * seg.duration)))
            audible = self._playing and not self._muted and not seg.muted and src.has_audio
            if self._playing:
                est = lp.estimate(now, self._rate)
                if lp.target is None or not lp.want_play:
                    lp.seek(target, True, now)
                elif est is not None and abs(est - target) > 0.12 and now - lp.seek_wall > 500:
                    lp.seek(target, True, now)
                else:
                    lp.set_play(True, now)
            else:
                pos = self._frame_pos(src, target)
                same = (lp.target is not None and not lp.want_play
                        and _frame_index(lp.target, lp.fps) == _frame_index(pos, lp.fps))
                if not same:
                    if lp.pending and lp.loaded and now - lp.seek_wall < _SCRUB_COALESCE_MS:
                        self._layer_timer.start(_SCRUB_COALESCE_MS)     # latest seek wins, later
                    else:
                        lp.seek(pos, False, now)
            lp.set_audio(vol, not audible)
        sig = tuple((s.id, lp.image is not None) for layer, s in sorted(active.items())
                    for lp in [self._layers.get(layer)] if lp is not None) + (
                        tuple(sorted(active)),)
        if sig != self._layer_sig:
            self._layer_sig = sig
            self._refresh_overlays()

    def _on_layer_loaded(self) -> None:
        if not self._shut:
            self._sync_layers()

    def _on_layer_frame(self, lp: _LayerPlayer) -> None:
        if self._shut or lp.dead:
            return
        self._refresh_overlays()

    def _on_layer_error(self, lp: _LayerPlayer, text: str) -> None:
        if lp.dead:
            return
        log.error("Preview: cannot play overlay %s (%s)", lp.path, text)
        if lp.source_id and lp.path:
            self._layer_bad.add((lp.source_id, lp.path))
        lp.unbind()
        if not self._shut:
            self._layer_sig = ()
            QTimer.singleShot(0, self._refresh_overlays)

    # overlay transform (handles + opacity) -----------------------------------
    def _on_transform_changed(self, seg_id: str, x: float, y: float, scale: float) -> None:
        """Live handle drag: one ``Document.set_transform`` per move (undo step at the end)."""
        self._doc.set_transform([seg_id], x=x, y=y, scale=scale)

    def _on_transform_ended(self) -> None:
        self._doc.end_drag("Transform clip")
        self._refresh_overlays()

    def _opacity_targets(self) -> list[Segment]:
        return [s for s in self._doc.selected_segments() if s.is_overlay]

    def _update_opacity_ui(self) -> None:
        if self._shut:
            return
        targets = self._opacity_targets()
        slider = self._opacity_slider
        if slider.isHidden() == bool(targets):
            slider.setVisible(bool(targets))
        if targets and not slider.isSliderDown():
            self._opacity_busy = True
            slider.setValue(int(round(targets[0].opacity * 100)))
            self._opacity_busy = False

    def _on_opacity_changed(self, value: int) -> None:
        if self._opacity_busy:
            return
        ids = [s.id for s in self._opacity_targets()]
        if ids:
            self._doc.set_transform(ids, opacity=value / 100.0)

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
        self._sync_layers(tick=True)

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
        self._viewport.show_frame(image, seg, max(start, seg.source_in))
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
