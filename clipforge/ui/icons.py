"""Built-in SVG icon set (24x24 line icons) rendered and tinted at run time.

``icon("scissors")`` returns a QIcon with normal / disabled states tinted from the
current theme; ``pixmap`` gives a single tinted, high-DPI pixmap.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme

_STROKE = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           'stroke="{color}" stroke-width="{width}" stroke-linecap="round" '
           'stroke-linejoin="round">{body}</svg>')

# Inner SVG markup for each icon. Shapes with fill use fill="{color}".
ICONS: dict[str, str] = {
    "new": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
           '<path d="M14 3v5h5"/><path d="M12 12v6M9 15h6"/>',
    "open": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
            '<path d="M3 11h18"/>',
    "save": '<path d="M5 3h11l5 5v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/>'
            '<path d="M7 3v5h8V3"/><rect x="7" y="13" width="10" height="8" rx="1"/>',
    "import": '<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    "export": '<path d="M12 15V3"/><path d="M7 8l5-5 5 5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    "download": '<path d="M12 4v11"/><path d="M7.5 11l4.5 4.5 4.5-4.5"/><path d="M5 20h14"/>',
    "settings": '<circle cx="12" cy="12" r="3"/>'
                '<path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 '
                '1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 '
                '1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 '
                '1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 '
                '1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 '
                '1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "play": '<path d="M7 4.5v15a1 1 0 0 0 1.5.86l12.5-7.5a1 1 0 0 0 0-1.72L8.5 3.64A1 1 0 0 0 7 4.5z" '
            'fill="{color}" stroke="none"/>',
    "pause": '<rect x="6" y="4" width="4.2" height="16" rx="1.2" fill="{color}" stroke="none"/>'
             '<rect x="13.8" y="4" width="4.2" height="16" rx="1.2" fill="{color}" stroke="none"/>',
    "step-back": '<path d="M17 6l-8 6 8 6z"/><path d="M7 5v14"/>',
    "step-forward": '<path d="M7 6l8 6-8 6z"/><path d="M17 5v14"/>',
    "skip-start": '<path d="M19 5l-8 7 8 7z"/><path d="M11 5l-6 7 6 7"/>',
    "skip-end": '<path d="M5 5l8 7-8 7z"/><path d="M13 5l6 7-6 7"/>',
    "scissors": '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/>'
                '<path d="M20 4L8.1 15.9"/><path d="M14.5 14.5L20 20"/><path d="M8.1 8.1L12 12"/>',
    "trash": '<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M6 7l1 13h10l1-13"/><path d="M10 11v6M14 11v6"/>',
    "ripple-delete": '<path d="M9 5v14"/><path d="M15 5v14"/><path d="M2 12h5"/><path d="M4.5 9.5L7 12l-2.5 2.5"/>'
                     '<path d="M22 12h-5"/><path d="M19.5 9.5L17 12l2.5 2.5"/>',
    "close-gaps": '<rect x="3" y="7" width="6" height="10" rx="1.5"/><rect x="15" y="7" width="6" height="10" rx="1.5"/>'
                  '<path d="M10.5 12h3"/>',
    "undo": '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "redo": '<path d="M15 14l5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/>',
    "zoom-in": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/><path d="M11 8v6M8 11h6"/>',
    "zoom-out": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/><path d="M8 11h6"/>',
    "fit": '<path d="M4 9V5h4"/><path d="M20 9V5h-4"/><path d="M4 15v4h4"/><path d="M20 15v4h-4"/>'
           '<rect x="8" y="9" width="8" height="6" rx="1"/>',
    "magnet": '<path d="M6 3v8a6 6 0 0 0 12 0V3h-4v8a2 2 0 0 1-4 0V3z"/><path d="M6 7h4M14 7h4"/>',
    "volume": '<path d="M11 5L6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/>',
    "volume-x": '<path d="M11 5L6 9H3v6h3l5 4z"/><path d="M22 9l-6 6M16 9l6 6"/>',
    "detach-audio": '<rect x="3" y="3" width="18" height="7" rx="1.5"/>'
                    '<path d="M8 21v-5M12 21v-9M16 21v-6M5.5 21v-2.5M18.5 21v-2.5"/>',
    "film": '<rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M7 3v18M17 3v18M3 8h4M3 12h18M3 16h4M17 8h4M17 16h4"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/>'
            '<path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    "x": '<path d="M18 6L6 18M6 6l12 12"/>',
    "retry": '<path d="M3 12a9 9 0 0 1 15.5-6.3L21 8"/><path d="M21 3v5h-5"/>'
             '<path d="M21 12a9 9 0 0 1-15.5 6.3L3 16"/><path d="M3 21v-5h5"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "alert": '<path d="M12 3.5L2.5 20h19z"/><path d="M12 10v4.5"/><path d="M12 17.3v.2"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "music": '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><path d="M12 7.5v.2"/>',
    "chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "more": '<circle cx="5" cy="12" r="1.3" fill="{color}"/><circle cx="12" cy="12" r="1.3" fill="{color}"/>'
            '<circle cx="19" cy="12" r="1.3" fill="{color}"/>',
    "youtube": '<rect x="2.5" y="5.5" width="19" height="13" rx="4"/>'
               '<path d="M10 9.2v5.6l5-2.8z" fill="{color}" stroke="none"/>',
    "speed": '<path d="M4.5 17a9 9 0 1 1 15 0"/><path d="M12 13l4-5"/><circle cx="12" cy="13.5" r="1.2" fill="{color}"/>',
    "cursor": '<path d="M5 3l14 7-6 2-2 6z"/>',
    "captions": '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 10.5h3.5M13 10.5h4M7 14h5M14.5 14H17"/>',
    "eye": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>'
               '<path d="M4 4l16 16"/>',
}

_cache: dict[tuple, QPixmap] = {}


def svg_markup(name: str, color: str, stroke_width: float = 1.8) -> str:
    body = ICONS.get(name) or ICONS["info"]
    return _STROKE.format(color=color, width=stroke_width, body=body.replace("{color}", color))


def pixmap(name: str, color: Optional[str] = None, size: int = 18,
           device_pixel_ratio: float = 2.0) -> QPixmap:
    color = color or theme.current().text
    key = (name, color, size, device_pixel_ratio)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    px = max(1, int(round(size * device_pixel_ratio)))
    image = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(svg_markup(name, color).encode("utf-8")))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    pm = QPixmap.fromImage(image)
    pm.setDevicePixelRatio(device_pixel_ratio)
    _cache[key] = pm
    return pm


def icon(name: str, color: Optional[str] = None, size: int = 18,
         active_color: Optional[str] = None) -> QIcon:
    """Themed icon. ``active_color`` tints the checked ("On") state."""
    p = theme.current()
    ic = QIcon()
    ic.addPixmap(pixmap(name, color or p.text, size), QIcon.Mode.Normal, QIcon.State.Off)
    ic.addPixmap(pixmap(name, p.text_faint, size), QIcon.Mode.Disabled, QIcon.State.Off)
    ic.addPixmap(pixmap(name, active_color or color or p.text, size), QIcon.Mode.Normal, QIcon.State.On)
    ic.addPixmap(pixmap(name, p.text_faint, size), QIcon.Mode.Disabled, QIcon.State.On)
    return ic


def icon_size(size: int = 18) -> QSize:
    return QSize(size, size)
