"""Downloader unit tests - no network: yt_dlp.YoutubeDL is replaced by a fake."""

from __future__ import annotations

import os
import threading

import pytest
import yt_dlp

from clipforge.media import MediaError, downloader
from clipforge.media.downloader import (QUALITY_CHOICES, DownloadJob, MetadataJob, VideoInfo,
                                        build_format_selector, translate_error, unique_path,
                                        validate_url)

VID = "dQw4w9WgXcQ"
CANON = f"https://www.youtube.com/watch?v={VID}"


# ------------------------------------------------------------------ validate_url
@pytest.mark.parametrize("url", [
    CANON,
    f"http://youtube.com/watch?v={VID}",
    f"https://www.youtube.com/watch?v={VID}&si=abc&t=42s&list=PL123&index=3",
    f"https://m.youtube.com/watch?v={VID}",
    f"https://music.youtube.com/watch?v={VID}&si=zzz",
    f"https://youtu.be/{VID}",
    f"https://youtu.be/{VID}?si=xyz&t=10",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/shorts/{VID}?feature=share",
    f"https://www.youtube.com/embed/{VID}",
    f"https://www.youtube-nocookie.com/embed/{VID}",
    f"  www.youtube.com/watch?v={VID}  ",
    f"youtu.be/{VID}",
])
def test_validate_url_accepts(url):
    assert validate_url(url) == CANON


@pytest.mark.parametrize("url", [
    "", "   ", "hello world", "not a url", "https://", "ftp://youtube.com/watch?v=" + VID,
    "https://vimeo.com/12345", "https://example.com/watch?v=" + VID,
    "https://notyoutube.com/watch?v=" + VID,
    "https://www.youtube.com/playlist?list=PL12345",
    "https://www.youtube.com/watch?list=PL12345",
    "https://www.youtube.com/watch?v=short",
    "https://www.youtube.com/watch?v=" + VID + "x",
    "https://www.youtube.com/",
    "https://www.youtube.com/@somechannel",
    "https://youtu.be/",
    "https://youtu.be/bad id!",
])
def test_validate_url_rejects(url):
    with pytest.raises(MediaError) as ei:
        validate_url(url)
    assert str(ei.value).strip()


def test_validate_url_messages_are_helpful():
    with pytest.raises(MediaError, match="YouTube"):
        validate_url("https://vimeo.com/1")
    with pytest.raises(MediaError, match="playlist"):
        validate_url("https://www.youtube.com/playlist?list=PL1")


# -------------------------------------------------------------- format selector
def test_format_selector_prefers_avc1_m4a_up_to_1080():
    sel = build_format_selector("720")
    first = sel.split("/")[0]
    assert "height<=720" in first and "vcodec^=avc1" in first and "ext=m4a" in first
    assert sel.endswith("/b")                         # always has a final fallback
    assert "height<=1080" in build_format_selector("1080")


@pytest.mark.parametrize("q", ["1440", "2160"])
def test_format_selector_high_res_beats_codec(q):
    sel = build_format_selector(q)
    assert sel.startswith(f"bv*[height<={q}]+ba")
    assert "avc1" not in sel.split("/")[0]


def test_format_selector_best_and_unknown():
    assert build_format_selector("best") == "bv*+ba/b"
    assert build_format_selector("garbage") == "bv*+ba/b"
    for q in QUALITY_CHOICES:
        assert build_format_selector(q)


# --------------------------------------------------------------- VideoInfo
def _info(heights, **kw):
    return VideoInfo(url=CANON, video_id=VID, title="T", duration=10, heights=heights, **kw)


def test_available_qualities():
    assert _info([2160, 1440, 1080, 720, 360]).available_qualities() == QUALITY_CHOICES
    assert _info([1080, 720, 480, 360]).available_qualities() == ["best", "1080", "720", "480"]
    assert _info([360, 144]).available_qualities() == ["best"]
    assert _info([]).available_qualities() == QUALITY_CHOICES
    assert _info([1080]).available_qualities()[0] == "best"


# --------------------------------------------------------------- errors
@pytest.mark.parametrize("raw, word", [
    ("ERROR: [youtube] abc: Private video. Sign in if you have been granted access", "private"),
    ("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate", "age"),
    ("ERROR: [youtube] abc: Sign in to confirm you're not a bot", "bot"),
    ("ERROR: [youtube] abc: Video unavailable", "unavailable"),
    ("ERROR: [youtube] abc: The uploader has not made this video available in your country",
     "country"),
    ("ERROR: [youtube] abc: This live event will begin in 2 hours.", "live"),
    ("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>",
     "internet"),
    ("ERROR: Requested format is not available", "quality"),
    ("HTTP Error 429: Too Many Requests", "wait"),
    ("[Errno 28] No space left on device", "disk space"),
])
def test_translate_error(raw, word):
    msg = translate_error(Exception(raw))
    assert word in msg.lower()
    assert "ERROR:" not in msg and "Traceback" not in msg and "[youtube]" not in msg


