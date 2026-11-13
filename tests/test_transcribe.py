"""Tests for clipforge.media.transcribe - no network, no real model.

The speech engine is replaced by a fake ``faster_whisper`` module injected into
``sys.modules``; the model download uses fake ``_list_repo_files`` / ``_snapshot_download``
(and a fake ``huggingface_hub`` for the thin wrappers). Audio decoding goes through the real
FFmpeg on tiny generated files.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from clipforge.core.captions import CaptionLine, Word
from clipforge.media import ffmpeg
from clipforge.media import transcribe as tr
from clipforge.workers import Cancelled, CancelToken

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ fixtures
@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(tr, "_gpu_cache", None)
    monkeypatch.setattr(tr, "_dll_paths_ready", False)
    monkeypatch.setattr(tr, "_dll_handles", [])
    return tmp_path


def make_model(key="small"):
    d = tr.models_dir() / key
    d.mkdir(parents=True)
    (d / "model.bin").write_bytes(b"x" * 100)
    (d / "config.json").write_text("{}")
    (d / "tokenizer.json").write_text("{}")
    (d / "vocabulary.txt").write_text("a")
    return d


def make_audio(path: Path, kind="tone", dur=3.0) -> str:
    src = {"tone": f"sine=f=440:d={dur}", "silence": f"anullsrc=r=16000:cl=mono:d={dur}"}[kind]
    subprocess.run([ffmpeg.ffmpeg_path(), "-v", "error", "-y", "-f", "lavfi", "-i", src,
                    str(path)], check=True, stdin=subprocess.DEVNULL,
                   creationflags=ffmpeg.creation_flags())
    return str(path)


class Rec:
    """Collects a job's signals; ``run`` starts it and pumps the Qt queue until it ends."""

    def __init__(self, job):
        self.job = job
        self.progress, self.finished, self.failed, self.cancelled = [], [], [], 0
        job.progress.connect(lambda f, s: self.progress.append((f, s)))
        job.finished.connect(lambda *a: self.finished.append(a[0]))
        job.failed.connect(self.failed.append)
        job.cancelled.connect(self.on_cancel)

    def on_cancel(self):
        self.cancelled += 1

    def pump(self):
        QCoreApplication.processEvents()

    def run(self, timeout=20):
        self.job.start()
        assert self.job.wait(timeout), "job did not finish"
        self.pump()
        return self


# ----------------------------------------------------------- fake faster_whisper
class Seg:
    def __init__(self, start, end, words):
        self.start, self.end = start, end
        self.words = [types.SimpleNamespace(start=a, end=b, word=w) for a, b, w in words]


class FakeEngine:
    """Installed as ``faster_whisper``; scripts what WhisperModel does."""

    def __init__(self):
        self.constructed = []
        self.transcribe_kwargs = []
        self.segments = []
        self.language = "en"
        self.init_error = {}                 # device -> exception
        self.iter_error = {}                 # device -> exception (raised at 1st segment)
        self.after_first = None              # callback run after the first segment is yielded
        self.transcribe_error = None

    def install(self, monkeypatch):
        eng = self
        mod = types.ModuleType("faster_whisper")

        class WhisperModel:
            def __init__(self, path, device="cpu", compute_type="default", **kw):
                eng.constructed.append(dict(path=path, device=device, compute_type=compute_type, **kw))
                self.device = device
                if device in eng.init_error:
                    raise eng.init_error[device]

            def transcribe(self, audio, **kw):
                eng.transcribe_kwargs.append(kw)
                if eng.transcribe_error:
                    raise eng.transcribe_error
                info = types.SimpleNamespace(language=eng.language, language_probability=0.9)
                dev = self.device

                def gen():
                    for i, s in enumerate(eng.segments):
                        if i == 0 and dev in eng.iter_error:
                            raise eng.iter_error[dev]
                        yield s
                        if i == 0 and eng.after_first:
                            eng.after_first()
                return gen(), info

        mod.WhisperModel = WhisperModel
        monkeypatch.setitem(sys.modules, "faster_whisper", mod)
        return self


