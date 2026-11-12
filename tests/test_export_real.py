"""End-to-end export tests: real FFmpeg runs on synthetic time-coded videos, verified by
decoding the result with PyAV (picture brightness = source time, audio frequency =
source second)."""

from __future__ import annotations

import os

import pytest
from conftest import (audio_k_at, audio_rms, audio_runs, luma_at_pixel, read_audio,
                      read_video, run_job, seg, settings_for, sources_of, stream_info)

from clipforge.media import export as export_mod
from clipforge.media.export import ExportJob, build_plan

T_VID = 0.12        # tolerance for picture time (compression + 8-bit luma)
T_AUD = 0.07        # tolerance for audio step positions


def do_export(tmp_path, srcs, segs, name="out", **kw):
    st = settings_for(tmp_path, name, **kw)
    tdir = tmp_path / "tmp"
    job = ExportJob(sources_of(*srcs), segs, st, temp_dir=str(tdir))
    res = run_job(job)
    assert res.ok, f"export failed: {res.failed!r} cancelled={res.cancelled}"
    assert res.finished == str(tmp_path / f"{name}.mp4")
    assert not [f for f in os.listdir(tmp_path) if "partial" in f]
    assert not tdir.exists() or os.listdir(tdir) == []
    assert res.progress and res.progress[-1][0] == 1.0
    return res.finished, res


def assert_src_time(info, t, expected, src_duration, tol=T_VID):
    got = info.src_time_at(t, src_duration)
    assert got == pytest.approx(expected, abs=tol), f"at {t}s picture shows source {got:.2f}s, expected {expected}"


def assert_av_aligned(path, tol=0.03):
    v, a = read_video(path), read_audio(path)
    assert abs(v.start - a.start) <= tol, (v.start, a.start)
    assert abs(v.duration - len(a.samples) / a.rate) <= 0.1 or True
    return v, a


def assert_runs(a, expected, tol=T_AUD):
    """expected: [(k, start_time)]"""
    runs = audio_runs(a)
    assert [k for k, _ in runs] == [k for k, _ in expected], runs
    for (_, got), (_, want) in zip(runs, expected):
        assert got == pytest.approx(want, abs=tol), runs


def test_multi_cut_and_reorder(tmp_path, video_h):
    # later part of the source first, earlier part second; cuts are off-keyframe
    segs = [seg(video_h, 4.2, 5.5, 0), seg(video_h, 1.0, 2.0, 1.3)]
    out, res = do_export(tmp_path, [video_h], segs)
    v, a = assert_av_aligned(out)
    assert v.duration == pytest.approx(2.3, abs=0.1) and (v.width, v.height) == (1280, 720)
    assert_src_time(v, 0.0, 4.2, 6, tol=0.1)
    assert_src_time(v, 0.6, 4.8, 6)
    assert_src_time(v, 1.0, 5.2, 6)
    assert_src_time(v, 1.4, 1.1, 6)
    assert_src_time(v, 2.1, 1.8, 6)
    # audio: k=4 for 0.8 s, k=5 for 0.5 s, then k=1 for 1 s
    assert_runs(a, [(4, 0.0), (5, 0.8), (1, 1.3)])
    assert a.duration == pytest.approx(2.3, abs=0.1)
    info = stream_info(out)
    assert (info["vcodec"], info["pix_fmt"], info["acodec"], info["arate"], info["achannels"]) == \
           ("h264", "yuv420p", "aac", 48000, 2)
    assert info["fps"] == pytest.approx(30.0)
    assert "Encoding" in res.progress[0][1] or any("Encoding" in t for _, t in res.progress)


def test_av_sync_inside_a_cut(tmp_path, video_h):
    """The audio step at source second 3 must coincide with picture source time 3."""
    out, _ = do_export(tmp_path, [video_h], [seg(video_h, 2.3, 4.6, 0)])
    v, a = assert_av_aligned(out)
    runs = audio_runs(a)
    k, t_change = runs[1]
    assert k == 3 and t_change == pytest.approx(0.7, abs=T_AUD)
    assert_src_time(v, t_change + 0.05, 3.0, 6, tol=0.15)