def test_translate_error_fallback_hides_details():
    msg = translate_error(RuntimeError("weird internal xyzzy"))
    assert "xyzzy" not in msg and msg


# --------------------------------------------------------------- unique path
def test_unique_path(tmp_path):
    p = str(tmp_path / "Video [abc].mp4")
    assert unique_path(p) == p
    open(p, "w").close()
    p2 = str(tmp_path / "Video [abc] (2).mp4")
    assert unique_path(p) == p2
    open(p2, "w").close()
    assert unique_path(p) == str(tmp_path / "Video [abc] (3).mp4")


# --------------------------------------------------------------- fake yt-dlp
class FakeYDL:
    """Stand-in for yt_dlp.YoutubeDL driven by class-level behaviour switches."""

    instances: list = []
    info: dict = {}
    extract_error = None
    download_error = None
    wait_for_cancel = None
    started = None
    steps = 4

    def __init__(self, opts):
        self.opts = opts
        self.params = dict(opts)
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if FakeYDL.extract_error:
            raise FakeYDL.extract_error
        return dict(FakeYDL.info)

    def prepare_filename(self, info):
        tmpl = self.params["outtmpl"]["default"].replace("%%", "%")
        return tmpl.replace("%(title).120B", info["title"]).replace(
            "%(id)s", info["id"]).replace("%(ext)s", info.get("ext", "mp4"))

    def process_ie_result(self, info, download=True):
        hooks = self.opts["progress_hooks"]
        name = self.prepare_filename({**info, "ext": "mp4"})
        for i in range(FakeYDL.steps):
            if FakeYDL.started:
                FakeYDL.started.set()
            if FakeYDL.wait_for_cancel:
                FakeYDL.wait_for_cancel.wait(2)
            for h in hooks:
                h({"status": "downloading", "filename": name + ".f137.part",
                   "downloaded_bytes": (i + 1) * 100, "total_bytes": FakeYDL.steps * 100,
                   "speed": 2.5 * 1024 * 1024, "eta": 65})
        if FakeYDL.download_error:
            raise FakeYDL.download_error
        for h in hooks:
            h({"status": "finished", "filename": name + ".f137.part",
               "downloaded_bytes": 400, "total_bytes": 400})
        for h in self.opts["postprocessor_hooks"]:
            h({"status": "started", "postprocessor": "Merger"})
        with open(name, "wb") as f:
            f.write(b"x")
        return {**info, "requested_downloads": [{"filepath": name}]}


@pytest.fixture
def fake(monkeypatch):
    FakeYDL.instances = []
    FakeYDL.extract_error = None
    FakeYDL.download_error = None
    FakeYDL.wait_for_cancel = None
    FakeYDL.started = None
    FakeYDL.steps = 4
    FakeYDL.info = {
        "id": VID, "title": "My Title", "duration": 12.5, "uploader": "Chan",
        "thumbnail": "http://insecure/never-fetched.jpg", "webpage_url": CANON,
        "formats": [{"height": 1080, "vcodec": "avc1"}, {"height": 720, "vcodec": "vp9"},
                    {"height": 1080, "vcodec": "vp9"}, {"height": None, "vcodec": "none"},
                    {"height": 360, "vcodec": "avc1"}],
    }
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(downloader, "_fetch_thumbnail", lambda urls: b"JPEGDATA")
    return FakeYDL


# --------------------------------------------------------------- MetadataJob
def test_metadata_job_success(qtbot, fake):
    job = MetadataJob(f"https://youtu.be/{VID}?si=1")
    with qtbot.waitSignal(job.finished, timeout=5000) as sig:
        job.start()
    info = sig.args[0]
    assert info.url == CANON and info.video_id == VID and info.title == "My Title"
    assert info.duration == 12.5 and info.uploader == "Chan"
    assert info.heights == [1080, 720, 360]
    assert info.thumbnail_jpeg == b"JPEGDATA"
    assert info.available_qualities() == ["best", "1080", "720", "480"]
    opts = fake.instances[0].opts
    assert opts["noplaylist"] is True
    for forbidden in ("cookiefile", "cookiesfrombrowser", "username", "password"):
        assert forbidden not in opts


def test_metadata_job_invalid_url_fails_without_ytdlp(qtbot, fake):
    job = MetadataJob("https://vimeo.com/1")
    with qtbot.waitSignal(job.failed, timeout=5000) as sig:
        job.start()
    assert "YouTube" in sig.args[0]
    assert not fake.instances


