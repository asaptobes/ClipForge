"""Command-generation tests for clipforge.media.export (no encoding is run here)."""

from __future__ import annotations

import os

import pytest
from conftest import seg, settings_for, sources_of

from clipforge.core.model import ExportSettings, MediaSource
from clipforge.media import MediaError
from clipforge.media.export import (build_plan, can_fast_export, output_path_for,
                                    partial_path, resolve_output_size)


def plan_for(tmp_path, srcs, segs, encoder="libx264", temp_dir=None, **kw):
    st = settings_for(tmp_path, **kw)
    return build_plan(sources_of(*srcs), segs, st, temp_dir=str(temp_dir or tmp_path / "tmp"),
                      video_encoder=encoder)


def args_of(plan, i=0):
    return plan.steps[i].args


def opt(args, flag):
    """Value following ``flag`` in an argument list."""
    return args[args.index(flag) + 1]


def graph_of(plan):
    return opt(args_of(plan), "-filter_complex")


def fake(w, h, fps=30.0, **kw) -> MediaSource:
    return MediaSource(id=f"f{w}x{h}", path="x.mp4", duration=10, width=w, height=h, fps=fps, **kw)


# ----------------------------------------------------------------------- sizes
class TestResolveOutputSize:
    def test_source_keeps_first_source(self):
        assert resolve_output_size([fake(1920, 1080), fake(720, 1280)], ExportSettings()) == (1920, 1080)

    def test_source_made_even(self):
        assert resolve_output_size([fake(1281, 721)], ExportSettings()) == (1280, 720)

    def test_short_side_landscape(self):
        assert resolve_output_size([fake(3840, 2160)], ExportSettings(resolution="1080")) == (1920, 1080)

    def test_short_side_portrait(self):
        assert resolve_output_size([fake(2160, 3840)], ExportSettings(resolution="1080")) == (1080, 1920)

    def test_never_upscales(self):
        assert resolve_output_size([fake(1280, 720)], ExportSettings(resolution="1080")) == (1280, 720)
        assert resolve_output_size([fake(1280, 720)], ExportSettings(resolution="720")) == (1280, 720)

    def test_odd_result_is_even(self):
        w, h = resolve_output_size([fake(1366, 768)], ExportSettings(resolution="480"))
        assert h == 480 and w % 2 == 0 and abs(w - 1366 * 480 / 768) <= 1

    def test_empty_sources(self):
        with pytest.raises(MediaError):
            resolve_output_size([], ExportSettings())


def test_paths():
    st = ExportSettings(filename='a:b?"c"', folder="D:/out")
    assert os.path.basename(output_path_for(st)) == "a_b__c_.mp4"
    assert output_path_for(ExportSettings(filename="clip.mp4", folder="D:/o")) == os.path.join("D:/o", "clip.mp4")
    assert partial_path(os.path.join("D:/o", "clip.mp4")) == os.path.join("D:/o", "clip.partial.mp4")


