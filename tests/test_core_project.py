"""``.clipforge`` project files: round trips, atomic saves, relative paths, damaged files."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from clipforge.core.model import ExportSettings, MediaSource
from clipforge.core.project import FORMAT_NAME, FORMAT_VERSION, Project, ProjectError
from clipforge.document import Document


def make_media(folder: Path, name: str, sid: str, duration: float = 10.0,
               title: str = "") -> MediaSource:
    """A real (tiny) file on disk so existence checks behave like in production."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"not really a video")
    return MediaSource(id=sid, path=str(path.resolve()), duration=duration, fps=30.0,
                       width=1920, height=1080, title=title or name)


def state(doc: Document):
    return [(s.id, s.source_id, s.source_in, s.source_out, s.timeline_start, s.muted)
            for s in doc.timeline.segments]


def build_document(folder: Path) -> Document:
    """Two sources, a split, a mute, a delete (so there is undo history), export prefs."""
    doc = Document()
    a = make_media(folder / "media", "clip a.mp4", "srcA", 10.0, title="First clip")
    b = make_media(folder / "media", "clip b.mp4", "srcB", 5.0, title="Second clip")
    doc.add_source(a)
    doc.add_source(b)
    doc.seek(4.0)
    doc.select([doc.timeline.segments[0].id])
    doc.split_at_playhead()
    doc.set_muted([doc.timeline.segments[2].id], True)
    doc.select([doc.timeline.segments[1].id])
    doc.delete_selected(ripple=True)
    doc.set_export_settings(ExportSettings(filename="final", codec="h265", fps="60",
                                           resolution="720", audio=False, quality="low",
                                           scope="selection"))
    doc.seek(7.5)
    return doc


# ----------------------------------------------------------------- round trip
def test_save_load_roundtrip_restores_sources_and_segments(tmp_path):
    folder = tmp_path / "proj"
    doc = build_document(folder)
    expected_state = state(doc)
    path = folder / "work.clipforge"
    doc.save(str(path))

    loaded = Project.load(str(path))
    assert loaded.path == str(path.resolve())
    assert set(loaded.sources) == {"srcA", "srcB"}
    for sid, src in doc.sources.items():
        assert loaded.sources[sid].to_dict() == src.to_dict()
    got_state = [(s.id, s.source_id, s.source_in, s.source_out, s.timeline_start, s.muted)
                 for s in loaded.timeline.segments]
    assert got_state == expected_state


def test_export_settings_and_playhead_survive_reload(tmp_path):
    folder = tmp_path / "proj"
    doc = build_document(folder)
    path = folder / "work.clipforge"
    doc.save(str(path))

    loaded = Project.load(str(path))
    assert loaded.export == ExportSettings(filename="final", codec="h265", fps="60",
                                           resolution="720", audio=False, quality="low",
                                           scope="selection", folder="")
    assert loaded.playhead == pytest.approx(7.5)


def test_history_survives_reload_and_undo_still_works(tmp_path):
    folder = tmp_path / "proj"
    doc = build_document(folder)
    path = folder / "work.clipforge"
    before_last_edit = None
    # the state before the final edit (the ripple delete) is what undo must return
    doc.undo()
    before_last_edit = state(doc)
    doc.redo()                                # back to the post-edit state for saving
    doc.save(str(path))
    final_state = state(doc)

    reopened = Document()
    missing = reopened.load(str(path))
    assert missing == []
    assert len(reopened.history.undo_stack) == len(doc.history.undo_stack)
    assert reopened.history.undo_label() == "Ripple delete"
    assert reopened.undo() is True
    assert state(reopened) == before_last_edit
    assert reopened.redo() is True
    assert state(reopened) == final_state


def test_redo_history_is_persisted(tmp_path):
    doc = Document()
    doc.add_source(make_media(tmp_path, "a.mp4", "a", 10.0))
    doc.set_muted([doc.timeline.segments[0].id], True)
    doc.undo()
    assert doc.can_redo()
    path = tmp_path / "redo.clipforge"
    doc.save(str(path))

    reopened = Document()
    reopened.load(str(path))
    assert reopened.can_redo()
    assert reopened.history.redo_label() == "Mute clip"
    assert reopened.redo() is True
    assert reopened.timeline.segments[0].muted is True


