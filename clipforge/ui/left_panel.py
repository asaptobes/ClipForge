"""Left side panel: video download (YouTube, Reddit, TikTok, Instagram), media library and details of the selected media."""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from PySide6.QtCore import QModelIndex, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy,
                               QStyledItemDelegate, QStyleOptionViewItem, QToolButton,
                               QVBoxLayout, QWidget)

from ..core.model import MediaSource
from ..core.timecode import format_duration, format_size
from ..media import downloader as dl
from ..settings import Settings
from . import icons, theme
from .widgets import (DOT, ElidedLabel, ThumbBox, heading, hseparator, make_label, plain_button,
                      primary_button, render_thumb, set_prop)
from .winutil import reveal_in_explorer

log = logging.getLogger(__name__)

QUALITY_LABELS = {"best": "Best", "2160": "2160p · 4K", "1440": "1440p",
                  "1080": "1080p", "720": "720p", "480": "480p"}
FOOTNOTE = ("Only download content you own or have the rights to use. You are responsible "
            "for following copyright law and the site's terms. Only public posts work; "
            "ClipForge never asks for passwords or cookies.")
URL_PLACEHOLDER = "Paste a video link"
URL_TOOLTIP = "Paste a video link from YouTube, Reddit, TikTok or Instagram (Ctrl+L)"
UPDATE_HINT = "update the downloader"
SITE_NAMES = {"youtube": "YouTube", "reddit": "Reddit", "tiktok": "TikTok",
              "instagram": "Instagram"}

ROW_HEIGHT = 58
POSTER_W, POSTER_H = 64, 40
MAX_VISIBLE_ROWS = 5

ID_ROLE = Qt.ItemDataRole.UserRole
TITLE_ROLE = Qt.ItemDataRole.UserRole + 1
META_ROLE = Qt.ItemDataRole.UserRole + 2
OFFLINE_ROLE = Qt.ItemDataRole.UserRole + 3


# ----------------------------------------------------------------- formatting
def media_meta_text(src: MediaSource) -> str:
    """``1920×1080 · 30 fps · 0:42`` (parts that are unknown are left out)."""
    parts = []
    if src.has_video and src.width and src.height:
        parts.append(f"{src.width}×{src.height}")
        if src.fps:
            parts.append(f"{src.fps:g} fps")
    elif not src.has_video:
        parts.append("Audio only")
    parts.append(format_duration(src.duration))
    return DOT.join(parts)


def media_title(src: MediaSource) -> str:
    return src.title or os.path.splitext(os.path.basename(src.path))[0] or src.path


def _channels_text(n: int) -> str:
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(n, f"{n} ch" if n else "")


def orientation_text(src: MediaSource) -> str:
    if src.width == src.height:
        return "Square"
    return "Portrait" if src.is_vertical else "Landscape"


def details_for(src: MediaSource) -> list[tuple[str, str]]:
    """Rows of the DETAILS grid for one source (label, value)."""
    rows: list[tuple[str, str]] = [
        ("File", os.path.basename(src.path)),
        ("Folder", os.path.dirname(src.path)),
    ]
    if src.has_video:
        res = f"{src.width}×{src.height}" if src.width and src.height else "Unknown"
        if src.width and src.height:
            res += f"{DOT}{orientation_text(src)}"
        rows.append(("Resolution", res))
        rows.append(("Frame rate", f"{src.fps:g} fps" if src.fps else "Unknown"))
    rows.append(("Duration", format_duration(src.duration)))
    if src.has_video:
        video = (src.video_codec or "Unknown").upper()
        if src.pix_fmt:
            video += f"{DOT}{src.pix_fmt}"
        rows.append(("Video", video))
    if src.has_audio:
        audio = [src.audio_codec.upper() if src.audio_codec else "Audio"]
        if src.sample_rate:
            audio.append(f"{src.sample_rate / 1000:g} kHz")
        if src.channels:
            audio.append(_channels_text(src.channels))
        rows.append(("Audio", DOT.join(audio)))
    else:
        rows.append(("Audio", "No audio"))
    size = src.file_size
    if not size:
        try:
            size = os.path.getsize(src.path)
        except OSError:
            size = 0
    if size:
        rows.append(("Size", format_size(size)))
    if src.source_url:
        rows.append(("Source", src.source_url))
    return rows


def _quality_available(info: object, key: str) -> bool:
    """Is quality ``key`` offered by this video ("best" always is)."""
    if key == "best":
        return True
    try:
        return key in info.available_qualities()
    except Exception:  # noqa: BLE001 - stub / odd info objects: derive from heights
        return int(key) in [int(h) for h in (getattr(info, "heights", None) or [])]


