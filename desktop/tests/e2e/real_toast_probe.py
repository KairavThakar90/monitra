"""
Show real notifications on a real Windows desktop and report what happened.

Run by `test_native_toast_e2e.py` in a subprocess on the real platform plugin (the test
suite itself runs headless). Not a test: it prints one JSON line.

For each scenario it records what the notification service handed the platform and which
surface it used, and photographs the corner of the screen where a toast (or the card) lands.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

DESKTOP_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DESKTOP_ROOT))

from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

OUT = Path(sys.argv[1])
REGION_W, REGION_H = 520, 360


def grab():
    from PIL import ImageGrab

    image = ImageGrab.grab(all_screens=True)
    width, height = image.size
    return image.crop((width - REGION_W, height - REGION_H - 48, width, height - 48))   # above the taskbar


def changed_pixels(before, after) -> int:
    from PIL import ImageChops

    diff = ImageChops.difference(before.convert("RGB"), after.convert("RGB")).convert("L")
    return sum(1 for value in diff.getdata() if value > 24)


def _is_ico(path: Path) -> bool:
    return path.is_file() and path.read_bytes()[:4] == b"\x00\x00\x01\x00"


def settle(ms):
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.02)


def main() -> None:
    # The icon file goes to the photograph directory, and the notification identity is a throwaway
    # one that the test removes again: the probe must not touch the developer's real registration.
    os.environ["MONITRA_DATA_DIR"] = str(OUT)
    from background_services.notifications import notification_service as module, os_toast, toast_identity
    from background_services.notifications.notification_service import NotificationService

    module.APP_USER_MODEL_ID = os.environ.get("PROBE_APP_ID", module.APP_USER_MODEL_ID)
    APP_USER_MODEL_ID = module.APP_USER_MODEL_ID

    app = QApplication(sys.argv)
    service = NotificationService(SimpleNamespace())
    service.on_start()
    report = {
        "tray_available": QSystemTrayIcon.isSystemTrayAvailable(),
        "os_suppression": os_toast.suppression_reason(APP_USER_MODEL_ID),
        "quiet_state": os_toast.quiet_state(),
        "app_id": APP_USER_MODEL_ID,
        "identity": toast_identity._read_values(APP_USER_MODEL_ID),
        "icon_file_is_ico": _is_ico(OUT / toast_identity.ICON_FILE_NAME),
        "scenarios": {},
    }
    if service._tray is None:
        print(json.dumps(report))
        return

    handed = []
    real_show = service._tray.showMessage

    def spy(title, message, icon, ms):
        handed.append({"title": title, "message": message, "icon_is_qicon": isinstance(icon, QIcon),
                       "icon_null": isinstance(icon, QIcon) and icon.isNull(),
                       "icon_sizes": sorted(s.width() for s in icon.availableSizes()) if isinstance(icon, QIcon) else None})
        return real_show(title, message, icon, ms)

    service._tray.showMessage = spy
    settle(800)

    def scenario(name, title, message, key, shot):
        handed.clear()
        before = grab()
        ok = service.notify(message, "info", title=title, key=key, native=True)
        settle(3500)
        after = grab()
        after.save(OUT / f"{name}.png")
        report["scenarios"][name] = {
            "accepted": ok, "platform_toast": list(handed), "cards_up": len(service._cards),
            "changed_pixels": changed_pixels(before, after),
        }
        service._retire_current()
        for card in list(service._spare_cards):
            card.hide()
        settle(1500)

    scenario("short", "🧘 Take a Lunch Break", "Take your lunch break and give yourself a proper rest.", "probe-short", True)
    long_message = " ".join(f"word{i:03d}" for i in range(1, 45)) + " END-OF-MESSAGE"
    scenario("long", "Server restart", long_message, "probe-long", True)
    report["card_text_complete"] = None
    service.on_stop(1000)
    print(json.dumps(report))


main()
