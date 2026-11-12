"""Timecode formatting shared by the UI and logs."""

from __future__ import annotations


def format_timecode(seconds: float, fps: float = 0.0, show_hours: bool = False) -> str:
    """``MM:SS.mmm`` (or ``HH:MM:SS.mmm``); with ``fps`` > 0 → ``MM:SS:FF`` frames."""
    seconds = max(0.0, float(seconds))
    if fps and fps > 0:
        total_frames = int(round(seconds * fps))
        nominal = max(1, int(round(fps)))
        ff = total_frames % nominal
        whole = total_frames // nominal
        tail = f":{ff:02d}"
    else:
        whole = int(seconds)
        ms = int(round((seconds - whole) * 1000))
        if ms >= 1000:
            whole, ms = whole + 1, 0
        tail = f".{ms:03d}"
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    if h or show_hours:
        return f"{h:02d}:{m:02d}:{s:02d}{tail}"
    return f"{m:02d}:{s:02d}{tail}"


def format_duration(seconds: float) -> str:
    """Compact ``H:MM:SS`` / ``M:SS`` for lists and labels."""
    seconds = int(round(max(0.0, seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"
