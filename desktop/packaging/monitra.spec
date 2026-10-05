# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build spec for the Monitra desktop client — Windows and macOS.

Usage (from desktop/, after `pip install -r requirements.txt pyinstaller` and
`python tools/generate_app_icon.py` — see BUILD.md for the full procedure):

    pyinstaller packaging/monitra.spec --distpath dist --workpath build/work --noconfirm

Output:
    Windows   dist/Monitra/Monitra.exe  (plus supporting files alongside it)
    macOS     dist/Monitra.app          (plus the same onedir tree inside it)

One spec covers both platforms deliberately. The two builds differ only in
the icon format, the version metadata container, and the macOS-only .app
bundle — duplicating a second spec to express that would mean every future
change to the excludes list had to be made twice, and the two would drift.

What "lightweight" means here: this app only ever imports PySide6.QtCore,
QtGui, QtWidgets, QtSvg, and QtSvgWidgets (confirm with
`grep -rhoE "from PySide6\.[A-Za-z0-9_.]+" --include="*.py" .` from
desktop/) — no WebEngine, Qml/Quick, Multimedia, Charts, Sql, Pdf,
Bluetooth, 3D, or any of PySide6's other ~25 submodules, each of which ships
by default and adds tens of MB. EXCLUDED_PYSIDE6_MODULES below excludes
exactly those; that list is the single biggest lever on the final size,
bigger than onefile/onedir or UPX.
"""
import os
import sys
from pathlib import Path

DESKTOP_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 -- injected by PyInstaller

# version.py is the single source of truth for the name, version and bundle
# identifier — see its docstring. Read as source rather than imported, so
# this spec never depends on the app's own import graph (importing main's
# package tree from inside a spec pulls Qt into the analysis process).
_version_ns = {}
exec((DESKTOP_ROOT / "version.py").read_text(encoding="utf-8"), _version_ns)  # noqa: S102

APP_NAME = _version_ns["APP_NAME"]
VERSION = _version_ns["VERSION"]
BUNDLE_ID = _version_ns["BUNDLE_ID"]
COPYRIGHT = _version_ns["COPYRIGHT"]
MACOS_MIN_VERSION = _version_ns["MACOS_MIN_VERSION"]
VERSION_TUPLE = _version_ns["version_tuple"]()

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

BUILD_DIR = DESKTOP_ROOT / "build"
ICON_PATH = BUILD_DIR / ("monitra.ico" if IS_WINDOWS else "monitra.icns")
if not ICON_PATH.exists():
    raise SystemExit(
        f"{ICON_PATH} is missing. Run `python tools/generate_app_icon.py` first "
        f"(see BUILD.md) — the icon is a generated build artifact, not a "
        f"committed file."
    )

# Every PySide6 submodule this codebase does NOT import anywhere. Update this
# list (and the grep above) if a future change adds a genuinely new Qt
# submodule import -- an unnecessary exclude for a module nothing imports is
# harmless, but excluding one that IS used breaks the build loudly at import
# time, not silently.
EXCLUDED_PYSIDE6_MODULES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtQuick3D", "PySide6.QtQuick3DRuntimeRender",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtMultimediaQuick",
    "PySide6.QtNetwork", "PySide6.QtNetworkAuth",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtHelp",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech", "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtXml", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPrintSupport",
    "PySide6.QtSpatialAudio", "PySide6.QtLocation", "PySide6.QtHttpServer",
    "PySide6.QtVirtualKeyboard", "PySide6.QtAxContainer",
]

# `pynput` and the pyobjc frameworks are imported lazily, inside functions and
# behind platform checks (background_services/activity/input_counter.py,
# tracking/active_window.py), so PyInstaller's static analysis does not always
# follow them. They are genuinely required at runtime for keyboard/mouse
# counting and for foreground-window detection; without them a packaged build
# silently reports zero activity, which is exactly the "looks fine, tracks
# nothing" failure this app must never ship.
HIDDEN_IMPORTS = ["tzdata"]
if IS_MACOS:
    # No pynput on macOS -- see requirements.txt. Its keyboard listener
    # SIGTRAPs the process on macOS 26, so it is not merely unused here, it is
    # deliberately not installed and must never be bundled.
    HIDDEN_IMPORTS += ["AppKit", "Foundation", "Quartz"]
elif IS_WINDOWS:
    HIDDEN_IMPORTS += [
        "pynput", "pynput.keyboard", "pynput.mouse",
        "pynput.keyboard._win32", "pynput.mouse._win32",
    ]

a = Analysis(  # noqa: F821 -- injected by PyInstaller's exec environment
    [str(DESKTOP_ROOT / "main.py")],
    pathex=[str(DESKTOP_ROOT)],
    binaries=[],
    # Icons are drawn in code (vendored SVG path data / QPainter), so the
    # only bundled data is desktop/assets/ -- the folder core/branding.py
    # reads an optional real logo file from. It is included when it holds
    # something beyond its README, so a build ships the same mark the
    # development run shows. The one genuinely external file, .env, is
    # deliberately NOT bundled -- it is read next to the executable at
    # runtime (see app/config.py) so a deployed build can be pointed at a
    # different backend without a rebuild, and so no environment file can
    # ever be baked into a shipped binary.
    datas=(
        [(str(DESKTOP_ROOT / "assets"), "assets")]
        if (DESKTOP_ROOT / "assets").is_dir()
        else []
    ),
    hiddenimports=HIDDEN_IMPORTS,
    # pynput is excluded outright on macOS, not merely left uninstalled: if a
    # stale build environment still has it, bundling it would put the
    # SIGTRAP-ing keyboard listener back inside the .app.
    excludes=EXCLUDED_PYSIDE6_MODULES + (["pynput"] if IS_MACOS else []),
    noarchive=False,
)

# `excludes` above only stops PyInstaller from following *Python* imports into
# those submodules. PySide6's own PyInstaller hook still bundles the
# underlying Qt library files it thinks the app might need, independent of
# which Python wrapper modules got imported -- so Qt6Quick.dll, Qt6Qml.dll,
# Qt6Pdf.dll and Qt6VirtualKeyboard.dll all showed up in a real build here
# despite QtQml/QtQuick/QtPdf/QtVirtualKeyboard being excluded above.
# opengl32sw.dll (Qt's software OpenGL rasterizer, ~20MB) is also pulled in by
# default; every real Windows desktop already has a hardware OpenGL/ANGLE
# driver, so the software fallback is dead weight for this app. Stripping
# these took a verified build from 114MB to 77MB.
#
# This filter is applied on Windows only, and deliberately so: the same
# saving on macOS would mean deleting files out of the Qt .framework bundles
# that codesign and Gatekeeper validate as a unit, which breaks signing for a
# size win nobody has measured here. Correct first, small second.
_DROP_BINARY_PREFIXES = (
    "opengl32sw",
    "Qt6Quick", "Qt6Qml",
    "Qt6Pdf",
    "Qt6VirtualKeyboard",
)
if IS_WINDOWS:
    a.binaries = [
        entry for entry in a.binaries
        if not Path(entry[0]).name.startswith(_DROP_BINARY_PREFIXES)
    ]

pyz = PYZ(a.pure)  # noqa: F821


def _windows_version_resource() -> str:
    """
    Write the VERSIONINFO resource stamped into Monitra.exe, and return its path.

    Without this, Explorer's Properties -> Details tab shows a blank publisher
    and no version, which is both unprofessional and a real support problem:
    "which build are you running?" has no answer. Generated from version.py so
    it cannot disagree with the installer or the About line.
    """
    resource = f"""
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={VERSION_TUPLE},
    prodvers={VERSION_TUPLE},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', '{APP_NAME}'),
        StringStruct('FileDescription', '{_version_ns["APP_DISPLAY_NAME"]}'),
        StringStruct('FileVersion', '{VERSION}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('LegalCopyright', '{COPYRIGHT}'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '{APP_NAME}'),
        StringStruct('ProductVersion', '{VERSION}'),
      ]),
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
"""
    path = BUILD_DIR / "file_version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(resource, encoding="utf-8")
    return str(path)


exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    icon=str(ICON_PATH),
    console=False,   # windowed GUI app -- no terminal window in production
    upx=True,        # no-op if the upx binary isn't installed; see BUILD.md
    strip=False,
    version=_windows_version_resource() if IS_WINDOWS else None,
    # Signing identity is supplied by the environment, never committed. An
    # unset value means an unsigned build, which is the documented default --
    # see BUILD.md "Signing".
    codesign_identity=os.environ.get("MONITRA_CODESIGN_IDENTITY") if IS_MACOS else None,
    entitlements_file=(
        str(DESKTOP_ROOT / "packaging" / "macos" / "entitlements.plist")
        if IS_MACOS else None
    ),
)

# onedir layout: Monitra.exe + its supporting files alongside it in the same
# folder. Faster to launch than a single self-extracting .exe (nothing to
# unpack into a temp dir on every run) and no larger on disk. On macOS this
# tree is what goes inside Monitra.app/Contents.
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    upx=True,
    name=APP_NAME,
)

if IS_MACOS:
    # The Info.plist below is the load-bearing part of the macOS build. The
    # NS*UsageDescription strings are not documentation: macOS refuses the
    # corresponding permission outright (and, for some, terminates the
    # process) if the key is absent, so a bundle without them would appear to
    # "just not track anything" with no error the user could act on.
    #
    # Monitra requests exactly three, and no more:
    #
    #   Screen Recording (NSScreenCaptureUsageDescription)
    #       Two things need it. Screenshots (background_services/screenshot/):
    #       without the permission macOS answers a capture with an image of
    #       only the wallpaper and Monitra's own windows, which
    #       screenshot/screen_access.py detects and refuses to record.
    #       And CGWindowListCopyWindowInfo returns window *titles* only with
    #       it granted (macOS 10.15+); titles are what browser URL tracking
    #       reads. Without it, app names still work and titles are empty --
    #       tracking/active_window.py documents this exact degradation.
    #       The grant is keyed to the bundle identifier and code signature, so
    #       a rebuild signed differently (or ad hoc) is a new app to macOS and
    #       asks again -- see BUILD.md "Signing".
    #
    #   Input Monitoring / Accessibility (NSInputMonitoringUsageDescription,
    #   NSAppleEventsUsageDescription is deliberately NOT requested)
    #       pynput's global listeners produce the keyboard/mouse *counts*
    #       (never keystroke content -- see input_counter.py's privacy
    #       contract). Denied, counts stay at zero and activity reports as
    #       unmeasured rather than crashing.
    #
    # No camera, microphone, contacts, location or Apple Events permission is
    # requested, because nothing in this application uses them.
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{APP_NAME}.app",
        icon=str(ICON_PATH),
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "CFBundleIdentifier": BUNDLE_ID,
            "NSHumanReadableCopyright": COPYRIGHT,
            "LSMinimumSystemVersion": MACOS_MIN_VERSION,
            "NSHighResolutionCapable": True,
            # Monitra keeps running in the menu bar/tray after its window is
            # closed, but it is a normal windowed app, not an agent -- it must
            # keep its Dock icon and its menu bar. LSUIElement stays false.
            "LSUIElement": False,
            "NSScreenCaptureUsageDescription": (
                "While a timer is running, Monitra takes periodic screenshots "
                "of your screen and uploads them to your organisation's Monitra "
                "account as proof of work, and reads the title of the window "
                "you are working in so your tracked time is attributed to the "
                "right application and browser page. Nothing is captured when "
                "no timer is running."
            ),
            "NSInputMonitoringUsageDescription": (
                "Monitra counts keyboard and mouse events to measure how active "
                "a tracked session was. Only the counts are kept — which keys "
                "you press is never stored, logged, or sent anywhere."
            ),
        },
    )
