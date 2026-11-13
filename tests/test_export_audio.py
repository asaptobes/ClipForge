"""Detached-audio export (mixed video/audio lanes) and audio extraction."""

from __future__ import annotations

import os
import uuid

import av
import pytest
from conftest import (audio_rms, audio_runs, read_audio, read_video, run_job, seg,
                      settings_for, sources_of, stream_info)

from clipforge.core.model import Segment
from clipforge.media import MediaError, ffmpeg
from clipforge.media.audio_extract import ExtractAudioJob, build_audio_plan
from clipforge.media.export import ExportJob, build_plan, can_fast_export


def aseg(src, a, b, start, muted=False) -> Segment:
    return Segment(uuid.uuid4().hex[:12], src.id, a, b, start, muted, track="audio")


def vmuted(src, a, b, start) -> Segment:
    return seg(src, a, b, start, muted=True)


def plan_of(tmp_path, srcs, segs, **kw):
    return build_plan(sources_of(*srcs), segs, settings_for(tmp_path, **kw),
                      temp_dir=str(tmp_path / "tmp"), video_encoder="libx264")


def graph_of(plan):
    a = plan.steps[0].args
    return a[a.index("-filter_complex") + 1]


# ------------------------------------------------------------------------- planning
def test_plan_has_delay_and_mix(tmp_path, video_h):
    plan = plan_of(tmp_path, [video_h], [vmuted(video_h, 0, 2, 0), aseg(video_h, 0, 2, 1.0)])
    g = graph_of(plan)
    assert plan.mode == "accurate" and "adelay=48000S:all=1" in g
    assert "amix=inputs=2:duration=longest:normalize=0" in g
    assert plan.duration == pytest.approx(3.0)


def test_plan_without_audio_clips_unchanged(tmp_path, video_h):
    g = graph_of(plan_of(tmp_path, [video_h], [seg(video_h, 0, 2, 0)]))
    assert "amix" not in g and "adelay" not in g


def test_fast_refused_with_audio_clips(tmp_path, video_h):
    segs = [seg(video_h, 0, 2, 0), aseg(video_h, 0, 2, 0)]
    ok, why = can_fast_export(sources_of(video_h), segs, settings_for(tmp_path))
    assert not ok and "audio" in why
    p = plan_of(tmp_path, [video_h], segs, mode="auto")
    assert p.mode == "accurate"
    with pytest.raises(MediaError):
        plan_of(tmp_path, [video_h], segs, mode="fast")


def test_noaudio_source_ok(tmp_path, video_h, video_noaudio):
    segs = [seg(video_h, 0, 2, 0), aseg(video_noaudio, 0, 2, 0.5)]
    p = plan_of(tmp_path, [video_h, video_noaudio], segs)
    assert "adelay" not in graph_of(p) and "amix" not in graph_of(p)
    assert p.duration == pytest.approx(2.5)        # the clip still occupies its time


def test_muted_audio_dropped(tmp_path, video_h):
    p = plan_of(tmp_path, [video_h], [seg(video_h, 0, 2, 0), aseg(video_h, 0, 2, 0.5, muted=True)])
    assert "amix" not in graph_of(p)


def test_audio_past_video_extends_length(tmp_path, video_h):
    p = plan_of(tmp_path, [video_h], [vmuted(video_h, 0, 1, 0), aseg(video_h, 0, 2, 2.0)])
    assert p.duration == pytest.approx(4.0)
    assert "color=c=black" in graph_of(p)          # video held black until the end


def test_audio_setting_off(tmp_path, video_h):
    p = plan_of(tmp_path, [video_h], [vmuted(video_h, 0, 1, 0), aseg(video_h, 0, 1, 0)], audio=False)
    assert "amix" not in graph_of(p) and "-an" in p.steps[0].args


# --------------------------------------------------------------------------- real runs
def export(tmp_path, srcs, segs, name="out", **kw):
    job = ExportJob(sources_of(*srcs), segs, settings_for(tmp_path, name, **kw),
                    temp_dir=str(tmp_path / "tmp"))
    res = run_job(job)
    assert res.ok, res.failed
    return res.finished


