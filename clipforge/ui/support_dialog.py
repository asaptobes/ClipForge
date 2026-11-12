"""One-time thank-you / support dialog shown after the main window opens."""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import paths
from ..settings import Settings
from .splash import headless
from .widgets import make_label, plain_button, primary_button

log = logging.getLogger(__name__)

SUPPORT_URL = "https://buymeacoffee.com/asaptobes"
TITLE = "Thanks for using ClipForge!"
BODY = ("I built ClipForge as a solo project and I'm glad it's useful to you. If it saves you "
        "time and you'd like to support its development, you can buy me a coffee. Totally "
        "optional, and ClipForge stays just as useful either way.")
BUY_TEXT = "☕ Buy me a coffee"
LATER_TEXT = "Maybe later"
DONT_SHOW_TEXT = "Don't show this again"


def open_support_page() -> bool:
    return bool(QDesktopServices.openUrl(QUrl(SUPPORT_URL)))


def prompt_allowed(settings: Settings, *, smoke_test: bool = False,
                   export_test: bool = False) -> bool:
    return settings.show_support_prompt and not (smoke_test or export_test or headless())


class SupportDialog(QDialog):
    def __init__(self, settings: Settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("ClipForge")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 22)
        root.setSpacing(12)

        self.logo_label = QLabel()
        self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_png = paths.resource_path("icon.png")
        if icon_png.is_file():
            self.logo_label.setPixmap(QIcon(str(icon_png)).pixmap(72, 72))
        else:
            self.logo_label.hide()
        root.addWidget(self.logo_label)

        self.title_label = make_label(TITLE, "title", shrinkable=False)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.title_label)

        self.body_label = make_label(BODY, "dim", wrap=True, shrinkable=False)
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.body_label)
        root.addSpacing(6)

        self.buy_button = primary_button(BUY_TEXT)
        self.buy_button.setDefault(True)
        self.buy_button.setMinimumHeight(36)
        self.later_button = plain_button(LATER_TEXT)
        self.later_button.setMinimumHeight(36)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.later_button)
        row.addWidget(self.buy_button, 1)
        root.addLayout(row)

        self.dont_show_check = QCheckBox(DONT_SHOW_TEXT)
        root.addWidget(self.dont_show_check, 0, Qt.AlignmentFlag.AlignHCenter)

        self.buy_button.clicked.connect(self._buy)
        self.later_button.clicked.connect(self.reject)

    def _buy(self) -> None:
        open_support_page()
        self.accept()

    def done(self, result: int) -> None:
        """Every way of closing (buttons, Esc, X) persists the checkbox."""
        if self.dont_show_check.isChecked() and self.settings.show_support_prompt:
            self.settings.show_support_prompt = False
            self.settings.save()
        super().done(result)


def show_support_dialog(settings: Settings, parent: Optional[QWidget] = None) -> None:
    """Show the dialog if still wanted; safe if the window was closed in the meantime."""
    if not settings.show_support_prompt:
        return
    try:
        if parent is not None and not parent.isVisible():
            return
        SupportDialog(settings, parent).exec()
    except Exception:  # noqa: BLE001
        log.debug("Support dialog failed", exc_info=True)
