"""
Handing a verified artifact to the platform's installer, safely.

The problem this solves
-----------------------
A running process holds its own executable and libraries open. On Windows the
Inno Setup installer refuses to run at all while Monitra's named mutex is held
(``packaging/windows/monitra.iss``), and on macOS replacing a mounted bundle
underneath itself is undefined. So the update cannot be applied by the process
being updated — something has to outlive it.

That something is a small helper script, written to the update scratch
directory and launched **detached**. Its whole job is:

    wait for this process to exit  →  run the installer  →  relaunch Monitra

Because the helper only starts installing once Monitra has genuinely exited,
the ordinary shutdown path runs first, unchanged: services stop in reverse
order, the sync queue and the timer's state are already durable in SQLite, the
WAL is checkpointed and the database is closed. Nothing about the update needs
special handling for the timer, the cache, screenshots or activity capture,
because none of it is being interrupted differently from a normal quit — and
``~/.monitra`` is outside the installation directory, so the installer cannot
touch it either way.

Failure leaves a working application
------------------------------------
Every path here is written so that the worst outcome is "the update did not
happen". On Windows, Inno Setup either completes or rolls back its own
transaction, and the helper relaunches whatever is installed afterwards
regardless. On macOS the existing bundle is moved aside rather than deleted,
and is moved back if the copy fails — so a failed copy ends with the previous
version in place and running, never with an empty ``/Applications``.

…and a failure is never silent
-------------------------------
The helper outlives the application, so it cannot show anything itself. It
writes ``update-result.txt`` in the data directory instead — the target version
and the installer's exit code — and the *next* launch reads it
(`read_and_clear_result`), compares it with the version actually running, and
tells the user whether the update happened. A relaunch of the old version after
a failed installer therefore reads, to the user, as "the update could not be
installed", never as a silent no-op and never as success.

No path is ever pasted into a script
------------------------------------
On Windows every path reaches the helper through the **environment**, and the
script itself is pure ASCII. A username with an accent, a space, ``&`` or ``%``
in it is ordinary on a staff machine, and an ASCII batch file with the path
written into it fails on the first one (it could not even be encoded). Cmd
expands ``%VAR%`` from its own Unicode environment and quotes the result, so
none of those characters is ever re-parsed as syntax.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

from core.logging_setup import get_logger
from core.paths import data_dir, is_frozen, is_portable

from . import policy
from .downloader import updates_dir

log = get_logger("updates.install")

#: How long the helper waits for Monitra to exit before giving up, in seconds.
#: Shutdown is bounded by the runtime's own timeouts and completes in well
#: under a second in practice; this is the ceiling that stops a helper waiting
#: forever on a process that will not die, which would leave the installer
#: never running and no explanation anywhere.
EXIT_WAIT_SECONDS = 120

#: Where the helper reports what happened. In the data directory, *not* the
#: update scratch directory: `clear_stale_downloads` empties that at every
#: launch, and this file has to survive until it has been read.
RESULT_FILENAME = "update-result.txt"

#: Exit codes the macOS helper reports, by the step that failed.
MAC_EXIT_MOUNT = 10
MAC_EXIT_NO_BUNDLE = 11
MAC_EXIT_CODESIGN = 12
MAC_EXIT_GATEKEEPER = 13
MAC_EXIT_TEAM = 14
MAC_EXIT_MOVE = 15
MAC_EXIT_COPY = 16

_EXPECTED_SUFFIX = {"win32": ".exe", "darwin": ".dmg"}


class InstallError(Exception):
    """The handoff could not be started. The application is untouched."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


def result_path() -> Path:
    return data_dir() / RESULT_FILENAME


def _current_pid() -> int:
    """The process the helper waits for. A function so a test can point the
    helper at a short-lived stand-in instead of the test runner itself."""
    return os.getpid()


