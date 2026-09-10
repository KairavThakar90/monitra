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
"""
from __future__ import annotations

#: Release version. The only line to edit when cutting a release.
VERSION = "1.1.1"

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


def user_agent() -> str:
    """Return the User-Agent the API client identifies itself with."""
    return f"{APP_NAME}/{VERSION}"
