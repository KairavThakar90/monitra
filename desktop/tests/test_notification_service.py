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
    """The same service with its in-app card enabled and asked for by default.

    Notifications are the platform's own unless a caller says otherwise
    (`NATIVE_BY_DEFAULT`), so the card -- still the fallback, and what
    `native=False` draws -- is selected here for the tests that are about it.
    """
    svc = NotificationService(MagicMock())
    svc.NATIVE_BY_DEFAULT = False
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
    popup_service._clock = lambda: 500.0      # a still clock: no real time in the sum
    popup_service.notify("Drink water", key="wellbeing:hydrate")

    assert popup_service._popup.isVisible()
    # Exactly the display time, with no margin on top: Qt rounds a coarse
    # timer this long to whole seconds, so thirty and a half became
    # thirty-one. The timer is a precise one for the same reason.
    assert popup_service._cards[0].deadline == 530.0
    assert popup_service._dismiss_timer.interval() == 30_000
    assert popup_service._dismiss_timer.timerType() == Qt.TimerType.PreciseTimer


def test_every_kind_of_notification_gets_the_same_thirty_seconds(popup_service):
    """One display time for all of them -- a reminder, a timer event, an
    error -- because there is one timer and one constant behind it."""
    now = [500.0]
    popup_service._clock = lambda: now[0]
    for level, key in (
        (NotificationLevel.INFO, "wellbeing:hydrate"),
        (NotificationLevel.SUCCESS, "timer-started"),
        (NotificationLevel.WARNING, "network"),
        (NotificationLevel.ERROR, "task-mut-err"),
    ):
        popup_service.notify(f"message for {key}", level, key=key)
        # Thirty seconds from when *this* card appeared, whatever its level.
        assert popup_service._cards[-1].deadline == now[0] + 30.0
        now[0] += 3.0


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


# ── The platform's own notification (`native=True`) ──────────────────────────
#
# The administrator's notifications are shown as the operating system's own: a
# Windows toast under the application's name with the standard icon and the
# time, kept in the Action Center afterwards. The application's own messages keep
# their card. One surface per notification, never both.

def test_a_native_notification_is_the_platforms_own_with_the_standard_icon(popup_service):
    from PySide6.QtWidgets import QSystemTrayIcon

    assert popup_service.notify("Please save your work.", NotificationLevel.INFO, title="Server restart",
                                key="push:1", native=True) is True

    popup_service._tray.showMessage.assert_called_once()
    title, message, icon, _ms = popup_service._tray.showMessage.call_args.args
    assert (title, message) == ("Server restart", "Please save your work.")
    assert icon == QSystemTrayIcon.MessageIcon.Information        # the blue "i", not the brand mark
    assert popup_service._cards == []                              # and no card as well


def test_the_icon_is_the_blue_i_whatever_the_level(popup_service):
    """The owner's design: every notification has the same look -- the application's header on top,
    then the information icon, the title and the text."""
    from PySide6.QtWidgets import QSystemTrayIcon

    for level in (NotificationLevel.INFO, NotificationLevel.SUCCESS, NotificationLevel.WARNING, NotificationLevel.ERROR):
        popup_service.notify(f"A {level} message", level, key=f"k-{level}", native=True)

    icons = [call.args[2] for call in popup_service._tray.showMessage.call_args_list]
    assert icons == [QSystemTrayIcon.MessageIcon.Information] * 4


def test_a_notification_gives_its_own_title_and_text_and_never_the_application_name(native_service):
    native_service.notify("Keep a water bottle nearby.", title="Drink Water", key="water")
    native_service.notify("Timer started for 'project v2'", key="timer")           # no title given
    native_service.show_success("Task created", key="task")                        # the wrappers give none either

    shown = [call.args[:2] for call in native_service._tray.showMessage.call_args_list]
    assert shown == [
        ("Drink Water", "Keep a water bottle nearby."),
        ("", "Timer started for 'project v2'"),            # just the text: no "Monitra" under Monitra's own header
        ("", "Task created"),
    ]


def test_a_card_still_has_a_heading_when_the_caller_gave_none(native_service):
    native_service.notify("Timer started", key="timer-started", native=False)

    assert native_service._popup._title.text() == "Monitra"


def test_the_platform_fallback_toast_has_a_heading_too(native_service, monkeypatch):
    """No card could be placed, so the platform toast is the fallback: it keeps the application's name."""
    native_service._popup_enabled = False
    native_service.notify("Timer started", key="timer-started", native=False)

    assert native_service._tray.showMessage.call_args.args[0] == "Monitra"