# ---------------------------------------------------------------- accurate plans
class TestAccuratePlan:
    def test_multi_cut(self, tmp_path, video_h, video_25):
        segs = [seg(video_h, 4.2, 5.5, 0), seg(video_25, 1.0, 2.0, 1.3), seg(video_h, 0.0, 1.0, 2.3)]
        p = plan_for(tmp_path, [video_h, video_25], segs)
        assert p.mode == "accurate" and len(p.steps) == 1
        a = args_of(p)
        assert a.count("-i") == 3
        assert a[:6] == ["-ss", "4.2", "-t", "1.3", "-i", video_h.path]
        g = graph_of(p)
        assert "concat=n=3:v=1:a=1[outv][outa]" in g
        assert "trim=duration=1.3,setpts=PTS-STARTPTS" in g
        assert "atrim=duration=1.3,asetpts=PTS-STARTPTS,aresample=48000:async=1" in g
        assert "format=yuv420p" in g
        assert p.duration == pytest.approx(3.3) and p.steps[0].duration == pytest.approx(3.3)
        assert (p.width, p.height, p.fps) == (1280, 720, 30.0)
        assert a[-1] == partial_path(p.output_path)
        assert p.output_path == str(tmp_path / "out.mp4")

    def test_no_seek_for_zero_start(self, tmp_path, video_h):
        p = plan_for(tmp_path, [video_h], [seg(video_h, 0, 1, 0)])
        assert args_of(p)[0] == "-t"

    def test_encoder_settings_h264(self, tmp_path, video_h):
        for quality, crf in (("high", "18"), ("medium", "23"), ("low", "28")):
            a = args_of(plan_for(tmp_path, [video_h], [seg(video_h, 0, 1, 0)], quality=quality))
            assert opt(a, "-c:v") == "libx264" and opt(a, "-crf") == crf
            assert opt(a, "-preset") == "medium" and opt(a, "-pix_fmt") == "yuv420p"
            assert opt(a, "-c:a") == "aac" and opt(a, "-b:a") == "192k"
            assert opt(a, "-ar") == "48000" and opt(a, "-ac") == "2"
            assert opt(a, "-movflags") == "+faststart" and "-tag:v" not in a

    def test_h265(self, tmp_path, video_h):
        a = args_of(plan_for(tmp_path, [video_h], [seg(video_h, 0, 1, 0)], encoder="libx265", codec="h265"))
        assert opt(a, "-c:v") == "libx265" and opt(a, "-tag:v") == "hvc1" and opt(a, "-crf") == "18"

    def test_nvenc(self, tmp_path, video_h):
        for enc, codec in (("h264_nvenc", "h264"), ("hevc_nvenc", "h265")):
            for quality, cq in (("high", "19"), ("medium", "24"), ("low", "29")):
                a = args_of(plan_for(tmp_path, [video_h], [seg(video_h, 0, 1, 0)], encoder=enc,
                                     codec=codec, quality=quality))
                assert opt(a, "-c:v") == enc and opt(a, "-rc") == "vbr" and opt(a, "-cq") == cq
                assert opt(a, "-preset") == "p5" and opt(a, "-b:v") == "0" and "-crf" not in a
                assert ("-tag:v" in a) == (codec == "h265")

    def test_encoder_defaults_to_pick(self, tmp_path, video_h):
        st = settings_for(tmp_path, hw_accel="off", codec="h265")
        p = build_plan(sources_of(video_h), [seg(video_h, 0, 1, 0)], st, temp_dir=str(tmp_path))
        assert p.video_encoder == "libx265"

    def test_audio_off(self, tmp_path, video_h):
        p = plan_for(tmp_path, [video_h], [seg(video_h, 0, 1, 0), seg(video_h, 2, 3, 1)], audio=False)
        a = args_of(p)
        assert "-an" in a and "-c:a" not in a and "[outa]" not in " ".join(a)
        assert "concat=n=2:v=1:a=0[outv]" in graph_of(p)
        assert "atrim" not in graph_of(p) and "anullsrc" not in graph_of(p)

    def test_gap_becomes_black_and_silence(self, tmp_path, video_h):
        segs = [seg(video_h, 1, 2, 0), seg(video_h, 3, 4, 1.5)]       # 0.5 s gap
        p = plan_for(tmp_path, [video_h], segs)
        g = graph_of(p)
        assert "color=c=black:s=1280x720:r=30:d=0.5" in g
        assert "anullsrc=r=48000:cl=stereo:d=0.5" in g
        assert "concat=n=3:v=1:a=1" in g
        assert p.duration == pytest.approx(2.5)

    def test_leading_offset_ignored_and_tiny_gap_merged(self, tmp_path, video_h):
        segs = [seg(video_h, 1, 2, 5.0), seg(video_h, 3, 4, 6.005)]
        p = plan_for(tmp_path, [video_h], segs)
        assert "color=" not in graph_of(p) and p.duration == pytest.approx(2.0)

    def test_segments_sorted_by_timeline(self, tmp_path, video_h):
        late, early = seg(video_h, 3, 4, 1), seg(video_h, 1, 2, 0)
        a = args_of(plan_for(tmp_path, [video_h], [late, early]))
        assert a[:2] == ["-ss", "1"] and a[6:8] == ["-ss", "3"]

    def test_muted_segment_uses_silence(self, tmp_path, video_h):
        segs = [seg(video_h, 0, 1, 0), seg(video_h, 2, 3, 1, muted=True), seg(video_h, 4, 5, 2)]
        g = graph_of(plan_for(tmp_path, [video_h], segs))
        assert g.count("asetpts=") == 2 and g.count("anullsrc=") == 1
        assert "[1:a:0]" not in g and "anullsrc=r=48000:cl=stereo:d=1,aformat" in g

    def test_source_without_audio_uses_silence(self, tmp_path, video_h, video_noaudio):
        segs = [seg(video_h, 0, 1, 0), seg(video_noaudio, 0, 1, 1)]
        g = graph_of(plan_for(tmp_path, [video_h, video_noaudio], segs))
        assert g.count("asetpts=") == 1 and "anullsrc=r=48000:cl=stereo:d=1," in g
        assert "[1:a:0]" not in g

    def test_selection_scope_only_given_segments(self, tmp_path, video_h):
        all_segs = [seg(video_h, 0, 1, 0), seg(video_h, 2, 3, 1), seg(video_h, 4, 5, 2)]
        p = plan_for(tmp_path, [video_h], all_segs[1:2], scope="selection")
        assert args_of(p).count("-i") == 1 and p.duration == pytest.approx(1.0)
        assert opt(args_of(p), "-ss") == "2"

    def test_resolution_and_fps_options(self, tmp_path, video_h, video_25):
        p = plan_for(tmp_path, [video_h], [seg(video_h, 0, 1, 0)], resolution="360", fps="24")
        assert (p.width, p.height, p.fps) == (640, 360, 24.0)
        assert "scale=640:360:force_original_aspect_ratio=decrease" in graph_of(p)
        assert "fps=24" in graph_of(p) and opt(args_of(p), "-r") == "24"
        p = plan_for(tmp_path, [video_h], [seg(video_h, 0, 1, 0)], fps="29.97")
        assert "fps=30000/1001" in graph_of(p)
        p = plan_for(tmp_path, [video_25], [seg(video_25, 0, 1, 0)])
        assert p.fps == 25.0 and "fps=25," in graph_of(p)

    def test_mixed_sources_normalised_to_first(self, tmp_path, video_h, video_v, video_25):
        segs = [seg(video_h, 0, 1, 0), seg(video_v, 0, 1, 1), seg(video_25, 0, 1, 2)]
        p = plan_for(tmp_path, [video_h, video_v, video_25], segs)
        g = graph_of(p)
        assert (p.width, p.height) == (1280, 720)
        assert g.count("pad=1280:720:(ow-iw)/2:(oh-ih)/2") == 2       # vertical + 640x360
        assert g.count("setsar=1") == 3
        assert any("letterbox" in w for w in p.warnings)
        assert any("frame rate" in w for w in p.warnings)

    def test_vertical_stays_vertical(self, tmp_path, video_v, video_h):
        p = plan_for(tmp_path, [video_v, video_h], [seg(video_v, 0, 1, 0), seg(video_h, 0, 1, 1)])
        assert (p.width, p.height) == (720, 1280)
        p = plan_for(tmp_path, [video_v], [seg(video_v, 0, 1, 0)], resolution="360")
        assert (p.width, p.height) == (360, 640)

    def test_huge_graph_goes_to_file(self, tmp_path, video_h):
        segs = [seg(video_h, 0, 0.5, i * 0.5) for i in range(150)]
        p = plan_for(tmp_path, [video_h], segs)
        a = args_of(p)
        assert "-filter_complex" not in a and "-/filter_complex" in a
        script = opt(a, "-/filter_complex")
        assert script in p.temp_files and os.path.isfile(script)
        with open(script, encoding="utf-8") as fh:
            assert "concat=n=150" in fh.read()

    def test_errors(self, tmp_path, video_h):
        with pytest.raises(MediaError):
            plan_for(tmp_path, [video_h], [])
        ghost = MediaSource(id="ghost", path=str(tmp_path / "nope.mp4"), duration=5)
        with pytest.raises(MediaError, match="missing"):
            plan_for(tmp_path, [ghost], [seg(ghost, 0, 1, 0)])
        with pytest.raises(MediaError):
            plan_for(tmp_path, [], [seg(video_h, 0, 1, 0)])


