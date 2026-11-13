"""Section downloads, Twitch/Kick URLs, timecode parsing, new error wording.

No network: ``yt_dlp.YoutubeDL`` is replaced by the fake from test_downloader_unit."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest
import yt_dlp

from clipforge.core.timecode import format_clock, format_timecode, parse_timecode
from clipforge.media import MediaError, downloader
from clipforge.media.downloader import (LIVE_MESSAGE, SUPPORTED_SITES, DownloadJob, VideoInfo,
                                        section_tag, site_of, translate_error, validate_url)
from test_downloader_unit import FORBIDDEN, FakeYDL, fake  # noqa: F401  (fixture)

VOD = "https://www.twitch.tv/videos/2345678901"
KICK_VOD = "https://kick.com/xqc/videos/01a0251f-ef48-702d-9289-d66922da0e84"


# ---------------------------------------------------------------- timecode
@pytest.mark.parametrize("text, expected", [
    ("90", 90.0), ("0", 0.0), ("1:30", 90.0), ("01:30", 90.0), ("1:02:03", 3723.0),
    ("1:02:03.5", 3723.5), ("90.5", 90.5), ("  1:30  ", 90.0), ("0:05", 5.0),
    ("10:00:00", 36000.0), ("100", 100.0), ("100:00", 6000.0), ("1:00:00", 3600.0),
])
def test_parse_timecode_ok(text, expected):
    assert parse_timecode(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "", "   ", "-5", "-1:30", "abc", "1:2:3:4", "1:60", "1:75", "1:60:00", "1:00:60",
    "1::30", ":30", "1:", "1.5:30", "1:30.5.5", ".", "1e3", "١٢", "1:3 0", "0x10", "--1",
])
def test_parse_timecode_rejects(text):
    with pytest.raises(ValueError) as ei:
        parse_timecode(text)
    assert str(ei.value).strip() and len(str(ei.value)) < 80


def test_parse_timecode_none_is_value_error():
    with pytest.raises(ValueError):
        parse_timecode(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("seconds, always, expected", [
    (90, False, "1:30"), (0, False, "0:00"), (5, False, "0:05"), (3723, False, "1:02:03"),
    (3599, False, "59:59"), (3600, False, "1:00:00"), (90, True, "0:01:30"),
    (90.5, False, "1:30.5"), (3723.5, False, "1:02:03.5"), (59.96, False, "1:00"),
    (-3, False, "0:00"), (90.0, False, "1:30"),
])
def test_format_clock(seconds, always, expected):
    assert format_clock(seconds, always) == expected


def test_clock_roundtrip():
    for v in (0, 1.5, 59, 90, 3723.5, 40000):
        assert parse_timecode(format_clock(v)) == pytest.approx(v)


def test_existing_format_timecode_unchanged():
    assert format_timecode(90.5) == "01:30.500"
    assert format_timecode(3723, show_hours=True) == "01:02:03.000"


# --------------------------------------------------------------- Twitch / Kick URLs
@pytest.mark.parametrize("url, expected", [
    ("https://www.twitch.tv/videos/2345678901", VOD),
    ("https://twitch.tv/videos/2345678901?t=1h2m3s", VOD),
    ("http://m.twitch.tv/videos/2345678901/", VOD),
    ("  twitch.tv/videos/2345678901?filter=archives&sort=time  ", VOD),
    ("https://www.twitch.tv/somechannel/v/2345678901", VOD),
    ("https://www.twitch.tv/somechannel/video/2345678901?t=5m", VOD),
    ("https://www.twitch.tv/some_channel/clip/FaintLightGullWholeWheat-abc_123?filter=clips",
     "https://www.twitch.tv/some_channel/clip/FaintLightGullWholeWheat-abc_123"),
    ("https://m.twitch.tv/clip/FaintLightGullWholeWheat",
     "https://www.twitch.tv/clip/FaintLightGullWholeWheat"),
    ("https://clips.twitch.tv/FaintLightGullWholeWheat?tt_medium=clips_api",
     "https://clips.twitch.tv/FaintLightGullWholeWheat"),
    (KICK_VOD, KICK_VOD),
    ("https://www.kick.com/xqc/videos/01A0251F-EF48-702D-9289-D66922DA0E84?x=1", KICK_VOD),
    ("http://kick.com/xqc/videos/01a0251f-ef48-702d-9289-d66922da0e84/", KICK_VOD),
    ("https://kick.com/mxddy?clip=clip_01GYXVB5Y8PWAPWCWMSBCFB05X",
     "https://kick.com/mxddy/clips/clip_01GYXVB5Y8PWAPWCWMSBCFB05X"),
    ("https://kick.com/mxddy?foo=1&clip=clip_01GYXVB5Y8PWAPWCWMSBCFB05X&sort=date",
     "https://kick.com/mxddy/clips/clip_01GYXVB5Y8PWAPWCWMSBCFB05X"),
    ("https://www.kick.com/mxddy/clips/clip_01GYXVB5Y8PWAPWCWMSBCFB05X?utm=1",
     "https://kick.com/mxddy/clips/clip_01GYXVB5Y8PWAPWCWMSBCFB05X"),
])
def test_twitch_kick_accept_and_normalise(url, expected):
    assert validate_url(url) == expected
    assert validate_url(expected) == expected            # idempotent


@pytest.mark.parametrize("url", [
    # look-alike / hostile hosts
    "https://twitch.tv.evil.com/videos/123456",
    "https://www.twitch.tv.evil.com/videos/123456",
    "https://nottwitch.tv/videos/123456", "https://twitch.tv@evil.com/videos/123456",
    "https://user:pw@www.twitch.tv/videos/123456", "https://www.twitch.tv:8443/videos/123456",
    "https://evil.com/?u=twitch.tv/videos/123456", "https://evil.com/twitch.tv/videos/123456",
    "https://twitch.tv./videos/123456", "https://xn--twtch-8qa.tv/videos/123456",
    "https://twitch.com/videos/123456", "https://twitchtv.com/videos/123456",
    "https://clips.twitch.tv.evil.com/FaintLightGull", "https://evil.clips.twitch.tv/FaintLight",
    "https://player.twitch.tv/?video=123456", "https://go.twitch.tv/videos/123456",
    "https://kick.com.evil.com/xqc/videos/01a0251f-ef48-702d-9289-d66922da0e84",
    "https://notkick.com/xqc/videos/01a0251f-ef48-702d-9289-d66922da0e84",
    "https://kick.com@evil.com/xqc/videos/01a0251f-ef48-702d-9289-d66922da0e84",
    "https://evil.com/kick.com/xqc/videos/01a0251f-ef48-702d-9289-d66922da0e84",
    "https://kick.com:444/xqc/videos/01a0251f-ef48-702d-9289-d66922da0e84",
    "https://kick.tv/xqc/videos/01a0251f-ef48-702d-9289-d66922da0e84",
    "https://api.kick.com/xqc/videos/01a0251f-ef48-702d-9289-d66922da0e84",
    "https://www.twitch.tv/videos/123456\nhttps://evil.com",
    "https://evil.com\\@www.twitch.tv/videos/123456", "ftp://www.twitch.tv/videos/123456",
    # right host, not a single video / clip
    "https://www.twitch.tv/", "https://www.twitch.tv/somechannel",
    "https://www.twitch.tv/somechannel/", "https://www.twitch.tv/somechannel/videos",
    "https://www.twitch.tv/somechannel/clips", "https://www.twitch.tv/somechannel/about",
    "https://www.twitch.tv/videos", "https://www.twitch.tv/videos/", "https://www.twitch.tv/videos/abc",
    "https://www.twitch.tv/videos/12ab", "https://www.twitch.tv/videos/123/456",
    "https://www.twitch.tv/directory/game/Just%20Chatting",
    "https://www.twitch.tv/collections/o9zZer3IQBhTJw",
    "https://www.twitch.tv/somechannel/clip/", "https://www.twitch.tv/somechannel/clip/ab",
    "https://www.twitch.tv/somechannel/clip/a/b", "https://clips.twitch.tv/",
    "https://clips.twitch.tv/embed", "https://clips.twitch.tv/a/b",
    "https://www.twitch.tv/bad%20name/v/123456", "https://www.twitch.tv/a-b/v/123456",
    "https://kick.com/", "https://kick.com/xqc", "https://kick.com/xqc/videos",
    "https://kick.com/xqc/clips", "https://kick.com/video/01a0251f-ef48-702d-9289-d66922da0e84",
    "https://kick.com/xqc/videos/not-a-uuid", "https://kick.com/xqc/videos/01a0251f",
    "https://kick.com/xqc/videos/01a0251f-ef48-702d-9289-d66922da0e84x",
    "https://kick.com/xqc/videos/01a0251f-ef48-702d-9289-d66922da0e84/extra",
    "https://kick.com/xqc/clips/notaclip", "https://kick.com/xqc?clip=notaclip",
    "https://kick.com/xqc?clip=clip_", "https://kick.com/categories/gaming",
    "https://kick.com/xqc/chatroom",
])
def test_twitch_kick_reject(url):
    with pytest.raises(MediaError) as ei:
        validate_url(url)
    assert str(ei.value).strip()


@pytest.mark.parametrize("url", [
    "https://www.twitch.tv/somechannel", "https://twitch.tv/somechannel/",
    "https://m.twitch.tv/somechannel/videos", "https://www.twitch.tv/somechannel/clips",
    "https://kick.com/xqc", "https://www.kick.com/xqc/videos", "https://kick.com/xqc/clips",
])
def test_channel_pages_get_friendly_message(url):
    with pytest.raises(MediaError, match="This is a channel page; paste a video or clip link."):
        validate_url(url)


@pytest.mark.parametrize("url, site", [
    ("https://www.twitch.tv/videos/1", "twitch"), ("https://clips.twitch.tv/abc", "twitch"),
    ("https://m.twitch.tv/x", "twitch"), ("twitch.tv/x", "twitch"),
    ("https://kick.com/x", "kick"), ("https://www.kick.com/x", "kick"),
    ("https://twitch.tv.evil.com/", None), ("https://kick.com.evil.com/", None),
    ("https://player.twitch.tv/", None), ("https://notkick.com/", None),
])
def test_site_of_twitch_kick(url, site):
    assert site_of(url) == site


def test_supported_sites_and_help_text():
    assert SUPPORTED_SITES["twitch"] == "Twitch" and SUPPORTED_SITES["kick"] == "Kick"
    with pytest.raises(MediaError, match="YouTube, Reddit, TikTok, Instagram, Twitch or Kick"):
        validate_url("https://vimeo.com/1")


# ------------------------------------------------------------- section validation
def _info(duration=600.0, **kw):
    return VideoInfo(url=VOD, video_id="2345678901", title="Big VOD", duration=duration,
                     heights=[1080, 720], site="twitch", **kw)


@pytest.mark.parametrize("section", [
    (0, 10), (0, 0.5), (90, 135), (599.5, 600), (10, 600), (10, 600.9), (0.25, 0.75),
    [5, 20], (0, 600),
])
def test_section_valid(tmp_path, section):
    job = DownloadJob(_info(), "best", str(tmp_path), section=section)
    start, end = job._section
    assert 0 <= start < end <= 600 and end - start >= 0.5


def test_section_end_slightly_past_duration_is_clamped(tmp_path):
    assert DownloadJob(_info(), "best", str(tmp_path), section=(590, 600.8))._section \
        == (590.0, 600.0)


@pytest.mark.parametrize("section", [
    (-1, 10), (10, 10), (20, 10), (0, 0.4), (10, 10.49), (600, 700), (700, 800),
    (0, 602), (0, float("nan")), (float("nan"), 5), (0, float("inf")), (None, 5), (1,),
    ("a", "b"),
])
def test_section_invalid_raises_value_error(tmp_path, section):
    with pytest.raises(ValueError) as ei:
        DownloadJob(_info(), "best", str(tmp_path), section=section)
    assert str(ei.value).strip()


def test_no_section_by_default(tmp_path):
    job = DownloadJob(_info(), "best", str(tmp_path))
    assert job._section is None
    opts = job._options(str(tmp_path))
    assert "download_ranges" not in opts and "force_keyframes_at_cuts" not in opts


def test_section_options(tmp_path):
    job = DownloadJob(_info(), "720", str(tmp_path), section=(90, 135.5))
    opts = job._options(str(tmp_path))
    assert opts["force_keyframes_at_cuts"] is True
    ranges = list(opts["download_ranges"]({"id": "x", "duration": 600}, None))
    assert ranges == [{"start_time": 90.0, "end_time": 135.5}]
    assert opts["download_ranges"] == yt_dlp.utils.download_range_func(None, [(90.0, 135.5)])
    assert opts["format"] == downloader.build_format_selector("720")
    assert opts["noplaylist"] is True and opts["windowsfilenames"] is True
    for key in FORBIDDEN:
        assert key not in opts
    downloader._checked(dict(opts))              # the credential guard still passes


def test_section_options_progress_arguments(tmp_path):
    job = DownloadJob(_info(), "best", str(tmp_path), section=(0, 10))
    job._progress_file = r"C:\Temp\a b\p.progress"
    args = job._options(str(tmp_path))["external_downloader_args"]["ffmpeg_o"]
    assert args[0] == "-progress" and args[1] == "C:/Temp/a b/p.progress"
    for key in FORBIDDEN:
        assert key not in job._options(str(tmp_path))


@pytest.mark.parametrize("start, end, tag", [
    (90, 135, "1m30s-2m15s"), (0, 45, "0s-45s"), (3723, 3800, "1h02m03s-1h03m20s"),
    (90.5, 100, "1m30.5s-1m40s"), (59.96, 120, "1m00s-2m00s"), (3600, 7200, "1h00m00s-2h00m00s"),
])
def test_section_tag(start, end, tag):
    assert section_tag(start, end) == tag
    assert not set(tag) & set('<>:"/\\|?*')


# -------------------------------------------------------- jobs through the fake
def _sec_dict(**kw):
    d = {"id": "2345678901", "title": 'Big: "VOD" / part?', "duration": 600.0,
         "uploader": "chan", "width": 1920, "height": 1080,
         "formats": [{"height": 1080, "vcodec": "avc1", "acodec": "aac", "ext": "mp4"},
                     {"height": 720, "vcodec": "avc1", "acodec": "aac", "ext": "mp4"}]}
    d.update(kw)
    return d


def test_section_download_name_and_options(qtbot, fake, tmp_path):
    fake.info = _sec_dict(title="Big VOD", id="2345678901")
    job = DownloadJob(_info(), "best", str(tmp_path), section=(90, 135))
    stages = []
    job.stage.connect(stages.append)
    with qtbot.waitSignal(job.finished, timeout=10000) as sig:
        job.start()
    path = sig.args[0]
    assert os.path.basename(path) == "Big VOD [2345678901] 1m30s-2m15s.mp4"
    assert "Downloading section…" in stages
    opts = fake.instances[0].opts
    assert opts["force_keyframes_at_cuts"] is True
    assert list(fake.instances[0].params["download_ranges"]({"duration": 600}, None)) == [
        {"start_time": 90.0, "end_time": 135.0}]
    for key in FORBIDDEN:
        assert key not in opts
    assert not os.path.exists(opts["external_downloader_args"]["ffmpeg_o"][1])   # temp removed


def test_section_name_never_collides_with_full_download(qtbot, fake, tmp_path):
    fake.info = _sec_dict(title="Big VOD")
    (tmp_path / "Big VOD [2345678901].mp4").write_bytes(b"full")
    job = DownloadJob(_info(), "best", str(tmp_path), section=(90, 135))
    with qtbot.waitSignal(job.finished, timeout=10000) as sig:
        job.start()
    assert os.path.basename(sig.args[0]) == "Big VOD [2345678901] 1m30s-2m15s.mp4"
    assert (tmp_path / "Big VOD [2345678901].mp4").read_bytes() == b"full"
    # the same section again: unique_path still protects the first section file
    job2 = DownloadJob(_info(), "best", str(tmp_path), section=(90, 135))
    with qtbot.waitSignal(job2.finished, timeout=10000) as sig2:
        job2.start()
    assert os.path.basename(sig2.args[0]) == "Big VOD [2345678901] 1m30s-2m15s (2).mp4"


def test_section_title_is_windows_safe(qtbot, fake, tmp_path):
    job = DownloadJob(_info(), "best", str(tmp_path), section=(0, 5))
    fake.info = _sec_dict(title="plain")
    with qtbot.waitSignal(job.finished, timeout=10000) as sig:
        job.start()
    assert not set(os.path.basename(sig.args[0])) & set('<>:"/\\|?*')


def test_full_download_name_unchanged(qtbot, fake, tmp_path):
    fake.info = _sec_dict(title="Big VOD")
    job = DownloadJob(_info(), "best", str(tmp_path))
    with qtbot.waitSignal(job.finished, timeout=10000) as sig:
        job.start()
    assert os.path.basename(sig.args[0]) == "Big VOD [2345678901].mp4"
    assert "download_ranges" not in fake.instances[0].opts


def test_section_beyond_real_duration_fails_friendly(qtbot, fake, tmp_path):
    fake.info = _sec_dict(title="Big VOD", duration=100.0)     # shorter than VideoInfo says
    job = DownloadJob(_info(), "best", str(tmp_path), section=(90, 135))
    with qtbot.waitSignal(job.failed, timeout=10000) as sig:
        job.start()
    assert "after the end" in sig.args[0] and "1:40" in sig.args[0]
    assert not list(tmp_path.glob("*"))


@pytest.mark.parametrize("kw", [{"is_live": True}, {"duration": 0.0}])
def test_live_or_unknown_duration_refused_for_sections(qtbot, fake, tmp_path, kw):
    info = _info(**{k: v for k, v in kw.items() if k == "is_live"})
    if "duration" in kw:
        info.duration = 0.0
    job = DownloadJob(info, "best", str(tmp_path), section=(10, 20))
    with qtbot.waitSignal(job.failed, timeout=5000) as sig:
        job.start()
    assert sig.args[0] == LIVE_MESSAGE == "Live streams can't be downloaded; wait until " \
                                          "the VOD is available."
    assert not fake.instances


def test_live_refused_without_section_too(qtbot, fake, tmp_path):
    job = DownloadJob(_info(is_live=True), "best", str(tmp_path))
    with qtbot.waitSignal(job.failed, timeout=5000) as sig:
        job.start()
    assert sig.args[0] == LIVE_MESSAGE and not fake.instances


def test_live_detected_after_extraction(qtbot, fake, tmp_path):
    fake.info = _sec_dict(is_live=True)
    job = DownloadJob(_info(), "best", str(tmp_path), section=(10, 20))
    with qtbot.waitSignal(job.failed, timeout=5000) as sig:
        job.start()
    assert sig.args[0] == LIVE_MESSAGE


def test_section_progress_comes_from_ffmpeg_time(qtbot, fake, tmp_path, monkeypatch):
    """A fake FFmpeg writes -progress lines; the monitor turns them into fractions and
    the byte-based yt-dlp hooks are ignored for sections."""
    fake.info = _sec_dict(title="Big VOD")

    class ProgressYDL(FakeYDL):
        def process_ie_result(self, info, download=True):
            path = self.opts["external_downloader_args"]["ffmpeg_o"][1]
            for h in self.opts["progress_hooks"]:      # byte hook noise must be ignored
                h({"status": "downloading", "filename": "x.part", "downloaded_bytes": 5,
                   "total_bytes": 10})
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("frame=10\nout_time_us=22500000\nspeed=2.5x\nprogress=continue\n")
            time.sleep(0.9)
            return super().process_ie_result(info, download)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", ProgressYDL)
    job = DownloadJob(_info(), "best", str(tmp_path), section=(90, 135))
    seen = []
    job.progress.connect(lambda f, s, e: seen.append((f, s)))
    with qtbot.waitSignal(job.finished, timeout=10000):
        job.start()
    mids = [x for x in seen if 0 < x[0] < 1]
    assert mids and mids[0][0] == pytest.approx(0.5, abs=0.01) and mids[0][1] == "2.5x"
    assert seen[0][0] == -1.0 and seen[-1][0] == 1.0
    assert all(f == -1.0 or 0 <= f <= 1 for f, _ in seen)


def test_section_cancel_kills_ffmpeg_child(qtbot, fake, tmp_path, monkeypatch):
    """The FFmpeg downloader blocks in Popen.wait() without hooks: cancel() must kill the
    child (started through yt-dlp's own Popen name) and report 'cancelled'."""
    assert downloader._TRACKING
    from yt_dlp.downloader import external
    started = threading.Event()
    holder = {}

    class BlockingYDL(FakeYDL):
        def process_ie_result(self, info, download=True):
            proc = external.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                  stdin=subprocess.PIPE)
            holder["proc"] = proc
            started.set()
            with proc:
                rc = proc.wait()
            raise yt_dlp.utils.DownloadError(f"ffmpeg exited with code {rc}")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", BlockingYDL)
    fake.info = _sec_dict(title="Big VOD")
    job = DownloadJob(_info(), "best", str(tmp_path), section=(0, 60))
    failures = []
    job.failed.connect(failures.append)
    t0 = time.monotonic()
    with qtbot.waitSignal(job.cancelled, timeout=15000):
        job.start()
        assert started.wait(10)
        job.cancel()
    assert time.monotonic() - t0 < 10
    qtbot.wait(100)
    assert not failures
    assert holder["proc"].poll() is not None            # the child is really gone
    assert not list(tmp_path.glob("*.mp4"))
    assert not downloader._thread_procs and not downloader._watched_threads


