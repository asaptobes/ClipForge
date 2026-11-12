"""Export the *sound* of timeline clips as an audio-only file (mp3 / m4a / wav).

Public API
----------
``build_audio_plan(sources, segments, dest_path, fmt=None, *, temp_dir) -> AudioPlan``
    Pure planning (no process is started). ``segments`` are clips of either lane
    (``Segment.is_audio`` or not); ``dest_path`` is the wished file; ``fmt`` is
    ``"mp3" | "m4a" | "wav"`` or ``None`` to take it from the extension of
    ``dest_path`` (default ``mp3``). The extension is corrected to match the format and
    an existing file is never overwritten: ``name.mp3`` becomes ``name (2).mp3`` etc.
    Raises ``MediaError`` for an empty selection, a missing source or an unknown format.

``ExtractAudioJob(sources, segments, dest_path, fmt=None, *, temp_dir, parent=None)``
    Runs the plan on a background thread. Methods: ``start()``, ``cancel()``,
    ``is_running()``, ``wait(timeout=None)``. Signals:

    * ``progress(float, str)``  - 0..1 and a status text
    * ``finished(str)``         - path of the written file
    * ``failed(str)``           - short user-presentable message
    * ``cancelled()``

Behaviour
---------
* The result starts at the earliest selected clip (leading offset ignored) and ends at
  the latest end of any selected clip. Clips keep their timeline position relative to
  each other: **gaps become silence**, overlapping clips (e.g. a video clip and a
  detached audio clip over it) are summed.
* **Muted clips contribute silence** (they still occupy their time, so the timing of
  everything else is preserved); so do clips whose source has no audio stream.
* Output is 48 kHz stereo; mp3 = libmp3lame 192k, m4a = AAC 192k, wav = pcm_s16le.
* Written to ``<name>.partial.<ext>`` and renamed on success after the duration was
  verified; partial files are removed on failure and cancel. Sources are only read.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from PySide6.QtCore import QObject, Signal

from ..core.model import MediaSource, Segment
from ..workers import Cancelled, CancelToken
from . import MediaError, ffmpeg
from .export import (AUDIO_RATE, _audio_clip_chain, _check_source_file, _fmt, _write_text,
                     _MAX_INLINE_ARGS, partial_path)

log = logging.getLogger(__name__)

FORMATS = ("mp3", "m4a", "wav")
_CODEC_ARGS = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
    "m4a": ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
    "wav": ["-c:a", "pcm_s16le"],
}


@dataclass
class AudioPlan:
    output_path: str
    fmt: str
    args: list[str]                 # one ffmpeg run (without the executable)
    duration: float                 # expected length of the result in seconds
    temp_files: list[str] = field(default_factory=list)


def unique_path(path: str) -> str:
    """``path`` if free, else ``name (2).ext``, ``name (3).ext`` ... (never overwrites)."""
    root, ext = os.path.splitext(path)
    cand, n = path, 2
    while os.path.exists(cand) or os.path.exists(partial_path(cand)):
        cand = f"{root} ({n}){ext}"
        n += 1
    return cand


def build_audio_plan(sources: dict[str, MediaSource], segments: Sequence[Segment],
                     dest_path: str, fmt: Optional[str] = None, *, temp_dir: str) -> AudioPlan:
    root, ext = os.path.splitext(dest_path)
    guess = ext.lstrip(".").lower()
    fmt = (fmt or (guess if guess in FORMATS else "mp3")).lower()
    if fmt not in FORMATS:
        raise MediaError(f"The audio format \"{fmt}\" is not supported (use mp3, m4a or wav).")
    if ext.lower() != "." + fmt:
        root = dest_path if not ext or ext.lstrip(".").lower() not in FORMATS else root
        dest_path = f"{root}.{fmt}"

    segs = sorted((s for s in segments if s.duration > 0), key=lambda s: s.timeline_start)
    if not segs:
        raise MediaError("There is nothing to extract.")
    used: list[MediaSource] = []
    for s in segs:
        src = sources.get(s.source_id)
        if src is None:
            raise MediaError("A clip refers to a source that is no longer in the project.")
        _check_source_file(src)
        used.append(src)

    origin = min(s.timeline_start for s in segs)
    total = max(s.timeline_end for s in segs) - origin
    td = _fmt(total)
    inputs: list[str] = []
    chains: list[str] = []
    labels: list[str] = []
    for s, src in zip(segs, used):
        if s.muted or not src.has_audio:
            continue
        idx = sum(1 for a in inputs if a == "-i")
        if s.source_in > 0:
            inputs += ["-ss", _fmt(s.source_in)]
        inputs += ["-t", _fmt(s.duration), "-i", src.path]
        label = f"c{len(labels)}"
        chains.append(_audio_clip_chain(idx, label, _fmt(s.duration), s.timeline_start - origin))
        labels.append(f"[{label}]")

    base = (f"anullsrc=r={AUDIO_RATE}:cl=stereo:d={td},"
            f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo")
    if labels:
        chains.append(base + "[sil]")
        chains.append(
            f"[sil]{''.join(labels)}amix=inputs={len(labels) + 1}:duration=longest:normalize=0,"
            f"alimiter=limit=0.97,apad=whole_dur={td},atrim=duration={td},"
            f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo[outa]")
    else:
        chains.append(base + "[outa]")
    graph = ";".join(chains)

    uid = os.urandom(4).hex()
    temps: list[str] = []
    if len(graph) > _MAX_INLINE_ARGS // 2:
        gpath = os.path.join(temp_dir, f"cf_{uid}_afilter.txt")
        _write_text(gpath, graph)
        temps.append(gpath)
        graph_args = ["-/filter_complex", gpath]
    else:
        graph_args = ["-filter_complex", graph]

    out = unique_path(dest_path)
    args = inputs + graph_args + ["-map", "[outa]", "-vn"] + _CODEC_ARGS[fmt] + [
        "-ar", str(AUDIO_RATE), "-ac", "2", "-map_metadata", "-1", partial_path(out)]
    return AudioPlan(output_path=out, fmt=fmt, args=args, duration=total, temp_files=temps)


def _remove(path: str) -> None:
    for attempt in range(6):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except OSError:
            time.sleep(0.15 * (attempt + 1))
    log.warning("Could not remove temporary file %s", path)


class ExtractAudioJob(QObject):
    """Background job around :func:`build_audio_plan`; see the module docstring."""

    progress = Signal(float, str)
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, sources: dict[str, MediaSource], segments: Sequence[Segment],
                 dest_path: str, fmt: Optional[str] = None, *, temp_dir: str,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._sources = dict(sources)
        self._segments = [s.copy() for s in segments]
        self._dest = dest_path
        self._fmt = fmt
        self._temp_dir = temp_dir
        self._cancel = CancelToken()
        self._thread: Optional[threading.Thread] = None
        self._partial = ""
        self._temps: list[str] = []
        self._last_pct = -1

    def start(self) -> None:
        if self.is_running():
            return
        self._cancel = CancelToken()
        self._thread = threading.Thread(target=self._run, name="clipforge-extract-audio",
                                        daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.cancel()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.is_running()

    def _run(self) -> None:
        outcome, message, final = "failed", "", ""
        try:
            os.makedirs(self._temp_dir, exist_ok=True)
            plan = build_audio_plan(self._sources, self._segments, self._dest, self._fmt,
                                    temp_dir=self._temp_dir)
            self._temps = list(plan.temp_files)
            self._partial = partial_path(plan.output_path)
            os.makedirs(os.path.dirname(plan.output_path) or ".", exist_ok=True)
            self.progress.emit(0.0, "Extracting audio… 0%")

            def on_pos(pos: float) -> None:
                frac = min(0.99, max(0.0, pos / plan.duration)) if plan.duration > 0 else 0.0
                pct = int(frac * 100)
                if pct != self._last_pct:
                    self._last_pct = pct
                    self.progress.emit(frac, f"Extracting audio… {pct}%")

            ffmpeg.run(plan.args, cancel=self._cancel, on_progress=on_pos)
            self._cancel.raise_if_cancelled()
            self._verify(plan)
            self._cancel.raise_if_cancelled()
            if os.path.exists(plan.output_path):             # raced with another writer
                plan.output_path = unique_path(plan.output_path)
            os.replace(self._partial, plan.output_path)
            self.progress.emit(1.0, "Done")
            final, outcome = plan.output_path, "finished"
        except Cancelled:
            outcome = "cancelled"
        except MediaError as exc:
            message = str(exc) or "The audio could not be extracted."
        except OSError as exc:
            log.exception("Audio extraction I/O error")
            message = f"The audio file could not be written: {exc.strerror or exc}"
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected audio extraction error")
            message = f"The audio extraction failed unexpectedly: {exc}"
        finally:
            if self._partial:
                _remove(self._partial)
            for t in self._temps:
                _remove(t)
        if outcome == "finished":
            self.finished.emit(final)
        elif outcome == "cancelled":
            self.cancelled.emit()
        else:
            self.failed.emit(message)

    def _verify(self, plan: AudioPlan) -> None:
        if not os.path.isfile(self._partial):
            raise MediaError("The audio extraction did not produce a file.")
        try:
            out = ffmpeg.run_capture(["-v", "error", "-show_entries", "format=duration",
                                      "-of", "default=nw=1:nk=1", self._partial], timeout=60)
            got = float(out.strip().splitlines()[0])
        except (MediaError, ValueError, IndexError) as exc:
            raise MediaError("The audio file could not be read back, so it was discarded.") from exc
        if abs(got - plan.duration) > 0.25:
            raise MediaError(f"The audio file is {got:.2f} s long instead of the expected "
                             f"{plan.duration:.2f} s, so it was discarded.")
