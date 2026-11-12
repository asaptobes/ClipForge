"""Asynchronous, cached audio peaks for waveform drawing."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, Signal

from ..core.model import MediaSource
from . import ffmpeg

log = logging.getLogger(__name__)

#: Peak pairs stored per second of audio.
PEAKS_PER_SECOND = 100

_SAMPLE_RATE = 8000
_WINDOW = _SAMPLE_RATE // PEAKS_PER_SECOND          # samples per peak window
_CHUNK_BYTES = _SAMPLE_RATE * 2 * 4                  # ~4 s of s16le mono
_CACHE_VERSION = 1


def _file_key(path: str) -> Optional[str]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    raw = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|v{_CACHE_VERSION}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]


def _reduce(samples: np.ndarray) -> np.ndarray:
    """int16 samples (multiple of ``_WINDOW``) -> (n, 2) float32 min/max."""
    w = samples.reshape(-1, _WINDOW)
    out = np.empty((w.shape[0], 2), dtype=np.float32)
    out[:, 0] = w.min(axis=1) / 32768.0
    out[:, 1] = w.max(axis=1) / 32767.0
    return out


def extract_peaks(path: str, should_stop=lambda: False) -> Optional[np.ndarray]:
    """Stream mono 8 kHz PCM out of FFmpeg and reduce it incrementally.
    Returns None if stopped; raises on FFmpeg failure."""
    cmd = [ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-i", path, "-vn", "-ac", "1", "-ar", str(_SAMPLE_RATE),
           "-f", "s16le", "pipe:1"]
    import subprocess
    proc = ffmpeg.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err_lines: list[bytes] = []

    def drain() -> None:
        try:
            for line in proc.stderr:            # type: ignore[union-attr]
                if len(err_lines) < 40:
                    err_lines.append(line)
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    parts: list[np.ndarray] = []
    carry = b""
    try:
        while True:
            if should_stop():
                return None
            data = proc.stdout.read(_CHUNK_BYTES)       # type: ignore[union-attr]
            if not data:
                break
            data = carry + data
            usable = len(data) - (len(data) % (2 * _WINDOW))
            carry = data[usable:]
            if usable:
                parts.append(_reduce(np.frombuffer(data[:usable], dtype="<i2")))
        if len(carry) >= 2:        # trailing partial window
            tail = np.frombuffer(carry[: len(carry) - (len(carry) % 2)], dtype="<i2")
            parts.append(np.array([[tail.min() / 32768.0, tail.max() / 32767.0]],
                                  dtype=np.float32))
        rc = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t.join(timeout=2)
    if rc != 0 and not parts:
        raise RuntimeError(b"".join(err_lines).decode("utf-8", "replace").strip()
                           or f"ffmpeg exited with code {rc}")
    if not parts:
        return np.zeros((0, 2), dtype=np.float32)
    return np.concatenate(parts).astype(np.float32, copy=False)


class WaveformProvider(QObject):
    """Asynchronous, cached audio peaks for drawing waveforms.

    Peaks are a float32 array of shape ``(n, 2)`` holding ``(min, max)`` in -1..1
    for consecutive windows of ``1 / PEAKS_PER_SECOND`` seconds of the mono mixdown,
    so source time ``t`` maps to row ``int(t * PEAKS_PER_SECOND)``. Extraction streams
    PCM out of FFmpeg in chunks (constant memory) on a background thread and is cached
    on disk (``.npy``) keyed by file path + size + mtime.
    """

    waveformReady = Signal(str)   # source_id

    def __init__(self, cache_dir: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._cache_dir = ""
        self._lock = threading.Lock()
        self._mem: dict[str, np.ndarray] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.set_cache_dir(cache_dir)

    def get(self, source: MediaSource) -> Optional[np.ndarray]:
        """Peaks if ready, else ``None`` (extraction is started; never blocks).
        Sources without audio return an empty ``(0, 2)`` array immediately."""
        if not source.has_audio:
            return np.zeros((0, 2), dtype=np.float32)
        key = _file_key(source.path)
        if key is None:
            return None
        with self._lock:
            arr = self._mem.get(key)
            if arr is not None:
                return arr
            if self._stop.is_set() or key in self._pending or key in self._failed:
                return None
            cached = self._cache_file(key)
            if cached and os.path.isfile(cached):
                try:
                    arr = np.load(cached)
                    if arr.ndim == 2 and arr.shape[1] == 2:
                        self._mem[key] = arr.astype(np.float32, copy=False)
                        return self._mem[key]
                except Exception as exc:  # noqa: BLE001
                    log.warning("Ignoring corrupt waveform cache %s: %s", cached, exc)
            self._pending.add(key)
            th = threading.Thread(target=self._work, args=(key, source.id, source.path),
                                  name="waveform", daemon=True)
            self._threads = [x for x in self._threads if x.is_alive()] + [th]
            th.start()
        return None

    def set_cache_dir(self, cache_dir: str) -> None:
        with self._lock:
            self._cache_dir = cache_dir or ""
        if cache_dir:
            try:
                os.makedirs(cache_dir, exist_ok=True)
            except OSError as exc:
                log.warning("Cannot create waveform cache %s: %s", cache_dir, exc)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            threads = list(self._threads)
        for th in threads:
            if th is not threading.current_thread():
                th.join(timeout=5)

    # ---------------------------------------------------------------- internals
    def _cache_file(self, key: str) -> str:
        return os.path.join(self._cache_dir, f"wave_{key}.npy") if self._cache_dir else ""

    def _work(self, key: str, source_id: str, path: str) -> None:
        peaks = None
        try:
            peaks = extract_peaks(path, self._stop.is_set)
        except Exception as exc:  # noqa: BLE001
            log.warning("Waveform extraction failed for %s: %s", path, exc)
        with self._lock:
            self._pending.discard(key)
            if peaks is None:
                if not self._stop.is_set():
                    self._failed.add(key)
                return
            self._mem[key] = peaks
            cache = self._cache_file(key)
        if cache:
            try:
                tmp = cache + ".tmp.npy"
                np.save(tmp, peaks)
                os.replace(tmp, cache)
            except OSError as exc:
                log.debug("Waveform cache write failed: %s", exc)
        if not self._stop.is_set():
            try:
                self.waveformReady.emit(source_id)
            except RuntimeError:
                pass