def test_tracking_is_transparent_for_other_threads():
    from yt_dlp.downloader import external
    proc = external.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)
    proc.wait()
    assert not downloader._thread_procs                 # unwatched thread: not recorded


# ----------------------------------------------------------- metadata for VODs
def test_metadata_twitch_vod_has_duration(qtbot, fake):
    from clipforge.media.downloader import MetadataJob
    fake.info = _sec_dict(duration=7384, title="", live_status="was_live")
    job = MetadataJob(VOD + "?t=3m")
    with qtbot.waitSignal(job.finished, timeout=5000) as sig:
        job.start()
    info = sig.args[0]
    assert info.site == "twitch" and info.url == VOD and info.duration == 7384.0
    assert info.is_live is False and info.title == "Twitch 2345678901"
    assert info.available_qualities() == ["best", "1080", "720"]


def test_metadata_kick_vod_and_live_flag(qtbot, fake):
    from clipforge.media.downloader import MetadataJob
    fake.info = _sec_dict(id="01a0251f-ef48-702d-9289-d66922da0e84", duration=42250,
                          live_status="is_live")
    job = MetadataJob(KICK_VOD)
    with qtbot.waitSignal(job.finished, timeout=5000) as sig:
        job.start()
    assert sig.args[0].site == "kick" and sig.args[0].duration == 42250.0
    assert sig.args[0].is_live is True


