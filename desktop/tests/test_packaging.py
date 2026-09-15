"""
Coverage for the packaging layer: where a packaged build writes its data,
which backend it decides to talk to, and whether the version it reports can
drift between the places that state it.

Every case here corresponds to a failure that is invisible from a source
checkout and only appears once the application is installed somewhere else:

  - data written next to the executable, into a directory the installer
    replaces on upgrade (losing unsynced time) or that Program Files makes
    read-only (failing to start at all);
  - a shipped build still pointed at http://localhost:8000, which cannot
    work on a staff machine and reports the server as unreachable;
  - an installer claiming one version while the binary inside claims another,
    which makes every subsequent support report untrustworthy.

These are cheap to test and expensive to discover on a user's machine.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as config_module  # noqa: E402
import core.paths as paths  # noqa: E402
import version as version_module  # noqa: E402

DESKTOP_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_path_cache():
    """The resolved data directory is process-cached; reset it per test."""
    paths.reset_cache()
    yield
    paths.reset_cache()


def _reload_config(monkeypatch, **env):
    """
    Re-import app.config with a controlled environment.

    The developer's own `desktop/.env` is neutralised: these tests assert what
    a *shipped* build resolves to, and that must not depend on whether the
    machine running them happens to have a local backend configured.
    """
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for key in ("MONITRA_ENV", "SMS_API_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(config_module)


# ── Runtime data locations ───────────────────────────────────────────────────

def test_data_dir_defaults_to_the_user_home(monkeypatch):
    monkeypatch.delenv("MONITRA_DATA_DIR", raising=False)
    assert paths.data_dir() == Path.home() / ".monitra"


def test_data_dir_honours_an_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path / "relocated"))
    assert paths.data_dir() == tmp_path / "relocated"
    assert paths.data_dir().is_dir()


def test_data_dir_is_resolved_once_per_process(monkeypatch, tmp_path):
    """
    A mid-run change must not move the data directory: the SQLite connections
    and the sync queue are already open against the first answer, and a second
    answer would silently split the database in two.
    """
    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path / "first"))
    first = paths.data_dir()
    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path / "second"))
    assert paths.data_dir() == first


def test_storage_and_logs_share_one_data_directory(monkeypatch, tmp_path):
    """
    The database and the logs must never disagree about where Monitra's data
    lives -- support cannot ask for "the log next to the database" otherwise.
    """
    monkeypatch.setenv("MONITRA_DATA_DIR", str(tmp_path / "shared"))

    from core.logging_setup import log_dir
    from storage.manager import cache_dir, db_path

    assert cache_dir() == tmp_path / "shared"
    assert db_path().parent == tmp_path / "shared"
    assert log_dir() == tmp_path / "shared" / "logs"


def test_data_dir_falls_back_when_the_preferred_location_is_unusable(
    monkeypatch, tmp_path
):
    """
    A portable build extracted into a read-only folder must still start, with
    its data in the user's home, rather than failing at import time.
    """
    blocker = tmp_path / "read-only-parent"
    blocker.write_text("this is a file, so nothing can be created underneath it")
    monkeypatch.setattr(paths, "_candidate_data_dir", lambda: blocker / "data")

    assert paths.data_dir() == Path.home() / ".monitra"


def test_portable_mode_requires_both_frozen_and_the_marker(monkeypatch, tmp_path):
    monkeypatch.delenv("MONITRA_DATA_DIR", raising=False)
    marker_dir = tmp_path / "Monitra-Portable"
    marker_dir.mkdir()
    (marker_dir / paths.PORTABLE_MARKER).write_text("portable")

    # A source checkout is never portable, even with a stray marker file.
    monkeypatch.setattr(paths, "app_dir", lambda: marker_dir)
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    assert paths.is_portable() is False
    assert paths.data_dir() == Path.home() / ".monitra"

    paths.reset_cache()
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    assert paths.is_portable() is True
    assert paths.data_dir() == marker_dir / "data"


def test_a_frozen_build_without_the_marker_never_writes_beside_the_executable(
    monkeypatch, tmp_path
):
    """
    An installed build's directory is read-only under Program Files and is
    replaced wholesale on upgrade. Data must not land there.
    """
    monkeypatch.delenv("MONITRA_DATA_DIR", raising=False)
    install_dir = tmp_path / "Program Files" / "Monitra"
    install_dir.mkdir(parents=True)
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(paths, "app_dir", lambda: install_dir)

    assert paths.data_dir() == Path.home() / ".monitra"


# ── Resource resolution ──────────────────────────────────────────────────────

def test_resource_path_resolves_into_the_extraction_dir_when_frozen(monkeypatch, tmp_path):
    """
    Under PyInstaller, __file__ points inside sys._MEIPASS rather than at the
    source tree, so a resource path built from __file__ finds nothing.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    try:
        assert paths.resource_path("ui", "styles.py") == tmp_path / "ui" / "styles.py"
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)


