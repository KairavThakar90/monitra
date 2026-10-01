"""
The notification card is fully on screen, whatever the message length.

`ToastPopup` is fixed at `WIDTH` pixels wide, but it used to be positioned by
`sizeHint()`, which reports the word-wrapped label's *unconstrained* width --
narrower than the card for a short message ("Screenshot captured at 3:57 PM"
gave 263px against a 360px card), far wider for a long one -- and its
unwrapped height. The card was therefore placed where a narrower one would
fit and its right third, close button included, hung off the screen. Users
saw "the notification shows half". These tests pin every edge of the drawn
card inside the screen's working area, at the intended margin.
"""
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QGuiApplication

from background_services.notifications.toast_popup import ToastPopup

SHORT = "Screenshot captured at 3:57 PM"
MEDIUM = "You have been idle. Monitra needs to know whether to keep that time."
LONG = "word " * 120


def _present(qapp, popup, message):
    assert popup.present("Monitra", message, "info")
    qapp.processEvents()
    qapp.processEvents()
    return popup.frameGeometry(), QGuiApplication.primaryScreen().availableGeometry()


def _visible_card_rect(popup) -> QRect:
    """The drawn white card's own rectangle, in global coordinates.

    `popup.frameGeometry()` is the whole (frameless) window, which includes
    the transparent margin reserved for the drop shadow -- wider on the
    bottom than the sides, to fit the shadow's downward offset. The margin
    that must sit CARD_SCREEN_MARGIN from the screen edge is the *drawn*
    card's, not the invisible window edge around it.
    """
    top_left = popup.mapToGlobal(popup._card.pos())
    return QRect(top_left, popup._card.size())


def test_a_short_message_sits_at_the_corner_margin(qapp):
    popup = ToastPopup()
    frame, area = _present(qapp, popup, SHORT)
    card = _visible_card_rect(popup)
    assert frame.width() == ToastPopup.WIDTH
    assert card.x() + card.width() == area.x() + area.width() - ToastPopup.CARD_SCREEN_MARGIN
    assert card.y() + card.height() == area.y() + area.height() - ToastPopup.CARD_SCREEN_MARGIN
    # The whole window -- shadow margin included -- must never hang off the
    # screen, which is the "notification shows half" defect this file exists
    # to pin.
    assert area.contains(frame), (frame, area)
    popup.hide()
    popup.deleteLater()


def test_the_close_button_is_on_screen(qapp):
    """The part that was cut off: the header's right end."""
    popup = ToastPopup()
    _frame, area = _present(qapp, popup, SHORT)
    close_right = popup._close.mapToGlobal(popup._close.rect().topRight()).x()
    assert close_right <= area.x() + area.width() - ToastPopup.CARD_SCREEN_MARGIN
    popup.hide()
    popup.deleteLater()


def test_a_wrapped_message_grows_downward_and_stays_on_screen(qapp):
    popup = ToastPopup()
    short_frame, _ = _present(qapp, popup, SHORT)
    long_frame, area = _present(qapp, popup, LONG)
    assert long_frame.height() > short_frame.height(), "the long text must wrap onto more lines"
    assert long_frame.width() == ToastPopup.WIDTH
    assert area.contains(long_frame), (long_frame, area)
    long_card = _visible_card_rect(popup)
    assert long_card.x() + long_card.width() == area.x() + area.width() - ToastPopup.CARD_SCREEN_MARGIN
    assert long_card.y() + long_card.height() == area.y() + area.height() - ToastPopup.CARD_SCREEN_MARGIN
    popup.hide()
    popup.deleteLater()


def test_replacing_a_long_message_with_a_short_one_repositions(qapp):
    """One card is reused for every notification; each must be re-placed."""
    popup = ToastPopup()
    _present(qapp, popup, LONG)
    frame, area = _present(qapp, popup, MEDIUM)
    assert area.contains(frame), (frame, area)
    card = _visible_card_rect(popup)
    assert card.y() + card.height() == area.y() + area.height() - ToastPopup.CARD_SCREEN_MARGIN
    popup.hide()
    popup.deleteLater()


def test_the_card_carries_the_monitra_badge(qapp):
    """The same badge tile `MaintenanceToast` draws, built from
    core.branding, so every floating notification card reads as one
    notification system rather than each inventing its own logo framing."""
    from core.branding import logo_badge_pixmap

    popup = ToastPopup()
    _present(qapp, popup, SHORT)
    shown = popup._logo.pixmap()
    assert shown is not None and not shown.isNull()
    assert popup._logo.size().width() == ToastPopup.LOGO_SIZE
    assert shown.toImage() == logo_badge_pixmap(ToastPopup.LOGO_SIZE).toImage()
    # The mark sits left of the title, both inside the card.
    assert popup._logo.geometry().right() <= popup._title.geometry().left()
    popup.hide()
    popup.deleteLater()