@pytest.fixture
def engine(monkeypatch):
    return FakeEngine().install(monkeypatch)


SEGS = [Seg(0.0, 2.0, [(0.0, 0.5, " Hello"), (0.5, 0.9, " world."), (1.0, 1.0, "  ")]),
        Seg(2.5, 4.0, [(2.5, 3.0, " it's"), (3.0, 3.5, " me"), (3.6, 3.9, "")])]


def no_gpu(monkeypatch, usable=False):
    monkeypatch.setattr(tr, "gpu_status", lambda: tr.GpuStatus(usable, "Fake GPU", "" if usable else "none"))


# ---------------------------------------------------------------- lazy import
def test_import_is_lazy():
    code = ("import sys; sys.path.insert(0, %r); import clipforge.media.transcribe as t; "
            "bad=[m for m in ('faster_whisper','ctranslate2','huggingface_hub','onnxruntime') "
            "if m in sys.modules]; assert not bad, bad; print('ok')" % str(ROOT))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout


def test_is_available_false_when_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)   # import raises ImportError
    assert tr.is_available() is False


def test_is_available_true_with_fake(engine):
    assert tr.is_available() is True


# --------------------------------------------------------------------- models
def test_models_table():
    assert list(tr.MODELS) == ["tiny", "base", "small", "medium"]
    for key, m in tr.MODELS.items():
        assert m.key == key and m.repo_id == f"Systran/faster-whisper-{key}"
        assert m.approx_download_mb > 0 and m.label and m.speed_hint
    assert tr.MODELS["small"].label == "Small (best balance)"


def test_models_dir_honours_localappdata(tmp_path):
    assert tr.models_dir() == tmp_path / "local" / "ClipForge" / "models" / "whisper"


def test_model_downloaded(tmp_path):
    assert tr.model_downloaded("small") is False
    assert tr.model_downloaded("nonsense") is False
    d = make_model("small")
    assert tr.model_downloaded("small") is True
    (d / "tokenizer.json").unlink()
    assert tr.model_downloaded("small") is True          # vocabulary.txt is enough
    (d / "vocabulary.txt").unlink()
    assert tr.model_downloaded("small") is False
    (d / "vocabulary.json").write_text("{}")
    assert tr.model_downloaded("small") is True
    (d / "model.bin").write_bytes(b"")
    assert tr.model_downloaded("small") is False
    (d / "model.bin").write_bytes(b"x")
    (d / "config.json").unlink()
    assert tr.model_downloaded("small") is False


def test_delete_model():
    make_model("base")
    tr.delete_model("base")
    assert not tr.model_downloaded("base") and not (tr.models_dir() / "base").exists()


def test_recommended_model(monkeypatch):
    no_gpu(monkeypatch, True)
    assert tr.recommended_model() == "small"
    no_gpu(monkeypatch, False)
    assert tr.recommended_model() == "base"
    monkeypatch.setattr(tr, "gpu_status", lambda: 1 / 0)
    assert tr.recommended_model() == "base"


# ------------------------------------------------------------------------ GPU
class FakeCt2:
    def __init__(self, devices=1, types_=("float16", "int8"), types_error=None):
        self.devices, self.types_, self.types_error = devices, types_, types_error

    def get_cuda_device_count(self):
        return self.devices

    def get_supported_compute_types(self, device):
        if self.types_error:
            raise self.types_error
        return set(self.types_)


def fake_gpu_env(monkeypatch, ct2, found=True, load_error=None):
    monkeypatch.setattr(tr, "_import_ct2", lambda: ct2)
    monkeypatch.setattr(tr, "_gpu_name", lambda: "NVIDIA Fake 9000")
    monkeypatch.setattr(tr, "_find_dll", lambda n: Path("C:/x") / n if found else None)
    monkeypatch.setattr(tr, "_try_load", lambda p: load_error)


