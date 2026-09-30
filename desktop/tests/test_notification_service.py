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

def test_a_notification_is_held_for_thirty_seconds(service):
    """The requested behaviour: thirty seconds, for every notification.

    It was a minute; the owner asked for thirty seconds (2026-09-30). Exact,
    not "at least": the point of the request is that the card is gone after
    half a minute, so a longer value is as wrong as a shorter one.

    Windows ignores the hint and uses its own accessibility setting, so this
    can only assert what the service actually controls -- the value passed to
    the platform, and how long the service keeps the notification alive.
    """
    assert NotificationService.DISPLAY_MS == 30_000

    service.notify("Drink water", key="wellbeing:hydrate")

    _, _, _, timeout_ms = service._tray.showMessage.call_args[0]
    assert timeout_ms == 30_000


def test_the_retirement_timer_outlasts_the_display_window(service):
    """Retiring early would drop the link while the toast is still on screen."""
    service.notify("Release 2.0 is available", key="update", link="https://example.com")

    assert service._dismiss_timer.remainingTime() > NotificationService.DISPLAY_MS
    assert service._pending_link == "https://example.com"


def test_a_click_still_opens_the_link_late_in_the_display_window(service):
    """A toast that is up for its whole window can be acted on until it ends."""
    service.notify("Release 2.0 is available", key="update", link="https://example.com")

    service._retire_current()          # only now does the window close
    assert service._pending_link is None


# ── The in-app card ───────────────────────────────────────────────────────────
#
# `DISPLAY_MS` used to be a hint nothing honoured: Windows ignores the timeout
# passed to `showMessage` and uses the user's accessibility setting instead --
# five seconds by default, about twenty-five for a long toast -- so the time
# asked for and the time on screen had nothing to do with each other. Monitra
# now draws the notification itself, and these tests pin the properties that
# make that both correct and safe.


def test_the_card_is_shown_even_when_no_system_tray_exists(qapp):
    """A missing tray must degrade to the in-app card, not drop the
    notification outright -- otherwise a failed action (e.g. a task that
    could not be created) gives the user no feedback at all on a machine
    or session (RDP, some Windows configs) where the tray is unavailable.
    """
    svc = NotificationService(MagicMock())
    svc._available = False
    svc._tray = None
    try:
        shown = svc.notify("Could not create task", NotificationLevel.ERROR, key="err")

        assert shown is True
        assert svc._popup is not None
        assert svc._popup.isVisible()
        assert svc._popup._message.text() == "Could not create task"
    finally:
        svc._dismiss_timer.stop()
        svc.on_stop(1000)


def test_a_notification_is_drawn_by_the_app_not_the_platform(popup_service):
    """The card is the surface, because it is the one that honours DISPLAY_MS."""
    popup_service.notify("Timer started", key="timer-started")

    popup = popup_service._popup
    assert popup is not None
    assert popup.isVisible()
    assert popup._message.text() == "Timer started"


def test_the_platform_toast_is_not_fired_alongside_the_card(popup_service):
    """One event, one notification. Both surfaces would notify twice."""
    popup_service.notify("Timer started", key="timer-started")

    popup_service._tray.showMessage.assert_not_called()


def test_the_card_stays_up_for_thirty_seconds_and_no_longer(popup_service):
    """Real on-screen time: the card is retired by the service's own timer,
    thirty seconds after it appears -- not by the platform, and not a minute
    later as it used to be."""
    popup_service.notify("Drink water", key="wellbeing:hydrate")

    assert popup_service._popup.isVisible()
    # Exactly the display time, with no margin on top: Qt rounds a timer this
    # long to whole seconds, so thirty and a half became thirty-one.
    assert popup_service._dismiss_timer.interval() == 30_000


def test_every_kind_of_notification_gets_the_same_thirty_seconds(popup_service):
    """One display time for all of them -- a reminder, a timer event, an
    error -- because there is one timer and one constant behind it."""
    for level, key in (
        (NotificationLevel.INFO, "wellbeing:hydrate"),
        (NotificationLevel.SUCCESS, "timer-started"),
        (NotificationLevel.WARNING, "network"),
        (NotificationLevel.ERROR, "task-mut-err"),
    ):
        popup_service.notify("message", level, key=key)
        assert popup_service._dismiss_timer.interval() == 30_000


