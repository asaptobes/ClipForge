"""Small shared UI helpers used by the panels and dialogs.

Nothing here knows about the document or the media back end; it is only themed
building blocks (elided labels, thumbnail boxes, folder fields) plus a few
file-system conveniences.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import (QColor, QDesktopServices, QImage, QPainter, QPainterPath, QPalette,
                           QPen, QPixmap, QTextLayout, QTextOption)
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QSizePolicy, QWidget)

from ..workers import Task
from . import icons, theme

DOT = "  \u00b7  "

_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                   *(f"LPT{i}" for i in range(1, 10))}


# --------------------------------------------------------------------- styling
def set_prop(widget: QWidget, name: str, value: Any) -> None:
    """Set a dynamic property and re-polish so the style sheet picks it up."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def make_label(text: str = "", role: Optional[str] = None, *, wrap: bool = False,
               selectable: bool = False, shrinkable: bool = True) -> QLabel:
    """``shrinkable`` wrapped labels never widen their parent (side panels); pass
    ``False`` in dialogs, where the layout should size them normally."""
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    if wrap:
        lab.setWordWrap(True)
        if shrinkable:
            lab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
            lab.setMinimumWidth(40)
        else:
            lab.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lab


def heading(text: str) -> QLabel:
    return make_label(text.upper(), "heading")


def hseparator() -> QFrame:
    line = QFrame()
    line.setProperty("hseparator", True)
    line.setFixedHeight(1)
    return line


def icon_label(name: str, color_field: str = "text_dim", size: int = 16) -> QLabel:
    """A QLabel showing a themed icon; ``color_field`` is a Palette attribute name."""
    lab = QLabel()
    lab.setPixmap(icons.pixmap(name, getattr(theme.current(), color_field), size))
    lab.setFixedSize(size, size)
    return lab


def primary_button(text: str, icon_name: Optional[str] = None) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("variant", "primary")
    if icon_name:
        btn.setIcon(icons.icon(icon_name, theme.current().accent_text))
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def plain_button(text: str, icon_name: Optional[str] = None,
                 variant: Optional[str] = None) -> QPushButton:
    btn = QPushButton(text)
    if variant:
        btn.setProperty("variant", variant)
    if icon_name:
        btn.setIcon(icons.icon(icon_name))
    return btn


# ---------------------------------------------------------------------- labels
class ElidedLabel(QLabel):
    """A label that never forces its parent wider: the text is elided to fit.

    ``text()`` always returns the full text. ``max_lines`` > 1 wraps and elides
    the last line. The full text is shown as a tooltip when ``auto_tooltip``.
    """

    clicked = Signal()

    def __init__(self, text: str = "", parent: Optional[QWidget] = None, *,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight, max_lines: int = 1,
                 role: Optional[str] = None, auto_tooltip: bool = True) -> None:
        super().__init__(parent)
        self._mode = mode
        self._max_lines = max(1, max_lines)
        self._auto_tooltip = auto_tooltip
        self._clickable = False
        if role:
            self.setProperty("role", role)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        super().setText(text)
        if self._auto_tooltip:
            self.setToolTip(text)
        self.updateGeometry()
        self.update()

    def set_clickable(self, clickable: bool) -> None:
        self._clickable = clickable
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()

    def _line_height(self) -> int:
        return self.fontMetrics().lineSpacing()

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        m = self.contentsMargins()
        width = fm.horizontalAdvance(self.text()) + m.left() + m.right() + 2
        return QSize(width, self._line_height() * self._max_lines + m.top() + m.bottom())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(16, self.sizeHint().height())

    def _lines(self, width: int) -> list[str]:
        text = self.text()
        fm = self.fontMetrics()
        if self._max_lines == 1 or not text:
            return [fm.elidedText(text, self._mode, width)]
        layout = QTextLayout(text, self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        out: list[str] = []
        layout.beginLayout()
        while len(out) < self._max_lines:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1, width))
            start, length = line.textStart(), line.textLength()
            if len(out) == self._max_lines - 1 and start + length < len(text):
                out.append(fm.elidedText(text[start:].strip(), Qt.TextElideMode.ElideRight, width))
            else:
                out.append(text[start:start + length].strip())
        layout.endLayout()
        return out

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setFont(self.font())
        role = QPalette.ColorRole.WindowText
        painter.setPen(self.palette().color(role))
        rect = self.contentsRect()
        lh = self._line_height()
        lines = self._lines(rect.width())
        y = rect.top()
        if self._max_lines == 1:
            y = rect.top() + (rect.height() - lh) // 2
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine
        for line in lines:
            painter.drawText(QRectF(rect.left(), y, rect.width(), lh), int(flags), line)
            y += lh
        painter.end()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._clickable and event.button() == Qt.MouseButton.LeftButton \
                and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