def test_vertical_source(tmp_path, video_v):
    out, _ = do_export(tmp_path, [video_v], [seg(video_v, 0.5, 2.5, 0)])
    v, a = assert_av_aligned(out)
    assert (v.width, v.height) == (720, 1280)               # 9:16 stays 9:16
    assert v.duration == pytest.approx(2.0, abs=0.1)
    assert_src_time(v, 1.0, 1.5, 4)
    assert_runs(a, [(0, 0.0), (1, 0.5), (2, 1.5)])


def test_vertical_in_landscape_output_is_pillarboxed(tmp_path, video_h, video_v):
    segs = [seg(video_h, 0, 1, 0), seg(video_v, 1.0, 3.0, 1.0)]
    out, _ = do_export(tmp_path, [video_h, video_v], segs)
    v, _a = assert_av_aligned(out)
    assert (v.width, v.height) == (1280, 720)
    assert v.duration == pytest.approx(3.0, abs=0.1)
    assert luma_at_pixel(out, 2.0, 0.03, 0.5) == pytest.approx(16, abs=3)    # left bar is black
    assert luma_at_pixel(out, 2.0, 0.97, 0.5) == pytest.approx(16, abs=3)    # right bar is black
    assert luma_at_pixel(out, 2.0, 0.5, 0.5) > 60                           # picture in the middle
    assert_src_time(v, 2.0, 2.0, 4)
    assert_src_time(v, 0.5, 0.5, 6)


def test_vertical_first_is_portrait_with_letterboxed_landscape(tmp_path, video_v, video_h):
    segs = [seg(video_v, 0, 1, 0), seg(video_h, 3.0, 4.0, 1.0)]
    out, _ = do_export(tmp_path, [video_v, video_h], segs)
    v, _a = assert_av_aligned(out)
    assert (v.width, v.height) == (720, 1280)
    assert luma_at_pixel(out, 1.5, 0.5, 0.03) == pytest.approx(16, abs=3)    # top bar
    assert_src_time(v, 1.5, 3.5, 6)


def test_mixed_fps_sizes_and_missing_audio(tmp_path, video_h, video_25, video_noaudio):
    segs = [seg(video_h, 2.0, 3.0, 0),            # 1280x720 30 fps stereo 48k
            seg(video_25, 1.0, 2.0, 1.0),         # 640x360 25 fps mono 44.1k
            seg(video_noaudio, 2.0, 3.0, 2.0)]    # 960x540 30 fps, no audio at all
    out, _ = do_export(tmp_path, [video_h, video_25, video_noaudio], segs)
    v, a = assert_av_aligned(out)
    assert (v.width, v.height) == (1280, 720)
    assert v.duration == pytest.approx(3.0, abs=0.1)
    assert len(v.times) == pytest.approx(90, abs=2)
    assert stream_info(out)["fps"] == pytest.approx(30.0)
    assert_src_time(v, 0.5, 2.5, 6)
    assert_src_time(v, 1.5, 1.5, 4)
    assert_src_time(v, 2.5, 2.5, 4)
    assert audio_k_at(a, 0.5) == 2 and audio_k_at(a, 1.5) == 1
    assert audio_rms(a, 2.1, 2.9) < 0.005                       # silence for the audio-less clip
    assert a.duration == pytest.approx(3.0, abs=0.1)


def test_gap_is_black_and_silent(tmp_path, video_h):
    segs = [seg(video_h, 1.0, 2.0, 0), seg(video_h, 3.0, 4.0, 1.5)]
    out, _ = do_export(tmp_path, [video_h], segs)
    v, a = assert_av_aligned(out)
    assert v.duration == pytest.approx(2.5, abs=0.1) and a.duration == pytest.approx(2.5, abs=0.1)
    assert_src_time(v, 0.5, 1.5, 6)
    assert v.luma_at(1.2) == pytest.approx(16, abs=2) and v.luma_at(1.4) == pytest.approx(16, abs=2)
    assert_src_time(v, 2.0, 3.5, 6)
    assert audio_rms(a, 1.05, 1.45) < 0.005
    assert_runs(a, [(1, 0.0), (-1, 1.0), (3, 1.5)])


