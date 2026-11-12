"""``.clipforge`` project files: JSON describing sources, timeline, history and export prefs.

A project only *references* media; it never contains or alters it. Each source is
stored with an absolute path and a path relative to the project file, so a project
folder that is moved together with its media still opens without relinking.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .history import History
from .model import ExportSettings, MediaSource, Segment, Timeline

PROJECT_EXTENSION = ".clipforge"
FORMAT_NAME = "clipforge-project"
FORMAT_VERSION = 1


class ProjectError(Exception):
    """The project file could not be read or written."""


@dataclass
class Project:
    sources: dict[str, MediaSource] = field(default_factory=dict)
    timeline: Timeline = None  # type: ignore[assignment]
    history: History = field(default_factory=History)
    export: ExportSettings = field(default_factory=ExportSettings)
    playhead: float = 0.0
    path: Optional[str] = None

    def __post_init__(self) -> None:
        if self.timeline is None:
            self.timeline = Timeline(self.sources)
        else:
            self.timeline.sources = self.sources

    # ------------------------------------------------------------------ media
    def missing_sources(self) -> list[MediaSource]:
        return [s for s in self.sources.values() if not os.path.isfile(s.path)]

    def used_source_ids(self) -> set[str]:
        return {s.source_id for s in self.timeline.all_segments()}

    def relink(self, source_id: str, new_path: str) -> None:
        """Point a source at a new file location (after the user located it)."""
        if not os.path.isfile(new_path):
            raise ProjectError(f"File not found: {new_path}")
        self.sources[source_id].path = os.path.abspath(new_path)

    # ------------------------------------------------------------ persistence
    def to_dict(self, project_path: Optional[str] = None) -> dict:
        base = Path(project_path).resolve().parent if project_path else None
        sources = []
        for src in self.sources.values():
            d = src.to_dict()
            d["relative_path"] = _relative(src.path, base)
            sources.append(d)
        return {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "sources": sources,
            "segments": [s.to_dict() for s in self.timeline.all_segments()],
            "playhead": self.playhead,
            "export": self.export.to_dict(),
            "history": self.history.to_dict(),
        }

    def save(self, path: str) -> None:
        """Write atomically (temp file + replace) so a crash never truncates a project."""
        path = os.path.abspath(path)
        data = json.dumps(self.to_dict(path), indent=2, ensure_ascii=False)
        folder = os.path.dirname(path)
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".clipforge-", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(data)
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
        except OSError as exc:
            raise ProjectError(f"Could not save the project:\n{exc}") from exc
        self.path = path

    @classmethod
    def load(cls, path: str) -> "Project":
        path = os.path.abspath(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError as exc:
            raise ProjectError(f"Could not open the project:\n{exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProjectError("This file is not a valid ClipForge project (it is damaged "
                               "or not JSON).") from exc
        if not isinstance(data, dict) or data.get("format") != FORMAT_NAME:
            raise ProjectError("This file is not a ClipForge project.")
        try:
            version = int(data.get("version", 0))
        except (TypeError, ValueError) as exc:
            raise ProjectError("The project file is damaged: bad version field.") from exc
        if version > FORMAT_VERSION:
            raise ProjectError("This project was saved by a newer version of ClipForge.")
        try:
            base = Path(path).parent
            sources: dict[str, MediaSource] = {}
            for d in data.get("sources", []):
                rel = d.pop("relative_path", None)
                src = MediaSource.from_dict(d)
                if not os.path.isfile(src.path) and rel:
                    candidate = (base / rel).resolve()
                    if candidate.is_file():
                        src.path = str(candidate)
                sources[src.id] = src
            segments = [Segment.from_dict(s) for s in data.get("segments", [])]
            segments = [s for s in segments if s.source_id in sources]
            project = cls(sources=sources, timeline=Timeline(sources, segments),
                          export=ExportSettings.from_dict(data.get("export", {})),
                          playhead=float(data.get("playhead", 0.0)), path=path)
            project.history.load_dict(data.get("history", {}))
            for entry in project.history.undo_stack + project.history.redo_stack:
                # same rule as for the live timeline: never keep clips of unknown media
                entry.segments = [s for s in entry.segments if s.source_id in sources]
            project.timeline.validate()
            # keep the playhead inside the timeline (NaN/negative/too large -> clamped)
            ph = project.playhead
            project.playhead = min(max(0.0, ph), project.timeline.duration) if ph == ph else 0.0
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ProjectError(f"The project file is damaged: {exc}") from exc
        return project


def _relative(path: str, base: Optional[Path]) -> Optional[str]:
    if base is None:
        return None
    try:
        return os.path.relpath(path, base).replace("\\", "/")
    except ValueError:          # different drive on Windows
        return None
