"""A notification card must not take the foreground from the user's app (macOS).

The defect: a card appearing while the user typed in another application made
Monitra the active application (the text cursor moved; clicking the card's x
did the same). Qt's Cocoa `raise_()` activates the whole app, and a `Qt.Tool`
panel is an activating panel that hides while its app is inactive.

These tests drive `ToastPopup` and `NotificationService` with the *native*
calls faked, because no macOS is available to the suite. They prove the
contract this code has to keep -- never `raise_()` on macOS, always configure
the window as passive first, fall back to the platform banner if that fails,
and leave Windows exactly as it was. They cannot prove what AppKit then does
with the window; that is the manual checklist's job.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt

from background_services.notifications import mac_window
from background_services.notifications.notification_service import NotificationService
from background_services.notifications.toast_popup import ToastPopup


class _Native:
    """Stands in for the pyobjc calls and records them in order."""

    def __init__(self):
        self.calls = []
        self.passive_ok = True


@pytest.fixture
def native(monkeypatch):
    fake = _Native()

    def make_passive(widget):
        fake.calls.append("make_passive")
        return fake.passive_ok

    def order_front(widget):
        fake.calls.append("order_front")
        return True

    monkeypatch.setattr(mac_window, "is_macos", lambda: True)
    monkeypatch.setattr(mac_window, "make_passive", make_passive)
    monkeypatch.setattr(mac_window, "order_front_without_activating", order_front)
    return fake


@pytest.fixture
def raised(monkeypatch):
    """Every `raise_()` on a card -- the call that activates the app on macOS."""
    seen = []
    original = ToastPopup.raise_
    monkeypatch.setattr(ToastPopup, "raise_", lambda self: (seen.append(self), original(self)))
    return seen


@pytest.fixture
def service(qapp):
    svc = NotificationService(MagicMock())
    svc.NATIVE_BY_DEFAULT = False          # these tests are about the card, which is what native=False draws
    svc._available = True
    svc._tray = MagicMock()
    svc._icon = MagicMock()
    svc._icon.isNull.return_value = False
    yield svc
    svc._dismiss_timer.stop()
    svc.on_stop(1000)


class TestOnMacOS:
    def test_describing_a_card_without_a_native_window_cannot_crash(self, qapp, native):
        """With `is_macos()` faked true under the offscreen platform there is no
        NSView behind `winId()`; reading it as one segfaulted the macOS release
        runners inside `describe()` (via `log_card_state` on every show). The
        cast is refused unless Qt is really drawing through Cocoa."""
        card = ToastPopup()
        assert mac_window._ns_window(card) is None
        assert mac_window.describe(card) == "ns_window=None"
        mac_window.log_card_state(card, "after_show")   # must not raise either
        card.deleteLater()

    def test_the_card_refuses_key_window_status(self, qapp, native):
        card = ToastPopup()
        assert card.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        assert card.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        card.deleteLater()

    def test_showing_a_card_never_raises_it(self, service, native, raised):
        assert service.notify("Timer started", key="timer-started") is True

        assert raised == [], "raise_() activates Monitra on macOS and steals the foreground"
        assert native.calls[:2] == ["make_passive", "order_front"]

    def test_the_card_is_made_passive_before_it_is_shown(self, service, native):
        service.notify("Timer started", key="timer-started")
        # Configured first, ordered front last: a card shown before it was
        # passive would already have activated the app.
        assert native.calls.index("make_passive") < native.calls.index("order_front")

    def test_a_stack_of_cards_is_restacked_without_raising_any(
        self, service, native, raised
    ):
        for n in range(3):
            service.notify(f"Message {n}", key=f"k{n}")
        # Closing one restacks the others -- once a `raise_()` per card.
        service._popup.dismissed.emit()

        assert raised == []
        assert native.calls.count("order_front") >= 3

    def test_closing_a_card_only_closes_it(self, service, native, raised, monkeypatch):
        restores = []
        service.restore_requested.connect(lambda: restores.append(1))
        opened = []
        monkeypatch.setattr(
            "background_services.notifications.notification_service.QDesktopServices.openUrl",
            lambda url: opened.append(url),
        )
        service.notify("Update available", key="update", link="https://example.invalid/r")
        card = service._popup

        card._close.click()          # the x

        assert not card.isVisible()
        assert restores == [], "dismissing a notification must not open Monitra"
        assert opened == []
        assert raised == []

    def test_clicking_the_body_is_the_one_thing_that_opens_monitra(self, service, native):
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest

        restores = []
        service.restore_requested.connect(lambda: restores.append(1))
        service.notify("Timer started", key="timer-started")
        card = service._popup

        QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=card._card.geometry().center())

        assert restores == [1]

    def test_a_card_that_cannot_be_made_passive_falls_back_to_the_platform_banner(
        self, service, native, raised
    ):
        native.passive_ok = False

        assert service.notify("Timer started", key="timer-started") is True

        service._tray.showMessage.assert_called_once()
        assert raised == []
        assert "order_front" not in native.calls
        assert service._cards == [], "a card that would steal focus must not be shown"

    def test_a_native_window_that_only_exists_after_the_first_show_is_still_configured(
        self, qapp, native, monkeypatch
    ):
        # Qt may create the NSWindow lazily: the first attempt finds none.
        attempts = []

        def make_passive(widget):
            attempts.append(widget.isVisible())
            return len(attempts) > 1

        monkeypatch.setattr(mac_window, "make_passive", make_passive)
        card = ToastPopup()

        assert card.present("Title", "Body", "info") is True

        assert attempts == [False, True], "retry must happen after the window is shown"
        assert card.isVisible()
        card.hide()
        card.deleteLater()

    def test_a_card_that_could_not_be_made_passive_is_not_visible(self, qapp, native):
        native.passive_ok = False
        card = ToastPopup()
        assert card.present("Title", "Body", "info") is False
        assert not card.isVisible()
        card.deleteLater()


class TestWindowsIsUntouched:
    """The Windows card works and is not to change."""

    def test_no_native_call_is_made_and_raise_is_still_used(self, qapp, service, raised, monkeypatch):
        calls = []
        monkeypatch.setattr(mac_window, "make_passive", lambda w: calls.append("passive"))
        monkeypatch.setattr(mac_window, "order_front_without_activating",
                            lambda w: calls.append("front"))
        assert not mac_window.is_macos()          # this suite runs on the dev/CI host

        assert service.notify("Timer started", key="timer-started") is True

        assert calls == []
        assert len(raised) >= 1

    def test_the_window_flags_are_the_original_three(self, qapp):
        card = ToastPopup()
        flags = card.windowFlags()
        assert flags & Qt.WindowType.Tool
        assert flags & Qt.WindowType.FramelessWindowHint
        assert flags & Qt.WindowType.WindowStaysOnTopHint
        assert not flags & Qt.WindowType.WindowDoesNotAcceptFocus
        card.deleteLater()


class TestMacWindowHelpersAreGuarded:
    def test_nothing_native_runs_off_macos(self, qapp):
        card = ToastPopup()
        assert mac_window.make_passive(card) is False
        assert mac_window.order_front_without_activating(card) is False
        card.deleteLater()

    def test_the_offscreen_platform_is_not_mistaken_for_cocoa(self, qapp, monkeypatch):
        # The suite runs on macOS under Qt's `offscreen` platform, where
        # winId() is not an NSView* and wrapping it would crash the process.
        monkeypatch.setattr("sys.platform", "darwin")
        assert mac_window.is_macos() is False

    def test_make_passive_configures_the_window_for_a_background_notification(
        self, qapp, monkeypatch
    ):
        class Panel:
            def __init__(self):
                self.style = 0b0001
                self.behaviour = 0
                self.calls = []

            def isKindOfClass_(self, cls):
                return True

            def styleMask(self):
                return self.style

            def setStyleMask_(self, mask):
                self.style = mask

            def collectionBehavior(self):
                return self.behaviour

            def setCollectionBehavior_(self, value):
                self.behaviour = value

            def setFloatingPanel_(self, value):
                self.calls.append(("floating", value))

            def setBecomesKeyOnlyIfNeeded_(self, value):
                self.calls.append(("key_only_if_needed", value))

            def setHidesOnDeactivate_(self, value):
                self.calls.append(("hides_on_deactivate", value))

        panel = Panel()
        appkit = type("AppKit", (), {"NSPanel": object})
        monkeypatch.setitem(__import__("sys").modules, "AppKit", appkit)
        monkeypatch.setattr(mac_window, "is_macos", lambda: True)
        monkeypatch.setattr(mac_window, "_ns_window", lambda widget: panel)

        assert mac_window.make_passive(object()) is True

        assert panel.style & mac_window.STYLE_MASK_NONACTIVATING_PANEL
        assert panel.style & 0b0001, "the existing style bits must be kept"
        assert ("hides_on_deactivate", False) in panel.calls
        assert ("floating", True) in panel.calls
        assert panel.behaviour & mac_window.COLLECTION_CAN_JOIN_ALL_SPACES
        assert panel.behaviour & mac_window.COLLECTION_FULL_SCREEN_AUXILIARY

    def test_a_native_failure_is_reported_not_raised(self, qapp, monkeypatch):
        monkeypatch.setattr(mac_window, "is_macos", lambda: True)

        def boom(widget):
            raise RuntimeError("no pyobjc")

        monkeypatch.setattr(mac_window, "_ns_window", boom)
        assert mac_window.make_passive(object()) is False
        assert mac_window.order_front_without_activating(object()) is False

    def test_order_front_uses_the_non_activating_call(self, qapp, monkeypatch):
        window = MagicMock()
        monkeypatch.setattr(mac_window, "is_macos", lambda: True)
        monkeypatch.setattr(mac_window, "_ns_window", lambda widget: window)

        assert mac_window.order_front_without_activating(object()) is True

        window.orderFrontRegardless.assert_called_once()
        # None of the activating calls.
        assert not window.makeKeyAndOrderFront_.called
        assert not window.makeKeyWindow.called
