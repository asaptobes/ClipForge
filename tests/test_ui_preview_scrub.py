"""Scrubbing with several tracks: seeks are not aborted before they answer, and the picture that
finally stays up is the one for the last position (no stale frame)."""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import LUMA_SPAN  # noqa: E402

from clipforge.core.model import MediaSource  # noqa: E402
from clipforge.document import Document  # noqa: E402
from clipforge.ui import preview as pv  # noqa: E402
from clipforge.ui import theme  # noqa: E402

_GREY_PER_SEC = 255.0 / 219.0 * LUMA_SPAN / 6.0          # video_h: 6 s luma ramp


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication([])
    theme.apply_theme(inst, "dark")
    return inst


class _Settings:
    volume = 1.0

    def save(self):
        pass


def src_time(image: QImage) -> float:
    img = image.convertToFormat(QImage.Format.Format_RGB32)
    cx, cy = img.width() // 2, img.height() // 2
    vals = [img.pixelColor(cx + dx, cy + dy).red() for dx in (-6, 0, 6) for dy in (-6, 0, 6)]
    return (sum(vals) / len(vals)) / _GREY_PER_SEC


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.002)


def test_patience_grows_only_while_an_overlay_is_under_the_playhead(app):
    doc = Document()
    a = MediaSource(id="a", path="C:/nope/a.mp4", duration=6.0, fps=30.0, width=1920, height=1080)
    b = MediaSource(id="b", path="C:/nope/b.mp4", duration=2.0, fps=30.0, width=1920, height=1080)
    doc.add_source(a)
    doc.add_source(b, track=1, at=3.0)
    panel = pv.PreviewPanel(doc, _Settings())
    panel._resync_timer.stop()
    try:
        panel._t = 1.0
        assert panel._seek_patience() == pv._SCRUB_COALESCE_MS
        panel._t = 3.5
        assert panel._seek_patience() == pv._SCRUB_PATIENCE_MS
        doc.set_track_hidden(1, True)                      # a hidden overlay does not compete
        assert panel._seek_patience() == pv._SCRUB_COALESCE_MS
    finally:
        panel.shutdown()
        panel.close()


@pytest.mark.parametrize("tracks", [1, 3])
def test_fast_scrub_ends_on_the_last_position(app, video_h, tracks):
    doc = Document()
    doc.add_source(video_h)
    for k in range(1, tracks):
        doc.add_source(video_h, track=k, at=0.0)
        doc.set_transform([doc.timeline.track(k)[0].id], scale=0.5, x=0.2 * k)
    doc.history.clear()
    panel = pv.PreviewPanel(doc, _Settings())
    panel.resize(800, 500)
    panel.show()
    shown = []
    panel.frameDisplayed.connect(lambda img, t: shown.append(t))
    try:
        doc.seek(0.5)
        pump(app, 1.5)
        rng = random.Random(7)
        for _ in range(60):                                # ~125 Hz pointer, random positions
            doc.seek(rng.uniform(0.2, 5.5))
            pump(app, 0.008)
        final = 4.0
        doc.seek(final)
        end = time.monotonic() + 8.0
        ok = False
        while time.monotonic() < end and not ok:
            pump(app, 0.02)
            img = panel.current_image()
            ok = (img is not None and abs(src_time(img) - final) < 2.5 / 30
                  and all(v["has_image"] for v in panel.layer_state().values()))
        assert ok, "the preview did not settle on the last scrub position"
        assert len(shown) >= 3                             # frames kept coming during the scrub
        assert panel.layer_state() == {} or all(
            abs(v["target"] - final) < 2.5 / 30 for v in panel.layer_state().values())
    finally:
        panel.shutdown()
        panel.close()