def read_and_clear_result() -> Optional[Dict[str, str]]:
    """What the previous process's installer helper recorded, or None.

    Returned as ``{"target": "1.3.2", "stage": ..., "exit_code": "0"}``; the
    ``exit_code`` key is absent when the helper started but never reported one
    (the machine was restarted mid-install, or the helper was killed).

    Read once and deleted, so a result is reported exactly once. Never raises: a
    missing or unreadable file means "no update was in flight".
    """
    path = result_path()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("could not read the update result file")
        return None
    try:
        path.unlink()
    except OSError:
        log.warning("could not remove the update result file")
    values: Dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            values[key.strip()] = value.strip()
    return values or None


def can_install() -> Optional[str]:
    """Why an in-place install is not possible here, or None if it is.

    Three honest refusals, each of which would otherwise become a confusing
    failure halfway through an update:

    * **Running from source.** There is no installed application to replace.
    * **A portable build.** It is a folder the user unzipped wherever they
      chose; there is no installer, and silently rewriting an arbitrary
      directory is not something to do on the user's behalf.
    * **An unsupported platform.** Linux has no packaged artifact in this
      project, so there is nothing to hand off to.
    """
    if not is_frozen():
        return (
            "Automatic updates are only available in an installed build of "
            "Monitra."
        )
    if is_portable():
        return (
            "This is the portable build of Monitra. Download the new portable "
            "package and replace this folder to update."
        )
    if sys.platform not in ("win32", "darwin"):
        return "Automatic updates are not available on this platform."
    return None


def launch_installer(artifact: Path, version: str) -> None:
    """Start the detached helper that will install `artifact` and relaunch.

    Returns as soon as the helper is running. The caller's next act must be an
    ordinary application quit: the helper is already waiting for this process
    to disappear.

    Fast and local: it writes a small script and starts a process. The
    signature of the artifact was already verified on the task pool before this
    is reached (`UpdateService._download`), because that check can wait on the
    network and this runs on the GUI thread.

    :raises InstallError: if the helper could not be started, in which case
        nothing has changed and the current installation is still running.
        Every failure is an `InstallError` — an unexpected exception here would
        otherwise leave the update state machine stranded half-way.
    """
    blocked = can_install()
    if blocked:
        raise InstallError(blocked, detail="install path unavailable")
    if not artifact.is_file():
        raise InstallError(
            "The downloaded update could not be found.",
            detail=f"missing artifact {artifact}",
        )
    expected = _EXPECTED_SUFFIX.get(sys.platform)
    if expected and artifact.suffix.lower() != expected:
        raise InstallError(
            "The downloaded update is not an installer for this system.",
            detail=f"unexpected artifact type {artifact.suffix!r}",
        )

    try:
        if sys.platform == "win32":
            command, env, kwargs = _windows_launch(artifact, version)
        else:
            command, env, kwargs = _macos_launch(artifact, version)
    except InstallError:
        raise
    except Exception as exc:  # noqa: BLE001 - nothing here may escape unclassified
        log.exception("could not prepare the update helper")
        raise InstallError(
            "The update could not be started. Your current version of "
            "Monitra is unaffected.",
            detail=repr(exc),
        )

    try:
        subprocess.Popen(  # noqa: S603 - a script this module just wrote
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(updates_dir()),
            env=env,
            **kwargs,
        )
    except OSError as exc:
        raise InstallError(
            "The update could not be started. Your current version of "
            "Monitra is unaffected.",
            detail=str(exc),
        )

    log.info("installer helper launched for version %s (pid %s)", version, _current_pid())


def _windows_launch(artifact: Path, version: str):
    script = _write_windows_helper(version)
    env = _helper_environment(artifact, version)
    # `cmd /d /c call <script>`: /d skips AutoRun commands a managed machine may
    # define, and the leading `call` keeps cmd from stripping the quotes around
    # a script path that contains a space.
    command = ["cmd.exe", "/d", "/c", "call", str(script)]
    # CREATE_NO_WINDOW + CREATE_NEW_PROCESS_GROUP: the helper must not die with
    # us, must not inherit our console, and must not be reached by a Ctrl+C
    # delivered to this process group as it exits -- but it does need a console
    # of its own, hidden. The first version used DETACHED_PROCESS (no console at
    # all), and under that tasklist prints nothing and a pipe into find never
    # completes: the helper waited for ever, so the installer never ran and the
    # application was never relaunched. Found by running the real helper, not by
    # reading it; see tests/test_update_hardening.py::TestWindowsHelper.
    return command, env, {"creationflags": 0x08000000 | 0x00000200}