# ---------------------------------------------------------------- errors
@pytest.mark.parametrize("raw, site", [
    ("ERROR: [twitch:vod] 123: You must be logged into an account that has access to this "
     "subscriber-only content. Use --cookies", "twitch"),
    ("ERROR: [twitch:vod] 123: Your account does not have access to this subscriber-only "
     "content", "twitch"),
    ("ERROR: [kick:vod] x: This video requires a channel subscription", "kick"),
    ("ERROR: [kick:vod] x: Sub-only VOD", "kick"),
    ("ERROR: x: this VOD is for subscribers only", "twitch"),
])
def test_translate_sub_only(raw, site):
    msg = translate_error(Exception(raw), site)
    assert msg.startswith("This VOD is for subscribers only")
    assert "[" not in msg and "ERROR" not in msg


@pytest.mark.parametrize("raw", [
    "ERROR: [twitch:vod] 123: This VOD has expired",
    "ERROR: [twitch:vod] 123: Video 123 does not exist",
    "ERROR: [kick:vod] x: the VOD was deleted",
])
def test_translate_expired(raw):
    msg = translate_error(Exception(raw), "twitch")
    assert "expired or been deleted" in msg and "[" not in msg


@pytest.mark.parametrize("raw", [
    "ERROR: [twitch:vod] 1: This stream contains mature content. Log in to watch",
    "ERROR: [kick:vod] 1: This video is private",
    "ERROR: [twitch:clips] 1: HTTP Error 403: Forbidden",
    "ERROR: [kick:clips] 1: Log in for access to age-restricted content",
])
@pytest.mark.parametrize("site", ["twitch", "kick"])
def test_translate_login_style_for_twitch_kick(raw, site):
    msg = translate_error(Exception(raw), site)
    assert "needs a login or isn't public" in msg and "[" not in msg


