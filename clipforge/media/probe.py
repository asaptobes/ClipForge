"""Inspecting media files with FFprobe."""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from ..core.model import MediaSource, new_id
from . import MediaError
from . import ffmpeg

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".m4v")


def _ratio(text) -> float:
    """Parse ``"30000/1001"`` / ``"25"`` → float (0.0 when unknown or 0/0)."""
    try:
        s = str(text)
        if "/" in s:
            num, den = s.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _number(value, default: float = 0.0) -> float:
    try:
        f = float(value)
        return f if f == f and f not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


def _rotation(stream: dict) -> int:
    """Clockwise display rotation in degrees (0/90/180/270) from the display matrix
    side data or the legacy ``rotate`` tag."""
    angle = None
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            # the display matrix reports counter-clockwise degrees
            angle = -_number(sd.get("rotation"))
            break
    if angle is None:
        tag = (stream.get("tags") or {}).get("rotate")
        if tag is not None:
            angle = _number(tag)
    if angle is None:
        return 0
    return int(round(angle / 90.0)) * 90 % 360


def _name(path: str) -> str:
    return os.path.basename(path) or path


def probe_source(path: str, *, source_url: Optional[str] = None,
                 title: Optional[str] = None) -> MediaSource:
    """Inspect a file with FFprobe and build a ``MediaSource`` (new id).

    width/height are display dimensions (rotation metadata applied), ``fps`` is the
    average frame rate, ``duration`` the container/stream duration in seconds.
    Raises ``MediaError`` with a clear message for missing, corrupt, audio-only or
    unsupported files. Blocking: call from a worker thread.
    """
    path = os.path.abspath(str(path))
    name = _name(path)
    if not os.path.isfile(path):
        raise MediaError(f"File not found: {path}")
    try:
        file_size = os.path.getsize(path)
    except OSError as exc:
        raise MediaError(f"\"{name}\" could not be read: {exc.strerror or exc}") from exc
    if file_size == 0:
        raise MediaError(f"\"{name}\" is empty (0 bytes). The download or copy may not have finished.")

    try:
        out = ffmpeg.run_capture(
            ["-v", "error", "-of", "json", "-show_format", "-show_streams", path],
            tool="ffprobe", timeout=120)
        data = json.loads(out or "{}")
    except MediaError as exc:
        if isinstance(exc, ffmpeg.DependencyError):
            raise
        log.warning("ffprobe failed for %s: %s", path, exc.detail or exc)
        raise MediaError(f"\"{name}\" could not be read. The file is damaged or is not a "
                         f"supported video.", detail=exc.detail) from exc
    except ValueError as exc:
        raise MediaError(f"\"{name}\" could not be read (unexpected FFprobe output).",
                         detail=str(exc)) from exc

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = None
    for s in streams:
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            video = s
            break
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        if audio is not None:
            raise MediaError(f"\"{name}\" has no video track (audio-only files are not supported).")
        raise MediaError(f"\"{name}\" is not a supported video file.")

    width = int(_number(video.get("width")))
    height = int(_number(video.get("height")))
    if width <= 0 or height <= 0:
        raise MediaError(f"\"{name}\" could not be read. The video track is damaged or uses "
                         f"an unsupported format.")
    rotation = _rotation(video)
    if rotation in (90, 270):
        width, height = height, width

    fps = _ratio(video.get("avg_frame_rate"))
    if not (0.5 <= fps <= 1000):
        fps = _ratio(video.get("r_frame_rate"))
    if not (0.5 <= fps <= 1000):
        fps = 30.0

    duration = _number(fmt.get("duration"))
    if duration <= 0:
        duration = _number(video.get("duration"))
    if duration <= 0 and audio is not None:
        duration = _number(audio.get("duration"))
    if duration <= 0:
        raise MediaError(f"\"{name}\" has no readable duration. The file is probably "
                         f"incomplete or damaged.")

    bit_rate = int(_number(fmt.get("bit_rate")))
    if bit_rate <= 0 and duration > 0:
        bit_rate = int(file_size * 8 / duration)

    if title is None:
        title = (fmt.get("tags") or {}).get("title") or os.path.splitext(name)[0]

    return MediaSource(
        id=new_id(),
        path=path,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        has_video=True,
        has_audio=audio is not None,
        video_codec=str(video.get("codec_name") or ""),
        audio_codec=str(audio.get("codec_name") or "") if audio else "",
        pix_fmt=str(video.get("pix_fmt") or ""),
        sample_rate=int(_number(audio.get("sample_rate"))) if audio else 0,
        channels=int(_number(audio.get("channels"))) if audio else 0,
        rotation=rotation,
        bit_rate=bit_rate,
        file_size=file_size,
        title=str(title),
        source_url=source_url,
    )


def load_keyframes(source: MediaSource) -> list[float]:
    """Sorted video keyframe timestamps (seconds); stored on ``source.keyframes`` too.
    Uses packet flags only (no decoding). Blocking.

    Times are relative to the container start time, i.e. the same time base FFmpeg
    seeking (``-ss``) and the editing model use."""
    if not os.path.isfile(source.path):
        raise MediaError(f"File not found: {source.path}")
    try:
        out = ffmpeg.run_capture(
            ["-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=pts_time,flags:format=start_time",
             "-of", "csv=p=1", source.path],
            tool="ffprobe", timeout=600)
    except MediaError as exc:
        raise MediaError(f"The keyframes of \"{_name(source.path)}\" could not be read.",
                         detail=exc.detail) from exc
    start = 0.0
    times: list[float] = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        if parts[0] == "format":
            start = _number(parts[1])
        elif parts[0] == "packet" and len(parts) >= 3 and "K" in parts[2]:
            try:
                times.append(float(parts[1]))
            except ValueError:
                continue                      # pts "N/A"
    keyframes = sorted({round(max(0.0, t - start), 6) for t in times})
    source.keyframes = keyframes
    return keyframes
