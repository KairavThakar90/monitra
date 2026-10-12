"""
os_toast — will the operating system actually put a platform toast on screen?

`NotificationService` hands most notifications to the platform (a Windows toast,
a macOS banner). `QSystemTrayIcon.showMessage` returns nothing and raises
nothing whether the toast was drawn or thrown away, so before this module a
user whose Windows had notifications switched off for Monitra -- or off for the
whole account, or blocked by a group policy -- simply never saw a single
reminder, timer message or update notice, and nothing in the log said why. The
application believed it had notified them.

Three settings, all of them the user's or an administrator's own explicit
choice, decide that on Windows, and all of them can be read without asking the
shell anything:

  * **"Get notifications from apps and other senders"**, the account-wide switch
    (`PushNotifications\\ToastEnabled`);
  * **Monitra's own entry** under Settings -> System -> Notifications
    (`Notifications\\Settings\\<app id>`): `Enabled` is its master switch and
    `ShowBanner` the "Show notification banners" toggle -- with banners off the
    toast goes to the Action Center only and is never on screen;
  * **the "Turn off toast notifications" policy**
    (`PushNotifications\\NoToastApplicationNotification`, per user or per
    machine).

When any of them is off the service shows the application's own card instead,
which no setting of the operating system can swallow, and writes one log line
naming the reason (`NOTIFICATION_OS_SUPPRESSED`). Where nothing is known -- not
Windows, a key that is not there, a registry that cannot be read -- the answer is
"not suppressed": the platform toast stays the first choice, and a guess never
takes it away.

What this deliberately does **not** do is override Windows' *Focus assist* / *Do
not disturb*, a full-screen game or a presentation (`SHQueryUserNotificationState`).
Those are the user asking the system to hold notifications for now; the toast
still lands in the Action Center, and drawing a card over the top of a
presentation would be exactly the interruption they asked to be spared. The state
is reported by `quiet_state()` for the log, not acted upon.

Read-only: nothing here writes to the registry.
"""
from __future__ import annotations

import sys
from typing import Optional

#: Registry hives and paths. Spelled out so a reader can find them in regedit.
_PUSH_KEY = r"Software\Microsoft\Windows\CurrentVersion\PushNotifications"
_APP_SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"
_POLICY_KEY = r"Software\Policies\Microsoft\Windows\CurrentVersion\PushNotifications"

#: `SHQueryUserNotificationState` results, by name, for the log.
_QUIET_STATES = {
    1: "not present (screen saver, locked, or switched user)",
    2: "busy (a full-screen application)",
    3: "a full-screen Direct3D application",
    4: "presentation mode",
    6: "quiet time (Focus assist / Do not disturb)",
    7: "a full-screen Windows Store application",
}


def _read_dword(root: str, subkey: str, value: str) -> Optional[int]:
    """One DWORD from `HKCU` (`root` = "user") or `HKLM` ("machine"), or None
    if the key or value is absent or cannot be read. The single place the
    registry is touched, so a test can stand in for it."""
    try:
        import winreg

        hive = winreg.HKEY_CURRENT_USER if root == "user" else winreg.HKEY_LOCAL_MACHINE
        with winreg.OpenKey(hive, subkey) as key:
            data, kind = winreg.QueryValueEx(key, value)
        return int(data) if kind == winreg.REG_DWORD else None
    except (OSError, ImportError, ValueError):
        return None


def suppression_reason(app_id: str) -> Optional[str]:
    """Why Windows would not show a toast from `app_id` on screen, in words a
    person can act on -- or None when it would (or when this is not Windows, or
    nothing can be read, which is "no known reason" rather than "fine")."""
    if sys.platform != "win32":
        return None
    try:
        if _read_dword("user", _PUSH_KEY, "ToastEnabled") == 0:
            return "Windows notifications are switched off for this account"
        for root in ("user", "machine"):
            if _read_dword(root, _POLICY_KEY, "NoToastApplicationNotification") == 1:
                return "a Windows policy turns toast notifications off"
        app_key = f"{_APP_SETTINGS_KEY}\\{app_id}"
        if _read_dword("user", app_key, "Enabled") == 0:
            return "notifications from Monitra are switched off in Windows settings"
        if _read_dword("user", app_key, "ShowBanner") == 0:
            return "Monitra's notification banners are switched off in Windows settings"
    except Exception:  # noqa: BLE001 -- a diagnostic must never break delivery
        return None
    return None


def quiet_state() -> Optional[str]:
    """What Windows says about interrupting the user right now, when it says
    "not now": quiet time, a full-screen application, a presentation. None when
    notifications are accepted, or on anything but Windows. For the log only --
    see the module docstring for why nothing acts on it."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return None
        return _QUIET_STATES.get(state.value)
    except Exception:  # noqa: BLE001
        return None
