# ClipForge

ClipForge is a Windows 10/11 desktop app that downloads videos from YouTube, Reddit, TikTok and public Instagram posts, lets you cut and rearrange them on a non-destructive timeline, and exports the result as an MP4.

Built with Python 3.12, PySide6, yt-dlp, FFmpeg and PyInstaller.

## Features

- Download a video by pasting its link (`Ctrl+L`) from YouTube (videos and Shorts), Reddit, TikTok, Instagram (public posts only), Twitch or Kick, choosing a quality.
- **Download only a section** of a long video or VOD: tick "Download only a section" and enter start and end times, so you never fetch the whole stream.
- **Make vertical clips:** choose a canvas (9:16, 4:5, 1:1, 16:9) and how clips fill it (crop, blurred background or bars). Drag the crop in the preview, or set a Start and End position for a slow pan.
- **Auto-captions:** generate word-by-word captions locally with Whisper (free, private, your computer only), edit them, pick a style (Pop, Classic, Boxed, Minimal) and burn them into the export. The speech model downloads once, after asking you.
- **Export presets** for TikTok / Reels / Shorts, Instagram feed, square and YouTube.
- Import local video files (`.mp4`, `.mov`, `.mkv`, `.webm`, `.m4v`) with `Ctrl+I` or drag and drop.
- Single-track timeline: split, delete, ripple delete, trim, reorder, mute, close gaps.
- Undo and redo for every edit.
- Snapping to clip edges and the playhead (hold `Alt` to bypass while dragging).
- Zoom, zoom-to-fit, and jump to previous or next cut.
- Live preview with transport controls and frame stepping.
- Export to MP4 with automatic fast or frame-accurate mode, and NVIDIA, Intel or AMD hardware encoding with CPU fallback.
- Save and open projects as `.clipforge` files. Missing media can be relinked.
- Light, dark and midnight themes.

## Install