def _macos_launch(artifact: Path, version: str):
    script = _write_macos_helper(artifact, version)
    # A new session, so the helper is not in this process's process group and
    # survives it.
    return ["/bin/bash", str(script)], None, {"start_new_session": True}


def _helper_environment(artifact: Path, version: str) -> Dict[str, str]:
    """The helper's inputs. Paths travel here, never inside the script."""
    env = dict(os.environ)
    env.update({
        "MONITRA_UPDATE_PID": str(_current_pid()),
        "MONITRA_UPDATE_WAIT_TRIES": str(EXIT_WAIT_SECONDS),
        "MONITRA_UPDATE_ARTIFACT": str(artifact),
        "MONITRA_UPDATE_RELAUNCH": str(_relaunch_target()),
        "MONITRA_UPDATE_RESULT": str(result_path()),
        "MONITRA_UPDATE_TARGET": version,
    })
    return env


def _relaunch_target() -> Path:
    """What the helper should start once the installer has finished.

    Resolved from the *running* executable rather than hardcoded, so a build
    installed somewhere unusual still relaunches itself. On macOS the bundle is
    the thing to open, not the binary inside it — opening the inner binary
    directly gives a process with no application identity, which loses the
    tray icon and the TCC permission grants.
    """
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for parent in executable.parents:
            if parent.suffix == ".app":
                return parent
    return executable


#: The Windows helper. Pure ASCII, and contains no path: everything it needs
#: arrives as MONITRA_UPDATE_* environment variables (see the module docstring).
_WINDOWS_HELPER = r"""@echo off
setlocal
rem Monitra update helper. Written by the running application and deleted by
rem itself at the end; safe to remove at any time. Pure ASCII: every path
rem arrives through the environment.
set TRIES=%MONITRA_UPDATE_WAIT_TRIES%
:waitloop
rem Is the old process still alive? tasklist prints one CSV row for it
rem ("name","pid",...) and nothing useful once it is gone. Read through
rem `for /f`, not a pipe into find: a pipe here once hung the helper for ever
rem when it had no console (see _windows_launch), and a bare `find` can resolve
rem to a different program entirely when PATH carries a Unix toolkit. Absolute
rem System32 paths for the same reason.
set ALIVE=
for /f "tokens=2 delims=," %%A in ('%SystemRoot%\System32	asklist.exe /FI "PID eq %MONITRA_UPDATE_PID%" /NH /FO CSV 2^>nul') do if "%%~A"=="%MONITRA_UPDATE_PID%" set ALIVE=1
if not defined ALIVE goto ready
set /a TRIES-=1
if %TRIES% LEQ 0 goto ready
rem ping as a sleep: `timeout` refuses to run without an interactive console.
%SystemRoot%\System32\ping.exe -n 2 127.0.0.1 >nul
goto waitloop
:ready
rem A short grace period even when the process is already gone: the OS needs a
rem moment to release file locks on the old executable (and a scanner may still
rem be reading it), and if tasklist could not answer at all this is the only wait
rem there is.
%SystemRoot%\System32\ping.exe -n 4 127.0.0.1 >nul
rem Recorded BEFORE the installer runs, without an exit code: if the machine is
rem restarted mid-install the next launch finds "started" and no code, and says
rem the update did not complete instead of saying nothing.
>"%MONITRA_UPDATE_RESULT%" echo target=%MONITRA_UPDATE_TARGET%
>>"%MONITRA_UPDATE_RESULT%" echo stage=started
rem /SILENT (not /VERYSILENT) still shows a progress window, which is the right
rem call: the user asked for this and a visible progress bar is what
rem distinguishes "updating" from "the app vanished". /SP- skips the "this will
rem install..." prompt, /NORESTART stops the installer rebooting the machine,
rem and /CLOSEAPPLICATIONS lets it deal with any straggler holding a file open
rem rather than failing the install.
start "" /wait "%MONITRA_UPDATE_ARTIFACT%" /SILENT /SP- /NORESTART /CLOSEAPPLICATIONS
set RC=%ERRORLEVEL%
>>"%MONITRA_UPDATE_RESULT%" echo exit_code=%RC%
rem Relaunch whatever is installed, whether or not the installer succeeded.
rem Inno rolls its own transaction back on failure, so this starts the previous
rem version rather than nothing at all -- which is the difference between a
rem failed update and a lost application. The result file above is what stops
rem that relaunch reading as success.
start "" "%MONITRA_UPDATE_RELAUNCH%"
rem Delete the artifact and then this script. A running .cmd may delete itself;
rem the interpreter has already buffered the line.
del /q "%MONITRA_UPDATE_ARTIFACT%" >nul 2>&1
del /q "%~f0" >nul 2>&1
"""


