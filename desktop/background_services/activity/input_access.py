"""
activity.input_access — Will macOS let this process count keyboard and mouse events?

On macOS (10.15 and later) a global event tap is gated by the user's **Input
Monitoring** permission, and the gate does not fail loudly: `CGEventTapCreate`
returns None, nothing raises, and the counter simply never hears a key or a
click. Before this module that outcome was indistinguishable, on screen and in
the data, from a user who sat still all day -- the TODAY'S ACTIVITY card read
"Not tracking yet" while a timer ran, every window was left unmeasured, and
the only trace was one WARNING line in a log file the user never opens.

A denied permission is a state the application can name, so it has to be named:
to the user (what is wrong, where the switch is), to the log (which state, and
why), and to the card (so an absence of measurement is never mistaken for an
absence of work).

What this module answers
------------------------
`classify(tap_installed, tap_failure)` turns the counter's start result into
one of five states:

``NOT_REQUIRED``
    Any platform with no such gate (Windows, Linux) where counting started.

``GRANTED``
    macOS, and the tap is installed. The permission is demonstrably in effect.

``DENIED``
    macOS refused the tap and `CGPreflightListenEventAccess` says the
    permission is not granted (never asked, refused, or revoked). Apple's
    supported question; it does not prompt and installs nothing.

``RESTART_REQUIRED``
    macOS refused the tap although the preflight says the permission *is*
    granted. The grant arrived after this process started and has not been
    applied to it; macOS itself tells the user to quit and reopen the app in
    exactly this case.

``UNAVAILABLE``
    The counting backend itself is missing or failed -- Quartz could not be
    imported, pynput is not installed, a listener raised. Not a permission
    problem and not something the user can fix in System Settings.

What it does not do
-------------------
It never installs a tap, never prompts on its own (`request_input_access` is a
separate, explicit call, made once per process), and never tries to work around
the gate. Obtaining the permission is the user's decision in System Settings;
this module only detects its absence and lets the caller say so.

Thread-safety: `classify` is one ctypes call and is safe from the activity
service's thread. `request_input_access` shows a system prompt and is best
called once, from wherever the blocked state is first entered.

Modelled on `screenshot/screen_access.py`, which does the same job for the
Screen Recording permission, and deliberately shaped like it.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from core.logging_setup import get_logger

log = get_logger("activity.input_access")

#: Opens System Settings at Privacy & Security -> Input Monitoring. Handed to
#: `NotificationService` as the card's link, so clicking the notification goes
#: straight to the switch the user has to flip.
SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
)

_CORE_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"


class InputAccess(str, Enum):
    NOT_REQUIRED = "not_required"
    GRANTED = "granted"
    DENIED = "denied"
    RESTART_REQUIRED = "restart_required"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class InputAccessStatus:
    state: InputAccess
    #: Short, log-only explanation. Never shown to the user.
    detail: str = ""

    @property
    def allowed(self) -> bool:
        """Whether keyboard/mouse events are being counted."""
        return self.state in (InputAccess.NOT_REQUIRED, InputAccess.GRANTED)


#: `CGRequestListenEventAccess` is asked at most once per process. macOS shows
#: its prompt only the first time for a given install anyway; asking again would
#: do nothing but add a call.
_prompt_requested = False


def _is_macos() -> bool:
    return sys.platform == "darwin"


def required() -> bool:
    """Whether this platform gates input counting behind a user permission."""
    return _is_macos()


def _preflight() -> Optional[bool]:
    """
    `CGPreflightListenEventAccess()`: True, False, or None if this OS has no
    such call (macOS older than 10.15, which has no gate at all).

    Raises on a failure to load CoreGraphics; the caller treats that as
    "unknown", not as a denial.
    """
    import ctypes

    core = ctypes.cdll.LoadLibrary(_CORE_GRAPHICS)
    function = getattr(core, "CGPreflightListenEventAccess", None)
    if function is None:
        return None
    function.argtypes = []
    function.restype = ctypes.c_bool
    return bool(function())


def _request() -> bool:
    import ctypes

    core = ctypes.cdll.LoadLibrary(_CORE_GRAPHICS)
    function = getattr(core, "CGRequestListenEventAccess", None)
    if function is None:
        return True
    function.argtypes = []
    function.restype = ctypes.c_bool
    return bool(function())


def preflight() -> Optional[bool]:
    """
    Whether macOS says this process may monitor input. None when the OS cannot
    say: not macOS, a macOS without the gate, or CoreGraphics would not load.
    Never raises, never prompts.
    """
    if not _is_macos():
        return None
    try:
        return _preflight()
    except Exception:  # noqa: BLE001
        log.exception("CGPreflightListenEventAccess could not be called")
        return None


def classify(tap_installed: bool, tap_failure: Optional[str]) -> InputAccessStatus:
    """
    What the counter's start result means.

    `tap_installed` is what `InputEventCounter.start()` answered;
    `tap_failure` is the counter's recorded reason when it answered False
    (`mac_input_tap.FAILURE_*`, or the counter's own pynput reasons). Cheap:
    at most one ctypes call, and only on macOS, and only on a failure.
    """
    if tap_installed:
        return InputAccessStatus(
            InputAccess.GRANTED if _is_macos() else InputAccess.NOT_REQUIRED,
            "input events are being counted",
        )

    if not _is_macos():
        return InputAccessStatus(
            InputAccess.UNAVAILABLE, tap_failure or "input listeners did not start"
        )

    if tap_failure == "quartz_unavailable":
        return InputAccessStatus(
            InputAccess.UNAVAILABLE, "pyobjc-framework-Quartz is not importable"
        )

    granted = preflight()
    if granted is True:
        return InputAccessStatus(
            InputAccess.RESTART_REQUIRED,
            "CGPreflightListenEventAccess=True, but the event tap could not be "
            "created; the grant has not been applied to this process",
        )
    if granted is False:
        return InputAccessStatus(InputAccess.DENIED, "CGPreflightListenEventAccess=False")
    return InputAccessStatus(
        InputAccess.DENIED,
        f"preflight unavailable and the event tap could not be created ({tap_failure})",
    )


def request_input_access() -> bool:
    """
    Ask macOS to show its Input Monitoring prompt, once per process.

    This is the supported way to get Monitra into the Input Monitoring list and
    in front of the user, and it grants nothing by itself: the user decides.
    Returns whether access is granted *now* (it will not be on the first ask).
    Never raises.
    """
    global _prompt_requested

    if not _is_macos() or _prompt_requested:
        return False
    _prompt_requested = True
    try:
        granted = _request()
        log.info("Input Monitoring permission requested from macOS (granted now: %s)", granted)
        return granted
    except Exception:  # noqa: BLE001
        log.exception("CGRequestListenEventAccess could not be called")
        return False


def guidance(state: InputAccess) -> Tuple[str, str]:
    """`(title, message)` telling the user what is wrong and what to do about it."""
    if state is InputAccess.RESTART_REQUIRED:
        return (
            "Restart Monitra to resume activity tracking",
            "Input Monitoring is on for Monitra, but macOS only applies it to an "
            "app once it has been reopened. Quit and reopen Monitra. Keyboard "
            "and mouse activity is not being measured until then; your timer is "
            "not affected.",
        )
    if state is InputAccess.UNAVAILABLE:
        return (
            "Activity tracking is unavailable",
            "Monitra could not start keyboard and mouse counting on this "
            "computer, so today's activity cannot be measured. Your timer is not "
            "affected. Please report this so it can be looked into.",
        )
    return (
        "Activity tracking needs a permission",
        "Monitra needs Input Monitoring permission to measure keyboard and "
        "mouse activity. Open System Settings > Privacy & Security > Input "
        "Monitoring and turn on Monitra. If activity does not resume within a "
        "minute, quit and reopen Monitra. Click here to open System Settings. "
        "Your timer is not affected.",
    )
