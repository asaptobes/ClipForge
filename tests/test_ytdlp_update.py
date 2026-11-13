"""yt-dlp updater tests - no network: ``ytdlp_update._open`` is replaced by fakes, the
override folder lives under a temporary ``LOCALAPPDATA``, and import-precedence checks run
in isolated ``python -I`` subprocesses so this process never imports a fake ``yt_dlp``."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

import pytest

from clipforge.media import ytdlp_update as upd
from clipforge.media.ytdlp_update import LatestInfo, UpdateError, UpdateJob

ROOT = Path(__file__).resolve().parent.parent
NEW = "2099.1.1"
WHEEL_URL = f"https://files.pythonhosted.org/packages/ab/cd/yt_dlp-{NEW}-py3-none-any.whl"


# ------------------------------------------------------------------ helpers
@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    tmp = tmp_path / "systmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    monkeypatch.setattr(upd, "_active_version", None)
    monkeypatch.setattr(upd, "_activated", False)
    return tmp_path


def package_files(version=NEW, extra=None):
    files = {
        "yt_dlp/__init__.py": b"# fake yt-dlp\n",
        "yt_dlp/version.py": f"__version__ = '{version}'\n".encode(),
        "yt_dlp/YoutubeDL.py": b"class YoutubeDL: pass\n",
        "yt_dlp/extractor/__init__.py": b"",
        f"yt_dlp-{version}.dist-info/METADATA": b"Name: yt-dlp\n",
        f"yt_dlp-{version}.dist-info/RECORD": b"",
    }
    files.update(extra or {})
    return files


def make_wheel(files=None, version=NEW) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in (files if files is not None else package_files(version)).items():
            zf.writestr(name, data)
    return buf.getvalue()


def pypi_json(version=NEW, wheel=b"", url=WHEEL_URL, sha=None, size=None, yanked=False,
              filename=None, extra_urls=None):
    return {"info": {"version": version, "yanked": False},
            "urls": (extra_urls or []) + [{
                "filename": filename or f"yt_dlp-{version}-py3-none-any.whl",
                "packagetype": "bdist_wheel", "url": url, "yanked": yanked,
                "size": len(wheel) if size is None else size,
                "digests": {"sha256": sha or hashlib.sha256(wheel).hexdigest()}}]}


class FakeResp:
    def __init__(self, data: bytes, headers=None, on_read=None):
        self._b = io.BytesIO(data)
        self.headers = headers or {}
        self._on_read = on_read

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if self._on_read:
            self._on_read()
        return self._b.read(n)


def install_fakes(monkeypatch, meta, wheel=b"", headers=None, on_read=None, calls=None):
    def fake_open(url, hosts, timeout=15):
        if calls is not None:
            calls.append((url, hosts))
        if url == upd.PYPI_URL:
            if isinstance(meta, BaseException):
                raise meta
            return FakeResp(json.dumps(meta).encode())
        return FakeResp(wheel, headers, on_read)
    monkeypatch.setattr(upd, "_open", fake_open)


def run_job(qtbot, expect="finished"):
    job = UpdateJob()
    progress = []
    job.progress.connect(lambda f, t: progress.append((f, t)))
    sig = job.finished if expect == "finished" else job.failed
    with qtbot.waitSignal(sig, timeout=20000) as blocker:
        job.start()
    return blocker.args, progress, job


def write_override(version=NEW, files=None, marker=None, base=None):
    base = base or upd.override_dir()
    for name, data in (files or package_files(version)).items():
        if name.startswith("yt_dlp/"):
            p = base / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    m = {"version": version, "sha256": upd._tree_digest(base / "yt_dlp"),
         "installed_at": "2026-01-01T00:00:00"}
    m.update(marker or {})
    (base / upd.MARKER_NAME).write_text(json.dumps(m), encoding="utf-8")
    return base


PROBE = (
    "import sys; sys.path.insert(0, {root!r})\n"
    "from clipforge.media import ytdlp_update as u\n"
    "r = u.activate_override({force})\n"
    "import yt_dlp, yt_dlp.version, yt_dlp.extractor\n"
    "print(r); print(yt_dlp.version.__version__); print(yt_dlp.__file__);"
    " print(yt_dlp.extractor.__file__); print(u.installed_version())\n"
    "print(type(sys.meta_path[0]).__name__)\n"
)


def probe(force=False):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    out = subprocess.run([sys.executable, "-I", "-c", PROBE.format(root=str(ROOT), force=force)],
                         capture_output=True, text=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
    assert out.returncode == 0, out.stderr
    r, ver, f, ext, inst, first = out.stdout.split("\n")[:6]
    return r, ver, f, ext, inst, first


def uses_override(result) -> bool:
    return result[1] == NEW and str(upd.override_dir()).lower() in result[2].lower()


def uses_bundled(result) -> bool:
    return result[0] == "None" and result[1] != NEW and "ytdlp-override" not in result[2]


# ----------------------------------------------------------------- fetch_latest
def test_fetch_latest_parses_pypi(monkeypatch):
    wheel = make_wheel()
    extra = [{"filename": f"yt_dlp-{NEW}.tar.gz", "packagetype": "sdist",
              "url": "https://files.pythonhosted.org/x.tar.gz", "digests": {"sha256": "0" * 64}}]
    install_fakes(monkeypatch, pypi_json(wheel=wheel, extra_urls=extra))
    got = upd.fetch_latest()
    assert got == LatestInfo(NEW, WHEEL_URL, hashlib.sha256(wheel).hexdigest(), len(wheel))


def test_fetch_latest_uses_only_pypi_host_and_no_user_data(monkeypatch):
    calls = []
    install_fakes(monkeypatch, pypi_json(wheel=b"x"), calls=calls)
    upd.fetch_latest()
    assert calls == [(upd.PYPI_URL, upd.JSON_HOSTS)]
    assert upd.PYPI_URL == "https://pypi.org/pypi/yt-dlp/json"
    assert upd.USER_AGENT == "ClipForge-updater"


@pytest.mark.parametrize("version", ["2099.1.1rc1", "2099.1.1.dev0", "2099.1.1.post1",
                                     "2099.1.1b2", "garbage", ""])
def test_fetch_latest_rejects_prereleases(monkeypatch, version):
    install_fakes(monkeypatch, pypi_json(version=version, wheel=b"x"))
    with pytest.raises(UpdateError):
        upd.fetch_latest()


def test_fetch_latest_rejects_yanked(monkeypatch):
    install_fakes(monkeypatch, pypi_json(wheel=b"x", yanked=True))
    with pytest.raises(UpdateError):
        upd.fetch_latest()
    meta = pypi_json(wheel=b"x")
    meta["info"]["yanked"] = True
    install_fakes(monkeypatch, meta)
    with pytest.raises(UpdateError):
        upd.fetch_latest()


@pytest.mark.parametrize("url", [
    "http://files.pythonhosted.org/yt_dlp.whl",
    "https://evil.com/yt_dlp.whl",
    "https://files.pythonhosted.org.evil.com/yt_dlp.whl",
    "https://user:pw@files.pythonhosted.org/yt_dlp.whl",
    "https://files.pythonhosted.org:8443/yt_dlp.whl",
    "ftp://files.pythonhosted.org/yt_dlp.whl",
])
def test_fetch_latest_rejects_foreign_urls(monkeypatch, url):
    install_fakes(monkeypatch, pypi_json(wheel=b"x", url=url))
    with pytest.raises(UpdateError):
        upd.fetch_latest()


def test_fetch_latest_rejects_missing_wheel_and_bad_digest_and_junk(monkeypatch):
    install_fakes(monkeypatch, pypi_json(wheel=b"x", filename="yt_dlp-0.0.1-py3-none-any.whl"))
    with pytest.raises(UpdateError):
        upd.fetch_latest()
    install_fakes(monkeypatch, pypi_json(wheel=b"x", sha="nothex"))
    with pytest.raises(UpdateError):
        upd.fetch_latest()
    install_fakes(monkeypatch, {"unexpected": True})
    with pytest.raises(UpdateError):
        upd.fetch_latest()


def test_fetch_latest_network_error_is_friendly(monkeypatch):
    import urllib.error
    install_fakes(monkeypatch, urllib.error.URLError("getaddrinfo failed"))
    with pytest.raises(UpdateError, match="Couldn't reach the update server") as ei:
        upd.fetch_latest()
    assert "getaddrinfo" not in str(ei.value)


def test_open_enforces_https_and_hosts():
    with pytest.raises(UpdateError):
        upd._open("http://pypi.org/pypi/yt-dlp/json", upd.JSON_HOSTS)
    with pytest.raises(UpdateError):
        upd._open("https://evil.com/x", upd.FILE_HOSTS)
    with pytest.raises(UpdateError):
        upd._open("https://pypi.org/x", upd.FILE_HOSTS)


def test_redirects_only_to_allowed_hosts():
    import urllib.request
    handler = upd._HostRedirect(upd.FILE_HOSTS)
    req = urllib.request.Request("https://files.pythonhosted.org/a")
    for bad in ("https://evil.com/a", "http://files.pythonhosted.org/a",
                "https://pypi.org/a"):
        with pytest.raises(UpdateError):
            handler.redirect_request(req, None, 302, "Found", {}, bad)
    ok = handler.redirect_request(req, None, 302, "Found", {},
                                  "https://files.pythonhosted.org/b")
    assert ok is not None and ok.full_url == "https://files.pythonhosted.org/b"


# --------------------------------------------------------------------- UpdateJob
def test_up_to_date(qtbot, monkeypatch):
    cur = upd.installed_version()
    install_fakes(monkeypatch, pypi_json(version=cur, wheel=b"x"))
    (version, changed), _, _ = run_job(qtbot)
    assert (version, changed) == (cur, False)
    assert not upd.override_dir().exists()


def test_older_latest_is_up_to_date(qtbot, monkeypatch):
    monkeypatch.setattr(upd, "installed_version", lambda: "2099.1.1")
    install_fakes(monkeypatch, pypi_json(version="2098.5.5", wheel=b"x"))
    (version, changed), _, _ = run_job(qtbot)
    assert (version, changed) == ("2099.1.1", False)


def test_successful_update(qtbot, monkeypatch, isolated):
    wheel = make_wheel(package_files(extra={
        f"yt_dlp-{NEW}.data/data/share/man/man1/yt-dlp.1": b".TH x\n"}))
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel)
    (version, changed), progress, _ = run_job(qtbot)
    assert (version, changed) == (NEW, True)
    d = upd.override_dir()
    assert (d / "yt_dlp" / "version.py").read_text().strip() == f"__version__ = '{NEW}'"
    assert (d / "yt_dlp" / "extractor" / "__init__.py").is_file()
    assert not (d / "yt_dlp-" f"{NEW}.data").exists() and not (d / "share").exists()
    marker = json.loads((d / upd.MARKER_NAME).read_text())
    assert marker["version"] == NEW and marker["installed_at"]
    assert marker["sha256"] == upd._tree_digest(d / "yt_dlp")
    assert upd._validated_override(d) == NEW
    assert not Path(str(d) + ".new").exists() and not Path(str(d) + ".old").exists()
    assert list(Path(tempfile.gettempdir()).iterdir()) == []         # temp wheel removed
    fractions = [f for f, _ in progress]
    assert max(fractions) == 1.0 and any(0 < f < 1 for f in fractions)


def test_update_replaces_older_override_and_cleans_old(qtbot, monkeypatch):
    write_override("2098.1.1")
    wheel = make_wheel()
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel)
    (version, changed), _, _ = run_job(qtbot)
    assert (version, changed) == (NEW, True)
    assert upd._validated_override(upd.override_dir()) == NEW
    assert not Path(str(upd.override_dir()) + ".old").exists()


def test_pending_override_is_not_downloaded_again(qtbot, monkeypatch):
    write_override()
    calls = []
    install_fakes(monkeypatch, pypi_json(wheel=b"x"), calls=calls)
    (version, changed), _, _ = run_job(qtbot)
    assert (version, changed) == (NEW, True)
    assert all(u == upd.PYPI_URL for u, _ in calls)                  # no wheel download


def test_failed_swap_restores_previous_override(qtbot, monkeypatch):
    write_override("2098.1.1")
    wheel = make_wheel()
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel)
    real = os.replace
    d = upd.override_dir()

    def flaky(src, dst):
        if str(dst) == str(d) and str(src).endswith(".new"):
            raise PermissionError("locked")
        return real(src, dst)
    monkeypatch.setattr(os, "replace", flaky)
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "install" in msg.lower()
    assert upd._validated_override(d) == "2098.1.1"                  # previous one intact
    assert not Path(str(d) + ".new").exists()


def test_hash_mismatch_rejected(qtbot, monkeypatch):
    wheel = make_wheel()
    install_fakes(monkeypatch, pypi_json(wheel=wheel, sha="0" * 64), wheel)
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "corrupted" in msg and "nothing was changed" in msg
    assert not upd.override_dir().exists()
    assert list(Path(tempfile.gettempdir()).iterdir()) == []


def test_tampered_download_rejected(qtbot, monkeypatch):
    wheel = make_wheel()
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel[:20] + bytes([wheel[20] ^ 0xFF]) + wheel[21:])
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "nothing was changed" in msg and not upd.override_dir().exists()


@pytest.mark.parametrize("evil", [
    "yt_dlp/../../evil.py", "../evil.py", "/abs/evil.py", "C:/evil.py",
    "yt_dlp/../evil.py", "yt_dlp\\..\\evil.py", "./yt_dlp/x.py",
])
def test_zip_slip_rejected(qtbot, monkeypatch, isolated, evil):
    wheel = make_wheel(package_files(extra={evil: b"boom"}))
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel)
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "not a valid yt-dlp package" in msg
    assert not upd.override_dir().exists()
    assert not (isolated / "evil.py").exists() and not (isolated / "local" / "evil.py").exists()


def test_symlink_member_rejected(qtbot, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, data in package_files().items():
            zf.writestr(n, data)
        zi = zipfile.ZipInfo("yt_dlp/link")
        zi.external_attr = (0o120777 << 16)
        zf.writestr(zi, "/etc/passwd")
    wheel = buf.getvalue()
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel)
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "not a valid yt-dlp package" in msg and not upd.override_dir().exists()


@pytest.mark.parametrize("files", [
    {"evilpkg/__init__.py": b"", "evilpkg/version.py": b"__version__ = '2099.1.1'\n"},
    {**package_files(), "other_pkg/x.py": b"print('hi')"},
    {**package_files(), "sitecustomize.py": b"print('hi')"},
    {k: v for k, v in package_files().items() if k != "yt_dlp/version.py"},
    package_files(version="2000.1.1"),                       # version.py lies about version
    {k: v for k, v in package_files().items() if k != "yt_dlp/extractor/__init__.py"},
], ids=["wrong-package", "extra-package", "extra-toplevel", "no-version", "wrong-version",
        "incomplete"])
def test_wrong_package_rejected(qtbot, monkeypatch, files):
    if files.get("yt_dlp/version.py", b"").startswith(b"__version__ = '2000"):
        files = {k.replace("2000.1.1", NEW): v for k, v in files.items()}
    wheel = make_wheel(files)
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel)
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "nothing was changed" in msg or "incomplete" in msg
    assert not upd.override_dir().exists()
    assert not Path(str(upd.override_dir()) + ".new").exists()


def test_not_a_zip_rejected(qtbot, monkeypatch):
    junk = b"this is not a zip file" * 10
    install_fakes(monkeypatch, pypi_json(wheel=junk), junk)
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "corrupted" in msg and not upd.override_dir().exists()


def test_oversize_by_metadata_rejected(qtbot, monkeypatch):
    calls = []
    install_fakes(monkeypatch, pypi_json(wheel=b"x", size=upd.MAX_WHEEL_BYTES + 1),
                  b"x", calls=calls)
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "larger than expected" in msg
    assert all(u == upd.PYPI_URL for u, _ in calls)


def test_oversize_by_content_length_rejected(qtbot, monkeypatch):
    wheel = make_wheel()
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel,
                  headers={"Content-Length": str(upd.MAX_WHEEL_BYTES + 1)})
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "larger than expected" in msg and not upd.override_dir().exists()


def test_oversize_stream_rejected(qtbot, monkeypatch):
    wheel = make_wheel(package_files(extra={"yt_dlp/big.bin": os.urandom(300_000)}))
    monkeypatch.setattr(upd, "MAX_WHEEL_BYTES", 100_000)
    install_fakes(monkeypatch, pypi_json(wheel=wheel, size=1), wheel)   # lying metadata
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "larger than expected" in msg
    assert list(Path(tempfile.gettempdir()).iterdir()) == []


def test_unpacked_size_bomb_rejected(qtbot, monkeypatch):
    wheel = make_wheel(package_files(extra={"yt_dlp/zeros.bin": b"\0" * 5_000_000}))
    monkeypatch.setattr(upd, "MAX_UNPACKED_BYTES", 1_000_000)
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel)
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "larger than expected" in msg and not upd.override_dir().exists()


def test_network_failure_during_download(qtbot, monkeypatch):
    import urllib.error
    wheel = make_wheel()

    def boom():
        raise urllib.error.URLError("connection reset")
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel, on_read=boom)
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "Couldn't reach the update server" in msg and "reset" not in msg
    assert list(Path(tempfile.gettempdir()).iterdir()) == []


def test_unreachable_server(qtbot, monkeypatch):
    install_fakes(monkeypatch, OSError("no route"))
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "Couldn't reach the update server" in msg


def test_unexpected_exception_is_friendly(qtbot, monkeypatch):
    def explode():
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(upd, "fetch_latest", explode)
    (msg,), _, _ = run_job(qtbot, "failed")
    assert "secret" not in msg and msg


def test_cancel_during_download(qtbot, monkeypatch):
    wheel = make_wheel(package_files(extra={"yt_dlp/big.bin": os.urandom(400_000)}))
    job = UpdateJob()
    started = threading.Event()

    def on_read():
        started.set()
        job.cancel()
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel, on_read=on_read)
    with qtbot.waitSignal(job.failed, timeout=20000) as sig:
        job.start()
    assert started.is_set() and "cancelled" in sig.args[0].lower()
    assert not upd.override_dir().exists()
    assert not Path(str(upd.override_dir()) + ".new").exists()
    assert list(Path(tempfile.gettempdir()).iterdir()) == []


def test_job_restartable_after_failure(qtbot, monkeypatch):
    wheel = make_wheel()
    install_fakes(monkeypatch, pypi_json(wheel=wheel, sha="0" * 64), wheel)
    _, _, job = run_job(qtbot, "failed")
    job._thread.join(5)
    install_fakes(monkeypatch, pypi_json(wheel=wheel), wheel)
    with qtbot.waitSignal(job.finished, timeout=20000) as sig:
        job.start()
    assert sig.args == [NEW, True]


# ------------------------------------------------- clear_override / sanity / marker
def test_clear_override_removes_everything():
    write_override()
    d = upd.override_dir()
    Path(str(d) + ".new").mkdir()
    Path(str(d) + ".old").mkdir()
    upd.clear_override()
    assert not d.exists() and not Path(str(d) + ".new").exists()
    assert not Path(str(d) + ".old").exists()
    upd.clear_override()                                # idempotent


def test_override_dir_under_local_appdata(isolated):
    assert upd.override_dir() == isolated / "local" / "ClipForge" / "ytdlp-override"


def test_installed_version_reports_loaded_ytdlp():
    import yt_dlp.version
    assert upd.installed_version() == yt_dlp.version.__version__


def test_version_comparison():
    assert upd._is_newer("2026.10.27", "2026.10.26")
    assert upd._is_newer("2026.11.1", "2026.10.26")
    assert upd._is_newer("2026.3.17.232650", "2026.3.17")
    assert not upd._is_newer("2026.10.26", "2026.10.26")
    assert not upd._is_newer("2025.1.1", "2026.10.26")
    assert not upd._is_newer("2099.1.1rc1", "2026.10.26")


def test_validated_override_detects_changes():
    base = write_override()
    assert upd._validated_override(base) == NEW
    (base / "yt_dlp" / "__pycache__").mkdir()
    (base / "yt_dlp" / "__pycache__" / "x.pyc").write_bytes(b"cache")
    assert upd._validated_override(base) == NEW                       # caches don't matter
    (base / "yt_dlp" / "YoutubeDL.py").write_bytes(b"import os; os.system('x')\n")
    assert upd._validated_override(base) is None                      # tampered
    assert upd._validated_override(base / "nope") is None


# --------------------------------------------------------- activation (subprocess)
def test_activation_prefers_newer_valid_override():
    write_override()
    r = probe()
    assert r[0] == NEW and uses_override(r), r
    assert r[4] == NEW                                       # installed_version() agrees
    assert Path(r[3]).parent.parent.parent == upd.override_dir()    # submodules too


def test_no_override_uses_bundled():
    r = probe()
    assert uses_bundled(r), r


@pytest.mark.parametrize("version", ["2000.1.1", "2026.10.26"])
def test_activation_ignores_older_or_equal(version):
    write_override(version)
    r = probe()
    assert r[0] == "None" and r[1] != NEW and "ytdlp-override" not in r[2], r


def test_activation_ignores_corrupt_override():
    base = write_override()
    (base / "yt_dlp" / "YoutubeDL.py").write_bytes(b"class YoutubeDL: tampered = True\n")
    assert uses_bundled(probe())


def test_activation_ignores_wrong_digest():
    write_override(marker={"sha256": "0" * 64})
    assert uses_bundled(probe())


@pytest.mark.parametrize("marker_text", ["", "not json", "[]", "{}",
                                         json.dumps({"version": NEW}),
                                         json.dumps({"version": 5, "sha256": 1,
                                                     "installed_at": 2})])
def test_activation_ignores_bad_markers(marker_text):
    base = write_override()
    (base / upd.MARKER_NAME).write_text(marker_text, encoding="utf-8")
    assert uses_bundled(probe())


def test_activation_ignores_missing_marker():
    base = write_override()
    (base / upd.MARKER_NAME).unlink()
    assert uses_bundled(probe())


def test_activation_ignores_marker_version_mismatch():
    # marker claims 2099.1.1 but version.py says something else
    files = package_files("2098.2.2")
    write_override(version=NEW, files=files)
    assert uses_bundled(probe())


def test_activation_ignores_incomplete_package():
    files = {k: v for k, v in package_files().items() if k != "yt_dlp/extractor/__init__.py"}
    write_override(files=files)
    assert uses_bundled(probe())


def test_activation_rolls_back_interrupted_swap():
    base = write_override()
    os.replace(base, Path(str(base) + ".old"))               # crashed between the renames
    r = probe()
    assert r[0] == NEW and uses_override(r), r


def test_frozen_style_finder_gives_override_precedence():
    write_override()
    r = probe(force=True)
    assert r[0] == NEW and uses_override(r), r
    assert r[5] == "_OverrideFinder"                         # sits first on sys.meta_path
    assert Path(r[3]).parent.parent.parent == upd.override_dir()


def test_override_finder_only_serves_ytdlp():
    base = write_override()
    finder = upd._OverrideFinder(base)
    assert finder.find_spec("json") is None
    assert finder.find_spec("yt_dlp_other") is None
    assert finder.find_spec("yt_dlp.version") is None        # needs the parent's __path__
    spec = finder.find_spec("yt_dlp")
    assert spec is not None and Path(spec.origin).parent.parent == base
    sub = finder.find_spec("yt_dlp.version", list(spec.submodule_search_locations))
    assert sub is not None and Path(sub.origin).parent == base / "yt_dlp"


def test_activate_override_never_raises(monkeypatch):
    monkeypatch.setattr(upd, "override_dir", lambda: (_ for _ in ()).throw(OSError("boom")))
    assert upd.activate_override() is None