def test_metadata_job_error_is_translated(qtbot, fake):
    fake.extract_error = yt_dlp.utils.DownloadError("ERROR: [youtube] x: Private video")
    job = MetadataJob(CANON)
    with qtbot.waitSignal(job.failed, timeout=5000) as sig:
        job.start()
    assert "private" in sig.args[0].lower() and "[youtube]" not in sig.args[0]


def test_metadata_job_cancel(qtbot, fake):
    job = MetadataJob(CANON)
    with qtbot.waitSignal(job.cancelled, timeout=5000):
        job.cancel()
    job.start()
    qtbot.wait(200)    # no second terminal signal expected / no crash


# --------------------------------------------------------------- DownloadJob
def _dl_info(**kw):
    return VideoInfo(url=CANON, video_id=VID, title="My Title", duration=12.5,
                     heights=[1080, 720], **kw)


def test_download_success_and_options(qtbot, fake, tmp_path):
    job = DownloadJob(_dl_info(), "720", str(tmp_path))
    progress, stages = [], []
    job.progress.connect(lambda f, s, e: progress.append((f, s, e)))
    job.stage.connect(stages.append)
    with qtbot.waitSignal(job.finished, timeout=10000) as sig:
        job.start()
    path, info = sig.args
    assert path == str(tmp_path / f"My Title [{VID}].mp4") and os.path.isfile(path)
    assert info.video_id == VID
    opts = fake.instances[0].opts
    assert opts["merge_output_format"] == "mp4" and opts["windowsfilenames"] is True
    assert opts["noplaylist"] is True
    assert opts["format"] == build_format_selector("720")
    assert os.path.isfile(os.path.join(opts["ffmpeg_location"], "ffmpeg.exe"))
    assert opts["outtmpl"]["default"].endswith("%(title).120B [%(id)s].%(ext)s")
    for forbidden in ("cookiefile", "cookiesfrombrowser", "username", "password", "netrc"):
        assert forbidden not in opts
    fractions = [p[0] for p in progress]
    assert any(0 < f < 1 for f in fractions) and fractions[-1] == 1.0
    mid = next(p for p in progress if 0 < p[0] < 1)
    assert "MB/s" in mid[1] and mid[2] == "1:05"
    assert "Merging…" in stages and stages[0] == "Preparing…"


def test_download_never_overwrites(qtbot, fake, tmp_path):
    (tmp_path / f"My Title [{VID}].mp4").write_bytes(b"old")
    job = DownloadJob(_dl_info(), "best", str(tmp_path))
    with qtbot.waitSignal(job.finished, timeout=10000) as sig:
        job.start()
    assert sig.args[0] == str(tmp_path / f"My Title [{VID}] (2).mp4")
    assert (tmp_path / f"My Title [{VID}].mp4").read_bytes() == b"old"


def test_download_error_translated_and_retry_possible(qtbot, fake, tmp_path):
    fake.download_error = yt_dlp.utils.DownloadError(
        "ERROR: Unable to download webpage: <urlopen error getaddrinfo failed>")
    job = DownloadJob(_dl_info(), "1080", str(tmp_path))
    with qtbot.waitSignal(job.failed, timeout=10000) as sig:
        job.start()
    assert "internet" in sig.args[0].lower() and "getaddrinfo" not in sig.args[0]
    job._thread.join(2)
    fake.download_error = None                     # network is back: retry on same job
    with qtbot.waitSignal(job.finished, timeout=10000):
        job.start()


def test_download_extract_error(qtbot, fake, tmp_path):
    fake.extract_error = yt_dlp.utils.DownloadError("ERROR: [youtube] x: Video unavailable")
    job = DownloadJob(_dl_info(), "best", str(tmp_path))
    with qtbot.waitSignal(job.failed, timeout=10000) as sig:
        job.start()
    assert "unavailable" in sig.args[0].lower()


def test_download_live_refused(qtbot, fake, tmp_path):
    job = DownloadJob(_dl_info(is_live=True), "best", str(tmp_path))
    with qtbot.waitSignal(job.failed, timeout=5000) as sig:
        job.start()
    assert "live" in sig.args[0].lower()
    assert not fake.instances


def test_download_cancel_via_hook(qtbot, fake, tmp_path):
    fake.started = threading.Event()
    fake.wait_for_cancel = threading.Event()
    job = DownloadJob(_dl_info(), "best", str(tmp_path))
    failures = []
    job.failed.connect(failures.append)
    with qtbot.waitSignal(job.cancelled, timeout=10000):
        job.start()
        assert fake.started.wait(5)
        assert job.is_running()
        job.cancel()
        fake.wait_for_cancel.set()
    qtbot.wait(100)
    assert not failures
    assert not list(tmp_path.glob("*.mp4"))


