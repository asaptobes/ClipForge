"""Start-up splash screen: the logo, a thin progress bar and a status line."""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Optional

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from .. import paths
from . import theme

log = logging.getLogger(__name__)

MIN_VISIBLE_SECONDS = 1.2
SIZE = 520


def headless() -> bool:
    """True for automated runs: tests, CLIPFORGE_NO_SPLASH, offscreen/minimal Qt platform."""
    if os.environ.get("CLIPFORGE_NO_SPLASH", "").strip() not in ("", "0"):
        return True
    if "pytest" in sys.modules:
        return True
    platform = (os.environ.get("QT_QPA_PLATFORM") or "").lower()
    if platform in ("offscreen", "minimal"):
        return True
    app = QGuiApplication.instance()
    return app is not None and app.platformName().lower() in ("offscreen", "minimal")


def splash_allowed(*, smoke_test: bool = False, export_test: bool = False) -> bool:
    return not (smoke_test or export_test or headless())


class SplashScreen(QWidget):
    def __init__(self, pixmap: QPixmap) -> None:
        super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("ClipForge")
        self._status = "Loading…"
        self._progress = 0.0
        self._shown_at = time.monotonic()
        self._fade: Optional[QPropertyAnimation] = None
        screen = QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen else 1.0
        side = SIZE
        if screen is not None:                       # never larger than ~60% of the screen
            avail = screen.availableGeometry()
            side = min(side, int(min(avail.width(), avail.height()) * 0.6))
        self.setFixedSize(side, side)
        scaled = pixmap.scaled(int(side * dpr), int(side * dpr),
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        self._pixmap = scaled
        self._backdrop = QColor(scaled.toImage().pixelColor(2, 2))
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())

    # ------------------------------------------------------------- state
    def set_status(self, text: str, progress: Optional[float] = None) -> None:
        self._status = text
        if progress is not None:
            self._progress = min(1.0, max(0.0, progress))
        self.update()
        self._pump()

    @staticmethod
    def _pump() -> None:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def show_now(self) -> None:
        self._shown_at = time.monotonic()
        self.show()
        self.raise_()
        self._pump()

    def finish(self, window: Optional[QWidget] = None,
               min_seconds: float = MIN_VISIBLE_SECONDS) -> None:
        """Stay up for ``min_seconds`` in total (counted from show), then fade out and close."""
        self.set_status("Ready", 1.0)
        deadline = self._shown_at + min_seconds
        while time.monotonic() < deadline:           # keep painting while waiting
            self._pump()
            time.sleep(0.01)
        if window is not None:
            window.raise_()
            window.activateWindow()
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(180)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self.close)
        self._fade.start()

    # ------------------------------------------------------------- paint
    def paintEvent(self, _event) -> None:
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        painter.fillRect(self.rect(), self._backdrop)
        dpr = self._pixmap.devicePixelRatio()
        painter.drawPixmap(int((w - self._pixmap.width() / dpr) / 2),
                           int((h - self._pixmap.height() / dpr) / 2), self._pixmap)
        font = self.font()
        font.setPointSizeF(9.0)
        painter.setFont(font)
        painter.setPen(QColor(p.text_dim))
        painter.drawText(QRectF(0, h - 64, w, 20), Qt.AlignmentFlag.AlignCenter, self._status)
        bar_w, bar_h = w * 0.42, 3.0
        x, y = (w - bar_w) / 2, h - 38.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(p.border_strong))
        painter.drawRoundedRect(QRectF(x, y, bar_w, bar_h), 1.5, 1.5)
        painter.setBrush(QColor(p.accent))
        painter.drawRoundedRect(QRectF(x, y, max(bar_h, bar_w * self._progress), bar_h), 1.5, 1.5)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(p.border), 1))
        painter.drawRect(QRectF(0.5, 0.5, w - 1, h - 1))


def create_splash() -> Optional[SplashScreen]:
    """Build the splash from ``resources/logo.png``; ``None`` if it cannot be made."""
    try:
        logo = paths.resource_path("logo.png")
        if not logo.is_file():
            return None
        pixmap = QPixmap(str(logo))
        if pixmap.isNull():
            return None
        return SplashScreen(pixmap)
    except Exception:  # noqa: BLE001 - a splash must never break start-up
        log.debug("Splash unavailable", exc_info=True)
        return None
