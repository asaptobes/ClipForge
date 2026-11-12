"""Persistent settings: defaults, load/save round trip, sanitising and recent projects.

APPDATA / LOCALAPPDATA are redirected into ``tmp_path`` so no test touches the real profile.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from clipforge import paths
from clipforge.settings import CODEC_CHOICES, RESOLUTION_CHOICES, THEME_CHOICES, Settings


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ------------------------------------------------------------------ defaults
def test_defaults(isolated_profile):
    s = Settings()
    assert s.preferred_resolution == "1080"
    assert s.export_codec == "h264"
    assert s.hw_accel == "auto"
    assert s.theme == "dark"
    assert s.volume == 1.0
    assert s.recent_projects == []
    assert s.window_geometry == "" and s.splitter_main == ""
    assert Path(s.download_dir) == paths.default_download_dir()
    assert Path(s.export_dir) == paths.default_export_dir()
    assert Path(s.cache_dir) == paths.default_cache_dir()
    assert Path(s.temp_dir) == paths.default_temp_dir()


def test_default_path_lives_in_app_data(isolated_profile):
    assert Settings.default_path() == isolated_profile / "roaming" / "ClipForge" / "settings.json"


def test_missing_file_gives_defaults(isolated_profile):
    s = Settings.load(isolated_profile / "nope.json")
    assert s == Settings()
    assert s.preferred_resolution == "1080"


def test_load_without_path_uses_default_location(isolated_profile):
    s = Settings.load()
    assert s.theme == "dark"
    assert s._path == Settings.default_path()


# --------------------------------------------------------------- round trip
def test_save_then_load_roundtrip(isolated_profile):
    path = isolated_profile / "cfg" / "settings.json"
    s = Settings.load(path)
    s.theme = "light"
    s.preferred_resolution = "720"
    s.export_codec = "h265"
    s.hw_accel = "off"
    s.volume = 0.25
    s.window_geometry = "AB12"
    s.splitter_main = "1,2,3"
    s.recent_projects = [str(isolated_profile / "a.clipforge")]
    s.save()

    again = Settings.load(path)
    assert again == s
    assert again.theme == "light" and again.volume == 0.25
    assert again.recent_projects == [str(isolated_profile / "a.clipforge")]


def test_saved_file_is_plain_json_with_all_fields(isolated_profile):
    path = isolated_profile / "settings.json"
    s = Settings.load(path)
    s.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert "_path" not in data
    assert set(data) >= {"download_dir", "export_dir", "cache_dir", "temp_dir",
                         "recent_projects", "volume"}


def test_save_leaves_no_tmp_file(isolated_profile):
    path = isolated_profile / "settings.json"
    Settings.load(path).save()
    Settings.load(path).save()
    assert path.is_file()
    assert not (isolated_profile / "settings.tmp").exists()
    assert not any(p.endswith(".tmp") for p in os.listdir(isolated_profile))


def test_save_creates_parent_folder(isolated_profile):
    path = isolated_profile / "deep" / "dir" / "settings.json"
    Settings.load(path).save()
    assert path.is_file()


def test_save_failure_is_logged_not_raised(isolated_profile, monkeypatch, caplog):
    path = isolated_profile / "settings.json"
    s = Settings.load(path)

    def boom(src, dst):
        raise OSError("locked")

    monkeypatch.setattr(os, "replace", boom)
    with caplog.at_level("ERROR"):
        s.save()                              # must not raise
    monkeypatch.undo()
    assert any("Could not save settings" in r.getMessage() for r in caplog.records)


# -------------------------------------------------------- unknown / wrong types
def test_unknown_keys_are_ignored(isolated_profile):
    path = write_json(isolated_profile / "s.json",
                      {"theme": "light", "bogus_key": 42, "__class__": "x", "_path": "/x"})
    s = Settings.load(path)
    assert s.theme == "light"
    assert not hasattr(s, "bogus_key")
    assert s._path == path


def test_wrong_types_fall_back_to_defaults(isolated_profile):
    path = write_json(isolated_profile / "s.json", {
        "theme": 5,
        "preferred_resolution": 1080,         # int, expected str
        "volume": "loud",
        "recent_projects": "not-a-list",
        "window_geometry": None,
    })
    s = Settings.load(path)
    assert s.theme == "dark"
    assert s.preferred_resolution == "1080"
    assert s.volume == 1.0
    assert s.recent_projects == []
    assert s.window_geometry == ""


def test_bool_is_not_accepted_for_numeric_fields(isolated_profile):
    path = write_json(isolated_profile / "s.json", {"volume": True})
    assert Settings.load(path).volume == 1.0


def test_int_is_accepted_for_float_field(isolated_profile):
    path = write_json(isolated_profile / "s.json", {"volume": 0})
    assert Settings.load(path).volume == 0.0
    assert isinstance(Settings.load(path).volume, float)


def test_unknown_key_names_do_not_override_private_path(isolated_profile):
    real = isolated_profile / "real.json"
    path = write_json(real, {"_path": str(isolated_profile / "elsewhere.json")})
    assert Settings.load(path)._path == real


# ------------------------------------------------------- invalid choices
def test_invalid_choices_are_sanitised(isolated_profile):
    path = write_json(isolated_profile / "s.json", {
        "theme": "neon",
        "export_codec": "vp9",
        "hw_accel": "maybe",
        "preferred_resolution": "4k",
    })
    s = Settings.load(path)
    assert s.theme == "dark"
    assert s.export_codec == "h264"
    assert s.hw_accel == "auto"
    assert s.preferred_resolution == "1080"


def test_every_valid_choice_is_kept(isolated_profile):
    for theme in THEME_CHOICES:
        assert Settings.load(write_json(isolated_profile / "t.json", {"theme": theme})).theme == theme
    for codec in CODEC_CHOICES:
        assert Settings.load(write_json(isolated_profile / "c.json",
                                        {"export_codec": codec})).export_codec == codec
    for res in RESOLUTION_CHOICES:
        assert Settings.load(write_json(isolated_profile / "r.json",
                                        {"preferred_resolution": res})).preferred_resolution == res


@pytest.mark.parametrize("raw, expected", [(-3.0, 0.0), (5, 1.0), (0.5, 0.5), (1.0, 1.0)])
def test_volume_is_clamped_to_unit_range(isolated_profile, raw, expected):
    path = write_json(isolated_profile / "v.json", {"volume": raw})
    assert Settings.load(path).volume == pytest.approx(expected)


# ---------------------------------------------------------- corrupt files
def test_corrupt_json_gives_defaults(isolated_profile):
    path = isolated_profile / "broken.json"
    path.write_text("{ this is not json", encoding="utf-8")
    s = Settings.load(path)
    assert s.theme == "dark" and s.preferred_resolution == "1080"
    assert s._path == path


def test_non_object_root_gives_defaults(isolated_profile):
    path = write_json(isolated_profile / "list.json", ["theme", "light"])
    assert Settings.load(path).theme == "dark"


def test_binary_garbage_gives_defaults(isolated_profile):
    path = isolated_profile / "garbage.json"
    path.write_bytes(b"\xff\xfe\x00\x01not text")
    assert Settings.load(path).theme == "dark"


def test_directory_at_settings_path_gives_defaults(isolated_profile):
    path = isolated_profile / "is_a_dir.json"
    path.mkdir()
    assert Settings.load(path).theme == "dark"


def test_corrupt_file_is_overwritten_on_next_save(isolated_profile):
    path = isolated_profile / "broken.json"
    path.write_text("###", encoding="utf-8")
    s = Settings.load(path)
    s.theme = "midnight"
    s.save()
    assert Settings.load(path).theme == "midnight"


# ------------------------------------------------------------ recent projects
def test_add_recent_puts_newest_first(isolated_profile):
    s = Settings()
    s.add_recent(str(isolated_profile / "one.clipforge"))
    s.add_recent(str(isolated_profile / "two.clipforge"))
    assert s.recent_projects[0] == os.path.abspath(str(isolated_profile / "two.clipforge"))
    assert len(s.recent_projects) == 2


def test_add_recent_deduplicates_and_moves_to_front(isolated_profile):
    s = Settings()
    a = str(isolated_profile / "a.clipforge")
    b = str(isolated_profile / "b.clipforge")
    s.add_recent(a)
    s.add_recent(b)
    s.add_recent(a)
    assert s.recent_projects == [os.path.abspath(a), os.path.abspath(b)]


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive paths are a Windows rule")
def test_add_recent_dedup_ignores_case_on_windows(isolated_profile):
    s = Settings()
    s.add_recent(str(isolated_profile / "Proj.clipforge"))
    s.add_recent(str(isolated_profile / "proj.CLIPFORGE"))
    assert len(s.recent_projects) == 1


def test_add_recent_caps_list_at_ten(isolated_profile):
    s = Settings()
    names = [str(isolated_profile / f"p{i}.clipforge") for i in range(12)]
    for n in names:
        s.add_recent(n)
    assert len(s.recent_projects) == 10
    assert s.recent_projects[0] == os.path.abspath(names[-1])
    assert os.path.abspath(names[0]) not in s.recent_projects
    assert os.path.abspath(names[1]) not in s.recent_projects
    assert os.path.abspath(names[2]) in s.recent_projects


def test_loaded_recent_list_is_capped_and_filtered(isolated_profile):
    entries = [f"p{i}.clipforge" for i in range(15)] + ["", 42, None]
    path = write_json(isolated_profile / "r.json", {"recent_projects": entries})
    s = Settings.load(path)
    assert s.recent_projects == entries[:10]


def test_recent_persists_through_save_and_load(isolated_profile):
    path = isolated_profile / "r.json"
    s = Settings.load(path)
    s.add_recent(str(isolated_profile / "ünïcødé proj.clipforge"))
    s.save()
    assert Settings.load(path).recent_projects == s.recent_projects


# -------------------------------------------------------- derived folders
def test_derived_folders_are_created(isolated_profile):
    s = Settings()
    s.cache_dir = str(isolated_profile / "cache")
    s.temp_dir = str(isolated_profile / "tmp")
    s.download_dir = str(isolated_profile / "dl")
    s.export_dir = str(isolated_profile / "ex")
    assert s.thumbnail_cache_dir().is_dir()
    assert s.waveform_cache_dir().is_dir()
    assert s.temp_path().is_dir()
    assert s.download_path().is_dir()
    assert s.export_path().is_dir()
    assert s.thumbnail_cache_dir() == isolated_profile / "cache" / "thumbnails"