def test_history_is_capped_to_last_fifty_entries_on_disk(tmp_path):
    doc = Document()
    doc.add_source(make_media(tmp_path, "a.mp4", "a", 10.0))
    seg_id = doc.timeline.segments[0].id
    for i in range(60):
        doc.set_muted([seg_id], i % 2 == 0)
    assert len(doc.history.undo_stack) == 61     # add + 60 mutes
    path = tmp_path / "many.clipforge"
    doc.save(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["history"]["undo"]) == 50


def test_empty_project_roundtrip(tmp_path):
    path = tmp_path / "empty.clipforge"
    Project().save(str(path))
    loaded = Project.load(str(path))
    assert loaded.sources == {} and loaded.timeline.segments == []
    assert loaded.playhead == 0.0
    assert loaded.export == ExportSettings()


def test_saved_file_has_format_marker_and_version(tmp_path):
    path = tmp_path / "p.clipforge"
    Project().save(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == FORMAT_NAME
    assert data["version"] == FORMAT_VERSION


def test_save_creates_missing_folder(tmp_path):
    path = tmp_path / "new" / "deeper" / "p.clipforge"
    Project().save(str(path))
    assert path.is_file()


# ------------------------------------------------------------- atomic save
def test_atomic_save_leaves_no_temp_files(tmp_path):
    path = tmp_path / "p.clipforge"
    Project().save(str(path))
    Project().save(str(path))                 # overwrite an existing project too
    assert sorted(os.listdir(tmp_path)) == ["p.clipforge"]


def test_failed_save_keeps_old_file_and_removes_temp(tmp_path, monkeypatch):
    path = tmp_path / "p.clipforge"
    Project(playhead=1.0).save(str(path))
    old = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(ProjectError, match="Could not save"):
        Project(playhead=2.0).save(str(path))
    monkeypatch.undo()
    assert path.read_bytes() == old
    assert sorted(os.listdir(tmp_path)) == ["p.clipforge"]


def test_save_into_unwritable_location_raises_project_error(tmp_path):
    blocker = tmp_path / "file-not-folder"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ProjectError):
        Project().save(str(blocker / "p.clipforge"))


# ----------------------------------------------------- relative path fallback
def test_project_and_media_moved_together_still_open(tmp_path):
    original = tmp_path / "original"
    doc = Document()
    doc.add_source(make_media(original / "media", "clip.mp4", "s1", 10.0))
    project_file = original / "work.clipforge"
    doc.save(str(project_file))

    moved = tmp_path / "moved-elsewhere"
    shutil.move(str(original), str(moved))

    loaded = Project.load(str(moved / "work.clipforge"))
    src = loaded.sources["s1"]
    assert Path(src.path).resolve() == (moved / "media" / "clip.mp4").resolve()
    assert loaded.missing_sources() == []


def test_absolute_path_is_used_when_it_still_exists(tmp_path):
    doc = Document()
    src = make_media(tmp_path / "media", "clip.mp4", "s1")
    doc.add_source(src)
    project_file = tmp_path / "work.clipforge"
    doc.save(str(project_file))
    loaded = Project.load(str(project_file))
    assert loaded.sources["s1"].path == src.path


def test_relative_path_is_not_stored_when_media_is_on_another_drive(tmp_path, monkeypatch):
    def relpath_across_drives(path, start=None):
        # what os.path.relpath does on Windows for paths on different drives
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    doc = Document()
    doc.add_source(make_media(tmp_path / "media", "clip.mp4", "s1"))
    project_file = tmp_path / "work.clipforge"
    monkeypatch.setattr(os.path, "relpath", relpath_across_drives)
    doc.save(str(project_file))
    monkeypatch.undo()
    data = json.loads(project_file.read_text(encoding="utf-8"))
    assert data["sources"][0]["relative_path"] is None


def test_relative_path_field_is_not_loaded_as_source_attribute(tmp_path):
    doc = Document()
    doc.add_source(make_media(tmp_path / "m", "clip.mp4", "s1"))
    p = tmp_path / "p.clipforge"
    doc.save(str(p))
    loaded = Project.load(str(p))
    assert not hasattr(loaded.sources["s1"], "relative_path")


