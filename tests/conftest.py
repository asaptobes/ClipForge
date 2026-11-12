"""Shared test fixtures: synthetic videos with a verifiable time signal.

Every synthetic video encodes *time* into its content:

* video: luma ramps linearly from 16 to 216 over the whole clip, so the brightness of a
  decoded frame tells which **source** second it came from (:func:`src_time_from_luma`)
* audio: a sine whose frequency steps once per second, ``300 + 150*k`` Hz for second
  ``k`` (:func:`audio_runs`), so the audio can be matched to source time and compared
  with the picture to prove A/V sync.

The helpers decode with PyAV, never with the code under test.
"""

from __future__ import annotations

import os
import subprocess
import uuid

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import av  # noqa: E402

from clipforge.core.model import ExportSettings, MediaSource, Segment  # noqa: E402
from clipforge.media import ffmpeg, probe  # noqa: E402

LUMA_LO, LUMA_SPAN = 16, 200


# ----------------------------------------------------------------- generating media
def _make_video(path: str, *, w: int, h: int, fps: int, dur: float,
                audio_rate: int = 0, channels: int = 2) -> None:
    sw, sh = (64, 36) if w >= h else (36, 64)
    vf = (f"color=c=black:s={sw}x{sh}:r={fps}:d={dur},format=yuv420p,"
          f"geq=lum='{LUMA_LO}+{LUMA_SPAN}*T/{dur}':cb=128:cr=128,"
          f"scale={w}:{h}:flags=neighbor,format=yuv420p")
    cmd = [ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-y",
           "-f", "lavfi", "-i", vf]
    if audio_rate:
        af = f"aevalsrc='0.5*sin(2*PI*(300+150*floor(t))*t)':s={audio_rate}:d={dur}"
        cmd += ["-f", "lavfi", "-i", af]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
            "-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0"]
    if audio_rate:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", str(audio_rate), "-ac", str(channels)]
    cmd += ["-movflags", "+faststart", path]
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, creationflags=ffmpeg.creation_flags())


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("media")


def _source(media_dir, name, **kw) -> MediaSource:
    path = str(media_dir / name)
    _make_video(path, **kw)
    return probe.probe_source(path)


@pytest.fixture(scope="session")
def video_h(media_dir) -> MediaSource:
    """1280x720, 30 fps, 6 s, H.264 + AAC 48 kHz stereo, keyframe every second."""
    return _source(media_dir, "video_h.mp4", w=1280, h=720, fps=30, dur=6, audio_rate=48000)


@pytest.fixture(scope="session")
def video_v(media_dir) -> MediaSource:
    """720x1280 vertical, 30 fps, 4 s."""
    return _source(media_dir, "video_v.mp4", w=720, h=1280, fps=30, dur=4, audio_rate=48000)


@pytest.fixture(scope="session")
def video_25(media_dir) -> MediaSource:
    """640x360, 25 fps, 4 s, AAC 44.1 kHz mono."""
    return _source(media_dir, "video_25.mp4", w=640, h=360, fps=25, dur=4,
                   audio_rate=44100, channels=1)


@pytest.fixture(scope="session")
def video_noaudio(media_dir) -> MediaSource:
    """960x540, 30 fps, 4 s, no audio stream."""
    return _source(media_dir, "video_noaudio.mp4", w=960, h=540, fps=30, dur=4)


@pytest.fixture(scope="session")
def video_unicode(media_dir) -> MediaSource:
    """Non-ASCII file name; 640x360 30 fps, 3 s."""
    return _source(media_dir, "tëst vidéo 日本.mp4", w=640, h=360, fps=30, dur=3, audio_rate=48000)


@pytest.fixture(scope="session")
def nvenc_available() -> bool:
    return ffmpeg.pick_video_encoder("h264", "auto").endswith("_nvenc")


# --------------------------------------------------------------- model construction
def seg(src: MediaSource, a: float, b: float, start: float, muted: bool = False) -> Segment:
    return Segment(uuid.uuid4().hex[:12], src.id, a, b, start, muted)


def sources_of(*srcs: MediaSource) -> dict[str, MediaSource]:
    return {s.id: s for s in srcs}


def settings_for(tmp_path, name: str = "out", **kw) -> ExportSettings:
    kw.setdefault("mode", "accurate")
    kw.setdefault("hw_accel", "off")
    return ExportSettings(filename=name, folder=str(tmp_path), **kw)


# --------------------------------------------------------------------- analysis
class VideoInfo:
    def __init__(self, width, height, times, lumas, start, duration):
        self.width, self.height = width, height
        self.times, self.lumas = np.array(times), np.array(lumas)
        self.start, self.duration = start, duration

    def luma_at(self, t: float) -> float:
        """Centre-patch luma of the frame displayed at output time ``t``."""
        i = int(np.searchsorted(self.times, t + 1e-4, side="right")) - 1
        return float(self.lumas[max(0, min(i, len(self.lumas) - 1))])

    def src_time_at(self, t: float, src_duration: float) -> float:
        """Source time that the frame at output time ``t`` came from."""
        return (self.luma_at(t) - LUMA_LO) / LUMA_SPAN * src_duration


def read_video(path: str) -> VideoInfo:
    times, lumas = [], []
    with av.open(path) as c:
        vs = c.streams.video[0]
        w, h = vs.codec_context.width, vs.codec_context.height
        start = float(vs.start_time * vs.time_base) if vs.start_time is not None else 0.0
        for fr in c.decode(vs):
            arr = fr.reformat(format="yuv420p").to_ndarray()[:fr.height]
            cy, cx = fr.height // 2, fr.width // 2
            lumas.append(float(arr[cy - 8:cy + 8, cx - 8:cx + 8].mean()))
            times.append(float(fr.pts * vs.time_base))
        duration = float(c.duration / av.time_base) if c.duration else 0.0
    return VideoInfo(w, h, times, lumas, start, duration)