def test_the_card_is_gone_when_its_thirty_seconds_are_up(popup_service):
    """The timer firing is what takes the card down."""
    popup_service.notify("Drink water", key="wellbeing:hydrate")
    assert popup_service._popup.isVisible()

    popup_service._dismiss_timer.timeout.emit()      # the thirty seconds elapsing

    assert not popup_service._popup.isVisible()


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


# ── A stack of cards ──────────────────────────────────────────────────────────
#
# There used to be one card, reused for every notification, and a test here
# pinned that a second notification replaced the first one's text. The owner
# reported what that meant in use (2026-09-30): with "Logged in successfully"
# still up and unclosed, "Pending activity synced successfully." arrived as a
# change of words on a card already there -- nothing on screen said a new
# notification had come. Each notification now gets its own card, above the
# ones still up.

def _card_rect(card):
    """The visible card's rectangle on screen, without its shadow margin."""
    frame, margins = card.geometry(), card.layout().contentsMargins()
    return frame.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())


def _messages(service):
    """What is on screen, bottom card first."""
    return [shown.card._message.text() for shown in service._cards]


def test_a_second_notification_gets_a_card_of_its_own(popup_service):
    """The reported defect: the first card's text must not be overwritten."""
    popup_service.notify("Logged in successfully", key="login")
    first = popup_service._popup
    popup_service.notify("Pending activity synced successfully.", key="sync")
    second = popup_service._popup

    assert second is not first
    assert first.isVisible() and second.isVisible()
    assert first._message.text() == "Logged in successfully"
    assert second._message.text() == "Pending activity synced successfully."


def test_the_new_card_sits_above_the_one_already_up(popup_service):
    popup_service.notify("Logged in successfully", key="login")
    first = popup_service._popup
    corner = _card_rect(first)
    popup_service.notify("Pending activity synced successfully.", key="sync")
    second = popup_service._popup

    # The earlier card has not moved, and the new one is directly above it.
    assert _card_rect(first) == corner
    above = _card_rect(second)
    assert above.bottom() + 1 == corner.top() - NotificationService.CARD_GAP
    assert above.left() == corner.left() and above.right() == corner.right()


def test_three_notifications_make_three_cards_in_arrival_order(popup_service):
    for index, text in enumerate(("First", "Second", "Third")):
        popup_service.notify(text, key=f"key-{index}")

    assert _messages(popup_service) == ["First", "Second", "Third"]
    tops = [_card_rect(shown.card).top() for shown in popup_service._cards]
    assert tops == sorted(tops, reverse=True), "each card is above the one before it"
    rects = [_card_rect(shown.card) for shown in popup_service._cards]
    assert not any(a.intersects(b) for a in rects for b in rects if a is not b)


def test_closing_one_card_leaves_the_others_and_closes_the_gap(popup_service):
    for index, text in enumerate(("First", "Second", "Third")):
        popup_service.notify(text, key=f"key-{index}")
    first, second, third = (shown.card for shown in popup_service._cards)
    corner = _card_rect(first)

    second.dismissed.emit()                  # the × on the middle card

    assert _messages(popup_service) == ["First", "Third"]
    assert not second.isVisible()
    assert first.isVisible() and third.isVisible()
    assert _card_rect(first) == corner
    assert _card_rect(third).bottom() + 1 == corner.top() - NotificationService.CARD_GAP


def test_closing_the_bottom_card_slides_the_rest_down_to_the_corner(popup_service):
    popup_service.notify("First", key="one")
    first = popup_service._popup
    corner = _card_rect(first)
    popup_service.notify("Second", key="two")
    second = popup_service._popup

    first.dismissed.emit()

    assert _messages(popup_service) == ["Second"]
    assert _card_rect(second).bottom() == corner.bottom()


