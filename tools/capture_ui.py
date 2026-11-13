"""Capture crisp PNGs of the real ClipForge main window in a sequence of editing states.

Everything is driven through the real ``MainWindow`` / ``Document`` API. The window is never
mapped to the desktop (``WA_DontShowOnScreen``), so nothing on the user's screen is captured
and no focus is stolen; ``QT_SCALE_FACTOR`` makes Qt render the widgets at a true 2x device
pixel ratio, so text and 1 px lines are genuinely sharper, not upscaled.

Run:  .venv/Scripts/python.exe tools/capture_ui.py            -> promo/ui/*.png + layout.json
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DPR = 2
OUT = ROOT / "promo" / "ui"
MEDIA_DIR = OUT / "media"
VIDEO_NAME = "My video.mp4"
WIN_W, WIN_H = 1600, 1000

os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"     # ignore the desktop scaling (150% etc.)
os.environ["QT_SCALE_FACTOR"] = str(DPR)
os.environ.pop("QT_QPA_PLATFORM", None)          # the normal Windows platform (real fonts)
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
os.environ["CLIPFORGE_NO_SPLASH"] = "1"

from clipforge import paths  # noqa: E402


# ------------------------------------------------------------------------ demo media
def make_demo_video(dest: Path) -> None:
    """24.5 s, 1280x720, 30 fps: four brand-coloured gradient sections cross-faded, plus
    a speech/music-like audio track with a clearly varied waveform."""
    ffmpeg = paths.find_tool("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found")
    sec, fade, n = 6.5, 0.5, 4
    cols = [  # (type, c0, c1, c2, c3, speed)
        ("linear", "0x3b82ff", "0x8b52ff", "0x22d3ee", "0x0b1020", 0.035),
        ("radial", "0x8b52ff", "0x22d3ee", "0x3b82ff", "0x0b1020", 0.04),
        ("circular", "0x22d3ee", "0x3b82ff", "0x8b52ff", "0x0b1020", 0.03),
        ("linear", "0x22d3ee", "0x8b52ff", "0x3b82ff", "0x0b1020", 0.06),
    ]
    inputs, chains = [], []
    for i, (typ, c0, c1, c2, c3, sp) in enumerate(cols):
        inputs += ["-f", "lavfi", "-i",
                   f"gradients=s=1280x720:r=30:d={sec}:c0={c0}:c1={c1}:c2={c2}:c3={c3}:"
                   f"nb_colors=4:type={typ}:speed={sp}:seed={7 + i}"]
        # a slow soft light blob drifting across the picture
        chains.append(f"[{i}:v]format=yuv420p,vignette=angle=0.5[g{i}]")
    chain = ";".join(chains)
    prev = "g0"
    for i in range(1, n):
        off = i * (sec - fade)
        chain += f";[{prev}][g{i}]xfade=transition=fade:duration={fade}:offset={off}[x{i}]"
        prev = f"x{i}"
    total = n * sec - (n - 1) * fade
    chain += f";[{prev}]format=yuv420p[v]"
    env = ("0.55*pow(abs(sin(2*PI*0.42*t+0.3)),0.7)*(0.6+0.4*sin(2*PI*2.9*t))"
           "*(0.35+0.65*abs(sin(2*PI*0.13*t+1.1)))")
    voice = f"{env}*sin(2*PI*(170+70*sin(2*PI*0.9*t))*t)"
    harm = f"0.22*{env}*sin(2*PI*(340+140*sin(2*PI*0.9*t))*t)"
    pad = "0.10*sin(2*PI*220*t)*(0.5+0.5*sin(2*PI*0.2*t))"
    expr = f"0.8*({voice}+{harm}+{pad})"
    aud = ["-f", "lavfi", "-i", f"aevalsrc='{expr}|{expr}':s=48000:d={total}"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", *inputs, *aud,
           "-filter_complex", chain, "-map", "[v]", "-map", f"{n}:a",
           "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
           "-g", "30", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dest)]
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL,
                   creationflags=0x08000000 if os.name == "nt" else 0)


# --------------------------------------------------------------------------- capture
def main() -> int:
    master = MEDIA_DIR / VIDEO_NAME
    if not master.is_file():
        print("generating demo media ...")
        make_demo_video(master)
    # work on a copy in a neutral public folder, so no personal path shows in the UI
    public = Path(os.environ.get("PUBLIC", "C:/Users/Public")) / "Videos" / "Demo"
    public.mkdir(parents=True, exist_ok=True)
    video = public / VIDEO_NAME
    shutil.copy2(master, video)

    from PySide6.QtCore import QCoreApplication, QPoint, QRect, Qt
    from PySide6.QtWidgets import QApplication

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("ClipForge")
    from clipforge.settings import Settings
    from clipforge.ui import theme
    from clipforge.ui.export_dialog import ExportDialog
    from clipforge.ui.main_window import MainWindow

    tmp = Path(tempfile.mkdtemp(prefix="clipforge_capture_"))
    exit_code = 0
    window = None
    try:
        settings = Settings(
            download_dir=str(tmp / "dl"), export_dir=str(public / "Exports"),
            cache_dir=str(tmp / "cache"), temp_dir=str(tmp / "temp"),
            show_support_prompt=False)
        settings._path = tmp / "settings.json"
        theme.apply_theme(app, "dark")

        window = MainWindow(settings)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.resize(WIN_W, WIN_H)
        window.show()
        global DPR
        DPR = window.devicePixelRatioF()
        print(f"device pixel ratio {DPR}")
        doc, pv, tlp = window.document, window.preview, window.timeline_panel
        lp = window.left_panel
        view = tlp.view

        def pump(seconds: float = 0.0) -> None:
            end = time.monotonic() + seconds
            app.processEvents()
            while time.monotonic() < end:
                app.processEvents()
                time.sleep(0.005)

        def wait_until(pred, timeout: float, what: str) -> bool:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                app.processEvents()
                if pred():
                    return True
                time.sleep(0.01)
            print(f"  warning: timed out waiting for {what}")
            return False

        frames: list[float] = []
        pv.frameDisplayed.connect(lambda _img, t: frames.append(t))

        def media_ready() -> bool:
            """Every thumbnail tile and waveform the visible clips need is loaded."""
            ok = True
            for seg in doc.timeline.all_segments():
                src = doc.source_of(seg)
                if src is None:
                    continue
                if seg.is_audio:
                    ok &= (window.waveforms.get(src) is not None)
                    continue
                ok &= (window.waveforms.get(src) is not None)
                y, h, thumb_h, _ = view.lanes()
                aspect = src.width / src.height if src.width and src.height else 16 / 9
                tile_w = max(8, int(round(thumb_h * min(4.0, max(0.3, aspect)))))
                spt = tile_w / view.pixels_per_second
                base = round(view.time_to_x(seg.timeline_start - seg.source_in))
                x1 = view.time_to_x(seg.timeline_end)
                k0 = max(0, (0 - base) // tile_w)
                k1 = (int(min(x1, view.width())) - 1 - base) // tile_w
                for k in range(k0, k1 + 1):
                    ok &= window.thumbnails.get(src, k * spt) is not None
            return ok

        def settle(fresh_frame: bool = True) -> None:
            """Wait for the preview frame at the playhead and for all media to load."""
            tlp.zoom_to_fit()
            pump(0.05)
            if fresh_frame and doc.timeline.segments:
                def got() -> bool:
                    return any(abs(t - doc.playhead) < 0.12 for t in frames)
                wait_until(got, 8.0, "preview frame")
            wait_until(media_ready, 20.0, "thumbnails/waveform")
            pump(0.35)
            if pv._viewport.state != "frame" and doc.timeline.segments:
                print("  warning: preview shows no frame")

        # ---- geometry helpers (capture pixels) --------------------------------
        def rect_of(w) -> list[int]:
            p = w.mapTo(window, QPoint(0, 0))
            return [round(p.x() * DPR), round(p.y() * DPR),
                    round(w.width() * DPR), round(w.height() * DPR)]

        def rect_px(r: QRect | object, origin: QPoint) -> list[int]:
            return [round((r.x() + origin.x()) * DPR), round((r.y() + origin.y()) * DPR),
                    round(r.width() * DPR), round(r.height() * DPR)]

        def static_layout() -> dict:
            vo = view.mapTo(window, QPoint(0, 0))
            vp = pv._viewport
            vpo = vp.mapTo(window, QPoint(0, 0))
            vr = vp.video_rect()
            tool_h = view.mapTo(tlp, QPoint(0, 0)).y()
            tlo = tlp.mapTo(window, QPoint(0, 0))
            top, h, _, _ = view.lanes()
            out = {
                "window": [0, 0, round(window.width() * DPR), round(window.height() * DPR)],
                "top_bar": rect_of(window.top_bar),
                "left_panel": rect_of(window.left_frame),
                "preview_panel": rect_of(window.preview_frame),
                "preview_viewport": rect_of(vp),
                "video_frame": [round((vr.x() + vpo.x()) * DPR), round((vr.y() + vpo.y()) * DPR),
                                round(vr.width() * DPR), round(vr.height() * DPR)],
                "transport_bar": rect_of(pv._bar),
                "timeline_panel": rect_of(window.timeline_frame),
                "timeline_toolbar": [round(tlo.x() * DPR), round(tlo.y() * DPR),
                                     round(tlp.width() * DPR), round(tool_h * DPR)],
                "timeline_view": rect_of(view),
                "video_lane": [round(vo.x() * DPR), round((vo.y() + top) * DPR),
                               round(view.width() * DPR), round(h * DPR)],
                "export_button": rect_of(window.btn_export),
                "status_bar": rect_of(window.statusBar()),
            }
            lane = view.audio_lane()
            if lane is not None:
                out["audio_lane"] = [round(vo.x() * DPR), round((vo.y() + lane[0]) * DPR),
                                     round(view.width() * DPR), round(lane[1] * DPR)]
            return out

        def state_layout() -> dict:
            vo = view.mapTo(window, QPoint(0, 0))

            def clip(seg) -> dict:
                r = view.clip_rect(seg)
                return {"id": seg.id, "start": round(seg.timeline_start, 3),
                        "end": round(seg.timeline_end, 3),
                        "rect": [round((r.x() + vo.x()) * DPR), round((r.y() + vo.y()) * DPR),
                                 round(r.width() * DPR), round(r.height() * DPR)],
                        "selected": seg.id in doc.selection}
            return {
                "playhead_x": round((view.time_to_x(doc.playhead) + 0.5 + vo.x()) * DPR),
                "playhead_time": round(doc.playhead, 3),
                "duration": round(doc.duration, 3),
                "video_clips": [clip(s) for s in doc.timeline.segments],
                "audio_clips": [clip(s) for s in doc.timeline.audio],
                "layout": static_layout(),
            }

        layout: dict = {"states": {}}
        sizes: set[tuple[int, int]] = set()

        def save_widget(widget, name: str) -> tuple[int, int]:
            pm = widget.grab()
            pm.setDevicePixelRatio(1.0)
            if not pm.save(str(OUT / f"{name}.png"), "PNG"):
                raise RuntimeError(f"could not write {name}.png")
            return pm.width(), pm.height()

        def dl_layout() -> dict:
            """Rects of the real download-panel widgets (capture px)."""
            from PySide6.QtWidgets import QToolButton as _TB
            ue = lp.url_edit
            out: dict = {}
            for key, w in (("url_field", ue), ("url_error", lp.url_error),
                           ("fetch_label", lp.fetch_label), ("info_card", lp.info_card),
                           ("thumbnail", lp.info_thumb), ("title_label", lp.info_title),
                           ("uploader_label", lp.info_uploader),
                           ("duration_label", lp.info_duration),
                           ("quality_selector", lp.quality_combo),
                           ("download_button", lp.download_button),
                           ("progress_box", lp.progress_box), ("progress_track", lp.progress_bar),
                           ("progress_label", lp.progress_label),
                           ("stage_label", lp.stage_label), ("cancel_button", lp.cancel_button),
                           ("footnote", lp.footnote)):
                if w.isVisible():
                    out[key] = rect_of(w)
            cur = ue.cursorRect()
            fm = ue.fontMetrics()
            o = ue.mapTo(window, QPoint(0, 0))
            out["url_text"] = {
                "caret_rect": [round((cur.x() + o.x()) * DPR), round((cur.y() + o.y()) * DPR),
                               round(max(1, cur.width()) * DPR), round(cur.height() * DPR)],
                "baseline_y": round((cur.y() + o.y() + (cur.height() - fm.height()) / 2
                                     + fm.ascent()) * DPR),
                "left_x": round((o.x() + 1 + ue.textMargins().left()) * DPR),
                "font_family": ue.font().family(),
                "font_px": round(fm.height() * DPR),
                "text": ue.text(), "text_width": round(fm.horizontalAdvance(ue.text()) * DPR),
                "has_focus": ue.hasFocus(),
            }
            clear = [b for b in ue.findChildren(_TB) if b.isVisible()]
            if clear:
                out["url_clear_button"] = rect_of(clear[0])
            if lp.progress_box.isVisible() and lp.progress_bar.maximum() > 0:
                r = rect_of(lp.progress_bar)
                frac = lp.progress_bar.value() / lp.progress_bar.maximum()
                out["progress_fill"] = [r[0], r[1], round(r[2] * frac), r[3]]
                out["progress_fraction"] = round(frac, 3)
            out["progress_text"] = lp.progress_label.text() if lp.progress_box.isVisible() else ""
            out["stage_text"] = lp.stage_label.text() if lp.progress_box.isVisible() else ""
            out["panel_state"] = lp.state()
            out["note"] = ("no separate fetch/paste button: the URL is validated on Enter or "
                           "350 ms after typing; the field has a built-in clear button")
            return out

        def capture(name: str, extra_wait: bool = True, dl_ui: bool = False) -> None:
            if extra_wait:
                settle()
            size = save_widget(window, name)
            sizes.add(size)
            layout["states"][name] = state_layout()
            if dl_ui:
                layout["states"][name]["download_ui"] = dl_layout()
            print(f"  {name}.png {size[0]}x{size[1]}")

        OUT.mkdir(parents=True, exist_ok=True)
        for old in OUT.glob("*.png"):
            old.unlink()

        # ---- 00 empty ---------------------------------------------------------
        pump(1.0)
        window.statusBar().clearMessage()
        capture("00_empty", extra_wait=False)
        layout["dpr"] = DPR
        layout["window_logical"] = [WIN_W, WIN_H]

        # ---- download flow (fake jobs: same signals as the real ones, no network) ----
        from PySide6.QtCore import QEvent, QObject, Signal
        from PySide6.QtGui import QFocusEvent
        from clipforge.media import downloader as dlm
        from clipforge.ui import left_panel as lpm

        class FakeMeta(QObject):
            finished = Signal(object)
            failed = Signal(str)
            cancelled = Signal()

            def __init__(self, *a):
                super().__init__(a[-1] if a and isinstance(a[-1], QObject) else None)

            def start(self):
                pass

            def cancel(self):
                pass

        class FakeDownload(QObject):
            progress = Signal(float, str, str)
            stage = Signal(str)
            finished = Signal(str, object)
            failed = Signal(str)
            cancelled = Signal()

            def __init__(self, *a):
                super().__init__(a[-1] if a and isinstance(a[-1], QObject) else None)

            def start(self):
                pass

            def cancel(self):
                pass

        lpm.dl.MetadataJob, lpm.dl.DownloadJob = FakeMeta, FakeDownload
        jpeg_path = tmp / "thumb.jpg"
        subprocess.run([paths.find_tool("ffmpeg"), "-hide_banner", "-v", "error", "-y", "-ss", "3",
                        "-i", str(video), "-frames:v", "1", "-vf", "scale=480:270", "-q:v", "2",
                        str(jpeg_path)], check=True, stdin=subprocess.DEVNULL,
                       creationflags=0x08000000 if os.name == "nt" else 0)
        URL = "https://www.youtube.com/watch?v=your-video"
        info = dlm.VideoInfo(url=URL, video_id="your-video", title="My video", duration=24.0,
                             uploader="Your channel", thumbnail_jpeg=jpeg_path.read_bytes(),
                             heights=[1080, 720, 480, 360])

        app.setActiveWindow(window)
        lp.url_edit.setFocus(Qt.FocusReason.MouseFocusReason)
        QApplication.sendEvent(lp.url_edit, QFocusEvent(QEvent.Type.FocusIn,
                                                         Qt.FocusReason.MouseFocusReason))
        pump(0.3)
        capture("d1_url_focus", extra_wait=False, dl_ui=True)
        lp.url_edit.setText(URL)
        lp._debounce.stop()                    # capture before the debounced validation fires
        pump(0.1)
        capture("d2_url_typed", extra_wait=False, dl_ui=True)
        # The real validator rejects the placeholder id "your-video" (ids are 11 chars), so
        # run the step validate_now() performs after a link passes validation.
        lp._set_url_state("ok", "")
        lp._valid_url = URL
        lp._start_metadata(URL)                # -> "Fetching video info..."
        lp.url_edit.setCursorPosition(0)
        pump(0.2)
        capture("d2b_fetching", extra_wait=False, dl_ui=True)
        lp._meta_job.finished.emit(info)       # what MetadataJob emits on success
        pump(0.5)
        capture("d3_info", extra_wait=False, dl_ui=True)
        lp.download_button.click()
        pump(0.2)
        job = lp._dl_job
        for name, frac, bps, eta in [("d4_dl_10", 0.10, 8.4e6, 95), ("d4_dl_45", 0.45, 9.1e6, 52),
                                     ("d4_dl_80", 0.80, 9.6e6, 14)]:
            job.stage.emit("Downloading video…")
            job.progress.emit(frac, dlm._fmt_speed(bps), dlm._fmt_eta(eta))
            pump(0.3)
            capture(name, extra_wait=False, dl_ui=True)
        job.progress.emit(1.0, "", "")
        job.stage.emit("Done")
        job.finished.emit(str(video), info)    # panel clears itself and emits downloadFinished
        wait_until(lambda: not doc.timeline.is_empty(), 20.0, "import after download")
        doc.seek(0.0)
        doc.clear_selection()
        pump(0.2)
        capture("d5_done", dl_ui=True)
        doc.new_project()                      # reset for the editing states
        window.show_status("Ready", 0)
        pump(0.5)

        # ---- 01 loaded --------------------------------------------------------
        window.import_files([str(video)])
        wait_until(lambda: not doc.timeline.is_empty(), 20.0, "import")
        doc.seek(0.0)
        doc.clear_selection()
        window.show_status("Ready", 0)
        capture("01_loaded")
        D = doc.duration

        # ---- 02..07 ----------------------------------------------------------
        doc.seek(D * 0.36)
        capture("02_playhead1")
        doc.split_at_playhead()
        capture("03_split1")
        doc.seek(D * 0.66)
        capture("04_playhead2")
        doc.split_at_playhead()
        capture("05_split2")
        mid = doc.timeline.segments[1]
        doc.select([mid.id])
        capture("06_mid_selected")
        doc.delete_selected(ripple=True)
        capture("07_ripple_deleted")

        # ---- 08 detached ------------------------------------------------------
        doc.select([s.id for s in doc.timeline.segments])
        doc.detach_audio()
        doc.select([s.id for s in doc.timeline.segments])      # "all video clips selected"
        capture("08_detached")

        # ---- 09 audio moved ---------------------------------------------------
        # A clip can only slide within its gap, and the first audio clip is flush against the
        # second one, so slide the last audio clip (the one with free space to its right).
        a = doc.timeline.audio[-1]
        doc.move_clip(a.id, a.timeline_start + 1.5)
        doc.select([a.id])
        capture("09_audio_moved")

        # ---- 10 export dialog -------------------------------------------------
        window.show_status("Ready", 0)
        settle()
        size = save_widget(window, "10_export_bg")
        sizes.add(size)
        layout["states"]["10_export_bg"] = state_layout()
        print(f"  10_export_bg.png {size[0]}x{size[1]}")

        dialog = ExportDialog(doc, settings, window)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.show()
        pump(0.3)
        dialog.adjustSize()
        wait_until(lambda: not getattr(dialog, "_loading", False), 5.0, "dialog load")
        pump(1.0)                                   # encoder detection etc.
        dsize = save_widget(dialog, "10_export_dialog")
        dpos = dialog.geometry().topLeft()
        layout["export_dialog"] = {
            "size": [dsize[0], dsize[1]],
            "centered_on_window": [round((WIN_W - dialog.width()) / 2 * DPR),
                                   round((WIN_H - dialog.height()) / 2 * DPR)],
            "pos_in_window_if_centered_logical": [(WIN_W - dialog.width()) // 2,
                                                  (WIN_H - dialog.height()) // 2],
            "reported_pos": [dpos.x(), dpos.y()],
        }
        print(f"  10_export_dialog.png {dsize[0]}x{dsize[1]}")
        dialog.close()
        dialog.deleteLater()
        pump(0.1)

        if len(sizes) != 1:
            print(f"  warning: main-window captures differ in size: {sizes}")
        w, h = next(iter(sizes))
        layout["capture_size"] = [w, h]
        layout["demo_media"] = {"name": VIDEO_NAME, "duration": round(D, 3)}
        (OUT / "layout.json").write_text(json.dumps(layout, indent=2), encoding="utf-8")
        make_sheet()
    except Exception:
        import traceback
        traceback.print_exc()
        exit_code = 1
    finally:
        if window is not None:
            try:
                window.document._set_dirty(False)
                window.close()
                window.thumbnails.shutdown()
                window.waveforms.shutdown()
            except Exception:  # noqa: BLE001
                pass
            window.deleteLater()
        QCoreApplication.processEvents()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(public, ignore_errors=True)
        try:
            public.parent.rmdir()
        except OSError:
            pass
    return exit_code


def make_sheet() -> None:
    """Contact sheet of the main-window captures for a quick look."""
    from PIL import Image
    names = ["00_empty", "d1_url_focus", "d2_url_typed", "d2b_fetching", "d3_info",
             "d4_dl_10", "d4_dl_45", "d4_dl_80", "d5_done", "01_loaded", "02_playhead1", "03_split1", "04_playhead2",
             "05_split2", "06_mid_selected", "07_ripple_deleted", "08_detached",
             "09_audio_moved", "10_export_bg", "10_export_dialog"]
    tw, th, cols = 640, 400, 4
    rows = (len(names) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), (30, 30, 30))
    for i, n in enumerate(names):
        f = OUT / f"{n}.png"
        if not f.is_file():
            continue
        im = Image.open(f).convert("RGB")
        im.thumbnail((tw - 8, th - 8), Image.LANCZOS)
        sheet.paste(im, ((i % cols) * tw + 4, (i // cols) * th + 4))
    sheet.save(OUT / "_sheet.png")


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
