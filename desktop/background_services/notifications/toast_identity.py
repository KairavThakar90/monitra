"""
toast_identity — Monitra's own name and logo at the top of every Windows notification.

A Windows toast is headed by the name and small icon of the application that raised it, and
for a program that has no packaged identity Windows finds them in one of two ways -- which
is what this module settles, because they do not behave alike:

* **Left to itself**, Windows invents an identity for a tray icon
  (`NotifyIconGeneratedAumid_<hash>`) and fills it in from the program's file description and
  tray icon. That is how a Monitra notification came to be headed "Monitra — Staff Management"
  beside the Monitra logo. But a notification asked to carry **its own icon** -- the Monitra
  circle in place of Windows' blue "i" -- is attributed to the process's *explicit* identity
  (`SetCurrentProcessExplicitAppUserModelID`) instead, and an explicit identity nobody has
  described is shown as its bare id: "Monitra", with no icon. Putting the circle on the toast
  lost the header, measured on a real Windows 11 desktop (2026-10-12).
* **Registered**, an identity is described once, under
  `HKCU\\Software\\Classes\\AppUserModelId\\<id>`: a `DisplayName` and an `IconUri`. That is
  the documented way for a desktop program to name itself in a toast (Firefox does exactly
  this), it needs no administrator rights, and whichever of the two paths Windows takes the
  header reads the same -- the Monitra mark and "Monitra — Staff Management" -- while the
  body carries the circle.

`register` writes that description at start-up, idempotently (nothing is written when it is
already right), and never raises: a locked-down machine where the key cannot be written keeps
its notifications, with the header reduced to "Monitra".

Two details that are not obvious and both cost an experiment:

* `IconUri` must be a **backslashed** Windows path. The same file given as `C:/Users/...` is
  accepted without complaint and no icon is drawn.
* The icon is a file, not a resource: it is rendered from the one definition of the mark
  (`core.branding`) into the user's data directory, so it is as current as the build and lives
  outside the install folder that an update replaces wholesale.
* It is an **.ico with a frame for each size the header can ask for**, drawn at that size and in
  the small-size form of the mark (`core.branding.logo_small_pixmap`: the M and its figure, without
  the orbit). The header icon is about 16-24 px, and a single large PNG left Windows to shrink it:
  the orbit's thin stroke became a smudge and the M a fuzzy blob (photographed on a real Windows 11
  toast, 2026-10-12, and compared at actual size against the alternatives: native frames of the full
  mark were still soft; the small-size mark was the one that read).

The installer removes the key on uninstall (`monitra.iss`); the program only ever writes it.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path
from typing import Dict, Optional

from core.logging_setup import get_logger

log = get_logger("notifications.identity")

#: Where Windows looks up what an application id looks like.
_AUMID_KEY = r"Software\Classes\AppUserModelId"

#: The file the header icon is read from, in the data directory.
ICON_FILE_NAME = "notification-icon.ico"

#: The file earlier builds wrote (a single 128 px PNG); removed when found.
_OLD_ICON_FILE_NAME = "notification-icon.png"

#: One frame for each size the header icon can be drawn at -- 16 px at 100% scaling, 20 at 125%,
#: 24 at 150%, 32 at 200%, and the larger ones for the Action Center and Settings.
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64)


def icon_path() -> Path:
    """Where the header icon is kept: the user's data directory, outside the install folder."""
    from core.paths import data_dir

    return data_dir() / ICON_FILE_NAME


def _png(pixmap) -> Optional[bytes]:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = pixmap.save(buffer, "PNG")
    buffer.close()
    return bytes(data) if ok else None


def ico_bytes(frames: Dict[int, bytes]) -> bytes:
    """An .ico holding one PNG-compressed frame per size (accepted since Windows Vista):
    a 6-byte header, a 16-byte entry per frame, then the frames. Every size here is under 256,
    so each fits the single byte an entry gives it."""
    sizes = sorted(frames)
    header = struct.pack("<HHH", 0, 1, len(sizes))          # reserved, type = icon, count
    offset = len(header) + 16 * len(sizes)
    entries, payloads = b"", b""
    for size in sizes:
        payload = frames[size]
        entries += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(payload), offset)
        payloads += payload
        offset += len(payload)
    return header + entries + payloads


def _render_icon() -> Optional[bytes]:
    """The Monitra mark as an .ico with a frame for each of `ICON_SIZES`, each drawn at its own
    size in the small-size form of the mark, from the one definition of it. None if any frame
    could not be made: a header icon with missing sizes would be scaled by Windows, which is the
    softness this exists to avoid."""
    from core.branding import logo_small_pixmap

    frames: Dict[int, bytes] = {}
    for size in ICON_SIZES:
        pixmap = logo_small_pixmap(size)
        encoded = None if pixmap.isNull() else _png(pixmap)
        if encoded is None:
            return None
        frames[size] = encoded
    return ico_bytes(frames)


def _ensure_icon(path: Path) -> bool:
    """Make `path` hold the current mark. Written only if it differs, through a temporary
    file, so Windows never reads a half-written icon. False if it could not be done."""
    rendered = _render_icon()
    if rendered is None:
        return False
    try:
        if path.is_file() and path.read_bytes() == rendered:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".tmp")
        partial.write_bytes(rendered)
        os.replace(partial, path)
        # The single PNG earlier builds wrote is no longer referenced by anything.
        old = path.parent / _OLD_ICON_FILE_NAME
        if old != path:
            old.unlink(missing_ok=True)
        return True
    except OSError:
        log.exception("could not write the notification icon %s", path)
        return False


def _read_values(app_id: str) -> Dict[str, str]:
    """The string values under the id's key (empty if there is none). The one place the
    registry is read, so a test can stand in for it."""
    import winreg

    values: Dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{_AUMID_KEY}\\{app_id}") as key:
            index = 0
            while True:
                try:
                    name, data, kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(data, str):
                    values[name] = data
                index += 1
    except OSError:
        return {}
    return values


def _write_values(app_id: str, values: Dict[str, str]) -> None:
    """Set each value under the id's key, creating the key. The one place the registry is
    written, so a test can stand in for it."""
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, f"{_AUMID_KEY}\\{app_id}", 0, winreg.KEY_SET_VALUE,
    ) as key:
        for name, data in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, data)


def register(app_id: str, display_name: str, icon: Optional[Path] = None) -> bool:
    """Describe `app_id` to Windows: its `display_name` and the mark as its icon.

    :return: True if the description is in place afterwards (written now or already right);
        False on anything but Windows, or if it could not be written. Never raises.
    """
    if sys.platform != "win32":
        return False
    try:
        wanted = {"DisplayName": display_name}
        target = Path(os.path.normpath(str(icon if icon is not None else icon_path())))
        if _ensure_icon(target):
            # Backslashes, as `os.path.normpath` gives on Windows: see the module docstring.
            wanted["IconUri"] = str(target)
        current = _read_values(app_id)
        if all(current.get(name) == value for name, value in wanted.items()):
            return True
        _write_values(app_id, wanted)
        log.info("registered the notification identity %r as %r", app_id, display_name)
        return True
    except Exception:  # noqa: BLE001 -- a nicety for the header must never stop the application
        log.exception("could not register the notification identity %r", app_id)
        return False
