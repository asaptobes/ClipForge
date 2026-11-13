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


# ------------------------------------------------------------ human-typed times
def parse_timecode(text: str) -> float:
    """Seconds for a typed time: ``"90"``, ``"90.5"``, ``"1:30"``, ``"01:30"``,
    ``"1:02:03"``, ``"1:02:03.5"``. Surrounding spaces are ignored. Raises
    ``ValueError`` with a short, user-facing message for anything else (empty, negative,
    more than three parts, minutes/seconds of 60 or more when a larger unit is given)."""
    raw = str(text if text is not None else "").strip()
    if not raw:
        raise ValueError("Enter a time like 1:30 or 1:02:03.")
    if raw.startswith("-"):
        raise ValueError("The time can't be negative.")
    parts = raw.split(":")
    if len(parts) > 3:
        raise ValueError("Use at most hours:minutes:seconds.")
    nums: list[float] = []
    for i, part in enumerate(parts):
        part = part.strip()
        last = i == len(parts) - 1
        ok = part.replace(".", "", 1).isdigit() if last else part.isdigit()
        if not (part.isascii() and ok) or part in ("", "."):
            raise ValueError("Enter a time like 1:30 or 1:02:03.")
        nums.append(float(part))
    if len(nums) > 1 and nums[-1] >= 60:
        raise ValueError("Seconds must be under 60 (for example 1:30).")
    if len(nums) == 3 and nums[1] >= 60:
        raise ValueError("Minutes must be under 60 (for example 1:02:03).")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def format_clock(seconds: float, always_hours: bool = False) -> str:
    """Compact clock text for typed times: ``"1:30"``, ``"1:02:03"`` (hours shown when
    needed or ``always_hours``), with tenths (``"1:30.5"``) only for non-integer values.

    The UI already uses :func:`format_timecode` (``MM:SS.mmm``) for the editor, so this
    is a separate function rather than a change to its output."""
    seconds = max(0.0, float(seconds))
    tenths = int(round(seconds * 10))
    whole, frac = divmod(tenths, 10)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    tail = f".{frac}" if frac else ""
    if h or always_hours:
        return f"{h}:{m:02d}:{s:02d}{tail}"
    return f"{m}:{s:02d}{tail}"
