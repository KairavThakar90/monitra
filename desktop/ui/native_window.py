"""
ui.native_window — ask the operating system, not Qt, what state a window is in.

`QWidget.isVisible()` is Qt's *intention* for a window, not the screen's
state. The two disagree exactly when it matters: Windows hides an owned
window with its minimised owner, Show Desktop minimises every top-level
window including a frameless stays-on-top one, a monitor that is unplugged
leaves a window at coordinates no screen covers, and a window created while
the user was on another *virtual desktop* stays on that desktop when they
switch away -- `IsWindowVisible` is true for it, nothing is minimised, and
yet nothing is on screen. For an application-modal popup every one of those
is the same failure: Monitra stops taking input and the one window that
could be answered cannot be found. (Measured on Windows 11 with
`tests/e2e/real_window_idle_popup_probe.py`.)

So the idle popup asks here before deciding it is fine. Everything in this
module is a cheap, synchronous read on the GUI thread: pure ctypes on
Windows, `None` ("cannot say") elsewhere. No thread, no timer, no state
beyond one cached COM interface. Nothing here moves or shows a window; the
caller decides what to do with the answer.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QRect
from PySide6.QtGui import QCursor, QGuiApplication, QScreen

from core.logging_setup import get_logger

log = get_logger("ui.native_window")


@dataclass(frozen=True)
class WindowProbe:
    """One reading of a top-level window's state, Qt's view beside the OS's.

    A `None` means the operating system could not be asked (not Windows, or
    the API refused) -- never that the answer is "no".
    """

    qt_visible: bool
    qt_minimised: bool
    on_a_screen: bool
    os_shown: Optional[bool] = None
    os_minimised: Optional[bool] = None
    on_current_desktop: Optional[bool] = None

    def describe(self) -> str:
        """Compact, log-safe: states only, no title, no text."""
        return (
            f"qt_visible={self.qt_visible} qt_minimised={self.qt_minimised} "
            f"on_screen={self.on_a_screen} os_shown={self.os_shown} "
            f"os_minimised={self.os_minimised} on_current_desktop={self.on_current_desktop}"
        )


def probe(widget) -> WindowProbe:
    """Read `widget`'s window state. Never raises."""
    hwnd: Optional[int] = None
    if sys.platform == "win32":
        try:
            hwnd = int(widget.winId())
        except Exception:  # noqa: BLE001 - a widget without a native window yet
            hwnd = None
    return WindowProbe(
        qt_visible=bool(widget.isVisible()),
        qt_minimised=bool(widget.isMinimized()),
        on_a_screen=rect_is_on_a_screen(widget.frameGeometry()),
        os_shown=is_shown_by_os(hwnd),
        os_minimised=is_minimised_by_os(hwnd),
        on_current_desktop=is_on_current_virtual_desktop(hwnd),
    )


def rect_is_on_a_screen(rect: QRect) -> bool:
    """Whether any connected display shows at least part of `rect`.

    Intersection, not containment: a window the user left half off the
    edge is still reachable. A host with no screen at all (headless) says
    True, because "off every screen" is then meaningless.
    """
    screens = QGuiApplication.screens()
    if not screens:
        return True
    return any(screen.availableGeometry().intersects(rect) for screen in screens)


def screen_for_recovery() -> Optional[QScreen]:
    """Where a window that has lost its screen should be put back.

    The screen under the pointer -- where the user is -- else the primary.
    """
    try:
        screen = QGuiApplication.screenAt(QCursor.pos())
    except Exception:  # noqa: BLE001
        screen = None
    return screen or QGuiApplication.primaryScreen()


def describe_displays() -> str:
    """The display configuration, for a log line. Geometry and scale only."""
    screens = QGuiApplication.screens()
    parts = []
    for screen in screens:
        g = screen.geometry()
        primary = "*" if screen is QGuiApplication.primaryScreen() else ""
        parts.append(f"{g.width()}x{g.height()}@{g.x()},{g.y()}x{screen.devicePixelRatio():g}{primary}")
    return f"screens={len(screens)} [{' '.join(parts)}]"


# ── Windows ──────────────────────────────────────────────────────────────────

_user32 = None
_desktop_manager = None  # the COM interface, False once it is known to be unavailable


def _win_user32():
    global _user32
    if _user32 is None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        _user32 = user32
    return _user32


def is_shown_by_os(hwnd: Optional[int]) -> Optional[bool]:
    """Windows' own `IsWindowVisible`: the WS_VISIBLE style, which the OS
    clears for a hidden window whatever Qt believes."""
    if sys.platform != "win32" or not hwnd:
        return None
    try:
        return bool(_win_user32().IsWindowVisible(hwnd))
    except Exception:  # noqa: BLE001
        return None


def is_minimised_by_os(hwnd: Optional[int]) -> Optional[bool]:
    if sys.platform != "win32" or not hwnd:
        return None
    try:
        return bool(_win_user32().IsIconic(hwnd))
    except Exception:  # noqa: BLE001
        return None


