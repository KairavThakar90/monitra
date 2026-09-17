# Building and releasing the Monitra desktop application

How to turn this source tree into something a member of staff can download,
install and use on a machine that has no Python, no virtualenv, no source
checkout and no developer tooling.

**Contents**

1. [What gets produced](#1-what-gets-produced)
2. [Prerequisites](#2-prerequisites)
3. [Windows build](#3-windows-build)
4. [Windows installer](#4-windows-installer)
5. [Windows portable build](#5-windows-portable-build)
6. [macOS build](#6-macos-build)
7. [macOS permissions](#7-macos-permissions)
8. [Configuration](#8-configuration)
9. [Where the application stores data](#9-where-the-application-stores-data)
10. [Versioning](#10-versioning)
11. [Signing and notarization](#11-signing-and-notarization)
12. [Release process](#12-release-process)
13. [Verifying a build](#13-verifying-a-build)
14. [Clean-machine testing](#14-clean-machine-testing)
15. [Reproducibility](#15-reproducibility)
16. [Troubleshooting](#16-troubleshooting)
17. [Known limitations](#17-known-limitations)

---

## 1. What gets produced

| Platform | Artifact | Built by |
|---|---|---|
| Windows | `Monitra-Setup-<version>.exe` — installer | `scripts/build_installer.ps1` |
| Windows | `Monitra-Portable-<version>.zip` — no-install build | `scripts/build_portable.ps1` |
| Windows | `dist/Monitra/` — the raw application both are made from | `scripts/build_windows.ps1` |
| macOS | `Monitra-macOS-<arch>-<version>.dmg` | `scripts/build_macos.sh` |
| macOS | `dist/Monitra.app` | `scripts/build_macos.sh` |

None of these require the user to install Python, PySide6, or anything else.
Everything the application needs is inside the package.

### Why PyInstaller

The application is PySide6 on CPython, with two C-extension dependencies
(`pynput`, and `pyobjc` on macOS) that are imported lazily behind platform
checks. PyInstaller was chosen over the alternatives because:

- **It is already proven on this codebase.** A real, launched, working
  Windows build predates this document; the packaging work here extended it
  rather than replacing a known-good toolchain on speculation.
- **Nuitka** compiles to C and produces a faster, smaller binary, but its Qt
  plugin support requires per-release attention, its build times are an order
  of magnitude longer, and the payoff — startup speed — is not this
  application's problem. Compilation would also make the "which line raised
  this?" question in a production traceback materially harder to answer.
- **cx_Freeze** would work, but has weaker PySide6 and macOS `.app` support
  and no equivalent of PyInstaller's per-package hooks, which are exactly what
  handles Qt's plugin/DLL layout correctly here.
- **A zipapp or an embedded-Python layout** cannot satisfy the requirement:
  they still need a Python runtime the user has to obtain.

The build is **onedir**, not onefile: `Monitra.exe` sits beside an
`_internal/` folder. A onefile build unpacks its entire contents to a temp
directory on *every* launch, which is slower to start, and repeatedly writing
a large payload of executable content into a temp directory is one of the
most reliable ways to attract antivirus attention. The installer and the DMG
make the folder invisible to users anyway.

---

## 2. Prerequisites

| | Windows | macOS |
|---|---|---|
| Python | 3.12 (3.12.10 verified) | 3.12 |
| Build deps | `requirements.txt` + `requirements-build.txt` | same |
| Installer tool | Inno Setup 6 | — (`hdiutil`, built in) |
| Signing | `signtool.exe` + a certificate (optional) | Xcode CLI tools + Developer ID (optional) |

Install Inno Setup once:

```powershell
winget install --id JRSoftware.InnoSetup
```

**Both build scripts create and populate their own virtual environment**
(`.venv-build/`, gitignored) on first run. That is not tidiness. PyInstaller
bundles whatever is importable in the environment it runs in if any code path
references it, including inside a `try/except ImportError`. Building from a
developer's everyday environment — which accumulates packages from other
projects — produced a **160 MB** build of an application that imports none of
them; the same source from a clean environment produced **79 MB** with
identical functionality.

---

## 3. Windows build

From `desktop/`:

```powershell
.\scripts\build_windows.ps1          # incremental
.\scripts\build_windows.ps1 -Clean   # after changing the spec or requirements
```

Output: `dist\Monitra\Monitra.exe` (~79 MB for the whole folder).

The script creates the build venv if needed, installs pinned dependencies,
generates the icons, runs PyInstaller against `packaging/monitra.spec`, and
prints the resulting version metadata and size.

### Why the build is 79 MB and not 160 MB

`PySide6` ships ~30 Qt submodules (WebEngine, Qml/Quick, Multimedia, Charts,
Pdf, Bluetooth, 3D, SerialPort, …). This application imports exactly five:

```bash
# from desktop/ — re-run this whenever the app's Qt usage changes
grep -rhoE "from PySide6\.[A-Za-z0-9_.]+" --include="*.py" .
```
```
from PySide6.QtCore
from PySide6.QtGui
from PySide6.QtSvg
from PySide6.QtSvgWidgets
from PySide6.QtWidgets
```

`EXCLUDED_PYSIDE6_MODULES` in the spec excludes every other submodule. That
is the single biggest lever on size — bigger than onefile/onedir or UPX.

Excluding a *Python* module does not, however, stop PySide6's own PyInstaller
hook from bundling the underlying **Qt DLL**. A real build still shipped
`Qt6Quick.dll`, `Qt6Qml.dll`, `Qt6Pdf.dll` and `Qt6VirtualKeyboard.dll`
despite all four Python modules being excluded, plus `opengl32sw.dll` (Qt's
~20 MB software OpenGL rasterizer, dead weight on any desktop with a real GL
or ANGLE driver). `_DROP_BINARY_PREFIXES` strips those from `a.binaries`
after `Analysis()` runs, taking the build from 114 MB to 79 MB — verified by
launching the result, not assumed safe.

**This filter is applied on Windows only.** The equivalent saving on macOS
means deleting files out of Qt `.framework` bundles that `codesign` and
Gatekeeper validate as a unit, which breaks signing for an unmeasured win.

If a future change starts genuinely using an excluded Qt submodule (for
example real network calls through `QtNetwork` instead of `httpx`), **both**
lists in the spec need updating, or the build fails loudly at import time or
subtly at runtime.

---

## 4. Windows installer

```powershell
.\scripts\build_installer.ps1
```

Output: `dist\installer\Monitra-Setup-<version>.exe`.

What the installer does:

- Installs to `%LOCALAPPDATA%\Programs\Monitra` — **no administrator rights
  and no UAC prompt**. Monitra needs no elevation at any point: it reads the
  foreground window title, counts input events through an ordinary user-level
  hook, talks HTTPS outbound, and writes only to the user's own home
  directory. Asking for elevation on a monitoring tool is a trust cost with
  no benefit. (The user can still choose a machine-wide install from the
  privileges dialog if they want one.)
- Creates a Start Menu entry, an optional desktop shortcut, and an optional
  "start when I sign in" entry (unchecked by default).
- Registers a proper Add/Remove Programs entry with the icon, version and
  publisher.
- Refuses to run while Monitra is running, by checking the same named mutex
  the application takes (`core/single_instance.py`). Because Monitra hides to
  the tray on close, "I closed it" is frequently not true, and overwriting
  open files leaves a half-replaced installation.
- Upgrades in place. The `AppId` GUID must never change; it is what makes a
  newer version replace an older one rather than install beside it.

**Uninstalling does not delete your data.** The tracked time, sync queue and
logs in `%USERPROFILE%\.monitra` are deliberately left behind, because
uninstalling must never silently destroy time that has not yet reached the
server. Removing that folder is a manual step, documented for the user.

---

## 5. Windows portable build

```powershell
.\scripts\build_portable.ps1
```

Output: `dist\Monitra-Portable-<version>.zip`, containing `Monitra.exe`, its
`_internal\` folder, a `README.txt`, and a `monitra.portable` marker file.

The marker is the whole difference: while it is present, the application
keeps its database, sync queue and logs in a `data\` folder **beside the
executable** instead of in the user profile, so the application and its data
travel together.

If the portable folder is extracted somewhere unwritable, the application
falls back to `%USERPROFILE%\.monitra` rather than failing to start. It never
writes runtime data into `Program Files`.

---

## 6. macOS build

**This must be run on a Mac.** A `.app` contains Mach-O binaries and Qt
frameworks produced by the host toolchain, and `codesign`/`hdiutil` exist
only on macOS. Cross-building one from Windows is not possible, and a file
produced that way would not be a macOS application. Use a real Mac, or the
`macos` job in `.github/workflows/desktop-release.yml`.

```bash
cd desktop
./scripts/build_macos.sh            # or --clean
```

Output: `dist/Monitra.app` and `dist/Monitra-macOS-<arch>-<version>.dmg`
(drag-to-Applications layout).

### Architectures

The build is **native to the machine it runs on** — `arm64` on Apple Silicon,
`x86_64` on Intel — and the artifact is named accordingly. It is deliberately
not `universal2`: PySide6 publishes per-architecture wheels rather than
universal ones, so a universal bundle would mean merging two independently
built Qt trees, and claiming "universal" without testing both halves on real
hardware is exactly the sort of unverified claim this project does not make.

The release workflow therefore builds both, on `macos-14` (arm64) and
`macos-13` (x86_64), and ships two DMGs.

`LSMinimumSystemVersion` is 11.0 (`MACOS_MIN_VERSION` in `version.py`): the
first release with Apple Silicon, and the floor for PySide6's arm64 wheels.

---

## 7. macOS permissions

Monitra is a desktop monitoring application, so this section is load-bearing.
It requests **exactly two** permissions, both declared in the `Info.plist`
generated by `packaging/monitra.spec`:

| Permission | Key | Why | If denied |
|---|---|---|---|
| Screen Recording | `NSScreenCaptureUsageDescription` | Since Catalina, `CGWindowListCopyWindowInfo` returns window **titles** only with this granted. Titles are what app-usage attribution and browser URL tracking read. | App names still tracked; window titles are empty, so URL tracking reports nothing. No crash. |
| Input Monitoring | `NSInputMonitoringUsageDescription` | A listen-only Quartz event tap produces keyboard/mouse **counts**. | `CGEventTapCreate` returns `None`; counts stay at zero and activity reports as unmeasured. No crash. |

Monitra does **not** record or transmit your screen, and never stores which
keys were pressed — only counts. That privacy contract is enforced in
`background_services/activity/input_counter.py`.

### Why macOS does not use pynput

`pynput` is a Windows-only dependency here (`sys_platform != "darwin"` in
`requirements.txt`, and excluded from the macOS bundle in the spec). Its
macOS keyboard listener resolves every key event to a character through
Carbon's Text Services Manager:

```
pynput/_util/darwin.py: keycode_context()
    -> TISCopyCurrentKeyboardInputSource()
    -> TISGetInputSourceProperty(...)
```

On macOS 26 those HIToolbox APIs assert they are running on the main dispatch
queue. Called from pynput's own listener thread, the assertion fails inside
`dispatch_assert_queue`, raising **SIGTRAP** — an `EXC_BREAKPOINT` that is a
signal, not a Python exception, so no `try/except` can contain it. The whole
process dies. This was observed on macOS 26.5.2 (arm64) killing Monitra the
instant a timer was started.

macOS therefore counts input through a listen-only `CGEventTap`
(`background_services/activity/mac_input_tap.py`), which sees event types and
layout-independent modifier keycodes and never decodes a character. It needs
no new dependency — `pyobjc-framework-Quartz` was already required — and it
makes the privacy contract stronger: keystroke content is never obtained at
all, rather than obtained and discarded.

One consequence is documented rather than hidden: a watched key that is a
*printable* character cannot be tallied individually on macOS, because
identifying it requires the keyboard layout, which requires the crashing
call. Such keys still count toward the keystroke total, and
`InputEventCounter` logs which ones were unresolvable. The shipped
unwanted-activity rule watches `ctrl`, a modifier, which resolves fine.

No camera, microphone, contacts, location or Apple Events permission is
requested, because no code path uses them. `tests/test_packaging.py` asserts
both the presence of the two required keys and the absence of the others.

**Degradation is graceful by design.** macOS denies these permissions
silently — it does not raise — so the failure mode is missing data, not a
crash, and it is the same behaviour the source build already had. The user
grants them once in System Settings → Privacy & Security, then relaunches.

### Windows permissions

None. No administrator rights, no special capability, no elevated hook.
`GetForegroundWindow` and `SetWindowsHookEx` (via pynput) both work for a
standard user, and the installer requests no elevation.

---

## 8. Configuration

The packaged application must never depend on a `.env` inside a source
checkout, because there isn't one on a staff machine. `app/config.py`
resolves configuration in this order, first hit wins per key:

1. **The process environment** — `MONITRA_ENV`, `SMS_API_BASE_URL`. Always
   outranks a file, so an administrator can repoint a deployed build without
   editing anything.
2. **A config file**, the first that exists:
   - `.env` beside the executable (`desktop/.env` when running from source) —
     the per-build override a deployment drops in;
   - `<data dir>/monitra.env` — the per-user override, which works even when
     the installation directory is read-only.
3. **The environment preset's default.**

| `MONITRA_ENV` | Default backend |
|---|---|
| `production` (the default) | `https://monitra-lvzq.vercel.app` |
| `staging` | none — `SMS_API_BASE_URL` must be supplied |
| `development` | `http://localhost:8000` |

**Production is the default because an installed build is production.** An
unconfigured install on a staff laptop must reach the real backend; the
localhost default it once had meant a fresh install could never connect and
reported the server unreachable over perfectly good internet.

There is deliberately no baked-in staging hostname — inventing one means
shipping an address nobody verified. Selecting `staging` without supplying a
URL is reported as a configuration error.

A **packaged** build configured for localhost is an error, shown to the user
at startup rather than failing mysteriously later. A **source checkout**
pointed at localhost is inferred to be a development machine, so no developer
has to set two variables.

### Configuration is never a place for secrets

The desktop client authenticates as a user, with a token it obtains at login
and stores locally. It has no API key, no signing key and no database
credential, and none may ever be added: a server-side secret compiled into a
binary distributed to staff laptops cannot be revoked by deleting a file. No
`.env` is bundled into the package (`datas=[]` in the spec) — it is read from
beside the executable at runtime. `tests/test_packaging.py` asserts both.

---

## 9. Where the application stores data

| | Default | Portable build |
|---|---|---|
| Windows | `%USERPROFILE%\.monitra` | `data\` beside `Monitra.exe` |
| macOS | `~/.monitra` | — |

Contents: `cache.db` (SQLite — cached projects/tasks, tracked time, the
durable sync queue), `logs/monitra.log` (rotating, 4 MB × 4), and
`monitra.env` if a per-user override was created.

`core/paths.py` is the only module that answers "where may we write?", and
`storage/manager.py` and `core/logging_setup.py` both defer to it — so the
database and the logs can never disagree.

Runtime data is **never** written into the installation directory, `Program
Files`, or the `.app` bundle. Those are read-only for a standard user and are
replaced wholesale by the next installer run, so data there would be either
refused or destroyed on update. This is also what makes the application
update-safe: upgrading or reinstalling leaves `cache.db` and the sync queue
untouched.

`MONITRA_DATA_DIR` overrides the location — used by the smoke tests, and
available to administrators relocating data.

---

## 10. Versioning

`desktop/version.py` is the single source of truth. Cutting a release is one
edit: `VERSION`.

It flows into the Windows `.exe` VERSIONINFO resource, the installer, the
macOS `Info.plist` (`CFBundleShortVersionString`/`CFBundleVersion`), the DMG
volume name, the artifact filenames, the Qt application object, and the
window title. `tests/test_packaging.py` fails if any build file hardcodes the
version literal — an installer claiming one version while the binary inside
claims another makes every subsequent support report untrustworthy.

`VERSION` must stay plain `major.minor.patch`: Windows' VERSIONINFO and
macOS' `CFBundleVersion` both require numeric-only components. Pre-release
identity belongs in the artifact filename, not the constant.

### Internal test builds

A build for a pilot group sets `PRERELEASE` in `version.py` (for example
`"beta.1"`) alongside a numeric `VERSION`. The build scripts then name every
artifact by `version.artifact_version()` — `Monitra-Setup-1.2.0-beta.1.exe`,
`Monitra-Portable-1.2.0-beta.1.zip`, `Monitra-macOS-<arch>-1.2.0-beta.1.dmg` —
the window title and the update dialog show `1.2.0-beta.1`, and
`tools/check_changelog.py` requires a `## [1.2.0-beta.1]` entry. The `.exe`
resource, the `Info.plist`, the Inno Setup `AppVersion` and the
`Monitra/<version>` User-Agent all keep the numeric `VERSION`, because each of
them requires or parses that form.

`tools/register_release.py` refuses to register a pre-release, so it can never
become a `desktop_releases` row and can never be offered by the update check
or the download page; testers install it by hand. Set `PRERELEASE = ""` for
the production release, and do not reuse the `VERSION` an internal build
carried — a version identifies exactly one build.

---

## 11. Signing and notarization

**Current status: all builds are unsigned. Signing is required before public
distribution.** No certificate is committed, and none is fabricated.

### Windows

Unsigned installers trigger a SmartScreen warning on first run ("Windows
protected your PC" → *More info* → *Run anyway*). To sign, obtain a
code-signing certificate (an EV certificate gets SmartScreen reputation
immediately; an OV one accumulates it) and sign **both** the executable and
the installer:

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
    /f cert.pfx /p $env:CERT_PASSWORD dist\Monitra\Monitra.exe
# then rebuild the installer, and sign it too
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
    /f cert.pfx /p $env:CERT_PASSWORD dist\installer\Monitra-Setup-<version>.exe
```

Timestamping is not optional: without it, every signature stops validating the
day the certificate expires.

### macOS

An unsigned `.app` is refused by Gatekeeper on any Mac other than the one that
built it. Distribution requires a Developer ID Application certificate, the
hardened runtime, and notarization. `scripts/build_macos.sh` does all of it
when the environment supplies the credentials:

```bash
export MONITRA_CODESIGN_IDENTITY="Developer ID Application: Your Org (TEAMID)"
xcrun notarytool store-credentials monitra-notary \
    --apple-id you@example.com --team-id TEAMID --password <app-specific-password>
export MONITRA_NOTARY_PROFILE=monitra-notary
./scripts/build_macos.sh
```

`packaging/macos/entitlements.plist` carries the hardened-runtime
entitlements, and is deliberately minimal: library validation is disabled
(the one entitlement a PyInstaller bundle genuinely cannot launch without,
since it re-signs Qt and CPython under a non-Apple identity), `DYLD_*`
variables are allowed, and outbound networking is permitted. It does **not**
request `allow-jit` or `allow-unsigned-executable-memory` — the two routinely
copy-pasted into Python bundles without need, both of which materially weaken
the hardened runtime. The App Sandbox is not used either: it forbids the
global input monitoring and window-list access this application exists to do,
so a sandboxed build would silently track nothing. Direct distribution
(Developer ID + notarization) is the supported path for this class of app.

For CI, add these repository secrets — the workflow already reads them and
falls back to an unsigned build when they are absent:

`MACOS_CERTIFICATE` (base64 `.p12`), `MACOS_CERTIFICATE_PASSWORD`,
`MACOS_KEYCHAIN_PASSWORD`, `MACOS_CODESIGN_IDENTITY`, `MACOS_NOTARY_PROFILE`.

`MACOS_NOTARY_PROFILE` is **not** a CI secret. It is the name of a keychain
profile created by `xcrun notarytool store-credentials`, which is a local,
interactive step on a developer's Mac — the variable above, for local builds.
A fresh runner has no such profile, so a secret holding that name would point
`notarytool` at nothing. The workflow therefore creates the profile itself,
under a fixed local name, from three Apple credentials:

`MACOS_NOTARY_APPLE_ID` — an Apple ID enrolled in the Developer Program.
`MACOS_NOTARY_TEAM_ID` — the ten-character team identifier.
`MACOS_NOTARY_PASSWORD` — an **app-specific password** generated at
appleid.apple.com, *not* the account password.

Notarization is gated on `MACOS_CODESIGN_IDENTITY` as well, because signing is
a prerequisite rather than an alternative: Gatekeeper rejects a
notarized-but-unsigned bundle. With any of the four absent, the build produces
an unsigned, un-notarized artifact and says so.

When they are present, the job verifies the result on the artifact itself
rather than trusting the build script's own report — `codesign --verify
--deep --strict`, `stapler validate`, and `spctl --assess` on both the `.app`
and each `.dmg`, which is the question a user's Mac actually asks. The build
fails if Gatekeeper would refuse it.

**This path has never executed.** No Apple credentials have been configured
for this repository, so every macOS build to date is unsigned and
un-notarized. The code is written and gated; it is not evidence. Treat macOS
signing and notarization as unverified until a run with the credentials
present has gone green and the verification step's output has been read.

---

## 12. Release process

1. Edit `VERSION` in `desktop/version.py`. Commit.
2. Verify locally — see [§13](#13-verifying-a-build).
3. Tag and push: `git tag v<version> && git push origin v<version>`.
4. `.github/workflows/desktop-release.yml` builds Windows and both macOS
   architectures, re-running the test suite and the architecture check **on
   each platform** before packaging, then smoke-tests each package.
5. The workflow opens a **draft** release with the artifacts attached. A human
   checks them and publishes.

Generated packages are never committed to the repository; they are CI/release
artifacts. `dist/` and `build/` are gitignored.

The workflow can also be run manually (`workflow_dispatch`) to produce
artifacts without a tag.

---

## 13. Verifying a build

Per `CLAUDE.md`, from `desktop/`:

```bash
python -m pytest tests/ -q                            # must pass
python tools/check_architecture.py                    # "Architecture boundaries OK"
python tests/soak/run_launch_cycles.py --cycles 10    # 10/10 clean
python tests/soak/run_soak.py --duration 60           # PASS
```

On the macOS release runners the suite is run one module per interpreter
instead -- `python tools/run_tests_per_module.py` -- because the
single-process run there dies inside Qt (a bus error on Apple Silicon, a
segmentation fault on Intel) while a test shows an ordinary window under the
offscreen platform, at a point that moved from build to build. Each module
gets its own QApplication, every module reports its result, and a crash is
attributed to the file it happened in rather than ending the run. Nothing is
skipped or retried; the same tests run, and every failure still fails the
build. The runner works on any platform if you want the same attribution
locally.

Then verify the *package*, which is the thing users get:

```powershell
.\scripts\smoke_test_package.ps1     # Windows
```
```bash
./scripts/smoke_test_package.sh      # macOS
```

The smoke test runs the packaged binary for real (via
`MONITRA_SELFTEST_SECONDS`, which starts the app normally and then quits it
through the ordinary shutdown path), then asserts it exited 0, logged its
startup line, logged no import/DLL error and no `terminate()` escalation,
created `cache.db` in the data directory, and wrote nothing into the
installation directory. On macOS it also checks the `Info.plist` keys.

This catches the entire class of failure packaging *introduces* and that no
test run from source can see: a stripped Qt DLL that turned out to be needed,
a lazily-imported module PyInstaller did not follow, a resource path that
resolved into the source tree.

It is a smoke test and no more. It does not prove login, tracking, or sync
work against a real backend — that is the clean-machine checklist below.

---

## 14. Clean-machine testing

A build is not releasable because it works on the machine that built it. The
developer machine has Python, the source tree, the venv and a warm cache;
none of that exists on a staff laptop, and every one of them can mask a
packaging bug.

Test on a machine (or a fresh VM) with **no Python, no source, no
virtualenv**, then walk the whole product:

- [ ] Installer runs without administrator rights; Start Menu entry appears
- [ ] Launch — no missing DLL/library error, no Python error, no console window
- [ ] Correct icon in the taskbar, Start Menu and Add/Remove Programs
- [ ] Correct version in Properties → Details (Windows) / About (macOS)
- [ ] Login against the configured backend; projects and tasks load
- [ ] Start the timer; use a browser, an editor, and one other application
- [ ] App usage: correct application names, sane durations, no overlaps
- [ ] URL usage: real URLs from supported browsers — never a fabricated
      placeholder; an unsupported browser must show an explicit unavailable
      state
- [ ] Keyboard/mouse activity counts move
- [ ] Notifications appear with the right title, icon and text; repeats do not
      storm
- [ ] Stop the timer; confirm the record locally **and in the backend
      database** — "the UI displayed it" is not verification
- [ ] Disconnect the network: the app stays usable, tracking continues, the
      queue grows, no request storm, no permanent loader
- [ ] Reconnect: the queue drains, records arrive, **no duplicates**
- [ ] Quit and relaunch: data is still there, the session behaves per product
      design
- [ ] Force-kill and relaunch: recovery runs, no duplicate records or workers
- [ ] Sleep/wake: no impossible durations, tracking and sync recover
- [ ] Log out and back in: session-scoped workers stop and restart, exactly
      one of each service
- [ ] Uninstall: binaries and shortcuts removed; `~/.monitra` intentionally
      left in place
- [ ] Reinstall over the top: existing local data still present

On macOS, additionally: grant Screen Recording and Input Monitoring, relaunch,
and confirm window titles and input counts start flowing; confirm the app
behaves correctly (missing data, no crash) *before* the grant.

---

## 15. Reproducibility

| | Pinned in |
|---|---|
| Python 3.12 | the CI workflow (`PYTHON_VERSION`); 3.12.10 verified locally |
| PyInstaller 6.22.2, pytest 9.1.1 | `requirements-build.txt` |
| Application dependencies | `requirements.txt` |
| Build environment | `.venv-build/`, created from those two files only |

Runtime and build dependencies are deliberately separate files. Nothing in
`requirements-build.txt` is imported by the application or ends up inside the
package; keeping them apart is what lets the build environment be both
complete and clean.

The same commit, with the same pins, on the same OS and Python, produces an
equivalent package. Byte-for-byte reproducibility is not claimed: PyInstaller
embeds timestamps and paths.

---

## 16. Troubleshooting

**The build is much larger than ~79 MB.** You built from an environment with
extra packages. Delete `.venv-build/` and re-run the build script.

**The build succeeds but the executable fails to launch, or reports a missing
DLL.** Almost always `_DROP_BINARY_PREFIXES` in `packaging/monitra.spec`
dropped something an app change now needs. Remove the newest entry, rebuild,
retest; narrow it down entry by entry rather than reverting the list.

**The packaged app starts but tracks nothing.** A lazily-imported dependency
was not bundled. Check `HIDDEN_IMPORTS` in the spec — `pynput` and the pyobjc
frameworks are imported inside functions behind platform checks, so
PyInstaller's static analysis does not always follow them. The smoke test's
log assertions are designed to catch this.

**"Monitra is already running" when it is not visible.** It is: closing the
window hides it to the tray. Use the tray icon to quit it. Only one instance
may run per user, deliberately — two would mean two timers, two sync services
draining one queue, and two writers on one SQLite database.

**Antivirus flags the executable / SmartScreen blocks it.** Expected for an
unsigned PyInstaller build. See [§11](#11-signing-and-notarization). The
onedir layout already avoids the worst trigger (repeatedly unpacking
executable content into a temp directory on every launch).

**The icon is missing and the build fails immediately.** `build/` is
gitignored, so icons are generated, not committed. The build scripts run
`tools/generate_app_icon.py` for you; run it manually if invoking PyInstaller
directly.

**macOS: "Monitra Not Opened — Apple could not verify Monitra is free of
malware", with only "Move to Trash" and "Done".** This is Gatekeeper refusing
an unsigned, un-notarized build; it is not a crash and not a build fault.

Note that the old advice — right-click → Open — **no longer works on macOS 15
and later**. That escape hatch was removed. The current sequence is:

1. Try to open the app, and click **Done** on the warning.
2. **System Settings → Privacy & Security**, scroll to **Security**. A line
   reads *"Monitra was blocked to protect your Mac."* Click **Open Anyway**.
3. Authenticate, then open Monitra again and confirm.

Or, equivalently, strip the quarantine attribute:

```bash
xattr -dr com.apple.quarantine /Applications/Monitra.app
```

Both are workarounds for the same missing thing. The real fix is signing and
notarizing — see [§11](#11-signing-and-notarization). Do not ask staff to do
either of the above as a permanent rollout procedure: training people to
bypass Gatekeeper for a monitoring tool is a bad habit to teach.

**A Python upgrade breaks the build.** PyInstaller pins bootloaders per Python
version. If `pip install pyinstaller` in the new venv has no bootloader for
the new interpreter, wait for a release that does, or stay on the previous
Python.

---

## 17. Known limitations

- **All artifacts are unsigned.** Windows SmartScreen will warn; macOS
  Gatekeeper will refuse the bundle on any Mac that did not build it. See
  [§11](#11-signing-and-notarization).
- **macOS: built and launched, not yet functionally verified.** A `.app` and
  `.dmg` have been produced and installed on macOS 26.5.2 (arm64), and the
  application starts and reaches its UI. The first attempt crashed on timer
  start (the pynput/TSM SIGTRAP described in [§7](#7-macos-permissions)),
  which is fixed and covered by `tests/test_mac_input_tap.py` — but that fix
  has **not yet been confirmed on a Mac**, and login, tracking, sync and
  shutdown have not been walked through on macOS. Treat macOS as unverified
  until [§14](#14-clean-machine-testing) has been completed on one.
- **A printable watched key cannot be tallied individually on macOS** — see
  [§7](#7-macos-permissions). It still counts toward the keystroke total.
- **Not universal2.** Two per-architecture DMGs, by choice — see
  [§6](#6-macos-build).
- **Windows on ARM is not supported.** The build is x64 only, matching the
  PySide6 wheels; the installer refuses to install elsewhere rather than
  failing at first launch.
- **No auto-update mechanism.** The client is *told* about a newer release —
  a notification, and an "Updates (n)" entry in the account menu, both of
  which open the download page — but it never downloads or installs anything.
  The user still fetches the installer and runs it themselves. A real
  self-updater is approved for later, as a *prompted* update, and is gated on
  code signing: an updater that silently runs an unsigned installer is a worse
  security posture than the manual download it would replace. The packaging is
  compatible with adding one: user data lives outside the installation
  directory, and the installer upgrades in place.

  What the client is told comes from the backend's `GET /desktop/latest-version`,
  which reports whatever `DESKTOP_LATEST_VERSION` and `DESKTOP_DOWNLOAD_URL`
  are set to on the deployment — never a git tag, and never GitHub directly.
  Clearing `DESKTOP_LATEST_VERSION` withdraws a release from every client at
  once. See `docs/Desktop_Release_Runbook.md`.
- **Screenshot capture is not implemented client-side** (see `CLAUDE.md` §5).
  Packaging does not change that; the tab shows an honest empty state.
