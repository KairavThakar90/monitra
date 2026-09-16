<#
.SYNOPSIS
    Build the portable Monitra zip (dist\Monitra-Portable-<version>.zip).

.DESCRIPTION
    Packages the same PyInstaller build the installer uses, plus a marker file
    that switches the application into portable mode. Run
    scripts\build_windows.ps1 first.

    Portable mode changes exactly one thing: where runtime data goes. A normal
    installed build writes its SQLite database, sync queue and logs to
    %USERPROFILE%\.monitra. A portable build writes them to a `data\` folder
    beside Monitra.exe instead, so the whole thing -- application and tracked
    time -- travels on a USB stick or a synced folder. See core\paths.py.

    Important: portable data is still never written into a protected location.
    If the portable folder is extracted somewhere read-only, the application
    falls back to %USERPROFILE%\.monitra rather than failing to start.

.EXAMPLE
    .\scripts\build_portable.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$DesktopRoot = Split-Path -Parent $PSScriptRoot
Set-Location $DesktopRoot

$Source = Join-Path $DesktopRoot 'dist\Monitra'
if (-not (Test-Path (Join-Path $Source 'Monitra.exe'))) {
    throw "dist\Monitra\Monitra.exe not found. Run .\scripts\build_windows.ps1 first."
}

$VenvPython = Join-Path $DesktopRoot '.venv-build\Scripts\python.exe'
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { 'python' }
# The artifact version carries the pre-release label of an internal test
# build (<version>-<prerelease>); it is the plain version for a production
# release.
$Version = & $PythonExe -c "import version; print(version.artifact_version())"
if ($LASTEXITCODE -ne 0) { throw "could not read the version from version.py" }

$Staging = Join-Path $DesktopRoot "build\portable\Monitra-Portable-$Version"
$ZipPath = Join-Path $DesktopRoot "dist\Monitra-Portable-$Version.zip"

Write-Host "==> Building portable Monitra $Version" -ForegroundColor Cyan

# Stage a copy rather than zipping dist\Monitra in place: the marker file must
# exist in the portable package and must NOT exist in the installed one, and
# the two are built from the same source tree.
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Staging
New-Item -ItemType Directory -Force -Path $Staging | Out-Null
Copy-Item -Recurse -Force (Join-Path $Source '*') $Staging

# The marker's presence is what makes the build portable; its contents are
# ignored by the application, so they are addressed to whoever opens it.
@"
This file marks this folder as a portable Monitra installation.

While it is present, Monitra keeps its local database, sync queue and logs in
the 'data' folder beside Monitra.exe, instead of in your user profile. Delete
this file and Monitra will behave like a normally installed copy and use
%USERPROFILE%\.monitra instead -- note that this does NOT move any data you
have already recorded.

Do not put this folder inside 'C:\Program Files': Windows makes that location
read-only for standard users, and Monitra would have to fall back to your user
profile to store anything.
"@ | Set-Content -Path (Join-Path $Staging 'monitra.portable') -Encoding utf8

# A README next to the .exe, because a portable build has no installer to
# explain itself and no Start Menu entry to be found from.
@"
Monitra $Version — portable build

To run:      double-click Monitra.exe
To remove:   delete this folder (this also deletes the 'data' folder, and with
             it any tracked time that has not yet synced to the server)

No installation, no administrator rights, and no Python required.

This build is unsigned, so Windows SmartScreen may warn the first time it
runs: choose "More info" then "Run anyway".

Your data lives in the 'data' folder beside Monitra.exe. Back that folder up
if the tracked time in it matters.
"@ | Set-Content -Path (Join-Path $Staging 'README.txt') -Encoding utf8

New-Item -ItemType Directory -Force -Path (Join-Path $DesktopRoot 'dist') | Out-Null
Remove-Item -Force -ErrorAction SilentlyContinue $ZipPath

# -Force is required, not belt-and-braces: Compress-Archive refuses to write
# over an existing archive, and it reports that refusal as a non-terminating
# error that slips past $ErrorActionPreference. Rebuilding a second time
# without it left the previous zip in place and failed later, in the size
# report, with a confusing "path not found".
Compress-Archive -Path $Staging -DestinationPath $ZipPath -CompressionLevel Optimal -Force

if (-not (Test-Path $ZipPath)) { throw "the portable archive was not created at $ZipPath" }

$SizeMb = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)

Write-Host ""
Write-Host "==> Portable build complete" -ForegroundColor Green
Write-Host "    $ZipPath ($SizeMb MB)"
