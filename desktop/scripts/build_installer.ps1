<#
.SYNOPSIS
    Build the Monitra Windows installer (dist\installer\Monitra-Setup-<version>.exe).

.DESCRIPTION
    Compiles packaging\windows\monitra.iss with Inno Setup, against the
    PyInstaller output in dist\Monitra\. Run scripts\build_windows.ps1 first.

    The version is read from desktop\version.py and passed to the compiler, so
    the installer, the .exe metadata and the About line can never disagree --
    an installer claiming one version while the binary inside claims another
    makes every
    subsequent support report untrustworthy.

.PARAMETER IsccPath
    Path to Inno Setup's command line compiler. Defaults to the standard
    install location; override for a portable or non-default installation.

.EXAMPLE
    .\scripts\build_installer.ps1
#>
[CmdletBinding()]
param(
    [string]$IsccPath
)

$ErrorActionPreference = 'Stop'

$DesktopRoot = Split-Path -Parent $PSScriptRoot
Set-Location $DesktopRoot

# ── 1. Inputs ───────────────────────────────────────────────────────────────
$BuildOutput = Join-Path $DesktopRoot 'dist\Monitra\Monitra.exe'
if (-not (Test-Path $BuildOutput)) {
    throw "dist\Monitra\Monitra.exe not found. Run .\scripts\build_windows.ps1 first."
}

$VenvPython = Join-Path $DesktopRoot '.venv-build\Scripts\python.exe'
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { 'python' }
$Version = & $PythonExe -c "import version; print(version.VERSION)"
if ($LASTEXITCODE -ne 0) { throw "could not read the version from version.py" }
# The artifact name carries the pre-release label of an internal test build
# (<version>-<prerelease>); the installer's own AppVersion stays numeric.
# Identical to $Version for a production release.
$ArtifactVersion = & $PythonExe -c "import version; print(version.artifact_version())"
if ($LASTEXITCODE -ne 0) { throw "could not read the artifact version from version.py" }

# ── 2. Locate Inno Setup ────────────────────────────────────────────────────
if (-not $IsccPath) {
    # %LOCALAPPDATA%\Programs is where `winget install JRSoftware.InnoSetup`
    # lands by default -- it installs per-user unless run elevated -- and is
    # checked first for that reason.
    $Candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        'ISCC.exe'
    )
    $IsccPath = $Candidates | Where-Object {
        $_ -and ((Test-Path $_) -or (Get-Command $_ -ErrorAction SilentlyContinue))
    } | Select-Object -First 1
}

if (-not $IsccPath) {
    throw @"
Inno Setup 6 was not found.

Install it (a one-time setup step, and the only tool this build needs beyond
Python) from https://jrsoftware.org/isdl.php, or via:

    winget install --id JRSoftware.InnoSetup

then re-run this script. Pass -IsccPath if it is installed somewhere
non-standard.
"@
}

Write-Host "==> Building Monitra $ArtifactVersion installer" -ForegroundColor Cyan
Write-Host "    compiler: $IsccPath"

# ── 3. Compile ──────────────────────────────────────────────────────────────
& $IsccPath "/DAppVersion=$Version" "/DArtifactVersion=$ArtifactVersion" (Join-Path $DesktopRoot 'packaging\windows\monitra.iss')
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed" }

$Setup = Join-Path $DesktopRoot "dist\installer\Monitra-Setup-$ArtifactVersion.exe"
if (-not (Test-Path $Setup)) { throw "compilation reported success but $Setup is missing" }

$SizeMb = [math]::Round((Get-Item $Setup).Length / 1MB, 1)

Write-Host ""
Write-Host "==> Installer built" -ForegroundColor Green
Write-Host "    $Setup ($SizeMb MB)"
Write-Host ""
Write-Host "    This installer is UNSIGNED: Windows SmartScreen will warn on first run."
Write-Host "    Public distribution should sign both Monitra.exe and this installer with"
Write-Host "    a trusted code-signing certificate -- see BUILD.md 'Signing'."