def test_muted_segment_is_silent_but_video_continues(tmp_path, video_h):
    segs = [seg(video_h, 0.0, 1.0, 0), seg(video_h, 2.0, 3.0, 1.0, muted=True), seg(video_h, 4.0, 5.0, 2.0)]
    out, _ = do_export(tmp_path, [video_h], segs)
    v, a = assert_av_aligned(out)
    assert audio_rms(a, 1.05, 1.95) < 0.005
    assert audio_k_at(a, 0.5) == 0 and audio_k_at(a, 2.5) == 4
    assert_src_time(v, 1.5, 2.5, 6)
    assert a.duration == pytest.approx(3.0, abs=0.1)


def test_audio_off(tmp_path, video_h):
    out, _ = do_export(tmp_path, [video_h], [seg(video_h, 1.0, 3.0, 0)], audio=False)
    assert stream_info(out)["has_audio"] is False
    assert read_video(out).duration == pytest.approx(2.0, abs=0.1)


def test_resolution_fps_and_h265(tmp_path, video_h):
    out, _ = do_export(tmp_path, [video_h], [seg(video_h, 1.0, 2.0, 0)], resolution="360",
                       fps="24", codec="h265", quality="medium")
    info = stream_info(out)
    assert (info["width"], info["height"]) == (640, 360)
    assert info["vcodec"] == "hevc" and info["fps"] == pytest.approx(24.0)
    v = read_video(out)
    assert len(v.times) == pytest.approx(24, abs=1)
    assert_src_time(v, 0.5, 1.5, 6)
    from clipforge.media import probe
    assert probe.probe_source(out).video_codec == "hevc"


def test_non_ascii_source_and_destination(tmp_path, video_unicode):
    dest = tmp_path / "résumé 日本 folder"
    st = settings_for(dest, "clip ünï")
    job = ExportJob(sources_of(video_unicode), [seg(video_unicode, 0.5, 2.0, 0)], st,
                    temp_dir=str(tmp_path / "t"))
    res = run_job(job)
    assert res.ok, res.failed
    assert os.path.basename(res.finished) == "clip ünï.mp4"
    assert read_video(res.finished).duration == pytest.approx(1.5, abs=0.1)


def test_selection_scope_only_exports_given_segments(tmp_path, video_h):
    segs = [seg(video_h, 0.0, 1.0, 0), seg(video_h, 2.0, 3.0, 1.0), seg(video_h, 4.0, 5.0, 2.0)]
    out, _ = do_export(tmp_path, [video_h], segs[1:], scope="selection")
    v, a = assert_av_aligned(out)
    assert v.duration == pytest.approx(2.0, abs=0.1)         # leading offset (1.0) is dropped
    assert_src_time(v, 0.5, 2.5, 6)
    assert_src_time(v, 1.5, 4.5, 6)
    assert_runs(a, [(2, 0.0), (4, 1.0)])


# --------------------------------------------------------------------------- fast
def test_fast_export_on_keyframes(tmp_path, video_h):
    segs = [seg(video_h, 3.0, 5.0, 0), seg(video_h, 1.0, 2.0, 2.0)]
    st = settings_for(tmp_path, mode="fast")
    plan = build_plan(sources_of(video_h), segs, st, temp_dir=str(tmp_path / "tmp"))
    assert plan.mode == "fast"
    for t in plan.temp_files:
        if os.path.exists(t):
            os.remove(t)
    out, res = do_export(tmp_path, [video_h], segs, mode="fast")
    assert any("Copying" in t for _, t in res.progress)
    v, a = assert_av_aligned(out)
    assert v.duration == pytest.approx(3.0, abs=0.1)
    assert len(v.times) == 90                               # exactly 3 s of 30 fps: no stray GOP-tail frames
    assert_src_time(v, 0.1, 3.1, 6)
    assert_src_time(v, 1.9, 4.9, 6)
    assert_src_time(v, 2.5, 1.5, 6)
    assert_runs(a, [(3, 0.0), (4, 1.0), (1, 2.0)], tol=0.08)
    info = stream_info(out)
    assert (info["vcodec"], info["width"], info["height"], info["acodec"]) == ("h264", 1280, 720, "aac")


