"""
version — the single source of truth for Monitra desktop identity.

Every place that needs to name or version this application reads from here:

    - the Qt application object (main.py)
    - the Windows .exe version resource (packaging/monitra.spec)
    - the Windows installer (packaging/windows/monitra.iss, generated from here)
    - the macOS Info.plist / bundle identifier (packaging/monitra.spec)
    - the DMG volume name (scripts/build_macos.sh)
    - the "About" line in the UI

Nothing else may hardcode a version string. A build whose installer claims
1.2.0 while the .exe metadata says 1.0.0 is worse than having no version at
all, because it makes support reports untrustworthy — so this module exists
to make the duplicate impossible.

Bumping a release is therefore exactly one edit: VERSION below.

VERSION must stay a plain three-part `major.minor.patch` string. Windows'
VERSIONINFO resource and macOS' CFBundleVersion both require numeric-only
components, so a suffix like "1.0.0-rc1" would have to be stripped in two
places and would drift. Pre-release identity belongs in the artifact
filename, which the build scripts derive, not in this constant.

Internal test builds
--------------------
A build handed to a small group of testers before a version ships is marked
by PRERELEASE (for example "beta.1"). It changes exactly three things, and
nothing the operating system or the backend parses:

    - the artifact filenames (`Monitra-Setup-1.2.0-beta.1.exe`), so a tester's
      download can never be mistaken for the production installer;
    - the version shown in the window title and the update dialog
      (`1.2.0-beta.1`), so a support report says which build it came from;
    - `tools/register_release.py` refuses to register the build with the
      backend, so a pre-release can never become the release the updater or
      the public download page offers to everyone.

The numeric VERSION is what the .exe resource, the Info.plist, the Inno Setup
AppVersion and the `Monitra/<version>` User-Agent carry, because every one of
them requires or parses the numeric form. Two internal builds with the same
VERSION are therefore indistinguishable to the fleet view; that is accepted for
a pilot ring and is exactly why a pre-release is never registered as a release.
PRERELEASE must be empty for a production release, and the VERSION an internal
build used must not be reused for the production release that follows it — a
version identifies exactly one build.
"""
from __future__ import annotations

#: Release version. The only line to edit when cutting a release.
VERSION = "1.2.1"

#: Pre-release label for an internal test build, or "" for a production
#: release. See "Internal test builds" above. Letters, digits and dots only.
PRERELEASE = ""

#: Product name as shown to users, and as used for the executable,
#: the installed folder, the .app bundle, and the Start Menu entry.
APP_NAME = "Monitra"

#: Longer display name for window titles and installer headings.
APP_DISPLAY_NAME = "Monitra — Staff Management"

#: Publisher / Qt organisation name. QSettings already persists under
#: ("Monitra", "SMSDesktop"); changing ORG_NAME would orphan existing user
#: preferences, so treat it as fixed.
ORG_NAME = "Monitra"

#: Reverse-DNS bundle identifier for the macOS .app. macOS uses this as the
#: identity for TCC permission grants (Screen Recording, Input Monitoring),
#: so changing it makes every user re-grant permissions. Treat as fixed.
BUNDLE_ID = "com.monitra.desktop"

COPYRIGHT = "Copyright (c) Monitra"

#: Oldest macOS this build supports. 11.0 is the first release with Apple
#: Silicon, and is the floor for the arm64 wheels PySide6 publishes.
MACOS_MIN_VERSION = "11.0"


def version_tuple() -> tuple[int, int, int, int]:
    """
    Return VERSION as the 4-part tuple Windows' VERSIONINFO resource needs.

    Windows requires four 16-bit fields; this project versions in three, so
    the build field is always 0.
    """
    major, minor, patch = (int(part) for part in VERSION.split("."))
    return (major, minor, patch, 0)


def is_prerelease() -> bool:
    """True for an internal test build, False for a production release."""
    return bool(PRERELEASE)


def display_version() -> str:
    """The version as shown to a person: `1.2.0`, or `1.2.0-beta.1`.

    For the window title, the update dialog and the changelog heading. Never
    for anything that parses the version — the User-Agent, the .exe resource
    and the Info.plist all take the numeric VERSION.
    """
    return f"{VERSION}-{PRERELEASE}" if PRERELEASE else VERSION


def artifact_version() -> str:
    """The version component of every artifact filename.

    The same string as `display_version()`, named separately because it is a
    contract with the build scripts and the release pipeline's filename
    patterns: `Monitra-Setup-<artifact_version>.exe`,
    `Monitra-Portable-<artifact_version>.zip`,
    `Monitra-macOS-<arch>-<artifact_version>.dmg`.
    """
    return display_version()


def user_agent() -> str:
    """Return the User-Agent the API client identifies itself with.

    Always the numeric VERSION: the backend parses `Monitra/major.minor.patch`
    for the update check and fleet visibility, and would not recognise a
    suffixed one at all.
    """
    return f"{APP_NAME}/{VERSION}"
