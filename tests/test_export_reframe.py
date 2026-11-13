"""Vertical/square reframing and burned-in captions: planning + real FFmpeg runs."""

from __future__ import annotations

import dataclasses
import subprocess

import av
import numpy as np
import pytest
from conftest import run_job, seg, settings_for, sources_of, stream_info

from clipforge.core.captions import CaptionLine, CaptionStyle, Word
from clipforge.core.model import ExportSettings, MediaSource
from clipforge.media import MediaError, ffmpeg, probe
from clipforge.media.export import ExportJob, build_plan, can_fast_export, resolve_output_size


# ------------------------------------------------------------------------- sources
def _make(path: str, graph: str, dur: float) -> None:
    cmd = [ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-y",
           "-f", "lavfi", "-i", graph, "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "12", "-pix_fmt", "yuv420p", "-g", "30", path]
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, creationflags=ffmpeg.creation_flags())


@pytest.fixture(scope="module")
def red_blue(tmp_path_factory) -> MediaSource:
    """1920x1080, 3 s, left half red, right half blue."""
    p = str(tmp_path_factory.mktemp("rb") / "red_blue.mp4")
    _make(p, "color=c=red:s=960x1080:r=30:d=3[a];color=c=blue:s=960x1080:r=30:d=3[b];"
             "[a][b]hstack", 3)
    return probe.probe_source(p)


@pytest.fixture(scope="module")
def dark(tmp_path_factory) -> MediaSource:
    """1280x720, 4 s, dark grey."""
    p = str(tmp_path_factory.mktemp("dk") / "dark.mp4")
    _make(p, "color=c=0x202020:s=1280x720:r=30:d=4", 4)
    return probe.probe_source(p)


@pytest.fixture(scope="module")
def vert_green(tmp_path_factory) -> MediaSource:
    """540x960 vertical, 3 s, green."""
    p = str(tmp_path_factory.mktemp("vg") / "vert.mp4")
    _make(p, "color=c=green:s=540x960:r=30:d=3", 3)
    return probe.probe_source(p)


# --------------------------------------------------------------------------- helpers
def frame_rgb(path: str, t: float) -> np.ndarray:
    with av.open(path) as c:
        vs = c.streams.video[0]
        best = None
        for fr in c.decode(vs):
            if float(fr.pts * vs.time_base) > t + 1e-4:
                break
            best = fr
        return best.to_ndarray(format="rgb24")


def px(img: np.ndarray, xf: float, yf: float) -> np.ndarray:
    h, w = img.shape[:2]
    return img[int(yf * (h - 1)), int(xf * (w - 1))].astype(int)


def is_red(p) -> bool:
    return p[0] > 180 and p[1] < 70 and p[2] < 70


def is_blue(p) -> bool:
    return p[2] > 180 and p[0] < 70 and p[1] < 70


def export(tmp_path, srcs, segs, name="out", captions=(), caption_style=None, **kw):
    kw.setdefault("audio", False)
    job = ExportJob(sources_of(*srcs), segs, settings_for(tmp_path, name, **kw),
                    temp_dir=str(tmp_path / "tmp"), captions=captions,
                    caption_style=caption_style)
    res = run_job(job)
    assert res.ok, res.failed
    return res.finished


def graph_of(plan):
    a = plan.steps[0].args
    return a[a.index("-filter_complex") + 1]


def plan_of(tmp_path, srcs, segs, **kw):
    cap = kw.pop("captions", ())
    return build_plan(sources_of(*srcs), segs, settings_for(tmp_path, **kw),
                      temp_dir=str(tmp_path / "tmp"), video_encoder="libx264", captions=cap)


def S(**kw) -> ExportSettings:
    return ExportSettings(**kw)


# -------------------------------------------------------------------------- sizing
def test_canvas_sizes(red_blue, dark):
    assert resolve_output_size([red_blue], S(aspect="9:16")) == (1080, 1920)
    assert resolve_output_size([red_blue], S(aspect="4:5")) == (1080, 1350)
    assert resolve_output_size([red_blue], S(aspect="1:1")) == (1080, 1080)
    assert resolve_output_size([red_blue], S(aspect="16:9")) == (1920, 1080)
    assert resolve_output_size([dark], S(aspect="9:16")) == (720, 1280)       # source short side
    assert resolve_output_size([dark, red_blue], S(aspect="9:16")) == (1080, 1920)   # largest
    assert resolve_output_size([red_blue], S(aspect="9:16", resolution="720")) == (720, 1280)
    assert resolve_output_size([red_blue], S(aspect="1:1", resolution="1440")) == (1440, 1440)
    assert resolve_output_size([red_blue], S(aspect="9:16", resolution="99999")) == (2160, 3840)


