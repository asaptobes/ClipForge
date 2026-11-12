"""Locating and safely running FFmpeg / FFprobe.

Every invocation uses an argument list (never a shell string), ``CREATE_NO_WINDOW``
on Windows, ``stdin=DEVNULL`` and UTF-8 decoding with ``errors="replace"``.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import subprocess
import threading
import time
from typing import Callable, Optional, Sequence

from .. import paths
from ..workers import Cancelled, CancelToken
from . import DependencyError, MediaError

log = logging.getLogger(__name__)

CPU_ENCODERS = {"h264": "libx264", "h265": "libx265"}
HARDWARE_ENCODERS = {
    "h264": ["h264_nvenc", "h264_qsv", "h264_amf"],
    "h265": ["hevc_nvenc", "hevc_qsv", "hevc_amf"],
}
_HW_SUFFIXES = ("_nvenc", "_qsv", "_amf", "_vaapi", "_videotoolbox", "_mf")

#: Lines of stderr kept for error reports.
_STDERR_TAIL_LINES = 60

_lock = threading.Lock()
_tool_cache: dict[str, str] = {}
_encoders_cache: Optional[set[str]] = None
_encoder_test_cache: dict[str, bool] = {}
_version_cache: Optional[str] = None


# --------------------------------------------------------------------- plumbing
def creation_flags() -> int:
    """``CREATE_NO_WINDOW`` on Windows so no console window ever flashes up."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def popen(cmd: Sequence[str], **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` with the project-wide safe defaults (no shell, no window,
    no inherited stdin). Callers pass ``stdout``/``stderr`` as needed."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    kwargs.setdefault("creationflags", creation_flags())
    kwargs["shell"] = False
    return subprocess.Popen(list(cmd), **kwargs)


def _tool(name: str) -> str:
    cached = _tool_cache.get(name)
    if cached and os.path.isfile(cached):
        return cached
    found = paths.find_tool(name)
    if not found:
        raise DependencyError(
            f"{name} was not found. ClipForge needs FFmpeg to work - reinstall the "
            f"application or put {name}.exe in its 'ffmpeg' folder.")
    found = os.path.abspath(found)
    _tool_cache[name] = found
    return found


def ffmpeg_path() -> str:
    """Absolute path of ffmpeg (``paths.find_tool``); raises ``DependencyError``."""
    return _tool("ffmpeg")


def ffprobe_path() -> str:
    return _tool("ffprobe")


