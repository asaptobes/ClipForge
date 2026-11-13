"""Safe in-app update of yt-dlp without reinstalling ClipForge.

yt-dlp is pure Python, so an update is simply a newer copy of the ``yt_dlp`` package
kept in a user folder (``%LOCALAPPDATA%/ClipForge/ytdlp-override``) that is imported
instead of the bundled one.

Layout of the override folder::

    ytdlp-override/
        override.json        marker: {"version", "sha256", "installed_at"}
        yt_dlp/              the extracted package (``version.py`` inside)

``sha256`` in the marker is a *tree digest* of the extracted files (relative paths and
contents, ``__pycache__`` ignored), so a half-written, edited or corrupted override is
detected at startup and ignored. Updates are built in ``ytdlp-override.new`` and swapped
into place only when complete (previous copy kept as ``.old`` until the swap worked).

Security: HTTPS only, hosts restricted to pypi.org / files.pythonhosted.org (redirects
included), SHA-256 from PyPI verified, zip members validated (no absolute paths, no
``..``, no symlinks, only ``yt_dlp/``), size capped. No user data is sent.

Import precedence (``activate_override``)
-----------------------------------------
* Normal Python: ``sys.path.insert(0, override_dir)`` is enough, ``PathFinder`` finds the
  override package first.
* PyInstaller: ``PyiFrozenImporter`` sits at the front of ``sys.meta_path`` and serves the
  embedded copy regardless of ``sys.path``. So when frozen we additionally install a
  meta-path finder at index 0 that answers ``yt_dlp`` and ``yt_dlp.*`` from the override
  folder only. The same finder logic is covered by a test that forces it on in a normal
  interpreter; the frozen build itself could not be exercised here.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from PySide6.QtCore import QObject, Signal

from .. import paths
from ..workers import CancelToken, Cancelled

log = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
USER_AGENT = "ClipForge-updater"
JSON_HOSTS = frozenset({"pypi.org"})
FILE_HOSTS = frozenset({"files.pythonhosted.org"})
ALLOWED_HOSTS = JSON_HOSTS | FILE_HOSTS
MAX_WHEEL_BYTES = 40 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_UNPACKED_BYTES = 120 * 1024 * 1024
MAX_MEMBERS = 5000
MARKER_NAME = "override.json"
_VERSION_RE = re.compile(r"^\d+(?:\.\d+){2,3}$")        # stable releases only
_VERSION_PY_RE = re.compile(r"""__version__\s*=\s*['"]([^'"]+)['"]""")


class UpdateError(Exception):
    """Internal: ``str(exc)`` is a friendly message suitable for the UI."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class LatestInfo:
    version: str
    wheel_url: str
    sha256: str
    size: int


# ------------------------------------------------------------------ locations
def override_dir() -> Path:
    """``<local data dir>/ytdlp-override`` (``%LOCALAPPDATA%\\ClipForge`` on Windows)."""
    return paths.local_data_dir() / "ytdlp-override"


def _sibling(suffix: str) -> Path:
    d = override_dir()
    return d.with_name(d.name + suffix)


# ------------------------------------------------------------------- versions
def _vkey(version: str) -> tuple[int, ...]:
    if not _VERSION_RE.match(version or ""):
        raise ValueError(f"not a stable version: {version!r}")
    return tuple(int(p) for p in version.split("."))


def _is_newer(candidate: str, than: str) -> bool:
    try:
        a, b = list(_vkey(candidate)), list(_vkey(than))
    except ValueError:
        return False
    n = max(len(a), len(b))
    a += [0] * (n - len(a))
    b += [0] * (n - len(b))
    return a > b


def _read_version_py(text: str) -> Optional[str]:
    m = _VERSION_PY_RE.search(text)
    return m.group(1) if m else None


_active_version: Optional[str] = None
_activated = False
_bundled_cache: Optional[str] = None


def _loaded_version() -> Optional[str]:
    mod = sys.modules.get("yt_dlp.version")
    v = getattr(mod, "__version__", None)
    return v if isinstance(v, str) else None