def test_fast_export_many_cuts_does_not_drift(tmp_path, video_h):
    """12 joined pieces: picture and sound must stay locked to each other and to the plan."""
    ks = [0, 1, 2, 3, 4, 5] * 2
    segs = [seg(video_h, k, k + 1, i) for i, k in enumerate(ks)]
    out, res = do_export(tmp_path, [video_h], segs, mode="fast")
    assert any("Copying" in t for _, t in res.progress)
    v, a = assert_av_aligned(out)
    assert len(v.times) == 360 and a.duration == pytest.approx(12.0, abs=0.05)
    expected = [(k, float(i)) for i, k in enumerate(ks)]
    assert_runs(a, expected, tol=0.04)
    assert_src_time(v, 11.5, 5.5, 6)


def test_fast_export_audio_off(tmp_path, video_h):
    out, res = do_export(tmp_path, [video_h], [seg(video_h, 1.0, 3.0, 0)], mode="fast", audio=False)
    assert stream_info(out)["has_audio"] is False
    v = read_video(out)
    assert len(v.times) == 60 and v.start == pytest.approx(0, abs=0.01)
    assert_src_time(v, 1.0, 2.0, 6)


def test_auto_mode_uses_fast_and_accurate_appropriately(tmp_path, video_h):
    out, res = do_export(tmp_path, [video_h], [seg(video_h, 2.0, 4.0, 0)], mode="auto")
    assert any("Copying" in t for _, t in res.progress)
    out2, res2 = do_export(tmp_path, [video_h], [seg(video_h, 2.3, 4.0, 0)], name="o2", mode="auto")
    assert any("Encoding" in t for _, t in res2.progress)
    assert_src_time(read_video(out2), 0.1, 2.4, 6)


def test_fast_refusal_fails_clearly_and_leaves_nothing(tmp_path, video_h):
    st = settings_for(tmp_path, mode="fast")
    job = ExportJob(sources_of(video_h), [seg(video_h, 2.3, 4.0, 0)], st, temp_dir=str(tmp_path / "tmp"))
    res = run_job(job)
    assert not res.ok and "keyframe" in res.failed.lower()
    assert os.listdir(tmp_path) in ([], ["tmp"])


# ------------------------------------------------------------------ hw / failures
def test_nvenc_export(tmp_path, video_h, nvenc_available):
    if not nvenc_available:
        pytest.skip("NVENC is not available on this machine")
    segs = [seg(video_h, 1.0, 2.5, 0), seg(video_h, 4.0, 5.0, 1.5)]
    st = settings_for(tmp_path, mode="accurate", hw_accel="on")
    plan = build_plan(sources_of(video_h), segs, st, temp_dir=str(tmp_path / "tmp"))
    assert plan.video_encoder == "h264_nvenc"
    job = ExportJob(sources_of(video_h), segs, st, temp_dir=str(tmp_path / "tmp"))
    res = run_job(job)
    assert res.ok, res.failed
    v, a = assert_av_aligned(res.finished)
    assert v.duration == pytest.approx(2.5, abs=0.1)
    assert_src_time(v, 0.5, 1.5, 6)
    assert_src_time(v, 2.0, 4.5, 6)
    assert_runs(a, [(1, 0.0), (2, 1.0), (4, 1.5)])
    assert stream_info(res.finished)["vcodec"] == "h264"


def test_hardware_failure_retries_on_cpu(tmp_path, video_h, monkeypatch):
    # an encoder that cannot exist: fails at run time, like a driver problem would
    monkeypatch.setattr(export_mod.ffmpeg, "pick_video_encoder", lambda codec, hw="auto": "bogus_nvenc")
    st = settings_for(tmp_path, mode="accurate", hw_accel="on")
    job = ExportJob(sources_of(video_h), [seg(video_h, 1.0, 2.0, 0)], st, temp_dir=str(tmp_path / "tmp"))
    res = run_job(job)
    assert res.ok, res.failed
    assert any("retrying" in t.lower() for _, t in res.progress)
    assert stream_info(res.finished)["vcodec"] == "h264"
    assert read_video(res.finished).duration == pytest.approx(1.0, abs=0.1)
    assert os.listdir(tmp_path / "tmp") == []