# --------------------------------------------------------------- fast vs accurate
@pytest.fixture
def kf_h(video_h):
    from clipforge.media import probe
    if not video_h.keyframes:
        probe.load_keyframes(video_h)
    return video_h


class TestFastDecision:
    def test_keyframes_detected(self, kf_h):
        assert kf_h.keyframes[:6] == pytest.approx([0, 1, 2, 3, 4, 5], abs=1e-3)

    def test_auto_picks_fast_on_keyframes(self, tmp_path, kf_h):
        segs = [seg(kf_h, 1, 3, 0), seg(kf_h, 4, 6, 2)]
        p = plan_for(tmp_path, [kf_h], segs, mode="auto")
        assert p.mode == "fast" and p.video_encoder == "copy" and "keyframe" in p.reason.lower()
        assert len(p.steps) == 5 and p.duration == pytest.approx(4.0)
        first = args_of(p, 0)
        assert opt(first, "-ss") == "1" and opt(first, "-c:v") == "copy" and "-an" in first
        # video is stopped one B-frame delay + half a frame early (cut by dts)
        assert float(opt(first, "-t")) == pytest.approx(2 - 2 / 30 - 1 / 60, abs=0.01)
        # the second segment ends at the end of the file: nothing to stop early for
        assert float(opt(args_of(p, 1), "-t")) > 2.0
        join = args_of(p, 2)
        assert opt(join, "-f") == "concat" and opt(join, "-c") == "copy" and "0:a" not in " ".join(join)
        audio = args_of(p, 3)                              # audio: decoded once, exact lengths
        assert "concat=n=2:v=0:a=1[outa]" in opt(audio, "-filter_complex") and opt(audio, "-c:a") == "aac"
        mux = args_of(p, 4)
        assert mux.count("-i") == 2 and opt(mux, "-c") == "copy" and mux[-1] == partial_path(p.output_path)
        assert len(p.temp_files) == 5                      # 2 parts, list, joined video, audio
        lst = [t for t in p.temp_files if t.endswith("list.txt")][0]
        with open(lst, encoding="utf-8") as fh:
            text = fh.read()
        assert text.count("file '") == 2 and "\\" not in text

    def test_concat_list_escapes_quotes(self, tmp_path, kf_h):
        p = plan_for(tmp_path, [kf_h], [seg(kf_h, 1, 2, 0)], mode="fast", temp_dir=tmp_path / "it's")
        lst = [t for t in p.temp_files if t.endswith("list.txt")][0]
        with open(lst, encoding="utf-8") as fh:
            assert "it'\\''s" in fh.read()

    def test_fast_audio_off_drops_audio(self, tmp_path, kf_h):
        p = plan_for(tmp_path, [kf_h], [seg(kf_h, 2, 3, 0)], mode="fast", audio=False)
        assert len(p.steps) == 2 and args_of(p, 1)[-1] == partial_path(p.output_path)
        assert "audio" not in " ".join(st.description for st in p.steps).lower()

    def test_muted_ok_when_audio_off(self, kf_h):
        ok, _ = can_fast_export(sources_of(kf_h), [seg(kf_h, 2, 3, 0, muted=True)],
                                ExportSettings(audio=False))
        assert ok

    def test_cut_within_half_frame_of_keyframe_ok(self, kf_h):
        ok, _ = can_fast_export(sources_of(kf_h), [seg(kf_h, 2.01, 3, 0)], ExportSettings())
        assert ok

    def test_auto_falls_back_when_cut_off_keyframe(self, tmp_path, kf_h):
        p = plan_for(tmp_path, [kf_h], [seg(kf_h, 1.5, 3, 0)], mode="auto")
        assert p.mode == "accurate" and "keyframe" in p.reason.lower()

    def test_fast_refused_with_reason(self, tmp_path, kf_h, video_25):
        cases = {
            "keyframe": ([seg(kf_h, 1.5, 3, 0)], {}),
            "end of a cut": ([seg(kf_h, 1, 2.5, 0)], {}),
            "gaps": ([seg(kf_h, 1, 2, 0), seg(kf_h, 3, 4, 2)], {}),
            "muted": ([seg(kf_h, 1, 2, 0, muted=True)], {}),
            "resolution": ([seg(kf_h, 1, 2, 0)], {"resolution": "360"}),
            "frame rate": ([seg(kf_h, 1, 2, 0)], {"fps": "24"}),
            "H.265": ([seg(kf_h, 1, 2, 0)], {"codec": "h265"}),
            "different": ([seg(kf_h, 1, 2, 0), seg(video_25, 1, 2, 1)], {}),
        }
        for word, (segs, extra) in cases.items():
            with pytest.raises(MediaError) as ei:
                plan_for(tmp_path, [kf_h, video_25], segs, mode="fast", **extra)
            assert word.lower() in str(ei.value).lower(), (word, str(ei.value))

    def test_accurate_mode_never_probes_keyframes(self, tmp_path, video_25):
        video_25.keyframes = []
        p = plan_for(tmp_path, [video_25], [seg(video_25, 1, 2, 0)], mode="accurate")
        assert p.mode == "accurate" and video_25.keyframes == []
