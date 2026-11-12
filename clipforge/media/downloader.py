"""YouTube downloads through the public ``yt_dlp`` Python API only.

No cookies, no credentials, no DRM/anti-bot circumvention.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import yt_dlp
from PySide6.QtCore import QObject, Signal

from . import MediaError, ffmpeg

log = logging.getLogger(__name__)

QUALITY_CHOICES = ["best", "2160", "1440", "1080", "720", "480"]

OUTPUT_TEMPLATE = "%(title).120B [%(id)s].%(ext)s"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
             "youtube-nocookie.com", "www.youtube-nocookie.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_MAX_THUMB_BYTES = 8 * 1024 * 1024


@dataclass
class VideoInfo:
    url: str                       # normalised watch URL
    video_id: str
    title: str
    duration: float                # seconds (0 if unknown)
    uploader: str = ""
    thumbnail_url: str = ""
    thumbnail_jpeg: bytes = b""    # downloaded thumbnail image data (may be empty)
    heights: list[int] = field(default_factory=list)   # available video heights, descending
    is_short: bool = False
    is_live: bool = False

    def available_qualities(self) -> list[str]:
        """Subset of ``QUALITY_CHOICES`` this video offers ("best" always first)."""
        out = ["best"]
        if not self.heights:                 # unknown: offer everything
            return list(QUALITY_CHOICES)
        top = max(self.heights)
        for q in QUALITY_CHOICES[1:]:
            if top >= int(q):
                out.append(q)
        return out


# ---------------------------------------------------------------------- URLs
_URL_HELP = ("Paste a YouTube video link, for example "
             "https://www.youtube.com/watch?v=XXXXXXXXXXX")


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def validate_url(url: str) -> str:
    """Return a normalised ``https://www.youtube.com/watch?v=ID`` URL for standard,
    Shorts, youtu.be, embed, music and mobile links (extra query params such as
    ``si``/``t``/``list`` dropped). Raises ``MediaError`` with a helpful message for
    anything else (empty, not a URL, other site, playlist-only link, bad id)."""
    text = (url or "").strip()
    if not text:
        raise MediaError(f"The link is empty. {_URL_HELP}")
    if re.search(r"\s", text):
        raise MediaError(f"That does not look like a single link. {_URL_HELP}")
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlparse(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise MediaError(f"That does not look like a valid link. {_URL_HELP}") from None
    if parts.scheme not in ("http", "https") or not host or "." not in host:
        raise MediaError(f"That does not look like a valid link. {_URL_HELP}")

    segs = [s for s in parts.path.split("/") if s]
    query = parse_qs(parts.query)
    video_id = ""

    if host in _SHORT_HOSTS:
        video_id = segs[0] if segs else ""
    elif host in _YT_HOSTS:
        if segs and segs[0] == "watch":
            video_id = (query.get("v") or [""])[0]
        elif len(segs) >= 2 and segs[0] in ("shorts", "embed", "v", "live"):
            video_id = segs[1]
        elif segs and segs[0] == "playlist":
            raise MediaError("That is a playlist link. ClipForge downloads one video at a "
                             "time - open the video you want and paste its link.")
        elif segs and segs[0] in ("channel", "c", "user", "feed", "results") or (
                segs and segs[0].startswith("@")):
            raise MediaError("That is a channel or search page, not a single video. "
                             "Open the video you want and paste its link.")
        else:
            raise MediaError(f"That YouTube link does not point to a video. {_URL_HELP}")
    else:
        raise MediaError(f"Only YouTube links are supported (got \"{host}\"). {_URL_HELP}")

    if not _ID_RE.match(video_id or ""):
        raise MediaError(f"The video ID in that link is not valid. {_URL_HELP}")
    return _watch_url(video_id)


# --------------------------------------------------------------- format / names
def build_format_selector(quality: str) -> str:
    """yt-dlp format selector for a ``QUALITY_CHOICES`` entry.

    Up to 1080p prefers H.264 + AAC (m4a) at that height for editing/preview
    compatibility; for "best"/1440/2160 the resolution wins over the codec."""
    q = str(quality).strip().lower()
    if q == "best" or not q.isdigit():
        return "bv*+ba/b"
    h = int(q)
    if h > 1080:
        return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
    return (f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]"
            f"/bv*[height<={h}][vcodec^=avc1]+ba"
            f"/bv*[height<={h}]+ba[ext=m4a]"
            f"/bv*[height<={h}]+ba"
            f"/b[height<={h}]/bv*+ba/b")


def unique_path(path: str) -> str:
    """``path`` if free, else ``name (2).ext``, ``name (3).ext`` ..."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{root} ({n}){ext}"):
        n += 1
    return f"{root} ({n}){ext}"


