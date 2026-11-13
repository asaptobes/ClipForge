# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build recipe for ClipForge (portable one-folder app).

Build with:  .\\build.ps1      (or:  pyinstaller ClipForge.spec --noconfirm)
Output:      dist\\ClipForge\\ClipForge.exe
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.getcwd())
FFMPEG_DIR = os.path.join(ROOT, "vendor", "ffmpeg")

datas = [(os.path.join(ROOT, "clipforge", "resources"), os.path.join("clipforge", "resources"))]
if os.path.isfile(os.path.join(FFMPEG_DIR, "ffmpeg.exe")):
    # Shipped as plain data so PyInstaller does not rewrite the binaries; found at
    # run time by clipforge.paths.find_tool() under <bundle>/ffmpeg/.
    for name in os.listdir(FFMPEG_DIR):
        datas.append((os.path.join(FFMPEG_DIR, name), "ffmpeg"))
else:
    print("WARNING: vendor/ffmpeg not found - the build will rely on FFmpeg being on PATH")

hiddenimports = collect_submodules("yt_dlp") + ["av", "numpy"]
# local auto-captions: faster-whisper ships its VAD model as a data file (silero_vad_v6.onnx)
datas += collect_data_files("faster_whisper")
hiddenimports += ["faster_whisper", "ctranslate2"]

# Qt modules ClipForge never uses - leaving them out saves several hundred MB.
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtWebView", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets", "PySide6.QtQml", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtSerialBus", "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech", "PySide6.QtSpatialAudio", "PySide6.QtHttpServer",
    "PySide6.QtUiTools", "PySide6.QtXml", "PySide6.QtConcurrent", "PySide6.QtPrintSupport",
    "tkinter", "pytest", "PIL", "matplotlib", "IPython",
]

a = Analysis(
    ["run_clipforge.py"],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ClipForge",
    icon=os.path.join(ROOT, "clipforge", "resources", "icon.ico"),
    console=False,
    debug=False,
    strip=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ClipForge")