def test_resource_path_resolves_into_the_source_tree_when_not_frozen():
    assert paths.resource_path("main.py").exists()


# ── Backend configuration ────────────────────────────────────────────────────

def test_an_unconfigured_build_targets_the_live_backend(monkeypatch):
    config = _reload_config(monkeypatch)
    assert config.settings.ENVIRONMENT == config.PRODUCTION
    assert config.settings.SMS_API_BASE_URL == config.LIVE_API_BASE_URL
    assert config.settings.SMS_API_BASE_URL.startswith("https://")
    assert config.settings.error is None


def test_the_process_environment_overrides_everything(monkeypatch):
    config = _reload_config(monkeypatch, SMS_API_BASE_URL="https://staging.example.com/")
    assert config.settings.SMS_API_BASE_URL == "https://staging.example.com"
    assert config.settings.error is None


def test_development_selects_a_local_backend_only_when_asked(monkeypatch):
    config = _reload_config(monkeypatch, MONITRA_ENV="development")
    # 127.0.0.1 rather than localhost: on Windows the latter resolves to ::1
    # first, and a dev server bound to IPv4 only makes every request wait out
    # the failed IPv6 attempt -- 2.06s each, measured, which pushed a grid of
    # thumbnails past its timeout and reported a working backend as down.
    assert config.settings.SMS_API_BASE_URL == "http://127.0.0.1:8000"
    assert config.settings.error is None


def test_staging_without_a_url_is_reported_rather_than_guessed(monkeypatch):
    """
    There is no baked-in staging hostname: inventing one would ship an address
    nobody verified. Selecting staging without supplying one is an error the
    user can act on.
    """
    config = _reload_config(monkeypatch, MONITRA_ENV="staging")
    assert config.settings.error is not None
    assert "SMS_API_BASE_URL" in config.settings.error


