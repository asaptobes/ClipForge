"""Splash, support dialog and the related setting."""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

from clipforge import paths
from clipforge.settings import Settings
from clipforge.ui import splash, support_dialog, theme
from clipforge.ui.support_dialog import SUPPORT_URL, SupportDialog


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication([])
    theme.apply_theme(inst, "dark")
    return inst


@pytest.fixture
def settings(tmp_path):
    return Settings.load(tmp_path / "settings.json")


def test_dialog_texts_and_buttons(app, settings):
    d = SupportDialog(settings)
    assert d.title_label.text() == "Thanks for using ClipForge!"
    assert d.body_label.text().startswith("I built ClipForge as a solo project")
    assert d.body_label.text().endswith("stays just as useful either way.")
    assert d.buy_button.text() == "☕ Buy me a coffee"
    assert d.later_button.text() == "Maybe later"
    assert d.dont_show_check.text() == "Don't show this again"
    assert not d.logo_label.pixmap().isNull()


def test_buy_opens_exact_url_and_closes(app, settings, monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    d = SupportDialog(settings)
    d.buy_button.click()
    assert opened == [SUPPORT_URL] == ["https://buymeacoffee.com/asaptobes"]
    assert d.result() == d.DialogCode.Accepted
    assert settings.show_support_prompt is True


@pytest.mark.parametrize("how", ["later", "escape", "buy"])
def test_checkbox_persists(app, settings, monkeypatch, tmp_path, how):
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: True)
    d = SupportDialog(settings)
    d.dont_show_check.setChecked(True)
    {"later": d.later_button.click, "escape": d.reject, "buy": d.buy_button.click}[how]()
    assert settings.show_support_prompt is False
    assert Settings.load(tmp_path / "settings.json").show_support_prompt is False


def test_unticked_keeps_prompt(app, settings, tmp_path):
    d = SupportDialog(settings)
    d.later_button.click()
    assert settings.show_support_prompt is True
    assert not (tmp_path / "settings.json").exists()


def test_old_settings_file_tolerated(tmp_path):
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"theme": "dark", "volume": 0.5}), encoding="utf-8")
    assert Settings.load(p).show_support_prompt is True
    p.write_text(json.dumps({"show_support_prompt": "nope"}), encoding="utf-8")
    assert Settings.load(p).show_support_prompt is True
    p.write_text(json.dumps({"show_support_prompt": False}), encoding="utf-8")
    assert Settings.load(p).show_support_prompt is False


def test_prompt_and_splash_skipped_headless(app, settings, monkeypatch):
    assert splash.headless()
    assert not splash.splash_allowed()
    assert not support_dialog.prompt_allowed(settings)
    monkeypatch.setenv("CLIPFORGE_NO_SPLASH", "1")
    assert not splash.splash_allowed(smoke_test=True)


def test_splash_builds_and_renders(app):
    s = splash.create_splash()
    assert s is not None
    s.set_status("Loading…", 0.4)
    assert not s.grab().isNull()
    assert s.windowFlags() & Qt.WindowType.FramelessWindowHint
    s.close()


def test_splash_missing_image_is_silent(app, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "resource_path", lambda *parts: tmp_path / "nope.png")
    assert splash.create_splash() is None
    (tmp_path / "bad.png").write_bytes(b"not an image")
    monkeypatch.setattr(paths, "resource_path", lambda *parts: tmp_path / "bad.png")
    assert splash.create_splash() is None


def test_settings_dialog_support_controls(app, settings, monkeypatch):
    from clipforge.ui.settings_dialog import SettingsDialog
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    d = SettingsDialog(settings)
    assert d.support_check.isChecked()
    d.support_button.click()
    assert opened == [SUPPORT_URL]
    d.support_check.setChecked(False)
    assert d.save()
    assert settings.show_support_prompt is False