def test_download_unwritable_dest_fails_friendly(qtbot, fake, tmp_path):
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    job = DownloadJob(_dl_info(), "best", str(blocker / "sub"))
    with qtbot.waitSignal(job.failed, timeout=5000) as sig:
        job.start()
    assert "folder" in sig.args[0].lower()


# =================================================================== multi-site support
from clipforge.media.downloader import SUPPORTED_SITES, site_of  # noqa: E402

LOGIN_TEXT = "needs a login or isn't public"


def test_supported_sites_contract():
    assert SUPPORTED_SITES == {"youtube": "YouTube", "reddit": "Reddit", "tiktok": "TikTok",
                               "instagram": "Instagram"}


@pytest.mark.parametrize("url, expected", [
    # Reddit
    ("https://www.reddit.com/r/videos/comments/abc123/some_title/",
     "https://www.reddit.com/r/videos/comments/abc123/"),
    ("https://reddit.com/r/videos/comments/abc123/?utm_source=share&utm_medium=web2x",
     "https://www.reddit.com/r/videos/comments/abc123/"),
    ("https://old.reddit.com/r/Some_Sub/comments/1a2b3c4/title/commentid/?context=3",
     "https://www.reddit.com/r/Some_Sub/comments/1a2b3c4/"),
    ("https://new.reddit.com/r/videos/comments/abc123",
     "https://www.reddit.com/r/videos/comments/abc123/"),
    ("http://m.reddit.com/r/videos/comments/abc123/x/",
     "https://www.reddit.com/r/videos/comments/abc123/"),
    ("https://redd.it/abc123?share=1", "https://redd.it/abc123"),
    ("https://v.redd.it/a1b2c3d4e5", "https://v.redd.it/a1b2c3d4e5"),
    # TikTok
    ("https://www.tiktok.com/@some.user_1/video/7234567890123456789?is_from_webapp=1&sender_device=pc",
     "https://www.tiktok.com/@some.user_1/video/7234567890123456789"),
    ("https://tiktok.com/@u/video/123456789", "https://www.tiktok.com/@u/video/123456789"),
    ("https://m.tiktok.com/@u/video/123456789/", "https://www.tiktok.com/@u/video/123456789"),
    ("https://www.tiktok.com/t/ZT8abcdef/?x=1", "https://www.tiktok.com/t/ZT8abcdef/"),
    ("https://vm.tiktok.com/ZMabc123/", "https://vm.tiktok.com/ZMabc123/"),
    ("https://vt.tiktok.com/ZSabc123?utm=1", "https://vt.tiktok.com/ZSabc123/"),
    # Instagram
    ("https://www.instagram.com/reel/C1abcDEFghi/?igsh=MzRlODBiNWFlZA==",
     "https://www.instagram.com/reel/C1abcDEFghi/"),
    ("https://instagram.com/reels/C1abcDEFghi", "https://www.instagram.com/reel/C1abcDEFghi/"),
    ("https://www.instagram.com/p/B_abc-DEF12/?utm_source=ig_web_copy_link",
     "https://www.instagram.com/p/B_abc-DEF12/"),
    ("https://www.instagram.com/tv/B_abc-DEF12/", "https://www.instagram.com/tv/B_abc-DEF12/"),
    ("https://www.instagram.com/some.user/reel/C1abcDEFghi/?igsh=x",
     "https://www.instagram.com/reel/C1abcDEFghi/"),
    ("https://instagr.am/p/B_abc-DEF12", "https://www.instagram.com/p/B_abc-DEF12/"),
    ("  reddit.com/r/videos/comments/abc123/x  ",
     "https://www.reddit.com/r/videos/comments/abc123/"),
])
def test_validate_url_other_sites_accepts_and_normalises(url, expected):
    assert validate_url(url) == expected
    assert validate_url(expected) == expected          # idempotent


