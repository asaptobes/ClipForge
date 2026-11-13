"""Multi-track export: overlay compositing plan + one real FFmpeg export."""

from __future__ import annotations

import subprocess
import uuid

import pytest
from conftest import (luma_at_pixel, read_video, run_job, settings_for, sources_of, stream_info)

from clipforge.core.model import Segment
from clipforge.media import MediaError, ffmpeg, probe
from clipforge.media.export import ExportJob, build_plan, can_fast_export


def clip(src, a, b, start, layer=0, **kw) -> Segment:
    return Segment(uuid.uuid4().hex[:12], src.id, a, b, start, layer=layer, **kw)


def aclip(src, a, b, start) -> Segment:
    return Segment(uuid.uuid4().hex[:12], src.id, a, b, start, track="audio")


def plan_of(tmp_path, srcs, segs, **kw):
    return build_plan(sources_of(*srcs), segs, settings_for(tmp_path, **kw),
                      temp_dir=str(tmp_path / "tmp"), video_encoder="libx264")


def graph_of(plan):
    a = plan.steps[0].args
    return a[a.index("-filter_complex") + 1]


@pytest.fixture(scope="module")
def white_clip(media_dir):
    """320x180 solid white, 25 fps, 2 s, with a 440 Hz-ish tone."""
    path = str(media_dir / "white_overlay.mp4")
    cmd = [ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-y",
           "-f", "lavfi", "-i", "color=c=white:s=320x180:r=25:d=2",
           "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path]
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, creationflags=ffmpeg.creation_flags())
    return probe.probe_source(path)


# ------------------------------------------------------------------------- planning
def test_two_tracks_graph(tmp_path, video_h, white_clip):
    base = clip(video_h, 0, 4, 0)
    ov = clip(white_clip, 0, 2, 1.0, layer=1, pos_x=0.25, pos_y=-0.25, scale=0.4)
    plan = plan_of(tmp_path, [video_h, white_clip], [base, ov])
    g = graph_of(plan)
    assert plan.mode == "accurate" and plan.duration == pytest.approx(4.0)
    assert "concat=n=1:v=1:a=1[base]" in g
    assert "setpts=PTS+1/TB[o0]" in g
    # 0.4 * 1280 = 512 wide, 16:9 -> 288 high; centre (0.75, 0.25) of 1280x720
    assert "scale=512:288" in g
    assert "overlay=x=704:y=36:" in g
    assert "enable='between(t,1,3)'" in g
    assert "[outv]" in g.split("overlay=")[-1]
    assert plan.steps[0].args.count("-i") == 2
    assert not any("shell" in a for a in plan.steps[0].args)


def test_single_track_graph_unchanged(tmp_path, video_h):
    plan = plan_of(tmp_path, [video_h], [clip(video_h, 0, 2, 0)])
    g = graph_of(plan)
    assert "overlay" not in g and "[base]" not in g
    assert "concat=n=1:v=1:a=1[outv][outa]" in g


def test_gap_under_overlay_is_black(tmp_path, video_h, white_clip):
    base = clip(video_h, 0, 1, 0)
    ov = clip(white_clip, 0, 2, 2.0, layer=1)
    plan = plan_of(tmp_path, [video_h, white_clip], [base, ov])
    g = graph_of(plan)
    assert "color=c=black" in g                       # V1 gap 1..2
    assert "concat=n=2" in g or "concat=n=3" in g
    assert plan.duration == pytest.approx(4.0)        # overlay ends the timeline


def test_overlay_before_v1_starts(tmp_path, video_h, white_clip):
    plan = plan_of(tmp_path, [video_h, white_clip],
                   [clip(video_h, 0, 1, 2.0), clip(white_clip, 0, 1, 0, layer=1)])
    assert plan.duration == pytest.approx(3.0)
    assert "color=c=black" in graph_of(plan)


def test_opacity(tmp_path, video_h, white_clip):
    half = clip(white_clip, 0, 1, 0, layer=1, opacity=0.5)
    full = clip(white_clip, 0, 1, 1, layer=1)
    g = graph_of(plan_of(tmp_path, [video_h, white_clip], [clip(video_h, 0, 2, 0), half, full]))
    assert "format=yuva420p,colorchannelmixer=aa=0.5" in g
    assert g.count("colorchannelmixer") == 1