# ------------------------------------------------------------------ thumbnails
def render_thumb(img: QImage, width: int, height: int, dpr: float = 1.0) -> QPixmap:
    """Letterbox ``img`` into ``width`` x ``height`` (never crops); bars are filled with
    a dimmed, zoomed copy so vertical videos still look good."""
    dpr = max(1.0, dpr)
    w, h = int(width * dpr), int(height * dpr)
    pm = QPixmap(w, h)
    pm.fill(QColor("#000000"))
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    fit = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    if fit.width() < w - 2 or fit.height() < h - 2:
        small = img.scaled(max(1, w // 6), max(1, h // 6),
                           Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
        cover = small.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
        painter.setOpacity(0.38)
        painter.drawImage(QPointF((w - cover.width()) / 2, (h - cover.height()) / 2), cover)
        painter.setOpacity(1.0)
    painter.drawImage(QPointF((w - fit.width()) / 2, (h - fit.height()) / 2), fit)
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm


class ThumbBox(QWidget):
    """Fixed-size rounded image box. Images are letterboxed (never cropped); the
    bars are filled with a dimmed, zoomed copy so vertical videos still look good."""

    def __init__(self, width: int, height: int, *, radius: int = 6,
                 placeholder: str = "film", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._radius = radius
        self._placeholder = placeholder
        self._image: Optional[QImage] = None
        self._cache: Optional[QPixmap] = None
        self.setFixedSize(width, height)

    def has_image(self) -> bool:
        return self._image is not None

    def set_image(self, image: Optional[QImage]) -> None:
        if isinstance(image, QPixmap):
            image = image.toImage()
        self._image = image if (image is not None and not image.isNull()) else None
        self._cache = None
        self.update()

    def set_image_data(self, data: bytes) -> bool:
        image = QImage()
        ok = bool(data) and image.loadFromData(data)
        self.set_image(image if ok else None)
        return ok

    def _render(self) -> QPixmap:
        return render_thumb(self._image, self.width(), self.height(), self.devicePixelRatioF())

    def paintEvent(self, event) -> None:  # noqa: N802
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        if self._image is None:
            painter.fillPath(path, QColor(p.panel_alt))
            size = max(12, min(22, int(self.height() * 0.45)))
            pm = icons.pixmap(self._placeholder, p.text_faint, size)
            painter.drawPixmap(QPointF((self.width() - size) / 2, (self.height() - size) / 2), pm)
        else:
            if self._cache is None:
                self._cache = self._render()
            painter.setClipPath(path)
            painter.drawPixmap(0, 0, self._cache)
            painter.setClipping(False)
        painter.setPen(QPen(QColor(p.border), 1))
        painter.drawPath(path)
        painter.end()


# ---------------------------------------------------------------- folder field
class PathField(QWidget):
    """Folder path line edit with a Browse… button."""

    changed = Signal(str)

    def __init__(self, path: str = "", *, dialog_title: str = "Choose folder",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._dialog_title = dialog_title
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setMinimumWidth(80)
        self.browse_button = QPushButton("Browse\u2026")
        self.browse_button.setIcon(icons.icon("folder"))
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.browse_button)
        self.edit.textChanged.connect(self._on_changed)
        self.browse_button.clicked.connect(self.browse)
        self.set_path(path)

    def path(self) -> str:
        text = self.edit.text().strip().strip('"')
        return os.path.normpath(text) if text else ""

    def set_path(self, path: str) -> None:
        self.edit.setText(os.path.normpath(path) if path else "")
        self.edit.setCursorPosition(0)

    def set_error(self, message: Optional[str]) -> None:
        set_prop(self.edit, "state", "error" if message else "")
        self.edit.setToolTip(message or self.edit.text())

    def has_error(self) -> bool:
        return self.edit.property("state") == "error"

    def _on_changed(self, _text: str) -> None:
        self.set_error(None)
        self.changed.emit(self.path())

    def browse(self) -> None:
        start = self.path()
        while start and not os.path.isdir(start):
            parent = os.path.dirname(start)
            if parent == start:
                start = ""
                break
            start = parent
        chosen = QFileDialog.getExistingDirectory(self, self._dialog_title, start)
        if chosen:
            self.set_path(chosen)


def folder_problem(path: str, create: bool = False) -> Optional[str]:
    """Why ``path`` cannot be used as an output folder (``None`` when it is fine).

    Without ``create`` nothing on disk is touched; with it the folder is created
    and a write test performed."""
    path = (path or "").strip()
    if not path:
        return "Choose a folder."
    if not os.path.isabs(path):
        return "Enter a full folder path, for example C:\\Videos."
    if any(c in '<>"|?*' for c in os.path.splitdrive(path)[1]):
        return "The folder path contains characters Windows does not allow."
    probe = path
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe or not parent:
            return "The drive or location does not exist."
        probe = parent
    if not os.path.isdir(probe):
        return "A file with this name is in the way, so the folder cannot be created."
    if create:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            return f"The folder could not be created ({exc.strerror or exc})."
        try:
            fd, tmp = tempfile.mkstemp(prefix=".clipforge_", dir=path)
            os.close(fd)
            os.remove(tmp)
        except OSError:
            return "ClipForge cannot write to this folder. Choose another one."
    return None


# ------------------------------------------------------------------ file names
def sanitize_filename(name: str) -> str:
    """Remove characters Windows does not allow in file names."""
    return _ILLEGAL_FILENAME.sub("", name)


def clean_filename(name: str) -> str:
    """Final form of a user-typed file name (no extension, no trailing dots/spaces)."""
    name = sanitize_filename(name).strip()
    if name.lower().endswith(".mp4"):
        name = name[:-4]
    return name.strip().rstrip(". ")


def is_reserved_filename(name: str) -> bool:
    return name.split(".")[0].strip().upper() in _RESERVED_NAMES


# ------------------------------------------------------------------- shell
def reveal_in_folder(path: str) -> None:
    """Open the containing folder in Explorer with the file selected."""
    path = os.path.normpath(path)
    if sys.platform == "win32" and os.path.exists(path):
        try:
            subprocess.Popen(["explorer", "/select,", path])  # noqa: S603,S607
            return
        except OSError:
            pass
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


def open_path(path: str) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.normpath(path)))


# ------------------------------------------------------------------- tasks
_live_tasks: dict[int, Task] = {}
_keeper: Optional["_TaskKeeper"] = None


class _TaskKeeper(QObject):
    """Keeps Task objects alive until shortly after they finished."""

    @Slot()
    def release(self) -> None:
        sender = self.sender()
        for key, task in list(_live_tasks.items()):
            if task.signals is sender:
                # the worker thread may still be unwinding run(); drop it a bit later
                QTimer.singleShot(1500, lambda k=key: _live_tasks.pop(k, None))


def run_task(fn: Callable, on_result: Optional[Callable] = None,
             on_failed: Optional[Callable] = None, name: str = "task") -> Task:
    """Run ``fn(cancel, progress)`` on the global pool; callbacks must be bound
    methods of QObjects (they are delivered on the GUI thread)."""
    global _keeper
    if _keeper is None:
        _keeper = _TaskKeeper()
    task = Task(fn, name=name)
    if on_result is not None:
        task.signals.result.connect(on_result)
    if on_failed is not None:
        task.signals.failed.connect(on_failed)
    task.signals.finished.connect(_keeper.release)
    _live_tasks[id(task)] = task
    task.start()
    return task