@pytest.mark.parametrize("url", [
    # look-alike / hostile hosts
    "https://notreddit.com/r/videos/comments/abc123/",
    "https://reddit.com.evil.com/r/videos/comments/abc123/",
    "https://evil.com/?u=reddit.com/r/videos/comments/abc123/",
    "https://evil.com/r/videos/comments/abc123/#reddit.com",
    "https://reddit.com.:443@evil.com/r/videos/comments/abc123/",
    "https://www.reddit.com@evil.com/r/videos/comments/abc123/",
    "https://user:pass@www.reddit.com/r/videos/comments/abc123/",
    "https://user@www.tiktok.com/@u/video/123456789",
    "https://faketiktok.com/@u/video/123456789",
    "https://tiktok.com.evil.com/@u/video/123456789",
    "https://www.tiktok.com.evil.com/@u/video/123456789",
    "https://evil.com/instagram.com/reel/C1abcDEFghi/",
    "https://notinstagram.com/reel/C1abcDEFghi/",
    "https://instagram.com.evil.com/reel/C1abcDEFghi/",
    "https://xn--instagram-9la.com/reel/C1abcDEFghi/",
    "https://reddit.com./r/videos/comments/abc123/",
    "https://www.reddit.com:8443/r/videos/comments/abc123/",
    "https://evil.com\\@www.reddit.com/r/videos/comments/abc123/",
    "javascript:alert(1)", "file:///C:/x.mp4", "ftp://www.reddit.com/r/v/comments/abc123/",
    "https://www.reddit.com/r/videos/comments/abc123/\nhttps://evil.com",
    "https://www.reddit.com/r/videos/comments/abc123/\x00",
    "https://www.reddit.com/r/videos/comments/abc 123/",
    # right host, not a single video / post
    "https://www.reddit.com/", "https://www.reddit.com/r/videos/",
    "https://www.reddit.com/r/videos/comments/", "https://www.reddit.com/user/someone/",
    "https://www.reddit.com/r/videos/s/AbCdEf",
    "https://redd.it/", "https://v.redd.it/",
    "https://www.tiktok.com/", "https://www.tiktok.com/@someone",
    "https://www.tiktok.com/@u/video/notdigits", "https://www.tiktok.com/@u/photo/123456",
    "https://www.tiktok.com/tag/dance", "https://vm.tiktok.com/",
    "https://www.instagram.com/", "https://www.instagram.com/someone/",
    "https://www.instagram.com/stories/someone/123456/",
    "https://www.instagram.com/explore/tags/cats/",
    "https://www.instagram.com/reel/", "https://www.instagram.com/p/ab/",
    "https://www.instagram.com/share/reel/C1abcDEFghi/",
    "https://www.instagram.com/reels/audio/123456789/",
])
def test_validate_url_other_sites_rejects_hostile_and_non_posts(url):
    with pytest.raises(MediaError) as ei:
        validate_url(url)
    assert str(ei.value).strip()


def test_rejection_text_lists_supported_sites():
    for url in ("https://vimeo.com/1", "", "hello world", "https://www.reddit.com/",
                "https://www.tiktok.com/", "https://www.instagram.com/"):
        with pytest.raises(MediaError) as ei:
            validate_url(url)
        text = str(ei.value)
        assert "Paste a video link from YouTube, Reddit, TikTok or Instagram" in text or (
            "Reddit" in text or "TikTok" in text or "Instagram" in text)
    with pytest.raises(MediaError, match="Paste a video link from YouTube, Reddit, TikTok "
                                         "or Instagram"):
        validate_url("https://vimeo.com/1")


@pytest.mark.parametrize("url, site", [
    (CANON, "youtube"), (f"https://youtu.be/{VID}", "youtube"),
    ("https://www.reddit.com/r/a/comments/abc123/", "reddit"),
    ("https://old.reddit.com/anything", "reddit"), ("https://redd.it/abc123", "reddit"),
    ("https://v.redd.it/abc123", "reddit"),
    ("https://vm.tiktok.com/abc", "tiktok"), ("tiktok.com/@u/video/1", "tiktok"),
    ("https://www.instagram.com/reel/abc12345/", "instagram"),
    ("https://instagr.am/p/abcde", "instagram"),
    ("https://notreddit.com/", None), ("https://reddit.com.evil.com/", None),
    ("https://evil.com/?u=reddit.com", None), ("https://vimeo.com/1", None),
    ("https://user:pw@reddit.com/", None), ("ftp://reddit.com/", None),
    ("", None), ("   ", None), ("not a url", None), ("https://", None),
    ("https://www.tiktokv.com/x", None),
])
def test_site_of(url, site):
    assert site_of(url) == site


# ---------------------------------------------------------- qualities for other sites
def _site_info(site, heights):
    return VideoInfo(url="https://x/", video_id="i", title="T", duration=5, heights=heights,
                     site=site)


def test_videoinfo_site_defaults_to_youtube():
    assert VideoInfo(url=CANON, video_id=VID, title="T", duration=1).site == "youtube"


def test_available_qualities_other_sites():
    assert _site_info("reddit", [720, 480, 360]).available_qualities() == [
        "best", "720", "480", "360"]
    assert _site_info("tiktok", [1024]).available_qualities() == ["best"]
    assert _site_info("instagram", []).available_qualities() == ["best"]
    assert _site_info("instagram", [0]).available_qualities() == ["best"]


