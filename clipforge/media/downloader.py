"""Downloads (YouTube, Reddit, TikTok, Instagram, Twitch and Kick public videos/clips) through the public
``yt_dlp`` Python API only.

No cookies, no credentials, no DRM/anti-bot circumvention. Users are responsible for
having the right to download what they fetch.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QObject, Signal

from ..core.timecode import format_clock
from . import MediaError, ffmpeg, ytdlp_update

# A newer yt-dlp installed by the in-app updater must win over the bundled copy, so it
# is activated before the first ``import yt_dlp``. Never raises.
ytdlp_update.activate_override()

import yt_dlp  # noqa: E402

log = logging.getLogger(__name__)

QUALITY_CHOICES = ["best", "2160", "1440", "1080", "720", "480"]

OUTPUT_TEMPLATE = "%(title).120B [%(id)s].%(ext)s"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
             "youtube-nocookie.com", "www.youtube-nocookie.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_MAX_THUMB_BYTES = 8 * 1024 * 1024

SUPPORTED_SITES: dict[str, str] = {"youtube": "YouTube", "reddit": "Reddit",
                                   "tiktok": "TikTok", "instagram": "Instagram",
                                   "twitch": "Twitch", "kick": "Kick"}
_SITE_HOSTS: dict[str, frozenset[str]] = {
    "youtube": frozenset(_YT_HOSTS | _SHORT_HOSTS),
    "reddit": frozenset({"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com",
                         "m.reddit.com", "redd.it", "v.redd.it"}),
    "tiktok": frozenset({"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com",
                         "vt.tiktok.com"}),
    "instagram": frozenset({"instagram.com", "www.instagram.com", "instagr.am"}),
    "twitch": frozenset({"twitch.tv", "www.twitch.tv", "m.twitch.tv", "clips.twitch.tv"}),
    "kick": frozenset({"kick.com", "www.kick.com"}),
}
_HOST_TO_SITE = {h: site for site, hosts in _SITE_HOSTS.items() for h in hosts}


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
    site: str = "youtube"          # key of ``SUPPORTED_SITES``

    def available_qualities(self) -> list[str]:
        """Qualities this video offers ("best" always first). YouTube: the subset of
        ``QUALITY_CHOICES``. Other sites: the real heights when the site reports a
        ladder (two or more), otherwise just ``["best"]``."""
        out = ["best"]
        if self.site != "youtube":
            ladder = sorted({int(h) for h in self.heights if int(h) > 0}, reverse=True)
            return out + [str(h) for h in ladder] if len(ladder) >= 2 else out
        if not self.heights:                 # unknown: offer everything
            return list(QUALITY_CHOICES)
        top = max(self.heights)
        for q in QUALITY_CHOICES[1:]:
            if top >= int(q):
                out.append(q)
        return out


# ---------------------------------------------------------------------- URLs
_URL_HELP = "Paste a video link from YouTube, Reddit, TikTok, Instagram, Twitch or Kick."

_REDDIT_SUB_RE = re.compile(r"^[A-Za-z0-9_-]{2,40}$")
_REDDIT_ID_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")
_TIKTOK_USER_RE = re.compile(r"^[\w.\-]{1,64}$")
_TIKTOK_CODE_RE = re.compile(r"^[A-Za-z0-9_]{4,40}$")
_IG_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,40}$")
_IG_USER_RE = re.compile(r"^[A-Za-z0-9_.]{1,40}$")
_TWITCH_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
_TWITCH_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{3,120}$")
_KICK_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_KICK_UUID_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
_KICK_CLIP_RE = re.compile(r"^clip_[A-Za-z0-9_-]{4,64}$")
# first path segments on twitch.tv / kick.com that are site pages, not channels
_TWITCH_RESERVED = frozenset({"directory", "videos", "search", "downloads", "settings", "p",
                              "collections", "turbo", "jobs", "store", "login", "signup",
                              "subscriptions", "inventory", "wallet", "drops", "friends",
                              "messages", "moderator", "popout", "embed"})
_KICK_RESERVED = frozenset({"video", "categories", "search", "auth", "browse", "dashboard",
                            "following", "settings"})
_CHANNEL_PAGE_MESSAGE = "This is a channel page; paste a video or clip link."


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _split_url(url: str):
    """Parse ``url`` defensively. Returns ``(parts, host)`` or ``None`` when it is not a
    plain http(s) URL without userinfo / port / control characters."""
    text = (url or "").strip()
    if not text or re.search(r"[\s\x00-\x1f\x7f\\]", text):
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlparse(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or "." not in host:
        return None
    if "@" in parts.netloc or port is not None:
        return None
    return parts, host


def site_of(url: str) -> Optional[str]:
    """Key of ``SUPPORTED_SITES`` for a URL whose host is exactly on the allow-list
    (``youtube``, ``reddit``, ``tiktok``, ``instagram``, ``twitch``, ``kick``), else ``None``. Does not check
    that the path points at a video - use :func:`validate_url` for that."""
    try:
        split = _split_url(url)
    except Exception:  # noqa: BLE001
        return None
    return _HOST_TO_SITE.get(split[1]) if split else None


def _validate_youtube(host: str, segs: list[str], query: dict) -> str:
    video_id = ""
    if host in _SHORT_HOSTS:
        video_id = segs[0] if segs else ""
    elif segs and segs[0] == "watch":
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
    if not _ID_RE.match(video_id or ""):
        raise MediaError(f"The video ID in that link is not valid. {_URL_HELP}")
    return _watch_url(video_id)


def _validate_reddit(host: str, segs: list[str]) -> str:
    if host == "redd.it":
        if len(segs) == 1 and _REDDIT_ID_RE.match(segs[0]):
            return f"https://redd.it/{segs[0]}"
    elif host == "v.redd.it":
        if len(segs) >= 1 and _REDDIT_ID_RE.match(segs[0]):
            return f"https://v.redd.it/{segs[0]}"
    else:
        # /r/<sub>/comments/<id>[/slug[/comment]]  or  /user/<name>/comments/<id>
        if len(segs) >= 4 and segs[0] in ("r", "user") and segs[2] == "comments"                 and _REDDIT_SUB_RE.match(segs[1]) and _REDDIT_ID_RE.match(segs[3]):
            return f"https://www.reddit.com/{segs[0]}/{segs[1]}/comments/{segs[3]}/"
    raise MediaError("That Reddit link does not point to a single post. Open the post "
                     "with the video and paste its link.")


def _validate_tiktok(host: str, segs: list[str]) -> str:
    if host in ("vm.tiktok.com", "vt.tiktok.com"):
        if len(segs) == 1 and _TIKTOK_CODE_RE.match(segs[0]):
            return f"https://{host}/{segs[0]}/"
    elif len(segs) == 3 and segs[0].startswith("@") and segs[1] == "video"             and segs[2].isascii() and segs[2].isdigit() and len(segs[2]) <= 25             and _TIKTOK_USER_RE.match(segs[0][1:]):
        return f"https://www.tiktok.com/{segs[0]}/video/{segs[2]}"
    elif len(segs) == 2 and segs[0] == "t" and _TIKTOK_CODE_RE.match(segs[1]):
        return f"https://www.tiktok.com/t/{segs[1]}/"
    raise MediaError("That TikTok link does not point to a single video. Open the video "
                     "and paste its link.")


def _validate_instagram(host: str, segs: list[str]) -> str:
    kinds = {"p": "p", "tv": "tv", "reel": "reel", "reels": "reel"}
    if len(segs) == 3 and segs[0] != "share" and _IG_USER_RE.match(segs[0]):
        segs = segs[1:]                       # /<user>/reel/<code>
    if len(segs) >= 2 and segs[0] in kinds and _IG_CODE_RE.match(segs[1])             and segs[1] != "audio":
        return f"https://www.instagram.com/{kinds[segs[0]]}/{segs[1]}/"
    raise MediaError("That Instagram link does not point to a single public post or reel. "
                     "Open the post and paste its link.")


def _validate_twitch(host: str, segs: list[str]) -> str:
    if host == "clips.twitch.tv":
        if len(segs) == 1 and segs[0] != "embed" and _TWITCH_SLUG_RE.match(segs[0]):
            return f"https://clips.twitch.tv/{segs[0]}"
    elif len(segs) == 2 and segs[0] == "videos" and segs[1].isascii() and segs[1].isdigit() \
            and len(segs[1]) <= 20:
        return f"https://www.twitch.tv/videos/{segs[1]}"
    elif len(segs) == 3 and segs[1] in ("v", "video") and _TWITCH_CHANNEL_RE.match(segs[0]) \
            and segs[2].isascii() and segs[2].isdigit() and len(segs[2]) <= 20:
        return f"https://www.twitch.tv/videos/{segs[2]}"        # /<channel>/v/<id>
    elif len(segs) == 3 and segs[1] == "clip" and _TWITCH_CHANNEL_RE.match(segs[0]) \
            and _TWITCH_SLUG_RE.match(segs[2]):
        return f"https://www.twitch.tv/{segs[0]}/clip/{segs[2]}"
    elif len(segs) == 2 and segs[0] == "clip" and _TWITCH_SLUG_RE.match(segs[1]):
        return f"https://www.twitch.tv/clip/{segs[1]}"
    elif segs and segs[0] not in _TWITCH_RESERVED and _TWITCH_CHANNEL_RE.match(segs[0]):
        raise MediaError(_CHANNEL_PAGE_MESSAGE)
    raise MediaError("That Twitch link does not point to a single video or clip. Open the "
                     "VOD or clip and paste its link.")


def _validate_kick(host: str, segs: list[str], query: dict) -> str:
    if len(segs) == 3 and segs[1] == "videos" and _KICK_CHANNEL_RE.match(segs[0]) \
            and _KICK_UUID_RE.match(segs[2].lower()):
        return f"https://kick.com/{segs[0]}/videos/{segs[2].lower()}"
    if len(segs) == 3 and segs[1] == "clips" and _KICK_CHANNEL_RE.match(segs[0]) \
            and _KICK_CLIP_RE.match(segs[2]):
        return f"https://kick.com/{segs[0]}/clips/{segs[2]}"
    clip = (query.get("clip") or [""])[0]
    if len(segs) == 1 and _KICK_CHANNEL_RE.match(segs[0]) and _KICK_CLIP_RE.match(clip):
        return f"https://kick.com/{segs[0]}/clips/{clip}"       # /<channel>?clip=clip_...
    if segs and segs[0] not in _KICK_RESERVED and _KICK_CHANNEL_RE.match(segs[0]) and (
            len(segs) == 1 or (len(segs) == 2 and segs[1] in ("videos", "clips"))):
        raise MediaError(_CHANNEL_PAGE_MESSAGE)
    raise MediaError("That Kick link does not point to a single video or clip. Open the "
                     "VOD or clip and paste its link.")


def validate_url(url: str) -> str:
    """Return a normalised URL for a single video/post on a supported site.

    YouTube (standard, Shorts, youtu.be, embed, music, mobile) becomes
    ``https://www.youtube.com/watch?v=ID``. Reddit posts, TikTok videos and Instagram
    posts/reels, Twitch VODs/clips and Kick VODs/clips are reduced to their canonical
    form without tracking parameters (a ``?t=`` start time is dropped: sections are
    chosen explicitly). The
    host must match the allow-list exactly. Raises ``MediaError`` with a helpful message
    for anything else (empty, not a URL, other site, playlist/profile link, bad id)."""
    text = (url or "").strip()
    if not text:
        raise MediaError(f"The link is empty. {_URL_HELP}")
    if re.search(r"\s", text):
        raise MediaError(f"That does not look like a single link. {_URL_HELP}")
    split = _split_url(text)
    if split is None:
        raise MediaError(f"That does not look like a valid link. {_URL_HELP}")
    parts, host = split
    site = _HOST_TO_SITE.get(host)
    if site is None:
        raise MediaError(f"That site is not supported (\"{host}\"). {_URL_HELP}")
    segs = [s for s in parts.path.split("/") if s]
    if site == "youtube":
        return _validate_youtube(host, segs, parse_qs(parts.query))
    if site == "reddit":
        return _validate_reddit(host, segs)
    if site == "tiktok":
        return _validate_tiktok(host, segs)
    if site == "twitch":
        return _validate_twitch(host, segs)
    if site == "kick":
        return _validate_kick(host, segs, parse_qs(parts.query))
    return _validate_instagram(host, segs)


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
LOGIN_MESSAGE = ("This post needs a login or isn't public. ClipForge doesn't store "
                 "passwords or cookies, so it can only download public posts.")
UPDATE_HINT = ("The site may have changed. Update the downloader in Settings "
               "(Downloader > Update) and try again.")
NO_VIDEO_MESSAGE = "This post has no video."
LIVE_MESSAGE = "Live streams can't be downloaded; wait until the VOD is available."
SUBONLY_MESSAGE = ("This VOD is for subscribers only. ClipForge doesn't sign in to "
                   "accounts, so it can only download public videos.")
EXPIRED_MESSAGE = ("This VOD is no longer available; it has expired or been deleted by "
                   "the streamer.")

# {site} = display name of the site, {noun} = "video" (YouTube) or "post" (others)
_ERROR_RULES: list[tuple[tuple[str, ...], str]] = [
    (("private video", "this video is private"),
     "This video is private, so it cannot be downloaded."),
    (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
     "This video is age-restricted and needs a signed-in account, which ClipForge does "
     "not support."),
    (("not a bot", "confirm you're not a bot", "confirm you’re not a bot"),
     "{site} is asking for a sign-in / bot check for this {noun} right now. "
     "Try again later or try another {noun}."),
    (("sign in to", "login required", "members-only", "members only", "join this channel"),
     "This video requires signing in or a membership, which ClipForge does not support."),
    (("available in your country", "blocked it in your country", "in your country", "geo restrict",
      "geo-restrict", "geographic"),
     "This {noun} is not available in your country."),
    (("live event", "is live", "live stream", "premieres in", "will begin in", "upcoming"),
     "This is a live stream or an upcoming premiere. Try again once it has ended."),
    (("requested format is not available", "no video formats"),
     "That quality is not available for this {noun}. Pick another quality."),
    (("http error 404", "404: not found", "page not found", "has been deleted",
      "was removed", "removed by", "video unavailable", "this video is unavailable",
      "not available", "has been removed", "no longer available",
      "account associated with this video has been terminated",
      "does not exist", "copyright", "unavailable"),
     "This {noun} is unavailable (it may have been removed or restricted)."),
    (("http error 429", "too many requests"),
     "{site} is limiting requests right now. Please wait a few minutes and try again."),
    (("no space left", "errno 28", "disk full"),
     "There is not enough free disk space in the download folder."),
    (("permission denied", "access is denied", "errno 13"),
     "ClipForge cannot write to the download folder. Choose another folder."),
    (("unable to extract", "unsupported url", "no suitable extractor", "extractor error",
      "unable to parse", "failed to parse"),
     UPDATE_HINT),
    (("urlopen error", "getaddrinfo", "timed out", "timeout", "connection", "network",
      "temporary failure", "name or service", "unable to download", "unable to connect",
      "remote end closed", "incompleteread", "ssl"),
     "Could not reach {site}. Check your internet connection and try again."),
    (("ffmpeg", "postprocess", "merg"),
     "The {noun} was downloaded but could not be merged. Please try again."),
]

# Reddit / TikTok / Instagram: anything that smells like "not public / needs an account".
_LOGIN_NEEDLES = ("login", "log in", "logged in", "logged-in", "sign in",
                  "private", "age-restricted", "age restricted", "confirm your age",
                  "rate-limit", "rate limit", "empty media response", "registered users",
                  "not granting access", "must be 18", "restricted video", "members only",
                  "members-only", "http error 401", "http error 403",
                  "only available for", "mature content", "mature audience", "for mature")
_SUBONLY_NEEDLES = ("subscriber-only", "subscriber only", "subscribers only",
                    "subscribers-only", "sub-only", "sub only", "channel subscription",
                    "requires a subscription", "subscriber")
_SUBONLY_ANY_SITE = ("subscriber-only", "subscribers-only", "subscribers only",
                     "sub-only", "channel subscription")
_EXPIRED_NEEDLES = ("expired", "vod is no longer", "vod was deleted", "vod has been deleted",
                    "vod no longer", "does not exist")
# Twitch / Kick extractor or API breakage (as opposed to a network problem)
_TK_BREAKAGE = ("gql", "unable to download json", "unable to download m3u8", "invalid json",
                "http error 5", "unexpected", "api error", "kick api")
_NETWORK_HINTS = ("urlopen", "getaddrinfo", "timed out", "timeout", "connection",
                  "name or service", "temporary failure", "remote end closed", "ssl")
_NO_VIDEO_NEEDLES = ("there is no video", "no video in this post", "has no video",
                     "no video formats", "no video found", "no media found")
# Phrases that are unambiguous even when the site is not known.
_ANY_SITE_LOGIN_NEEDLES = ("rate-limit reached", "empty media response",
                           "not granting access", "registered users")


def translate_error(exc: BaseException | str, site: Optional[str] = None) -> str:
    """Friendly, jargon-free message for a yt-dlp / network failure. Technical detail
    belongs in the log only. ``site`` (a ``SUPPORTED_SITES`` key) tunes the wording;
    without it the YouTube-era wording is kept."""
    raw = str(exc) if not isinstance(exc, str) else exc
    low = raw.lower()
    other_site = site in SUPPORTED_SITES and site != "youtube"
    tk_site = site in ("twitch", "kick")
    if any(n in low for n in _SUBONLY_ANY_SITE) or (
            tk_site and any(n in low for n in _SUBONLY_NEEDLES)):
        return SUBONLY_MESSAGE
    if tk_site and any(n in low for n in _EXPIRED_NEEDLES):
        return EXPIRED_MESSAGE
    if tk_site and any(n in low for n in _TK_BREAKAGE) and not any(
            n in low for n in _NETWORK_HINTS) and not any(
            n in low for n in ("http error 404", "http error 401", "http error 403")):
        return UPDATE_HINT
    if other_site:
        if any(n in low for n in _NO_VIDEO_NEEDLES):
            return NO_VIDEO_MESSAGE
        if any(n in low for n in _LOGIN_NEEDLES):
            return LOGIN_MESSAGE
    elif any(n in low for n in _ANY_SITE_LOGIN_NEEDLES):
        return LOGIN_MESSAGE
    name = SUPPORTED_SITES.get(site or "", "the site")
    noun = "post" if other_site and not tk_site else "video"
    for needles, message in _ERROR_RULES:
        if any(n in low for n in needles):
            text = message.replace("{site}", name).replace("{noun}", noun)
            return text[:1].upper() + text[1:]
    return ("The download failed. Check your connection and try again; if it keeps "
            "failing, update the downloader in Settings (Downloader > Update) "
            "(details are in the log file).")


# Options that would store or send credentials; the downloader never sets them.
_FORBIDDEN_OPTS = frozenset({
    "cookiefile", "cookiesfrombrowser", "username", "password", "twofactor", "netrc",
    "usenetrc", "netrc_location", "netrc_cmd", "videopassword", "proxy", "geo_verification_proxy",
    "impersonate", "client_certificate", "client_certificate_key",
    "client_certificate_password", "http_headers", "username_cmd", "ap_username",
    "ap_password", "ap_mso",
})


def _checked(opts: dict[str, Any]) -> dict[str, Any]:
    """Refuse (programming error) to build yt-dlp options that carry credentials."""
    bad = sorted(_FORBIDDEN_OPTS & set(opts))
    if bad:
        raise MediaError("Internal error: credential options are not allowed.",
                         f"forbidden yt-dlp options: {bad}")
    return opts


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


# ---------------------------------------------------- stopping yt-dlp's FFmpeg child
# A section download is done by yt-dlp's FFmpeg downloader, which blocks in
# ``Popen.wait()`` without calling any progress hook, so a hook can't interrupt it.
# yt-dlp's FFmpeg downloader creates its process through the module-level ``Popen`` name;
# a transparent subclass records the processes started by watched worker threads so a
# cancel can kill them promptly. Behaviour for every other caller is unchanged.
_proc_lock = threading.Lock()
_watched_threads: set[int] = set()
_thread_procs: dict[int, list] = {}


class _TrackedPopen(yt_dlp.utils.Popen):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ident = threading.get_ident()
        with _proc_lock:
            if ident in _watched_threads:
                _thread_procs.setdefault(ident, []).append(self)


def _install_tracking() -> bool:
    try:
        import yt_dlp.downloader.external as ext  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    if not hasattr(ext, "Popen"):
        return False
    if not issubclass(ext.Popen, _TrackedPopen):
        ext.Popen = _TrackedPopen
    return True


_TRACKING = _install_tracking()


def _watch_thread(ident: int) -> None:
    with _proc_lock:
        _watched_threads.add(ident)
        _thread_procs.pop(ident, None)


def _unwatch_thread(ident: int) -> None:
    with _proc_lock:
        _watched_threads.discard(ident)
        _thread_procs.pop(ident, None)


def _kill_thread_procs(ident: Optional[int]) -> int:
    """Kill the FFmpeg processes started by the watched thread ``ident``."""
    if ident is None:
        return 0
    with _proc_lock:
        procs = list(_thread_procs.get(ident, []))
    killed = 0
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.kill()
                killed += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not stop ffmpeg: %s", exc)
    return killed


def _bind_ffmpeg_location(ffmpeg_dir: str) -> None:
    """yt-dlp decides whether its FFmpeg downloader (needed for sections) is usable with
    ``FFmpegFD.available()``, which only knows ``ffmpeg_location`` through a context
    variable the yt-dlp CLI sets; API users must set it themselves, per thread, or the
    bundled FFmpeg is not found when it is not on PATH."""
    try:
        from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor  # noqa: PLC0415
        FFmpegPostProcessor._ffmpeg_location.set(ffmpeg_dir)
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not bind ffmpeg location for yt-dlp: %s", exc)


# ------------------------------------------------------------------ sections
_MIN_SECTION = 0.5
_DURATION_SLACK = 1.0       # an end up to this far past the reported duration is clamped


def _clamp_section(section: Sequence[float], duration: float) -> tuple[float, float]:
    """Validated ``(start, end)``; ``end`` slightly past ``duration`` is clamped to it.
    Raises ``ValueError`` (short, user-facing) when the range makes no sense."""
    try:
        start, end = float(section[0]), float(section[1])
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("The section needs a start and an end time.") from exc
    if start != start or end != end or start == float("inf") or end == float("inf"):
        raise ValueError("The section times are not valid.")
    if start < 0:
        raise ValueError("The section start can't be negative.")
    if end <= start:
        raise ValueError("The section end must be after its start.")
    if duration and duration > 0:
        if start >= duration:
            raise ValueError("The section starts after the end of the video.")
        if end > duration + _DURATION_SLACK:
            raise ValueError("The section ends after the end of the video.")
        end = min(end, duration)
    if end - start < _MIN_SECTION:
        raise ValueError("The section must be at least half a second long.")
    return start, end


def _tag_time(seconds: float) -> str:
    """``1m30s`` / ``1h02m03s`` / ``45s`` / ``1m30.5s`` - safe in Windows file names."""
    tenths = int(round(max(0.0, seconds) * 10))
    whole, frac = divmod(tenths, 10)
    h, rem = divmod(whole, 3600)
    m, sec = divmod(rem, 60)
    tail = f".{frac}" if frac else ""
    if h:
        return f"{h}h{m:02d}m{sec:02d}{tail}s"
    if m:
        return f"{m}m{sec:02d}{tail}s"
    return f"{sec}{tail}s"


def section_tag(start: float, end: float) -> str:
    """File-name suffix for a section, e.g. ``"1m30s-2m15s"``."""
    return f"{_tag_time(start)}-{_tag_time(end)}"


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


_VIDEO_EXTS = {"mp4", "webm", "mov", "m4v", "mkv", "gif", "gifv", "avi", "flv", "3gp", "ts"}
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "heic", "avif", "bmp"}


def _clean_title(title: Any, site: str, video_id: str) -> str:
    """Single-line title safe for a file name; falls back to ``"<Site> <id>"``."""
    text = re.sub(r"[\s\x00-\x1f\x7f]+", " ", str(title or "")).strip()
    if text.strip(". ") == "":
        text = f"{SUPPORTED_SITES.get(site, 'Video')} {video_id}".strip()
    return text


def _format_has_video(f: dict) -> bool:
    if f.get("vcodec") == "none" or f.get("video_ext") == "none":
        return False
    ext = str(f.get("ext") or "").lower()
    if ext in _IMAGE_EXTS:
        return False
    return f.get("vcodec") is not None or bool(f.get("height")) or ext in _VIDEO_EXTS \
        or bool(f.get("manifest_url") or f.get("fragments"))


def _has_video(d: dict) -> bool:
    """Does this yt-dlp result contain any video stream (GIF/silent videos count)?"""
    fmts = d.get("formats") or d.get("requested_formats") or []
    if fmts:
        return any(_format_has_video(f) for f in fmts if isinstance(f, dict))
    if d.get("url"):
        return _format_has_video(d)
    return False


def _pick_entry(data: dict, site: str) -> dict:
    """Single item of a multi-item result (e.g. an Instagram carousel): the first one
    that has video, else the first one."""
    entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
    if data.get("_type") != "playlist" or not entries:
        return data
    if site != "youtube":
        for e in entries:
            if _has_video(e):
                return e
    return entries[0]


def _info_from_dict(d: dict, url: str) -> VideoInfo:
    site = site_of(url) or "youtube"
    heights = sorted({int(f["height"]) for f in (d.get("formats") or [])
                      if f.get("height") and f.get("vcodec") not in (None, "none")},
                     reverse=True)
    if not heights and d.get("height"):
        heights = [int(d["height"])]
    vid = str(d.get("id") or "")
    page = str(d.get("webpage_url") or "")
    live = bool(d.get("is_live")) or d.get("live_status") in ("is_live", "is_upcoming")
    return VideoInfo(
        url=_watch_url(vid) if site == "youtube" and _ID_RE.match(vid) else url,
        video_id=vid,
        title=_clean_title(d.get("title") or (vid if site == "youtube" else ""), site, vid),
        duration=float(d.get("duration") or 0),
        uploader=str(d.get("uploader") or d.get("channel") or ""),
        thumbnail_url=str(d.get("thumbnail") or ""),
        heights=heights,
        is_short="/shorts/" in page or (
            0 < float(d.get("duration") or 0) <= 180
            and int(d.get("width") or 0) > 0
            and int(d.get("height") or 0) > int(d.get("width") or 0)),
        is_live=live,
        site=site,
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
        self._site: Optional[str] = site_of(url)

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
            site = site_of(url) or "youtube"
            self._site = site
            opts = _checked(_base_opts())
            opts.update({"skip_download": True})
            with yt_dlp.YoutubeDL(opts) as ydl:
                data = ydl.extract_info(url, download=False)
            if not isinstance(data, dict):
                raise MediaError(f"{SUPPORTED_SITES[site]} returned no information for "
                                 "that link.")
            data = _pick_entry(data, site)
            if site != "youtube" and not _has_video(data):
                raise MediaError(NO_VIDEO_MESSAGE)
            info = _info_from_dict(data, url)
            if self._cancel.is_set():
                return
            candidates = [info.thumbnail_url] if info.thumbnail_url else []
            if not candidates and isinstance(data.get("thumbnails"), list):
                candidates = [str(t.get("url")) for t in reversed(data["thumbnails"])
                              if isinstance(t, dict) and t.get("url")][:3]
            if site == "youtube" and info.video_id:
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
                self.failed.emit(translate_error(exc, self._site))


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

    # fraction 0..1, speed text, ETA text. A fraction < 0 (-1.0) means "indeterminate":
    # no real total is known (a section download before FFmpeg reports its first time=,
    # merging, preparing) and the UI should show a busy bar, not a percentage.
    progress = Signal(float, str, str)
    stage = Signal(str)                  # "Downloading video…", "Merging…", …
    finished = Signal(str, object)       # output file path, VideoInfo
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, info: VideoInfo, quality: str, dest_dir: str,
                 parent: Optional[QObject] = None, *,
                 section: Optional[tuple[float, float]] = None) -> None:
        super().__init__(parent)
        self._info = info
        # ``section=(start_s, end_s)``: download only that range (frame-accurate cuts
        # through the bundled FFmpeg). ValueError when it is not a valid range.
        self._section: Optional[tuple[float, float]] = (
            _clamp_section(section, float(info.duration or 0)) if section is not None
            else None)
        self._worker_ident: Optional[int] = None
        self._progress_file = ""
        q = str(quality).strip()
        # QUALITY_CHOICES, or a real height reported by another site (e.g. "540")
        self._quality = q if q in QUALITY_CHOICES or (q.isascii() and q.isdigit()
                                                      and 0 < int(q) <= 8640) else "best"
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
        if self._section is not None:
            _kill_thread_procs(self._worker_ident)

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
        if self._section is not None:
            start, end = self._section
            opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start, end)])
            opts["force_keyframes_at_cuts"] = True     # frame-accurate cut points
            if self._progress_file:
                # FFmpeg writes "out_time_us=..." lines here; read by _SectionMonitor
                opts["external_downloader_args"] = {
                    "ffmpeg_o": ["-progress", self._progress_file.replace("\\", "/"),
                                 "-nostats"]}
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
        if self._section is not None:
            return          # progress comes from FFmpeg's own time= via _SectionMonitor
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
        suffix = f" {section_tag(*self._section)}" if self._section is not None else ""
        final = unique_path(os.path.splitext(expected)[0] + suffix + ".mp4")
        stem = os.path.splitext(final)[0]
        ydl.params["outtmpl"] = {"default": stem.replace("%", "%%") + ".%(ext)s"}
        return final

    def _run(self) -> None:
        self._worker_ident = threading.get_ident()
        if self._section is not None:
            _watch_thread(self._worker_ident)
        try:
            self._run_guarded()
        finally:
            _unwatch_thread(self._worker_ident)
            self._drop_progress_file()

    def _drop_progress_file(self) -> None:
        path, self._progress_file = self._progress_file, ""
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    def _run_guarded(self) -> None:
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
            self.failed.emit(translate_error(exc, self._info.site))

    def _emit_cancelled(self) -> None:
        self.stage.emit("Cancelled")
        self.cancelled.emit()

    def _run_inner(self) -> None:
        if self._info.is_live or (self._section is not None
                                  and float(self._info.duration or 0) <= 0):
            raise MediaError(LIVE_MESSAGE)
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
        if self._section is not None:
            _bind_ffmpeg_location(ffmpeg_dir)
            fd, self._progress_file = tempfile.mkstemp(prefix="clipforge-dl-",
                                                       suffix=".progress")
            os.close(fd)
        opts = _checked(self._options(ffmpeg_dir))
        site = site_of(url) or self._info.site
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
            if self._cancel.is_set():
                raise _DownloadCancelled()
            if not isinstance(data, dict):
                raise MediaError(f"{SUPPORTED_SITES.get(site, 'The site')} returned no "
                                 "information for that link.")
            data = _pick_entry(data, site)
            if site != "youtube" and not _has_video(data):
                raise MediaError(NO_VIDEO_MESSAGE)
            data["title"] = _clean_title(data.get("title"), site, str(data.get("id") or ""))
            if data.get("is_live") or data.get("live_status") in ("is_live", "is_upcoming"):
                raise MediaError(LIVE_MESSAGE)
            if self._section is not None:
                self._check_section_against(data)
                ydl.params["download_ranges"] = yt_dlp.utils.download_range_func(
                    None, [self._section])
            final = self._plan(ydl, data)
            self._prepare_progress_model(ydl, data)
            self.stage.emit("Downloading section…" if self._section is not None
                            else "Downloading video…")
            monitor = _SectionMonitor(self) if self._section is not None else None
            try:
                if monitor:
                    monitor.start()
                result = ydl.process_ie_result(data, download=True)
            finally:
                if monitor:
                    monitor.stop()
            if self._cancel.is_set():
                raise _DownloadCancelled()
            path = self._result_path(result, final)
        if not path or not os.path.isfile(path):
            raise MediaError("The download finished but the file could not be found. "
                             "Please try again.", f"expected {final}")
        self._drop_progress_file()
        self.progress.emit(1.0, "", "")
        self.stage.emit("Done")
        self.finished.emit(path, self._info)

    def _check_section_against(self, data: dict) -> None:
        """Re-validate the range against the duration yt-dlp reports (may differ)."""
        real = float(data.get("duration") or self._info.duration or 0)
        if real <= 0:
            raise MediaError(LIVE_MESSAGE)
        try:
            self._section = _clamp_section(self._section, real)
        except ValueError as exc:
            raise MediaError(f"{exc} The video is {format_clock(real)} long.") from exc

    def _prepare_progress_model(self, ydl: Any, data: dict) -> None:
        if self._section is not None:
            self._streams_total = 1
            return          # byte totals are meaningless for an FFmpeg section download
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


class _SectionMonitor:
    """Turns FFmpeg's ``-progress`` file (``out_time_us=`` lines) into real progress for a
    section download, and enforces cancel by killing the FFmpeg child when the job's
    cancel flag is set. Until FFmpeg reports its first time the bar stays indeterminate."""

    def __init__(self, job: "DownloadJob") -> None:
        self._job = job
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ident = threading.get_ident()      # the worker thread that owns the child
        self._path = job._progress_file
        self._speed = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="yt-section-progress",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)

    def _read_seconds(self) -> Optional[float]:
        if not self._path:
            return None
        try:
            with open(self._path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return None
        value = None
        for line in text.splitlines():
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    value = int(line.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    pass
            elif line.startswith("speed="):
                self._speed = line.split("=", 1)[1].strip()
        return value

    def _loop(self) -> None:
        job = self._job
        start, end = job._section
        total = max(end - start, 0.001)
        t0 = time.monotonic()
        while not self._stop.wait(0.3):
            if job._cancel.is_set():
                _kill_thread_procs(self._ident)
                return
            seconds = self._read_seconds()
            if seconds is None or seconds < 0:
                continue
            fraction = max(0.0, min(0.99, seconds / total))
            elapsed = time.monotonic() - t0
            eta = ""
            if 0.02 < fraction < 0.99 and elapsed > 1:
                eta = _fmt_eta(elapsed * (1 - fraction) / fraction)
            speed = self._speed if self._speed not in ("", "N/A") else ""
            job.progress.emit(fraction, speed, eta)


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