# ------------------------------------------------------ missing media / relink
def test_missing_sources_and_relink(tmp_path):
    doc = Document()
    media = tmp_path / "media"
    a = make_media(media, "a.mp4", "sa")
    b = make_media(media, "b.mp4", "sb")
    doc.add_source(a)
    doc.add_source(b)
    project_file = tmp_path / "p.clipforge"
    doc.save(str(project_file))

    os.remove(a.path)
    reopened = Document()
    missing = reopened.load(str(project_file))
    assert [m.id for m in missing] == ["sa"]

    relocated = tmp_path / "elsewhere" / "a-moved.mp4"
    relocated.parent.mkdir()
    relocated.write_bytes(b"x")
    reopened.relink("sa", str(relocated))
    assert reopened.sources["sa"].path == str(relocated.resolve())
    assert reopened.project.missing_sources() == []
    assert reopened.dirty is True


def test_relink_to_missing_file_raises_and_changes_nothing(tmp_path):
    project = Project()
    project.sources["s"] = make_media(tmp_path, "a.mp4", "s")
    original = project.sources["s"].path
    with pytest.raises(ProjectError, match="File not found"):
        project.relink("s", str(tmp_path / "nope.mp4"))
    assert project.sources["s"].path == original


# --------------------------------------------------- unicode and spaces
def test_unicode_and_spaces_in_folder_file_title_and_media(tmp_path):
    folder = tmp_path / "Mi Carpeta ñ 日本"
    folder.mkdir()
    doc = Document()
    media_name = "tëst vidéo 日本.mp4"
    doc.add_source(make_media(folder, media_name, "u1", 6.0, title="Título – 映画 ✓"))
    project_file = folder / "proyecto final ü.clipforge"
    doc.save(str(project_file))

    reopened = Document()
    assert reopened.load(str(project_file)) == []
    assert reopened.display_name() == "proyecto final ü"
    src = reopened.sources["u1"]
    assert src.title == "Título – 映画 ✓"
    assert Path(src.path).name == media_name
    assert reopened.timeline.segments[0].source_id == "u1"


# ------------------------------------------------------------- damaged files
def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_missing_file_raises_project_error(tmp_path):
    with pytest.raises(ProjectError, match="Could not open"):
        Project.load(str(tmp_path / "absent.clipforge"))


def test_corrupted_json_raises_project_error(tmp_path):
    p = _write(tmp_path / "bad.clipforge", '{"format": "clipforge-project", "sources": [')
    with pytest.raises(ProjectError, match="not a valid ClipForge project"):
        Project.load(str(p))


def test_empty_file_raises_project_error(tmp_path):
    p = _write(tmp_path / "empty.clipforge", "")
    with pytest.raises(ProjectError):
        Project.load(str(p))


def test_non_utf8_bytes_raise_project_error(tmp_path):
    p = tmp_path / "binary.clipforge"
    p.write_bytes(b"\xff\xfe\x00garbage\x81")
    with pytest.raises(ProjectError):
        Project.load(str(p))


def test_json_root_that_is_not_an_object_raises(tmp_path):
    p = _write(tmp_path / "list.clipforge", "[1, 2, 3]")
    with pytest.raises(ProjectError, match="not a ClipForge project"):
        Project.load(str(p))


def test_wrong_format_marker_raises(tmp_path):
    p = _write(tmp_path / "other.clipforge",
               json.dumps({"format": "some-other-app", "version": 1}))
    with pytest.raises(ProjectError, match="not a ClipForge project"):
        Project.load(str(p))


def test_future_version_raises_with_clear_message(tmp_path):
    p = _write(tmp_path / "future.clipforge",
               json.dumps({"format": FORMAT_NAME, "version": FORMAT_VERSION + 1}))
    with pytest.raises(ProjectError, match="newer version"):
        Project.load(str(p))


def test_non_numeric_version_raises(tmp_path):
    p = _write(tmp_path / "ver.clipforge",
               json.dumps({"format": FORMAT_NAME, "version": "two"}))
    with pytest.raises(ProjectError, match="bad version"):
        Project.load(str(p))


def test_missing_version_is_treated_as_legacy_and_loads(tmp_path):
    p = _write(tmp_path / "legacy.clipforge", json.dumps({"format": FORMAT_NAME}))
    loaded = Project.load(str(p))
    assert loaded.sources == {}