def test_gpu_usable(monkeypatch):
    fake_gpu_env(monkeypatch, FakeCt2())
    st = tr.gpu_status()
    assert st.usable and st.name == "NVIDIA Fake 9000" and st.reason == ""


def test_gpu_status_is_cached(monkeypatch):
    fake_gpu_env(monkeypatch, FakeCt2())
    first = tr.gpu_status()
    monkeypatch.setattr(tr, "_import_ct2", lambda: FakeCt2(devices=0))
    assert tr.gpu_status() is first
    assert tr.refresh_gpu_status().usable is False


def test_gpu_no_device(monkeypatch):
    fake_gpu_env(monkeypatch, FakeCt2(devices=0))
    st = tr.gpu_status()
    assert not st.usable and "No NVIDIA" in st.reason and "CPU" in st.reason


def test_gpu_dlls_missing(monkeypatch):
    fake_gpu_env(monkeypatch, FakeCt2(), found=False)
    st = tr.gpu_status()
    assert not st.usable and st.name == "NVIDIA Fake 9000"
    assert st.reason == ("Your NVIDIA GPU was found but its CUDA libraries are not installed; "
                         "using the CPU (slower).")


def test_gpu_dll_cannot_load(monkeypatch):
    fake_gpu_env(monkeypatch, FakeCt2(), load_error="bad image")
    st = tr.gpu_status()
    assert not st.usable and "could not be loaded" in st.reason


def test_gpu_compute_types_fail_or_old(monkeypatch):
    fake_gpu_env(monkeypatch, FakeCt2(types_error=RuntimeError("cuda boom")))
    assert not tr.gpu_status().usable
    fake_gpu_env(monkeypatch, FakeCt2(types_=("float32", "int8")))
    assert "too old" in tr.refresh_gpu_status().reason


def test_gpu_engine_missing_and_exception(monkeypatch):
    def boom():
        raise ImportError("no ctranslate2")
    monkeypatch.setattr(tr, "_import_ct2", boom)
    assert tr.gpu_status().reason == "The speech engine isn't installed."
    monkeypatch.setattr(tr, "_gpu_cache", None)
    monkeypatch.setattr(tr, "_compute_gpu_status", lambda: 1 / 0)
    st = tr.gpu_status()
    assert not st.usable and "CPU" in st.reason


def test_gpu_pack_dir_added_to_dll_search(tmp_path, monkeypatch):
    pack = tr.gpu_pack_dir()
    assert pack == tmp_path / "local" / "ClipForge" / "gpu"
    added = []
    monkeypatch.setattr(os, "add_dll_directory", lambda p: added.append(p) or object(),
                        raising=False)
    monkeypatch.setenv("PATH", "C:\\somewhere")
    tr._prepare_dll_paths()                      # no folder: nothing happens
    assert added == []
    pack.mkdir(parents=True)
    monkeypatch.setattr(tr, "_dll_paths_ready", False)
    tr._prepare_dll_paths()
    assert added == [str(pack)]
    assert os.environ["PATH"].split(os.pathsep)[0] == str(pack)
    tr._prepare_dll_paths()                      # idempotent
    assert added == [str(pack)]


def test_find_dll_looks_in_gpu_pack(monkeypatch):
    pack = tr.gpu_pack_dir()
    pack.mkdir(parents=True)
    (pack / "cublas64_12.dll").write_bytes(b"MZ")
    assert tr._find_dll("cublas64_12.dll") == pack / "cublas64_12.dll"
    assert tr._find_dll("definitely_not_here_12.dll") is None


# --------------------------------------------------------------- words_to_lines
def W(a, b, t):
    return Word(a, b, t)


def test_words_to_lines_basic_chunking():
    words = [W(0, .3, " Hello"), W(.3, .6, "world "), W(.6, .9, "this"), W(.9, 1.2, "is"),
             W(1.2, 1.5, "fun.")]
    lines = tr.words_to_lines(words, 3)
    assert [ln.text for ln in lines] == ["Hello world this", "is fun."]
    assert all(isinstance(ln, CaptionLine) for ln in lines)
    assert [w.text for w in lines[0].words] == ["Hello", "world", "this"]


