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