def _write_windows_helper(version: str) -> Path:
    """Write the batch file that installs on Windows and relaunches.

    A ``.cmd`` rather than PowerShell on purpose: PowerShell script execution
    is routinely restricted by group policy on managed machines, and an updater
    that works only where the execution policy allows it is an updater that
    fails exactly where IT control is tightest.
    """
    script = updates_dir() / f"apply-update-{_safe_token(version)}.cmd"
    script.write_text(_WINDOWS_HELPER.replace("\n", "\r\n"), encoding="ascii")
    return script


_MACOS_HELPER = r"""#!/bin/bash
# Monitra update helper for @VERSION@. Written by the running application and
# deleted by itself at the end; safe to remove at any time.
set -u

PID=@PID@
DMG=@DMG@
BUNDLE=@BUNDLE@
RESULT=@RESULT@
TARGET=@TARGET@
BACKUP="$BUNDLE.previous"
TRIES=@TRIES@
REQUIRE_SIGNED=@REQUIRE_SIGNED@
TEAM_PINS=@TEAM_PINS@

# Recorded without an exit code first: a helper that dies mid-way leaves
# "started" and no code, which the next launch reports as an incomplete update.
printf 'target=%s\nstage=started\n' "$TARGET" > "$RESULT"
finish() {
  printf 'target=%s\nstage=%s\nexit_code=%s\n' "$TARGET" "$1" "$2" > "$RESULT"
}

# Wait for Monitra to exit. A mounted bundle must not be replaced underneath a
# running process.
while kill -0 "$PID" 2>/dev/null && [ "$TRIES" -gt 0 ]; do
  sleep 1
  TRIES=$((TRIES - 1))
done

MOUNT=$(mktemp -d /tmp/monitra-update.XXXXXX)
cleanup() {
  hdiutil detach "$MOUNT" -quiet 2>/dev/null || true
  rmdir "$MOUNT" 2>/dev/null || true
  rm -f "$DMG"
  rm -f "$0"
}
# Nothing has been touched on any of these paths: the installed application is
# exactly as it was. Start it again, say why, and stop.
abort() {
  finish "$1" "$2"
  open "$BUNDLE" 2>/dev/null || true
  cleanup
  exit "$2"
}

if ! hdiutil attach -nobrowse -quiet -mountpoint "$MOUNT" "$DMG"; then
  abort mount @EXIT_MOUNT@
fi

NEW="$MOUNT/Monitra.app"
if [ ! -d "$NEW" ]; then
  abort bundle @EXIT_NO_BUNDLE@
fi

# The signature check Gatekeeper itself applies, made before the old bundle is
# touched. A downloaded DMG's checksum proves it is the file the backend named;
# this proves who built it.
if [ "$REQUIRE_SIGNED" = "1" ]; then
  if ! codesign --verify --deep --strict "$NEW" 2>/dev/null; then
    abort codesign @EXIT_CODESIGN@
  fi
  if ! spctl --assess --type execute "$NEW" 2>/dev/null; then
    abort gatekeeper @EXIT_GATEKEEPER@
  fi
  if [ -n "$TEAM_PINS" ]; then
    TEAM=$(codesign -dv --verbose=4 "$NEW" 2>&1 | sed -n 's/^TeamIdentifier=//p' | head -n 1)
    case " $TEAM_PINS " in
      *" $TEAM "*) ;;
      *) abort team @EXIT_TEAM@ ;;
    esac
  fi
fi

# Move the old bundle aside rather than deleting it, so there is something to
# put back. This is the step that guarantees a failed update still leaves a
# working application.
rm -rf "$BACKUP"
if [ -d "$BUNDLE" ] && ! mv "$BUNDLE" "$BACKUP"; then
  abort move @EXIT_MOVE@
fi

if cp -R "$NEW" "$BUNDLE"; then
  rm -rf "$BACKUP"
  finish done 0
  open "$BUNDLE" 2>/dev/null || true
  cleanup
  exit 0
fi

# Put the previous version back and start it. The user keeps a working
# Monitra; only the update failed.
rm -rf "$BUNDLE"
mv "$BACKUP" "$BUNDLE" 2>/dev/null || true
abort copy @EXIT_COPY@
"""


