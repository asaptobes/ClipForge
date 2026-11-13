"""Local speech-to-text with word timestamps (faster-whisper / CTranslate2).

Everything runs on this computer: no audio ever leaves it. The only network use is the
one-off download of a speech model from huggingface.co (``ModelDownloadJob``).

Nothing here imports ``faster_whisper`` / ``ctranslate2`` / ``huggingface_hub`` at module
import time - they are imported inside the functions that need them, so the app starts
fast and still starts when the library is missing (:func:`is_available`).

Public API
----------
``is_available() -> bool``                    the speech engine can be imported
``MODELS`` / ``ModelInfo`` / ``recommended_model()``
``models_dir() / model_downloaded(key) / delete_model(key)``
``ModelDownloadJob(key, parent=None)``        signals progress(float, str), finished(str key),
                                              failed(str), cancelled(); start/cancel/
                                              is_running/wait
``gpu_pack_dir() / gpu_status() -> GpuStatus``
``TranscribeJob(audio_path, model="small", language=None, device="auto", parent=None)``
                                              signals progress(float, str), finished(list of
                                              clipforge.core.captions.Word), failed(str),
                                              cancelled(); start/cancel/is_running/wait
``words_to_lines(words, max_words) -> list[CaptionLine]``

GPU
---
The CUDA path needs NVIDIA's cuBLAS 12 DLLs (cublas64_12.dll + cublasLt64_12.dll, about
770 MB), which are far too large to ship (cuDNN is not needed for Whisper).
If ``<local data dir>/gpu`` exists it is added to the DLL search path (and ``PATH``) before
CTranslate2 loads, so a downloadable "GPU pack" can simply drop the DLLs there. Without
them everything runs on the CPU (int8), which is slower but always works.
"""

from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

from .. import paths
from ..core.captions import CaptionLine, Word, chunk_words
from ..workers import Cancelled, CancelToken
from . import MediaError

log = logging.getLogger(__name__)

HF_ENDPOINT = "https://huggingface.co"
SAMPLE_RATE = 16000
MIN_LINE_SECONDS = 0.25


# ------------------------------------------------------------------ availability
def is_available() -> bool:
    """True if the speech engine (``faster_whisper``) can be imported."""
    try:
        _prepare_dll_paths()
        import faster_whisper  # noqa: F401
        return True
    except Exception as exc:  # noqa: BLE001 - missing wheel, broken DLL, ...
        log.info("faster-whisper is not available: %s", exc)
        return False


# ----------------------------------------------------------------------- models
@dataclass(frozen=True)
class ModelInfo:
    key: str
    label: str
    approx_download_mb: int
    repo_id: str
    speed_hint: str


MODELS: dict[str, ModelInfo] = {
    "tiny": ModelInfo("tiny", "Tiny (fastest, rough)", 75, "Systran/faster-whisper-tiny",
                      "Fastest. Good for clear speech in English; makes more mistakes."),
    "base": ModelInfo("base", "Base (fast)", 145, "Systran/faster-whisper-base",
                      "Fast, even on a slow computer. Fine for clear speech."),
    "small": ModelInfo("small", "Small (best balance)", 484, "Systran/faster-whisper-small",
                       "Good accuracy. Very fast on an NVIDIA graphics card, several times "
                       "faster than real time on a modern processor."),
    "medium": ModelInfo("medium", "Medium (most accurate)", 1530, "Systran/faster-whisper-medium",
                        "Best accuracy, slow without a graphics card."),
}

_ALLOW_PATTERNS = ["config.json", "model.bin", "tokenizer.json", "vocabulary.*",
                   "preprocessor_config.json"]
_REQUIRED_ALWAYS = ("model.bin", "config.json")


def _model_info(key: str) -> ModelInfo:
    try:
        return MODELS[key]
    except KeyError:
        raise MediaError(f"Unknown speech model \"{key}\".") from None


def models_dir() -> Path:
    """``%LOCALAPPDATA%\\ClipForge\\models\\whisper`` (not created here)."""
    return paths.local_data_dir() / "models" / "whisper"


def _model_path(key: str) -> Path:
    return models_dir() / key


