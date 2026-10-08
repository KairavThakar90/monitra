"""The name Windows puts at the top of a notification, when the app runs from source.

Windows prints, at the top of every notification, the *file description* of the
program that raised it. The installed build is stamped `NOTIFICATION_HEADER_NAME`
(`packaging/monitra.spec`); run with ``python main.py`` the program is the
interpreter, so every notification says "Python". This makes a source run say the
same thing the installed app does, for developers trying notifications:

* `prepare_runner` copies the interpreter to ``~/.monitra/dev-runner/Monitra.exe``
  (with the DLLs it needs) and stamps that copy's version resource. The real
  interpreter is never touched, nothing is installed and nothing is written to the
  registry (a registry name was tried: it changes nothing).
* `relaunch_if_needed` is called first thing by ``main.py``: on Windows, from source,
  it runs the app through that copy and hands back its exit code. Anywhere else, in
  an installed build, in the copy itself, with ``MONITRA_NO_DEV_RELAUNCH=1`` set, or
  if the copy cannot be prepared, it does nothing and the app runs in place.

Windows only: elsewhere a notification is named after the application bundle.

Quit a Monitra that is already running first -- there is one instance at a time.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import struct
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import List, Optional

from version import APP_NAME, NOTIFICATION_HEADER_NAME, VERSION

DESKTOP = Path(__file__).resolve().parent.parent

RUNNER_DIR = Path.home() / ".monitra" / "dev-runner"
RUNNER_NAME = f"{APP_NAME}.exe"

#: Files beside the interpreter it cannot start without.
_RUNTIME_PREFIXES = ("python3", "vcruntime", "libffi", "msvcp", "concrt")

_RT_VERSION = 16
_VS_VERSION_INFO = 1
_LANGUAGE = 0x0409
_CODEPAGE = 0x04B0


# ── The version resource ──────────────────────────────────────────────────────


def _align(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


def _node(key: str, value: bytes = b"", children=(), *, text: bool = False, value_length: Optional[int] = None) -> bytes:
    """One node of a VS_VERSIONINFO tree: length, value length, type, key, value, children."""
    out = struct.pack("<HHH", 0, len(value) if value_length is None else value_length, 1 if text else 0)
    out += (key + "\0").encode("utf-16-le")
    out = _align(out) + value
    for child in children:
        out = _align(out) + child
    return struct.pack("<H", len(out)) + out[2:]


def _string(name: str, value: str) -> bytes:
    return _node(name, (value + "\0").encode("utf-16-le"), text=True, value_length=len(value) + 1)


def _version_parts(version: str) -> tuple:
    parts = [int(p) if p.isdigit() else 0 for p in version.split(".")[:4]]
    return tuple(parts + [0] * (4 - len(parts)))


def build_version_resource(description: str, name: str = APP_NAME, version: str = VERSION) -> bytes:
    """A complete VS_VERSIONINFO resource whose FileDescription is `description`."""
    major, minor, patch, build = _version_parts(version)
    fixed = struct.pack(
        "<13I", 0xFEEF04BD, 0x00010000,
        (major << 16) | minor, (patch << 16) | build, (major << 16) | minor, (patch << 16) | build,
        0x3F, 0, 0x40004, 1, 0, 0, 0,
    )
    strings = [
        _string("CompanyName", name), _string("FileDescription", description), _string("FileVersion", version),
        _string("InternalName", name), _string("OriginalFilename", f"{name}.exe"),
        _string("ProductName", name), _string("ProductVersion", version),
    ]
    table = _node(f"{_LANGUAGE:04X}{_CODEPAGE:04X}", children=strings, text=True, value_length=0)
    string_info = _node("StringFileInfo", children=[table], text=True, value_length=0)
    translation = _node("Translation", struct.pack("<HH", _LANGUAGE, _CODEPAGE), value_length=4)
    var_info = _node("VarFileInfo", children=[translation], text=True, value_length=0)
    return _node("VS_VERSION_INFO", fixed, children=[string_info, var_info], value_length=len(fixed))


def stamp(executable: Path, description: str) -> None:
    """Write `description` into `executable`'s version resource (replacing it)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel32.UpdateResourceW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD, ctypes.c_void_p, wintypes.DWORD]
    kernel32.UpdateResourceW.restype = wintypes.BOOL
    kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    kernel32.EndUpdateResourceW.restype = wintypes.BOOL

    blob = build_version_resource(description)
    handle = kernel32.BeginUpdateResourceW(str(executable), False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(blob, len(blob))
    ok = kernel32.UpdateResourceW(handle, ctypes.c_void_p(_RT_VERSION), ctypes.c_void_p(_VS_VERSION_INFO), _LANGUAGE, buffer, len(blob))
    if not ok:
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.EndUpdateResourceW(handle, True)  # discard
        raise error
    if not kernel32.EndUpdateResourceW(handle, False):
        raise ctypes.WinError(ctypes.get_last_error())


def read_file_description(executable: Path) -> Optional[str]:
    """The FileDescription Windows would print for `executable`, or None."""
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    version.VerQueryValueW.restype = wintypes.BOOL

    size = version.GetFileVersionInfoSizeW(str(executable), None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(executable), 0, size, data):
        return None
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not version.VerQueryValueW(data, f"\\StringFileInfo\\{_LANGUAGE:04X}{_CODEPAGE:04X}\\FileDescription", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    return ctypes.wstring_at(pointer.value, max(0, length.value - 1))


# ── The runner ────────────────────────────────────────────────────────────────


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and int(a.stat().st_mtime) == int(b.stat().st_mtime)
    except OSError:
        return False


def prepare_runner(
    runner_dir: Path = RUNNER_DIR, interpreter: Optional[Path] = None, description: str = NOTIFICATION_HEADER_NAME,
) -> Path:
    """Make `runner_dir/Monitra.exe`: the interpreter, with its DLLs, stamped.

    Idempotent and cheap on a second run: files are copied only when they differ and
    the stamp is written only when it is wrong. Returns the path to the runner.
    """
    interpreter = interpreter or Path(getattr(sys, "_base_executable", sys.executable))
    runner_dir.mkdir(parents=True, exist_ok=True)
    runner = runner_dir / RUNNER_NAME

    # Stamping changes the copy's size, so "is it the same file?" cannot be asked of
    # the copy: what is remembered is which interpreter it was made from.
    source = interpreter.stat()
    origin = f"{interpreter}|{source.st_size}|{int(source.st_mtime)}"
    marker = runner_dir / ".source"
    copied = False
    if not runner.exists() or not marker.exists() or marker.read_text(encoding="utf-8") != origin:
        shutil.copy2(interpreter, runner)
        marker.write_text(origin, encoding="utf-8")
        copied = True
    for dll in interpreter.parent.glob("*.dll"):
        if dll.name.lower().startswith(_RUNTIME_PREFIXES) and not _same_file(dll, runner_dir / dll.name):
            shutil.copy2(dll, runner_dir / dll.name)
    if copied or read_file_description(runner) != description:
        stamp(runner, description)
    return runner


def runner_environment(extra_paths: Optional[List[str]] = None) -> dict:
    """The environment the stamped copy needs to find the real interpreter's library
    and everything installed for the current one (a virtualenv included)."""
    env = os.environ.copy()
    env["PYTHONHOME"] = sys.base_prefix
    paths = [str(DESKTOP)] + list(extra_paths or []) + [p for p in sys.path if p and Path(p).is_dir()]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
    return env


# ── Running the source through the stamped copy ──────────────────────────────

#: Set in the child, so it never relaunches itself.
RELAUNCHED_ENV = "MONITRA_DEV_RELAUNCHED"
#: Set by a developer to run in the plain interpreter (its notifications then say "Python").
DISABLE_ENV = "MONITRA_NO_DEV_RELAUNCH"


def should_relaunch() -> bool:
    """Whether this process should hand over to the stamped copy."""
    return (
        sys.platform == "win32"
        and not getattr(sys, "frozen", False)
        and not os.environ.get(RELAUNCHED_ENV)
        and not os.environ.get(DISABLE_ENV)
        and Path(sys.executable).name.lower() != RUNNER_NAME.lower()
    )


def relaunch_if_needed(script: Path, argv: Optional[List[str]] = None) -> Optional[int]:
    """Run `script` through the stamped copy if that is called for.

    :return: the exit code of the run, if it happened in the copy (the caller exits with
        it); None if the app should simply carry on in this process.
    """
    if not should_relaunch():
        return None
    try:
        runner = prepare_runner()
    except Exception as exc:  # noqa: BLE001 - a developer nicety must never stop the app starting
        print(f"Monitra: could not prepare the '{NOTIFICATION_HEADER_NAME}' runner ({exc}); running in place.", file=sys.stderr)
        return None

    env = runner_environment()
    env[RELAUNCHED_ENV] = "1"
    print(
        f"Monitra: running through {runner.name} so notifications are headed '{NOTIFICATION_HEADER_NAME}' "
        f"(set {DISABLE_ENV}=1 to run in the plain interpreter).",
        file=sys.stderr, flush=True,
    )
    try:
        child = subprocess.Popen([str(runner), str(script), *(sys.argv[1:] if argv is None else argv)], env=env)
    except OSError as exc:
        print(f"Monitra: could not start the runner ({exc}); running in place.", file=sys.stderr)
        return None
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            # Ctrl+C reaches the child too, and it shuts itself down; this process only
            # waits, so the second press of an impatient pair cannot cut the shutdown short.
            continue