def _write_macos_helper(artifact: Path, version: str) -> Path:
    """Write the shell script that installs on macOS and relaunches.

    The DMG is mounted with ``-nobrowse`` so no Finder window appears, the new
    bundle is verified (signature, Gatekeeper, and the pinned Team ID when one
    is configured), copied into place beside the old one, and the old one is
    only discarded once the copy has succeeded. If anything fails, the old
    bundle is put back — the user ends up on the version they started on,
    running, and the next launch says the update did not happen.

    **Never executed on a real Mac by this project's tests** — see
    docs/Desktop_Release_Runbook.md. The generated text is unit-tested; the
    behaviour is not yet verified on hardware.
    """
    bundle = _relaunch_target()
    script = updates_dir() / f"apply-update-{_safe_token(version)}.sh"
    pins = " ".join(policy.MACOS_TEAM_ID_PINS)
    filled = (
        _MACOS_HELPER
        .replace("@VERSION@", _safe_token(version))
        .replace("@PID@", str(_current_pid()))
        .replace("@DMG@", _sh_quote(str(artifact)))
        .replace("@BUNDLE@", _sh_quote(str(bundle)))
        .replace("@RESULT@", _sh_quote(str(result_path())))
        .replace("@TARGET@", _sh_quote(version))
        .replace("@TRIES@", str(EXIT_WAIT_SECONDS))
        .replace("@REQUIRE_SIGNED@", "0" if policy.unsigned_allowed() else "1")
        .replace("@TEAM_PINS@", _sh_quote(pins))
        .replace("@EXIT_MOUNT@", str(MAC_EXIT_MOUNT))
        .replace("@EXIT_NO_BUNDLE@", str(MAC_EXIT_NO_BUNDLE))
        .replace("@EXIT_CODESIGN@", str(MAC_EXIT_CODESIGN))
        .replace("@EXIT_GATEKEEPER@", str(MAC_EXIT_GATEKEEPER))
        .replace("@EXIT_TEAM@", str(MAC_EXIT_TEAM))
        .replace("@EXIT_MOVE@", str(MAC_EXIT_MOVE))
        .replace("@EXIT_COPY@", str(MAC_EXIT_COPY))
    )
    script.write_text(filled, encoding="utf-8")
    script.chmod(0o700)
    return script


def _safe_token(value: str) -> str:
    """A version string reduced to characters safe in a file name and a comment."""
    return "".join(ch for ch in value if ch.isalnum() or ch in ".-_") or "unknown"


def _sh_quote(value: str) -> str:
    """Single-quote a value for the shell, escaping embedded quotes.

    Paths here come from `sys.executable` and from a filename this application
    chose, so neither is attacker-controlled — but quoting a path that goes
    into a generated script is not a place to rely on that staying true.
    """
    return "'" + value.replace("'", "'\\''") + "'"
