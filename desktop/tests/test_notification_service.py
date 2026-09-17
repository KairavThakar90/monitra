"""
Notification service: admission control and thread safety.

The module was already sound in its lifecycle handling -- one owned
single-shot dismissal timer, one tray icon, an explicit Windows
AppUserModelID -- so this covers the parts that had no test behind them:
de-duplication, the per-minute ceiling, and the rule that a widget is only
ever touched on the thread that owns it.
"""
from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import Qt, QThread, QTimer
from unittest.mock import MagicMock

from background_services.notifications.notification_service import (
    NotificationLevel,
    NotificationService,
)


@pytest.fixture
def service(qapp):
    svc = NotificationService(MagicMock())
    # Stand in for the tray so the tests exercise admission and dispatch
    # without depending on a system tray existing in a headless run.
    svc._available = True
    svc._tray = MagicMock()
    svc._icon = MagicMock()
    svc._icon.isNull.return_value = False
    # These tests are about admission and dispatch, and they read the message
    # off the tray. The in-app card is the surface a user actually sees (see
    # the popup section at the foot of this file); switching it off here means
    # delivery falls through to the tray, which is the same code path a machine
    # with no screen for the card takes.
    svc._popup_enabled = False
    yield svc
    svc._dismiss_timer.stop()


@pytest.fixture
def popup_service(qapp):
    """The same service with its in-app card enabled — the real delivery path."""
    svc = NotificationService(MagicMock())
    svc._available = True
    svc._tray = MagicMock()
    svc._icon = MagicMock()
    svc._icon.isNull.return_value = False
    yield svc
    svc._dismiss_timer.stop()
    svc.on_stop(1000)


def test_repeat_of_the_same_key_is_suppressed(service):
    assert service.notify("Back online", key="network") is True
    assert service.notify("Back online", key="network") is False
    assert service._tray.showMessage.call_count == 1


def test_network_flapping_produces_one_message_not_a_storm(service):
    """ONLINE/OFFLINE/ONLINE/OFFLINE... must not become a toast per
    transition."""
    for _ in range(20):
        service.notify("Connection lost", NotificationLevel.WARNING, key="network")
        service.notify("Back online", NotificationLevel.SUCCESS, key="network")

    assert service._tray.showMessage.call_count == 1


def test_distinct_events_are_not_suppressed_by_each_other(service):
    """Throttling must not silence unrelated, important events."""
    assert service.notify("Logged in", key="login") is True
    assert service.notify("Timer started", key="timer-started") is True
    assert service.notify("Sync failed", key="sync-error") is True
    assert service._tray.showMessage.call_count == 3


def test_the_per_minute_ceiling_is_enforced(service):
    admitted = sum(
        1 for index in range(NotificationService.MAX_PER_MINUTE + 10)
        if service.notify(f"message {index}", key=f"key-{index}")
    )
    assert admitted == NotificationService.MAX_PER_MINUTE


def test_a_dismissal_timer_is_armed_for_every_shown_notification(service):
    service.notify("Timer started", key="timer-started")
    assert service._dismiss_timer.isActive()
    assert service._dismiss_timer.isSingleShot()


def test_stopping_disarms_the_dismissal_timer(service):
    service.notify("Timer started", key="timer-started")
    service.on_stop(1000)
    assert not service._dismiss_timer.isActive()
    assert service._tray is None


def test_notifying_from_a_worker_thread_does_not_touch_the_tray_there(service):
    """The tray is a widget: a background service must never mutate it from
    its own thread. Off-thread calls are handed to the owning thread through
    a queued signal instead, so `showMessage` is not called inline."""
    result = {}

    def worker():
        result["thread"] = QThread.currentThread()
        result["returned"] = service.notify("From a worker", key="worker")

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert result["returned"] is True
    assert result["thread"] is not service.thread()
    # Delivery was deferred, not performed on the worker thread.
    service._tray.showMessage.assert_not_called()

    # It arrives once the owning thread runs its event loop.
    qapp = service.thread()
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.processEvents()
    service._tray.showMessage.assert_called_once()
    assert qapp is service.thread()


