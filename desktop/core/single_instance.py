"""
core.single_instance — guarantee at most one Monitra process per user.

Two copies of this application running at once is not a cosmetic problem. It
is the packaged-build form of the duplicate-worker failure DO_NOT_DO.md
documents: two TimerServices deriving elapsed time, two SyncServices draining
the same durable queue (uploading each row twice, or fighting over the
idempotency keys that are supposed to prevent exactly that), two
AppUsageServices writing overlapping activity windows, and two writers on one
SQLite database.

Packaging makes this markedly easier to hit than a source checkout did. An
installed build has a Start Menu entry, a desktop shortcut, an optional
startup entry and a tray icon, and closing the window *hides to tray* rather
than exiting — so "I closed it, I'll open it again" produces a second
process, and the user cannot tell, because the first one has no window
either.

The lock is held for the entire process lifetime and released by the OS on
exit, including a crash or a kill. There is deliberately no manual release
path and no stale-lock cleanup to get wrong:

    Windows   A named mutex. The kernel destroys it when the last handle
              closes, which happens on process exit however that occurs. The
              name is also what the installer checks (`Global\\MonitraRunning`
              in packaging/windows/monitra.iss) to refuse to overwrite a
              running installation — the two must stay in sync.

    macOS     An flock() on a file in the data directory. flock is released
              on process exit; a leftover lock *file* is harmless because the
              lock itself, not the file's existence, is what is tested.

The lock is per-user, not per-machine: two different people signed into the
same machine are two independent Monitra users with separate data
directories, and must both be able to run it.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Optional

from core.logging_setup import get_logger

log = get_logger("single-instance")

#: Must match CheckForMutexes() in packaging/windows/monitra.iss.
#:
#: "Global\" would make the lock machine-wide and would stop a second user on
#: a shared or terminal-server machine from running Monitra at all. The
#: session-local namespace is what we want; Inno Setup's CheckForMutexes
#: checks both namespaces, so the installer still sees it.
WINDOWS_MUTEX_NAME = "MonitraRunning"

_handle = None  # kept alive for the process lifetime; never closed


def _lock_scope() -> str:
    """What one lock guards: one data directory.

    The harm the lock prevents is two runtimes on one database and one
    durable queue. An instance launched with ``MONITRA_DATA_DIR`` pointing
    somewhere else -- the soak and launch-cycle harnesses, a developer's
    scratch copy beside the installed one -- shares nothing with it, and
    refusing to start it would only mean those checks cannot run while
    Monitra is in use. The default data directory keeps the bare name the
    installer checks; an override suffixes it with a digest of the path,
    so two instances on one directory are still refused.
    """
    override = os.getenv("MONITRA_DATA_DIR")
    if not override:
        return ""
    normalised = str(Path(override).expanduser().resolve()).lower()
    return "-" + hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:12]


def _mutex_name() -> str:
    return WINDOWS_MUTEX_NAME + _lock_scope()


def acquire() -> bool:
    """
    Try to become the single running instance.

    Returns True if this process now holds the lock, False if another Monitra
    process already does. Never raises: if the lock cannot be evaluated (an
    unsupported platform, a read-only data directory), it returns True and
    lets the application start. Refusing to launch because a *lock* failed
    would be a worse failure than the duplicate it guards against.
    """
    global _handle
    try:
        if sys.platform == "win32":
            _handle = _acquire_windows()
        else:
            _handle = _acquire_posix()
    except Exception:
        log.warning("could not evaluate the single-instance lock; starting anyway",
                    exc_info=True)
        return True
    return _handle is not None


def _acquire_windows():
    import ctypes
    from ctypes import wintypes

    ERROR_ALREADY_EXISTS = 183

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    handle = kernel32.CreateMutexW(None, False, _mutex_name())
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        # The handle is valid but someone else created the mutex first.
        kernel32.CloseHandle(handle)
        return None
    return handle


def _acquire_posix():
    import fcntl

    from core.paths import data_dir

    lock_path: Path = data_dir() / "monitra.lock"
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def already_running_message() -> str:
    """User-facing explanation for the second instance, before it exits."""
    return (
        "Monitra is already running.\n\n"
        "Closing the Monitra window leaves it running in the background so "
        "your time keeps being tracked. Look for the Monitra icon in the "
        "notification area (Windows) or the menu bar (macOS) and click it to "
        "bring the window back."
    )


def describe() -> Optional[str]:
    """Return how the lock is held, for the startup log."""
    if _handle is None:
        return None
    return "windows-mutex" if sys.platform == "win32" else "posix-flock"