def model_downloaded(key: str) -> bool:
    """A complete snapshot of model ``key`` is on disk."""
    if key not in MODELS:
        return False
    d = _model_path(key)
    try:
        for name in _REQUIRED_ALWAYS:
            f = d / name
            if not f.is_file() or f.stat().st_size <= 0:
                return False
        if not ((d / "tokenizer.json").is_file()
                or any(d.glob("vocabulary.*"))):
            return False
        return True
    except OSError:
        return False


def recommended_model() -> str:
    """"small" when a usable GPU is present, else "base"."""
    try:
        return "small" if gpu_status().usable else "base"
    except Exception as exc:  # noqa: BLE001
        log.warning("GPU check failed: %s", exc)
        return "base"


def delete_model(key: str) -> None:
    """Remove a downloaded (or half downloaded) model from disk."""
    if key in MODELS:
        shutil.rmtree(_model_path(key), ignore_errors=True)


# ------------------------------------------------------------- model download
class _DownloadError(Exception):
    """Internal: ``str(exc)`` is a friendly message for the UI."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


_NET_MESSAGE = ("Couldn't reach the model server. Check your internet connection and try "
                "again.")


def _configure_hf() -> Any:
    """Import huggingface_hub with telemetry, tokens and the Xet side channel disabled."""
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_DISABLE_XET"] = "1"          # plain HTTPS: real progress, cancellable
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    import huggingface_hub  # noqa: PLC0415
    try:                                            # constants are read at import time
        from huggingface_hub import constants  # noqa: PLC0415
        for name in ("HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_IMPLICIT_TOKEN",
                     "HF_HUB_DISABLE_XET"):
            if hasattr(constants, name):
                setattr(constants, name, True)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not tune huggingface_hub constants: %s", exc)
    return huggingface_hub


def _list_repo_files(repo_id: str) -> dict[str, tuple[int, Optional[str]]]:
    """``{filename: (size, sha256 or None)}`` of the files we download (HF API listing)."""
    hf = _configure_hf()
    api = hf.HfApi(endpoint=HF_ENDPOINT, token=False)
    info = api.model_info(repo_id, files_metadata=True, timeout=20)
    out: dict[str, tuple[int, Optional[str]]] = {}
    for sib in info.siblings or []:
        name = sib.rfilename
        if not _wanted(name):
            continue
        lfs = getattr(sib, "lfs", None)
        sha = None
        if lfs is not None:
            sha = lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)
        out[name] = (int(sib.size or 0), sha)
    return out


def _wanted(name: str) -> bool:
    return "/" not in name and any(fnmatch.fnmatch(name, p) for p in _ALLOW_PATTERNS)


def _snapshot_download(repo_id: str, local_dir: Path, cancel: CancelToken) -> None:
    """``huggingface_hub.snapshot_download`` of the needed files; raises ``Cancelled``
    between chunks when ``cancel`` is set. Resumes partial files."""
    hf = _configure_hf()
    from huggingface_hub.utils import tqdm as hf_tqdm  # noqa: PLC0415

    class _CancelBar(hf_tqdm):  # type: ignore[misc, valid-type]
        def update(self, n=1):  # noqa: ANN001
            if cancel.cancelled:
                raise Cancelled()
            return super().update(n)

    hf.snapshot_download(repo_id, local_dir=str(local_dir), allow_patterns=_ALLOW_PATTERNS,
                         endpoint=HF_ENDPOINT, token=False, tqdm_class=_CancelBar,
                         max_workers=2)


def _sha256_file(path: Path, cancel: CancelToken, on_bytes: Callable[[int], None]) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as fh:
        while True:
            cancel.raise_if_cancelled()
            chunk = fh.read(4 << 20)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            on_bytes(done)
    return h.hexdigest()


def _download_bytes_on_disk(local_dir: Path, listing: dict[str, tuple[int, Optional[str]]]) -> int:
    """Bytes already present: finished files plus ``.incomplete`` partials."""
    total = 0
    inc = local_dir / ".cache" / "huggingface" / "download"
    for name, (size, _sha) in listing.items():
        try:
            f = local_dir / name
            if f.is_file():
                total += min(size, f.stat().st_size) if size else f.stat().st_size
                continue
            best = 0
            if inc.is_dir():
                for p in inc.glob(name + ".*.incomplete"):
                    best = max(best, p.stat().st_size)
            total += min(size, best) if size else best
        except OSError:
            continue
    return total


def _friendly_download_error(exc: BaseException) -> _DownloadError:
    if isinstance(exc, _DownloadError):
        return exc
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return _DownloadError("There isn't enough free disk space for the speech model.",
                              repr(exc))
    if isinstance(exc, PermissionError):
        return _DownloadError("The speech model couldn't be saved. Check that ClipForge's "
                              "data folder is writable.", repr(exc))
    names = " ".join(c.__name__ for c in type(exc).__mro__)
    text = f"{names} {exc}".lower()
    if isinstance(exc, (OSError, TimeoutError, ConnectionError)) or any(
            w in text for w in ("connect", "timeout", "timed out", "network", "offline",
                                "ssl", "resolve", "http", "unreachable")):
        return _DownloadError(_NET_MESSAGE, repr(exc))
    return _DownloadError("The speech model download failed. Details are in the log file.",
                          repr(exc))


class ModelDownloadJob(QObject):
    """Download a speech model into ``models_dir()/<key>`` on a background thread.

    Files are fetched from huggingface.co over HTTPS without credentials or telemetry, with
    resume support. The sizes of all files and the SHA-256 of ``model.bin`` are checked
    against the repository listing; a model that fails the check is deleted."""

    progress = Signal(float, str)
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, key: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._key = key
        self._cancel = CancelToken()
        self._thread: Optional[threading.Thread] = None
        self._last_pct = -1

    def start(self) -> None:
        if self.is_running():
            return
        self._cancel = CancelToken()
        self._last_pct = -1
        self._thread = threading.Thread(target=self._run, name="clipforge-model-download",
                                        daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.cancel()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.is_running()

    def _emit(self, fraction: float, text: str) -> None:
        pct = int(max(0.0, min(1.0, fraction)) * 100)
        if pct != self._last_pct or fraction < 0:
            self._last_pct = pct
            self.progress.emit(fraction, text)

    def _run(self) -> None:
        outcome, message = "failed", ""
        try:
            self._work()
            outcome = "finished"
        except Cancelled:
            outcome = "cancelled"
        except _DownloadError as exc:
            log.error("Model download failed: %s %s", exc, exc.detail)
            message = str(exc)
        except MediaError as exc:
            message = str(exc)
        except Exception as exc:  # noqa: BLE001
            err = _friendly_download_error(exc)
            log.error("Model download failed: %s", exc, exc_info=True)
            message = str(err)
        if outcome == "finished":
            self.finished.emit(self._key)
        elif outcome == "cancelled":
            self.cancelled.emit()
        else:
            self.failed.emit(message or "The speech model download failed.")

    def _work(self) -> None:
        info = _model_info(self._key)
        cancel = self._cancel
        dest = _model_path(self._key)
        self._emit(-1.0, f"Contacting the model server for {info.label}...")
        try:
            listing = _list_repo_files(info.repo_id)
        except Exception as exc:  # noqa: BLE001
            raise _friendly_download_error(exc) from exc
        cancel.raise_if_cancelled()
        if "model.bin" not in listing or "config.json" not in listing or not (
                "tokenizer.json" in listing or any(n.startswith("vocabulary.") for n in listing)):
            raise _DownloadError("The model server doesn't have the expected files for this "
                                 "model.", f"listing {sorted(listing)}")
        total = sum(s for s, _ in listing.values()) or info.approx_download_mb * 1024 * 1024
        dest.mkdir(parents=True, exist_ok=True)
        have = _download_bytes_on_disk(dest, listing)
        try:
            free = shutil.disk_usage(dest).free
        except OSError:
            free = None
        if free is not None and free < (total - have) * 1.05 + 64 * 1024 * 1024:
            raise _DownloadError("There isn't enough free disk space for the speech model "
                                 f"(about {total // (1024 * 1024)} MB needed).")

        result: dict[str, BaseException] = {}

        def fetch() -> None:
            try:
                _snapshot_download(info.repo_id, dest, cancel)
            except BaseException as exc:  # noqa: BLE001 - handed to the polling loop
                result["error"] = exc

        t = threading.Thread(target=fetch, name="clipforge-model-fetch", daemon=True)
        t.start()
        while t.is_alive():
            t.join(0.25)
            got = _download_bytes_on_disk(dest, listing)
            self._emit(0.95 * min(1.0, got / total) if total else -1.0,
                       f"Downloading the {info.label} model... "
                       f"{got // (1024 * 1024)} / {total // (1024 * 1024)} MB")
            if cancel.cancelled:
                t.join(15)            # the progress hook aborts the transfer promptly
                break
        if t.is_alive():
            raise Cancelled()
        cancel.raise_if_cancelled()
        if "error" in result:
            err = result["error"]
            if isinstance(err, Cancelled):
                raise err
            raise _friendly_download_error(err) from err

        self._verify(dest, listing)
        self._emit(1.0, "Model ready")

    def _verify(self, dest: Path, listing: dict[str, tuple[int, Optional[str]]]) -> None:
        cancel = self._cancel
        bad = _DownloadError("The downloaded model was corrupted and was removed. Please "
                             "try again.")
        for name, (size, _sha) in listing.items():
            f = dest / name
            if not f.is_file():
                shutil.rmtree(dest, ignore_errors=True)
                raise _DownloadError("The model download was incomplete. Please try again.",
                                     f"missing {name}")
            got_size = f.stat().st_size
            if size and got_size != size:
                shutil.rmtree(dest, ignore_errors=True)
                bad.detail = f"{name}: {got_size} != {size}"
                raise bad
        size, sha = listing["model.bin"]
        if sha:
            self._emit(0.96, "Checking the downloaded model...")

            def hashing(done: int) -> None:
                self._emit(0.96 + 0.04 * done / max(1, size), "Checking the downloaded model...")

            try:
                got = _sha256_file(dest / "model.bin", cancel, hashing)
            except Cancelled:
                raise
            except OSError as exc:
                raise _friendly_download_error(exc) from exc
            if got.lower() != sha.lower():
                shutil.rmtree(dest, ignore_errors=True)
                bad.detail = f"model.bin sha256 {got} != {sha}"
                raise bad
        else:
            log.warning("No SHA-256 in the listing for model.bin of %s; size checked only",
                        self._key)


# ------------------------------------------------------------------------- GPU
# Measured with ctranslate2 4.9.0 / Whisper: cuBLAS 12 is all that is needed (cuDNN is not).
_CUDA_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")
_dll_paths_ready = False
_dll_handles: list[Any] = []
_gpu_cache: Optional["GpuStatus"] = None


@dataclass(frozen=True)
class GpuStatus:
    usable: bool
    name: str
    reason: str


def gpu_pack_dir() -> Path:
    """Folder where a "GPU pack" (NVIDIA cuBLAS/cuDNN DLLs) is expected; may not exist."""
    return paths.local_data_dir() / "gpu"


def _prepare_dll_paths() -> None:
    """Make ``<local data dir>/gpu`` visible to the DLL loader before CTranslate2 loads."""
    global _dll_paths_ready
    if _dll_paths_ready or os.name != "nt":
        return
    _dll_paths_ready = True
    d = gpu_pack_dir()
    if not d.is_dir():
        return
    try:
        _dll_handles.append(os.add_dll_directory(str(d)))
    except (OSError, AttributeError) as exc:
        log.warning("Could not add %s to the DLL search path: %s", d, exc)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if str(d) not in parts:
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def _import_ct2() -> Any:
    _prepare_dll_paths()
    import ctranslate2  # noqa: PLC0415
    return ctranslate2


def _dll_search_dirs() -> list[Path]:
    dirs: list[Path] = [gpu_pack_dir()]
    try:
        import ctranslate2  # noqa: PLC0415
        dirs.append(Path(ctranslate2.__file__).resolve().parent)
    except Exception:  # noqa: BLE001
        pass
    dirs += [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return dirs


def _find_dll(name: str) -> Optional[Path]:
    seen: set[str] = set()
    for d in _dll_search_dirs():
        try:
            key = os.path.normcase(str(d))
            if key in seen:
                continue
            seen.add(key)
            p = d / name
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _try_load(path: Path) -> Optional[str]:
    """Load a DLL by absolute path (its dependencies from the same folder); error or None."""
    try:
        ctypes.WinDLL(str(path))
        return None
    except OSError as exc:
        return str(exc)


def _gpu_name() -> str:
    """Name of the first CUDA device through the driver API (no CUDA toolkit needed)."""
    try:
        cuda = ctypes.WinDLL("nvcuda.dll")
        if cuda.cuInit(0) != 0:
            return "NVIDIA GPU"
        dev = ctypes.c_int()
        if cuda.cuDeviceGet(ctypes.byref(dev), 0) != 0:
            return "NVIDIA GPU"
        buf = ctypes.create_string_buffer(256)
        if cuda.cuDeviceGetName(buf, 256, dev) != 0:
            return "NVIDIA GPU"
        return buf.value.decode("utf-8", errors="replace") or "NVIDIA GPU"
    except (OSError, AttributeError):
        return "NVIDIA GPU"


def refresh_gpu_status() -> GpuStatus:
    """Forget the cached answer of :func:`gpu_status` (e.g. after installing a GPU pack)."""
    global _gpu_cache
    _gpu_cache = None
    return gpu_status()


def gpu_status() -> GpuStatus:
    """Can transcription use the NVIDIA GPU? Cached after the first call."""
    global _gpu_cache
    if _gpu_cache is None:
        try:
            _gpu_cache = _compute_gpu_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("GPU detection failed: %s", exc, exc_info=True)
            _gpu_cache = GpuStatus(False, "", "The graphics card could not be checked; using "
                                              "the CPU (slower).")
    return _gpu_cache


def _compute_gpu_status() -> GpuStatus:
    try:
        ct2 = _import_ct2()
    except Exception as exc:  # noqa: BLE001
        log.info("ctranslate2 unavailable: %s", exc)
        return GpuStatus(False, "", "The speech engine isn't installed.")
    try:
        count = int(ct2.get_cuda_device_count())
    except Exception as exc:  # noqa: BLE001
        log.info("CUDA device query failed: %s", exc)
        count = 0
    if count <= 0:
        return GpuStatus(False, "", "No NVIDIA graphics card was found; using the CPU "
                                    "(slower).")
    name = _gpu_name()
    missing = [n for n in _CUDA_DLLS if _find_dll(n) is None]
    if missing:
        log.info("CUDA libraries missing: %s", ", ".join(missing))
        return GpuStatus(False, name, "Your NVIDIA GPU was found but its CUDA libraries are "
                                      "not installed; using the CPU (slower).")
    for n in _CUDA_DLLS:
        found = _find_dll(n)
        err = _try_load(found) if found else "not found"
        if err:
            log.info("CUDA library %s could not be loaded: %s", n, err)
            return GpuStatus(False, name, "Your NVIDIA GPU was found but its CUDA libraries "
                                          "could not be loaded; using the CPU (slower).")
    try:
        types = ct2.get_supported_compute_types("cuda")
    except Exception as exc:  # noqa: BLE001
        log.info("CUDA compute types unavailable: %s", exc)
        return GpuStatus(False, name, "Your NVIDIA GPU was found but isn't usable for "
                                      "captions; using the CPU (slower).")
    if "float16" not in types and "int8_float16" not in types:
        return GpuStatus(False, name, "Your NVIDIA GPU is too old for fast captions; using "
                                      "the CPU (slower).")
    return GpuStatus(True, name, "")


# ----------------------------------------------------------------- transcription
_LANGUAGES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "nl": "Dutch", "pl": "Polish", "ru": "Russian", "uk": "Ukrainian",
    "tr": "Turkish", "ar": "Arabic", "hi": "Hindi", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
    "cs": "Czech", "el": "Greek", "he": "Hebrew", "id": "Indonesian", "ro": "Romanian",
    "hu": "Hungarian", "vi": "Vietnamese", "th": "Thai",
}


def _language_name(code: str) -> str:
    return _LANGUAGES.get(code, code.upper() if code else "")


def _is_device_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(w in text for w in ("out of memory", "cuda", "cublas", "cudnn", "cannot be loaded",
                                   "not found or cannot", "device-side", "gpu"))


class _EngineMissing(Exception):
    pass


class TranscribeJob(QObject):
    """Transcribe an audio (or video) file into timed words on a background thread."""

    progress = Signal(float, str)
    finished = Signal(list)          # list[Word], seconds from the start of the audio
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, audio_path: str, model: str = "small", language: Optional[str] = None,
                 device: str = "auto", parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._audio = str(audio_path)
        self._model = model
        self._language = (language or None)
        self._device = device if device in ("auto", "cpu", "gpu") else "auto"
        self._cancel = CancelToken()
        self._thread: Optional[threading.Thread] = None
        self._last_pct = -1
        self._last_text = ""

    def start(self) -> None:
        if self.is_running():
            return
        self._cancel = CancelToken()
        self._last_pct = -1
        self._last_text = ""
        self._thread = threading.Thread(target=self._run, name="clipforge-transcribe",
                                        daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.cancel()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.is_running()

    # ------------------------------------------------------------------ worker
    def _emit(self, fraction: float, text: str) -> None:
        pct = int(max(0.0, min(1.0, fraction)) * 100)
        if text != self._last_text or pct != self._last_pct:
            self._last_pct = pct
            self._last_text = text
            self.progress.emit(fraction, text)

    def _run(self) -> None:
        outcome, message, words = "failed", "", []
        try:
            words = self._work()
            outcome = "finished"
        except Cancelled:
            outcome = "cancelled"
        except MediaError as exc:
            message = str(exc)
        except _EngineMissing:
            message = ("The caption engine isn't available in this copy of ClipForge. "
                       "Reinstall ClipForge to fix this.")
        except MemoryError:
            log.exception("Out of memory while transcribing")
            message = "There isn't enough memory to transcribe this audio. Try a shorter clip."
        except Exception as exc:  # noqa: BLE001
            log.exception("Transcription failed: %s", exc)
            message = "The captions could not be created. Details are in the log file."
        if outcome == "finished":
            self.finished.emit(words)
        elif outcome == "cancelled":
            self.cancelled.emit()
        else:
            self.failed.emit(message or "The captions could not be created.")

    def _work(self) -> list[Word]:
        info = _model_info(self._model)
        if not model_downloaded(self._model):
            raise MediaError(f"Download the {info.label} model first.")
        if not os.path.isfile(self._audio):
            raise MediaError("The audio file could not be found.")
        if not is_available():
            raise _EngineMissing()
        cancel = self._cancel
        self._emit(-1.0, "Reading the audio...")
        audio = self._decode()
        cancel.raise_if_cancelled()
        duration = len(audio) / SAMPLE_RATE
        if duration <= 0.05 or _is_silent(audio):
            self._emit(1.0, "No speech found")
            return []

        want_gpu = self._device == "gpu" or (self._device == "auto" and gpu_status().usable)
        if self._device == "gpu" and not gpu_status().usable:
            log.info("GPU requested but unusable (%s); using the CPU", gpu_status().reason)
            want_gpu = False
        if want_gpu:
            try:
                return self._transcribe(audio, duration, "cuda", "float16")
            except (Cancelled, MediaError):
                raise
            except Exception as exc:  # noqa: BLE001
                if not _is_device_error(exc):
                    raise
                log.warning("GPU transcription failed (%s); retrying once on the CPU", exc)
                self._emit(0.0, "The graphics card failed; switching to the CPU...")
        return self._transcribe(audio, duration, "cpu", "int8")

    def _decode(self) -> Any:
        """Mono 16 kHz float32 samples of the first audio stream, decoded by FFmpeg
        (faster-whisper's own decoder is not used: it breaks with newer PyAV releases)."""
        import numpy as np  # noqa: PLC0415
        from . import ffmpeg  # noqa: PLC0415
        cmd = [ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-i",
               self._audio, "-map", "0:a:0?", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
               "-f", "f32le", "-"]
        buf = bytearray()
        try:
            proc = ffmpeg.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.warning("Could not start ffmpeg: %s", exc)
            raise MediaError("The audio could not be read.") from exc
        try:
            assert proc.stdout is not None
            while True:
                if self._cancel.cancelled:
                    raise Cancelled()
                chunk = proc.stdout.read(1 << 20)
                if not chunk:
                    break
                buf += chunk
            rc = proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                ffmpeg._kill(proc)
            if proc.stdout:
                proc.stdout.close()
        if rc != 0 and not buf:
            log.warning("ffmpeg could not decode %s (exit %s)", self._audio, rc)
            raise MediaError("This file's audio couldn't be read. It may be damaged or in "
                             "an unsupported format.")
        usable = len(buf) - len(buf) % 4
        return np.frombuffer(bytes(memoryview(buf)[:usable]), dtype=np.float32)

    def _transcribe(self, audio: Any, duration: float, device: str, compute: str) -> list[Word]:
        from faster_whisper import WhisperModel  # noqa: PLC0415
        cancel = self._cancel
        label = MODELS[self._model].label.split(" (")[0]
        self._emit(-1.0, f"Loading the {label} model ({'GPU' if device == 'cuda' else 'CPU'})...")
        threads = min(8, max(2, (os.cpu_count() or 4) // 2)) if device == "cpu" else 0
        model = WhisperModel(str(_model_path(self._model)), device=device,
                             compute_type=compute, cpu_threads=threads, local_files_only=True)
        cancel.raise_if_cancelled()
        try:
            segments, info = model.transcribe(
                audio, language=self._language, beam_size=1, word_timestamps=True,
                vad_filter=True, condition_on_previous_text=False)
        except ValueError as exc:
            raise MediaError("That language isn't supported by the caption engine.") from exc
        lang = _language_name(getattr(info, "language", "") or self._language or "")
        where = "GPU" if device == "cuda" else "CPU"
        prefix = f"Transcribing {lang} ({where})" if lang else f"Transcribing ({where})"
        self._emit(0.0, prefix + "...")
        words: list[Word] = []
        for seg in segments:                      # lazy: decoding happens here
            cancel.raise_if_cancelled()
            for w in getattr(seg, "words", None) or []:
                text = (w.word or "").strip()
                if not text:
                    continue
                start = max(0.0, float(w.start))
                end = max(start, float(w.end))
                words.append(Word(start, end, text))
            frac = min(0.99, max(0.0, float(seg.end) / duration)) if duration > 0 else 0.0
            self._emit(frac, f"{prefix}... {int(frac * 100)}%")
        cancel.raise_if_cancelled()
        words.sort(key=lambda w: (w.start, w.end))
        self._emit(1.0, f"Found {len(words)} words")
        return words


def _is_silent(audio: Any) -> bool:
    try:
        import numpy as np  # noqa: PLC0415
        return float(np.max(np.abs(audio))) < 1e-4
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ word to lines
_SENTENCE_END = ".?!…"


def _has_alnum(text: str) -> bool:
    return any(c.isalnum() for c in text)


def words_to_lines(words: list[Word], max_words: int) -> list[CaptionLine]:
    """Caption lines of at most ``max_words`` words from a flat word list.

    Cleans the words first: leading/trailing spaces are removed, empty tokens dropped,
    punctuation-only tokens dropped (sentence-ending ones are attached to the previous word
    so line breaks still happen there), tokens like ``'s`` joined to the previous word.
    Lines are never shorter than 0.25 s: a short line is extended up to the next line."""
    clean: list[Word] = []
    for w in sorted(words, key=lambda x: (x.start, x.end)):
        text = (w.text or "").strip()
        if not text:
            continue
        if not _has_alnum(text):
            if clean and text[-1] in _SENTENCE_END and not clean[-1].text.endswith(
                    tuple(_SENTENCE_END)):
                prev = clean[-1]
                clean[-1] = Word(prev.start, max(prev.end, w.end), prev.text + text[-1])
            continue
        if clean and text[0] in "'’" and len(text) <= 4 and clean[-1].end >= w.start - 0.3:
            prev = clean[-1]
            clean[-1] = Word(prev.start, max(prev.end, w.end), prev.text + text)
            continue
        clean.append(Word(float(w.start), max(float(w.end), float(w.start)), text))

    lines = chunk_words(clean, max_words)
    for i, ln in enumerate(lines):
        if ln.end - ln.start < MIN_LINE_SECONDS:
            limit = lines[i + 1].start if i + 1 < len(lines) else ln.start + MIN_LINE_SECONDS
            ln.end = max(ln.end, min(ln.start + MIN_LINE_SECONDS, limit))
    return lines