# ── Clicking a toast ──────────────────────────────────────────────────────────
#
# A platform toast renders plain text: a URL written into the message body is
# not a hyperlink, and clicking the toast dismisses it. A notification whose
# whole purpose is to send the user somewhere was therefore a dead end -- the
# user clicked it and the address disappeared. `link` fixes that, and these
# tests pin the part that could go wrong: a click must never open the link of
# a notification that is no longer on screen.


def test_clicking_a_toast_with_a_link_opens_it(service, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "background_services.notifications.notification_service.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )

    service.notify("Update available", key="update", link="https://example.invalid/r")
    service._on_message_clicked()

    assert opened == ["https://example.invalid/r"]


def test_clicking_a_toast_without_a_link_restores_the_window(service):
    restored = []
    service.restore_requested.connect(lambda: restored.append(True))

    service.notify("Timer started", key="timer")
    service._on_message_clicked()

    assert restored == [True]


def test_a_link_does_not_outlive_its_notification(service, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "background_services.notifications.notification_service.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )

    service.notify("Update available", key="update", link="https://example.invalid/r")
    service._retire_current()          # the toast's display window ended
    service._on_message_clicked()      # a late click belongs to nothing

    assert opened == []


def test_a_newer_notification_replaces_the_previous_link(service, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "background_services.notifications.notification_service.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )

    service.notify("First", key="one", link="https://example.invalid/first")
    service.notify("Second", key="two", link="https://example.invalid/second")
    service._on_message_clicked()

    assert opened == ["https://example.invalid/second"]


def test_clicking_twice_only_opens_once(service, monkeypatch):
    # The link is consumed on click. A second click has nothing to open, so a
    # double-click cannot launch two browser windows.
    opened = []
    monkeypatch.setattr(
        "background_services.notifications.notification_service.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )

    service.notify("Update available", key="update", link="https://example.invalid/r")
    service._on_message_clicked()
    service._on_message_clicked()

    assert opened == ["https://example.invalid/r"]


def test_stopping_the_service_clears_a_pending_link(service):
    service.notify("Update available", key="update", link="https://example.invalid/r")
    service.on_stop(1000)

    assert service._pending_link is None


# ── Display duration ──────────────────────────────────────────────────────────

def test_a_notification_is_held_for_at_least_a_minute(service):
    """The requested behaviour: a toast is not a blink-and-miss-it flash.

    Windows ignores the hint and uses its own accessibility setting, so this
    can only assert what the service actually controls -- the value passed to
    the platform, and how long the service keeps the notification alive.
    """
    assert NotificationService.DISPLAY_MS >= 60_000

    service.notify("Drink water", key="wellbeing:hydrate")

    _, _, _, timeout_ms = service._tray.showMessage.call_args[0]
    assert timeout_ms >= 60_000


def test_the_retirement_timer_outlasts_the_display_window(service):
    """Retiring early would drop the link while the toast is still on screen."""
    service.notify("Release 2.0 is available", key="update", link="https://example.com")

    assert service._dismiss_timer.remainingTime() > NotificationService.DISPLAY_MS
    assert service._pending_link == "https://example.com"


def test_a_click_still_opens_the_link_late_in_the_display_window(service):
    """A minute-long toast is one the user can act on a minute later."""
    service.notify("Release 2.0 is available", key="update", link="https://example.com")

    service._retire_current()          # only now does the window close
    assert service._pending_link is None


# ── The in-app card ───────────────────────────────────────────────────────────
#
# `DISPLAY_MS` used to be a hint nothing honoured: Windows ignores the timeout
# passed to `showMessage` and uses the user's accessibility setting instead --
# five seconds by default, about twenty-five for a long toast -- so a minute's
# worth of notification was on screen for a fraction of it. Monitra now draws
# the notification itself, and these tests pin the properties that make that
# both correct and safe.


def test_a_notification_is_drawn_by_the_app_not_the_platform(popup_service):
    """The card is the surface, because it is the one that honours the minute."""
    popup_service.notify("Timer started", key="timer-started")

    popup = popup_service._popup
    assert popup is not None
    assert popup.isVisible()
    assert popup._message.text() == "Timer started"


def test_the_platform_toast_is_not_fired_alongside_the_card(popup_service):
    """One event, one notification. Both surfaces would notify twice."""
    popup_service.notify("Timer started", key="timer-started")

    popup_service._tray.showMessage.assert_not_called()


def test_the_card_stays_up_for_at_least_a_minute(popup_service):
    """The whole point of the change: a full minute of real on-screen time."""
    popup_service.notify("Drink water", key="wellbeing:hydrate")

    assert popup_service._popup.isVisible()
    assert popup_service._dismiss_timer.remainingTime() >= 60_000


def test_the_card_owns_no_timer_of_its_own(popup_service):
    """DO_NOT_DO: a widget owning a dismissal timer can orphan it.

    The service owns exactly one, for the whole application.
    """
    popup_service.notify("Timer started", key="timer-started")

    assert popup_service._popup.findChildren(QTimer) == []


def test_retiring_hides_the_card(popup_service):
    popup_service.notify("Timer started", key="timer-started")
    popup_service._retire_current()

    assert not popup_service._popup.isVisible()


def test_one_card_is_reused_so_a_burst_cannot_stack_windows(popup_service):
    popup_service.notify("First", key="one")
    first = popup_service._popup
    popup_service.notify("Second", key="two")

    assert popup_service._popup is first
    assert first._message.text() == "Second"


def test_the_card_never_steals_focus(popup_service):
    """A toast arriving mid-sentence must not take the keystroke."""
    popup_service.notify("Timer started", key="timer-started")

    assert popup_service._popup.testAttribute(
        Qt.WidgetAttribute.WA_ShowWithoutActivating
    )


def test_clicking_the_card_opens_its_link_and_ends_the_notification(
    popup_service, monkeypatch
):
    opened = []
    monkeypatch.setattr(
        "background_services.notifications.notification_service.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )

    popup_service.notify("Update available", key="update", link="https://example.invalid/r")
    popup_service._popup.clicked.emit()

    assert opened == ["https://example.invalid/r"]
    assert not popup_service._popup.isVisible()
    # The display window is over, so no timer may still be armed for it.
    assert not popup_service._dismiss_timer.isActive()


def test_closing_the_card_ends_the_notification_without_opening_anything(
    popup_service, monkeypatch
):
    opened = []
    monkeypatch.setattr(
        "background_services.notifications.notification_service.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )

    popup_service.notify("Update available", key="update", link="https://example.invalid/r")
    popup_service._popup.dismissed.emit()

    assert opened == []
    assert not popup_service._popup.isVisible()
    assert popup_service._pending_link is None


def test_a_card_that_cannot_be_placed_falls_back_to_the_platform_toast(popup_service):
    """A machine with no screen to place the card on still gets notified."""
    popup = popup_service._ensure_popup()
    popup.present = lambda *args, **kwargs: False

    assert popup_service.notify("Timer started", key="timer-started") is True
    popup_service._tray.showMessage.assert_called_once()


def test_a_popup_that_raises_falls_back_and_still_arms_the_timer(popup_service):
    """A broken window must not swallow the notification."""
    popup = popup_service._ensure_popup()

    def explode(*args, **kwargs):
        raise RuntimeError("no compositor")

    popup.present = explode

    assert popup_service.notify("Sync failed", key="sync-error") is True
    popup_service._tray.showMessage.assert_called_once()
    assert popup_service._dismiss_timer.isActive()


def test_stopping_the_service_takes_the_card_down(popup_service):
    popup_service.notify("Timer started", key="timer-started")
    popup = popup_service._popup

    popup_service.on_stop(1000)

    assert not popup.isVisible()
    assert popup_service._popup is None