def test_canvas_4k_source_capped():
    big = MediaSource(id="x", path="x", duration=5.0, width=3840, height=2160, fps=30)
    assert resolve_output_size([big], S(aspect="9:16")) == (1080, 1920)
    assert resolve_output_size([big], S(aspect="16:9")) == (3840, 2160)
    assert resolve_output_size([big], S()) == (3840, 2160)                  # unchanged


def test_source_aspect_unchanged(red_blue):
    assert resolve_output_size([red_blue], S()) == (1920, 1080)
    assert resolve_output_size([red_blue], S(resolution="720")) == (1280, 720)


# ------------------------------------------------------------------------ planning
def test_fast_refused_for_aspect_and_captions(red_blue, tmp_path):
    segs = [seg(red_blue, 0, 2, 0)]
    ok, why = can_fast_export(sources_of(red_blue), segs, S(aspect="9:16"))
    assert not ok and "aspect" in why
    ok, why = can_fast_export(sources_of(red_blue), segs, S(captions=True),
                              [CaptionLine(0, 1, "x")])
    assert not ok and "Captions" in why
    _, why = can_fast_export(sources_of(red_blue), segs, S(captions=False), [CaptionLine(0, 1, "x")])
    assert "Captions" not in why
    p = plan_of(tmp_path, [red_blue], segs, mode="auto", aspect="9:16")
    assert p.mode == "accurate" and (p.width, p.height) == (1080, 1920)
    with pytest.raises(MediaError):
        plan_of(tmp_path, [red_blue], segs, mode="fast", aspect="1:1")


def test_source_aspect_graph_has_no_reframe(red_blue, tmp_path):
    g = graph_of(plan_of(tmp_path, [red_blue], [seg(red_blue, 0, 2, 0)]))
    assert "crop" not in g and "gblur" not in g and "ass=" not in g


def test_plan_modes(red_blue, tmp_path):
    s = seg(red_blue, 0, 2, 0)
    s.crop_x = 0.0
    g = graph_of(plan_of(tmp_path, [red_blue], [s], aspect="9:16", reframe="fill"))
    assert "crop=606:1080:0:0" in g and "scale=1080:1920" in g
    s.crop_x = 1.0
    assert "crop=606:1080:1314:0" in graph_of(plan_of(tmp_path, [red_blue], [s], aspect="9:16"))
    s.crop_x, s.crop_x_end = 0.0, 1.0
    g = graph_of(plan_of(tmp_path, [red_blue], [s], aspect="9:16"))
    assert "x='(iw-ow)*clip(0+(1)*t/2,0,1)'" in g
    g = graph_of(plan_of(tmp_path, [red_blue], [s], aspect="9:16", reframe="blur"))
    assert "gblur" in g and "overlay" in g and "split=2" in g
    g = graph_of(plan_of(tmp_path, [red_blue], [s], aspect="9:16", reframe="fit"))
    assert "pad=1080:1920" in g and "crop" not in g


def test_plan_vertical_source_in_wide_canvas_crops_vertically(vert_green, tmp_path):
    g = graph_of(plan_of(tmp_path, [vert_green], [seg(vert_green, 0, 2, 0)], aspect="16:9"))
    assert "crop=540:302:0:329" in g                      # 540/(16/9) = 303.75 -> 302 (even, down)
    g = graph_of(plan_of(tmp_path, [vert_green], [seg(vert_green, 0, 2, 0)], aspect="9:16"))
    assert "crop" not in g                                # already the canvas shape


def test_captions_outside_range_do_nothing(dark, tmp_path):
    p = plan_of(tmp_path, [dark], [seg(dark, 0, 2, 0)],
                captions=[CaptionLine(10, 12, "far away")])
    assert "ass=" not in graph_of(p) and p.temp_files == []
    p = plan_of(tmp_path, [dark], [seg(dark, 0, 2, 0)], captions=[CaptionLine(0, 1, "x")])
    assert "ass=filename=" in graph_of(p) and any(t.endswith(".ass") for t in p.temp_files)