@pytest.mark.parametrize("raw", [
    "ERROR: [twitch:vod] 1: Unable to download JSON metadata: HTTP Error 500: Internal "
    "Server Error",
    "ERROR: [twitch:clips] 1: Unable to extract clip data; please report this issue",
    "ERROR: [twitch:vod] 1: Unable to download m3u8 information: HTTP Error 502",
    "ERROR: [kick] x: Unexpected response from the Kick API",
    "ERROR: [twitch:vod] 1: GQL request failed",
    "ERROR: Unsupported URL: https://www.twitch.tv/x/schedule",
])
@pytest.mark.parametrize("site", ["twitch", "kick"])
def test_translate_twitch_kick_breakage_says_update_the_downloader(raw, site):
    msg = translate_error(Exception(raw), site)
    assert "update the downloader" in msg.lower() and "ERROR" not in msg


def test_translate_twitch_network_error_is_not_an_update_hint():
    msg = translate_error("ERROR: [twitch:vod] 1: Unable to download JSON metadata: "
                          "<urlopen error getaddrinfo failed>", "twitch")
    assert "Twitch" in msg and "internet" in msg


def test_translate_twitch_404_is_unavailable_video():
    msg = translate_error("ERROR: [twitch:vod] 1: HTTP Error 404: Not Found", "twitch")
    assert "unavailable" in msg.lower() and "video" in msg.lower()


def test_download_error_for_sub_only_reaches_failed_signal(qtbot, fake, tmp_path):
    fake.extract_error = yt_dlp.utils.DownloadError(
        "ERROR: [twitch:vod] 1: You must be logged into an account that has access to "
        "this subscriber-only content")
    job = DownloadJob(_info(), "best", str(tmp_path), section=(0, 10))
    with qtbot.waitSignal(job.failed, timeout=5000) as sig:
        job.start()
    assert "subscribers only" in sig.args[0]


def test_bind_ffmpeg_location_makes_ffmpegfd_available_off_path():
    """yt-dlp's FFmpegFD.available() only sees ffmpeg_location via a context variable that
    API users must set; without it sections fail when ffmpeg isn't on PATH."""
    from yt_dlp.downloader.external import FFmpegFD
    from clipforge.media import ffmpeg
    result = {}

    def work():
        downloader._bind_ffmpeg_location(os.path.dirname(ffmpeg.ffmpeg_path()))
        result["ok"] = FFmpegFD.available()

    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    assert result["ok"] is True
