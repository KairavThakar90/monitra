; ============================================================================
;  Inno Setup script for the Monitra desktop client.
;
;  Build it with (from desktop/ -- scripts/build_installer.ps1 does this for
;  you and passes the version automatically):
;
;      iscc /DAppVersion=<version> [/DArtifactVersion=<version>[-<prerelease>]] packaging\windows\monitra.iss
;
;  Input:  dist\Monitra\           (the PyInstaller onedir build)
;  Output: dist\installer\Monitra-Setup-<artifact version>.exe
;
;  Why Inno Setup rather than NSIS or a zip: it produces a single signed-able
;  .exe, handles upgrade-in-place and a real Add/Remove Programs entry with no
;  scripting, and -- the deciding factor here -- supports a genuine
;  non-administrator install, which is what this application needs (see
;  PrivilegesRequired below).
; ============================================================================

#ifndef AppVersion
  #error AppVersion is not defined. Pass /DAppVersion=<version> -- the value must come from desktop/version.py, which is the single source of truth. Use scripts/build_installer.ps1.
#endif

; ArtifactVersion names the output file and the Add/Remove Programs entry. For
; a production release it is the same as AppVersion; for an internal test
; build the build script passes the pre-release form (<version>-<prerelease>,
; from version.artifact_version()) so the installer a tester downloads can
; never be mistaken for the production one. AppVersion and VersionInfoVersion
; stay the numeric form, which is what Windows requires. No example number
; here on purpose -- see the AppId note below.
#ifndef ArtifactVersion
  #define ArtifactVersion AppVersion
#endif

#define AppName          "Monitra"
#define AppPublisher     "Monitra"
#define AppExeName       "Monitra.exe"
#define SourceDir        "..\..\dist\Monitra"
#define OutputDirectory  "..\..\dist\installer"

[Setup]
; AppId identifies the product across versions. It must never change: it is
; what makes installing a newer version over an older one an upgrade rather
; than a second, parallel installation with its own uninstall entry -- a newer
; build must replace the one already installed, not sit beside it.
;
; Deliberately written without example version numbers: tests/test_packaging.py
; fails on any version-shaped literal in this file, and it is right to, because
; a second literal anywhere in the build chain is how an installer ends up
; claiming a version its binary does not have. An illustration in a comment is
; not worth an exception in the guard.
AppId={{8F3B6A94-2C57-4E1B-9A0D-6B7C4E9A1D22}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#ArtifactVersion}
VersionInfoVersion={#AppVersion}
AppPublisher={#AppPublisher}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}

; PrivilegesRequired=lowest is a deliberate product decision, not a
; convenience. Monitra needs no administrator rights at any point: it reads
; the foreground window title, counts input events through a normal
; user-level hook, talks HTTPS out, and writes only to the user's own home
; directory. Requesting elevation for that would be both a UAC prompt users
; are right to distrust and an unnecessary privilege on a machine running a
; monitoring tool. With `lowest`, {autopf} resolves to
; %LOCALAPPDATA%\Programs, which is user-writable -- so installs, upgrades
; and uninstalls all work for a standard user with no IT involvement.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

OutputDir={#OutputDirectory}
OutputBaseFilename=Monitra-Setup-{#ArtifactVersion}
SetupIconFile=..\..\build\monitra.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

; The PyInstaller build is 64-bit only, matching the 64-bit-only PySide6
; wheels. Saying so here gives a clear "this app cannot run on this PC"
; message instead of an install that fails at first launch.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "startupicon"; Description: "Start {#AppName} automatically when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; The whole PyInstaller onedir tree. Monitra.exe cannot run without the
; _internal\ folder beside it, so this must stay a recursive copy of the
; entire directory rather than a list of hand-picked files.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the installed program files are removed, and only the ones the
; installer created. The user's tracked time, sync queue and logs live in
; %USERPROFILE%\.monitra and are deliberately left in place: uninstalling an
; application must never silently destroy data the user has not finished
; syncing. Removing that folder is a documented manual step (see BUILD.md).
Type: filesandordirs; Name: "{app}\_internal"

[Code]
// A running instance holds its own .exe and DLLs open, so an upgrade that
// proceeds anyway leaves a half-replaced installation behind. Stopping with a
// clear instruction is better than a partial write; Monitra hides to the tray
// on close, so "I closed it" is frequently not true and worth saying plainly.
function InitializeSetup(): Boolean;
begin
  Result := True;
  // Must match WINDOWS_MUTEX_NAME in desktop/core/single_instance.py. The
  // application creates it in the session-local namespace (so two users on
  // one machine can each run Monitra); CheckForMutexes tests the name in
  // both namespaces, so the unqualified name is the correct thing to pass.
  if CheckForMutexes('MonitraRunning') then
  begin
    MsgBox('Monitra is currently running.'#13#10#13#10
      + 'Please quit it first — right-click the Monitra icon in the '
      + 'notification area (bottom-right of the taskbar) and choose Quit — '
      + 'then run this installer again.',
      mbError, MB_OK);
    Result := False;
  end;
end;
