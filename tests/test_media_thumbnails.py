"""ThumbnailProvider tests using small synthetic videos made with the bundled FFmpeg."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from PySide6.QtGui import QImage

from clipforge.core.model import MediaSource
from clipforge.media.probe import probe_source
from clipforge.media.thumbnails import THUMB_HEIGHT, ThumbnailProvider

FFMPEG = str(Path(__file__).resolve().parent.parent / "vendor" / "ffmpeg" / "ffmpeg.exe")

pytestmark = pytest.mark.skipif(not os.path.isfile(FFMPEG), reason="bundled ffmpeg missing")


def _ff(*args: str) -> None:
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, stdin=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("thumbs")
    land = str(d / "land.mp4")      # 160x90, solid red left half, 4 s, keyframe every 1 s
    _ff("-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=4", "-vf",
        "drawbox=x=0:y=0:w=80:h=90:c=red:t=fill", "-c:v", "libx264", "-g", "25",
        "-pix_fmt", "yuv420p", land)
    plain = str(d / "plain.mp4")
    _ff("-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=4", "-c:v", "libx264", "-g", "25",
        "-pix_fmt", "yuv420p", plain)
    # clockwise 90 degrees display rotation metadata (ffmpeg's option is counter-clockwise)
    rot = str(d / "rot.mp4")
    _ff("-display_rotation", "-90", "-i", land, "-c", "copy", rot)
    bad = d / "bad.mp4"
    bad.write_bytes(b"this is not a video" * 50)
    return {"dir": d, "land": land, "plain": plain, "rot": rot, "bad": str(bad)}


@pytest.fixture
def provider(tmp_path):
    p = ThumbnailProvider(str(tmp_path / "cache"))
    yield p
    p.shutdown()


def _wait_image(qtbot, fn, timeout=10000):
    box = {}

    def ok():
        box["img"] = fn()
        return box["img"] is not None
    qtbot.waitUntil(ok, timeout=timeout)
    return box["img"]


def test_async_decode_and_signal(qtbot, provider, media):
    src = probe_source(media["plain"])
    assert provider.get(src, 1.2) is None          # first call never blocks
    with qtbot.waitSignal(provider.thumbnailReady, timeout=10000) as sig:
        pass
    assert sig.args[0] == src.id
    assert sig.args[1] == 1.0                       # quantised to 0.5 s
    img = provider.get(src, 1.2)
    assert isinstance(img, QImage) and not img.isNull()
    assert img.height() == THUMB_HEIGHT
    assert abs(img.width() - 128) <= 2              # 16:9 aspect


def test_quantisation_shares_entries(qtbot, provider, media):
    src = probe_source(media["plain"])
    provider.get(src, 1.1)
    img = _wait_image(qtbot, lambda: provider.get(src, 1.1))
    assert provider.get(src, 0.9) is not None       # 0.9 and 1.1 both -> 1.0
    assert provider.get(src, 1.2).cacheKey() == img.cacheKey()


def test_frames_differ_over_time(qtbot, provider, media):
    src = probe_source(media["plain"])
    provider.get(src, 0.0)
    provider.get(src, 3.0)
    a = _wait_image(qtbot, lambda: provider.get(src, 0.0))
    b = _wait_image(qtbot, lambda: provider.get(src, 3.0))
    assert a != b


def test_rotation_honoured(qtbot, provider, media):
    src = probe_source(media["rot"])
    assert src.rotation == 90 and src.is_vertical
    provider.get(src, 0.5)
    img = _wait_image(qtbot, lambda: provider.get(src, 0.5))
    assert img.height() == THUMB_HEIGHT
    assert img.width() < img.height()               # vertical thumbnail
    # red half was on the left; rotated clockwise it must be on top
    top = img.pixelColor(img.width() // 2, 8)
    bottom = img.pixelColor(img.width() // 2, img.height() - 8)
    assert top.red() > 150 and top.blue() < 100
    assert not (bottom.red() > 150 and bottom.blue() < 100)   # not the red half


def test_disk_cache_reused_by_new_provider(qtbot, tmp_path, media):
    cache = str(tmp_path / "c")
    src = probe_source(media["plain"])
    p1 = ThumbnailProvider(cache)
    try:
        p1.get(src, 2.0)
        _wait_image(qtbot, lambda: p1.get(src, 2.0))
    finally:
        p1.shutdown()
    jpgs = [f for f in os.listdir(cache) if f.endswith(".jpg")]
    assert jpgs
    p2 = ThumbnailProvider(cache)
    try:
        assert p2.get(src, 2.0) is not None         # immediate: served from disk
    finally:
        p2.shutdown()


def test_cache_invalidated_when_file_changes(qtbot, tmp_path, media):
    cache = str(tmp_path / "c")
    copy = str(tmp_path / "copy.mp4")
    with open(media["plain"], "rb") as f:
        data = f.read()
    with open(copy, "wb") as f:
        f.write(data)
    src = probe_source(copy)
    p = ThumbnailProvider(cache)
    try:
        p.get(src, 1.0)
        _wait_image(qtbot, lambda: p.get(src, 1.0))
        os.utime(copy, (time.time() + 100, time.time() + 100))
        assert p.get(src, 1.0) is None              # new mtime -> new key -> miss
    finally:
        p.shutdown()


def test_poster_near_ten_percent(qtbot, provider, media):
    src = probe_source(media["plain"])
    provider.poster(src)
    with qtbot.waitSignal(provider.thumbnailReady, timeout=10000) as sig:
        pass
    assert sig.args[1] == 0.5                       # 10% of ~4 s = 0.4 -> 0.5
    assert provider.poster(src) is not None


def test_corrupt_and_missing_files_yield_none(qtbot, provider, media, tmp_path):
    bad = MediaSource(id="bad", path=media["bad"], duration=5)
    missing = MediaSource(id="gone", path=str(tmp_path / "nope.mp4"), duration=5)
    assert provider.get(bad, 1.0) is None
    assert provider.get(missing, 1.0) is None
    qtbot.wait(500)
    assert provider.get(bad, 1.0) is None           # still None, no crash
    good = probe_source(media["plain"])
    provider.get(good, 1.0)
    assert _wait_image(qtbot, lambda: provider.get(good, 1.0)) is not None  # worker alive


def test_memory_cache_bounded(provider):
    from clipforge.media import thumbnails
    img = QImage(2, 2, QImage.Format.Format_RGB888)
    for i in range(thumbnails.MEMORY_CACHE_SIZE + 50):
        provider._remember(("k", float(i)), img)
    assert len(provider._mem) == thumbnails.MEMORY_CACHE_SIZE
    assert ("k", 0.0) not in provider._mem


def test_cancel_pending_drops_queue_and_shutdown_idempotent(media, tmp_path):
    p = ThumbnailProvider(str(tmp_path / "c"))
    src = probe_source(media["plain"])
    for i in range(20):
        p.get(src, i * 0.5)
    p.cancel_pending()
    assert not p._queue
    p.shutdown()
    p.shutdown()
    assert p.get(src, 3.5) is None                  # after shutdown: no work scheduled
    p.set_cache_dir(str(tmp_path / "other"))