# ------------------------------------------------------------------- errors
_ERROR_RULES: list[tuple[tuple[str, ...], str]] = [
    (("private video", "this video is private"),
     "This video is private, so it cannot be downloaded."),
    (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
     "This video is age-restricted and needs a signed-in account, which ClipForge does "
     "not support."),
    (("not a bot", "confirm you're not a bot", "confirm you’re not a bot"),
     "YouTube is asking for a sign-in / bot check for this video right now. "
     "Try again later or try another video."),
    (("sign in to", "login required", "members-only", "members only", "join this channel"),
     "This video requires signing in or a membership, which ClipForge does not support."),
    (("available in your country", "blocked it in your country", "in your country", "geo restrict",
      "geo-restrict", "geographic"),
     "This video is not available in your country."),
    (("live event", "is live", "live stream", "premieres in", "will begin in", "upcoming"),
     "This is a live stream or an upcoming premiere. Try again once it has ended."),
    (("requested format is not available", "no video formats"),
     "That quality is not available for this video. Pick another quality."),
    (("video unavailable", "this video is unavailable", "not available", "has been removed",
      "no longer available", "account associated with this video has been terminated",
      "does not exist", "copyright", "unavailable"),
     "This video is unavailable (it may have been removed or restricted)."),
    (("http error 429", "too many requests"),
     "YouTube is limiting requests right now. Please wait a few minutes and try again."),
    (("no space left", "errno 28", "disk full"),
     "There is not enough free disk space in the download folder."),
    (("permission denied", "access is denied", "errno 13"),
     "ClipForge cannot write to the download folder. Choose another folder."),
    (("urlopen error", "getaddrinfo", "timed out", "timeout", "connection", "network",
      "temporary failure", "name or service", "unable to download", "unable to connect",
      "remote end closed", "incompleteread", "ssl"),
     "Could not reach YouTube. Check your internet connection and try again."),
    (("ffmpeg", "postprocess", "merg"),
     "The video was downloaded but could not be merged. Please try again."),
]


def translate_error(exc: BaseException | str) -> str:
    """Friendly, jargon-free message for a yt-dlp / network failure. Technical detail
    belongs in the log only."""
    raw = str(exc) if not isinstance(exc, str) else exc
    low = raw.lower()
    for needles, message in _ERROR_RULES:
        if any(n in low for n in needles):
            return message
    return ("The download failed. Check your connection and try again; if it keeps "
            "failing, yt-dlp may need updating (details are in the log file).")


