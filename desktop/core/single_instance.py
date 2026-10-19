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

The second launch is a request, not a mistake
---------------------------------------------
A user who launches Monitra while it is already running is almost always
*looking for it*: the window is hidden in the tray, minimised, on another
virtual desktop, or -- the case that made this matter -- blocked by an idle
popup they cannot see. Telling them "it is already running" and exiting
leaves them exactly where they were. So the second process asks the first to
show itself (`request_activation`), and exits quietly once the first has
taken the request; the explanation is shown only when the first did not
answer, which is itself useful information (it is hung, or still starting).

The channel is a request file in `<data dir>/activation/`, watched by the
lock holder with a `QFileSystemWatcher` (`ActivationListener`). A file,
rather than a socket or a Windows message, because it is the same on every
platform, needs no Qt module the packaged build leaves out (QtNetwork is
excluded), and is owned by the lock: only the process holding the lock
listens, so only it ever removes a request -- a stale request left by a
crash is cleared by the next lock holder at startup, never by a second
instance guessing. Taking the request *is* the acknowledgement: the
requester waits, bounded, for its file to disappear.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from core.logging_setup import get_logger

log = get_logger("single-instance")

#: Where a second launch leaves its request, under the data directory the
#: lock guards -- so the scope of the channel is exactly the scope of the lock.
ACTIVATION_DIRNAME = "activation"
REQUEST_SUFFIX = ".request"
#: A request older than this at the moment the listener sees it was written
#: for an instance that is no longer waiting (a crash between write and
#: consume, or a listener that started late): removed, not acted on.
REQUEST_MAX_AGE_SECONDS = 10.0
#: How long the second launch waits for the first to take its request. A
#: responsive instance answers in well under a second; the ceiling exists so
#: a hung one does not hold the user at a blank screen.
ACTIVATION_TIMEOUT_SECONDS = 3.0

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


def already_running_message(unanswered: bool = False) -> str:
    """User-facing explanation for the second instance, before it exits.

    Shown only when the running instance did not take the activation request
    (`unanswered`), or on a platform where asking was not possible.
    """
    asked = (
        "Monitra asked the running copy to bring its window back, but it did "
        "not answer.\n\n"
    ) if unanswered else ""
    return (
        "Monitra is already running.\n\n" + asked +
        "Closing the Monitra window leaves it running in the background so "
        "your time keeps being tracked. Look for the Monitra icon in the "
        "notification area (Windows) or the menu bar (macOS) and click it to "
        "bring the window back."
    )


# ── Activation: the second launch asks the first to show itself ──────────────

def activation_dir() -> Path:
    """The directory the lock holder watches. Created on demand."""
    from core.paths import data_dir

    path = data_dir() / ACTIVATION_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


class ActivationListener(QObject):
    """The lock holder's side of the channel. GUI thread; no thread of its own.

    Signals:
        activation_requested() — another launch asked this instance to show
                                 its window. Emitted once per batch of
                                 requests, after they have been taken.
    """

    activation_requested = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._dir = activation_dir()
        # Whatever is there now was written for a process that is not
        # waiting any more, unless it is fresh -- a launch that raced this
        # start-up. We hold the lock, so these files are ours to clear. Done
        # from the event loop, not here, so the owner has connected to the
        # signal by the time a fresh one is honoured.
        QTimer.singleShot(0, self, self._drain)
        self._watcher = QFileSystemWatcher(self)
        if not self._watcher.addPath(str(self._dir)):
            log.warning("SINGLE_INSTANCE_ACTIVATION cannot watch the request directory; a second launch will be told to use the tray")
        self._watcher.directoryChanged.connect(self._on_directory_changed)

    def _on_directory_changed(self, _path: str) -> None:
        self._drain()

    def _drain(self) -> None:
        live = 0
        now = time.time()
        for path in sorted(self._dir.glob("*" + REQUEST_SUFFIX)):
            try:
                age = now - path.stat().st_mtime
                path.unlink()
            except OSError:
                continue  # withdrawn by its requester between the listing and here
            if age <= REQUEST_MAX_AGE_SECONDS:
                live += 1
            else:
                log.info("SINGLE_INSTANCE_ACTIVATION stale request removed age=%.0fs", age)
        if live:
            log.info("SINGLE_INSTANCE_ACTIVATION received requests=%d", live)
            self.activation_requested.emit()


def request_activation(timeout: float = ACTIVATION_TIMEOUT_SECONDS) -> bool:
    """Ask the running instance to show its window. True once it has taken
    the request; False if it did not within `timeout`. Never raises.

    The second launch is in the foreground (the user just started it), so on
    Windows it first lends that right to any process, which is what lets the
    running instance's `activateWindow()` actually come to the front.
    """
    try:
        _allow_running_instance_to_take_foreground()
        directory = activation_dir()
        name = f"activate-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        pending = directory / (name + ".tmp")
        request = directory / (name + REQUEST_SUFFIX)
        pending.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8")
        pending.replace(request)  # the listener only ever sees a complete file
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not request.exists():
                return True
            time.sleep(0.05)
        # Unanswered: withdraw it, so a listener that starts later does not
        # act on a request nobody is waiting for.
        try:
            request.unlink()
        except FileNotFoundError:
            return True  # taken at the last moment
        except OSError:
            pass
        return False
    except Exception:  # noqa: BLE001
        log.warning("SINGLE_INSTANCE_ACTIVATION could not be requested", exc_info=True)
        return False


def _allow_running_instance_to_take_foreground() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ASFW_ANY = ctypes.c_uint32(0xFFFFFFFF).value
        ctypes.WinDLL("user32", use_last_error=True).AllowSetForegroundWindow(ASFW_ANY)
    except Exception:  # noqa: BLE001
        pass  # cosmetic: the window still comes back, possibly behind this one


def describe() -> Optional[str]:
    """Return how the lock is held, for the startup log."""
    if _handle is None:
        return None
    return "windows-mutex" if sys.platform == "win32" else "posix-flock"