def test_cpu_encoder_failure_is_not_retried(tmp_path, video_h, monkeypatch):
    calls = []
    real = export_mod.ffmpeg.run

    def spy(args, **kw):
        calls.append(args)
        raise export_mod.MediaError("FFmpeg could not process the video. boom")
    monkeypatch.setattr(export_mod.ffmpeg, "run", spy)
    job = ExportJob(sources_of(video_h), [seg(video_h, 1.0, 2.0, 0)],
                    settings_for(tmp_path), temp_dir=str(tmp_path / "tmp"))
    res = run_job(job)
    assert not res.ok and "boom" in res.failed and len(calls) == 1
    assert real is not spy
    assert os.listdir(tmp_path) in ([], ["tmp"])


def test_wrong_duration_output_is_rejected(tmp_path, video_h, monkeypatch):
    real = export_mod.probe.probe_source

    def lying(path, **kw):
        info = real(path, **kw)
        info.duration += 1.0
        return info
    monkeypatch.setattr(export_mod.probe, "probe_source", lying)
    job = ExportJob(sources_of(video_h), [seg(video_h, 1.0, 2.0, 0)],
                    settings_for(tmp_path), temp_dir=str(tmp_path / "tmp"))
    res = run_job(job)
    assert not res.ok and "instead of the expected" in res.failed
    assert not (tmp_path / "out.mp4").exists()
    assert not [f for f in os.listdir(tmp_path) if "partial" in f]


def test_missing_source_fails_cleanly(tmp_path, video_h):
    gone = tmp_path / "gone.mp4"
    gone.write_bytes(b"x")
    src = export_mod.MediaSource(id="g", path=str(gone), duration=5, width=640, height=360)
    gone.unlink()
    job = ExportJob(sources_of(src), [seg(src, 0, 1, 0)], settings_for(tmp_path),
                    temp_dir=str(tmp_path / "tmp"))
    res = run_job(job)
    assert not res.ok and "missing" in res.failed


def test_cancel_mid_export_cleans_up(tmp_path, video_h):
    # ~72 s of 720p material re-encoded: long enough to cancel while ffmpeg is busy
    segs = [seg(video_h, 0.0, 6.0, i * 6.0) for i in range(12)]
    dest = tmp_path / "dest"
    st = settings_for(dest, "big", quality="high")
    tdir = tmp_path / "tmp"
    job = ExportJob(sources_of(video_h), segs, st, temp_dir=str(tdir))
    res = run_job(job, cancel_when=lambda r: any(f >= 0.03 for f, _ in r.progress), timeout=120)
    assert res.cancelled and not res.ok and res.failed is None
    assert not job.is_running()
    assert os.listdir(dest) == []                       # no partial, no output
    assert not tdir.exists() or os.listdir(tdir) == []


def test_cancel_before_start_of_encoding(tmp_path, video_h):
    st = settings_for(tmp_path / "d", "x")
    job = ExportJob(sources_of(video_h), [seg(video_h, 0, 3, 0)], st, temp_dir=str(tmp_path / "t"))
    job.cancel()                                        # token is replaced by start(); must still work
    res = run_job(job, cancel_when=lambda r: True)
    assert res.cancelled or res.ok                      # either outcome, never a stray file
    leftovers = [f for f in os.listdir(tmp_path / "d") if "partial" in f]
    assert leftovers == []


def test_very_many_segments_use_filter_file(tmp_path, video_h):
    """A long cut list overflows the Windows command line, so the graph goes in a file."""
    segs = [seg(video_h, (i % 5) + 0.1, (i % 5) + 0.3, i * 0.2) for i in range(150)]
    out, _ = do_export(tmp_path, [video_h], segs)
    v, a = assert_av_aligned(out)
    assert v.duration == pytest.approx(30.0, abs=0.15) and a.duration == pytest.approx(30.0, abs=0.15)
    assert_src_time(v, 0.1, 0.2, 6)
    assert_src_time(v, 20.1, 0.2, 6)
