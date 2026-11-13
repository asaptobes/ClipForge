"""Export pipeline: pure planning (testable command generation) + a QObject job that
runs the plan on a background thread.

Two modes:

* **accurate** - one FFmpeg run. Every timeline piece becomes its own input
  (``-ss/-t`` for a fast, frame-accurate seek) and filter chain
  (trim/setpts, atrim/asetpts, scale+pad, fps, format); gaps / muted / audio-less
  pieces are filled with ``color`` / ``anullsrc``; the pieces are joined with the
  ``concat`` filter and encoded once.
* **fast** - each piece is stream-copied into a temp file, then the temp files are
  joined with the concat demuxer. Only used when that gives frame-exact results
  (see :func:`can_fast_export`).

The last step of a plan writes to :func:`partial_path` of ``plan.output_path``;
:class:`ExportJob` renames it into place once the result has been verified.
"""

from __future__ import annotations

import bisect
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Collection, Optional, Sequence

from PySide6.QtCore import QObject, Signal

from .. import paths
from ..core.captions import CaptionLine, CaptionStyle, Word, style_for_preset
from ..core.model import ExportSettings, MediaSource, Segment
from ..workers import Cancelled, CancelToken
from . import MediaError, ass, ffmpeg, probe

log = logging.getLogger(__name__)

AUDIO_RATE = 48000
_GAP_EPS = 1e-3
_CRF = {"high": 18, "medium": 23, "low": 28}
_CQ = {"high": 19, "medium": 24, "low": 29}
_QSV_Q = {"high": 21, "medium": 26, "low": 31}
_CONTAINER_EXT = {"mp4": "mp4", "mov": "mov", "mkv": "mkv"}
#: Above this total argument length the filter graph goes into a file (Windows limit is 32767).
_MAX_INLINE_ARGS = 20000
_COPY_WEIGHT = 0.05                # stream copy is ~20x faster than encoding
_AUDIO_WEIGHT = 0.25


@dataclass
class ExportStep:
    description: str
    args: list[str]                 # ffmpeg arguments (without the executable)
    duration: float = 0.0           # expected output seconds (clamps the progress position)
    weight: float = 1.0             # share of the overall progress bar per output second