# ----------------------------------------------- format selector against real yt-dlp
def _select(selector, formats):
    ydl = yt_dlp.YoutubeDL({"quiet": True, "logger": downloader._YtdlpLogger()})
    ctx = {"formats": formats, "incomplete_formats": all(
        f.get("acodec") == "none" for f in formats) or all(
        f.get("vcodec") == "none" for f in formats)}
    chosen = list(ydl.build_format_selector(selector)(ctx))
    return [f["format_id"] for f in (chosen[0].get("requested_formats") or [chosen[0]])] \
        if chosen else []


def _f(fid, h=0, v="avc1", a="none", ext="mp4"):
    return {"format_id": fid, "height": h or None, "vcodec": v, "acodec": a, "ext": ext,
            "protocol": "https", "url": "https://x/" + fid, "tbr": h or 1}


@pytest.mark.parametrize("quality", QUALITY_CHOICES)
def test_selector_separate_video_and_audio(quality):
    fmts = [_f("a1", v="none", a="mp4a", ext="m4a"), _f("v360", 360), _f("v720", 720)]
    expected = "v360" if quality == "480" else "v720"
    assert _select(build_format_selector(quality), fmts) == [expected, "a1"]


@pytest.mark.parametrize("quality", QUALITY_CHOICES)
def test_selector_single_combined_stream(quality):
    fmts = [_f("only", 480, a="aac")]
    assert _select(build_format_selector(quality), fmts) == ["only"]


@pytest.mark.parametrize("quality", ["best", "720"])
def test_selector_silent_video(quality):
    fmts = [_f("silent", 720)]
    assert _select(build_format_selector(quality), fmts) == ["silent"]


def test_selector_accepts_odd_real_heights():
    sel = build_format_selector("540")
    assert "height<=540" in sel and sel.endswith("/b")
    fmts = [_f("a", v="none", a="aac", ext="m4a"), _f("v480", 480), _f("v720", 720)]
    assert _select(sel, fmts) == ["v480", "a"]


# ------------------------------------------------------------------ error translation
@pytest.mark.parametrize("raw", [
    "ERROR: [instagram] C1abc: Instagram sent an empty media response. Check if this post is "
    "accessible in your browser without being logged-in.",
    "ERROR: [instagram] C1abc: Main webpage is locked behind the login page. Use --cookies",
    "ERROR: [instagram] x: rate-limit reached or login required",
    "ERROR: [Instagram] x: This content is only available for registered users who follow "
    "this account",
    "ERROR: [Reddit] x: HTTP Error 403: Blocked",
    "ERROR: [reddit] x: Private subreddit",
    "ERROR: [tiktok] x: This video is private",
    "ERROR: [instagram] x: Restricted Video: You must be 18 years old to view this video",
    "ERROR: [tiktok] x: Log in for access to age-restricted content",
])
@pytest.mark.parametrize("site", ["instagram", "reddit", "tiktok"])
def test_translate_login_private_public_only(raw, site):
    msg = translate_error(Exception(raw), site)
    assert LOGIN_TEXT in msg and "passwords or cookies" in msg
    assert "ERROR:" not in msg and "[" not in msg


def test_translate_login_phrases_without_site():
    assert LOGIN_TEXT in translate_error("Instagram sent an empty media response")
    assert LOGIN_TEXT in translate_error("rate-limit reached or login required")


@pytest.mark.parametrize("raw", [
    "ERROR: [tiktok] 123: Unable to extract universal data for rehydration",
    "ERROR: Unsupported URL: https://www.tiktok.com/@u/photo/1",
    "ERROR: [Reddit] x: Unable to extract video url; please report this issue",
    "ERROR: [instagram] x: Unable to extract shared data; please report",
])
def test_translate_extractor_failure_suggests_update(raw):
    for site in (None, "tiktok", "reddit", "instagram", "youtube"):
        msg = translate_error(raw, site)
        assert "Update the downloader in Settings (Downloader > Update)" in msg
        assert "ERROR" not in msg


@pytest.mark.parametrize("raw", [
    "ERROR: [Reddit] x: HTTP Error 404: Not Found",
    "ERROR: [tiktok] x: Video not available",
    "ERROR: [Reddit] x: This post was removed by the moderators",
    "ERROR: [instagram] x: Video unavailable",
])
def test_translate_removed_is_unavailable(raw):
    msg = translate_error(raw, "reddit")
    assert "unavailable" in msg.lower() and "post" in msg.lower()


def test_translate_geo_and_rate_limit_and_network_other_sites():
    assert "country" in translate_error("Video is not available in your country", "tiktok")
    msg = translate_error("HTTP Error 429: Too Many Requests", "reddit")
    assert "Reddit" in msg and "wait" in msg
    msg = translate_error("Unable to download webpage: <urlopen error getaddrinfo failed>",
                          "tiktok")
    assert "TikTok" in msg and "internet" in msg