def test_words_to_lines_does_not_mutate_input():
    words = [W(0, .3, " Hi "), W(.3, .6, "there")]
    tr.words_to_lines(words, 3)
    assert words[0].text == " Hi "


def test_words_to_lines_drops_punctuation_and_empty():
    words = [W(0, .3, "Wait"), W(.3, .35, "-"), W(.35, .4, ""), W(.4, .8, "what"),
             W(.8, .85, "?"), W(.85, 1.2, "Really")]
    lines = tr.words_to_lines(words, 5)
    assert [ln.text for ln in lines] == ["Wait what?", "Really"]     # '?' kept as break


def test_words_to_lines_merges_apostrophe_tokens():
    words = [W(0, .4, "Tom"), W(.4, .5, "'s"), W(.5, 1.0, "car"), W(1.0, 1.2, "don"),
             W(1.2, 1.3, "\u2019t")]
    lines = tr.words_to_lines(words, 10)
    assert lines[0].text == "Tom's car don\u2019t".replace("don\u2019t", "don\u2019t")
    assert [w.text for w in lines[0].words] == ["Tom's", "car", "don\u2019t"]
    assert lines[0].words[0].end == pytest.approx(0.5)


def test_words_to_lines_min_duration_extends_up_to_next_line():
    words = [W(0, .1, "Hey."), W(.2, .5, "Next"), W(5, 5.05, "Last.")]
    lines = tr.words_to_lines(words, 3)
    assert lines[0].end == pytest.approx(0.2)            # wanted 0.25 but next starts at 0.2
    assert lines[1].end == pytest.approx(.5)
    assert lines[2].end == pytest.approx(5.25)           # last line extends freely
    assert all(ln.end > ln.start for ln in lines)


def test_words_to_lines_empty():
    assert tr.words_to_lines([], 3) == []
    assert tr.words_to_lines([W(0, 1, " "), W(1, 2, "...")], 3) == []


# ------------------------------------------------------------- TranscribeJob
def test_transcribe_success(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch, False)
    engine.segments = list(SEGS)
    audio = make_audio(tmp_path / "t\u00e9st clip \u00fc.wav", dur=4)
    rec = Rec(tr.TranscribeJob(audio, "small", None, "auto")).run()
    assert rec.failed == [] and rec.cancelled == 0
    (words,) = rec.finished
    assert [(w.start, w.end, w.text) for w in words] == [
        (0.0, 0.5, "Hello"), (0.5, 0.9, "world."), (2.5, 3.0, "it's"), (3.0, 3.5, "me")]
    assert all(isinstance(w, Word) for w in words)
    fr = [f for f, _ in rec.progress]
    assert fr[-1] == 1.0 and 0 < max(f for f in fr[:-1]) < 1
    seq = [f for f in fr if f >= 0]
    assert seq == sorted(seq)
    texts = " ".join(s for _, s in rec.progress)
    assert "English" in texts and "CPU" in texts
    c = engine.constructed[0]
    assert c["device"] == "cpu" and c["compute_type"] == "int8"
    assert c["path"] == str(tr.models_dir() / "small") and c["local_files_only"] is True
    kw = engine.transcribe_kwargs[0]
    assert kw["word_timestamps"] is True and kw["vad_filter"] is True
    assert kw["condition_on_previous_text"] is False and kw["beam_size"] in (1, 2)
    assert kw["language"] is None


def test_transcribe_passes_language(tmp_path, engine, monkeypatch):
    make_model("base")
    no_gpu(monkeypatch)
    engine.segments, engine.language = [SEGS[0]], "fr"
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "base", "fr", "cpu")).run()
    assert engine.transcribe_kwargs[0]["language"] == "fr"
    assert "French" in " ".join(s for _, s in rec.progress)


