"""
mac_window — making a notification card genuinely passive on macOS.

Why this exists
---------------
On Windows a Qt window shown with `WA_ShowWithoutActivating` is passive: it
appears, it takes no focus, and clicking its close button does not activate the
application. On macOS three separate things each break that, and they all show
up as the same complaint -- "a Monitra notification appeared while I was
typing in another app and Monitra became the active application":

1. **`QWidget.raise_()` activates the whole application.** Qt's Cocoa
   `QCocoaWindow::raise()` orders the window front and then calls
   `[NSApp activateIgnoringOtherApps:YES]` (unless the process was started with
   `QT_MAC_SET_RAISE_PROCESS=0`). `ToastPopup.present()` called `raise_()`
   after `show()`, and `NotificationService._restack()` called it again on
   every card whenever the stack changed. A card appearing therefore pulled
   Monitra -- and its keyboard focus -- in front of whatever the user was in.
2. **A `Qt.Tool` window is an activating `NSPanel` that hides when the app is
   inactive.** Clicking anything in it (the close button included) makes
   Monitra the active application, and `hidesOnDeactivate` means the card is
   not even shown while Monitra is in the background -- which is why the
   `raise_()` above was "needed": it made the card visible by activating the
   app.
3. **Keyboard focus.** An ordinary panel can become the key window, which is
   what moves the text cursor away from the user's document.

The fixes are these helpers, used by `ToastPopup` on macOS only:

- `make_passive` -- a *non-activating* panel that does not hide on deactivate,
  floats, and may appear on every Space and over full-screen apps.
- `order_front_without_activating` -- `orderFrontRegardless`, which brings the
  window to the front of the screen without making its application active.
  This is what replaces `raise_()` on macOS.

Everything is done through pyobjc, which is already a macOS requirement of this
application (`tracking/active_window.py`). Nothing in this module runs on any
other platform, and every call is guarded: if the native window cannot be
configured, `make_passive` says so and the caller falls back to the platform's
own notification banner (which is non-activating by nature) instead of showing
a card that would steal focus -- or one that would never be visible.
"""
from __future__ import annotations

import sys
from typing import Any, Optional

from core.logging_setup import get_logger

log = get_logger("notifications.mac_window")

# AppKit constants, spelled out so the values are reviewable here and the
# helpers can be exercised without AppKit. They are the documented values of
# NSWindowStyleMaskNonactivatingPanel, NSWindowCollectionBehaviorCanJoinAllSpaces
# and NSWindowCollectionBehaviorFullScreenAuxiliary.
STYLE_MASK_NONACTIVATING_PANEL = 1 << 7
COLLECTION_CAN_JOIN_ALL_SPACES = 1 << 0
COLLECTION_FULL_SCREEN_AUXILIARY = 1 << 8


def is_macos() -> bool:
    """True only when Qt is really drawing through Cocoa.

    Not merely `sys.platform == "darwin"`: the test suite runs on macOS under
    Qt's `offscreen` platform, where `winId()` is a synthetic number and not an
    `NSView*`, and treating it as one would crash the interpreter. There is no
    native window to configure there, and the ordinary path is the right one.
    """
    if sys.platform != "darwin":
        return False
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.platformName() == "cocoa"


def _ns_window(widget) -> Optional[Any]:
    """The `NSWindow` behind a top-level Qt widget, or None.

    `winId()` of a top-level widget is its `NSView*`; creating it is what
    gives the widget a native window to configure before it is first shown.
    """
    import ctypes

    import objc  # type: ignore

    view = objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
    return view.window()


def make_passive(widget) -> bool:
    """
    Configure `widget`'s native window so showing it, and clicking it, never
    activates this application or takes keyboard focus.

    Idempotent: safe to call before every show.

    :return: True if the window is configured. False if it could not be, in
        which case the widget must not be used as a notification surface.
    """
    if not is_macos():
        return False
    step = "find the native window"   # which call failed, for the log only
    try:
        window = _ns_window(widget)
        if window is None:
            log.warning("notification card has no native window to configure")
            return False

        step = "import AppKit"
        from AppKit import NSPanel  # type: ignore

        if window.isKindOfClass_(NSPanel):
            step = "style mask / panel flags"
            window.setStyleMask_(window.styleMask() | STYLE_MASK_NONACTIVATING_PANEL)
            window.setFloatingPanel_(True)
            window.setBecomesKeyOnlyIfNeeded_(True)
        # A Qt.Tool panel hides while its application is inactive, and a
        # notification is shown precisely while another application is.
        step = "hidesOnDeactivate"
        window.setHidesOnDeactivate_(False)
        step = "collection behavior"
        window.setCollectionBehavior_(
            window.collectionBehavior()
            | COLLECTION_CAN_JOIN_ALL_SPACES
            | COLLECTION_FULL_SCREEN_AUXILIARY
        )
        return True
    except Exception:  # noqa: BLE001 - pyobjc missing, or AppKit refusing a call
        log.exception("could not make the notification card passive (failed at: %s)", step)
        return False


def order_front_without_activating(widget) -> bool:
    """
    Bring `widget` to the front of the screen without activating the
    application. The macOS replacement for `QWidget.raise_()`, which activates.

    :return: True if the window was ordered front.
    """
    if not is_macos():
        return False
    try:
        window = _ns_window(widget)
        if window is None:
            log.warning("notification card has no native window to order front")
            return False
        window.orderFrontRegardless()
        return True
    except Exception:  # noqa: BLE001
        log.exception("could not order the notification card to the front")
        return False


# ── Diagnostics (logging only) ────────────────────────────────────────────────

def describe(widget) -> str:
    """
    One line describing the native window behind a card, and whether Monitra is
    the active application -- the facts that say why a card is, or is not,
    visible and whether showing it activated anything. No content.

    Never raises: a failure is reported in the line itself.
    """
    if not is_macos():
        return "not-macos"
    try:
        window = _ns_window(widget)
        if window is None:
            return "ns_window=None"
        frame = window.frame()
        facts = {
            "class": str(window.className()),
            "style_mask": int(window.styleMask()),
            "level": int(window.level()),
            "visible": bool(window.isVisible()),
            "occlusion": int(window.occlusionState()),   # bit 2 = visible
            "on_active_space": bool(window.isOnActiveSpace()),
            "alpha": round(float(window.alphaValue()), 2),
            "hides_on_deactivate": bool(window.hidesOnDeactivate()),
            "can_become_key": bool(window.canBecomeKeyWindow()),
            "is_key": bool(window.isKeyWindow()),
            "collection": int(window.collectionBehavior()),
            "frame": "{:.0f}x{:.0f}@{:.0f},{:.0f}".format(
                frame.size.width, frame.size.height, frame.origin.x, frame.origin.y,
            ),
        }
        try:
            from AppKit import NSApplication  # type: ignore

            facts["app_active"] = bool(NSApplication.sharedApplication().isActive())
        except Exception as exc:  # noqa: BLE001
            facts["app_active"] = f"unavailable:{type(exc).__name__}"
        return " ".join(f"{key}={value}" for key, value in facts.items())
    except Exception as exc:  # noqa: BLE001
        return f"describe_failed:{type(exc).__name__}"


def log_card_state(widget, stage: str, **extra) -> None:
    """Log `describe(widget)` under a stage name. macOS only; never raises."""
    if not is_macos():
        return
    try:
        more = "".join(f" {key}={value}" for key, value in extra.items())
        log.info("MACDIAG_CARD stage=%s%s %s", stage, more, describe(widget))
    except Exception:  # noqa: BLE001
        pass