def test_translate_no_video_post():
    for raw in ("ERROR: [instagram] x: There is no video in this post",
                "ERROR: No video formats found!"):
        assert translate_error(raw, "instagram") == "This post has no video."
    # the YouTube wording of "no video formats" is unchanged
    assert "quality" in translate_error("No video formats found", None).lower()


def test_translate_youtube_wording_unchanged_by_site_argument():
    assert "private" in translate_error("Private video", "youtube").lower()
    assert "bot" in translate_error("Sign in to confirm you're not a bot", "youtube")


# ------------------------------------------------------ no credentials / no cookies
FORBIDDEN = ("cookiefile", "cookiesfrombrowser", "username", "password", "proxy", "netrc",
             "usenetrc", "videopassword", "twofactor", "impersonate", "http_headers",
             "client_certificate")


def test_base_opts_has_no_credentials():
    opts = downloader._base_opts()
    for key in FORBIDDEN:
        assert key not in opts
    assert opts["noplaylist"] is True


def test_download_options_have_no_credentials(tmp_path):
    job = DownloadJob(_dl_info(), "best", str(tmp_path))
    opts = job._options(str(tmp_path))
    for key in FORBIDDEN:
        assert key not in opts
    assert opts["windowsfilenames"] is True


def test_checked_refuses_credential_options():
    for key in FORBIDDEN:
        with pytest.raises(MediaError):
            downloader._checked({key: "x"})
    assert downloader._checked({"quiet": True}) == {"quiet": True}


def test_no_credential_option_names_in_module_source():
    import inspect
    src = inspect.getsource(downloader)
    body = src.split("_FORBIDDEN_OPTS = frozenset(")[0] + src.split("def _checked")[1]
    for key in ("cookiefile", "cookiesfrombrowser", "usenetrc", "videopassword"):
        assert f'"{key}"' not in body and f"'{key}'" not in body


# ---------------------------------------------------- end-to-end with the fake yt-dlp
REDDIT_URL = "https://www.reddit.com/r/videos/comments/abc123/"
IG_URL = "https://www.instagram.com/reel/C1abcDEFghi/"
TT_URL = "https://www.tiktok.com/@u/video/7234567890123456789"