class _YtdlpLogger:
    """Route yt-dlp console output into the application log, never to stdout."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.error("yt-dlp: %s", msg)


class _DownloadCancelled(Exception):
    pass


def _base_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "logger": _YtdlpLogger(),
        "socket_timeout": 20,
        "retries": 5,
        "extractor_retries": 3,
        "color": {"stdout": "never", "stderr": "never"},
    }


def _fetch_thumbnail(urls: list[str]) -> bytes:
    for u in urls:
        if not u.lower().startswith("https://"):
            continue
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "ClipForge"})
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - https only
                data = resp.read(_MAX_THUMB_BYTES + 1)
            if 0 < len(data) <= _MAX_THUMB_BYTES:
                return data
        except Exception as exc:  # noqa: BLE001
            log.debug("Thumbnail fetch failed (%s): %s", u, exc)
    return b""


def _info_from_dict(d: dict, url: str) -> VideoInfo:
    heights = sorted({int(f["height"]) for f in (d.get("formats") or [])
                      if f.get("height") and f.get("vcodec") not in (None, "none")},
                     reverse=True)
    if not heights and d.get("height"):
        heights = [int(d["height"])]
    vid = str(d.get("id") or "")
    page = str(d.get("webpage_url") or "")
    live = bool(d.get("is_live")) or d.get("live_status") in ("is_live", "is_upcoming")
    return VideoInfo(
        url=_watch_url(vid) if _ID_RE.match(vid) else url,
        video_id=vid,
        title=str(d.get("title") or vid or "Video"),
        duration=float(d.get("duration") or 0),
        uploader=str(d.get("uploader") or d.get("channel") or ""),
        thumbnail_url=str(d.get("thumbnail") or ""),
        heights=heights,
        is_short="/shorts/" in page or (
            0 < float(d.get("duration") or 0) <= 180
            and int(d.get("width") or 0) > 0
            and int(d.get("height") or 0) > int(d.get("width") or 0)),
        is_live=live,
    )


# ---------------------------------------------------------------------- jobs
class MetadataJob(QObject):
    """Fetch :class:`VideoInfo` (including thumbnail bytes) on a background thread."""

    finished = Signal(object)      # VideoInfo
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, url: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._url = url
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._done = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="yt-metadata", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        if self._finish_once():
            self.cancelled.emit()

    def _finish_once(self) -> bool:
        with self._lock:
            if self._done:
                return False
            self._done = True
            return True

    def _run(self) -> None:
        try:
            url = validate_url(self._url)
            opts = _base_opts()
            opts.update({"skip_download": True})
            with yt_dlp.YoutubeDL(opts) as ydl:
                data = ydl.extract_info(url, download=False)
            if not isinstance(data, dict):
                raise MediaError("YouTube returned no information for that link.")
            if data.get("_type") == "playlist" and data.get("entries"):
                data = next((e for e in data["entries"] if e), data)
            info = _info_from_dict(data, url)
            if self._cancel.is_set():
                return
            candidates = [info.thumbnail_url] if info.thumbnail_url else []
            if info.video_id:
                candidates.append(f"https://i.ytimg.com/vi/{info.video_id}/hqdefault.jpg")
            info.thumbnail_jpeg = _fetch_thumbnail(candidates)
            if self._finish_once():
                self.finished.emit(info)
        except MediaError as exc:
            if self._finish_once():
                self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            log.error("Metadata fetch failed for %s: %s", self._url, exc, exc_info=True)
            if self._finish_once():
                self.failed.emit(translate_error(exc))


def _fmt_speed(bps: Optional[float]) -> str:
    if not bps or bps <= 0:
        return ""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024 or unit == "GB/s":
            return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"
        bps /= 1024
    return ""


def _fmt_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


class DownloadJob(QObject):
    """Download + merge to a single MP4 in ``dest_dir`` on a background thread.

    ``quality`` is one of ``QUALITY_CHOICES``; an unavailable height falls back to
    the best one below it. Prefers H.264/AAC streams when the height is available in
    them (best editing/preview compatibility), merges with the bundled FFmpeg, never
    overwrites an existing file (adds " (2)" etc.), and keeps partial data so
    ``Retry`` resumes an interrupted download. Cancelling keeps yt-dlp's ``.part``
    fragments too (so a retry resumes) and removes nothing else."""

    progress = Signal(float, str, str)   # fraction 0..1 (-1 unknown), speed text, ETA text
    stage = Signal(str)                  # "Downloading video…", "Merging…", …
    finished = Signal(str, object)       # output file path, VideoInfo
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, info: VideoInfo, quality: str, dest_dir: str,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._info = info
        self._quality = quality if quality in QUALITY_CHOICES else "best"
        self._dest = dest_dir
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # progress bookkeeping (worker thread only)
        self._expected_total: Optional[float] = None
        self._completed_bytes = 0.0
        self._current_file = ""
        self._streams_done = 0
        self._streams_total = 1

    # ------------------------------------------------------------------- API
    def start(self) -> None:
        with self._lock:
            if self.is_running():
                return
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, name="yt-download",
                                            daemon=True)
            self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -------------------------------------------------------------- internals
    def _options(self, ffmpeg_dir: str) -> dict[str, Any]:
        opts = _base_opts()
        opts.update({
            "format": build_format_selector(self._quality),
            "merge_output_format": "mp4",
            "ffmpeg_location": ffmpeg_dir,
            "windowsfilenames": True,
            "outtmpl": {"default": os.path.join(self._dest.replace("%", "%%"),
                                                OUTPUT_TEMPLATE)},
            "continuedl": True,
            "nooverwrites": False,
            "overwrites": False,
            "progress_hooks": [self._on_progress],
            "postprocessor_hooks": [self._on_postprocess],
            "fragment_retries": 5,
        })
        return opts

    def _on_postprocess(self, d: dict) -> None:
        if self._cancel.is_set():
            raise _DownloadCancelled()
        if d.get("status") == "started":
            name = str(d.get("postprocessor") or "")
            if name in ("Merger", "FFmpegMerger", "VideoConvertor", "FFmpegVideoRemuxer"):
                self.stage.emit("Merging…")
                self.progress.emit(-1.0, "", "")

    def _on_progress(self, d: dict) -> None:
        if self._cancel.is_set():
            raise _DownloadCancelled()
        status = d.get("status")
        fname = str(d.get("filename") or d.get("tmpfilename") or "")
        if fname and fname != self._current_file:
            if self._current_file:
                self._streams_done += 1
            self._current_file = fname
            self.stage.emit("Downloading audio…" if self._streams_done > 0
                            and self._streams_total > 1 else "Downloading video…")
        done = float(d.get("downloaded_bytes") or 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if status == "finished":
            self._completed_bytes += float(total or done)
            self.progress.emit(self._overall(0.0, None), "", "")
            return
        if status != "downloading":
            return
        fraction = self._overall(done, float(total) if total else None)
        self.progress.emit(fraction, _fmt_speed(d.get("speed")), _fmt_eta(d.get("eta")))

    def _overall(self, current_done: float, current_total: Optional[float]) -> float:
        if self._expected_total:
            return max(0.0, min(0.99, (self._completed_bytes + current_done)
                                / self._expected_total))
        if current_total and self._streams_total > 0:
            f = (self._streams_done + min(1.0, current_done / current_total)) \
                / self._streams_total
            return max(0.0, min(0.99, f))
        return -1.0

    def _plan(self, ydl: Any, data: dict) -> str:
        """Pick a collision-free final path and pin the output template to it."""
        probe = dict(data)
        probe["ext"] = "mp4"
        expected = ydl.prepare_filename(probe)
        final = unique_path(os.path.splitext(expected)[0] + ".mp4")
        stem = os.path.splitext(final)[0]
        ydl.params["outtmpl"] = {"default": stem.replace("%", "%%") + ".%(ext)s"}
        return final

    def _run(self) -> None:
        self._expected_total = None
        self._completed_bytes = 0.0
        self._current_file = ""
        self._streams_done = 0
        self._streams_total = 1
        try:
            self._run_inner()
        except (_DownloadCancelled, KeyboardInterrupt):
            self._emit_cancelled()
        except MediaError as exc:
            if self._cancel.is_set():
                self._emit_cancelled()
            else:
                log.error("Download failed: %s %s", exc, exc.detail)
                self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            if self._cancel.is_set() or _is_cancel(exc):
                self._emit_cancelled()
                return
            log.error("Download failed for %s: %s", self._info.url, exc, exc_info=True)
            self.failed.emit(translate_error(exc))

    def _emit_cancelled(self) -> None:
        self.stage.emit("Cancelled")
        self.cancelled.emit()

    def _run_inner(self) -> None:
        if self._info.is_live:
            raise MediaError("This is a live stream. Try again once it has ended.")
        try:
            ffmpeg_dir = os.path.dirname(ffmpeg.ffmpeg_path())
        except MediaError:
            raise
        try:
            os.makedirs(self._dest, exist_ok=True)
        except OSError as exc:
            raise MediaError("ClipForge cannot create the download folder. "
                             "Choose another folder.", str(exc)) from exc
        url = validate_url(self._info.url)

        self.stage.emit("Preparing…")
        self.progress.emit(-1.0, "", "")
        opts = self._options(ffmpeg_dir)
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
            if self._cancel.is_set():
                raise _DownloadCancelled()
            if not isinstance(data, dict):
                raise MediaError("YouTube returned no information for that link.")
            if data.get("is_live") or data.get("live_status") in ("is_live", "is_upcoming"):
                raise MediaError("This is a live stream. Try again once it has ended.")
            final = self._plan(ydl, data)
            self._prepare_progress_model(ydl, data)
            self.stage.emit("Downloading video…")
            result = ydl.process_ie_result(data, download=True)
            if self._cancel.is_set():
                raise _DownloadCancelled()
            path = self._result_path(result, final)
        if not path or not os.path.isfile(path):
            raise MediaError("The download finished but the file could not be found. "
                             "Please try again.", f"expected {final}")
        self.progress.emit(1.0, "", "")
        self.stage.emit("Done")
        self.finished.emit(path, self._info)

    def _prepare_progress_model(self, ydl: Any, data: dict) -> None:
        try:
            selector = ydl.build_format_selector(build_format_selector(self._quality))
            ctx = {"formats": data.get("formats") or [], "incomplete_formats": False}
            chosen = next(iter(selector(ctx)), None)
        except Exception as exc:  # noqa: BLE001 - progress estimate only
            log.debug("Could not pre-select formats: %s", exc)
            return
        if not chosen:
            return
        fmts = chosen.get("requested_formats") or [chosen]
        self._streams_total = max(1, len(fmts))
        sizes = [f.get("filesize") or f.get("filesize_approx") for f in fmts]
        if all(sizes):
            self._expected_total = float(sum(sizes))

    @staticmethod
    def _result_path(result: Any, fallback: str) -> str:
        if isinstance(result, dict):
            for rd in result.get("requested_downloads") or []:
                p = rd.get("filepath")
                if p and os.path.isfile(p):
                    return p
            p = result.get("filepath") or result.get("_filename")
            if p and os.path.isfile(p):
                return p
        return fallback


def _is_cancel(exc: BaseException) -> bool:
    seen = 0
    while exc is not None and seen < 6:
        if isinstance(exc, _DownloadCancelled):
            return True
        info = getattr(exc, "exc_info", None)
        nxt = info[1] if info and len(info) > 1 else None
        exc = nxt or exc.__cause__ or exc.__context__
        seen += 1
    return False