def test_the_badge_top_edge_lines_up_with_the_titles_top_edge(qapp):
    """The badge used to sit flush with the card's raw top edge while the
    title sat inset beneath it (TOP_INSET), so it read as floating well
    above the title instead of beside it -- reported directly from a real
    notification's screenshot. `QVBoxLayout` centres a lone fixed-size
    widget in whatever height it is stretched to unless told otherwise, so
    this also pins the fix against that default coming back."""
    popup = ToastPopup()
    _present(qapp, popup, SHORT)
    assert popup._logo.geometry().y() == popup._title.geometry().y()
    popup.hide()
    popup.deleteLater()


def test_the_badge_stays_top_aligned_for_a_message_long_enough_to_wrap(qapp):
    """A tall, multi-line message must not pull the badge down with it --
    the badge aligns with the *title*, not the vertical centre of the card."""
    popup = ToastPopup()
    _present(qapp, popup, LONG)
    assert popup._logo.geometry().y() == popup._title.geometry().y()
    popup.hide()
    popup.deleteLater()


def test_a_long_title_wraps_instead_of_being_cut_off(qapp):
    """Reported 2026-09-30: "Stretch Your Hands & Wrists" was clipped at the
    close button because the title was a single-line label in a fixed-width
    card. It wraps like the message, and the card grows to hold it."""
    popup = ToastPopup()
    popup.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)

    popup.present("Short", "message", "info")
    short_card = popup.card_height()
    popup.present("🤲 Stretch Your Hands & Wrists and Arms and More", "message", "info")
    for _ in range(3):
        qapp.processEvents()

    title = popup._title
    assert title.wordWrap()
    assert title.heightForWidth(title.width()) <= title.height(), "the wrapped title is not fully shown"
    assert popup.card_height() > short_card, "the card must grow to hold a wrapped title"


def _fit_report(popup):
    """The sizes the card settled on, after the event loop has run."""
    return (
        popup._title.height(), popup._message.height(),
        popup.card_height(), popup.height(),
    )


def test_the_text_gets_exactly_the_room_it_needs_and_no_blank_gap(qapp):
    """Reported 2026-10-01: a reminder card ("Follow the 20-20-20 Rule") had a
    large empty band between its title and its message. The title had been
    measured at an intermediate, too-narrow width and given lines it did not
    use. Each label is now exactly as tall as its text at its real width."""
    popup = ToastPopup()
    popup.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    for title, message in (
        ("👁️ Follow the 20-20-20 Rule", "Every 20 minutes, look at something about 20 feet away for 20 seconds."),
        ("💧 Drink Water", "Keep a water bottle nearby and stay hydrated throughout the day."),
        ("Standup", "Daily standup in 5 minutes."),
        ("🤲 Stretch Your Hands & Wrists", "Stretch your fingers, wrists, and arms to reduce stiffness."),
    ):
        popup.present(title, message, "info")
        for _ in range(3):
            qapp.processEvents()
        needed_title = popup._title.heightForWidth(popup._title.width())
        needed_message = popup._message.heightForWidth(popup._message.width())
        # Exactly the text's height -- the title is only ever as short as the
        # close button beside it, never taller than its text calls for.
        assert popup._title.height() == max(needed_title, popup._CLOSE_SIZE), title
        assert popup._message.height() == needed_message, title
    popup.deleteLater()


def test_a_reused_card_returns_to_the_size_a_fresh_one_would_have(qapp):
    """The application keeps one card and reuses it. A short notification shown
    after a tall one must be as short as it would be on its own -- it used to
    stay as tall as the one before, or lag a notification behind."""
    notices = [
        ("🤲 Stretch Your Hands & Wrists", "Stretch your fingers, wrists, and arms to reduce stiffness from typing and mouse use."),
        ("Standup", "Daily standup in 5 minutes."),
        ("💧 Drink Water", "Keep a water bottle nearby and stay hydrated throughout the day."),
        ("Standup", "Daily standup in 5 minutes."),
    ]
    reused = ToastPopup()
    reused.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    for title, message in notices:
        reused.present(title, message, "info")
        for _ in range(3):
            qapp.processEvents()
        fresh = ToastPopup()
        fresh.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        fresh.present(title, message, "info")
        for _ in range(3):
            qapp.processEvents()
        assert _fit_report(reused) == _fit_report(fresh), title
        fresh.deleteLater()
    reused.deleteLater()