def is_on_current_virtual_desktop(hwnd: Optional[int]) -> Optional[bool]:
    """Whether the window is on the virtual desktop the user is looking at.

    `IVirtualDesktopManager` (shell32, documented, Windows 10+). A window on
    another desktop is neither hidden nor minimised to every other API, and
    it is not on screen. Unavailable (an old Windows, a shell that refused):
    `None`, decided once and not asked again every second.
    """
    if sys.platform != "win32" or not hwnd:
        return None
    manager = _virtual_desktop_manager()
    if not manager:
        return None
    import ctypes

    flag = ctypes.c_int(0)
    try:
        hr = manager.is_on_current(manager.ptr, ctypes.c_void_p(hwnd), ctypes.byref(flag))
    except Exception:  # noqa: BLE001
        return None
    if hr != 0:
        return None  # a window the shell will not place (hidden, or being created)
    return bool(flag.value)


def move_to_current_virtual_desktop(hwnd: Optional[int], reference_hwnds) -> bool:
    """Move `hwnd` to the virtual desktop the user is looking at. True if done.

    `IVirtualDesktopManager` can only *name* a desktop through a window that
    is on it, so the current desktop is taken from the first of
    `reference_hwnds` the shell confirms is current -- the application's main
    window when the user has come back to it, else whatever window is in the
    foreground. When no reference qualifies (the foreground is the shell
    itself) nothing is moved and False is returned; the caller tries again
    later. Re-showing the window, or activating it, is not an alternative:
    measured, a shown window keeps its desktop and an activated one *switches
    the user* to its desktop instead.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    manager = _virtual_desktop_manager()
    if not manager:
        return False
    import ctypes

    for reference in list(reference_hwnds) + [_foreground_window()]:
        if not reference or reference == hwnd:
            continue
        flag = ctypes.c_int(0)
        try:
            if manager.is_on_current(manager.ptr, ctypes.c_void_p(reference), ctypes.byref(flag)) != 0 or not flag.value:
                continue
            desktop = manager.GUID()
            if manager.desktop_of(manager.ptr, ctypes.c_void_p(reference), ctypes.byref(desktop)) != 0:
                continue
            hr = manager.move_to(manager.ptr, ctypes.c_void_p(hwnd), ctypes.byref(desktop))
        except Exception:  # noqa: BLE001
            return False
        if hr == 0:
            return True
        log.info("MoveWindowToDesktop refused: hr=%#x", hr & 0xFFFFFFFF)
        return False
    return False


def _foreground_window() -> Optional[int]:
    try:
        import ctypes

        user32 = _win_user32()
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        return user32.GetForegroundWindow() or None
    except Exception:  # noqa: BLE001
        return None


class _DesktopManager:
    """The one COM pointer, with its methods bound. Apartment-threaded:
    created and used on the GUI thread only."""

    def __init__(self, ptr, GUID, is_on_current, desktop_of, move_to) -> None:  # noqa: N803
        self.ptr = ptr
        self.GUID = GUID
        self.is_on_current = is_on_current
        self.desktop_of = desktop_of
        self.move_to = move_to


def _virtual_desktop_manager():
    global _desktop_manager
    if _desktop_manager is not None:
        return _desktop_manager  # the interface, or False
    try:
        _desktop_manager = _create_virtual_desktop_manager()
    except Exception as exc:  # noqa: BLE001
        log.info("virtual desktop state is not available here: %s", exc)
        _desktop_manager = False
    return _desktop_manager


def _create_virtual_desktop_manager():
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def of(cls, text: str) -> "GUID":
            value = uuid.UUID(text)
            guid = cls()
            guid.Data1, guid.Data2, guid.Data3 = value.time_low, value.time_mid, value.time_hi_version
            for index, byte in enumerate(value.bytes[8:]):
                guid.Data4[index] = byte
            return guid

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    COINIT_APARTMENTTHREADED = 0x2
    RPC_E_CHANGED_MODE = -2147417850  # 0x80010106: COM is already up in another mode -- usable
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr not in (0, 1, RPC_E_CHANGED_MODE):  # S_OK, S_FALSE
        raise OSError(hr, "CoInitializeEx failed")

    clsid = GUID.of("aa509086-5ca9-4c25-8f95-589d3c07b48a")  # CLSID_VirtualDesktopManager
    iid = GUID.of("a5cd92ff-29be-454c-8d04-d82879fb3f1b")    # IID_IVirtualDesktopManager
    CLSCTX_INPROC_SERVER = 0x1
    ptr = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER, ctypes.byref(iid), ctypes.byref(ptr))
    if hr != 0 or not ptr.value:
        raise OSError(hr, "CoCreateInstance(VirtualDesktopManager) failed")

    # IVirtualDesktopManager vtable: IUnknown (0-2), IsWindowOnCurrentVirtualDesktop (3),
    # GetWindowDesktopId (4), MoveWindowToDesktop (5).
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    HWND, HRESULT = ctypes.c_void_p, ctypes.c_long
    is_on_current = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, HWND, ctypes.POINTER(ctypes.c_int))(vtable[3])
    desktop_of = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, HWND, ctypes.POINTER(GUID))(vtable[4])
    move_to = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, HWND, ctypes.POINTER(GUID))(vtable[5])
    return _DesktopManager(ptr, GUID, is_on_current, desktop_of, move_to)


def reset_for_tests() -> None:
    """Forget the cached COM interface (tests that fake the platform)."""
    global _desktop_manager, _user32
    _desktop_manager = None
    _user32 = None
