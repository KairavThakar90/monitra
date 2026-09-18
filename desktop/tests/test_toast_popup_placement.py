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


def test_a_short_message_sits_at_the_corner_margin(qapp):
    popup = ToastPopup()
    card, area = _present(qapp, popup, SHORT)
    assert card.width() == ToastPopup.WIDTH
    assert card.x() + card.width() == area.x() + area.width() - ToastPopup.SCREEN_MARGIN
    assert card.y() + card.height() == area.y() + area.height() - ToastPopup.SCREEN_MARGIN
    assert area.contains(card), (card, area)
    popup.hide()
    popup.deleteLater()


def test_the_close_button_is_on_screen(qapp):
    """The part that was cut off: the header's right end."""
    popup = ToastPopup()
    card, area = _present(qapp, popup, SHORT)
    close_right = popup._close.mapToGlobal(popup._close.rect().topRight()).x()
    assert close_right <= area.x() + area.width() - ToastPopup.SCREEN_MARGIN
    popup.hide()
    popup.deleteLater()


def test_a_wrapped_message_grows_downward_and_stays_on_screen(qapp):
    popup = ToastPopup()
    short_card, _ = _present(qapp, popup, SHORT)
    long_card, area = _present(qapp, popup, LONG)
    assert long_card.height() > short_card.height(), "the long text must wrap onto more lines"
    assert long_card.width() == ToastPopup.WIDTH
    assert area.contains(long_card), (long_card, area)
    assert long_card.x() + long_card.width() == area.x() + area.width() - ToastPopup.SCREEN_MARGIN
    assert long_card.y() + long_card.height() == area.y() + area.height() - ToastPopup.SCREEN_MARGIN
    popup.hide()
    popup.deleteLater()


def test_replacing_a_long_message_with_a_short_one_repositions(qapp):
    """One card is reused for every notification; each must be re-placed."""
    popup = ToastPopup()
    _present(qapp, popup, LONG)
    card, area = _present(qapp, popup, MEDIUM)
    assert area.contains(card), (card, area)
    assert card.y() + card.height() == area.y() + area.height() - ToastPopup.SCREEN_MARGIN
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