def test_detach_and_shift_audio(tmp_path, video_h):
    segs = [vmuted(video_h, 0, 2, 0), aseg(video_h, 0, 2, 1.0)]
    out = export(tmp_path, [video_h], segs)
    v, a = read_video(out), read_audio(out)
    assert v.duration == pytest.approx(3.0, abs=0.1)      # video held on black past 2 s
    assert a.duration == pytest.approx(3.0, abs=0.1)
    assert stream_info(out)["has_audio"]
    runs = audio_runs(a)
    assert [k for k, _ in runs] == [-1, 0, 1]
    assert runs[1][1] == pytest.approx(1.0, abs=0.07) and runs[2][1] == pytest.approx(2.0, abs=0.07)
    assert audio_rms(a, 0.1, 0.9) < 0.005


def test_mix_with_unmuted_video_audio(tmp_path, video_h):
    segs = [seg(video_h, 0, 2, 0), aseg(video_h, 3, 4, 0.5)]     # k=3 tone over k=0 / k=1
    a = read_audio(export(tmp_path, [video_h], segs))
    assert a.duration == pytest.approx(2.0, abs=0.1)
    assert audio_rms(a, 0.1, 0.4) > 0.2 and audio_rms(a, 0.6, 1.4) > 0.3   # sum is louder


def test_video_longer_pads_silence(tmp_path, video_h):
    segs = [vmuted(video_h, 0, 3, 0), aseg(video_h, 0, 1, 0)]
    a = read_audio(export(tmp_path, [video_h], segs))
    assert a.duration == pytest.approx(3.0, abs=0.1)
    assert audio_rms(a, 0.1, 0.9) > 0.2 and audio_rms(a, 1.3, 2.9) < 0.005


# --------------------------------------------------------------------- extract audio
def _duration(path):
    return float(ffmpeg.run_capture(["-v", "error", "-show_entries", "format=duration",
                                     "-of", "default=nw=1:nk=1", path]).strip())


@pytest.mark.parametrize("fmt,codec", [("mp3", "mp3float"), ("m4a", "aac"), ("wav", "pcm_s16le")])
def test_extract_formats(tmp_path, video_h, fmt, codec):
    segs = [seg(video_h, 1, 2, 0), aseg(video_h, 3, 4, 2.0)]       # 1 s gap between
    job = ExtractAudioJob(sources_of(video_h), segs, str(tmp_path / f"snd.{fmt}"),
                          temp_dir=str(tmp_path / "tmp"))
    res = run_job(job)
    assert res.ok, res.failed
    assert res.finished == str(tmp_path / f"snd.{fmt}")
    with av.open(res.finished) as c:
        assert not c.streams.video and c.streams.audio[0].codec_context.name == codec
    assert _duration(res.finished) == pytest.approx(3.0, abs=0.15)
    a = read_audio(res.finished)
    assert audio_rms(a, 1.2, 1.8) < 0.01 and audio_rms(a, 0.2, 0.8) > 0.2
    assert not [f for f in os.listdir(tmp_path) if "partial" in f]


def test_extract_unique_name_and_muted(tmp_path, video_h):
    (tmp_path / "snd.wav").write_bytes(b"keep")
    p = build_audio_plan(sources_of(video_h), [seg(video_h, 0, 1, 0, muted=True)],
                         str(tmp_path / "snd.wav"), temp_dir=str(tmp_path / "tmp"))
    assert p.output_path == str(tmp_path / "snd (2).wav")
    res = run_job(ExtractAudioJob(sources_of(video_h), [seg(video_h, 0, 1, 0, muted=True)],
                                  str(tmp_path / "snd.wav"), temp_dir=str(tmp_path / "tmp")))
    assert res.ok and audio_rms(read_audio(res.finished), 0, 0.9) < 0.005
    assert (tmp_path / "snd.wav").read_bytes() == b"keep"


def test_extract_errors_and_cancel(tmp_path, video_h):
    with pytest.raises(MediaError):
        build_audio_plan(sources_of(video_h), [], str(tmp_path / "a.mp3"), temp_dir=str(tmp_path))
    with pytest.raises(MediaError):
        build_audio_plan(sources_of(video_h), [seg(video_h, 0, 1, 0)], str(tmp_path / "a.mp3"),
                         "ogg", temp_dir=str(tmp_path))
    job = ExtractAudioJob(sources_of(video_h), [seg(video_h, 0, 6, 0)], str(tmp_path / "c.mp3"),
                          temp_dir=str(tmp_path / "tmp"))
    job.cancel()
    res = run_job(job, cancel_when=lambda r: True)
    assert res.cancelled or res.ok
    assert not [f for f in os.listdir(tmp_path) if "partial" in f]