def _bundled_version() -> str:
    """Version of the bundled yt-dlp, without leaving ``yt_dlp`` imported when possible."""
    global _bundled_cache
    if _bundled_cache:
        return _bundled_cache
    version: Optional[str] = None
    if _active_version is None:                  # what is loaded IS the bundled copy
        version = _loaded_version()
    if not version:
        try:                                     # plain source install: read version.py
            spec = importlib.machinery.PathFinder.find_spec("yt_dlp")
            for loc in (spec.submodule_search_locations or []) if spec else []:
                p = Path(loc) / "version.py"
                if p.is_file():
                    version = _read_version_py(p.read_text(encoding="utf-8"))
                    break
        except Exception as exc:  # noqa: BLE001
            log.debug("bundled version via path failed: %s", exc)
    if not version:
        try:
            from importlib import metadata
            version = metadata.version("yt-dlp")
        except Exception as exc:  # noqa: BLE001
            log.debug("bundled version via metadata failed: %s", exc)
    if not version and "yt_dlp" not in sys.modules:   # frozen: import, read, forget
        try:
            import yt_dlp.version as v
            version = v.__version__
        except Exception as exc:  # noqa: BLE001
            log.debug("bundled version via import failed: %s", exc)
        finally:
            for name in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.")]:
                del sys.modules[name]
    _bundled_cache = version or "0"
    return _bundled_cache


def installed_version() -> str:
    """The yt-dlp version actually in use (override if active, else the bundled one)."""
    return _loaded_version() or _active_version or _bundled_version()