def test_unknown_source_segment_is_dropped_and_rest_loads(tmp_path):
    doc = Document()
    doc.add_source(make_media(tmp_path / "m", "a.mp4", "good", 10.0))
    p = tmp_path / "p.clipforge"
    doc.save(str(p))
    data = json.loads(p.read_text(encoding="utf-8"))
    data["segments"].append({"id": "ghost-seg", "source_id": "ghost", "source_in": 0.0,
                             "source_out": 2.0, "timeline_start": 10.0, "muted": False})
    p.write_text(json.dumps(data), encoding="utf-8")

    loaded = Project.load(str(p))
    assert [s.id for s in loaded.timeline.segments] == [data["segments"][0]["id"]]


def test_unknown_source_in_history_is_dropped(tmp_path):
    doc = Document()
    doc.add_source(make_media(tmp_path / "m", "a.mp4", "good", 10.0))
    doc.add_source(make_media(tmp_path / "m", "b.mp4", "other", 4.0))
    p = tmp_path / "p.clipforge"
    doc.save(str(p))
    data = json.loads(p.read_text(encoding="utf-8"))
    data["sources"] = [s for s in data["sources"] if s["id"] != "other"]
    p.write_text(json.dumps(data), encoding="utf-8")

    loaded = Project.load(str(p))
    for entry in loaded.history.undo_stack + loaded.history.redo_stack:
        assert all(s.source_id != "other" for s in entry.segments)


def test_malformed_segment_raises_project_error(tmp_path):
    doc = Document()
    doc.add_source(make_media(tmp_path / "m", "a.mp4", "good", 10.0))
    p = tmp_path / "p.clipforge"
    doc.save(str(p))
    data = json.loads(p.read_text(encoding="utf-8"))
    del data["segments"][0]["source_out"]
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError, match="damaged"):
        Project.load(str(p))


def test_overlapping_segments_fail_validation_as_project_error(tmp_path):
    doc = Document()
    doc.add_source(make_media(tmp_path / "m", "a.mp4", "good", 10.0))
    p = tmp_path / "p.clipforge"
    doc.save(str(p))
    data = json.loads(p.read_text(encoding="utf-8"))
    first = data["segments"][0]
    clone = dict(first, id="clone", timeline_start=first["timeline_start"] + 1.0)
    data["segments"].append(clone)
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError, match="damaged"):
        Project.load(str(p))


def test_segment_past_end_of_source_fails_validation(tmp_path):
    doc = Document()
    doc.add_source(make_media(tmp_path / "m", "a.mp4", "good", 10.0))
    p = tmp_path / "p.clipforge"
    doc.save(str(p))
    data = json.loads(p.read_text(encoding="utf-8"))
    data["segments"][0]["source_out"] = 999.0
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError):
        Project.load(str(p))


def test_playhead_is_clamped_on_load(tmp_path):
    p = tmp_path / "ph.clipforge"
    Project().save(str(p))
    data = json.loads(p.read_text(encoding="utf-8"))
    data["playhead"] = -5
    p.write_text(json.dumps(data), encoding="utf-8")
    assert Project.load(str(p)).playhead == 0.0

    data["playhead"] = 1e9
    p.write_text(json.dumps(data), encoding="utf-8")
    assert Project.load(str(p)).playhead == 0.0      # empty timeline: duration 0

    data["playhead"] = "soon"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError):
        Project.load(str(p))


def test_nan_playhead_loads_as_zero(tmp_path):
    p = tmp_path / "nan.clipforge"
    p.write_text('{"format": "clipforge-project", "version": 1, "playhead": NaN}',
                 encoding="utf-8")
    assert Project.load(str(p)).playhead == 0.0


def test_unknown_top_level_keys_are_ignored(tmp_path):
    p = tmp_path / "future-fields.clipforge"
    p.write_text(json.dumps({"format": FORMAT_NAME, "version": 1,
                             "some_future_block": {"x": 1}}), encoding="utf-8")
    assert Project.load(str(p)).sources == {}


def test_unknown_export_keys_are_ignored(tmp_path):
    p = tmp_path / "export.clipforge"
    p.write_text(json.dumps({"format": FORMAT_NAME, "version": 1,
                             "export": {"codec": "h265", "bogus": True}}),
                 encoding="utf-8")
    loaded = Project.load(str(p))
    assert loaded.export.codec == "h265"