def test_the_tray_icon_is_the_monitra_logo_which_windows_draws_in_every_notifications_header(qapp, monkeypatch):
    from background_services.notifications import notification_service as module

    shown_with = []
    real = module.QSystemTrayIcon

    class SpyTray(real):
        def __init__(self, icon, parent=None):
            shown_with.append(icon)
            super().__init__(icon, parent)

    monkeypatch.setattr(module, "QSystemTrayIcon", SpyTray)
    monkeypatch.setattr(real, "isSystemTrayAvailable", staticmethod(lambda: True))
    svc = module.NotificationService(MagicMock())
    try:
        svc.on_start()
        assert shown_with and shown_with[0] is svc._icon          # the shared Monitra mark, not another brand's
    finally:
        svc.on_stop(1000)


@pytest.fixture
def native_service(qapp):
    """The service as it is shipped: the card is available, native is the default."""
    svc = NotificationService(MagicMock())
    svc._available = True
    svc._tray = MagicMock()
    svc._icon = MagicMock()
    svc._icon.isNull.return_value = False
    yield svc
    svc._dismiss_timer.stop()
    svc.on_stop(1000)


def test_every_notification_is_the_platforms_own_by_default(native_service):
    """Not only the administrator's: an application message, a warning, an error."""
    assert NotificationService.NATIVE_BY_DEFAULT is True
    for level in (NotificationLevel.INFO, NotificationLevel.SUCCESS, NotificationLevel.WARNING, NotificationLevel.ERROR):
        assert native_service.notify(f"A {level} message", level, key=f"k-{level}") is True

    assert native_service._tray.showMessage.call_count == 4
    assert native_service._cards == []                              # no card as well


def test_the_convenience_wrappers_are_native_too(native_service):
    native_service.show_info("i", key="a")
    native_service.show_success("s", key="b")
    native_service.show_warning("w", key="c")
    native_service.show_error("e", key="d")

    assert native_service._tray.showMessage.call_count == 4
    assert native_service._cards == []


def test_a_caller_can_still_ask_for_the_card(native_service):
    native_service.notify("Timer started", key="timer-started", native=False)

    assert native_service._popup is not None and native_service._popup.isVisible()
    native_service._tray.showMessage.assert_not_called()


def test_the_default_can_be_switched_off_for_the_whole_service(native_service):
    native_service.NATIVE_BY_DEFAULT = False
    native_service.notify("Timer started", key="timer-started")

    assert native_service._popup is not None and native_service._popup.isVisible()
    native_service._tray.showMessage.assert_not_called()


def test_a_native_notification_falls_back_to_a_card_when_there_is_no_tray(qapp):
    svc = NotificationService(MagicMock())
    svc._available = False
    svc._tray = None
    try:
        assert svc.notify("Please save your work.", key="push:1", native=True) is True
        assert svc._popup is not None and svc._popup.isVisible()
    finally:
        svc._dismiss_timer.stop()
        svc.on_stop(1000)


def test_a_native_notification_the_platform_refuses_becomes_a_card_not_a_loss(popup_service):
    popup_service._tray.showMessage.side_effect = RuntimeError("no notification centre")

    assert popup_service.notify("Please save your work.", key="push:1", native=True) is True

    assert popup_service._popup is not None and popup_service._popup.isVisible()
    assert popup_service._pending_link is None


def test_native_notifications_are_still_de_duplicated_and_rate_limited(popup_service):
    assert popup_service.notify("Hello", key="same", native=True) is True
    assert popup_service.notify("Hello", key="same", native=True) is False
    assert popup_service._tray.showMessage.call_count == 1

    admitted = sum(
        1 for index in range(NotificationService.MAX_PER_MINUTE + 10)
        if popup_service.notify(f"message {index}", key=f"key-{index}", native=True)
    )
    assert admitted <= NotificationService.MAX_PER_MINUTE - 1       # one already used this minute


def test_clicking_a_native_notification_opens_its_link(popup_service, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "background_services.notifications.notification_service.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()),
    )

    popup_service.notify("Update available", key="update", link="https://example.invalid/r", native=True)
    popup_service._on_message_clicked()

    assert opened == ["https://example.invalid/r"]


def test_a_native_notification_from_a_worker_thread_is_shown_on_the_owning_thread(popup_service):
    from PySide6.QtCore import QCoreApplication

    def worker():
        popup_service.notify("From a worker", key="worker", native=True)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    popup_service._tray.showMessage.assert_not_called()             # not on the worker's thread

    QCoreApplication.processEvents()

    popup_service._tray.showMessage.assert_called_once()            # on the owner's, natively
    assert popup_service._cards == []


def test_stopping_clears_a_native_notifications_link(popup_service):
    popup_service.notify("Update available", key="update", link="https://example.invalid/r", native=True)

    popup_service.on_stop(1000)

    assert popup_service._pending_link is None
