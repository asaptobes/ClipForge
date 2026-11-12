<#
.SYNOPSIS
    Builds the portable ClipForge Windows application with PyInstaller.

.DESCRIPTION
    1. Creates .venv and installs dependencies if needed.
    2. Downloads FFmpeg/FFprobe into vendor\ffmpeg if they are not there yet.
    3. Runs the test suite (skip with -SkipTests).
    4. Builds dist\ClipForge\ClipForge.exe and smoke-tests the packaged app.
    5. Optionally zips it (-Zip) to dist\ClipForge-<version>-win64.zip.
    6. Optionally builds the Inno Setup installer (-Installer) to dist\ClipForge-Setup-<version>.exe.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -SkipTests -Zip
#>
param(
    [switch]$SkipTests,
    [switch]$Zip,
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

if (-not (Test-Path $py)) {
    Step "Creating virtual environment"
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { & py -3.12 -m venv .venv } else { & python -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12+ is required (https://www.python.org/downloads/)." }
}

Step "Installing dependencies"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$ffmpegDir = Join-Path $root "vendor\ffmpeg"
if (-not (Test-Path (Join-Path $ffmpegDir "ffmpeg.exe"))) {
    Step "Downloading FFmpeg (BtbN GPL shared build)"
    New-Item -ItemType Directory -Force $ffmpegDir | Out-Null
    $zipPath = Join-Path $root "vendor\ffmpeg.zip"
    $url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-shared-8.1.zip"
    & curl.exe -L --fail -s -o $zipPath $url
    if ($LASTEXITCODE -ne 0) { throw "FFmpeg download failed" }
    & tar -xf $zipPath -C (Join-Path $root "vendor")
    $extracted = Get-ChildItem (Join-Path $root "vendor") -Directory | Where-Object { $_.Name -like "ffmpeg-n*" } | Select-Object -First 1
    Copy-Item (Join-Path $extracted.FullName "bin\*") $ffmpegDir -Recurse -Force
    Copy-Item (Join-Path $extracted.FullName "LICENSE.txt") $ffmpegDir -Force
    Remove-Item $extracted.FullName -Recurse -Force
    Remove-Item $zipPath -Force
    Remove-Item (Join-Path $ffmpegDir "ffplay.exe") -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path (Join-Path $root "clipforge\resources\icon.ico"))) {
    Step "Generating application icon"
    & $py tools\make_icon.py
}

if (-not $SkipTests) {
    Step "Running tests"
    $env:QT_QPA_PLATFORM = "offscreen"
    & $py -m pytest tests -q
    $testExit = $LASTEXITCODE
    Remove-Item Env:\QT_QPA_PLATFORM
    if ($testExit -ne 0) { throw "Tests failed - not building. Use -SkipTests to build anyway." }
}

Step "Building with PyInstaller"
$ErrorActionPreference = "Continue"      # PyInstaller logs to stderr; judge by exit code instead
& $py -m PyInstaller ClipForge.spec --noconfirm --clean --log-level WARN 2>&1 | ForEach-Object { "$_" }
$buildExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($buildExit -ne 0) { throw "PyInstaller failed (exit code $buildExit)" }

$exe = Join-Path $root "dist\ClipForge\ClipForge.exe"
if (-not (Test-Path $exe)) { throw "Build finished but $exe is missing" }

Step "Smoke-testing the packaged app"
$proc = Start-Process $exe -ArgumentList "--smoke-test" -PassThru
if (-not $proc.WaitForExit(90000)) { $proc.Kill(); throw "Packaged app did not exit from the smoke test" }
if ($proc.ExitCode -ne 0) { throw "Packaged app smoke test failed with exit code $($proc.ExitCode) (see %LOCALAPPDATA%\ClipForge\logs)" }

$sizeMb = [math]::Round(((Get-ChildItem (Join-Path $root "dist\ClipForge") -Recurse | Measure-Object Length -Sum).Sum / 1MB), 0)
Write-Host "`nBuilt $exe ($sizeMb MB folder)" -ForegroundColor Green

if ($Zip) {
    Step "Creating zip"
    $version = (& $py -c "import clipforge; print(clipforge.__version__)").Trim()
    $zipOut = Join-Path $root "dist\ClipForge-$version-win64.zip"
    if (Test-Path $zipOut) { Remove-Item $zipOut -Force }
    Compress-Archive -Path (Join-Path $root "dist\ClipForge") -DestinationPath $zipOut
    Write-Host "Created $zipOut" -ForegroundColor Green
}

if ($Installer) {
    Step "Building the installer (Inno Setup)"
    $iscc = $null
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source }
    foreach ($candidate in @(
            (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"))) {
        if (-not $iscc -and (Test-Path $candidate)) { $iscc = $candidate }
    }
    if (-not $iscc) {
        throw "Inno Setup 6 (ISCC.exe) was not found. Install it with: winget install JRSoftware.InnoSetup"
    }
    $version = (& $py -c "import clipforge; print(clipforge.__version__)").Trim()
    $ErrorActionPreference = "Continue"
    & $iscc /Qp "/DAppVersion=$version" (Join-Path $root "installer\ClipForge.iss") 2>&1 | ForEach-Object { "$_" }
    $isccExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($isccExit -ne 0) { throw "Inno Setup failed (exit code $isccExit)" }
    $setup = Join-Path $root "dist\ClipForge-Setup-$version.exe"
    if (-not (Test-Path $setup)) { throw "Inno Setup finished but $setup is missing" }
    $setupMb = [math]::Round((Get-Item $setup).Length / 1MB, 0)
    Write-Host "Created $setup ($setupMb MB)" -ForegroundColor Green
}