@dataclass
class ExportPlan:
    mode: str                       # "fast" | "accurate" - what will actually be used
    reason: str                     # one sentence shown to the user explaining the choice
    output_path: str
    steps: list[ExportStep] = field(default_factory=list)
    temp_files: list[str] = field(default_factory=list)   # removed afterwards, always
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_encoder: str = ""
    duration: float = 0.0           # expected duration of the finished file
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------ helpers
def _even_down(x: float) -> int:
    return max(2, int(x) // 2 * 2)


def _even_near(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def _ordered(segments: Sequence[Segment]) -> list[Segment]:
    return sorted((s for s in segments if s.duration > 0), key=lambda s: s.timeline_start)


def _fmt(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def _fps_expr(fps: float) -> str:
    for n in (24, 30, 48, 60, 120):                     # NTSC rates are n*1000/1001
        if abs(fps - n * 1000 / 1001) < 0.006:
            return f"{n * 1000}/1001"
    fr = Fraction(fps).limit_denominator(1001)
    return str(fr.numerator) if fr.denominator == 1 else f"{fr.numerator}/{fr.denominator}"


def _extension(settings: ExportSettings) -> str:
    return _CONTAINER_EXT.get((settings.container or "mp4").lower(), "mp4")


def _safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip()).strip(" .")
    return name or "export"


def output_path_for(settings: ExportSettings) -> str:
    """Destination file for ``settings`` (folder + filename + container extension)."""
    ext = _extension(settings)
    name = _safe_name(settings.filename)
    if name.lower().endswith("." + ext):
        name = name[: -(len(ext) + 1)] or "export"
    folder = settings.folder or str(paths.default_export_dir())
    return os.path.join(folder, f"{name}.{ext}")


def partial_path(output_path: str) -> str:
    """``dir/name.mp4`` → ``dir/name.partial.mp4`` (what is written until success)."""
    root, ext = os.path.splitext(output_path)
    return f"{root}.partial{ext}"


def _output_fps(sources: Sequence[MediaSource], settings: ExportSettings) -> float:
    if (settings.fps or "source").lower() != "source":
        try:
            value = float(settings.fps)
            if 1 <= value <= 240:
                return value
        except ValueError:
            pass
    fps = sources[0].fps if sources else 30.0
    return fps if 1 <= fps <= 240 else 30.0


_ASPECTS = {"9:16": (9, 16), "4:5": (4, 5), "1:1": (1, 1), "16:9": (16, 9)}
_MAX_SHORT_SIDE = 2160
_SOCIAL_SHORT_SIDE = 1080


def _aspect_ratio(settings: ExportSettings) -> Optional[tuple[int, int]]:
    """``(w, h)`` ratio of a chosen canvas aspect, None for "source" (or unknown)."""
    return _ASPECTS.get((settings.aspect or "source").strip().lower())


def _canvas_size(sources: Sequence[MediaSource], settings: ExportSettings,
                 ratio: tuple[int, int]) -> tuple[int, int]:
    """Canvas for a fixed aspect. Short side = the chosen resolution when it is a number
    (up to 2160, user's choice even if it upscales); with "source" it is the largest short
    side among ``sources``, capped at 1080 for the portrait/square social aspects (a 4K
    clip still gives a 1080x1920 file) and at 2160 for 16:9. Dimensions are even."""
    res = (settings.resolution or "source").strip().lower()
    short = 0
    try:
        short = int(float(res))
    except ValueError:
        pass
    if short <= 0:
        shorts = [min(s.width, s.height) for s in sources if s.width > 0 and s.height > 0]
        cap = _MAX_SHORT_SIDE if ratio[0] > ratio[1] else _SOCIAL_SHORT_SIDE
        short = min(max(shorts) if shorts else 1080, cap)
    short = max(2, min(short, _MAX_SHORT_SIDE))
    rw, rh = ratio
    if rw > rh:                                            # landscape: height is the short side
        return _even_near(short * rw / rh), _even_near(short)
    return _even_near(short), _even_near(short * rh / rw)


def resolve_output_size(sources: Sequence[MediaSource], settings: ExportSettings) -> tuple[int, int]:
    """Even ``(width, height)`` for the export. "source" keeps the first segment's
    source size; a number is the *short side* (so "1080" → 1920x1080 for 16:9 and
    1080x1920 for 9:16), aspect ratio preserved, never upscaling past the source.
    With ``settings.aspect`` other than "source" the canvas has that aspect ratio instead
    (see :func:`_canvas_size` for how its size is chosen)."""
    if not sources:
        raise MediaError("There is nothing to export.")
    ratio = _aspect_ratio(settings)
    if ratio is not None:
        return _canvas_size(sources, settings, ratio)
    w, h = sources[0].width, sources[0].height
    if w <= 0 or h <= 0:
        w, h = 1920, 1080
    res = (settings.resolution or "source").strip().lower()
    if res == "source":
        return _even_down(w), _even_down(h)
    try:
        short = int(float(res))
    except ValueError:
        return _even_down(w), _even_down(h)
    src_short = min(w, h)
    if short <= 0 or short >= src_short:                    # never upscale
        return _even_down(w), _even_down(h)
    scale = short / src_short
    if w <= h:
        return _even_down(short), _even_near(h * scale)
    return _even_near(w * scale), _even_down(short)


# ------------------------------------------------------------------ fast-export check
_MP4_VIDEO = ("h264", "hevc")


def _source_signature(s: MediaSource) -> tuple:
    return (s.video_codec, s.width, s.height, round(s.fps, 3), s.pix_fmt, s.rotation,
            s.has_audio, s.audio_codec, s.sample_rate, s.channels)


def _nearest_keyframe(keyframes: Sequence[float], t: float) -> Optional[float]:
    if not keyframes:
        return None
    i = bisect.bisect_left(keyframes, t)
    cands = [keyframes[j] for j in (i - 1, i) if 0 <= j < len(keyframes)]
    return min(cands, key=lambda k: abs(k - t))


def _keyframes_of(source: MediaSource) -> list[float]:
    if not source.keyframes:
        probe.load_keyframes(source)
    return source.keyframes


_delay_cache: dict[tuple, float] = {}


def _video_delay(src: MediaSource) -> float:
    """Reorder delay (``pts - dts`` of the first video packet, i.e. B-frame depth in
    seconds). Stream copy cuts by *dts*, so the end of a copied piece has to be placed
    this much earlier to stop exactly before the next GOP's first frame."""
    try:
        st = os.stat(src.path)
        key = (src.path, st.st_size, st.st_mtime_ns)
    except OSError as exc:
        raise MediaError(f"File not found: {src.path}") from exc
    if key in _delay_cache:
        return _delay_cache[key]
    out = ffmpeg.run_capture(
        ["-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#4",
         "-show_entries", "packet=pts_time,dts_time", "-of", "csv=p=0", src.path], timeout=60)
    delay = None
    for line in out.splitlines():
        parts = line.strip().split(",")
        try:
            pts, dts = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            continue
        delay = max(0.0, pts - dts)
        break
    if delay is None:
        raise MediaError("The frame order of the video could not be read.")
    _delay_cache[key] = delay
    return delay


def _cut_is_clean(src: MediaSource, seg: Segment) -> Optional[str]:
    """None when both ends of ``seg`` fall on keyframes (the end may also be the end of
    the file); otherwise the reason."""
    kf = _keyframes_of(src)
    half = 0.5 * src.frame_duration + 1e-6
    near = _nearest_keyframe(kf, seg.source_in)
    if near is None or abs(near - seg.source_in) > half:
        return ("A cut does not fall on a keyframe, so the video has to be re-encoded to "
                "cut exactly there.")
    if seg.source_out < src.duration - 1.5 * src.frame_duration:
        near = _nearest_keyframe(kf, seg.source_out)
        if near is None or abs(near - seg.source_out) > half:
            return ("The end of a cut does not fall on a keyframe, so the video has to be "
                    "re-encoded to cut exactly there.")
    return None


def can_fast_export(sources: dict[str, MediaSource], segments: Sequence[Segment],
                    settings: ExportSettings,
                    captions: Sequence[CaptionLine] = ()) -> tuple[bool, str]:
    """Whether stream copy gives *accurate* results; second item says why not.

    Requires: no gaps, no muted segments (unless audio is off for all), all segments
    from sources with identical codec/size/fps/pix_fmt/audio layout, output settings
    all "source", and every cut landing on a keyframe (each ``source_in`` - and each
    ``source_out`` unless it is the end of the file - within half a frame of a keyframe
    from ``probe.load_keyframes``). The end matters too: stream copy cuts by decode
    time, so a mid-GOP end would leave a few frames of the following material."""
    if _aspect_ratio(settings) is not None:
        return False, ("A different picture shape (aspect ratio) was chosen, which needs the "
                       "video to be re-framed and re-encoded.")
    if settings.captions and captions:
        return False, "Captions are burned into the picture, which needs the video to be re-encoded."
    if any(s.is_overlay for s in segments):
        return False, ("The timeline has clips on overlay tracks, which have to be composited "
                       "over the base track, so the export is re-encoded.")
    if any(s.is_audio for s in segments):
        return False, ("The timeline has detached audio clips, which have to be mixed into "
                       "the sound, so the export is re-encoded.")
    segs = _ordered(segments)
    if not segs:
        return False, "There is nothing to export."
    try:
        used = [sources[s.source_id] for s in segs]
    except KeyError:
        return False, "A clip refers to a missing source."
    first = used[0]

    if (settings.resolution or "source").lower() != "source":
        return False, "Fast export keeps the original resolution, but a different one was chosen."
    if (settings.fps or "source").lower() != "source":
        return False, "Fast export keeps the original frame rate, but a different one was chosen."
    want = "hevc" if (settings.codec or "h264").lower() in ("h265", "hevc") else "h264"
    if first.video_codec != want:
        return False, (f"The source is {first.video_codec or 'unknown'} video, "
                       f"not {'H.265' if want == 'hevc' else 'H.264'}, so it must be re-encoded.")
    ext = _extension(settings)
    if ext in ("mp4", "mov"):
        if first.video_codec not in _MP4_VIDEO:
            return False, f"The video cannot be copied into a .{ext} file."
    for s in used[1:]:
        if _source_signature(s) != _source_signature(first):
            return False, "The clips come from sources with different formats, sizes or frame rates."

    cursor = segs[0].timeline_start
    for seg in segs:
        if seg.timeline_start - cursor > _GAP_EPS:
            return False, "The timeline has gaps, which need black video and silence."
        cursor = seg.timeline_end
    if settings.audio and first.has_audio and any(s.muted for s in segs):
        return False, "Some clips are muted, which needs the audio to be re-encoded."

    for seg, src in zip(segs, used):
        try:
            why = _cut_is_clean(src, seg)
            if why is None:
                _video_delay(src)
        except MediaError:
            return False, "The keyframes of the source could not be read."
        if why:
            return False, why
    return True, "All cuts are on keyframes."


# --------------------------------------------------------------------------- planning
def _video_encoder_args(encoder: str, codec: str, quality: str) -> list[str]:
    q = quality if quality in _CRF else "high"
    args = ["-c:v", encoder]
    name = encoder.lower()
    if name in ("libx264", "libx265"):
        args += ["-preset", "medium", "-crf", str(_CRF[q])]
        if name == "libx265":
            args += ["-x265-params", "log-level=error"]
    elif name.endswith("_nvenc"):
        args += ["-preset", "p5", "-rc", "vbr", "-cq", str(_CQ[q]), "-b:v", "0"]
    elif name.endswith("_qsv"):
        args += ["-preset", "medium", "-global_quality", str(_QSV_Q[q])]
    elif name.endswith("_amf"):
        qp = str(_QSV_Q[q])
        args += ["-quality", "quality", "-rc", "cqp", "-qp_i", qp, "-qp_p", qp]
    args += ["-pix_fmt", "yuv420p"]
    if codec == "h265":
        args += ["-tag:v", "hvc1"]
    return args


def _check_source_file(src: MediaSource) -> None:
    if not os.path.isfile(src.path):
        raise MediaError(f"The source file \"{os.path.basename(src.path)}\" is missing "
                         f"or was moved: {src.path}")


def _norm_codec(settings: ExportSettings) -> str:
    return "h265" if (settings.codec or "").lower() in ("h265", "hevc") else "h264"


def build_plan(sources: dict[str, MediaSource], segments: Sequence[Segment],
               settings: ExportSettings, *, temp_dir: str,
               video_encoder: Optional[str] = None,
               captions: Sequence[CaptionLine] = (),
               caption_style: Optional[CaptionStyle] = None,
               hidden_layers: Collection[int] = ()) -> ExportPlan:
    """Turn timeline segments into FFmpeg commands.

    ``segments`` are in timeline order with their real ``timeline_start`` values
    (for a selection export pass only the selected ones; leading offset is ignored,
    gaps *between* them are kept as black/silence).
    mode "auto" → fast when ``can_fast_export`` allows, otherwise accurate;
    mode "fast" when not possible → raises ``MediaError`` explaining why (never a
    silently wrong cut). Accurate mode decodes with frame-exact trim/atrim +
    setpts/asetpts per segment, normalises size/fps/sample format, fills gaps and
    missing/muted audio with black/silence, concatenates with the concat filter and
    encodes once (``-movflags +faststart``, yuv420p, AAC 48 kHz stereo).
    ``video_encoder`` overrides ``ffmpeg.pick_video_encoder`` (used by tests).
    ``captions`` (timeline-time lines) are burned in with ``caption_style`` (default: the
    "pop" preset) when ``settings.captions`` is on; this forces accurate mode.
    ``segments`` may hold every lane (V1, overlay tracks ``layer >= 1``, detached audio):
    overlay clips are composited over V1 per the multi-track contract and force accurate mode.
    ``hidden_layers`` lists switched-off video tracks (``Timeline.track_hidden``): their clips
    are left out entirely (a hidden V1 gives a black base under the overlays), and the graph
    is unchanged when nothing is hidden.
    Raises ``MediaError`` for an empty timeline or missing source files."""
    hidden = set(hidden_layers)
    if hidden:
        segments = [s for s in segments if s.is_audio or s.layer not in hidden]
        if not segments:
            raise MediaError("There is nothing to export: every video track is hidden.")
    everything = _ordered(segments)
    segs = [s for s in everything if not s.is_audio and not s.is_overlay]
    asegs = [s for s in everything if s.is_audio]
    osegs = sorted((s for s in everything if s.is_overlay),
                   key=lambda s: (s.layer, s.timeline_start))
    if not everything:
        raise MediaError("There is nothing to export.")
    used: list[MediaSource] = []
    aused: list[MediaSource] = []
    oused: list[MediaSource] = []
    for lane, bucket in ((segs, used), (asegs, aused), (osegs, oused)):
        for s in lane:
            src = sources.get(s.source_id)
            if src is None:
                raise MediaError("A clip refers to a source that is no longer in the project.")
            _check_source_file(src)
            bucket.append(src)

    mode = (settings.mode or "auto").lower()
    if mode not in ("auto", "fast", "accurate"):
        mode = "auto"
    uid = uuid.uuid4().hex[:8]
    out_path = output_path_for(settings)
    ctx = dict(sources=sources, segs=segs, used=used, settings=settings, temp_dir=temp_dir,
               uid=uid, out_path=out_path)
    acc = dict(asegs=asegs, aused=aused, osegs=osegs, oused=oused, video_encoder=video_encoder,
               captions=list(captions) if settings.captions else [],
               caption_style=caption_style)

    if mode == "accurate":
        return _plan_accurate(**ctx, reason="Frame-accurate export (re-encoded) was selected.", **acc)
    ok, why = can_fast_export(sources, everything, settings, captions)
    if mode == "fast":
        if not ok:
            raise MediaError(f"Fast export is not possible: {why}", detail=why)
        return _plan_fast(**ctx)
    if ok:
        return _plan_fast(**ctx)
    return _plan_accurate(**ctx, reason=f"Re-encoding for frame-accurate cuts: {why}", **acc)


def _escape_concat(path: str) -> str:
    """Path as written inside single quotes in a concat-demuxer list file."""
    return path.replace("\\", "/").replace("'", "'\\''")


def _audio_chain(idx: int, label: str, d: str) -> str:
    """Filter chain: decode audio of input ``idx``, cut to ``d`` seconds exactly,
    48 kHz stereo float - gives a bit-exact length so concatenation never drifts."""
    return (f"[{idx}:a:0]atrim=duration={d},asetpts=PTS-STARTPTS,"
            f"aresample={AUDIO_RATE}:async=1:first_pts=0,"
            f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo,"
            f"apad=whole_dur={d},atrim=duration={d}[{label}]")


def _audio_clip_chain(idx: int, label: str, d: str, delay_s: float) -> str:
    """Like :func:`_audio_chain` for a detached audio clip, then delayed by ``delay_s``
    seconds (whole samples) so it starts at its place on the timeline."""
    chain = (f"[{idx}:a:0]atrim=duration={d},asetpts=PTS-STARTPTS,"
             f"aresample={AUDIO_RATE}:async=1:first_pts=0,"
             f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo")
    samples = int(round(delay_s * AUDIO_RATE))
    if samples > 0:
        chain += f",adelay={samples}S:all=1"
    return f"{chain}[{label}]"


def _plan_fast(*, sources, segs, used, settings, temp_dir, uid, out_path) -> ExportPlan:
    """Video: stream-copied per segment and joined with the concat demuxer.
    Audio: decoded and encoded once for the whole timeline (cheap) because copied AAC
    cannot be cut or joined without a small timing error per cut that adds up."""
    first = used[0]
    with_audio = bool(settings.audio and first.has_audio)
    final = partial_path(out_path)
    plan = ExportPlan(
        mode="fast", output_path=out_path,
        reason="Cuts are on keyframes, so the video is copied without re-encoding "
               "(fastest, no quality loss).",
        width=_even_down(first.width), height=_even_down(first.height), fps=first.fps,
        video_encoder="copy", duration=sum(s.duration for s in segs))
    temps: list[str] = []
    parts: list[str] = []
    for i, (seg, src) in enumerate(zip(segs, used)):
        kf = _nearest_keyframe(_keyframes_of(src), seg.source_in)
        start = seg.source_in if kf is None else kf
        tmp = os.path.join(temp_dir, f"cf_{uid}_part{i:03d}.mp4")
        parts.append(tmp)
        at_end = seg.source_out >= src.duration - 1.5 * src.frame_duration
        # video is cut by decode time: stop before the first packet of the next GOP
        t_video = (seg.duration + 1.0 if at_end
                   else seg.duration - _video_delay(src) - 0.5 * src.frame_duration)
        args = ["-ss", _fmt(start)] if start > 0 else []
        args += ["-t", _fmt(t_video), "-i", src.path, "-map", "0:v:0", "-an", "-c:v", "copy",
                 "-map_metadata", "-1", tmp]
        plan.steps.append(ExportStep(f"Copying clip {i + 1} of {len(segs)}", args,
                                     seg.duration, weight=_COPY_WEIGHT))
    temps += parts
    list_path = os.path.join(temp_dir, f"cf_{uid}_list.txt")
    temps.append(list_path)
    _write_text(list_path, "".join(f"file '{_escape_concat(t)}'\n" for t in parts))

    hevc = first.video_codec == "hevc"
    extra = ["-tag:v", "hvc1"] if hevc else []
    faststart = ["-movflags", "+faststart"] if _extension(settings) in ("mp4", "mov") else []
    joined = os.path.join(temp_dir, f"cf_{uid}_video.mp4") if with_audio else final
    if with_audio:
        temps.append(joined)
    plan.steps.append(ExportStep(
        "Joining clips",
        ["-f", "concat", "-safe", "0", "-i", list_path, "-map", "0:v:0", "-c", "copy"]
        + extra + ([] if with_audio else faststart) + [joined],
        0.0, weight=_COPY_WEIGHT))

    if with_audio:
        inputs: list[str] = []
        chains: list[str] = []
        for i, (seg, src) in enumerate(zip(segs, used)):
            if seg.source_in > 0:
                inputs += ["-ss", _fmt(seg.source_in)]
            inputs += ["-t", _fmt(seg.duration), "-i", src.path]
            chains.append(_audio_chain(i, f"a{i}", _fmt(seg.duration)))
        graph = ";".join(chains + [
            "".join(f"[a{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=0:a=1[outa]"])
        audio = os.path.join(temp_dir, f"cf_{uid}_audio.m4a")
        temps.append(audio)
        graph_args = ["-filter_complex", graph]
        if len(graph) > _MAX_INLINE_ARGS // 2:
            gpath = os.path.join(temp_dir, f"cf_{uid}_filter.txt")
            _write_text(gpath, graph)
            temps.append(gpath)
            graph_args = ["-/filter_complex", gpath]
        plan.steps.append(ExportStep(
            "Preparing audio",
            inputs + graph_args + ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k",
                                   "-ar", str(AUDIO_RATE), "-ac", "2", audio],
            plan.duration, weight=_AUDIO_WEIGHT))
        plan.steps.append(ExportStep(
            "Joining audio and video",
            ["-i", joined, "-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c", "copy"]
            + extra + faststart + [final],
            0.0, weight=_COPY_WEIGHT))
    plan.temp_files = temps
    return plan


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _escape_level(text: str, specials: str) -> str:
    text = text.replace("\\", "\\\\")
    for ch in specials:
        text = text.replace(ch, "\\" + ch)
    return text


def _filter_path(path: str) -> str:
    """``path`` as the value of a filter option inside a filter graph given on the command
    line (no shell): forward slashes, then escaped for the option parser (``\\ ' :``) and
    again for the graph parser (``\\ ' [ ] , ;``) - the classic ``C\\\\:/dir/file`` form."""
    p = path.replace("\\", "/")
    return _escape_level(_escape_level(p, "':"), "'[],;")


def _shift_captions(lines: Sequence[CaptionLine], origin: float, total: float) -> list[CaptionLine]:
    """Captions moved from timeline time to output time (``-origin``) and clipped to
    ``[0, total]``; lines (and words) wholly outside are dropped."""
    out: list[CaptionLine] = []
    for ln in sorted(lines, key=lambda x: x.start):
        s, e = max(0.0, ln.start - origin), min(total, ln.end - origin)
        if e - s < 0.02:
            continue
        words: list[Word] = []
        for w in ln.words:
            ws, we = max(s, w.start - origin), min(e, w.end - origin)
            if we > ws or (ws < e and w.end - w.start <= 0):
                words.append(Word(ws, max(we, ws), w.text))
        out.append(CaptionLine(s, e, ln.text, words))
    return out


_filters_cache: Optional[str] = None


def _ffmpeg_has_filter(name: str) -> bool:
    global _filters_cache
    if _filters_cache is None:
        _filters_cache = ffmpeg.run_capture(["-hide_banner", "-filters"], tool="ffmpeg", timeout=30)
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", _filters_cache, re.M) is not None


def _fonts_dir() -> str:
    d = os.path.join(os.environ.get("WINDIR") or r"C:\Windows", "Fonts")
    return d if os.path.isdir(d) else ""


def _font_installed(family: str) -> bool:
    """Best-effort check that ``family`` is an installed Windows font (registry list);
    True when it cannot be told."""
    try:
        import winreg
    except ImportError:
        return True
    want = family.strip().lower()
    for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
                      (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")):
        try:
            with winreg.OpenKey(hive, sub) as key:
                i = 0
                while True:
                    try:
                        name, _v, _t = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    i += 1
                    base = re.sub(r"\s*\((TrueType|OpenType)\)\s*$", "", name, flags=re.I).lower()
                    if base == want or any(part.strip() == want for part in base.split(" & ")):
                        return True
        except OSError:
            continue
    return False


def _reframe_chain(idx: int, label: str, d: str, seg: Segment, src: MediaSource,
                   width: int, height: int, mode: str, fps_e: str) -> str:
    """Filter chain(s) turning input ``idx`` into a ``width`` x ``height`` canvas picture
    (modes "fill", "blur", "fit"), ending in ``[label]``."""
    head = f"trim=duration={d},setpts=PTS-STARTPTS"
    tail = f"setsar=1,fps={fps_e},format=yuv420p"
    sw, sh = _even_down(src.width), _even_down(src.height)
    src_in = f"[{idx}:v:0]"
    if src.width <= 0 or src.height <= 0 or mode == "fit":
        return (f"{src_in}{head},scale={width}:{height}:force_original_aspect_ratio=decrease:"
                f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"{tail}[{label}]")
    canvas = width / height
    if abs(sw / sh - canvas) < 0.01:                       # already the canvas shape
        return f"{src_in}{head},scale={width}:{height},{tail}[{label}]"
    if mode == "blur":
        bw, bh = _even_near(width / 6), _even_near(height / 6)
        return (
            f"{src_in}{head},fps={fps_e},split=2[bs{label}][fs{label}];"
            f"[bs{label}]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
            f"crop={bw}:{bh},gblur=sigma=5:steps=2,eq=brightness=-0.12:saturation=0.9,"
            f"scale={width}:{height}:flags=bilinear[bb{label}];"
            f"[fs{label}]scale={width}:{height}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2[ff{label}];"
            f"[bb{label}][ff{label}]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1,"
            f"format=yuv420p[{label}]")
    # fill: crop to the canvas shape, then scale
    if sw / sh > canvas:                                   # wider: crop sideways (pan-able)
        cw, ch = min(sw, _even_down(sh * canvas)), sh
        a, b = seg.crop_x_at(seg.source_in), seg.crop_x_at(seg.source_out)
        if abs(a - b) < 1e-4 or seg.duration <= 0:
            crop = f"crop={cw}:{ch}:{int(round((sw - cw) * a))}:0"
        else:
            crop = (f"crop={cw}:{ch}:x='(iw-ow)*clip({_fmt(a)}+({_fmt(b - a)})*t/"
                    f"{_fmt(seg.duration)},0,1)':y=0")
    else:                                                  # taller: crop vertically, centred
        cw, ch = sw, min(sh, _even_down(sw / canvas))
        crop = f"crop={cw}:{ch}:0:{int(round((sh - ch) / 2))}"
    return f"{src_in}{head},{crop},scale={width}:{height},{tail}[{label}]"


def _overlay_size(o: Segment, src: MediaSource, width: int, height: int) -> tuple[int, int]:
    """Rendered ``(w, h)`` of an overlay clip: ``scale * canvas width``, source aspect kept."""
    ow = _even_near(max(0.05, o.scale) * width)
    aspect = src.width / src.height if src.width > 0 and src.height > 0 else width / height
    return ow, _even_near(ow / aspect)


def _overlay_xy(o: Segment, src: MediaSource, width: int, height: int) -> tuple[int, int]:
    """Top-left corner of an overlay clip so its centre sits at the contract position."""
    ow, oh = _overlay_size(o, src, width, height)
    return (int(round(width * (0.5 + o.pos_x) - ow / 2)),
            int(round(height * (0.5 + o.pos_y) - oh / 2)))


def _overlay_chain(idx: int, label: str, o: Segment, src: MediaSource, width: int, height: int,
                   fps_e: str, shift: float) -> str:
    """Filter chain turning input ``idx`` into a scaled, faded overlay picture whose
    timestamps start at ``shift`` seconds (its place on the output timeline)."""
    ow, oh = _overlay_size(o, src, width, height)
    opacity = min(1.0, max(0.0, o.opacity))
    chain = (f"[{idx}:v:0]trim=duration={_fmt(o.duration)},setpts=PTS-STARTPTS,"
             f"fps={fps_e},scale={ow}:{oh},setsar=1,")
    if opacity < 0.999:
        chain += f"format=yuva420p,colorchannelmixer=aa={_fmt(opacity)},"
    else:
        chain += "format=yuv420p,"
    return f"{chain}setpts=PTS+{_fmt(shift)}/TB[{label}]"


def _plan_accurate(*, sources, segs, used, settings, temp_dir, uid, out_path, reason,
                   video_encoder, asegs=(), aused=(), osegs=(), oused=(), captions=(),
                   caption_style=None) -> ExportPlan:
    # picture format comes from V1, else the lowest overlay track, else the audio
    ref = used or list(oused) or list(aused)
    width, height = resolve_output_size(ref, settings)
    fps = _output_fps(ref, settings)
    fps_e = _fps_expr(fps)
    codec = _norm_codec(settings)
    encoder = video_encoder or ffmpeg.pick_video_encoder(settings.codec, settings.hw_accel)
    with_audio = bool(settings.audio)
    plan = ExportPlan(mode="accurate", reason=reason, output_path=out_path, width=width,
                      height=height, fps=fps, video_encoder=encoder)

    reframing = _aspect_ratio(settings) is not None
    reframe = (settings.reframe or "fill").lower()
    if reframe not in ("fill", "blur", "fit"):
        reframe = "fill"
    out_ar = width / height
    mixed = {round(s.width / max(1, s.height), 2) for s in used}
    if (not reframing or reframe == "fit") and any(abs(a - round(out_ar, 2)) > 0.02 for a in mixed):
        plan.warnings.append("Clips with a different aspect ratio are letterboxed to fit the "
                             "output size.")
    if len({round(s.fps, 2) for s in used}) > 1 and (settings.fps or "source") == "source":
        plan.warnings.append("The clips have different frame rates; all are converted to "
                             f"{fps:g} fps.")

    inputs: list[str] = []
    chains: list[str] = []
    labels: list[str] = []
    # Audio clips that actually make sound (not muted, source has an audio stream).
    sounding = ([(a, src) for a, src in zip(asegs, aused) if not a.muted and src.has_audio]
                if with_audio else [])
    everyone = list(segs) + list(asegs) + list(osegs)
    origin = min(s.timeline_start for s in everyone)
    timeline_end = max(s.timeline_end for s in everyone)
    cursor = origin
    total = 0.0
    piece = 0

    def add_gap(dur: float) -> None:
        nonlocal piece, total
        i = piece
        piece += 1
        d = _fmt(dur)
        chains.append(f"color=c=black:s={width}x{height}:r={fps_e}:d={d},format=yuv420p,"
                      f"setsar=1[v{i}]")
        labels.append(f"[v{i}]")
        if with_audio:
            chains.append(f"anullsrc=r={AUDIO_RATE}:cl=stereo:d={d},"
                          f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:"
                          f"channel_layouts=stereo[a{i}]")
            labels.append(f"[a{i}]")
        total += dur

    for seg, src in zip(segs, used):
        gap = seg.timeline_start - cursor
        if gap > max(_GAP_EPS, 0.5 / fps):
            add_gap(gap)
        cursor = seg.timeline_end
        i = piece
        piece += 1
        dur = seg.duration
        d = _fmt(dur)
        idx = sum(1 for a in inputs if a == "-i")
        if seg.source_in > 0:
            inputs += ["-ss", _fmt(seg.source_in)]
        inputs += ["-t", d, "-i", src.path]

        if reframing:
            chains.append(_reframe_chain(idx, f"v{i}", d, seg, src, width, height, reframe, fps_e))
        else:
            vf = [f"trim=duration={d}", "setpts=PTS-STARTPTS"]
            sw, sh = _even_down(src.width) if src.width else width, _even_down(src.height) if src.height else height
            if (sw, sh) != (width, height):
                vf.append(f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                          f"force_divisible_by=2")
                vf.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
            vf += ["setsar=1", f"fps={fps_e}", "format=yuv420p"]
            chains.append(f"[{idx}:v:0]{','.join(vf)}[v{i}]")
        labels.append(f"[v{i}]")

        if with_audio:
            if src.has_audio and not seg.muted:
                chains.append(_audio_chain(idx, f"a{i}", d))
            else:
                chains.append(f"anullsrc=r={AUDIO_RATE}:cl=stereo:d={d},"
                              f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:"
                              f"channel_layouts=stereo[a{i}]")
            labels.append(f"[a{i}]")
        total += dur

    if asegs or osegs:                       # audio / overlays ending the timeline later
        tail = timeline_end - cursor
        if tail > max(_GAP_EPS, 0.5 / fps):
            add_gap(tail)

    n = piece
    base_v = "[base]" if osegs else "[outv]"
    ov_chains: list[str] = []
    ov_audio: list[tuple[Segment, int]] = []
    prev = "[base]"
    for k, (o, src) in enumerate(zip(osegs, oused)):
        idx = sum(1 for x in inputs if x == "-i")
        if o.source_in > 0:
            inputs += ["-ss", _fmt(o.source_in)]
        inputs += ["-t", _fmt(o.duration), "-i", src.path]
        t0 = o.timeline_start - origin
        ov_chains.append(_overlay_chain(idx, f"o{k}", o, src, width, height, fps_e, t0))
        out = "[outv]" if k == len(osegs) - 1 else f"[m{k}]"
        ox, oy = _overlay_xy(o, src, width, height)
        ov_chains.append(f"{prev}[o{k}]overlay=x={ox}:y={oy}:eof_action=pass:"
                         f"enable='between(t,{_fmt(t0)},{_fmt(t0 + o.duration)})'{out}")
        prev = out
        if with_audio and src.has_audio and not o.muted:
            ov_audio.append((o, idx))
    mix_count = len(sounding) + len(ov_audio)
    vlabel = "[va]" if mix_count else "[outa]"
    concat = (f"{''.join(labels)}concat=n={n}:v=1:a={1 if with_audio else 0}{base_v}"
              + (vlabel if with_audio else ""))
    tail_chains: list[str] = []
    if mix_count:
        mix_in = [vlabel]
        for k, (a, src) in enumerate(sounding):
            idx = sum(1 for x in inputs if x == "-i")
            if a.source_in > 0:
                inputs += ["-ss", _fmt(a.source_in)]
            inputs += ["-t", _fmt(a.duration), "-i", src.path]
            tail_chains.append(_audio_clip_chain(idx, f"ac{k}", _fmt(a.duration),
                                                 a.timeline_start - origin))
            mix_in.append(f"[ac{k}]")
        for k, (o, idx) in enumerate(ov_audio):
            tail_chains.append(_audio_clip_chain(idx, f"ao{k}", _fmt(o.duration),
                                                 o.timeline_start - origin))
            mix_in.append(f"[ao{k}]")
        td = _fmt(total)
        tail_chains.append(
            f"{''.join(mix_in)}amix=inputs={len(mix_in)}:duration=longest:normalize=0,"
            f"alimiter=limit=0.97,apad=whole_dur={td},atrim=duration={td},"
            f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo[outa]")
    temps: list[str] = []
    video_label = "[outv]"
    caption_chain: list[str] = []
    shifted = _shift_captions(captions, origin, total) if (settings.captions and captions) else []
    if shifted:
        style = caption_style or style_for_preset("pop")
        if not _ffmpeg_has_filter("ass"):
            raise MediaError("Captions cannot be burned in: this FFmpeg build has no subtitle "
                             "renderer (the \"ass\" filter).")
        if not _font_installed(style.font):
            plan.warnings.append(f"The caption font \"{style.font}\" is not installed, so a "
                                 "substitute font is used.")
        ass_path = os.path.join(temp_dir, f"cf_{uid}_captions.ass")
        temps.append(ass_path)
        _write_text(ass_path, ass.build_ass(shifted, style, width, height))
        opts = f"filename={_filter_path(ass_path)}"
        fonts = _fonts_dir()
        if fonts:
            opts += f":fontsdir={_filter_path(fonts)}"
        caption_chain = [f"[outv]ass={opts}[outvc]"]
        video_label = "[outvc]"
    graph = ";".join(chains + [concat] + ov_chains + caption_chain + tail_chains)

    out_args = ["-map", video_label]
    if with_audio:
        out_args += ["-map", "[outa]"]
    out_args += _video_encoder_args(encoder, codec, settings.quality)
    out_args += ["-fps_mode", "cfr", "-r", fps_e]
    if with_audio:
        out_args += ["-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-ac", "2"]
    else:
        out_args += ["-an"]
    if _extension(settings) in ("mp4", "mov"):
        out_args += ["-movflags", "+faststart"]
    out_args += ["-max_muxing_queue_size", "4096", partial_path(out_path)]

    if len(graph) + sum(len(a) + 1 for a in inputs + out_args) > _MAX_INLINE_ARGS:
        gpath = os.path.join(temp_dir, f"cf_{uid}_filter.txt")
        _write_text(gpath, graph)
        temps.append(gpath)
        graph_args = ["-/filter_complex", gpath]
    else:
        graph_args = ["-filter_complex", graph]
    plan.temp_files = temps
    plan.duration = total
    plan.steps.append(ExportStep("Encoding", inputs + graph_args + out_args, total))
    return plan


# ---------------------------------------------------------------------------- the job
def _remove(path: str) -> None:
    for attempt in range(6):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except OSError:
            time.sleep(0.15 * (attempt + 1))
    log.warning("Could not remove temporary file %s", path)


def _clock(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class ExportJob(QObject):
    """Runs an :class:`ExportPlan` in the background.

    Writes to a temporary ``*.partial.mp4`` next to the destination and renames on
    success, so a failed/cancelled export never leaves a broken output file. If a
    hardware encoder fails at run time the job retries once with the CPU encoder.
    Temp files are removed in every outcome."""

    progress = Signal(float, str)   # 0..1, status text ("Encoding… 42%  ·  3.1x  ·  0:12 left")
    finished = Signal(str)          # final output path
    failed = Signal(str)            # user-presentable message
    cancelled = Signal()

    def __init__(self, sources: dict[str, MediaSource], segments: Sequence[Segment],
                 settings: ExportSettings, *, temp_dir: str,
                 captions: Sequence[CaptionLine] = (),
                 caption_style: Optional[CaptionStyle] = None,
                 hidden_layers: Collection[int] = (),
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._hidden_layers = sorted(set(hidden_layers))
        self._captions = [c.copy() for c in captions]
        self._caption_style = (CaptionStyle.from_dict(caption_style.to_dict())
                               if caption_style is not None else None)
        self._sources = dict(sources)
        self._segments = [s.copy() for s in segments]
        self._settings = ExportSettings.from_dict(settings.to_dict())
        self._temp_dir = temp_dir
        self._cancel = CancelToken()
        self._thread: Optional[threading.Thread] = None
        self._temps: list[str] = []
        self._partial = ""
        self._t0 = 0.0
        self._last_emit = 0.0
        self._last_pct = -1

    # -------------------------------------------------------------------- control
    def start(self) -> None:
        if self.is_running():
            return
        self._cancel = CancelToken()
        self._thread = threading.Thread(target=self._run, name="clipforge-export", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.cancel()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until the worker thread ends (handy for tests/shutdown)."""
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.is_running()

    # ----------------------------------------------------------------- worker side
    def _run(self) -> None:
        outcome = "failed"
        message = ""
        final = ""
        try:
            os.makedirs(self._temp_dir, exist_ok=True)
            self._t0 = time.monotonic()
            plan = build_plan(self._sources, self._segments, self._settings,
                              temp_dir=self._temp_dir, captions=self._captions,
                              caption_style=self._caption_style,
                              hidden_layers=self._hidden_layers)
            self._track(plan)
            os.makedirs(os.path.dirname(plan.output_path) or ".", exist_ok=True)
            try:
                self._execute(plan)
            except MediaError as exc:
                if not ffmpeg.is_hardware_encoder(plan.video_encoder):
                    raise
                log.warning("Hardware encoder %s failed (%s); retrying on the CPU",
                            plan.video_encoder, exc.detail or exc)
                self._cancel.raise_if_cancelled()
                self._discard_outputs()
                self._emit_progress(0.0, "Hardware encoder failed - retrying on the CPU…", force=True)
                self._t0 = time.monotonic()
                plan = build_plan(self._sources, self._segments, self._settings,
                                  temp_dir=self._temp_dir,
                                  video_encoder=ffmpeg.cpu_encoder(self._settings.codec),
                                  captions=self._captions, caption_style=self._caption_style,
                                  hidden_layers=self._hidden_layers)
                self._track(plan)
                self._execute(plan)
            self._cancel.raise_if_cancelled()
            self._verify(plan)
            self._cancel.raise_if_cancelled()
            self._emit_progress(1.0, "Finishing…", force=True)
            os.replace(self._partial, plan.output_path)
            final, outcome = plan.output_path, "finished"
        except Cancelled:
            outcome = "cancelled"
        except MediaError as exc:
            message = str(exc) or "The export failed."
        except OSError as exc:
            log.exception("Export I/O error")
            message = f"The export could not be written: {exc.strerror or exc}"
        except Exception as exc:  # noqa: BLE001 - never let the thread die silently
            log.exception("Unexpected export error")
            message = f"The export failed unexpectedly: {exc}"
        finally:
            self._discard_outputs(keep_partial=False)
            for t in self._temps:
                _remove(t)
        if outcome == "finished":
            self.finished.emit(final)
        elif outcome == "cancelled":
            self.cancelled.emit()
        else:
            self.failed.emit(message)

    def _track(self, plan: ExportPlan) -> None:
        self._temps.extend(plan.temp_files)
        self._partial = partial_path(plan.output_path)

    def _discard_outputs(self, keep_partial: bool = False) -> None:
        if self._partial and not keep_partial:
            _remove(self._partial)

    def _execute(self, plan: ExportPlan) -> None:
        total = sum(s.duration * s.weight for s in plan.steps) or plan.duration or 1.0
        done = 0.0
        verb = "Copying" if plan.mode == "fast" else "Encoding"
        started = time.monotonic()
        for step in plan.steps:
            self._cancel.raise_if_cancelled()

            def on_pos(pos: float, base: float = done, dur: float = step.duration,
                       w: float = step.weight) -> None:
                cur = base + min(max(pos, 0.0), dur if dur > 0 else pos) * w
                frac = min(0.99, cur / total)
                text = f"{verb}… {int(frac * 100)}%"
                elapsed = time.monotonic() - started
                if elapsed > 1.0 and cur > 0:
                    speed = cur / elapsed
                    text += f"  ·  {speed:.1f}x"
                    if speed > 0:
                        text += f"  ·  {_clock((total - cur) / speed)} left"
                self._emit_progress(frac, text)

            ffmpeg.run(step.args, cancel=self._cancel, on_progress=on_pos)
            done += step.duration * step.weight

    def _emit_progress(self, frac: float, text: str, force: bool = False) -> None:
        now = time.monotonic()
        pct = int(frac * 100)
        if not force and pct == self._last_pct and now - self._last_emit < 0.5:
            return
        self._last_pct, self._last_emit = pct, now
        self.progress.emit(float(frac), text)

    def _verify(self, plan: ExportPlan) -> None:
        if not os.path.isfile(self._partial):
            raise MediaError("The export did not produce a file.")
        try:
            info = probe.probe_source(self._partial)
        except MediaError as exc:
            raise MediaError("The exported file could not be read back, so it was discarded.",
                             detail=exc.detail) from exc
        tol = max(0.1, 2.0 / (plan.fps if plan.fps > 0 else 30.0))
        if abs(info.duration - plan.duration) > tol:
            raise MediaError(
                f"The exported video is {info.duration:.2f} s long instead of the expected "
                f"{plan.duration:.2f} s, so it was discarded.")
