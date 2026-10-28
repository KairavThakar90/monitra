# Monitra Desktop — Setup & Running (Windows + macOS)

This is the from-source setup and run procedure, for either platform. For
packaging a distributable Windows `.exe`, see [BUILD.md](BUILD.md) instead
— that document doesn't apply to macOS (see its note at the bottom of
this file).

## Requirements

- **Python 3.10+**, any platform build.
- **Windows**: no extra system packages.
- **macOS**: Xcode Command Line Tools (`xcode-select --install` if not
  already present) — only needed if pip has to compile `pyobjc` from
  source rather than use a prebuilt wheel for your exact Python version;
  most common versions have one.

Every dependency is declared in [requirements.txt](requirements.txt):
PySide6, httpx, and python-dotenv install identically on both platforms;
`pyobjc-framework-Cocoa` and `pyobjc-framework-Quartz` are macOS-only and
`pip` skips them automatically on Windows (they're marked
`; sys_platform == "darwin"`) — one requirements file, no per-OS variant
needed.

## Setup

From `desktop/`:

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

**macOS (bash/zsh):**
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

**Windows:**
```powershell
.venv\Scripts\python main.py
```

**macOS:**
```bash
.venv/bin/python main.py
```

Both platforms store their local cache database and logs at
`~/.monitra/` (via `Path.home()`, so this resolves correctly on either
OS) — nothing else needs configuring for a first run. By default the app
talks to the live production backend; only if you need to point a
particular run at a different one, copy `.env.example` to `.env` in
`desktop/` and set `SMS_API_BASE_URL`.

## What's platform-specific, and what to expect on macOS

Reviewed and made cross-platform in this pass — see the code comments at
each for the full detail:

| Feature | Windows | macOS |
|---|---|---|
| Login, tasks, timer, manual time entries, sync | ✅ | ✅ |
| Activity summary (reading percentages already stored on the backend) | ✅ | ✅ |
| App-usage tracking (which application is focused) | ✅ (`tracking/active_window.py`) | ✅, via `pyobjc` (`NSWorkspace`) |
| Browser URL tracking (which site is open) | ✅ | ✅, via the same `pyobjc` window-title lookup |
| Keyboard/mouse activity counting + unwanted-activity detection | ✅ (`pynput`, no special permission) | ✅ (listen-only Quartz event tap; requires **Input Monitoring** permission — see below) |
| App-icon lookup for the Activity tabs | ✅ | ✅ (Qt's own cross-platform `QFileIconProvider`, plus known `/Applications/*.app` paths) |
| Windows taskbar toast identity (`set_windows_app_identity`) | ✅ | not applicable — macOS notification identity comes from the app bundle, not a runtime call |

**macOS Input Monitoring permission**: keyboard/mouse activity *counting*
(`background_services/activity/input_counter.py`, through the Quartz event tap
in `mac_input_tap.py`) needs **Input Monitoring** (System Settings → Privacy &
Security → Input Monitoring; some macOS versions gate it under Accessibility
instead). Windows needs no permission for this. When denied, the app keeps
working — nothing is recorded or fabricated, the user is told once where the
switch is (`activity/input_access.py`), the TODAY'S ACTIVITY card says "Needs
Input Monitoring" while the timer runs, the counter is retried every minute,
and unwanted-activity detection stays quiet; nothing crashes. A permission
granted after launch may need Monitra to be quit and reopened before macOS
applies it ("Restart Monitra to resume"). A grant that is not in effect for the
running build (macOS keys it to the bundle's signature, so a re-signed build is
a new app) leaves the tap installed but deaf to other applications; Monitra
notices this by comparing against the OS's own idle clock, restarts the
listener once, and then says "Not receiving input" on the card and in a
notification. Idle *detection* on macOS is off by default; set
`MONITRA_MAC_IDLE_DETECTION=1` to turn it on (unverified on real hardware). The listeners only run while a timer is actively tracking,
and only aggregate counts (plus tallies for the handful of rule keys, e.g.
CTRL) ever leave the input callbacks — what was typed is never stored or
transmitted.

**macOS Screen Recording permission**: the window-title half of app-usage
and URL tracking (`_macos_active_window_details` in
`tracking/active_window.py`) uses `CGWindowListCopyWindowInfo`, which only
returns real window titles once this app has been granted **Screen
Recording** permission (System Settings → Privacy & Security → Screen
Recording). Without it, the call still succeeds — it just silently omits
window names, so app-usage tracking still records *which app* was used,
but URL tracking (which needs the window/tab title to read a browser's
current page) won't see anything to parse until permission is granted.
This is a one-time, per-machine grant; the app cannot request it
automatically.

**Not verified on real macOS hardware**: the `pyobjc`-based code in
`tracking/active_window.py` was written and unit-tested against mocked
`AppKit`/`Quartz` objects (see
`tests/test_active_window_cross_platform.py`), matching the documented
pyobjc API shape, but this development environment has no macOS machine
to actually run it on. Test it for real on a Mac before relying on it,
the same way any new feature would be — if `NSWorkspace.frontmostApplication()`
or `CGWindowListCopyWindowInfo`'s exact calling convention differs from
what's coded here, this is the first place to look.

## Building the distributable application

[BUILD.md](BUILD.md) is the complete reference: prerequisites, both
platforms, configuration, signing, the release process and clean-machine
testing. In short, from `desktop/`:

```powershell
.\scripts\build_windows.ps1      # dist\Monitra\Monitra.exe
.\scripts\build_installer.ps1    # dist\installer\Monitra-Setup-<version>.exe
.\scripts\build_portable.ps1     # dist\Monitra-Portable-<version>.zip
```
```bash
./scripts/build_macos.sh         # dist/Monitra.app + dist/Monitra-macOS-<arch>-<version>.dmg
```

PyInstaller does not cross-compile: the Windows scripts must run on Windows
and `build_macos.sh` must run on a Mac. `.github/workflows/desktop-release.yml`
runs both on their own runners.

The macOS half — the spec's `BUNDLE`, the `Info.plist` permission strings,
the entitlements, the DMG step and the CI job — is complete but has **never
been executed**, for the same reason as the macOS tracking code above: there
is no Mac in this environment. Treat it as unverified until someone with a
Mac runs it.