# ------------------------------------------------------------------- delegate
class _MediaDelegate(QStyledItemDelegate):
    """Paints one library row: poster, title, meta line and an Offline flag."""

    def __init__(self, posters: dict[str, QPixmap], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._posters = posters

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        from PySide6.QtWidgets import QStyle
        p = theme.current()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(1, 1, -1, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            path = QPainterPath()
            path.addRoundedRect(rect, theme.RADIUS_SMALL, theme.RADIUS_SMALL)
            painter.fillPath(path, QColor(p.selection if selected else p.raised))
        offline = bool(index.data(OFFLINE_ROLE))
        sid = index.data(ID_ROLE)
        pr = QRectF(rect.left() + 6, rect.top() + (rect.height() - POSTER_H) / 2,
                    POSTER_W, POSTER_H)
        ppath = QPainterPath()
        ppath.addRoundedRect(pr, 5, 5)
        pm = self._posters.get(sid)
        if pm is None:
            painter.fillPath(ppath, QColor(p.panel_alt))
            ic = icons.pixmap("film", p.text_faint, 18)
            painter.drawPixmap(QPoint(int(pr.center().x() - 9), int(pr.center().y() - 9)), ic)
        else:
            painter.setClipPath(ppath)
            if offline:
                painter.setOpacity(0.4)
            painter.drawPixmap(pr.toRect().topLeft(), pm)
            painter.setOpacity(1.0)
            painter.setClipping(False)
        painter.setPen(QPen(QColor(p.border), 1))
        painter.drawPath(ppath)

        tx = int(pr.right()) + 10
        tw = int(rect.right()) - tx - 6
        font = option.font
        fm = QFontMetrics(font)
        title = str(index.data(TITLE_ROLE) or "")
        meta = str(index.data(META_ROLE) or "")
        top = int(rect.top()) + (int(rect.height()) - 2 * fm.height() - 3) // 2
        painter.setFont(font)
        badge_w = 0
        if offline:
            bf = painter.font()
            bf.setPointSizeF(max(7.0, font.pointSizeF() - 2))
            bfm = QFontMetrics(bf)
            label = "Offline"
            badge_w = bfm.horizontalAdvance(label) + 12
            br = QRectF(rect.right() - 6 - badge_w, top + 1, badge_w, bfm.height() + 2)
            bp = QPainterPath()
            bp.addRoundedRect(br, br.height() / 2, br.height() / 2)
            fill = QColor(p.warning)
            fill.setAlpha(48)
            painter.fillPath(bp, fill)
            painter.setFont(bf)
            painter.setPen(QColor(p.warning))
            painter.drawText(br, int(Qt.AlignmentFlag.AlignCenter), label)
            painter.setFont(font)
            badge_w += 6
        painter.setPen(QColor(p.text if not offline else p.text_dim))
        painter.drawText(QRect(tx, top, tw - badge_w, fm.height()),
                         int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         fm.elidedText(title, Qt.TextElideMode.ElideRight, tw - badge_w))
        small = painter.font()
        small.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        sfm = QFontMetrics(small)
        painter.setFont(small)
        painter.setPen(QColor(p.text_faint if not selected else p.text_dim))
        painter.drawText(QRect(tx, top + fm.height() + 3, tw, sfm.height()),
                         int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         sfm.elidedText(meta, Qt.TextElideMode.ElideRight, tw))
        painter.restore()


class _MediaList(QListWidget):
    """Library list: no frame scroll bars when it fits, fixed row height."""

    def __init__(self, posters: dict[str, QPixmap], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setItemDelegate(_MediaDelegate(posters, self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setUniformItemSizes(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def fit_rows(self) -> None:
        rows = max(1, min(self.count(), MAX_VISIBLE_ROWS))
        self.setFixedHeight(rows * (ROW_HEIGHT + 2) + 8)


# ----------------------------------------------------------------- left panel
class LeftPanel(QWidget):
    """DOWNLOAD / MEDIA / DETAILS sections in one vertically scrolling column."""

    downloadFinished = Signal(str, object)     # output path, VideoInfo
    importRequested = Signal()
    relinkRequested = Signal(str)              # source id
    statusMessage = Signal(str)
    openSettingsRequested = Signal()           # error hint: "Open Settings" (Downloader section)

    DEBOUNCE_MS = 350

    def __init__(self, document, settings: Settings, thumbnails=None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.document = document
        self.settings = settings
        self.thumbnails = thumbnails
        self._info = None                       # VideoInfo of the current valid URL
        self._valid_url = ""
        self._fetching = False
        self._downloading = False
        self._cancelling = False
        self._importing = False
        self._message: Optional[tuple[str, str]] = None      # (text, kind)
        self._retry: Optional[Callable[[], None]] = None
        self._meta_job = None
        self._dl_job = None
        self._dl_args = None
        self._old_jobs: list = []
        self._quality = ""
        self._posters: dict[str, QPixmap] = {}
        self._offline: dict[str, bool] = {}
        self._detail_rows: list[tuple[str, str]] = []
        self.details: dict[str, str] = {}

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self.validate_now)
        self._offline_timer = QTimer(self)
        self._offline_timer.setInterval(5000)
        self._offline_timer.timeout.connect(self.refresh_offline)

        self._build()
        self._connect()
        self.set_default_quality()
        self.refresh_media()
        self._sync()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self.scroll)
        body = QWidget()
        self.scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        # --- download -------------------------------------------------------
        head = QHBoxLayout()
        head.addWidget(heading("Download"))
        head.addStretch(1)
        lay.addLayout(head)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(URL_PLACEHOLDER)
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.setToolTip(URL_TOOLTIP)
        lay.addWidget(self.url_edit)
        self.url_error = make_label("", "error", wrap=True)
        self.url_error.setVisible(False)
        lay.addWidget(self.url_error)
        self.fetch_label = make_label("Fetching video info…", "dim")
        lay.addWidget(self.fetch_label)

        self.info_card = QFrame()
        self.info_card.setProperty("card", True)
        card = QHBoxLayout(self.info_card)
        card.setContentsMargins(8, 8, 8, 8)
        card.setSpacing(10)
        self.info_thumb = ThumbBox(88, 50, placeholder="youtube")
        card.addWidget(self.info_thumb, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.info_title = ElidedLabel("", max_lines=2)
        self.info_uploader = ElidedLabel("", role="dim")
        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        self.info_duration = make_label("", "faint")
        self.info_badge = make_label("Short", "badge")
        self.info_badge.setFixedHeight(20)
        self.info_site = make_label("", "badge")
        self.info_site.setFixedHeight(20)
        self.info_site.setVisible(False)
        meta_row.addWidget(self.info_duration)
        meta_row.addWidget(self.info_site)
        meta_row.addWidget(self.info_badge)
        meta_row.addStretch(1)
        col.addWidget(self.info_title)
        col.addWidget(self.info_uploader)
        col.addLayout(meta_row)
        card.addLayout(col, 1)
        lay.addWidget(self.info_card)

        self.quality_row = QWidget()
        qr = QHBoxLayout(self.quality_row)
        qr.setContentsMargins(0, 0, 0, 0)
        qr.setSpacing(8)
        self.quality_combo = QComboBox()
        self.quality_combo.setMinimumWidth(90)
        self.quality_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        for key, text in QUALITY_LABELS.items():
            self.quality_combo.addItem(text, key)
        self.quality_combo.setToolTip("Maximum video quality to download")
        self.download_button = primary_button("Download", "download")
        qr.addWidget(self.quality_combo, 1)
        qr.addWidget(self.download_button)
        lay.addWidget(self.quality_row)

        self.progress_box = QWidget()
        pb = QVBoxLayout(self.progress_box)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.setSpacing(6)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_label = make_label("", "dim", wrap=True)
        row = QHBoxLayout()
        self.stage_label = ElidedLabel("", role="faint")
        self.cancel_button = plain_button("Cancel", "x")
        row.addWidget(self.stage_label, 1)
        row.addWidget(self.cancel_button)
        pb.addWidget(self.progress_bar)
        pb.addWidget(self.progress_label)
        pb.addLayout(row)
        lay.addWidget(self.progress_box)

        self.message_box = QWidget()
        mb = QVBoxLayout(self.message_box)
        mb.setContentsMargins(0, 0, 0, 0)
        mb.setSpacing(6)
        self.message_label = make_label("", "error", wrap=True)
        mrow = QHBoxLayout()
        self.retry_button = primary_button("Retry", "retry")
        self.dismiss_button = plain_button("Dismiss")
        self.settings_button = plain_button("Open Settings", "settings", variant="ghost")
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.setToolTip("Open Settings to update the downloader")
        mrow.addWidget(self.retry_button)
        mrow.addWidget(self.dismiss_button)
        mrow.addStretch(1)
        mb.addWidget(self.message_label)
        srow = QHBoxLayout()
        srow.addWidget(self.settings_button)
        srow.addStretch(1)
        mb.addLayout(srow)
        mb.addLayout(mrow)
        lay.addWidget(self.message_box)

        self.footnote = make_label(FOOTNOTE, "faint", wrap=True)
        lay.addWidget(self.footnote)

        lay.addSpacing(2)
        lay.addWidget(hseparator())
        lay.addSpacing(2)

        # --- media ------------------------------------------------------------
        mh = QHBoxLayout()
        mh.addWidget(heading("Media"))
        self.media_count = make_label("0", "badge")
        self.media_count.setFixedHeight(20)
        mh.addWidget(self.media_count)
        self.importing_label = make_label("Importing…", "faint")
        self.importing_label.setVisible(False)
        mh.addWidget(self.importing_label)
        mh.addStretch(1)
        self.import_tool = QToolButton()
        self.import_tool.setIcon(icons.icon("import"))
        self.import_tool.setToolTip("Import media files")
        mh.addWidget(self.import_tool)
        lay.addLayout(mh)

        self.empty_card = QFrame()
        self.empty_card.setProperty("inset", True)
        ec = QVBoxLayout(self.empty_card)
        ec.setContentsMargins(14, 16, 14, 16)
        ec.setSpacing(6)
        ico = QLabel()
        ico.setPixmap(icons.pixmap("film", theme.current().text_faint, 28))
        ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ec.addWidget(ico)
        t = make_label("No media yet", "dim", wrap=True)
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ec.addWidget(t)
        s = make_label("Import video files or download a video above.", "faint", wrap=True)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ec.addWidget(s)
        self.import_button = primary_button("Import media…", "import")
        ec.addWidget(self.import_button, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.empty_card)

        self.media_list = _MediaList(self._posters)
        lay.addWidget(self.media_list)

        lay.addSpacing(2)
        lay.addWidget(hseparator())
        lay.addSpacing(2)

        # --- details ----------------------------------------------------------
        lay.addWidget(heading("Details"))
        self.details_hint = make_label("Select a media item or a clip to see its details.",
                                       "faint", wrap=True)
        lay.addWidget(self.details_hint)
        self.details_box = QWidget()
        self.details_grid = QGridLayout(self.details_box)
        self.details_grid.setContentsMargins(0, 0, 0, 0)
        self.details_grid.setHorizontalSpacing(10)
        self.details_grid.setVerticalSpacing(5)
        self.details_grid.setColumnStretch(1, 1)
        lay.addWidget(self.details_box)
        lay.addStretch(1)

    def _connect(self) -> None:
        self.url_edit.textChanged.connect(self._on_url_text)
        self.url_edit.returnPressed.connect(self.validate_now)
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        self.download_button.clicked.connect(self.start_download)
        self.cancel_button.clicked.connect(self.cancel_download)
        self.retry_button.clicked.connect(self._on_retry)
        self.dismiss_button.clicked.connect(self.dismiss_message)
        self.settings_button.clicked.connect(self.openSettingsRequested)
        self.import_button.clicked.connect(self.importRequested)
        self.import_tool.clicked.connect(self.importRequested)
        ml = self.media_list
        ml.itemSelectionChanged.connect(self._update_details)
        ml.itemDoubleClicked.connect(lambda item: self.add_to_timeline(item.data(ID_ROLE)))
        ml.customContextMenuRequested.connect(self._media_menu)
        d = self.document
        d.sourcesChanged.connect(self.refresh_media)
        d.selectionChanged.connect(self._on_clip_selection)
        d.timelineChanged.connect(self._update_details)
        if self.thumbnails is not None:
            self.thumbnails.thumbnailReady.connect(self._on_thumbnail_ready)

    # ----------------------------------------------------------------- states
    def state(self) -> str:
        """``idle`` / ``fetching`` / ``ready`` / ``downloading`` / ``message``."""
        if self._downloading:
            return "downloading"
        if self._message is not None:
            return "message"
        if self._fetching:
            return "fetching"
        return "ready" if self._info is not None else "idle"

    def _sync(self) -> None:
        st = self.state()
        self.url_edit.setEnabled(not self._downloading)
        self.fetch_label.setVisible(st == "fetching")
        self.info_card.setVisible(self._info is not None)
        self.quality_row.setVisible(self._info is not None and st == "ready")
        self.download_button.setEnabled(self._info is not None and st == "ready"
                                        and not getattr(self._info, "is_live", False))
        self.progress_box.setVisible(st == "downloading")
        self.cancel_button.setEnabled(not self._cancelling)
        self.message_box.setVisible(st == "message")
        if self._message:
            self.message_label.setText(self._message[0])
            set_prop(self.message_label, "role", self._message[1])
        self.retry_button.setVisible(self._retry is not None)
        self.settings_button.setVisible(
            bool(self._message) and self._message[1] == "error"
            and UPDATE_HINT in self._message[0].lower())
        self.footnote.setVisible(True)

    # -------------------------------------------------------------- url field
    def _on_url_text(self, _text: str) -> None:
        if self._downloading:
            return
        self._debounce.start()

    def validate_now(self) -> None:
        """Validate the URL immediately (normally called by the debounce timer)."""
        self._debounce.stop()
        if self._downloading:
            return
        text = self.url_edit.text().strip()
        if not text:
            self._set_url_state("", "")
            self._reset_info()
            return
        try:
            url = dl.validate_url(text)
        except Exception as exc:  # noqa: BLE001 - MediaError carries a friendly message
            self._set_url_state("error", str(exc) or "That doesn't look like a supported link.")
            self._reset_info()
            return
        self._set_url_state("ok", "")
        if url == self._valid_url and (self._info is not None or self._fetching):
            return
        self._reset_info()
        self._valid_url = url
        self._start_metadata(url)

    def _set_url_state(self, state: str, message: str) -> None:
        set_prop(self.url_edit, "state", state)
        self.url_error.setText(message)
        self.url_error.setVisible(bool(message))

    def _reset_info(self) -> None:
        self._abandon_meta()
        self._info = None
        self._valid_url = ""
        self._fetching = False
        self._message = None
        self._retry = None
        self.info_thumb.set_image(None)
        self._sync()

    # --------------------------------------------------------------- metadata
    def _start_metadata(self, url: str) -> None:
        self._message = None
        self._fetching = True
        try:
            job = dl.MetadataJob(url, self)
            job.finished.connect(self._on_meta_finished)
            job.failed.connect(self._on_meta_failed)
            job.cancelled.connect(self._on_meta_cancelled)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not start metadata job")
            self._fetching = False
            self._show_message(str(exc) or "Could not look up this video.", "error",
                               lambda: self.validate_now_force())
            return
        self._meta_job = job
        self._sync()
        job.start()

    def validate_now_force(self) -> None:
        self._valid_url = ""
        self._reset_info()
        self.validate_now()

    def _abandon_meta(self) -> None:
        job, self._meta_job = self._meta_job, None
        if job is not None:
            self._old_jobs.append(job)
            try:
                job.cancel()
            except Exception:  # noqa: BLE001
                log.debug("metadata cancel failed", exc_info=True)

    def _release_old(self, job) -> bool:
        """True if ``job`` was an abandoned job (its result must be ignored)."""
        if job in self._old_jobs:
            self._old_jobs.remove(job)
            job.deleteLater()
            return True
        return False

    @Slot(object)
    def _on_meta_finished(self, info) -> None:
        job = self.sender()
        if self._release_old(job) or job is not self._meta_job:
            return
        self._meta_job = None
        job.deleteLater()
        self._fetching = False
        self._info = info
        self._populate_info(info)
        self._sync()

    @Slot(str)
    def _on_meta_failed(self, message: str) -> None:
        job = self.sender()
        if self._release_old(job) or job is not self._meta_job:
            return
        self._meta_job = None
        job.deleteLater()
        self._fetching = False
        self._show_message(message or "Could not load this video.", "error",
                           self.validate_now_force)

    @Slot()
    def _on_meta_cancelled(self) -> None:
        job = self.sender()
        if self._release_old(job) or job is not self._meta_job:
            return
        self._meta_job = None
        job.deleteLater()
        self._fetching = False
        self._sync()

    def _populate_info(self, info) -> None:
        self.info_title.setText(getattr(info, "title", "") or "Untitled video")
        self.info_uploader.setText(getattr(info, "uploader", "") or "")
        self.info_uploader.setVisible(bool(getattr(info, "uploader", "")))
        dur = getattr(info, "duration", 0) or 0
        live = getattr(info, "is_live", False)
        self.info_duration.setText("Live" if live else (format_duration(dur) if dur else ""))
        self.info_badge.setVisible(bool(getattr(info, "is_short", False)))
        site = str(getattr(info, "site", "") or "youtube")
        self.info_site.setText(SITE_NAMES.get(site, site.title()))
        self.info_site.setToolTip(f"Source: {self.info_site.text()}")
        self.info_site.setVisible(site != "youtube")
        self.info_thumb.set_image_data(getattr(info, "thumbnail_jpeg", b"") or b"")
        self._fill_qualities(info)
        if live:
            self._set_url_state("error", "Live streams can't be downloaded.")

    def _fill_qualities(self, info) -> None:
        combo = self.quality_combo
        model = combo.model()
        avail = {k: _quality_available(info, k) for k in QUALITY_LABELS}
        combo.blockSignals(True)
        for i in range(combo.count()):
            key = combo.itemData(i)
            item = model.item(i)
            ok = avail.get(key, False)
            item.setEnabled(ok)
            combo.setItemData(i, None if ok else "Not offered by this video",
                              Qt.ItemDataRole.ToolTipRole)
        only_best = not any(ok for k, ok in avail.items() if k != "best")
        if only_best:
            combo.setItemText(0, "Best available")
            combo.setToolTip("This site offers a single quality; the best one is downloaded")
        else:
            combo.setItemText(0, QUALITY_LABELS["best"])
            combo.setToolTip("Maximum video quality to download")
        combo.setEnabled(not only_best)
        combo.blockSignals(False)
        self.set_default_quality()

    def set_default_quality(self) -> None:
        """Select ``settings.preferred_resolution`` (or the best enabled one below it)."""
        combo, model = self.quality_combo, self.quality_combo.model()
        keys = [combo.itemData(i) for i in range(combo.count())]
        pref = self.settings.preferred_resolution
        order = [k for k in keys if k != "best"]            # descending heights
        candidates = [pref] if pref == "best" else (
            [k for k in order if int(k) <= int(pref)] if pref.isdigit() else ["best"])
        for key in candidates + ["best"]:
            i = keys.index(key) if key in keys else -1
            if i >= 0 and model.item(i).isEnabled():
                combo.setCurrentIndex(i)
                break
        self._quality = str(combo.currentData())

    def _on_quality_changed(self, _index: int) -> None:
        self._quality = str(self.quality_combo.currentData())

    def selected_quality(self) -> str:
        return str(self.quality_combo.currentData() or "best")

    # --------------------------------------------------------------- download
    def is_downloading(self) -> bool:
        return self._downloading

    def start_download(self) -> None:
        if self._info is None or self._downloading:
            return
        self._launch_download(self._info, self.selected_quality())

    def _launch_download(self, info, quality: str) -> None:
        self._message = None
        self._retry = None
        self._cancelling = False
        try:
            dest = str(self.settings.download_path())
            job = dl.DownloadJob(info, quality, dest, self)
            job.progress.connect(self._on_dl_progress)
            job.stage.connect(self._on_dl_stage)
            job.finished.connect(self._on_dl_finished)
            job.failed.connect(self._on_dl_failed)
            job.cancelled.connect(self._on_dl_cancelled)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not create download job")
            self._show_message(f"Could not start the download. {exc}", "error",
                               lambda: self._launch_download(info, quality))
            return
        self._dl_job = job
        self._dl_args = (info, quality)
        self._downloading = True
        self.progress_bar.setRange(0, 0)         # busy until the first progress report
        self.progress_label.setText("Starting…")
        self.stage_label.setText("")
        self._sync()
        self.statusMessage.emit("Download started")
        job.start()

    @Slot(float, str, str)
    def _on_dl_progress(self, fraction: float, speed: str, eta: str) -> None:
        if self.sender() is not self._dl_job:
            return
        if fraction is None or fraction < 0:
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText(speed or "Working…")
            return
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(int(min(1.0, fraction) * 1000))
        parts = [f"{int(min(1.0, fraction) * 100)}%"]
        if speed:
            parts.append(speed)
        if eta:
            parts.append(eta if "left" in eta else f"{eta} left")
        self.progress_label.setText(DOT.join(parts))

    @Slot(str)
    def _on_dl_stage(self, text: str) -> None:
        if self.sender() is self._dl_job:
            self.stage_label.setText(text)

    def _end_download(self, job) -> None:
        self._downloading = False
        self._cancelling = False
        self._dl_job = None
        job.deleteLater()

    @Slot(str, object)
    def _on_dl_finished(self, path: str, info: object) -> None:
        job = self.sender()
        if job is not self._dl_job:
            return
        self._end_download(job)
        self._info = None
        self._valid_url = ""
        self._message = None
        self.url_edit.blockSignals(True)
        self.url_edit.clear()
        self.url_edit.blockSignals(False)
        self._set_url_state("", "")
        self.info_thumb.set_image(None)
        self._sync()
        self.statusMessage.emit(f"Downloaded {os.path.basename(path)}")
        self.downloadFinished.emit(path, info)

    @Slot(str)
    def _on_dl_failed(self, message: str) -> None:
        job = self.sender()
        if job is not self._dl_job:
            return
        self._end_download(job)
        info, quality = self._dl_args
        self._show_message(message or "The download failed.", "error",
                           lambda: self._launch_download(info, quality))
        self.statusMessage.emit("Download failed")

    @Slot()
    def _on_dl_cancelled(self) -> None:
        job = self.sender()
        if job is not self._dl_job:
            return
        self._end_download(job)
        info, quality = self._dl_args
        self._show_message("Download cancelled.", "dim",
                           lambda: self._launch_download(info, quality))
        self.statusMessage.emit("Download cancelled")

    def cancel_download(self) -> None:
        if self._dl_job is None or self._cancelling:
            return
        self._cancelling = True
        self.progress_label.setText("Cancelling…")
        self.cancel_button.setEnabled(False)
        try:
            self._dl_job.cancel()
        except Exception:  # noqa: BLE001
            log.exception("cancel failed")

    def _show_message(self, text: str, kind: str, retry: Optional[Callable[[], None]]) -> None:
        self._message = (text, kind)
        self._retry = retry
        self._sync()

    def _on_retry(self) -> None:
        retry, self._retry = self._retry, None
        self._message = None
        self._sync()
        if retry:
            retry()

    def dismiss_message(self) -> None:
        self._message = None
        self._retry = None
        self._sync()

    def shutdown(self) -> None:
        """Cancel running jobs (called when the window closes)."""
        self._debounce.stop()
        self._offline_timer.stop()
        for job in [self._meta_job, self._dl_job]:
            if job is not None:
                try:
                    job.cancel()
                except Exception:  # noqa: BLE001
                    log.debug("job cancel failed at shutdown", exc_info=True)
        self._meta_job = None

    def focus_url(self) -> None:
        self.scroll.verticalScrollBar().setValue(0)
        self.url_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.url_edit.selectAll()

    def set_busy_importing(self, busy: bool) -> None:
        self._importing = busy
        self.importing_label.setVisible(busy)
        self.import_button.setEnabled(not busy)
        self.import_tool.setEnabled(not busy)

    # ------------------------------------------------------------------ media
    def _source_ids(self) -> list[str]:
        return list(self.document.sources.keys())

    def refresh_media(self) -> None:
        """Rebuild the library list from ``document.sources``."""
        sources = self.document.sources
        selected = self.selected_media_id()
        ml = self.media_list
        ml.blockSignals(True)
        ml.clear()
        self._offline = {sid: not os.path.isfile(s.path) for sid, s in sources.items()}
        for sid in list(self._posters):
            if sid not in sources:
                del self._posters[sid]
        for sid, src in sources.items():
            item = QListWidgetItem()
            item.setData(ID_ROLE, sid)
            item.setData(TITLE_ROLE, media_title(src))
            item.setData(META_ROLE, media_meta_text(src))
            item.setData(OFFLINE_ROLE, self._offline[sid])
            item.setToolTip(src.path)
            item.setSizeHint(QSize(10, ROW_HEIGHT))
            ml.addItem(item)
            if sid == selected:
                ml.setCurrentItem(item)
            if sid not in self._posters:
                self._request_poster(src)
        ml.blockSignals(False)
        ml.fit_rows()
        empty = not sources
        self.empty_card.setVisible(empty)
        ml.setVisible(not empty)
        self.media_count.setText(str(len(sources)))
        self.media_count.setVisible(not empty)
        if not empty and not self._offline_timer.isActive():
            self._offline_timer.start()
        self._update_details()

    def _request_poster(self, src: MediaSource) -> None:
        if self.thumbnails is None or self._offline.get(src.id):
            return
        try:
            image = self.thumbnails.poster(src)
        except Exception:  # noqa: BLE001 - a missing poster is cosmetic
            log.debug("poster request failed", exc_info=True)
            return
        if isinstance(image, QImage) and not image.isNull():
            self._posters[src.id] = render_thumb(image, POSTER_W, POSTER_H,
                                                 self.devicePixelRatioF())

    @Slot(str, float)
    def _on_thumbnail_ready(self, source_id: str, _t: float) -> None:
        src = self.document.sources.get(source_id)
        if src is None or source_id in self._posters:
            return
        self._request_poster(src)
        if source_id in self._posters:
            self.media_list.viewport().update()

    def refresh_offline(self) -> None:
        """Re-check which library files are missing on disk (cheap; run periodically)."""
        changed = False
        for i in range(self.media_list.count()):
            item = self.media_list.item(i)
            sid = item.data(ID_ROLE)
            src = self.document.sources.get(sid)
            if src is None:
                continue
            off = not os.path.isfile(src.path)
            if off != self._offline.get(sid):
                self._offline[sid] = off
                item.setData(OFFLINE_ROLE, off)
                changed = True
                if not off:
                    self._request_poster(src)
        if changed:
            self.media_list.viewport().update()
            self._update_details()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self.document.sources:
            self.refresh_offline()
            self._offline_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._offline_timer.stop()

    def is_offline(self, source_id: str) -> bool:
        return bool(self._offline.get(source_id))

    def selected_media_id(self) -> Optional[str]:
        item = self.media_list.currentItem() if self.media_list.selectedItems() else None
        return item.data(ID_ROLE) if item else None

    def select_media(self, source_id: Optional[str]) -> None:
        for i in range(self.media_list.count()):
            if self.media_list.item(i).data(ID_ROLE) == source_id:
                self.media_list.setCurrentRow(i)
                return
        self.media_list.clearSelection()

    def add_to_timeline(self, source_id: str) -> None:
        src = self.document.sources.get(source_id)
        if src is None:
            return
        if not os.path.isfile(src.path):
            self.refresh_offline()
            self.statusMessage.emit("That media is offline. Relink it first.")
            return
        self.document.append_to_timeline(source_id)
        self.statusMessage.emit(f"Added {media_title(src)} to the timeline")

    def remove_source(self, source_id: str) -> None:
        self.document.remove_source(source_id)

    def show_in_folder(self, source_id: str) -> None:
        src = self.document.sources.get(source_id)
        if src is not None:
            reveal_in_explorer(src.path)

    def _run_menu(self, menu: QMenu, global_pos: QPoint):
        return menu.exec(global_pos)

    def _media_menu(self, pos: QPoint) -> None:
        item = self.media_list.itemAt(pos)
        if item is None:
            return
        self.media_list.setCurrentItem(item)
        sid = item.data(ID_ROLE)
        menu = QMenu(self)
        a_add = menu.addAction(icons.icon("plus"), "Add to timeline")
        a_add.setEnabled(not self.is_offline(sid))
        a_show = menu.addAction(icons.icon("folder"), "Show in folder")
        a_relink = menu.addAction(icons.icon("link"), "Relink…")
        menu.addSeparator()
        a_remove = menu.addAction(icons.icon("trash"), "Remove from project")
        chosen = self._run_menu(menu, self.media_list.viewport().mapToGlobal(pos))
        if chosen is a_add:
            self.add_to_timeline(sid)
        elif chosen is a_show:
            self.show_in_folder(sid)
        elif chosen is a_relink:
            self.relinkRequested.emit(sid)
        elif chosen is a_remove:
            self.remove_source(sid)

    # ---------------------------------------------------------------- details
    def _on_clip_selection(self) -> None:
        if self.document.selection and self.media_list.selectedItems():
            self.media_list.blockSignals(True)
            self.media_list.clearSelection()
            self.media_list.blockSignals(False)
        self._update_details()

    def details_source(self) -> Optional[MediaSource]:
        sid = self.selected_media_id()
        if sid:
            return self.document.sources.get(sid)
        segs = self.document.selected_segments()
        if segs:
            return self.document.source_of(segs[0])
        return None

    def _update_details(self) -> None:
        src = self.details_source()
        rows = details_for(src) if src else []
        self.details = dict(rows)
        if src and self._offline.get(src.id):
            self.details["Status"] = "Offline (file not found)"
            rows = rows + [("Status", self.details["Status"])]
        if rows == self._detail_rows:
            return
        self._detail_rows = rows
        grid = self.details_grid
        while grid.count():
            w = grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        for r, (key, value) in enumerate(rows):
            k = make_label(key, "faint")
            k.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            if key in ("Folder", "Source", "File"):
                mode = Qt.TextElideMode.ElideMiddle
                v = ElidedLabel(value, mode=mode)
            else:
                v = ElidedLabel(value)
            if key == "Status":
                v.setProperty("role", "warning")
            grid.addWidget(k, r, 0)
            grid.addWidget(v, r, 1)
        self.details_hint.setVisible(not rows)
        self.details_box.setVisible(bool(rows))
