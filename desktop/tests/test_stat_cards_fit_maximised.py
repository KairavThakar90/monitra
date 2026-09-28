"""
The summary cards fit on one line wherever the window has the width for it.

The defect: each card's floor was a 190px constant for its value column,
with a note that "01:02:05" in the mono clock face measured 184px. It does --
on the offscreen test platform on Windows, which resolves no font at all and
draws every glyph as a 23px box. On the Windows font engine it measures
104px. Four cards each ~80px wider than they needed to be put the one-row
threshold at 1226px, above the ~1163px of content a maximised window has on
a 1920x1080 display at 125% scaling, so the cards wrapped to two rows there,
206px tall, and took the height from the task list and the Activity panel
underneath.

The floor is now measured from the font, per card, from the widest value the
card must show whole, plus the card's own border. The normal 1280x800 window
is unchanged (two rows); the maximised one gets one.
"""
import pytest
from PySide6.QtGui import QFont, QFontInfo
from PySide6.QtWidgets import QApplication

from ui.stat_cards import (
    ACTIVE_CARD_EXTRA_WIDTH, ACTIVE_CARD_WIDEST_VALUE, ACTIVITY_CARD_WIDEST_VALUE,
    STATUS_CARD_WIDEST_VALUE, TOTAL_CARD_WIDEST_VALUE, StatCardsRow, _CARD_CHROME_WIDTH,
    text_width, value_width,
)

#: 1920x1080 at 125% is 1536 logical pixels wide; the sidebar takes 300 and
#: the content margins 40.
SCALED_1080P_MAXIMISED_CONTENT_WIDTH = 1536 - 300 - 40
#: The default window: 1280 wide, same sidebar and margins.
DEFAULT_WINDOW_CONTENT_WIDTH = 1280 - 300 - 40


def real_value_fonts_available() -> bool:
    """Whether this platform resolves the cards' value fonts to real font
    families. The offscreen platform on Windows resolves none -- every glyph
    is a 23px box -- so a width measured there describes no screen a user
    has, and a test that asserts a pixel width against a real screen has
    nothing to say. Linux and macOS substitute a real family, and the test
    then holds them to it."""
    return all(
        QFontInfo(QFont(family, 17)).family()
        for family in ("Consolas", "Segoe UI")
    )


def _require_real_fonts() -> None:
    """Called inside a test, once the QApplication exists: a QFont made at
    import time, before it, aborts the interpreter."""
    if not real_value_fonts_available():
        pytest.skip("this platform resolves no real font; every glyph is a 23px box")


def _laid_out(row, width):
    row.resize(width, row.sizeHint().height())
    row.show()
    QApplication.processEvents()
    return row


def _filled(row):
    """Every card showing its widest fixed value."""
    row.set_total_seconds(3_725, True)
    row.set_project_status(STATUS_CARD_WIDEST_VALUE, "#3B82F6")
    row.set_active_task(None, None)
    row.set_today_activity(100, has_measurement=True, is_tracking=True)
    return row


def _border(card) -> int:
    margins = card.contentsMargins()
    return margins.left() + margins.right()


def _caption_width(card) -> int:
    return text_width(card._caption.full_text(), card._caption.font())


def test_each_floor_is_measured_from_the_font_and_the_border(qapp):
    row = StatCardsRow()
    status, total, active, activity = row._cards
    assert _border(status) == 2, "the 1px style-sheet border must be counted"
    assert status.minimumWidth() == _CARD_CHROME_WIDTH + 2 + max(
        value_width(STATUS_CARD_WIDEST_VALUE), _caption_width(status)
    )
    assert total.minimumWidth() == _CARD_CHROME_WIDTH + 2 + max(
        value_width(TOTAL_CARD_WIDEST_VALUE, mono=True), _caption_width(total)
    )
    assert activity.minimumWidth() == _CARD_CHROME_WIDTH + 2 + max(
        value_width(ACTIVITY_CARD_WIDEST_VALUE), _caption_width(activity)
    )
    assert active.minimumWidth() == _CARD_CHROME_WIDTH + 2 + max(
        value_width(ACTIVE_CARD_WIDEST_VALUE), _caption_width(active)
    ) + ACTIVE_CARD_EXTRA_WIDTH
    row.deleteLater()


def test_the_captions_are_whole_at_the_one_row_floor(qapp):
    """"TODAY'S ACTIVITY" was elided to "TODAY'S A..." on the reported
    screen: the value alone left the caption no room."""
    row = _filled(StatCardsRow())
    _laid_out(row, row.SINGLE_ROW_MINIMUM_WIDTH)
    for card in row._cards:
        assert card._caption.text() == card._caption.full_text()
    row.hide()
    row.deleteLater()


def test_the_thresholds_follow_the_floors(qapp):
    row = StatCardsRow()
    cards = row._cards
    assert row.SINGLE_ROW_MINIMUM_WIDTH == sum(c.minimumWidth() for c in cards) + 14 * 3
    assert row.TWO_COLUMN_MINIMUM_WIDTH == (
        max(cards[0].minimumWidth(), cards[2].minimumWidth())
        + max(cards[1].minimumWidth(), cards[3].minimumWidth()) + 14
    )
    assert row.minimumWidth() == row.TWO_COLUMN_MINIMUM_WIDTH
    row.deleteLater()


def test_every_value_is_whole_at_the_one_row_floor(qapp):
    """The point of a measured floor: at the narrowest one-row width, no
    card's fixed value is elided -- including "No active task" beside the
    break button, which the old 40px allowance shortened. Holds on every
    platform, because the floor is measured on the same one."""
    row = _filled(StatCardsRow())
    _laid_out(row, row.SINGLE_ROW_MINIMUM_WIDTH)
    assert row.columns() == len(row._cards)
    assert row.total_card._value.text() == "01:02:05"
    assert row.status_card._value.text() == STATUS_CARD_WIDEST_VALUE
    assert row.active_card._value.text() == ACTIVE_CARD_WIDEST_VALUE
    assert row.activity_card._value.text() == ACTIVITY_CARD_WIDEST_VALUE
    assert not row.active_card._value.toolTip(), "nothing hidden behind an ellipsis"
    row.hide()
    row.deleteLater()


def test_every_value_is_whole_at_the_two_column_floor(qapp):
    row = _filled(StatCardsRow())
    _laid_out(row, row.TWO_COLUMN_MINIMUM_WIDTH)
    assert row.columns() == 2
    assert row.total_card._value.text() == "01:02:05"
    assert row.active_card._value.text() == ACTIVE_CARD_WIDEST_VALUE
    row.hide()
    row.deleteLater()


def test_a_maximised_scaled_1080p_display_gets_one_row(qapp):
    """The reported screen: the cards wrapped to two rows there."""
    _require_real_fonts()
    row = _filled(StatCardsRow())
    _laid_out(row, SCALED_1080P_MAXIMISED_CONTENT_WIDTH)
    assert row.columns() == len(row._cards), (
        f"one row needs {row.SINGLE_ROW_MINIMUM_WIDTH}px, the screen has "
        f"{SCALED_1080P_MAXIMISED_CONTENT_WIDTH}px"
    )
    row.hide()
    row.deleteLater()


def test_the_default_window_is_unchanged(qapp):
    """Two rows at 1280x800, as before: only the wider layouts change."""
    row = _filled(StatCardsRow())
    _laid_out(row, DEFAULT_WINDOW_CONTENT_WIDTH)
    assert row.columns() == 2
    row.hide()
    row.deleteLater()