# -------------------------------------------------------------------- real: reframe
def test_fill_static_left_right_centre(red_blue, tmp_path):
    results = {}
    for x in (0.0, 1.0, 0.5):
        s = seg(red_blue, 0, 1.5, 0)
        s.crop_x = x
        out = export(tmp_path, [red_blue], [s], name=f"fill{x}", aspect="9:16", reframe="fill")
        info = stream_info(out)
        assert (info["width"], info["height"]) == (1080, 1920)
        results[x] = frame_rgb(out, 0.7)
    for yf in (0.1, 0.5, 0.9):
        assert is_red(px(results[0.0], 0.05, yf)) and is_red(px(results[0.0], 0.95, yf))
        assert is_blue(px(results[1.0], 0.05, yf)) and is_blue(px(results[1.0], 0.95, yf))
    mid = results[0.5]                                    # 608 px window centred on the seam
    assert is_red(px(mid, 0.02, 0.5)) and is_blue(px(mid, 0.98, 0.5))


def test_fill_pan_red_to_blue(red_blue, tmp_path):
    s = seg(red_blue, 0, 3, 0)
    s.crop_x, s.crop_x_end = 0.0, 1.0
    out = export(tmp_path, [red_blue], [s], aspect="9:16", reframe="fill")
    first, last, middle = frame_rgb(out, 0.0), frame_rgb(out, 2.9), frame_rgb(out, 1.5)
    assert is_red(px(first, 0.5, 0.5)) and is_red(px(first, 0.95, 0.5))
    assert is_blue(px(last, 0.5, 0.5)) and is_blue(px(last, 0.05, 0.5))
    assert px(middle, 0.02, 0.5)[0] > 150 and px(middle, 0.98, 0.5)[2] > 150    # mix at the half


