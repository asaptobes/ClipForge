"""WaveformProvider tests using small synthetic media made with the bundled FFmpeg."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from clipforge.core.model import MediaSource
from clipforge.media import waveform
from clipforge.media.probe import probe_source
from clipforge.media.waveform import PEAKS_PER_SECOND, WaveformProvider

FFMPEG = str(Path(__file__).resolve().parent.parent / "vendor" / "ffmpeg" / "ffmpeg.exe")

pytestmark = pytest.mark.skipif(not os.path.isfile(FFMPEG), reason="bundled ffmpeg missing")


def _ff(*args: str) -> None:
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, stdin=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("wave")
    # 2 s of silence then 2 s of a loud sine, 4 s total, with video
    av = str(d / "av.mp4")
    _ff("-f", "lavfi", "-i", "testsrc=s=64x48:r=25:d=4",
        "-f", "lavfi", "-i", "aevalsrc='if(lt(t,2),0,0.8*sin(2*PI*440*t))':s=44100:d=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", av)
    silent = str(d / "silent.mp4")
    _ff("-f", "lavfi", "-i", "testsrc=s=64x48:r=25:d=2", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-an", silent)
    bad = d / "bad.mp4"
    bad.write_bytes(b"garbage" * 100)
    return {"av": av, "silent": silent, "bad": str(bad)}


@pytest.fixture
def provider(tmp_path):
    p = WaveformProvider(str(tmp_path / "cache"))
    yield p
    p.shutdown()


def _wait(qtbot, fn, timeout=15000):
    box = {}

    def ok():
        box["v"] = fn()
        return box["v"] is not None
    qtbot.waitUntil(ok, timeout=timeout)
    return box["v"]


def test_extracts_peaks_async(qtbot, provider, media):
    src = probe_source(media["av"])
    assert provider.get(src) is None
    with qtbot.waitSignal(provider.waveformReady, timeout=15000) as sig:
        pass
    assert sig.args == [src.id]
    peaks = provider.get(src)
    assert peaks.dtype == np.float32 and peaks.ndim == 2 and peaks.shape[1] == 2
    assert abs(peaks.shape[0] - 4 * PEAKS_PER_SECOND) <= 0.1 * PEAKS_PER_SECOND
    assert np.all(peaks[:, 0] <= peaks[:, 1])
    assert np.abs(peaks).max() <= 1.0
    quiet = peaks[: 1 * PEAKS_PER_SECOND]
    loud = peaks[int(2.5 * PEAKS_PER_SECOND): int(3.5 * PEAKS_PER_SECOND)]
    assert np.abs(quiet).max() < 0.05
    assert loud[:, 1].max() > 0.5 and loud[:, 0].min() < -0.5


def test_no_audio_returns_empty_immediately(provider, media):
    src = probe_source(media["silent"])
    assert not src.has_audio
    peaks = provider.get(src)
    assert peaks is not None and peaks.shape == (0, 2) and peaks.dtype == np.float32


def test_disk_cache_reused(qtbot, tmp_path, media):
    cache = str(tmp_path / "c")
    src = probe_source(media["av"])
    p1 = WaveformProvider(cache)
    try:
        p1.get(src)
        first = _wait(qtbot, lambda: p1.get(src))
    finally:
        p1.shutdown()
    assert any(f.endswith(".npy") for f in os.listdir(cache))
    p2 = WaveformProvider(cache)
    try:
        second = p2.get(src)                        # immediate, from the .npy
        assert second is not None
        np.testing.assert_array_equal(first, second)
    finally:
        p2.shutdown()


def test_corrupt_file_yields_none_and_no_crash(qtbot, provider, media):
    bad = MediaSource(id="bad", path=media["bad"], duration=3, has_audio=True)
    assert provider.get(bad) is None
    qtbot.wait(1500)
    assert provider.get(bad) is None
    missing = MediaSource(id="m", path=media["bad"] + ".nope", duration=3)
    assert provider.get(missing) is None
    good = probe_source(media["av"])
    provider.get(good)
    assert _wait(qtbot, lambda: provider.get(good)) is not None


def test_reduce_windows_are_exact():
    w = waveform._WINDOW
    s = np.zeros(w * 3, dtype=np.int16)
    s[w + 3] = 16384
    s[2 * w + 1] = -32768
    out = waveform._reduce(s)
    assert out.shape == (3, 2)
    assert out[1, 1] == pytest.approx(0.5, abs=1e-3)
    assert out[2, 0] == -1.0


def test_extract_peaks_streams_in_chunks(media, monkeypatch):
    monkeypatch.setattr(waveform, "_CHUNK_BYTES", 1000 * 2 + 6)    # awkward chunk size
    a = waveform.extract_peaks(media["av"])
    monkeypatch.undo()
    b = waveform.extract_peaks(media["av"])
    np.testing.assert_allclose(a, b, atol=1e-6)


def test_shutdown_idempotent(provider, media):
    src = probe_source(media["av"])
    provider.get(src)
    provider.shutdown()
    provider.shutdown()
    provider.set_cache_dir(os.path.join(os.path.dirname(media["av"]), "x"))