def _site_dict(vid, title="A title", **kw):
    d = {"id": vid, "title": title, "duration": 20, "uploader": "someone", "width": 720,
         "height": 1280, "thumbnail": "https://img/x.jpg",
         "formats": [{"height": 720, "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
                     {"height": 480, "vcodec": "avc1", "acodec": "none", "ext": "mp4"},
                     {"height": None, "vcodec": "none", "acodec": "mp4a", "ext": "m4a"}]}
    d.update(kw)
    return d


def test_metadata_reddit_site_and_qualities(qtbot, fake):
    fake.info = _site_dict("abc123")
    job = MetadataJob(REDDIT_URL + "?utm_source=share")
    with qtbot.waitSignal(job.finished, timeout=5000) as sig:
        job.start()
    info = sig.args[0]
    assert info.site == "reddit" and info.url == REDDIT_URL and info.video_id == "abc123"
    assert info.available_qualities() == ["best", "720", "480"]
    assert info.is_short is True                       # vertical, <= 180 s
    assert fake.instances[0].opts["noplaylist"] is True


def test_metadata_instagram_single_height_and_empty_title(qtbot, fake):
    fake.info = _site_dict("C1abcDEFghi", title="", formats=[
        {"height": 1280, "vcodec": "avc1", "acodec": "aac", "ext": "mp4"}])
    job = MetadataJob(IG_URL)
    with qtbot.waitSignal(job.finished, timeout=5000) as sig:
        job.start()
    info = sig.args[0]
    assert info.site == "instagram" and info.available_qualities() == ["best"]
    assert info.title == "Instagram C1abcDEFghi"


def test_metadata_tiktok_silent_video_ok(qtbot, fake):
    fake.info = _site_dict("7234567890123456789", formats=[
        {"height": 1024, "vcodec": "h264", "acodec": "none", "ext": "mp4"}])
    job = MetadataJob(TT_URL)
    with qtbot.waitSignal(job.finished, timeout=5000) as sig:
        job.start()
    assert sig.args[0].site == "tiktok"


@pytest.mark.parametrize("formats", [
    [],
    [{"vcodec": "none", "acodec": "mp4a", "ext": "m4a"}],
    [{"ext": "jpg", "url": "https://img/1.jpg"}],
])
def test_metadata_image_only_post_says_no_video(qtbot, fake, formats):
    fake.info = _site_dict("abc123", formats=formats)
    job = MetadataJob(REDDIT_URL)
    with qtbot.waitSignal(job.failed, timeout=5000) as sig:
        job.start()
    assert sig.args[0] == "This post has no video."


def test_metadata_carousel_picks_first_video_entry(qtbot, fake):
    img = _site_dict("img1", formats=[{"ext": "jpg", "url": "https://img/1.jpg"}])
    vid = _site_dict("vid2", title="Second")
    fake.info = {"_type": "playlist", "id": "post", "entries": [img, vid]}
    job = MetadataJob(IG_URL)
    with qtbot.waitSignal(job.finished, timeout=5000) as sig:
        job.start()
    assert sig.args[0].video_id == "vid2"


def test_metadata_site_specific_error_translation(qtbot, fake):
    fake.extract_error = yt_dlp.utils.DownloadError(
        "ERROR: [instagram] x: Instagram sent an empty media response.")
    job = MetadataJob(IG_URL)
    with qtbot.waitSignal(job.failed, timeout=5000) as sig:
        job.start()
    assert LOGIN_TEXT in sig.args[0]


def test_metadata_wrong_site_rejected_without_ytdlp(qtbot, fake):
    job = MetadataJob("https://reddit.com.evil.com/r/a/comments/abc123/")
    with qtbot.waitSignal(job.failed, timeout=5000) as sig:
        job.start()
    assert "Paste a video link from YouTube, Reddit, TikTok or Instagram" in sig.args[0]
    assert not fake.instances


def _dl_site_info(site, url, vid, **kw):
    return VideoInfo(url=url, video_id=vid, title="x", duration=20, heights=[720, 480],
                     site=site, **kw)


def test_download_reddit_uses_title_and_has_no_credentials(qtbot, fake, tmp_path):
    fake.info = _site_dict("abc123", title="Funny cat \U0001F602 part 2\nline two")
    job = DownloadJob(_dl_site_info("reddit", REDDIT_URL, "abc123"), "540", str(tmp_path))
    with qtbot.waitSignal(job.finished, timeout=10000) as sig:
        job.start()
    path = sig.args[0]
    assert os.path.isfile(path)
    assert "\n" not in os.path.basename(path)
    opts = fake.instances[0].opts
    assert opts["format"] == build_format_selector("540")
    for key in FORBIDDEN:
        assert key not in opts


@pytest.mark.parametrize("title", ["", "   ", None, "...", "\n\t"])
def test_download_empty_title_falls_back_to_site_and_id(qtbot, fake, tmp_path, title):
    fake.info = _site_dict("C1abcDEFghi", title=title)
    job = DownloadJob(_dl_site_info("instagram", IG_URL, "C1abcDEFghi"), "best",
                      str(tmp_path))
    with qtbot.waitSignal(job.finished, timeout=10000) as sig:
        job.start()
    assert os.path.basename(sig.args[0]) == "Instagram C1abcDEFghi [C1abcDEFghi].mp4"


def test_download_image_post_fails_with_no_video(qtbot, fake, tmp_path):
    fake.info = _site_dict("abc123", formats=[{"ext": "png", "url": "https://img/1.png"}])
    job = DownloadJob(_dl_site_info("reddit", REDDIT_URL, "abc123"), "best", str(tmp_path))
    with qtbot.waitSignal(job.failed, timeout=10000) as sig:
        job.start()
    assert sig.args[0] == "This post has no video."
    assert not list(tmp_path.glob("*"))


def test_download_error_uses_site_wording(qtbot, fake, tmp_path):
    fake.download_error = yt_dlp.utils.DownloadError("ERROR: [tiktok] x: HTTP Error 403")
    fake.info = _site_dict("7234567890123456789")
    job = DownloadJob(_dl_site_info("tiktok", TT_URL, "7234567890123456789"), "best",
                      str(tmp_path))
    with qtbot.waitSignal(job.failed, timeout=10000) as sig:
        job.start()
    assert LOGIN_TEXT in sig.args[0]


def test_download_quality_accepts_real_heights(tmp_path):
    assert DownloadJob(_dl_info(), "540", str(tmp_path))._quality == "540"
    assert DownloadJob(_dl_info(), "junk", str(tmp_path))._quality == "best"
    assert DownloadJob(_dl_info(), "0", str(tmp_path))._quality == "best"


def test_clean_title_helper():
    assert downloader._clean_title("a\n b\t\tc", "tiktok", "1") == "a b c"
    assert downloader._clean_title("", "tiktok", "99") == "TikTok 99"
    assert downloader._clean_title(None, "reddit", "abc") == "Reddit abc"
    assert downloader._clean_title("\U0001F602\U0001F602", "tiktok", "1") == "\U0001F602\U0001F602"


def test_activate_override_runs_before_yt_dlp_import():
    import inspect
    src = inspect.getsource(downloader)
    assert src.index("ytdlp_update.activate_override()") < src.index("import yt_dlp  #")
