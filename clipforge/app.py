"""Application bootstrap."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional, Sequence

from . import __app_name__, __version__


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=__app_name__, description="YouTube downloader and video editor")
    parser.add_argument("path", nargs="?", help="a .clipforge project or a video file to open")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--smoke-test", action="store_true",
                        help="start the full UI, verify dependencies, then exit (used by the build)")
    parser.add_argument("--export-test", metavar="DIR",
                        help="make a synthetic clip, edit and export it in DIR, verify the "
                             "result, then exit (0 = OK); used to validate the packaged app")
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    return parser.parse_args(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)

    from .logging_setup import setup_logging
    log_path = setup_logging(debug=args.debug)
    log = logging.getLogger("clipforge")
    log.info("%s %s starting (frozen=%s)", __app_name__, __version__, getattr(sys, "frozen", False))

    # Prefer the FFmpeg media backend for Qt Multimedia (accurate seeking, wide codec support).
    os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(__app_name__)
    app.setApplicationDisplayName(__app_name__)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(__app_name__)

    if sys.platform == "win32":      # own taskbar identity/icon instead of python.exe's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClipForge.App")
        except Exception:  # noqa: BLE001
            pass

    from . import paths
    from .settings import Settings
    from .ui import theme
    from .ui.winutil import TitleBarStyler

    settings = Settings.load()
    theme.apply_theme(app, settings.theme)
    styler = TitleBarStyler(dark=settings.theme != "light", parent=app)
    app.installEventFilter(styler)
    icon_file = paths.resource_path("icon.ico")
    if icon_file.is_file():
        app.setWindowIcon(QIcon(str(icon_file)))

    try:
        from .ui.main_window import MainWindow
        window = MainWindow(settings)
    except Exception as exc:  # noqa: BLE001 - show something instead of dying silently
        log.critical("Start-up failed", exc_info=True)
        if args.smoke_test:
            return 2
        QMessageBox.critical(None, __app_name__,
                             f"ClipForge could not start:\n\n{exc}\n\nDetails: {log_path}")
        return 1

    window.show()
    if args.path:
        QTimer.singleShot(100, lambda: window.open_path(os.path.abspath(args.path)))

    if args.smoke_test:
        from .media import ffmpeg

        def finish() -> None:
            missing = ffmpeg.missing_dependencies()
            log.info("Smoke test: missing dependencies = %s", missing)
            window.document._set_dirty(False)  # never prompt during the automated check
            window.close()
            app.exit(3 if missing else 0)

        QTimer.singleShot(2500, finish)

    if args.export_test:
        QTimer.singleShot(500, lambda: _export_selftest(app, window, args.export_test, log))

    code = app.exec()
    log.info("Exiting with code %s", code)
    return code


def _export_selftest(app, window, folder: str, log) -> None:
    """Synthetic clip -> split/delete/reorder -> export -> probe. Exits the app with 0/4."""
    import json
    import subprocess

    from .media import ffmpeg
    from .media.export import ExportJob
    from .media.probe import probe_source

    def fail(message: str) -> None:
        log.error("Export self-test failed: %s", message)
        print(f"EXPORT-TEST FAILED: {message}")
        window.document._set_dirty(False)
        app.exit(4)

    try:
        os.makedirs(folder, exist_ok=True)
        src = os.path.join(folder, "selftest source.mp4")
        ffmpeg.run(["-f", "lavfi", "-i", "testsrc2=s=720x1280:r=30:d=9",
                    "-f", "lavfi", "-i", "sine=f=440:d=9", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-g", "30", "-c:a", "aac", "-shortest", src])
        source = probe_source(src)
        doc = window.document
        doc.add_source(source)
        for t in (3.0, 6.0):
            doc.seek(t)
            doc.split_at_playhead()
        segs = doc.timeline.segments
        doc.select([segs[1].id])
        doc.delete_selected(ripple=True)                       # 9 s -> 6 s
        doc.reorder(doc.timeline.segments[1].id, 0)
        settings = doc.export_settings
        settings.folder, settings.filename, settings.mode = folder, "selftest out", "accurate"
        job = ExportJob(dict(doc.sources), [s.copy() for s in doc.timeline.segments], settings,
                        temp_dir=str(window.settings.temp_path()))
        app._selftest_job = job

        def done(path: str) -> None:
            info = json.loads(subprocess.check_output(
                [ffmpeg.ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
                 "-of", "json", path], creationflags=ffmpeg.creation_flags()))
            duration = float(info["format"]["duration"])
            if abs(duration - 6.0) > 0.15:
                return fail(f"expected 6.0 s, got {duration:.3f} s")
            print(f"EXPORT-TEST OK: {path} ({duration:.3f} s, encoder "
                  f"{ffmpeg.pick_video_encoder(settings.codec, settings.hw_accel)})")
            doc._set_dirty(False)
            app.exit(0)

        job.finished.connect(done)
        job.failed.connect(fail)
        job.start()
    except Exception as exc:  # noqa: BLE001
        log.exception("Export self-test crashed")
        fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