def test_each_card_has_its_own_thirty_seconds(popup_service):
    """A later card does not extend an earlier one, and is not cut short by
    it: each goes thirty seconds after it appeared."""
    now = [100.0]
    popup_service._clock = lambda: now[0]

    popup_service.notify("First", key="one")
    now[0] = 112.0
    popup_service.notify("Second", key="two")

    first, second = popup_service._cards
    assert first.deadline == 130.0
    assert second.deadline == 142.0
    # One timer, armed for whichever goes next.
    assert popup_service._dismiss_timer.interval() == 18_000

    now[0] = 130.0
    popup_service._dismiss_timer.timeout.emit()
    assert _messages(popup_service) == ["Second"]
    assert popup_service._dismiss_timer.isActive()
    assert popup_service._dismiss_timer.interval() == 12_000

    now[0] = 142.0
    popup_service._dismiss_timer.timeout.emit()
    assert _messages(popup_service) == []
    assert not popup_service._dismiss_timer.isActive()


def test_there_is_still_exactly_one_timer_however_many_cards(popup_service):
    """DO_NOT_DO: a widget owning a dismissal timer can orphan it."""
    for index in range(3):
        popup_service.notify(f"message {index}", key=f"key-{index}")

    assert len(popup_service._cards) == 3
    for shown in popup_service._cards:
        assert shown.card.findChildren(QTimer) == []
    assert popup_service.findChildren(QTimer) == [popup_service._dismiss_timer]


def test_the_stack_is_capped_and_the_oldest_makes_room(popup_service):
    for index in range(NotificationService.MAX_CARDS + 2):
        popup_service.notify(f"message {index}", key=f"key-{index}")

    assert len(popup_service._cards) == NotificationService.MAX_CARDS
    newest = NotificationService.MAX_CARDS + 1
    assert _messages(popup_service)[-1] == f"message {newest}"
    assert "message 0" not in _messages(popup_service)
    assert "message 1" not in _messages(popup_service)


def test_no_card_is_ever_placed_off_the_screen(popup_service, qapp):
    long_text = "A long notification that wraps onto several lines. " * 14
    for index in range(NotificationService.MAX_CARDS):
        popup_service.notify(long_text + str(index), key=f"key-{index}")

    area = qapp.primaryScreen().availableGeometry()
    for shown in popup_service._cards:
        assert area.contains(_card_rect(shown.card)), "a card is off the working area"
    assert _messages(popup_service)[-1].endswith(str(NotificationService.MAX_CARDS - 1))


def test_the_same_notification_again_does_not_add_a_second_card(popup_service):
    """Nothing new to say, so no second card saying it -- its time restarts."""
    now = [100.0]
    popup_service._clock = lambda: now[0]
    popup_service.DEDUPE_SECONDS = 0.0       # let the repeat through admission
    popup_service.notify("Refreshed", key="refresh")

    now[0] = 110.0
    popup_service.notify("Refreshed", key="refresh")

    assert _messages(popup_service) == ["Refreshed"]
    assert popup_service._cards[0].deadline == 140.0


def test_a_click_opens_the_link_of_the_card_that_was_clicked(popup_service, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "background_services.notifications.notification_service.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )
    popup_service.notify("Update available", key="update", link="https://example.invalid/update")
    lower = popup_service._popup
    popup_service.notify("Download ready", key="download", link="https://example.invalid/download")

    lower.clicked.emit()

    assert opened == ["https://example.invalid/update"]
    assert _messages(popup_service) == ["Download ready"]


def test_a_press_on_the_shadow_margin_is_not_a_click_on_the_card(popup_service, qapp):
    """The window is larger than the card, and in a stack that margin lies
    over the neighbouring card."""
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    popup_service.notify("Timer started", key="timer-started")
    card = popup_service._popup
    clicks = []
    card.clicked.connect(lambda: clicks.append(1))

    QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(card.width() // 2, 4))
    assert clicks == [] and card.isVisible()

    QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=card._card.geometry().center())
    assert clicks == [1]


def test_a_retired_card_is_used_again_rather_than_building_a_window(popup_service):
    popup_service.notify("First", key="one")
    first = popup_service._popup
    first.dismissed.emit()

    popup_service.notify("Second", key="two")

    assert popup_service._popup is first
    assert first._message.text() == "Second"
    assert _messages(popup_service) == ["Second"]


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
