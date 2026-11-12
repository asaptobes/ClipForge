"""Filesystem locations, correct in both development and PyInstaller-frozen mode."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

APP_DIR_NAME = "ClipForge"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Folder holding bundled read-only data (``_MEIPASS``/``_internal`` when frozen)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """Path of a file under ``clipforge/resources``."""
    return bundle_dir() / "clipforge" / "resources" / Path(*parts)


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return _ensure(Path(base) / APP_DIR_NAME)


def local_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return _ensure(Path(base) / APP_DIR_NAME)


def log_dir() -> Path:
    return _ensure(local_data_dir() / "logs")


def default_cache_dir() -> Path:
    return local_data_dir() / "cache"


def default_temp_dir() -> Path:
    return local_data_dir() / "temp"


def default_download_dir() -> Path:
    return Path.home() / "Videos" / APP_DIR_NAME / "Downloads"


def default_export_dir() -> Path:
    return Path.home() / "Videos" / APP_DIR_NAME / "Exports"


def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def ensure_dir(p: str | os.PathLike) -> Path:
    return _ensure(Path(p))


def find_tool(name: str) -> Optional[str]:
    """Locate ``ffmpeg``/``ffprobe``: bundled copy first, then ``vendor/``, then PATH."""
    exe = f"{name}.exe" if os.name == "nt" else name
    candidates = [
        bundle_dir() / "ffmpeg" / exe,                 # packaged build
        Path(sys.executable).parent / "ffmpeg" / exe,  # next to the exe
        bundle_dir() / "vendor" / "ffmpeg" / exe,      # development checkout
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which(name)