def test_auto_uses_gpu_float16_when_usable(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch, True)
    engine.segments = [SEGS[0]]
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "small", None, "auto")).run()
    assert rec.failed == [] and len(rec.finished[0]) == 2
    assert [(c["device"], c["compute_type"]) for c in engine.constructed] == [("cuda", "float16")]
    assert "GPU" in " ".join(s for _, s in rec.progress)


def test_forced_cpu_ignores_gpu(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch, True)
    engine.segments = [SEGS[0]]
    Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "small", None, "cpu")).run()
    assert engine.constructed[0]["device"] == "cpu"


def test_forced_gpu_unusable_falls_back(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch, False)
    engine.segments = [SEGS[0]]
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "small", None, "gpu")).run()
    assert rec.failed == [] and len(rec.finished[0]) == 2
    assert [c["device"] for c in engine.constructed] == ["cpu"]


@pytest.mark.parametrize("where", ["init", "iter"])
@pytest.mark.parametrize("msg", ["CUDA failed with error out of memory",
                                 "Library cublas64_12.dll is not found or cannot be loaded"])
def test_cuda_error_retries_once_on_cpu(tmp_path, engine, monkeypatch, where, msg):
    make_model("small")
    no_gpu(monkeypatch, True)
    engine.segments = list(SEGS)
    (engine.init_error if where == "init" else engine.iter_error)["cuda"] = RuntimeError(msg)
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav", dur=4), "small", None, "auto")).run()
    assert rec.failed == [] and len(rec.finished[0]) == 4
    assert [(c["device"], c["compute_type"]) for c in engine.constructed] == [
        ("cuda", "float16"), ("cpu", "int8")]


def test_cuda_error_then_cpu_error_fails(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch, True)
    engine.init_error["cuda"] = RuntimeError("CUDA out of memory")
    engine.init_error["cpu"] = RuntimeError("cpu exploded")
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "small")).run()
    assert rec.finished == [] and len(rec.failed) == 1
    assert "cpu exploded" not in rec.failed[0] and "log" in rec.failed[0]
    assert len(engine.constructed) == 2


def test_other_errors_do_not_retry(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch, True)
    engine.iter_error["cuda"] = ValueError("tokenizer exploded")
    engine.segments = [SEGS[0]]
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "small")).run()
    assert len(rec.failed) == 1 and rec.finished == []
    assert len(engine.constructed) == 1


def test_unsupported_language(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch)
    engine.transcribe_error = ValueError("not a language")
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "small", "xx", "cpu")).run()
    assert rec.failed == ["That language isn't supported by the caption engine."]


def test_cancel_mid_run(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch)
    engine.segments = [SEGS[0], SEGS[1], Seg(5, 6, [(5, 5.5, " never")])]
    job = tr.TranscribeJob(make_audio(tmp_path / "a.wav", dur=6), "small", None, "cpu")
    engine.after_first = job.cancel          # cancelled right after the first segment
    rec = Rec(job).run()
    assert rec.cancelled == 1 and rec.finished == [] and rec.failed == []
    assert not job.is_running()


def test_silent_audio_gives_empty_list_without_loading_model(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch)
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "s.wav", "silence"), "small")).run()
    assert rec.finished == [[]] and rec.failed == []
    assert engine.constructed == []


def test_no_speech_found(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch)
    engine.segments = []
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "small")).run()
    assert rec.finished == [[]] and rec.failed == []


def test_corrupt_file(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch)
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(os.urandom(5000))
    rec = Rec(tr.TranscribeJob(str(bad), "small")).run()
    assert rec.finished == [] and rec.failed == [
        "This file's audio couldn't be read. It may be damaged or in an unsupported format."]
    assert engine.constructed == []


def test_missing_file_and_model_not_downloaded(tmp_path, engine, monkeypatch):
    no_gpu(monkeypatch)
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "small")).run()
    assert rec.failed == ["Download the Small (best balance) model first."]
    make_model("small")
    rec = Rec(tr.TranscribeJob(str(tmp_path / "nope.wav"), "small")).run()
    assert rec.failed == ["The audio file could not be found."]
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "b.wav"), "huge")).run()
    assert rec.failed and "Unknown" in rec.failed[0]