def missing_dependencies() -> list[str]:
    """Names of required tools that are missing or fail to run (empty = all good)."""
    missing = []
    for name in ("ffmpeg", "ffprobe"):
        try:
            exe = _tool(name)
            proc = subprocess.run([exe, "-version"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  creationflags=creation_flags(), timeout=20)
            if proc.returncode != 0:
                raise OSError(f"exit code {proc.returncode}")
        except (MediaError, OSError, subprocess.SubprocessError) as exc:
            log.error("Dependency check failed for %s: %s", name, exc)
            missing.append(name)
    return missing


def version() -> str:
    """Short FFmpeg version string, e.g. ``"8.1.3"`` ("" if unavailable)."""
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    try:
        out = run_capture(["-version"], tool="ffmpeg", timeout=20)
    except MediaError:
        return ""
    first = out.splitlines()[0] if out else ""
    m = re.search(r"version\s+[nN]?(\d+(?:\.\d+)*)", first)
    if m:
        result = m.group(1)
    else:
        parts = first.split()
        result = parts[2] if len(parts) > 2 else ""
    _version_cache = result
    return result


def _tail(lines, limit: int = _STDERR_TAIL_LINES) -> str:
    return "\n".join(list(lines)[-limit:]).strip()


def _summarise_error(stderr_tail: str) -> str:
    """Pick the most telling line of FFmpeg's stderr for the user-facing message."""
    lines = [ln.strip() for ln in stderr_tail.splitlines() if ln.strip()]
    lowered = [ln.lower() for ln in lines]
    for needle, text in (
        ("no space left on device", "The disk is full."),
        ("permission denied", "Access to a file was denied (is it open in another program?)."),
        ("no such file or directory", "A file needed for this operation could not be found."),
        ("invalid data found when processing input", "A media file is damaged or not a supported video."),
    ):
        if any(needle in ln for ln in lowered):
            return text
    noise = ("conversion failed", "error while", "exiting", "terminating thread")
    for ln in reversed(lines):
        low = ln.lower()
        if any(low.startswith(n) for n in noise):
            continue
        if "error" in low or "invalid" in low or "failed" in low or "cannot" in low:
            return re.sub(r"^\[[^\]]+\]\s*", "", ln)[:300]
    return lines[-1][:300] if lines else ""


def _kill(proc: subprocess.Popen) -> None:
    """Terminate, then kill if the process does not go away."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not stop process %s: %s", proc.pid, exc)


# -------------------------------------------------------------------------- run
def run(args: Sequence[str], *, cancel: Optional[CancelToken] = None,
        on_progress: Optional[Callable[[float], None]] = None,
        timeout: Optional[float] = None) -> str:
    """Run ``ffmpeg <args>`` to completion and return stderr text.

    ``on_progress`` receives the output position in seconds (parsed from
    ``-progress pipe:1``, which this function adds itself); it is called on the
    calling thread. On cancel the process is terminated and ``workers.Cancelled``
    raised. Non-zero exit raises ``MediaError`` with the stderr tail in ``detail``.
    """
    if cancel is not None:
        cancel.raise_if_cancelled()
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
           "-progress", "pipe:1", "-nostats", *[str(a) for a in args]]
    log.debug("ffmpeg %s", subprocess.list2cmdline(cmd[1:]))
    try:
        proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        raise DependencyError(f"FFmpeg could not be started: {exc}", detail=str(exc)) from exc

    stderr_lines: collections.deque[str] = collections.deque(maxlen=400)
    state = {"pos": -1.0}

    def drain_stderr() -> None:
        try:
            for line in proc.stderr:               # type: ignore[union-attr]
                stderr_lines.append(line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass

    def read_progress() -> None:
        try:
            for line in proc.stdout:               # type: ignore[union-attr]
                key, _, value = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms"):
                    # both are microseconds (out_time_ms is a historical misnomer)
                    try:
                        state["pos"] = max(0.0, int(value) / 1_000_000.0)
                    except ValueError:
                        pass                       # "N/A" before the first frame
        except (OSError, ValueError):
            pass

    threads = [threading.Thread(target=drain_stderr, name="ffmpeg-stderr", daemon=True),
               threading.Thread(target=read_progress, name="ffmpeg-progress", daemon=True)]
    for t in threads:
        t.start()

    started = time.monotonic()
    reported = -1.0
    try:
        while True:
            try:
                proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            pos = state["pos"]
            if on_progress is not None and pos >= 0 and pos != reported:
                reported = pos
                on_progress(pos)
            if proc.poll() is not None:
                break
            if cancel is not None and cancel.cancelled:
                _kill(proc)
                raise Cancelled()
            if timeout is not None and time.monotonic() - started > timeout:
                _kill(proc)
                raise MediaError("FFmpeg took too long and was stopped.",
                                 detail=_tail(stderr_lines))
    except BaseException:
        _kill(proc)
        raise
    finally:
        for t in threads:
            t.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    stderr_text = "\n".join(stderr_lines)
    if proc.returncode != 0:
        if cancel is not None and cancel.cancelled:
            raise Cancelled()
        tail = _tail(stderr_lines)
        log.error("ffmpeg failed (exit %s): %s\n%s", proc.returncode,
                  subprocess.list2cmdline(cmd[1:]), tail)
        summary = _summarise_error(tail)
        message = "FFmpeg could not process the video."
        if summary:
            message += f" {summary}"
        raise MediaError(message, detail=tail)
    return stderr_text


def run_capture(tool_args: Sequence[str], *, tool: str = "ffprobe",
                timeout: Optional[float] = 60) -> str:
    """Run ffprobe/ffmpeg and return stdout; raises ``MediaError`` on failure."""
    exe = ffmpeg_path() if tool == "ffmpeg" else ffprobe_path()
    cmd = [exe, *[str(a) for a in tool_args]]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                              creationflags=creation_flags(), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{tool} took too long and was stopped.", detail=str(exc)) from exc
    except OSError as exc:
        raise DependencyError(f"{tool} could not be started: {exc}", detail=str(exc)) from exc
    if proc.returncode != 0:
        tail = _tail((proc.stderr or "").splitlines())
        summary = _summarise_error(tail)
        raise MediaError(f"{tool} failed." + (f" {summary}" if summary else ""), detail=tail)
    return proc.stdout or ""


# --------------------------------------------------------------------- encoders
def available_encoders() -> set[str]:
    """Encoder names compiled into this FFmpeg (cached)."""
    global _encoders_cache
    with _lock:
        if _encoders_cache is not None:
            return set(_encoders_cache)
    try:
        out = run_capture(["-hide_banner", "-encoders"], tool="ffmpeg", timeout=30)
    except MediaError as exc:
        log.error("Could not list FFmpeg encoders: %s", exc)
        return set()
    names: set[str] = set()
    seen_separator = False
    for line in out.splitlines():
        if not seen_separator:
            seen_separator = line.strip().startswith("------")
            continue
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6:
            names.add(parts[1])
    with _lock:
        _encoders_cache = set(names)
    return names


def is_hardware_encoder(name: str) -> bool:
    return bool(name) and name.lower().endswith(_HW_SUFFIXES)


def cpu_encoder(codec: str) -> str:
    """Software encoder for ``codec`` ("h264"/"h265")."""
    return CPU_ENCODERS.get(_normalise_codec(codec), "libx264")


def _normalise_codec(codec: str) -> str:
    c = (codec or "").lower()
    return "h265" if c in ("h265", "hevc", "x265") else "h264"


def _test_encoder(name: str) -> bool:
    """Really encode a few tiny frames: the only reliable way to know that a
    hardware encoder works with the installed GPU/driver."""
    with _lock:
        if name in _encoder_test_cache:
            return _encoder_test_cache[name]
    ok = False
    if name in available_encoders():
        cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error",
               "-f", "lavfi", "-i", "testsrc=size=256x256:rate=30",
               "-frames:v", "5", "-pix_fmt", "yuv420p", "-c:v", name, "-f", "null", "-"]
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                                  creationflags=creation_flags(), timeout=30)
            ok = proc.returncode == 0
            if not ok:
                log.info("Encoder %s is not usable: %s", name, _tail((proc.stderr or "").splitlines(), 5))
        except (OSError, subprocess.SubprocessError, MediaError) as exc:
            log.info("Encoder %s test failed: %s", name, exc)
    else:
        log.debug("Encoder %s is not compiled into this FFmpeg", name)
    with _lock:
        _encoder_test_cache[name] = ok
    return ok


def pick_video_encoder(codec: str, hw_accel: str = "auto") -> str:
    """Choose an encoder for ``codec`` ("h264"/"h265").

    ``hw_accel``: "auto" → first hardware encoder (NVENC, then QSV, then AMF) that
    passes a real tiny test encode, else CPU (libx264/libx265); "on" → same probe
    but still falls back to CPU if none works; "off" → CPU. Results are cached.
    """
    codec = _normalise_codec(codec)
    if (hw_accel or "auto").lower() != "off":
        for name in HARDWARE_ENCODERS[codec]:
            if _test_encoder(name):
                return name
    return CPU_ENCODERS[codec]