def test_layers_stack_bottom_to_top(tmp_path, video_h, white_clip):
    v2 = clip(white_clip, 0, 1, 0, layer=1)
    v3 = clip(video_h, 0, 1, 0, layer=2)
    g = graph_of(plan_of(tmp_path, [video_h, white_clip],
                         [clip(video_h, 0, 2, 0), v3, v2]))       # given out of order
    first, second = g.index("[base][o0]overlay"), g.index("[m0][o1]overlay")
    assert first < second
    # o0 is the V2 clip (white 320x180 -> 1280x720), o1 the V3 clip
    assert "[1:v:0]" in g.split("[o0]")[0].split(";")[-1]


def test_audio_mix_and_mute(tmp_path, video_h, white_clip):
    base = clip(video_h, 0, 3, 0)
    ov = clip(white_clip, 0, 2, 1.0, layer=1)
    g = graph_of(plan_of(tmp_path, [video_h, white_clip], [base, ov]))
    assert "adelay=48000S:all=1[ao0]" in g
    assert "amix=inputs=2:duration=longest:normalize=0" in g and "[va][ao0]" in g

    muted = clip(white_clip, 0, 2, 1.0, layer=1, muted=True)
    g = graph_of(plan_of(tmp_path, [video_h, white_clip], [base, muted]))
    assert "amix" not in g and "[ao0]" not in g

    lane = aclip(video_h, 0, 2, 0.5)
    g = graph_of(plan_of(tmp_path, [video_h, white_clip], [base, ov, lane]))
    assert "amix=inputs=3" in g and "[ac0]" in g and "[ao0]" in g


def test_audio_off_has_no_audio_graph(tmp_path, video_h, white_clip):
    g = graph_of(plan_of(tmp_path, [video_h, white_clip],
                         [clip(video_h, 0, 2, 0), clip(white_clip, 0, 1, 0, layer=1)], audio=False))
    assert "amix" not in g and "[outa]" not in g


def test_overlay_disables_fast_mode(tmp_path, video_h, white_clip):
    segs = [clip(video_h, 0, 2, 0), clip(white_clip, 0, 1, 0, layer=1)]
    ok, why = can_fast_export(sources_of(video_h, white_clip), segs, settings_for(tmp_path, mode="auto"))
    assert not ok and "overlay" in why
    plan = build_plan(sources_of(video_h, white_clip), segs, settings_for(tmp_path, mode="auto"),
                      temp_dir=str(tmp_path / "t"), video_encoder="libx264")
    assert plan.mode == "accurate"
    with pytest.raises(MediaError):
        build_plan(sources_of(video_h, white_clip), segs, settings_for(tmp_path, mode="fast"),
                   temp_dir=str(tmp_path / "t"), video_encoder="libx264")


def test_overlay_only_timeline(tmp_path, white_clip):
    plan = plan_of(tmp_path, [white_clip], [clip(white_clip, 0, 2, 0, layer=1)])
    assert plan.duration == pytest.approx(2.0)
    assert (plan.width, plan.height) == (320, 180)


# --------------------------------------------------------------------------- real run
def test_real_two_track_export(tmp_path, video_h, white_clip):
    base = clip(video_h, 0, 4, 0)
    ov = clip(white_clip, 0, 2, 1.0, layer=1, pos_x=0.25, pos_y=0.0, scale=0.4)
    st = settings_for(tmp_path, "mt")
    job = ExportJob(sources_of(video_h, white_clip), [base, ov], st, temp_dir=str(tmp_path / "tmp"))
    res = run_job(job)
    assert res.ok, res.failed
    info = read_video(res.finished)
    assert (info.width, info.height) == (1280, 720)
    assert info.duration == pytest.approx(4.0, abs=0.15)
    streams = stream_info(res.finished)
    assert streams["has_audio"]
    # overlay covers x in [0.75 +- 0.2] of the width, y centred
    inside = luma_at_pixel(res.finished, 2.0, 0.75, 0.5)
    base_side = luma_at_pixel(res.finished, 2.0, 0.25, 0.5)
    after = luma_at_pixel(res.finished, 3.5, 0.75, 0.5)
    before = luma_at_pixel(res.finished, 0.5, 0.75, 0.5)
    assert inside > 220                                   # white overlay
    assert base_side < 120                                # base ramp only
    assert abs(after - before) < 120 and after < 140      # overlay gone after t=3
    assert abs(inside - base_side) > 80
