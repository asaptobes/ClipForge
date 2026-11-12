"""Colour palettes and the application-wide Qt style sheet.

Custom-painted widgets read colours from :func:`current` at paint time, so a theme
switch only needs ``apply_theme`` followed by a repaint.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

FONT_FAMILY = "Segoe UI Variable Text, Segoe UI, Inter, Arial"
MONO_FAMILY = "Cascadia Mono, Consolas, monospace"
RADIUS = 8
RADIUS_SMALL = 6


@dataclass(frozen=True)
class Palette:
    name: str
    window: str          # app background (behind panels)
    panel: str           # panel surface
    panel_alt: str       # inset areas: inputs, lists, timeline tracks
    raised: str          # buttons, cards
    raised_hover: str
    border: str
    border_strong: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str     # text on accent backgrounds
    selection: str       # selected rows / translucent selections
    danger: str
    success: str
    warning: str
    preview_bg: str      # behind the video
    ruler_bg: str
    ruler_tick: str
    track_bg: str
    clip: str            # clip body
    clip_alt: str        # clip header strip
    clip_selected: str   # selected clip outline
    clip_muted: str
    waveform: str
    playhead: str
    snap_line: str
    scrollbar: str
    scrollbar_hover: str

    def color(self, field: str) -> QColor:
        return QColor(getattr(self, field))


DARK = Palette(
    name="dark",
    window="#131417", panel="#1b1d21", panel_alt="#15161a", raised="#262930",
    raised_hover="#30343c", border="#2c2f36", border_strong="#3c414a",
    text="#e7e9ec", text_dim="#a3a9b3", text_faint="#6c727c",
    accent="#4d8dff", accent_hover="#6aa0ff", accent_pressed="#3b78e6", accent_text="#ffffff",
    selection="#24385c", danger="#ff5f6b", success="#3fcf8e", warning="#f2b441",
    preview_bg="#0b0c0e", ruler_bg="#181a1e", ruler_tick="#565c66", track_bg="#111215",
    clip="#23415f", clip_alt="#2f5680", clip_selected="#ffd166", clip_muted="#3a3d44",
    waveform="#46c9a4", playhead="#ff4d5e", snap_line="#ffd166",
    scrollbar="#353941", scrollbar_hover="#4a4f59",
)

MIDNIGHT = Palette(
    name="midnight",
    window="#050506", panel="#0d0e10", panel_alt="#08090a", raised="#191b1f",
    raised_hover="#23262b", border="#1e2024", border_strong="#2d3036",
    text="#eceef1", text_dim="#9aa0aa", text_faint="#636872",
    accent="#7c6cff", accent_hover="#9488ff", accent_pressed="#6657e0", accent_text="#ffffff",
    selection="#27224f", danger="#ff5f6b", success="#3fcf8e", warning="#f2b441",
    preview_bg="#000000", ruler_bg="#0b0c0e", ruler_tick="#4a4f58", track_bg="#060607",
    clip="#2a2757", clip_alt="#3b3780", clip_selected="#ffd166", clip_muted="#2c2e33",
    waveform="#5fd4c0", playhead="#ff4d5e", snap_line="#ffd166",
    scrollbar="#2a2d33", scrollbar_hover="#3d4149",
)

LIGHT = Palette(
    name="light",
    window="#e9ebee", panel="#f7f8fa", panel_alt="#ffffff", raised="#ffffff",
    raised_hover="#eef1f5", border="#d5d9df", border_strong="#b9bfc8",
    text="#1b1e23", text_dim="#555c67", text_faint="#8a919c",
    accent="#2f6fed", accent_hover="#4a84f5", accent_pressed="#245bd0", accent_text="#ffffff",
    selection="#d6e4ff", danger="#d93848", success="#1e9e63", warning="#b97a05",
    preview_bg="#101114", ruler_bg="#eef0f3", ruler_tick="#9aa1ac", track_bg="#dfe2e7",
    clip="#9cc0f2", clip_alt="#6f9fe3", clip_selected="#e08a00", clip_muted="#c3c7ce",
    waveform="#11795d", playhead="#e02b3d", snap_line="#e08a00",
    scrollbar="#c3c8d0", scrollbar_hover="#a5acb6",
)

PALETTES = {"dark": DARK, "midnight": MIDNIGHT, "light": LIGHT}
_current = DARK


def current() -> Palette:
    return _current


def apply_theme(app: QApplication, name: str = "dark") -> Palette:
    """Install palette + style sheet on the application and make it current."""
    global _current
    p = PALETTES.get(name, DARK)
    _current = p
    app.setStyle("Fusion")
    font = QFont("Segoe UI Variable Text")
    if not font.exactMatch():
        font = QFont("Segoe UI")
    font.setPointSizeF(9.5)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(p.window))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(p.text))
    pal.setColor(QPalette.ColorRole.Base, QColor(p.panel_alt))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(p.panel))
    pal.setColor(QPalette.ColorRole.Text, QColor(p.text))
    pal.setColor(QPalette.ColorRole.Button, QColor(p.raised))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(p.text))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(p.accent))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(p.accent_text))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(p.raised))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(p.text))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(p.text_faint))
    pal.setColor(QPalette.ColorRole.Link, QColor(p.accent))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(p.text_faint))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(p))
    return p


_ASSET_SVG = {
    "chevron": '<path d="M3 4.5l3 3 3-3" fill="none" stroke="{c}" stroke-width="1.6" '
               'stroke-linecap="round" stroke-linejoin="round"/>',
    "tick": '<path d="M3 8.5l3.2 3.2L13 4.8" fill="none" stroke="{c}" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round"/>',
    "dot": '<circle cx="8" cy="8" r="3" fill="{c}"/>',
}


def _asset(name: str, color: str) -> str:
    """Write a tiny themed SVG to the cache folder and return its path for ``url()``."""
    import tempfile
    from pathlib import Path
    folder = Path(tempfile.gettempdir()) / "ClipForge-theme"
    folder.mkdir(parents=True, exist_ok=True)
    size = 12 if name == "chevron" else 16
    file = folder / f"{name}-{color.lstrip('#')}.svg"
    if not file.exists():
        file.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
                        f'{_ASSET_SVG[name].format(c=color)}</svg>', encoding="utf-8")
    return str(file).replace("\\", "/")


def stylesheet(p: Palette) -> str:
    r, rs = RADIUS, RADIUS_SMALL
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {p.text}; font-family: {FONT_FAMILY}; }}
QMainWindow, QDialog {{ background: {p.window}; }}
QToolTip {{ background: {p.raised}; color: {p.text}; border: 1px solid {p.border_strong};
           border-radius: {rs}px; padding: 5px 8px; }}

/* ---- surfaces: set objectName or the "panel" property on a QFrame ---- */
QFrame[panel="true"] {{ background: {p.panel}; border: 1px solid {p.border}; border-radius: {r}px; }}
QFrame[card="true"] {{ background: {p.raised}; border: 1px solid {p.border}; border-radius: {r}px; }}
QFrame[inset="true"] {{ background: {p.panel_alt}; border: 1px solid {p.border}; border-radius: {r}px; }}
QFrame#TopBar {{ background: {p.panel}; border: none; border-bottom: 1px solid {p.border}; }}
QFrame[separator="true"] {{ background: {p.border}; border: none; max-width: 1px; min-width: 1px; }}
QFrame[hseparator="true"] {{ background: {p.border}; border: none; max-height: 1px; min-height: 1px; }}

/* ---- text roles ---- */
QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: 11px; font-weight: 600; color: {p.text_dim};
                         letter-spacing: 1px; text-transform: uppercase; }}
QLabel[role="dim"] {{ color: {p.text_dim}; }}
QLabel[role="faint"] {{ color: {p.text_faint}; font-size: 11px; }}
QLabel[role="error"] {{ color: {p.danger}; }}
QLabel[role="success"] {{ color: {p.success}; }}
QLabel[role="warning"] {{ color: {p.warning}; }}
QLabel[role="mono"] {{ font-family: {MONO_FAMILY}; font-size: 13px; }}
QLabel[role="brand"] {{ font-size: 16px; font-weight: 700; letter-spacing: 0.3px; }}
QLabel[role="badge"] {{ background: {p.raised}; border: 1px solid {p.border}; border-radius: 9px;
                       padding: 1px 8px; color: {p.text_dim}; font-size: 11px; }}

/* ---- buttons ---- */
QPushButton {{ background: {p.raised}; border: 1px solid {p.border}; border-radius: {rs}px;
              padding: 6px 14px; min-height: 18px; }}
QPushButton:hover {{ background: {p.raised_hover}; border-color: {p.border_strong}; }}
QPushButton:pressed {{ background: {p.panel_alt}; }}
QPushButton:focus {{ border-color: {p.accent}; }}
QPushButton:disabled {{ color: {p.text_faint}; background: {p.panel}; border-color: {p.border}; }}
QPushButton[variant="primary"] {{ background: {p.accent}; border-color: {p.accent};
                                 color: {p.accent_text}; font-weight: 600; }}
QPushButton[variant="primary"]:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: {p.accent_pressed}; }}
QPushButton[variant="primary"]:disabled {{ background: {p.raised}; border-color: {p.border};
                                          color: {p.text_faint}; }}
QPushButton[variant="danger"] {{ color: {p.danger}; }}
QPushButton[variant="danger"]:hover {{ border-color: {p.danger}; }}
QPushButton[variant="ghost"] {{ background: transparent; border-color: transparent; }}
QPushButton[variant="ghost"]:hover {{ background: {p.raised}; }}

QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: {rs}px;
              padding: 5px; }}
QToolButton:hover {{ background: {p.raised}; border-color: {p.border}; }}
QToolButton:pressed {{ background: {p.panel_alt}; }}
QToolButton:checked {{ background: {p.selection}; border-color: {p.accent}; }}
QToolButton:disabled {{ color: {p.text_faint}; }}
QToolButton[text-beside="true"] {{ padding: 5px 10px; }}
QToolButton[variant="primary"] {{ background: {p.accent}; border-color: {p.accent};
                                 color: {p.accent_text}; font-weight: 600; padding: 6px 14px; }}
QToolButton[variant="primary"]:hover {{ background: {p.accent_hover}; }}
QToolButton[variant="primary"]:disabled {{ background: {p.raised}; border-color: {p.border};
                                          color: {p.text_faint}; }}
QToolButton[variant="transport"] {{ border-radius: 18px; padding: 8px; }}
QToolButton[variant="transport-main"] {{ background: {p.accent}; border-radius: 20px; padding: 9px; }}
QToolButton[variant="transport-main"]:hover {{ background: {p.accent_hover}; }}
QToolButton::menu-indicator {{ image: none; }}

/* ---- inputs ---- */
QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: {p.panel_alt}; border: 1px solid {p.border}; border-radius: {rs}px;
    padding: 6px 9px; selection-background-color: {p.accent}; selection-color: {p.accent_text}; }}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {p.border_strong}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{ border-color: {p.accent}; }}
QLineEdit:disabled, QSpinBox:disabled {{ color: {p.text_faint}; background: {p.panel}; }}
QLineEdit[state="error"] {{ border-color: {p.danger}; }}
QLineEdit[state="ok"] {{ border-color: {p.success}; }}

QComboBox {{ background: {p.raised}; border: 1px solid {p.border}; border-radius: {rs}px;
            padding: 5px 28px 5px 10px; min-height: 20px; }}
QComboBox:hover {{ border-color: {p.border_strong}; background: {p.raised_hover}; }}
QComboBox:focus {{ border-color: {p.accent}; }}
QComboBox:disabled {{ color: {p.text_faint}; background: {p.panel}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({_asset('chevron', p.text_dim)}); width: 12px; height: 12px;
    margin-right: 8px; }}
QComboBox QAbstractItemView {{ background: {p.raised}; border: 1px solid {p.border_strong};
    border-radius: {rs}px; padding: 4px; selection-background-color: {p.selection};
    selection-color: {p.text}; outline: 0; }}
QComboBox QAbstractItemView::item {{ padding: 5px 8px; border-radius: 4px; min-height: 20px; }}

QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px;
    border: 1px solid {p.border_strong}; background: {p.panel_alt}; }}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent};
    image: url({_asset('tick', p.accent_text)}); }}
QRadioButton::indicator:checked {{ background: {p.accent}; border-color: {p.accent};
    image: url({_asset('dot', p.accent_text)}); }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ background: {p.panel};
    border-color: {p.border}; }}

/* ---- sliders / progress ---- */
QSlider::groove:horizontal {{ height: 4px; background: {p.border_strong}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -5px 0; border-radius: 7px;
    background: {p.text}; border: 1px solid {p.border}; }}
QSlider::handle:horizontal:hover {{ background: {p.accent_hover}; }}
QSlider::handle:horizontal:disabled {{ background: {p.text_faint}; }}
QProgressBar {{ background: {p.panel_alt}; border: 1px solid {p.border}; border-radius: 4px;
               min-height: 6px; max-height: 6px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}

/* ---- lists / menus / tabs ---- */
QListWidget, QListView, QTreeView {{ background: {p.panel_alt}; border: 1px solid {p.border};
    border-radius: {r}px; padding: 4px; }}
QListWidget::item, QListView::item {{ border-radius: {rs}px; padding: 4px; margin: 1px 0; }}
QListWidget::item:hover, QListView::item:hover {{ background: {p.raised}; }}
QListWidget::item:selected, QListView::item:selected {{ background: {p.selection}; color: {p.text}; }}
QMenu {{ background: {p.raised}; border: 1px solid {p.border_strong}; border-radius: {r}px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {p.selection}; }}
QMenu::item:disabled {{ color: {p.text_faint}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 6px; }}
QMenu::icon {{ padding-left: 8px; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {p.text_dim}; padding: 7px 14px; margin-right: 2px;
               border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {p.text}; }}
QTabBar::tab:selected {{ color: {p.text}; border-bottom-color: {p.accent}; }}
QGroupBox {{ border: 1px solid {p.border}; border-radius: {r}px; margin-top: 14px; padding: 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {p.text_dim}; }}

/* ---- splitters / scrollbars / status ---- */
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:vertical {{ height: 6px; }}
QSplitter::handle:hover {{ background: {p.accent}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {p.scrollbar}; border-radius: 3px; min-width: 28px; min-height: 28px; }}
QScrollBar::handle:hover {{ background: {p.scrollbar_hover}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QStatusBar {{ background: {p.panel}; border-top: 1px solid {p.border}; color: {p.text_dim}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {p.text_dim}; padding: 0 6px; }}
QMessageBox {{ background: {p.panel}; }}
QMessageBox QLabel {{ color: {p.text}; }}
"""
