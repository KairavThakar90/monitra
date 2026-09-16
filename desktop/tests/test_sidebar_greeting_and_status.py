"""
Coverage for the sidebar's greeting block, the centred day total, and the
colour the idle tracking state is drawn in.

Three behaviours are asserted here:

* **Idle reads as red.** The status pill under the day's total used to draw
  idle in `SIDEBAR_MUTED` — the same grey as the "TOTAL TIME TODAY" caption
  beside it — so "not tracking" looked like a caption rather than a state.
  Idle is `ERROR` and active is `SUCCESS`, and the dot and the word always
  agree, because both come from one branch in `set_timer_active`.

* **The day's total is centred.** The caption, the hero duration and the
  status pill share one horizontal centre. The pill is the easy one to get
  wrong: centring a row whose only stretch sits *after* its widgets leaves
  them pinned to the left of a block that is otherwise centred.

* **The greeting follows the IST clock, on an edge.** `core.time_format`
  decides where the afternoon ends — the widget never spells the boundaries
  itself — and the sidebar's watchdog repaints only when the greeting has
  actually changed. A level-triggered version of that slot is the shape that
  caused the worker storm in DO_NOT_DO.md.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from PySide6.QtCore import Qt

from core.time_format import IST, ist_greeting, ist_part_of_day
from ui.sidebar import GREETING_CHECK_MS, SidebarWidget
from ui.styles import ERROR, SUCCESS


SIDEBAR_HEIGHT = 800


def _drain(qapp):
    for _ in range(6):
        qapp.processEvents()


@pytest.fixture
def sidebar(qapp):
    widget = SidebarWidget()
    widget.resize(300, SIDEBAR_HEIGHT)
    widget.show()
    _drain(qapp)
    yield widget
    widget.deleteLater()


def _status_color(sidebar) -> str:
    """The colour the status word is styled in, read back from the widget."""
    return sidebar._status_text.styleSheet().split("color:")[1].split(";")[0].strip()


# ── idle is red ──────────────────────────────────────────────────────────────

def test_idle_is_drawn_in_error_red(sidebar, qapp):
    sidebar.set_timer_active(False)
    _drain(qapp)
    assert sidebar._status_text.text() == "Idle"
    assert _status_color(sidebar) == ERROR


def test_the_freshly_built_sidebar_is_already_idle_red(sidebar):
    """The initial paint goes through the same branch, not a second spelling
    of the idle look in `_build_ui`."""
    assert sidebar._status_text.text() == "Idle"
    assert _status_color(sidebar) == ERROR


def test_active_is_still_success_green(sidebar, qapp):
    sidebar.set_timer_active(True)
    _drain(qapp)
    assert sidebar._status_text.text() == "Active"
    assert _status_color(sidebar) == SUCCESS


def test_the_status_survives_repeated_transitions(sidebar, qapp):
    for active in (True, False, False, True, True, False):
        sidebar.set_timer_active(active)
        _drain(qapp)
        assert _status_color(sidebar) == (SUCCESS if active else ERROR)
        assert sidebar._status_text.text() == ("Active" if active else "Idle")
        assert not sidebar._status_dot.pixmap().isNull()


# ── the day's total is centred ───────────────────────────────────────────────

def test_the_total_and_its_caption_are_centre_aligned(sidebar):
    from PySide6.QtWidgets import QLabel

    assert sidebar._time_display.alignment() & Qt.AlignmentFlag.AlignHCenter

    captions = [
        label for label in sidebar._time_section.findChildren(QLabel)
        if label.text() == "Total Time Today"
    ]
    assert len(captions) == 1
    assert captions[0].alignment() & Qt.AlignmentFlag.AlignHCenter


def test_the_hero_duration_sits_on_the_section_centre_line(sidebar, qapp):
    sidebar.set_total_seconds(3661)
    _drain(qapp)
    assert sidebar._time_display.text() == "01:01:01"

    section_centre = sidebar._time_section.width() / 2
    label_centre = sidebar._time_display.x() + sidebar._time_display.width() / 2
    assert abs(label_centre - section_centre) <= 1


def test_the_status_pill_is_centred_rather_than_left_pinned(sidebar, qapp):
    sidebar.set_timer_active(True)
    _drain(qapp)

    section_centre = sidebar._time_section.width() / 2
    pill_left = sidebar._status_dot.x()
    pill_right = sidebar._status_text.x() + sidebar._status_text.width()
    pill_centre = (pill_left + pill_right) / 2

    assert abs(pill_centre - section_centre) <= 2, "the dot + word must centre as one unit"
    assert pill_left > sidebar._time_section.contentsRect().left() + 20, (
        "a left-pinned pill is the regression this guards"
    )


# ── the greeting maps IST hours to the four parts of the day ─────────────────

@pytest.mark.parametrize(
    "hour, expected",
    [
        (0, "night"), (4, "night"),
        (5, "morning"), (9, "morning"), (11, "morning"),
        (12, "afternoon"), (16, "afternoon"),
        (17, "evening"), (20, "evening"),
        (21, "night"), (23, "night"),
    ],
)
def test_every_ist_hour_maps_to_the_right_part_of_day(hour, expected):
    moment = datetime(2026, 9, 15, hour, 30, tzinfo=IST)
    assert ist_part_of_day(moment) == expected
    assert ist_greeting(moment) == f"Good {expected}"


def test_the_part_of_day_is_ist_and_not_the_machine_zone():
    """06:00 UTC is 11:30 IST — morning — not the small hours."""
    assert ist_part_of_day(datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)) == "morning"
    # 18:00 UTC is 23:30 IST, which is night even though it is evening in UTC.
    assert ist_part_of_day(datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc)) == "night"


def test_a_naive_instant_is_read_as_utc():
    assert ist_part_of_day(datetime(2026, 9, 15, 6, 0)) == "morning"


def test_the_four_parts_cover_the_whole_day():
    seen = {ist_part_of_day(datetime(2026, 9, 15, h, 0, tzinfo=IST)) for h in range(24)}
    assert seen == {"morning", "afternoon", "evening", "night"}


# ── the greeting block in the sidebar ────────────────────────────────────────

def test_the_greeting_is_hidden_until_a_session_supplies_a_name(sidebar, qapp):
    assert not sidebar._greeting_section.isVisible()
    assert sidebar._welcome_label.text() == ""


def test_signing_in_shows_the_welcome_line_and_the_greeting(sidebar, qapp):
    sidebar.set_user({"name": "Kairav Thakar", "email": "k@example.com"})
    _drain(qapp)

    assert sidebar._greeting_section.isVisible()
    assert sidebar._welcome_label.text() == "Welcome Kairav!"
    assert sidebar._greeting_label.text() == ist_greeting()
    assert sidebar._greeting_label.text().startswith("Good ")


def test_the_greeting_sits_above_the_day_total(sidebar, qapp):
    sidebar.set_user({"name": "Kairav Thakar", "email": "k@example.com"})
    _drain(qapp)
    assert sidebar._greeting_section.y() < sidebar._time_section.y()
    assert sidebar._greeting_section.y() >= sidebar._header_widget.height()


def test_the_welcome_line_and_the_account_card_name_the_same_user(sidebar, qapp):
    sidebar.set_user({"username": "Priya Sharma", "email": "p@example.com"})
    _drain(qapp)
    assert sidebar._welcome_label.text() == "Welcome Priya!"
    assert sidebar._user_name_label.text() == "Priya Sharma"


def test_a_single_word_name_still_greets(sidebar, qapp):
    sidebar.set_user({"name": "Admin", "email": "a@example.com"})
    _drain(qapp)
    assert sidebar._welcome_label.text() == "Welcome Admin!"


def test_a_nameless_session_is_not_greeted_with_a_placeholder(sidebar, qapp):
    sidebar.set_user({"name": "", "email": "a@example.com"})
    _drain(qapp)
    assert not sidebar._greeting_section.isVisible()
    assert sidebar._welcome_label.text() == ""


def test_a_long_name_elides_rather_than_widening_the_column(sidebar, qapp):
    sidebar.set_user({
        "name": "Bartholomew Featherstonehaugh-Cholmondeley",
        "email": "b@example.com",
    })
    _drain(qapp)
    assert sidebar.width() == 300
    assert sidebar._welcome_label.width() <= sidebar._greeting_section.width()


def test_the_greeting_is_centred_in_the_column(sidebar, qapp):
    sidebar.set_user({"name": "Kairav Thakar", "email": "k@example.com"})
    _drain(qapp)
    assert sidebar._welcome_label._align == Qt.AlignmentFlag.AlignHCenter
    assert sidebar._greeting_label.alignment() & Qt.AlignmentFlag.AlignHCenter


# ── the rollover watchdog ────────────────────────────────────────────────────

def test_the_watchdog_is_a_ui_only_timer_on_a_coarse_interval(sidebar):
    assert sidebar._greeting_timer.isActive()
    assert sidebar._greeting_timer.interval() == GREETING_CHECK_MS
    assert GREETING_CHECK_MS >= 30_000, "a greeting changes four times a day"
    assert sidebar._greeting_timer.parent() is sidebar


def test_the_watchdog_repaints_nothing_when_the_greeting_is_unchanged(sidebar, qapp, monkeypatch):
    sidebar.set_user({"name": "Kairav Thakar", "email": "k@example.com"})
    _drain(qapp)

    writes = []
    original = type(sidebar._greeting_label).setText
    monkeypatch.setattr(
        type(sidebar._greeting_label), "setText",
        lambda self, text: (writes.append(text), original(self, text))[1],
    )

    for _ in range(20):
        sidebar._check_greeting_rollover()

    assert writes == [], "an unchanged greeting must not touch the label"


def test_the_watchdog_follows_a_crossing_into_the_next_part_of_day(sidebar, qapp, monkeypatch):
    sidebar.set_user({"name": "Kairav Thakar", "email": "k@example.com"})
    _drain(qapp)

    clock = {"now": datetime(2026, 9, 15, 9, 0, tzinfo=IST)}
    monkeypatch.setattr("ui.sidebar.ist_greeting", lambda: ist_greeting(clock["now"]))

    sidebar._render_greeting()
    assert sidebar._greeting_label.text() == "Good morning"

    # Still morning: no transition, no change.
    clock["now"] = datetime(2026, 9, 15, 11, 59, tzinfo=IST)
    sidebar._check_greeting_rollover()
    assert sidebar._greeting_label.text() == "Good morning"

    for hour, expected in ((12, "Good afternoon"), (17, "Good evening"), (21, "Good night")):
        clock["now"] = datetime(2026, 9, 15, hour, 0, tzinfo=IST)
        sidebar._check_greeting_rollover()
        assert sidebar._greeting_label.text() == expected

    # Overnight into the next morning — the case a machine left signed in hits.
    clock["now"] = datetime(2026, 9, 16, 7, 0, tzinfo=IST)
    sidebar._check_greeting_rollover()
    assert sidebar._greeting_label.text() == "Good morning"


def test_the_watchdog_does_not_reveal_the_block_for_a_signed_out_sidebar(sidebar, monkeypatch):
    """Visibility depends on the session, not the clock."""
    clock = {"now": datetime(2026, 9, 15, 9, 0, tzinfo=IST)}
    monkeypatch.setattr("ui.sidebar.ist_greeting", lambda: ist_greeting(clock["now"]))
    sidebar._render_greeting()
    clock["now"] = datetime(2026, 9, 15, 14, 0, tzinfo=IST)
    sidebar._check_greeting_rollover()
    assert not sidebar._greeting_section.isVisible()


# ── collapse ─────────────────────────────────────────────────────────────────

def test_collapsing_hides_the_greeting_and_expanding_restores_it(sidebar, qapp):
    sidebar.set_user({"name": "Kairav Thakar", "email": "k@example.com"})
    _drain(qapp)
    assert sidebar._greeting_section.isVisible()

    sidebar.toggle_collapse()
    _drain(qapp)
    assert not sidebar._greeting_section.isVisible()
    assert not sidebar._time_section.isVisible()

    sidebar.toggle_collapse()
    _drain(qapp)
    assert sidebar._greeting_section.isVisible()
    assert sidebar._welcome_label.text() == "Welcome Kairav!"


def test_expanding_a_signed_out_sidebar_leaves_the_block_hidden(sidebar, qapp):
    sidebar.toggle_collapse()
    _drain(qapp)
    sidebar.toggle_collapse()
    _drain(qapp)
    assert not sidebar._greeting_section.isVisible()


# ── the greeting block does not disturb the column's anchors ─────────────────

def test_showing_the_greeting_does_not_move_the_account_card_or_footer(sidebar, qapp):
    sidebar.set_projects([{"id": i, "project_name": f"P{i}"} for i in range(1, 6)])
    _drain(qapp)
    before = (sidebar._user_card.y(), sidebar._sync_row.y())

    sidebar.set_user({"name": "Kairav Thakar", "email": "k@example.com"})
    _drain(qapp)

    assert (sidebar._user_card.y(), sidebar._sync_row.y()) == before
