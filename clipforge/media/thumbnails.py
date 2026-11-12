"""Asynchronous, cached timeline thumbnails decoded with PyAV."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections import OrderedDict
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from ..core.model import MediaSource

log = logging.getLogger(__name__)

#: Height in pixels thumbnails are decoded/cached at (the timeline scales them down).
THUMB_HEIGHT = 72
#: Time quantisation step in seconds.
TIME_STEP = 0.5
MEMORY_CACHE_SIZE = 600
_JPEG_QUALITY = 82


def _quantise(t: float) -> float:
    if not (t == t) or t < 0:
        t = 0.0
    return round(round(t / TIME_STEP) * TIME_STEP, 3)


def _file_key(path: str) -> Optional[str]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    raw = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|{THUMB_HEIGHT}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]


def _ndarray_to_qimage(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(arr)
    h, w, _ = arr.shape
    img = QImage(arr.data, w, h, arr.strides[0], QImage.Format.Format_RGB888)
    return img.copy()      # detach from the numpy buffer


class ThumbnailProvider(QObject):
    """Asynchronous, cached timeline thumbnails (PyAV keyframe-seek decoding).

    ``get`` never blocks: it returns the cached image or ``None`` and schedules the
    decode on a background thread, emitting ``thumbnailReady`` when it lands.
    Memory cache is LRU-bounded; images are also stored on disk (JPEG) under
    ``cache_dir`` keyed by file path + size + mtime, so reopening a project is instant.
    A whole video is never decoded into memory.
    """

    thumbnailReady = Signal(str, float)   # source_id, quantised time

    def __init__(self, cache_dir: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._cache_dir = ""
        self._cond = threading.Condition(threading.Lock())
        self._mem: "OrderedDict[tuple, QImage]" = OrderedDict()
        self._failed: set[tuple] = set()
        self._queue: list[tuple] = []          # last = newest = decoded first
        self._queued: set[tuple] = set()
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self.set_cache_dir(cache_dir)

    # ------------------------------------------------------------------ public
    def get(self, source: MediaSource, time: float) -> Optional[QImage]:
        """Thumbnail nearest to source ``time`` (quantised to 0.5 s steps internally)."""
        return self._request(source, _quantise(time))

    def poster(self, source: MediaSource) -> Optional[QImage]:
        """Representative frame for the media library (same async behaviour as ``get``)."""
        return self._request(source, _quantise(max(0.0, source.duration) * 0.1))

    def set_cache_dir(self, cache_dir: str) -> None:
        with self._cond:
            self._cache_dir = cache_dir or ""
        if cache_dir:
            try:
                os.makedirs(cache_dir, exist_ok=True)
            except OSError as exc:
                log.warning("Cannot create thumbnail cache %s: %s", cache_dir, exc)

    def cancel_pending(self) -> None:
        """Drop queued requests that have not started (called when zoom/scroll changes)."""
        with self._cond:
            self._queue.clear()
            self._queued.clear()

    def shutdown(self) -> None:
        """Stop worker threads; safe to call more than once."""
        with self._cond:
            self._stop = True
            self._queue.clear()
            self._queued.clear()
            self._cond.notify_all()
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    # ---------------------------------------------------------------- internals
    def _request(self, source: MediaSource, qt: float) -> Optional[QImage]:
        fk = _file_key(source.path)
        if fk is None:
            return None
        key = (fk, qt)
        with self._cond:
            img = self._mem.get(key)
            if img is not None:
                self._mem.move_to_end(key)
                return img
            if self._stop or key in self._failed:
                return None
            disk = self._disk_path(key)
            if disk and os.path.isfile(disk):      # tiny JPEG: cheap to load inline
                img = QImage(disk)
                if not img.isNull():
                    self._remember(key, img)
                    return img
            if key in self._queued:                # bump: newest request decodes first
                self._queue = [q for q in self._queue if q[0] != key]
            else:
                self._queued.add(key)
            self._queue.append((key, source.id, source.path, source.rotation, qt))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="thumbnails",
                                                daemon=True)
                self._thread.start()
            self._cond.notify()
        return None

    def _disk_path(self, key: tuple) -> str:
        if not self._cache_dir:
            return ""
        return os.path.join(self._cache_dir, f"{key[0]}_{int(round(key[1] * 1000))}.jpg")

    def _remember(self, key: tuple, img: QImage) -> None:
        self._mem[key] = img
        self._mem.move_to_end(key)
        while len(self._mem) > MEMORY_CACHE_SIZE:
            self._mem.popitem(last=False)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                key, source_id, path, rotation, qt = self._queue.pop()
                self._queued.discard(key)
            img = None
            try:
                img = self._decode(path, rotation, qt)
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                log.warning("Thumbnail decode failed for %s @ %.1fs: %s", path, qt, exc)
            with self._cond:
                if self._stop:
                    return
                if img is None:
                    self._failed.add(key)
                    continue
                self._remember(key, img)
                disk = self._disk_path(key)
            if disk:
                try:
                    tmp = disk + ".tmp.jpg"
                    if img.save(tmp, "JPG", _JPEG_QUALITY):
                        os.replace(tmp, disk)
                except OSError as exc:
                    log.debug("Thumbnail disk write failed: %s", exc)
            try:
                self.thumbnailReady.emit(source_id, qt)
            except RuntimeError:       # QObject already deleted
                return

    @staticmethod
    def _decode(path: str, rotation: int, qt: float) -> Optional[QImage]:
        import av

        with av.open(path) as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                return None
            tb = float(stream.time_base) if stream.time_base else 0.0
            start = float(stream.start_time * tb) if stream.start_time is not None else 0.0
            target = qt + start
            if tb > 0 and target > 0.01:
                try:
                    container.seek(int(target / tb), stream=stream, backward=True)
                except av.AVError:
                    container.seek(0)
            frame = None
            try:
                for f in container.decode(stream):
                    frame = f
                    if f.pts is None or tb <= 0 or f.pts * tb >= target - 1e-3:
                        break
            except av.AVError:
                if frame is None:
                    raise
            if frame is None or frame.width <= 0 or frame.height <= 0:
                return None

            rot = rotation % 360
            w, h = frame.width, frame.height
            disp_w, disp_h = (h, w) if rot in (90, 270) else (w, h)
            out_h = THUMB_HEIGHT
            out_w = max(2, int(round(disp_w * out_h / disp_h)))
            sw, sh = (out_h, out_w) if rot in (90, 270) else (out_w, out_h)
            arr = frame.reformat(width=sw, height=sh, format="rgb24").to_ndarray()
            if rot:
                arr = np.rot90(arr, k=-(rot // 90))        # clockwise
            return _ndarray_to_qimage(arr)
