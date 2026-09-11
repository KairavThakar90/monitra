"""
Coverage for the top bar's date filter pill (chevrons around a calendar-picker
button, plus a "Today" shortcut that only exists while another day is shown)
and the Request button that replaced the task section's "Log Time".

The rule this file now pins down is **today is the maximum selectable date**.
The header previously let the user browse into a future day and read an empty
state there; the tests below replace the ones that asserted that, because
"today or earlier" is a business rule about what may be acted on, not a
presentation choice — a day the timer refuses to track against is not a day the
header should offer. Every route into a future date is covered here: the
chevron, the calendar, a programmatic call, and the midnight rollover.
"""
from datetime import timedelta

from PySide6.QtCore import QDate

from core.time_format import ist_today
from ui.topbar import TopBar


def test_today_shortcut_is_hidden_on_today_and_shown_on_a_past_date(qapp):
    bar = TopBar()
    assert bar._today_btn.isHidden()

    bar._on_prev_day()
    assert not bar._today_btn.isHidden()
    assert bar.selected_date == ist_today() - timedelta(days=1)


def test_today_shortcut_returns_to_today_and_emits_once(qapp):
    bar = TopBar()
    bar._on_prev_day()

    seen = []
    bar.date_changed.connect(seen.append)
    bar._today_btn.click()

    assert seen == [ist_today()]
    assert bar._today_btn.isHidden()


def test_selecting_the_date_already_shown_emits_nothing(qapp):
    """Picking today's date from the calendar while today is displayed must
    not trigger a reload of data that is already on screen."""
    bar = TopBar()
    seen = []
    bar.date_changed.connect(seen.append)
    bar._set_selected_date(ist_today())
    assert seen == []


# ── today is the maximum selectable date ─────────────────────────────────────

def test_tomorrow_is_rejected(qapp):
    bar = TopBar()
    seen = []
    bar.date_changed.connect(seen.append)

    assert bar._set_selected_date(ist_today() + timedelta(days=1)) is False
    assert bar.selected_date == ist_today()
    assert seen == []


def test_a_date_several_days_ahead_is_rejected(qapp):
    bar = TopBar()
    seen = []
    bar.date_changed.connect(seen.append)

    assert bar._set_selected_date(ist_today() + timedelta(days=30)) is False
    assert bar.selected_date == ist_today()
    assert seen == []


def test_the_next_chevron_is_disabled_on_today(qapp):
    bar = TopBar()
    assert not bar.next_btn.isEnabled()


def test_clicking_next_on_today_cannot_move_the_selection(qapp):
    """The chevron is disabled, but a click delivered from a queue would still
    reach the slot -- so the slot itself must refuse, not merely be
    unreachable."""
    bar = TopBar()
    bar._on_next_day()
    assert bar.selected_date == ist_today()


def test_the_next_chevron_is_live_on_a_past_date_and_stops_at_today(qapp):
    bar = TopBar()
    bar._on_prev_day()
    assert bar.next_btn.isEnabled()

    bar._on_next_day()
    assert bar.selected_date == ist_today()
    assert not bar.next_btn.isEnabled()


def test_rapid_forward_clicks_never_pass_today(qapp):
    bar = TopBar()
    bar._set_selected_date(ist_today() - timedelta(days=2))
    for _ in range(10):
        bar._on_next_day()
    assert bar.selected_date == ist_today()


def test_the_calendar_popup_caps_at_today(qapp):
    """`setMaximumDate` is what refuses the click *and* the keyboard inside the
    popup: Qt will not move the cursor past the maximum with arrow keys, Page
    Down or End, so the picker offers no second route into a future day."""
    bar = TopBar()
    calendar = bar.build_calendar()
    today = ist_today()

    assert calendar.maximumDate() == QDate(today.year, today.month, today.day)