def test_engine_missing(tmp_path, monkeypatch):
    make_model("small")
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    rec = Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "small")).run()
    assert len(rec.failed) == 1 and "isn't available" in rec.failed[0]


def test_job_can_run_twice(tmp_path, engine, monkeypatch):
    make_model("small")
    no_gpu(monkeypatch)
    engine.segments = [SEGS[0]]
    job = tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "small", None, "cpu")
    rec = Rec(job).run()
    rec.run()
    assert len(rec.finished) == 2


# ------------------------------------------------------------ ModelDownloadJob
MB = 1024 * 1024


def fake_repo(monkeypatch, *, bin_bytes=b"M" * (3 * MB), sha=None, size=None, extra=None,
              steps=4, delay=0.0, fail_snapshot=None, fail_list=None, calls=None):
    """Patch the two network wrappers. Returns the listing dict."""
    sha = sha or hashlib.sha256(bin_bytes).hexdigest()
    files = {"model.bin": bin_bytes, "config.json": b"{}", "tokenizer.json": b"{\"a\":1}",
             "vocabulary.txt": b"hello"}
    files.update(extra or {})
    listing = {n: (len(b), None) for n, b in files.items()}
    listing["model.bin"] = (size if size is not None else len(bin_bytes), sha)

    def list_files(repo_id):
        if calls is not None:
            calls.append(("list", repo_id))
        if fail_list:
            raise fail_list
        return dict(listing)

    def snapshot(repo_id, local_dir, cancel):
        if calls is not None:
            calls.append(("snap", repo_id, str(local_dir)))
        if fail_snapshot:
            raise fail_snapshot
        local_dir.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            if name == "model.bin":
                part = local_dir / ".cache" / "huggingface" / "download"
                part.mkdir(parents=True, exist_ok=True)
                inc = part / "model.bin.abc.incomplete"
                with open(inc, "wb") as fh:
                    step = max(1, len(data) // steps)
                    for i in range(0, len(data), step):
                        if cancel.cancelled:
                            raise Cancelled()
                        fh.write(data[i:i + step])
                        fh.flush()
                        time.sleep(delay)
                os.replace(inc, local_dir / name)
            else:
                (local_dir / name).write_bytes(data)

    monkeypatch.setattr(tr, "_list_repo_files", list_files)
    monkeypatch.setattr(tr, "_snapshot_download", snapshot)
    return listing


def dl(key="tiny", **kw):
    return Rec(tr.ModelDownloadJob(key)).run(**kw)


def test_download_success_with_progress(monkeypatch):
    calls = []
    fake_repo(monkeypatch, delay=0.12, calls=calls)
    rec = dl()
    assert rec.failed == [] and rec.finished == ["tiny"]
    assert tr.model_downloaded("tiny")
    assert calls[0] == ("list", "Systran/faster-whisper-tiny")
    fr = [f for f, _ in rec.progress]
    assert fr[-1] == 1.0
    mid = [f for f in fr if 0 < f < 0.95]
    assert mid, fr                                    # real intermediate progress
    seq = [f for f in fr if f >= 0]
    assert seq == sorted(seq)
    assert any("Checking" in s for _, s in rec.progress)


def test_download_cancel(monkeypatch):
    fake_repo(monkeypatch, bin_bytes=b"M" * (20 * MB), steps=200, delay=0.05)
    job = tr.ModelDownloadJob("tiny")
    rec = Rec(job)
    job.start()
    time.sleep(0.6)
    job.cancel()
    assert job.wait(20)
    rec.pump()
    assert rec.cancelled == 1 and rec.finished == [] and rec.failed == []
    assert not tr.model_downloaded("tiny")


def test_download_cancel_during_hash(monkeypatch):
    fake_repo(monkeypatch, bin_bytes=b"M" * (30 * MB), steps=3)
    job = tr.ModelDownloadJob("tiny")
    rec = Rec(job)
    job.progress.connect(lambda f, s: None)
    orig = tr._sha256_file

    def slow(path, cancel, on_bytes):
        job.cancel()
        return orig(path, cancel, on_bytes)

    monkeypatch.setattr(tr, "_sha256_file", slow)
    rec.run()
    assert rec.cancelled == 1 and rec.finished == []


def test_download_network_error_on_listing(monkeypatch):
    fake_repo(monkeypatch, fail_list=ConnectionError("getaddrinfo failed"))
    rec = dl()
    assert rec.finished == [] and len(rec.failed) == 1
    assert rec.failed[0].startswith("Couldn't reach the model server")
    assert not tr.model_downloaded("tiny")


def test_download_network_error_during_transfer(monkeypatch):
    class ReadTimeout(Exception):
        pass
    fake_repo(monkeypatch, fail_snapshot=ReadTimeout("read timed out"))
    rec = dl()
    assert rec.failed and rec.failed[0].startswith("Couldn't reach the model server")


def test_download_unknown_error_is_generic(monkeypatch):
    fake_repo(monkeypatch, fail_snapshot=KeyError("weird"))
    rec = dl()
    assert rec.failed == ["The speech model download failed. Details are in the log file."]


def test_download_size_mismatch_removes_model(monkeypatch):
    fake_repo(monkeypatch, size=3 * MB + 5)
    rec = dl()
    assert rec.finished == [] and "corrupted" in rec.failed[0]
    assert not (tr.models_dir() / "tiny").exists()


def test_download_hash_mismatch_removes_model(monkeypatch):
    fake_repo(monkeypatch, sha="0" * 64)
    rec = dl()
    assert rec.finished == [] and "corrupted" in rec.failed[0]
    assert not (tr.models_dir() / "tiny").exists() and not tr.model_downloaded("tiny")


def test_download_incomplete_listing(monkeypatch):
    fake_repo(monkeypatch)
    monkeypatch.setattr(tr, "_list_repo_files", lambda r: {"config.json": (2, None)})
    rec = dl()
    assert rec.failed and "expected files" in rec.failed[0]


def test_download_missing_file_after_snapshot(monkeypatch):
    fake_repo(monkeypatch)
    monkeypatch.setattr(tr, "_snapshot_download", lambda r, d, c: d.mkdir(parents=True, exist_ok=True))
    rec = dl()
    assert rec.failed and "incomplete" in rec.failed[0]
    assert not (tr.models_dir() / "tiny").exists()


def test_download_not_enough_disk(monkeypatch):
    calls = []
    fake_repo(monkeypatch, calls=calls)
    monkeypatch.setattr(tr.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(total=1, used=1, free=1024))
    rec = dl()
    assert rec.failed and "free disk space" in rec.failed[0]
    assert not any(c[0] == "snap" for c in calls)


def test_download_disk_full_oserror(monkeypatch):
    import errno
    fake_repo(monkeypatch, fail_snapshot=OSError(errno.ENOSPC, "No space left on device"))
    rec = dl()
    assert rec.failed and "free disk space" in rec.failed[0]


def test_download_unknown_model():
    rec = dl("gigantic")
    assert rec.failed and "Unknown" in rec.failed[0]


def test_download_resumes_existing_partial(monkeypatch):
    fake_repo(monkeypatch)
    d = tr.models_dir() / "tiny" / ".cache" / "huggingface" / "download"
    d.mkdir(parents=True)
    (d / "model.bin.abc.incomplete").write_bytes(b"M" * MB)
    seen = []
    orig = tr._download_bytes_on_disk
    monkeypatch.setattr(tr, "_download_bytes_on_disk", lambda *a: seen.append(orig(*a)) or seen[-1])
    rec = dl()
    assert rec.finished == ["tiny"]
    assert seen[0] == MB                                  # the partial file counted as progress


def test_download_job_not_restarted_while_running(monkeypatch):
    fake_repo(monkeypatch, bin_bytes=b"M" * (8 * MB), steps=40, delay=0.03)
    job = tr.ModelDownloadJob("tiny")
    rec = Rec(job)
    job.start()
    first = job._thread
    job.start()
    assert job._thread is first
    assert job.wait(30)
    rec.pump()
    assert rec.finished == ["tiny"]


# ----------------------------------------- thin huggingface_hub wrappers (fakes)
@pytest.fixture
def fake_hf(monkeypatch):
    for var in ("HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_IMPLICIT_TOKEN",
                "HF_HUB_DISABLE_XET", "HF_HUB_DISABLE_PROGRESS_BARS"):
        monkeypatch.delenv(var, raising=False)
    rec = types.SimpleNamespace(api_kwargs=None, info_kwargs=None, snap=None)

    class Lfs(dict):
        pass

    class FakeApi:
        def __init__(self, **kw):
            rec.api_kwargs = kw

        def model_info(self, repo_id, **kw):
            rec.info_kwargs = dict(repo_id=repo_id, **kw)
            sib = lambda n, s, sha=None: types.SimpleNamespace(
                rfilename=n, size=s, lfs=Lfs(sha256=sha) if sha else None)
            return types.SimpleNamespace(siblings=[
                sib("model.bin", 100, "ab" * 32), sib("config.json", 5), sib("README.md", 9),
                sib("vocabulary.txt", 7), sib("sub/other.bin", 3), sib("tokenizer.json", 4)])

    class BaseBar:
        def __init__(self, *a, **kw):
            self.n = 0

        def update(self, n=1):
            self.n += n
            return True

    def snapshot_download(repo_id, **kw):
        rec.snap = dict(repo_id=repo_id, **kw)
        bar = kw["tqdm_class"](total=10)
        bar.update(5)
        rec.bar = bar
        return kw["local_dir"]

    hub = types.ModuleType("huggingface_hub")
    hub.HfApi, hub.snapshot_download = FakeApi, snapshot_download
    utils = types.ModuleType("huggingface_hub.utils")
    utils.tqdm = BaseBar
    consts = types.ModuleType("huggingface_hub.constants")
    hub.utils, hub.constants = utils, consts
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils", utils)
    monkeypatch.setitem(sys.modules, "huggingface_hub.constants", consts)
    return rec


def test_list_repo_files_wrapper(fake_hf):
    listing = tr._list_repo_files("Systran/faster-whisper-tiny")
    assert listing == {"model.bin": (100, "ab" * 32), "config.json": (5, None),
                       "vocabulary.txt": (7, None), "tokenizer.json": (4, None)}
    assert fake_hf.api_kwargs == {"endpoint": "https://huggingface.co", "token": False}
    assert fake_hf.info_kwargs["files_metadata"] is True
    for var in ("HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_IMPLICIT_TOKEN"):
        assert os.environ[var] == "1"


def test_snapshot_wrapper_arguments_and_cancel_hook(tmp_path, fake_hf):
    token = CancelToken()
    tr._snapshot_download("Systran/faster-whisper-tiny", tmp_path / "t", token)
    s = fake_hf.snap
    assert s["repo_id"] == "Systran/faster-whisper-tiny" and s["local_dir"] == str(tmp_path / "t")
    assert s["endpoint"] == "https://huggingface.co" and s["token"] is False
    assert set(s["allow_patterns"]) >= {"model.bin", "config.json", "tokenizer.json", "vocabulary.*"}
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"
    assert fake_hf.bar.n == 5
    token.cancel()
    with pytest.raises(Cancelled):
        fake_hf.bar.update(1)


# ------------------------------------------------------------------ thread-safety
def test_jobs_do_not_leave_threads(monkeypatch, engine, tmp_path):
    make_model("small")
    no_gpu(monkeypatch)
    engine.segments = [SEGS[0]]
    before = threading.active_count()
    Rec(tr.TranscribeJob(make_audio(tmp_path / "a.wav"), "small", None, "cpu")).run()
    time.sleep(0.2)
    assert threading.active_count() <= before