def luma_at_pixel(path: str, t: float, x_frac: float, y_frac: float) -> float:
    """Luma of one pixel (given as fractions of the frame) of the frame at time ``t``."""
    with av.open(path) as c:
        vs = c.streams.video[0]
        best = None
        for fr in c.decode(vs):
            if float(fr.pts * vs.time_base) > t + 1e-4:
                break
            best = fr
        arr = best.reformat(format="yuv420p").to_ndarray()[:best.height]
        return float(arr[int(y_frac * (best.height - 1)), int(x_frac * (best.width - 1))])


class AudioInfo:
    def __init__(self, samples, rate, start, channels, sample_rate_in):
        self.samples, self.rate, self.start = samples, rate, start
        self.channels, self.stream_rate = channels, sample_rate_in

    @property
    def duration(self) -> float:
        return len(self.samples) / self.rate


def read_audio(path: str) -> AudioInfo:
    with av.open(path) as c:
        a = c.streams.audio[0]
        rs = av.AudioResampler(format="flt", layout="mono", rate=48000)
        chunks, start = [], None
        for fr in c.decode(a):
            if start is None and fr.pts is not None:
                start = float(fr.pts * a.time_base)
            for out in rs.resample(fr):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in rs.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
        return AudioInfo(np.concatenate(chunks) if chunks else np.zeros(0, np.float32),
                         48000, start or 0.0, a.codec_context.channels, a.codec_context.sample_rate)


def audio_runs(info: AudioInfo, win: float = 0.03, hop: float = 0.01):
    """``[(k, start_time)]`` runs of the per-second frequency step index ``k``;
    ``k == -1`` is silence."""
    n = int(win * info.rate)
    hann = np.hanning(n)
    ks, centres = [], []
    for s in range(0, len(info.samples) - n, int(hop * info.rate)):
        chunk = info.samples[s:s + n]
        if np.sqrt(np.mean(chunk ** 2)) < 0.02:
            k = -1
        else:
            spec = np.abs(np.fft.rfft(chunk * hann, n=n * 8))
            f = np.argmax(spec) * info.rate / (n * 8)
            k = int(round((f - 300) / 150))
        ks.append(k)
        centres.append((s + n / 2) / info.rate)
    runs = []                                 # (k, first_centre, last_centre)
    for k, c in zip(ks, centres):
        if runs and runs[-1][0] == k:
            runs[-1][2] = c
        else:
            runs.append([k, c, c])
    runs = [r for r in runs if r[2] - r[1] >= 0.04 or r is runs[0]]   # drop transition glitches
    merged = []
    for r in runs:
        if merged and merged[-1][0] == r[0]:
            merged[-1][2] = r[2]
        else:
            merged.append(list(r))
    out = []
    for i, (k, first, _last) in enumerate(merged):
        t0 = 0.0 if i == 0 else (merged[i - 1][2] + first) / 2
        out.append((k, t0))
    return out


def audio_k_at(info: AudioInfo, t: float) -> int:
    """Step index ``k`` of the audio around output time ``t`` (-1 = silence)."""
    runs = audio_runs(info)
    k = runs[0][0]
    for rk, rt in runs:
        if rt <= t:
            k = rk
    return k


def audio_rms(info: AudioInfo, a: float, b: float) -> float:
    s = info.samples[int(a * info.rate):int(b * info.rate)]
    return float(np.sqrt(np.mean(s ** 2))) if len(s) else 0.0


def stream_info(path: str) -> dict:
    with av.open(path) as c:
        v = c.streams.video[0]
        d = {"vcodec": v.codec_context.name, "width": v.codec_context.width,
             "height": v.codec_context.height, "fps": float(v.average_rate),
             "pix_fmt": v.codec_context.pix_fmt, "tag": v.codec_context.codec_tag,
             "has_audio": bool(c.streams.audio)}
        if c.streams.audio:
            a = c.streams.audio[0]
            d.update(acodec=a.codec_context.name, arate=a.codec_context.sample_rate,
                     achannels=a.codec_context.channels)
        return d


# ------------------------------------------------------------------ running a job
class JobResult:
    def __init__(self):
        self.finished = None
        self.failed = None
        self.cancelled = False
        self.progress: list[tuple[float, str]] = []

    @property
    def ok(self) -> bool:
        return self.finished is not None


def run_job(job, *, cancel_when=None, timeout: float = 120.0) -> JobResult:
    """Start ``job`` and pump the Qt event loop until its thread ends. ``cancel_when``
    is an optional predicate on the :class:`JobResult` that triggers ``job.cancel()``."""
    import time
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])
    res = JobResult()
    job.finished.connect(lambda p: setattr(res, "finished", p))
    job.failed.connect(lambda m: setattr(res, "failed", m))
    job.cancelled.connect(lambda: setattr(res, "cancelled", True))
    job.progress.connect(lambda f, t: res.progress.append((f, t)))
    job.start()
    deadline = time.monotonic() + timeout
    cancelled = False
    while job.is_running() and time.monotonic() < deadline:
        app.processEvents()
        if cancel_when and not cancelled and cancel_when(res):
            job.cancel()
            cancelled = True
        time.sleep(0.01)
    job.wait(5)
    for _ in range(5):
        app.processEvents()
    return res
