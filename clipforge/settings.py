"""Persistent user settings (JSON in ``%APPDATA%/ClipForge/settings.json``)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import paths

log = logging.getLogger(__name__)

RESOLUTION_CHOICES = ["best", "2160", "1440", "1080", "720", "480"]
CODEC_CHOICES = ["h264", "h265"]
HW_ACCEL_CHOICES = ["auto", "on", "off"]
THEME_CHOICES = ["dark", "midnight", "light"]


@dataclass
class Settings:
    download_dir: str = field(default_factory=lambda: str(paths.default_download_dir()))
    export_dir: str = field(default_factory=lambda: str(paths.default_export_dir()))
    preferred_resolution: str = "1080"
    export_codec: str = "h264"
    hw_accel: str = "auto"
    theme: str = "dark"
    cache_dir: str = field(default_factory=lambda: str(paths.default_cache_dir()))
    temp_dir: str = field(default_factory=lambda: str(paths.default_temp_dir()))
    # window state (not shown in the settings dialog)
    recent_projects: list[str] = field(default_factory=list)
    window_geometry: str = ""
    window_state: str = ""
    splitter_main: str = ""
    splitter_vertical: str = ""
    volume: float = 1.0
    show_support_prompt: bool = True

    _path: Optional[Path] = field(default=None, repr=False, compare=False)

    @classmethod
    def default_path(cls) -> Path:
        return paths.app_data_dir() / "settings.json"

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        path = Path(path) if path else cls.default_path()
        s = cls()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("settings root is not an object")
            for key, value in data.items():
                if key in cls.__dataclass_fields__ and not key.startswith("_"):
                    expected = type(getattr(s, key))
                    if expected is float and isinstance(value, int) \
                            and not isinstance(value, bool):
                        value = float(value)          # JSON "1" for a float setting
                    if isinstance(value, expected) and not (
                            expected is not bool and isinstance(value, bool)):
                        setattr(s, key, value)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("Settings file unreadable, using defaults: %s", exc)
        s._path = path
        s._sanitize()
        return s

    def _sanitize(self) -> None:
        if self.preferred_resolution not in RESOLUTION_CHOICES:
            self.preferred_resolution = "1080"
        if self.export_codec not in CODEC_CHOICES:
            self.export_codec = "h264"
        if self.hw_accel not in HW_ACCEL_CHOICES:
            self.hw_accel = "auto"
        if self.theme not in THEME_CHOICES:
            self.theme = "dark"
        self.volume = min(1.0, max(0.0, float(self.volume))) if self.volume == self.volume else 1.0
        self.recent_projects = [p for p in self.recent_projects if isinstance(p, str) and p][:10]

    def save(self) -> None:
        path = self._path or self.default_path()
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            log.error("Could not save settings: %s", exc)

    def add_recent(self, project_path: str) -> None:
        project_path = os.path.abspath(project_path)
        key = os.path.normcase(project_path)          # Windows paths are case-insensitive
        self.recent_projects = [project_path] + [
            p for p in self.recent_projects
            if os.path.normcase(os.path.abspath(p)) != key]
        del self.recent_projects[10:]

    # resolved, existing folders -------------------------------------------------
    def thumbnail_cache_dir(self) -> Path:
        return paths.ensure_dir(Path(self.cache_dir) / "thumbnails")

    def waveform_cache_dir(self) -> Path:
        return paths.ensure_dir(Path(self.cache_dir) / "waveforms")

    def temp_path(self) -> Path:
        return paths.ensure_dir(self.temp_dir)

    def download_path(self) -> Path:
        return paths.ensure_dir(self.download_dir)

    def export_path(self) -> Path:
        return paths.ensure_dir(self.export_dir)
