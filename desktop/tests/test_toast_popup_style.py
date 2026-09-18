"""
The accent bar's colour matches the level a brand-new card is first shown at.

`ToastPopup` used to call `_apply_style()` once in `__init__` (the default
"info" colour) and again in `present()` (the level the caller actually
asked for). Qt applies a widget's very *first* `setStyleSheet()` on its
first paint one call late -- so with two calls made before that first
paint, the paint used the constructor's colour, not `present()`'s. Every
real caller constructs a popup and calls `present()` in the same breath
(`NotificationService._ensure_popup()` then `.present(...)`), so this was
not a lab curiosity: the very first notification of a session always
rendered its accent bar as the default blue, whatever level it actually
was, and only later notifications on the same (reused) popup showed the
right colour. Fixed by never styling the widget before `present()` gives
it real content. These tests render each level on a *freshly constructed*
popup -- the exact shape of the bug -- and read the accent bar's own pixel.
"""
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor

from background_services.notifications.toast_popup import (
    ToastPopup, _LEVEL_ACCENTS,
)


def _accent_color(qapp, popup: ToastPopup) -> QColor:
    """The level colour, read from the card's own left border.

    There is no separate accent widget any more -- see `ToastPopup._apply_style`
    for why -- so the colour lives in the card's `border-left`. Read a
    couple of pixels in from the card's left edge, at its vertical centre,
    which is inside that 4px border and clear of the top/bottom rounded
    corners. `_card.geometry()` is already in the popup's own coordinates
    (the card is `popup`'s direct child), so no mapping is needed.
    """
    popup.show()
    for _ in range(4):
        qapp.processEvents()
    image = popup.grab().toImage()
    card = popup._card.geometry()
    point = card.topLeft() + QPoint(2, card.height() // 2)
    return image.pixelColor(point)


def test_a_brand_new_popups_first_present_shows_its_own_level(qapp):
    for level, hex_colour in _LEVEL_ACCENTS.items():
        popup = ToastPopup()  # fresh instance: never shown before
        assert popup.present("Monitra", "message", level)
        assert _accent_color(qapp, popup) == QColor(hex_colour), (
            f"a fresh popup's first present({level!r}) did not show {hex_colour}"
        )
        popup.hide()
        popup.deleteLater()


def test_a_reused_popup_still_updates_on_every_later_present(qapp):
    """The fix must not just move the bug to the second call."""
    popup = ToastPopup()
    assert popup.present("Monitra", "one", "success")
    assert _accent_color(qapp, popup) == QColor(_LEVEL_ACCENTS["success"])

    assert popup.present("Monitra", "two", "error")
    assert _accent_color(qapp, popup) == QColor(_LEVEL_ACCENTS["error"])

    assert popup.present("Monitra", "three", "warning")
    assert _accent_color(qapp, popup) == QColor(_LEVEL_ACCENTS["warning"])

    popup.hide()
    popup.deleteLater()


def test_an_unrecognised_level_falls_back_to_the_info_colour(qapp):
    popup = ToastPopup()
    assert popup.present("Monitra", "message", "not-a-real-level")
    assert _accent_color(qapp, popup) == QColor(_LEVEL_ACCENTS["info"])
    popup.hide()
    popup.deleteLater()