1. Download `ClipForge-Setup-x.y.z.exe` from the [GitHub Releases page](https://github.com/asaptobes/ClipForge/releases).
2. Run it and follow the wizard. It installs for the current user by default and needs no administrator rights; choose "all users" in the prompt if you want a machine-wide install. You can add a desktop shortcut and associate `.clipforge` project files with ClipForge.
3. Start ClipForge from the Start menu.

**SmartScreen warning.** The installer is not code-signed yet, so Windows SmartScreen may show "Windows protected your PC". Click **More info**, then **Run anyway**.

Updating: run a newer setup over the existing install. Your settings (`%APPDATA%\ClipForge`) and cache and logs (`%LOCALAPPDATA%\ClipForge`) are kept on update and on uninstall. Uninstall from Windows Settings > Apps.

**Portable alternative.** Download `ClipForge-x.y.z-win64.zip`, extract the whole folder anywhere and run `ClipForge.exe`. No installation is needed.

**Build the installer from source.** Install [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`), then run `.\build.ps1 -Installer`. The result is `dist\ClipForge-Setup-<version>.exe`. See [Building the exe](#building-the-exe).

## Quick start (end users)

No Python installation is needed.

1. Get the `ClipForge` folder (the build output is `dist\ClipForge`, or the zip `dist\ClipForge-1.0.0-win64.zip`).
2. Extract or copy the whole folder. Keep `ClipForge.exe` together with the folder contents, because FFmpeg lives inside it.
3. Start the app:

```powershell
.\ClipForge\ClipForge.exe
```

The app is not code-signed. If Windows or antivirus blocks it, see [Troubleshooting](#troubleshooting).

## Usage walkthrough

### Get media onto the timeline

- **Download:** paste a YouTube, Reddit, TikTok or public Instagram link (`Ctrl+L`), pick a quality, and download. Instagram often asks for a login even for public posts; ClipForge never stores passwords or cookies, so those posts cannot be downloaded. Downloads are saved to `%USERPROFILE%\Videos\ClipForge\Downloads` by default.
- **Import:** `Ctrl+I` opens a file picker. You can also drag video files into the window.
- Add a media item to the timeline from the media list panel.

### Edit on the timeline

- **Select:** click a clip. `Ctrl`+click adds or removes clips from the selection. Double-click a clip to zoom to it.
- **Split:** move the playhead, then press `S` or `Ctrl+B`.
- **Trim:** drag the left or right edge of a clip. Edges snap to other edges and the playhead.
- **Delete:** `Del` leaves a gap. `Shift+Del` (ripple delete) removes the clip and closes the gap behind it.
- **Reorder:** drag a clip body to a new position.
- **Mute:** press `M` to mute or unmute the selected clips.
- **Close gaps:** use the gaps button in the timeline toolbar to remove all gaps.
- **Undo / redo:** `Ctrl+Z` and `Ctrl+Y` (or `Ctrl+Shift+Z`).
- **Zoom:** `Ctrl`+mouse wheel zooms around the cursor, or use `Ctrl+=` / `Ctrl+-`. `Shift+Z` or `Ctrl+0` fits the whole timeline.

### Playback

- `Space` plays and pauses. `Left` and `Right` step one frame. `Shift+Left` and `Shift+Right` step ten frames.
- `J`, `K`, `L` shuttle backward, stop, and forward.
- `Home` and `End` jump to the start and end. `Up` and `Down` jump to the previous and next cut.

### Export

1. Press `Ctrl+E` (or the Export button).
2. Choose the file name, folder, codec, resolution, frame rate, quality, and whether to include audio.
3. Choose the **Export mode** (see [Export options](#export-options)) and the **Range** (entire timeline or selected clips).
4. Click **Export**. The result page offers **Open file** and **Show in folder**.

The output is first written as `<name>.partial.mp4` and renamed when complete, so a failed or cancelled export never leaves a broken file. ClipForge refuses to overwrite one of your source files and asks before replacing an existing output.

### Save and open projects

- `Ctrl+S` saves, `Ctrl+Shift+S` saves as, and `Ctrl+O` opens a project. The Open menu also lists recent projects.
- A project is a `.clipforge` JSON file. It stores references to your media, not copies of it. Source paths are saved both as absolute paths and relative to the project file, so a project folder that is moved together with its media still opens.

### Missing media

If a source file has moved, ClipForge shows it as offline when a project opens. Right-click the clip in the media list and choose **Relink…** to point to the new file. Exports refuse to run while media is offline.

## Keyboard shortcuts

| Action | Shortcut |
| --- | --- |
| New project | `Ctrl+N` |
| Open project | `Ctrl+O` |
| Save project | `Ctrl+S` |
| Save project as | `Ctrl+Shift+S` |
| Import video | `Ctrl+I` |
| Settings | `Ctrl+,` |
| Export | `Ctrl+E` |
| Paste link (focus the URL field) | `Ctrl+L` |
| Undo | `Ctrl+Z` |
| Redo | `Ctrl+Y` or `Ctrl+Shift+Z` |
| Split at playhead | `S` or `Ctrl+B` |
| Delete selected clips | `Del` or `Backspace` |
| Ripple delete | `Shift+Del` or `Shift+Backspace` |
| Mute clip | `M` |
| Select all clips | `Ctrl+A` |
| Deselect | `Ctrl+D` |
| Play / pause | `Space` |
| Previous / next frame | `Left` / `Right` |
| Back / forward 10 frames | `Shift+Left` / `Shift+Right` |
| Shuttle back / stop / forward | `J` / `K` / `L` |
| Go to start / end | `Home` / `End` |
| Previous / next cut | `Up` / `Down` |
| Zoom in | `Ctrl+=` or `Ctrl++` |
| Zoom out | `Ctrl+-` |
| Zoom to fit | `Shift+Z` or `Ctrl+0` |
| Zoom timeline | `Ctrl`+mouse wheel |
| Cancel a drag in the timeline | `Esc` |
| Bypass snapping while dragging | Hold `Alt` |

## Settings

Open settings with `Ctrl+,`. Settings are saved to `%APPDATA%\ClipForge\settings.json`.

| Section | Setting | Default | Notes |
| --- | --- | --- | --- |
| Folders | Downloads | `%USERPROFILE%\Videos\ClipForge\Downloads` | Where downloads are saved |
| Folders | Exports | `%USERPROFILE%\Videos\ClipForge\Exports` | Default export folder |
| Folders | Cache | `%LOCALAPPDATA%\ClipForge\cache` | Thumbnails and waveforms. Use **Clear cache** to free space. Your media is not touched |
| Folders | Temporary files | `%LOCALAPPDATA%\ClipForge\temp` | Scratch files used during export |
| Defaults | Download quality | 1080p | Best available, 2160, 1440, 1080, 720, or 480 |
| Defaults | Export codec | H.264 | H.264 (most compatible) or H.265 (smaller files) |
| Defaults | Hardware acceleration | Auto | Auto, On, or Off |
| Appearance | Theme | Dark | Dark, Midnight, or Light. Applied after a restart |

The dialog also shows the FFmpeg version and has an **Open log folder** button.

## Export options

These options appear in the export dialog. Settings chosen there are saved with the project.

- **Codec:** H.264 (most compatible) or H.265 (smaller files). The container is always MP4.
- **Resolution:** Source, 2160p, 1440p, 1080p, 720p, or 480p. The number is the short side, so aspect ratio is kept. ClipForge never upscales past the source.
- **Frame rate:** Source, 24, 25, 30, 50, or 60 fps.
- **Quality:** High, Medium, or Low.
- **Include audio:** on or off.
- **Hardware acceleration:** Auto, On, or Off.
- **Export mode:**
  - **Automatic** uses fast export when it is possible, and accurate export otherwise.
  - **Fast (no re-encoding)** copies the streams. It is only allowed when the output can be frame-exact without re-encoding. Otherwise the export is refused with the reason.
  - **Accurate (frame-exact)** re-encodes the whole timeline in one pass. It works with any resolution, frame rate, and mix of sources.
- **Range:** entire timeline or selected clips.

### Hardware encoding

- **Auto** and **On** try NVIDIA NVENC first, then Intel Quick Sync, then AMD AMF. Each candidate must pass a real test encode.
- If no hardware encoder works, or **Off** is selected, ClipForge uses the CPU encoder (libx264 or libx265).
- If a hardware encoder fails during an export, the export is retried once on the CPU.

The export dialog shows which encoder will be used.

## Running from source

Requires Python 3.12 or newer.

Create a virtual environment and install the dependencies:

```powershell
py -3.12 -m venv .venv
```

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

FFmpeg is expected in `vendor\ffmpeg` (`ffmpeg.exe`, `ffprobe.exe`, and their DLLs). `build.ps1` downloads it automatically the first time it runs if `vendor\ffmpeg\ffmpeg.exe` is missing. You can also copy FFmpeg builds there by hand. ClipForge also falls back to `ffmpeg` on your `PATH`.

Start the app:

```powershell
.venv\Scripts\python.exe -m clipforge
```

You can pass a `.clipforge` project or a video file as an argument, and `--debug` enables verbose logging:

```powershell
.venv\Scripts\python.exe -m clipforge --debug "C:\path\to\video.mp4"
```

## Running tests

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
```

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

The tests live in `tests\`. They generate small synthetic videos with FFmpeg, so FFmpeg must be available. Hardware-encoder tests only check NVENC when it is available.

## Building the exe

Run from the project root:

```powershell
.\build.ps1
```

The script:

1. Creates `.venv` and installs dependencies if needed.
2. Downloads FFmpeg into `vendor\ffmpeg` if it is missing.
3. Runs the test suite.
4. Builds `dist\ClipForge\ClipForge.exe` with PyInstaller (`ClipForge.spec`).
5. Starts the packaged app with `--smoke-test` to check that it runs and its dependencies are present.

To skip the tests:

```powershell
.\build.ps1 -SkipTests
```

To also create a zip archive, `dist\ClipForge-<version>-win64.zip`:

```powershell
.\build.ps1 -SkipTests -Zip
```

To build the Windows installer (needs Inno Setup 6), `dist\ClipForge-Setup-<version>.exe`:

```powershell
.\build.ps1 -SkipTests -Installer
```

The version comes from `clipforge\__init__.py`. The wizard images in `installer\` are generated from the logo by `installer\make_wizard_images.py` and are committed, so the build does not need Pillow.

Omit `-SkipTests` if you want the build to stop when tests fail.

## Architecture

ClipForge is split into four layers, plus shared modules.

- **`clipforge/core/`** holds pure data and logic with no UI. `model.py` defines `MediaSource`, `Segment`, and `Timeline`. `history.py` implements undo and redo. `project.py` reads and writes `.clipforge` files. `timecode.py` formats times and sizes.
- **`clipforge/document.py`** is the `Document` that the UI edits. It owns the project and history, and exposes editing actions such as split, delete, trim, reorder, mute, and close gaps.
- **`clipforge/media/`** wraps FFmpeg and yt-dlp. `ffmpeg.py` finds and runs FFmpeg and picks encoders. `probe.py` reads media information. `export.py` builds and runs export plans. `downloader.py` handles YouTube downloads. `thumbnails.py` and `waveform.py` create cached previews.
- **`clipforge/ui/`** is the PySide6 interface: `main_window.py` (menus, shortcuts, project lifecycle), `timeline.py`, `preview.py`, `left_panel.py`, `export_dialog.py`, `settings_dialog.py`, and `theme.py`.
- **Shared:** `workers.py` runs background tasks with cancellation. `settings.py` stores user settings. `paths.py` finds data folders and bundled tools. `logging_setup.py` configures logging.

Time is stored in two coordinate systems:

- **Source time** is a position inside the original media file (`source_in` and `source_out` on a segment).
- **Timeline time** is the position on the edited timeline (`timeline_start` and `timeline_end`). Gaps are the space between segments in timeline time.

Splitting, trimming, and reordering change only segments. Source files are never modified.

## Troubleshooting

### FFmpeg not found

The app reports that `ffmpeg` was not found. Check that `ffmpeg.exe` and `ffprobe.exe` are in the `ffmpeg` folder next to `ClipForge.exe`, or in `vendor\ffmpeg` when running from source. Reinstalling from the build output restores the folder.

### Blocked by Windows Smart App Control or antivirus

ClipForge and the FFmpeg binaries are not code-signed, so Smart App Control or antivirus software may block them. Ask your IT department to allow the `ClipForge` folder, or run ClipForge from source as described above.

### Downloads fail after a site changes

In the app, open **Settings > Downloader > Check for updates** to fetch the newest yt-dlp (checksum-verified, applied after a restart). From source, you can also update it manually:

YouTube changes often break downloads until yt-dlp is updated. In the venv, run:

```powershell
.venv\Scripts\python.exe -m pip install -U yt-dlp
```

Then rebuild the exe with `.\build.ps1` if you use the packaged app.

### yt-dlp warns about a missing JavaScript runtime

yt-dlp may warn that it has no JavaScript runtime, and some formats may be missing from the list. ClipForge does not configure a JavaScript runtime itself.

### Log file

The log is at:

```
%LOCALAPPDATA%\ClipForge\logs\clipforge.log
```

It rotates at about 2 MB and keeps four older copies. Use `--debug` for more detail. **Open log folder** in Settings opens it.

## Limitations

- **Fast export** is only used when every cut lands on a keyframe, all clips come from sources with the same codec, size, frame rate, and audio layout, the output settings are "Source", there are no gaps, and muted clips do not need re-encoding. Otherwise export uses accurate mode.
- **Single video track.** The timeline has one track.
- **No effects, transitions, or titles.**
- **Preview** uses Qt Multimedia, so unusual codecs may not preview even though they export.
- **Theme changes** take effect after restarting ClipForge.
- **Unsigned executable.** The build does not sign `ClipForge.exe` or the bundled FFmpeg.
- **Downloads depend on YouTube and yt-dlp.** Availability and available formats can change without notice.

## Legal

You are responsible for having the right to download and edit any content you use with ClipForge, and for complying with the terms of the service you download from and the copyright laws that apply to you.

ClipForge downloads through the public yt-dlp Python API only. It does not use cookies or credentials, and it does not circumvent DRM or anti-bot measures.