def test_blur_mode(red_blue, tmp_path):
    out = export(tmp_path, [red_blue], [seg(red_blue, 0, 1.5, 0)], aspect="9:16", reframe="blur")
    info = stream_info(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    img = frame_rgb(out, 0.7)
    top, bottom = px(img, 0.25, 0.04), px(img, 0.75, 0.96)     # background, outside the fitted frame
    assert top.sum() > 60 and bottom.sum() > 60               # not black
    assert top[0] > top[2] and bottom[2] > bottom[0]           # red left / blue right, blurred
    assert is_red(px(img, 0.25, 0.5)) and is_blue(px(img, 0.75, 0.5))   # sharp centre strip


def test_fit_mode_letterboxes(red_blue, tmp_path):
    out = export(tmp_path, [red_blue], [seg(red_blue, 0, 1.5, 0)], aspect="9:16", reframe="fit")
    img = frame_rgb(out, 0.7)
    assert px(img, 0.5, 0.05).sum() < 40 and px(img, 0.5, 0.95).sum() < 40      # black bars
    assert is_red(px(img, 0.2, 0.5)) and is_blue(px(img, 0.8, 0.5))


@pytest.mark.parametrize("aspect,size", [("4:5", (1080, 1350)), ("1:1", (1080, 1080)),
                                         ("16:9", (1920, 1080))])
def test_other_aspect_sizes(red_blue, tmp_path, aspect, size):
    out = export(tmp_path, [red_blue], [seg(red_blue, 0, 1, 0)], aspect=aspect, resolution="1080")
    info = stream_info(out)
    assert (info["width"], info["height"]) == size


def test_mixed_sources_handled_individually(red_blue, vert_green, tmp_path):
    segs = [seg(red_blue, 0, 1, 0), seg(vert_green, 0, 1, 1)]
    segs[0].crop_x = 0.0
    out = export(tmp_path, [red_blue, vert_green], segs, aspect="9:16", reframe="fill")
    info = stream_info(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    a, b = frame_rgb(out, 0.5), frame_rgb(out, 1.5)
    assert is_red(px(a, 0.5, 0.5))
    g = px(b, 0.5, 0.5)
    assert g[1] > 80 and g[0] < 60 and g[2] < 60


def test_gap_black_with_canvas(red_blue, tmp_path):
    segs = [seg(red_blue, 0, 1, 0), seg(red_blue, 1, 2, 2.0)]
    out = export(tmp_path, [red_blue], segs, aspect="1:1")
    assert px(frame_rgb(out, 1.5), 0.5, 0.5).sum() < 40


# ------------------------------------------------------------------- real: captions
STYLE = CaptionStyle(preset="classic", font="Segoe UI Black", size_pct=8.0, position="bottom",
                     margin_pct=10, primary="#FFFFFF", outline_pct=0.0, uppercase=True,
                     highlight_words=False)


def bright(img: np.ndarray, y0: float, y1: float) -> int:
    h = img.shape[0]
    band = img[int(y0 * h):int(y1 * h)]
    return int((band.min(axis=2) > 200).sum())


def test_caption_window_and_region(dark, tmp_path):
    cap = [CaptionLine(1.0, 2.0, "Hello world")]
    out = export(tmp_path, [dark], [seg(dark, 0, 4, 0)], captions=cap, caption_style=STYLE)
    inside = frame_rgb(out, 1.5)
    assert bright(inside, 0.66, 1.0) > 800                       # text in the bottom third
    assert bright(inside, 0.0, 0.66) == 0
    for t in (0.5, 2.5, 3.5):
        assert bright(frame_rgb(out, t), 0.0, 1.0) == 0


def test_caption_position_top(dark, tmp_path):
    cap = [CaptionLine(0.0, 2.0, "Top text")]
    out = export(tmp_path, [dark], [seg(dark, 0, 2, 0)], captions=cap,
                 caption_style=dataclasses.replace(STYLE, position="top", margin_pct=8))
    img = frame_rgb(out, 1.0)
    assert bright(img, 0.0, 0.33) > 800 and bright(img, 0.33, 1.0) == 0


def test_caption_disabled_in_settings(dark, tmp_path):
    job = ExportJob(sources_of(dark), [seg(dark, 0, 2, 0)],
                    settings_for(tmp_path, "nocap", audio=False, captions=False),
                    temp_dir=str(tmp_path / "tmp"), captions=[CaptionLine(0.0, 2.0, "Hidden")],
                    caption_style=STYLE)
    res = run_job(job)
    assert res.ok and bright(frame_rgb(res.finished, 1.0), 0.0, 1.0) == 0


def test_word_highlight_moves(dark, tmp_path):
    words = [Word(0.5, 1.5, "LEFT"), Word(1.5, 2.5, "RIGHT")]
    cap = [CaptionLine(0.5, 2.5, "LEFT RIGHT", words)]
    st = CaptionStyle(preset="classic", font="Segoe UI Black", size_pct=8.0, position="middle",
                      primary="#FFFFFF", highlight="#00FF00", outline_pct=0.0, uppercase=True,
                      highlight_words=True)
    out = export(tmp_path, [dark], [seg(dark, 0, 4, 0)], captions=cap, caption_style=st)

    def green_cx(t):
        img = frame_rgb(out, t).astype(int)
        m = (img[..., 1] > 180) & (img[..., 0] < 90) & (img[..., 2] < 90)
        if m.sum() < 50:
            return None
        return float(np.nonzero(m)[1].mean()) / img.shape[1]

    assert green_cx(0.2) is None and green_cx(3.0) is None
    a, b = green_cx(1.0), green_cx(2.0)
    assert a is not None and b is not None and a < 0.5 < b


def test_captions_with_selection_shift(dark, tmp_path):
    """Selection scope: clips start at 2.0 on the timeline, so output t=0 is timeline 2.0."""
    s = seg(dark, 0, 3, 2.0)
    cap = [CaptionLine(0.5, 1.5, "Before"),               # wholly before the selection: dropped
           CaptionLine(2.5, 3.5, "During"),               # output 0.5 .. 1.5
           CaptionLine(4.5, 9.0, "Tail")]                 # clipped to the end (output 2.5 .. 3)
    out = export(tmp_path, [dark], [s], captions=cap, caption_style=STYLE)
    assert bright(frame_rgb(out, 0.2), 0.0, 1.0) == 0
    assert bright(frame_rgb(out, 1.0), 0.66, 1.0) > 500
    assert bright(frame_rgb(out, 2.0), 0.0, 1.0) == 0
    assert bright(frame_rgb(out, 2.8), 0.66, 1.0) > 500


def test_captions_on_reframed_canvas(red_blue, tmp_path):
    cap = [CaptionLine(0.2, 1.4, "Vertical")]
    out = export(tmp_path, [red_blue], [seg(red_blue, 0, 1.5, 0)], aspect="9:16", reframe="blur",
                 captions=cap, caption_style=STYLE)
    info = stream_info(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert bright(frame_rgb(out, 0.8), 0.7, 0.95) > 500


def test_caption_path_with_spaces_unicode_and_apostrophe(dark, tmp_path):
    weird = tmp_path / "tëmp d'ir sp ace"
    job = ExportJob(sources_of(dark), [seg(dark, 0, 2, 0)],
                    settings_for(tmp_path, "weird", audio=False),
                    temp_dir=str(weird), captions=[CaptionLine(0.2, 1.8, "Path test")],
                    caption_style=STYLE)
    res = run_job(job)
    assert res.ok, res.failed
    assert bright(frame_rgb(res.finished, 1.0), 0.66, 1.0) > 500
    assert not list(weird.glob("*"))                      # temp files cleaned up