def test_the_calendar_refuses_a_future_date_set_on_it_directly(qapp):
    """Qt's own enforcement of the cap, asserted rather than assumed: a
    selection past the maximum is clamped, not honoured."""
    bar = TopBar()
    calendar = bar.build_calendar()
    tomorrow = ist_today() + timedelta(days=1)

    calendar.setSelectedDate(QDate(tomorrow.year, tomorrow.month, tomorrow.day))

    assert calendar.selectedDate() <= calendar.maximumDate()


def test_a_programmatic_selection_is_held_to_the_same_rule(qapp):
    """select_date() is the supported entry point for other components. It is
    not a plain setter: nothing may put the header into a state the user could
    not have reached themselves."""
    bar = TopBar()
    assert bar.select_date(ist_today() + timedelta(days=1)) is False
    assert bar.selected_date == ist_today()

    yesterday = ist_today() - timedelta(days=1)
    assert bar.select_date(yesterday) is True
    assert bar.selected_date == yesterday


def test_an_unreadable_date_is_refused_rather_than_defaulted(qapp):
    bar = TopBar()
    seen = []
    bar.date_changed.connect(seen.append)

    assert bar._set_selected_date("not a date") is False
    assert bar._set_selected_date(None) is False
    assert bar.selected_date == ist_today()
    assert seen == []


def test_an_iso_string_is_accepted_as_the_calendar_day_it_names(qapp):
    bar = TopBar()
    yesterday = ist_today() - timedelta(days=1)
    assert bar._set_selected_date(yesterday.isoformat()) is True
    assert bar.selected_date == yesterday


# ── the midnight boundary ────────────────────────────────────────────────────

def test_a_selection_that_was_today_follows_the_clock_over_midnight(qapp):
    """Left parked on the day that has just become history, the user would find
    Stop disabled with their timer still running and no control to stop it."""
    bar = TopBar()
    seen = []
    bar.date_changed.connect(seen.append)

    # Pretend the window was opened yesterday and has just crossed midnight.
    yesterday = ist_today() - timedelta(days=1)
    bar._today = yesterday
    bar._selected_date = yesterday
    bar._check_day_rollover()

    assert bar.selected_date == ist_today()
    assert seen == [ist_today()]
    assert not bar.next_btn.isEnabled()


def test_a_past_selection_stays_put_over_midnight_and_re_renders(qapp):
    bar = TopBar()
    two_days_ago = ist_today() - timedelta(days=2)
    bar._today = ist_today() - timedelta(days=1)
    bar._selected_date = two_days_ago
    seen = []
    bar.date_changed.connect(seen.append)

    bar._check_day_rollover()

    assert bar.selected_date == two_days_ago
    assert seen == []
    assert bar.next_btn.isEnabled()


def test_the_rollover_check_is_edge_triggered(qapp):
    """It runs once a minute for the life of the window. Emitting on every tick
    of an unchanged day is the level-triggered signal DO_NOT_DO.md records."""
    bar = TopBar()
    seen = []
    bar.date_changed.connect(seen.append)

    for _ in range(5):
        bar._check_day_rollover()

    assert seen == []


def test_date_button_label_follows_the_selection(qapp):
    bar = TopBar()
    bar._on_prev_day()
    yesterday = ist_today() - timedelta(days=1)
    assert str(yesterday.day) in bar._date_btn.text()
    assert yesterday.strftime("%B") in bar._date_btn.text()


def test_date_filter_is_still_the_first_item_in_the_layout(qapp):
    bar = TopBar()
    assert bar.layout().itemAt(0).widget() is bar.date_row


def test_request_button_click_emits_request_clicked(qapp):
    bar = TopBar()
    seen = []
    bar.request_clicked.connect(lambda: seen.append(True))
    bar._request_btn.click()
    assert seen == [True]


def test_request_button_is_labelled_request(qapp):
    bar = TopBar()
    assert bar._request_btn.text().strip() == "Request"
    assert not bar._request_btn.icon().isNull()
