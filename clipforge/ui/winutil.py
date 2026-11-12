"""Small Windows-specific helpers (safe no-ops elsewhere)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)


def set_dark_titlebar(widget: QWidget, dark: bool = True) -> None:
    """Ask DWM for a dark (or light) native title bar on Windows 10 20H1+ / 11."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = wintypes.HWND(int(widget.winId()))
        value = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        for attribute in (20, 19):       # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old id)
            if dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                         ctypes.sizeof(value)) == 0:
                break
    except Exception as exc:  # noqa: BLE001 - purely cosmetic
        log.debug("Dark title bar unavailable: %s", exc)


class TitleBarStyler(QObject):
    """App-wide event filter giving every top-level window a theme-matching title bar."""

    def __init__(self, dark: bool = True, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.dark = dark

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            set_dark_titlebar(obj, self.dark)
        return False


def reveal_in_explorer(path: str) -> None:
    """Open the containing folder with the file selected."""
    path = os.path.normpath(path)
    try:
        if sys.platform == "win32":
            if os.path.isdir(path):
                os.startfile(path)  # noqa: S606 - opening a folder the user chose
            else:
                subprocess.Popen(["explorer", "/select,", path])  # noqa: S603,S607
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
    except OSError as exc:
        log.warning("Could not open Explorer for %s: %s", path, exc)