def test_a_shipped_build_pointed_at_localhost_is_an_error(monkeypatch):
    """
    A packaged build configured for a local backend cannot work on a staff
    machine. It must say so, not silently relabel itself as development.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    try:
        config = _reload_config(monkeypatch, SMS_API_BASE_URL="http://localhost:8000")
        assert config.settings.ENVIRONMENT == config.PRODUCTION
        assert config.settings.error is not None
        assert "production" in config.settings.error
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)
        _reload_config(monkeypatch)


def test_a_source_checkout_pointed_at_localhost_is_development(monkeypatch):
    assert not getattr(sys, "frozen", False)
    config = _reload_config(monkeypatch, SMS_API_BASE_URL="http://127.0.0.1:8000")
    assert config.settings.ENVIRONMENT == config.DEVELOPMENT
    assert config.settings.error is None


def test_a_non_http_url_is_rejected(monkeypatch):
    config = _reload_config(monkeypatch, SMS_API_BASE_URL="staffmanagement.example.com")
    assert config.settings.error is not None
    assert "http://" in config.settings.error


def test_the_startup_summary_leaks_no_secret(monkeypatch):
    monkeypatch.setenv("SMS_API_BASE_URL", "https://api.example.com")
    config = _reload_config(monkeypatch, SMS_API_BASE_URL="https://api.example.com")
    summary = config.settings.describe()
    assert "https://api.example.com" in summary
    assert "token" not in summary.lower()
    assert "password" not in summary.lower()


# ── Version single-sourcing ──────────────────────────────────────────────────

def test_version_is_a_plain_three_part_number():
    """
    Windows' VERSIONINFO and macOS' CFBundleVersion both require numeric-only
    components; a suffix would have to be stripped in two places and drift.
    """
    assert re.fullmatch(r"\d+\.\d+\.\d+", version_module.VERSION)
    assert version_module.version_tuple() == (
        *(int(part) for part in version_module.VERSION.split(".")), 0,
    )


def test_a_prerelease_label_is_plain_and_only_decorates_the_display_forms(monkeypatch):
    """
    An internal test build is told apart by a label, not by a different
    number: the .exe resource, the Info.plist and the User-Agent all require
    or parse the numeric form, so the label may only ever reach the places a
    person reads -- the window title, the update dialog, the artifact name.
    """
    assert re.fullmatch(r"[A-Za-z0-9.]*", version_module.PRERELEASE)

    monkeypatch.setattr(version_module, "PRERELEASE", "beta.1")
    assert version_module.is_prerelease() is True
    assert version_module.display_version() == f"{version_module.VERSION}-beta.1"
    assert version_module.artifact_version() == version_module.display_version()
    assert version_module.user_agent() == f"Monitra/{version_module.VERSION}"
    assert version_module.version_tuple()[:3] == tuple(
        int(part) for part in version_module.VERSION.split(".")
    )

    monkeypatch.setattr(version_module, "PRERELEASE", "")
    assert version_module.is_prerelease() is False
    assert version_module.display_version() == version_module.VERSION
    assert version_module.artifact_version() == version_module.VERSION


def test_the_installer_names_its_output_by_the_artifact_version():
    """
    The installer a tester downloads must be unmistakable from the production
    one, and a production build must be unaffected: the .iss names the file by
    ArtifactVersion, which falls back to AppVersion when the build script
    passes nothing.
    """
    iss = (DESKTOP_ROOT / "packaging" / "windows" / "monitra.iss").read_text(encoding="utf-8")
    assert "OutputBaseFilename=Monitra-Setup-{#ArtifactVersion}" in iss
    assert "#define ArtifactVersion AppVersion" in iss
    # The numeric fields Windows validates keep the numeric version.
    assert "VersionInfoVersion={#AppVersion}" in iss
    assert "AppVersion={#AppVersion}" in iss

    installer = (DESKTOP_ROOT / "scripts" / "build_installer.ps1").read_text(encoding="utf-8")
    assert "version.artifact_version()" in installer
    assert "/DArtifactVersion=$ArtifactVersion" in installer
    for relative in ("scripts/build_portable.ps1", "scripts/build_macos.sh"):
        assert "version.artifact_version()" in (DESKTOP_ROOT / relative).read_text(
            encoding="utf-8"
        ), relative


def test_the_window_title_shows_the_display_version():
    """A support report from a tester must name the build it came from."""
    source = (DESKTOP_ROOT / "main.py").read_text(encoding="utf-8")
    assert 'setWindowTitle(f"{APP_DISPLAY_NAME} {display_version()}")' in source


@pytest.mark.parametrize("relative", [
    "packaging/monitra.spec",
    "packaging/windows/monitra.iss",
    "scripts/build_windows.ps1",
    "scripts/build_installer.ps1",
    "scripts/build_macos.sh",
])
def test_no_build_file_hardcodes_a_version(relative):
    """
    version.py is the single source of truth. A second literal anywhere in the
    build chain is how an installer ends up claiming a version its binary does
    not have.
    """
    text = (DESKTOP_ROOT / relative).read_text(encoding="utf-8")
    literals = re.findall(r"(?<![\w.])\d+\.\d+\.\d+(?![\w.])", text)
    assert version_module.VERSION not in literals, (
        f"{relative} contains the literal {version_module.VERSION}; read the "
        f"version from version.py instead so the installer, the executable "
        f"metadata and the About line cannot disagree"
    )


def test_the_installer_and_the_bundle_agree_on_the_application_identity():
    """
    The Inno Setup AppId and the macOS bundle identifier are what make an
    upgrade an upgrade (rather than a second parallel install) and what macOS
    ties granted permissions to. Both must stay stable, and the human-facing
    name must match version.py.
    """
    iss = (DESKTOP_ROOT / "packaging" / "windows" / "monitra.iss").read_text(encoding="utf-8")
    assert f'#define AppName          "{version_module.APP_NAME}"' in iss
    assert "AppId={{8F3B6A94-2C57-4E1B-9A0D-6B7C4E9A1D22}" in iss
    assert version_module.BUNDLE_ID == "com.monitra.desktop"


def test_the_installer_requests_no_administrator_rights():
    """
    Monitra reads a window title, counts input events and talks HTTPS out. It
    needs no elevation, and asking for it on a monitoring tool is a trust cost
    with no benefit.
    """
    iss = (DESKTOP_ROOT / "packaging" / "windows" / "monitra.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in iss


def test_the_installer_does_not_delete_user_data_on_uninstall():
    """
    Uninstalling must never silently destroy tracked time that has not yet
    reached the server.
    """
    iss = (DESKTOP_ROOT / "packaging" / "windows" / "monitra.iss").read_text(encoding="utf-8")
    assert ".monitra" not in re.sub(r"^\s*;.*$", "", iss, flags=re.MULTILINE)


# ── macOS permissions ────────────────────────────────────────────────────────

def test_the_macos_bundle_declares_every_permission_it_uses_and_no_others():
    """
    macOS silently denies (or kills) a process that uses a protected resource
    without the matching NSUsageDescription. Equally, a monitoring tool asking
    for permissions it does not use is a trust problem -- so the absent ones
    are asserted too.
    """
    spec = (DESKTOP_ROOT / "packaging" / "monitra.spec").read_text(encoding="utf-8")

    # Needed: window titles (app usage + browser URLs) and input counting.
    assert "NSScreenCaptureUsageDescription" in spec
    assert "NSInputMonitoringUsageDescription" in spec

    # Not used by any code path in this application.
    for unused in (
        "NSCameraUsageDescription",
        "NSMicrophoneUsageDescription",
        "NSLocationWhenInUseUsageDescription",
        "NSContactsUsageDescription",
        "NSAppleEventsUsageDescription",
    ):
        # Matched as a quoted plist key, so the comment in the spec that
        # explains why Apple Events is deliberately not requested does not
        # read as a declaration of it.
        assert f'"{unused}"' not in spec, f"{unused} is declared but nothing uses it"


def test_the_hardened_runtime_entitlements_stay_minimal():
    """
    allow-unsigned-executable-memory and allow-jit are the two entitlements
    routinely copy-pasted into Python bundles without need, and both materially
    weaken the hardened runtime. CPython here needs neither.
    """
    entitlements = (
        DESKTOP_ROOT / "packaging" / "macos" / "entitlements.plist"
    ).read_text(encoding="utf-8")
    body = entitlements.split("-->", 1)[-1]

    assert "com.apple.security.cs.disable-library-validation" in body
    assert "com.apple.security.cs.allow-unsigned-executable-memory" not in body
    assert "com.apple.security.cs.allow-jit" not in body
    assert "com.apple.security.app-sandbox" not in body


# ── Single instance ──────────────────────────────────────────────────────────

def test_the_installer_checks_the_lock_the_application_actually_takes():
    """
    The installer refuses to overwrite a running installation by checking a
    named mutex. If the application stopped creating that exact name, the
    check would silently pass forever and upgrades would write over open
    files.
    """
    from core import single_instance

    iss = (DESKTOP_ROOT / "packaging" / "windows" / "monitra.iss").read_text(encoding="utf-8")
    assert f"CheckForMutexes('{single_instance.WINDOWS_MUTEX_NAME}')" in iss


def test_acquiring_the_instance_lock_never_raises(monkeypatch):
    """
    Refusing to launch because the *lock* could not be evaluated would be a
    worse failure than the duplicate process it guards against.
    """
    from core import single_instance

    monkeypatch.setattr(
        single_instance, "_acquire_windows",
        lambda: (_ for _ in ()).throw(OSError("no handle")),
    )
    monkeypatch.setattr(
        single_instance, "_acquire_posix",
        lambda: (_ for _ in ()).throw(OSError("no lock file")),
    )
    assert single_instance.acquire() is True


# ── Security ─────────────────────────────────────────────────────────────────

def test_no_secret_is_committed_in_anything_the_build_packages():
    """
    The desktop client authenticates as a user with a token obtained at login.
    It has no API key, no signing key and no database credential -- and a
    server-side secret compiled into a binary shipped to staff laptops cannot
    be revoked by deleting a file.
    """
    suspicious = re.compile(
        r"(?i)(secret_key|api_key|private_key|aws_secret|"
        r"password\s*=\s*['\"][^'\"]+['\"]|"
        r"postgres(ql)?://[^\s'\"]*:[^\s'\"]*@)"
    )
    skip_dirs = {".venv-build", "__pycache__", "build", "dist", ".pytest_cache", "tests"}

    offenders = []
    for path in DESKTOP_ROOT.rglob("*.py"):
        if any(part in skip_dirs for part in path.relative_to(DESKTOP_ROOT).parts):
            continue
        match = suspicious.search(path.read_text(encoding="utf-8", errors="ignore"))
        if match:
            offenders.append(f"{path.relative_to(DESKTOP_ROOT)}: {match.group(0)[:40]}")

    assert not offenders, "possible secret in packaged source: " + "; ".join(offenders)


def test_no_environment_file_is_bundled_into_the_package():
    """
    A .env baked into the build would ship one deployment's backend to every
    install and could not be changed without a rebuild. It is read from beside
    the executable at runtime instead.
    """
    spec = (DESKTOP_ROOT / "packaging" / "monitra.spec").read_text(encoding="utf-8")
    datas = spec[spec.index("datas="):spec.index("hiddenimports=")]

    # The only thing the build bundles is desktop/assets/ -- the optional
    # brand logo core/branding.py reads. Nothing environment-shaped may join
    # it: a .env baked into the build would ship one deployment's backend to
    # every install and could not be changed without a rebuild.
    assert ".env" not in datas
    assert "environ" not in datas.lower()
    assert '"assets"' in datas
