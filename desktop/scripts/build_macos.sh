#!/usr/bin/env bash
#
# Build Monitra.app and Monitra-macOS-<arch>-<version>.dmg.
#
# Run on macOS only. This script cannot be run on Windows or Linux and does
# not pretend to be: a macOS application bundle contains Mach-O binaries and
# Qt frameworks for the host architecture, produced by the host's own
# toolchain, and codesign/hdiutil exist only on macOS. Cross-building one from
# Windows is not possible, and a file produced that way would not be a macOS
# application. Use the macOS runner in .github/workflows/desktop-release.yml,
# or a real Mac.
#
# Architecture: the build is native to the machine it runs on -- arm64 on
# Apple Silicon, x86_64 on Intel -- and the artifact is named accordingly. It
# is deliberately NOT built as a universal2 binary: PySide6 publishes separate
# per-architecture wheels rather than universal ones, so a universal bundle
# would require merging two independently built Qt trees, and claiming
# "universal" without having tested both halves on real hardware is exactly
# the sort of unverified claim this project does not make. Ship one DMG per
# architecture instead.
#
# Usage (from desktop/):
#     ./scripts/build_macos.sh
#     ./scripts/build_macos.sh --clean
#
# Signing and notarization are opt-in, driven entirely by the environment --
# no credential is ever read from a file in the repository. See BUILD.md.
#
#     MONITRA_CODESIGN_IDENTITY   e.g. "Developer ID Application: Acme (TEAMID)"
#     MONITRA_NOTARY_PROFILE      a `xcrun notarytool store-credentials` profile
#
# With neither set, the result is an unsigned .app and .dmg, which is correct
# for internal testing and NOT suitable for public distribution: Gatekeeper
# will refuse to open it on any Mac that did not build it.

set -euo pipefail

DESKTOP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DESKTOP_ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: this script builds a macOS application and must run on macOS." >&2
    echo "       On other platforms, use the macos job in" >&2
    echo "       .github/workflows/desktop-release.yml." >&2
    exit 1
fi

CLEAN=0
[[ "${1:-}" == "--clean" ]] && CLEAN=1

VENV="$DESKTOP_ROOT/.venv-build"
PY="$VENV/bin/python"
ARCH="$(uname -m)"

# ── 1. Build environment ─────────────────────────────────────────────────────
# A dedicated venv, for the same reason as the Windows build: PyInstaller
# bundles what is importable in the environment it runs in.
if [[ ! -x "$PY" ]]; then
    echo "==> Creating clean build venv ($VENV)"
    python3 -m venv "$VENV"
fi

echo "==> Installing runtime + build dependencies"
"$PY" -m pip install --upgrade pip --quiet
"$PY" -m pip install --quiet -r requirements.txt -r requirements-build.txt

VERSION="$("$PY" -c 'import version; print(version.VERSION)')"
# The artifact name carries the pre-release label of an internal test build
# (<version>-<prerelease>). The bundle's own CFBundleVersion stays numeric, as
# macOS requires; identical to $VERSION for a production release.
ARTIFACT_VERSION="$("$PY" -c 'import version; print(version.artifact_version())')"
echo "==> Building Monitra $ARTIFACT_VERSION for $ARCH"

# ── 2. Clean ─────────────────────────────────────────────────────────────────
if [[ $CLEAN -eq 1 ]]; then
    echo "==> Cleaning previous build output"
    rm -rf dist build/work build/dmg
fi

# ── 3. Icons ─────────────────────────────────────────────────────────────────
echo "==> Generating application icons"
QT_QPA_PLATFORM=offscreen "$PY" tools/generate_app_icon.py

# ── 4. Package ───────────────────────────────────────────────────────────────
echo "==> Running PyInstaller"
"$PY" -m PyInstaller packaging/monitra.spec \
    --distpath dist --workpath build/work --noconfirm

APP="$DESKTOP_ROOT/dist/Monitra.app"
[[ -d "$APP" ]] || { echo "error: build succeeded but $APP is missing" >&2; exit 1; }

# ── 5. Sign ──────────────────────────────────────────────────────────────────
# PyInstaller signs the individual binaries during the build when
# MONITRA_CODESIGN_IDENTITY is set (see packaging/monitra.spec). The bundle as
# a whole still needs one deep signature afterwards, or notarization rejects
# it with "the signature of the binary is invalid".
if [[ -n "${MONITRA_CODESIGN_IDENTITY:-}" ]]; then
    echo "==> Signing $APP"
    codesign --force --deep --options runtime --timestamp \
        --entitlements packaging/macos/entitlements.plist \
        --sign "$MONITRA_CODESIGN_IDENTITY" "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
else
    echo "==> No MONITRA_CODESIGN_IDENTITY set: building UNSIGNED."
    echo "    Gatekeeper will refuse this bundle on any other Mac."
fi

# ── 6. DMG ───────────────────────────────────────────────────────────────────
# hdiutil, not a third-party DMG tool: it ships with macOS, needs no
# dependency, and produces the standard drag-to-Applications layout when given
# a staging folder containing the app and a symlink.
DMG="$DESKTOP_ROOT/dist/Monitra-macOS-$ARCH-$ARTIFACT_VERSION.dmg"
STAGING="$DESKTOP_ROOT/build/dmg"

echo "==> Building $(basename "$DMG")"
rm -rf "$STAGING" "$DMG"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

hdiutil create -volname "Monitra $ARTIFACT_VERSION" \
    -srcfolder "$STAGING" -ov -format UDZO "$DMG"

if [[ -n "${MONITRA_CODESIGN_IDENTITY:-}" ]]; then
    codesign --force --sign "$MONITRA_CODESIGN_IDENTITY" "$DMG"
fi

# ── 7. Notarize ──────────────────────────────────────────────────────────────
# Notarization is what lets a downloaded DMG open on a Mac that has never seen
# it. Stapling attaches the ticket so it works offline too.
if [[ -n "${MONITRA_NOTARY_PROFILE:-}" ]]; then
    echo "==> Submitting for notarization (this can take several minutes)"
    xcrun notarytool submit "$DMG" --keychain-profile "$MONITRA_NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
    xcrun stapler validate "$DMG"
    echo "==> Notarized and stapled"
else
    echo "==> No MONITRA_NOTARY_PROFILE set: NOT notarized."
    echo "    Users would have to right-click -> Open and approve the warning."
fi

SIZE="$(du -h "$DMG" | cut -f1)"
echo
echo "==> Build complete"
echo "    app : $APP"
echo "    dmg : $DMG ($SIZE)"
echo "    arch: $ARCH"