# ----------------------------------------------------------------- tree digest
def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in filenames:
            if fn.endswith((".pyc", ".pyo")):
                continue
            files.append(Path(dirpath) / fn)
    for f in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        rel = f.relative_to(root).as_posix().encode("utf-8")
        h.update(len(rel).to_bytes(4, "big") + rel)
        with open(f, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


_REQUIRED_FILES = ("__init__.py", "version.py", "extractor/__init__.py")


def _sanity_check(pkg: Path) -> None:
    for rel in _REQUIRED_FILES:
        if not (pkg / rel).is_file():
            raise UpdateError("The downloaded package is incomplete.", f"missing {rel}")


def _read_marker(base: Path) -> dict[str, Any]:
    with open(base / MARKER_NAME, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("marker is not an object")
    for key in ("version", "sha256", "installed_at"):
        if key not in data:
            raise ValueError(f"marker lacks {key}")
    if not isinstance(data["version"], str) or not isinstance(data["sha256"], str):
        raise ValueError("marker has wrong types")
    return data


def _validated_override(base: Path) -> Optional[str]:
    """Version of the override in ``base`` if it is complete, intact and unmodified."""
    try:
        marker = _read_marker(base)
        version = marker["version"]
        _vkey(version)
        pkg = base / "yt_dlp"
        text = (pkg / "version.py").read_text(encoding="utf-8")
        if _read_version_py(text) != version:
            raise ValueError("version.py does not match marker")
        _sanity_check(pkg)
        if _tree_digest(pkg) != marker["sha256"]:
            raise ValueError("content digest mismatch")
        return version
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UpdateError, UnicodeError) as exc:
        log.warning("Ignoring yt-dlp override in %s: %s", base, exc)
        return None


# ------------------------------------------------------------------ activation
class _OverrideFinder:
    """Meta-path finder serving ``yt_dlp`` and ``yt_dlp.*`` from the override folder only
    (needed when a frozen importer would otherwise win over ``sys.path``)."""

    def __init__(self, root: Path) -> None:
        self.root = str(root)

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "yt_dlp":
            return importlib.machinery.PathFinder.find_spec(fullname, [self.root])
        if fullname.startswith("yt_dlp.") and path:
            return importlib.machinery.PathFinder.find_spec(fullname, list(path))
        return None


def _install(base: Path, force_finder: bool = False) -> None:
    root = str(base)
    if root not in sys.path:
        sys.path.insert(0, root)
    if force_finder or getattr(sys, "frozen", False):
        if not any(isinstance(f, _OverrideFinder) and f.root == root for f in sys.meta_path):
            sys.meta_path.insert(0, _OverrideFinder(base))
    importlib.invalidate_caches()


def activate_override(_force_finder: bool = False) -> Optional[str]:
    """Make Python import ``yt_dlp`` from a validated, strictly newer override.

    Call once at startup before anything imports ``yt_dlp``. Returns the version
    activated or ``None``. Never raises: any problem means the bundled copy is used."""
    global _active_version, _activated
    if _activated:
        return _active_version
    _activated = True
    try:
        base = override_dir()
        old = _sibling(".old")
        if not base.exists() and old.is_dir():           # interrupted swap: roll back
            try:
                os.replace(old, base)
            except OSError as exc:
                log.warning("Could not restore previous yt-dlp override: %s", exc)
        if not base.is_dir():
            return None
        version = _validated_override(base)
        if not version:
            return None
        if "yt_dlp" in sys.modules:
            log.warning("yt-dlp already imported; override %s not activated", version)
            return None
        bundled = _bundled_version()
        if not _is_newer(version, bundled):
            log.info("yt-dlp override %s is not newer than bundled %s; ignoring",
                     version, bundled)
            return None
        _install(base, _force_finder)
        _active_version = version
        log.info("Using yt-dlp %s from %s (bundled %s)", version, base, bundled)
        return version
    except Exception as exc:  # noqa: BLE001 - startup must never fail because of this
        log.warning("yt-dlp override not activated: %s", exc)
        return None


def clear_override() -> None:
    """Remove the override folder (and any leftovers of an update); next start uses the
    bundled yt-dlp."""
    for p in (override_dir(), _sibling(".new"), _sibling(".old")):
        shutil.rmtree(p, ignore_errors=True)


# ------------------------------------------------------------------- network
class _HostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: frozenset[str]) -> None:
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_url(url: str, hosts: frozenset[str]) -> None:
    parts = urlparse(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in hosts \
            or parts.username or parts.password or parts.port not in (None, 443):
        raise UpdateError("The update server returned an unexpected address.",
                          f"rejected url {url!r}")


def _open(url: str, hosts: frozenset[str], timeout: float = 15):
    """Open ``url`` (https, allow-listed hosts incl. redirects). Tests replace this."""
    _check_url(url, hosts)
    opener = urllib.request.build_opener(_HostRedirect(hosts))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json, */*"})
    return opener.open(req, timeout=timeout)


def _net_error(exc: BaseException) -> UpdateError:
    return UpdateError("Couldn't reach the update server. Check your internet "
                       "connection and try again.", repr(exc))


def fetch_latest() -> LatestInfo:
    """Look up the newest stable yt-dlp wheel on PyPI. Raises ``UpdateError``."""
    try:
        with _open(PYPI_URL, JSON_HOSTS, 15) as resp:
            raw = resp.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise UpdateError("The update server sent an unexpected answer.", "json too big")
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except UpdateError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise _net_error(exc) from exc
    except ValueError as exc:
        raise UpdateError("The update server sent an unexpected answer.", repr(exc)) from exc
    return _parse_latest(data)


def _parse_latest(data: Any) -> LatestInfo:
    bad = UpdateError("The update server sent an unexpected answer.")
    try:
        info = data["info"]
        version = str(info["version"])
        if not _VERSION_RE.match(version):          # pre-release / dev / odd version
            raise UpdateError("No stable yt-dlp release was found.", f"version {version}")
        if info.get("yanked"):
            raise UpdateError("The newest yt-dlp release was withdrawn. Try again later.")
        want = f"yt_dlp-{version}-py3-none-any.whl"
        for f in data["urls"]:
            if f.get("filename") != want or f.get("packagetype") != "bdist_wheel":
                continue
            if f.get("yanked"):
                raise UpdateError("The newest yt-dlp release was withdrawn. Try again later.")
            url = str(f["url"])
            _check_url(url, ALLOWED_HOSTS)
            digest = str(f["digests"]["sha256"]).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise bad
            return LatestInfo(version=version, wheel_url=url, sha256=digest,
                              size=int(f.get("size") or 0))
    except UpdateError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise UpdateError("The update server sent an unexpected answer.", repr(exc)) from exc
    raise UpdateError("The update server sent an unexpected answer.", "wheel not listed")


# ------------------------------------------------------------- wheel handling
def _download_wheel(info: LatestInfo, dest: Path, cancel: CancelToken,
                    progress) -> None:
    if info.size > MAX_WHEEL_BYTES:
        raise UpdateError("The update is larger than expected, so it was not installed.",
                          f"size {info.size}")
    h = hashlib.sha256()
    total = 0
    try:
        with _open(info.wheel_url, FILE_HOSTS, 30) as resp, open(dest, "wb") as out:
            try:
                length = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError, AttributeError):
                length = 0
            if length > MAX_WHEEL_BYTES:
                raise UpdateError("The update is larger than expected, so it was not "
                                  "installed.", f"content-length {length}")
            expected = length or info.size
            while True:
                cancel.raise_if_cancelled()
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_WHEEL_BYTES:
                    raise UpdateError("The update is larger than expected, so it was not "
                                      "installed.", "download exceeded cap")
                h.update(chunk)
                out.write(chunk)
                if expected:
                    progress(min(0.9, 0.1 + 0.6 * total / expected), "Downloading update...")
    except UpdateError:
        raise
    except Cancelled:
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise _net_error(exc) from exc
    if h.hexdigest() != info.sha256:
        raise UpdateError("The download was corrupted, nothing was changed.",
                          f"sha256 {h.hexdigest()} != {info.sha256}")


def _check_member_name(name: str, version: str) -> Optional[str]:
    """Validate a wheel member; return ``"pkg"`` for files to extract, ``"skip"`` for
    harmless metadata, raise ``UpdateError`` for anything suspicious."""
    bad = UpdateError("The downloaded file is not a valid yt-dlp package, nothing was "
                      "changed.", f"bad member {name!r}")
    if not name or "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name) \
            or any(ord(c) < 32 for c in name):
        raise bad
    parts = name.split("/")
    if any(p in ("..", ".") for p in parts[:-1]) or parts[-1] == "..":
        raise bad
    top = parts[0]
    if top == "yt_dlp":
        return "pkg"
    if top in (f"yt_dlp-{version}.dist-info", f"yt_dlp-{version}.data"):
        return "skip"            # real wheels also carry man pages / completions here
    raise bad


def _extract_wheel(wheel: Path, version: str, target: Path, cancel: CancelToken) -> None:
    """Validate and extract the ``yt_dlp/`` tree of ``wheel`` into ``target``/yt_dlp."""
    invalid = UpdateError("The downloaded file is not a valid yt-dlp package, nothing was "
                          "changed.")
    try:
        zf = zipfile.ZipFile(wheel)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError("The download was corrupted, nothing was changed.",
                          repr(exc)) from exc
    with zf:
        infos = zf.infolist()
        if not infos or len(infos) > MAX_MEMBERS:
            raise invalid
        total = 0
        plan: list[tuple[zipfile.ZipInfo, str]] = []
        for zi in infos:
            kind = _check_member_name(zi.filename, version)
            mode = (zi.external_attr >> 16) & 0xFFFF
            if mode and stat.S_ISLNK(mode):
                raise UpdateError("The downloaded file is not a valid yt-dlp package, "
                                  "nothing was changed.", f"symlink {zi.filename!r}")
            total += zi.file_size
            if total > MAX_UNPACKED_BYTES:
                raise UpdateError("The update is larger than expected, so it was not "
                                  "installed.", "unpacked size")
            plan.append((zi, kind or "skip"))
        # the version the package claims must be the one PyPI advertised
        try:
            vtext = zf.read("yt_dlp/version.py").decode("utf-8", errors="replace")
        except KeyError:
            raise invalid from None
        if _read_version_py(vtext) != version:
            raise UpdateError("The downloaded package is not the expected yt-dlp version, "
                              "nothing was changed.", "version.py mismatch")
        root = target.resolve()
        for zi, kind in plan:
            cancel.raise_if_cancelled()
            if kind != "pkg" or zi.is_dir():
                continue
            out = (target / zi.filename).resolve()
            if root not in out.parents:
                raise invalid
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(zi) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)


def _install_tree(version: str, wheel: Path, cancel: CancelToken, progress) -> None:
    new, old, cur = _sibling(".new"), _sibling(".old"), override_dir()
    shutil.rmtree(new, ignore_errors=True)
    try:
        new.mkdir(parents=True)
        progress(0.8, "Unpacking update...")
        _extract_wheel(wheel, version, new, cancel)
        _sanity_check(new / "yt_dlp")
        marker = {"version": version, "sha256": _tree_digest(new / "yt_dlp"),
                  "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        mtmp = new / (MARKER_NAME + ".tmp")
        mtmp.write_text(json.dumps(marker, indent=2), encoding="utf-8")
        os.replace(mtmp, new / MARKER_NAME)
        if _validated_override(new) != version:       # final self-check before the swap
            raise UpdateError("The downloaded package could not be verified, nothing was "
                              "changed.")
        cancel.raise_if_cancelled()
        progress(0.95, "Installing update...")
        shutil.rmtree(old, ignore_errors=True)
        had_old = cur.exists()
        try:
            if had_old:
                os.replace(cur, old)
            os.replace(new, cur)
        except OSError as exc:
            if had_old and not cur.exists() and old.exists():
                try:
                    os.replace(old, cur)               # put the previous override back
                except OSError:
                    log.error("Could not restore previous yt-dlp override")
            raise UpdateError("Couldn't install the update. Close other programs using "
                              "ClipForge's data folder and try again.", repr(exc)) from exc
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(new, ignore_errors=True)


def _pending_version() -> Optional[str]:
    """Version of a valid override already on disk (maybe waiting for a restart)."""
    return _validated_override(override_dir()) if override_dir().is_dir() else None


# ----------------------------------------------------------------------- job
class UpdateJob(QObject):
    """Check PyPI and install a newer yt-dlp into the override folder (background thread).

    ``finished(version, changed)``: ``changed`` False = already up to date (``version`` is
    the current one); True = installed, active after restarting ClipForge."""

    progress = Signal(float, str)
    finished = Signal(str, bool)
    failed = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._cancel = CancelToken()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._cancel = CancelToken()
            self._thread = threading.Thread(target=self._run, name="ytdlp-update",
                                            daemon=True)
            self._thread.start()

    def cancel(self) -> None:
        self._cancel.cancel()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _emit_progress(self, fraction: float, text: str) -> None:
        self.progress.emit(fraction, text)

    def _run(self) -> None:
        # Work first, clean up, and only then report: listeners must never see a
        # result while temp files are still around.
        tmpdir = tempfile.mkdtemp(prefix="clipforge-ytdlp-")
        outcome: tuple
        try:
            outcome = self._work(Path(tmpdir))
        except Cancelled:
            outcome = ("failed", "The update was cancelled. Nothing was changed.")
        except UpdateError as exc:
            log.error("yt-dlp update failed: %s %s", exc, exc.detail)
            outcome = ("failed", str(exc))
        except Exception as exc:  # noqa: BLE001
            log.error("yt-dlp update failed: %s", exc, exc_info=True)
            outcome = ("failed", "The update failed and nothing was changed. "
                                 "Details are in the log file.")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if outcome[0] == "finished":
            self.finished.emit(outcome[1], outcome[2])
        else:
            self.failed.emit(outcome[1])

    def _work(self, tmpdir: Path) -> tuple:
        cancel = self._cancel
        self._emit_progress(-1.0, "Checking for updates...")
        latest = fetch_latest()
        cancel.raise_if_cancelled()
        current = installed_version()
        pending = _pending_version()
        if pending and _is_newer(pending, current) and not _is_newer(latest.version, pending):
            return ("finished", pending, True)           # installed already, restart due
        if not _is_newer(latest.version, current):
            return ("finished", current, False)
        wheel = tmpdir / f"yt_dlp-{latest.version}-py3-none-any.whl"
        self._emit_progress(0.1, "Downloading update...")
        _download_wheel(latest, wheel, cancel, self._emit_progress)
        cancel.raise_if_cancelled()
        self._emit_progress(0.75, "Verifying update...")
        _install_tree(latest.version, wheel, cancel, self._emit_progress)
        self._emit_progress(1.0, "Update installed")
        return ("finished", latest.version, True)
