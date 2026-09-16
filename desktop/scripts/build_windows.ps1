<#
.SYNOPSIS
    Build the Monitra Windows application (dist\Monitra\Monitra.exe).

.DESCRIPTION
    Produces the PyInstaller onedir build that the installer and the portable
    zip are both made from. Run this first; scripts\build_installer.ps1 and
    scripts\build_portable.ps1 consume its output.

    The build runs from a dedicated virtual environment containing only this
    application's own dependencies plus PyInstaller, created here if it does
    not exist. That is not tidiness: PyInstaller bundles whatever is
    importable in the environment it runs in if any code path references it,
    including inside a try/except ImportError. Building from a developer's
    everyday environment (which tends to accumulate numpy, Pillow, psutil and
    friends from other projects) produced a 160MB build of an application
    that imports none of them; the same source from a clean venv produced
    79MB with identical functionality.

.PARAMETER Clean
    Delete dist\ and build\work\ first. Use after changing the spec, the
    excludes list, or requirements.txt -- PyInstaller's incremental cache
    does not notice all of those.

.EXAMPLE
    .\scripts\build_windows.ps1
    .\scripts\build_windows.ps1 -Clean
#>
[CmdletBinding()]
param(
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

# Resolve desktop/ from this script's own location, so the script works from
# any working directory -- including a CI runner's checkout root.
$DesktopRoot = Split-Path -Parent $PSScriptRoot
Set-Location $DesktopRoot

$VenvDir    = Join-Path $DesktopRoot '.venv-build'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'

Write-Host "==> Monitra Windows build" -ForegroundColor Cyan
Write-Host "    desktop root: $DesktopRoot"

# ── 1. Build environment ────────────────────────────────────────────────────
if (-not (Test-Path $VenvPython)) {
    Write-Host "==> Creating clean build venv ($VenvDir)" -ForegroundColor Cyan
    python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "failed to create the build virtualenv" }
}

Write-Host "==> Installing runtime + build dependencies" -ForegroundColor Cyan
& $VenvPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { throw "pip self-upgrade failed" }
& $VenvPython -m pip install --quiet -r requirements.txt -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }

# ── 2. Version ──────────────────────────────────────────────────────────────
# Read from version.py, the single source of truth, rather than repeating it.
$Version = & $VenvPython -c "import version; print(version.VERSION)"
if ($LASTEXITCODE -ne 0) { throw "could not read the version from version.py" }
# The pre-release form (<version>-<prerelease>) for an internal test build,
# used only in artifact names; the same as $Version for a production release.
$ArtifactVersion = & $VenvPython -c "import version; print(version.artifact_version())"
if ($LASTEXITCODE -ne 0) { throw "could not read the artifact version from version.py" }
Write-Host "==> Building Monitra $ArtifactVersion" -ForegroundColor Cyan

# ── 3. Clean ────────────────────────────────────────────────────────────────
if ($Clean) {
    Write-Host "==> Cleaning previous build output" -ForegroundColor Cyan
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $DesktopRoot 'dist')
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $DesktopRoot 'build\work')
}

# ── 4. Icons ────────────────────────────────────────────────────────────────
# build/ is gitignored, so the icons are generated rather than committed. The
# spec fails loudly if they are missing, so generate them every time -- it
# takes under a second and removes a whole class of "works on my machine".
Write-Host "==> Generating application icons" -ForegroundColor Cyan
$env:QT_QPA_PLATFORM = 'offscreen'
& $VenvPython tools\generate_app_icon.py
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }
Remove-Item Env:\QT_QPA_PLATFORM

# ── 5. Package ──────────────────────────────────────────────────────────────
Write-Host "==> Running PyInstaller" -ForegroundColor Cyan
& $VenvPython -m PyInstaller packaging\monitra.spec `
    --distpath dist --workpath build\work --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$ExePath = Join-Path $DesktopRoot 'dist\Monitra\Monitra.exe'
if (-not (Test-Path $ExePath)) { throw "build reported success but $ExePath is missing" }

# ── 6. Report ───────────────────────────────────────────────────────────────
$SizeMb = [math]::Round(
    ((Get-ChildItem (Join-Path $DesktopRoot 'dist\Monitra') -Recurse |
        Measure-Object -Property Length -Sum).Sum / 1MB), 1)
$Info = (Get-Item $ExePath).VersionInfo

Write-Host ""
Write-Host "==> Build complete" -ForegroundColor Green
Write-Host "    executable : $ExePath"
Write-Host "    version    : $($Info.ProductVersion)"
Write-Host "    total size : $SizeMb MB"
Write-Host ""
Write-Host "    This build is UNSIGNED. See BUILD.md 'Signing' before public distribution."
Write-Host "    Next: .\scripts\build_installer.ps1   (Monitra-Setup-$ArtifactVersion.exe)"
Write-Host "          .\scripts\build_portable.ps1    (Monitra-Portable-$ArtifactVersion.zip)"
