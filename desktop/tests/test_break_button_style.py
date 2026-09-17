"""
The Break In / Break Out button's look.

Break In used to be an outlined white button while Break Out was the brand
blue, so the same control changed colour with its label. Both now share the
blue, and the enabled button wears a thin blue -> violet gradient ring so it
is the highlighted control on the ACTIVE TASK card whichever way it reads.
The ring is painted, not styled -- a QSS border colour cannot be a gradient
in this Qt build -- so these tests sample rendered pixels rather than the
stylesheet: the left edge leans blue, the right edge leans violet, the
middle is the brand blue, and a disabled button carries no ring at all.
"""
from __future__ import annotations

import sys

import pytest

from PySide6.QtGui import QColor

from background_services.public_api import BreakStatus
from ui.break_button import BreakButton
from ui.styles import BRAND_BLUE, BRAND_VIOLET, PRIMARY, PRIMARY_LIGHT


#: These read pixels back from the offscreen render. The gradient ring and
#: the flat fills are painted the same way everywhere, but the macOS release
#: runners hand back a different image for a stylesheet-driven QPushButton
#: under QT_QPA_PLATFORM=offscreen (the centre pixel is 300 units from the
#: fill it asks for), so the pixel assertions pin the Windows render only.
_pixels = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="pixel read-back of a stylesheet-driven button differs on macOS offscreen",
)


def _shown(qapp, break_status=BreakStatus.NONE, timer_active=True):
    button = BreakButton()
    button.set_state(break_status, timer_active)
    button.show()
    for _ in range(4):
        qapp.processEvents()
    return button


def _pixel(button, x, y) -> QColor:
    return button.grab().toImage().pixelColor(x, y)


def _distance(a: QColor, b: QColor) -> float:
    return abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())


@_pixels
def test_break_in_and_break_out_share_the_brand_blue(qapp):
    break_in = _shown(qapp, BreakStatus.NONE, timer_active=True)
    break_out = _shown(qapp, BreakStatus.ON_BREAK, timer_active=False)
    assert break_in.text() == "Break In" and break_out.text() == "Break Out"
    assert break_in.styleSheet() == break_out.styleSheet()
    assert f"background: {PRIMARY};" in break_in.styleSheet()
    centre_in = _pixel(break_in, break_in.width() // 4, break_in.height() // 2)
    centre_out = _pixel(break_out, break_out.width() // 4, break_out.height() // 2)
    assert _distance(centre_in, QColor(PRIMARY)) < 30
    assert _distance(centre_out, QColor(PRIMARY)) < 30


def test_the_enabled_button_wears_a_gradient_ring(qapp):
    button = _shown(qapp, BreakStatus.NONE, timer_active=True)
    y = button.height() // 2
    left = _pixel(button, 0, y)
    right = _pixel(button, button.width() - 1, y)
    # The ring is the brand gradient: blue at the left edge, violet at the right.
    assert _distance(left, QColor(BRAND_BLUE)) < _distance(left, QColor(BRAND_VIOLET))
    assert _distance(right, QColor(BRAND_VIOLET)) < _distance(right, QColor(BRAND_BLUE))
    # And it is a ring, not a fill: just inside it the card shows through
    # before the blue begins, so the outline reads as an outline.
    gap = _pixel(button, 2, y)
    assert gap.lightness() > QColor(PRIMARY).lightness() + 40


@_pixels
def test_a_disabled_button_has_no_ring(qapp):
    button = _shown(qapp, BreakStatus.NONE, timer_active=False)
    assert not button.isEnabled()
    y = button.height() // 2
    edge = _pixel(button, 0, y)
    # No gradient paint at the edge: it is the light disabled fill's
    # surroundings, nowhere near the ring's violet or blue.
    assert _distance(edge, QColor(BRAND_VIOLET)) > 60
    assert _distance(edge, QColor(BRAND_BLUE)) > 60
    centre = _pixel(button, button.width() // 4, y)
    assert _distance(centre, QColor(PRIMARY_LIGHT)) < 30
