"""
The circular Play / Pause control, the Break button's new home in the ACTIVE
TASK card, the Active/Idle pill on the account row, and the top-of-content
break message.

The rule under test everywhere here: **one timer, whichever control**. The
circular button, the task rows' Start/Stop and Break In/Out all go through
`TimerService`, so they can never disagree with each other, and none of them
can create a second session. Everything runs against the real `TimerService`,
the real durable queue and the real widgets; only the HTTP boundary is faked.
"""
from __future__ import annotations

import gc
from datetime import timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from background_services.public_api import BreakStatus
from core.time_format import ist_today
from tests.test_break_in_out import (  # noqa: F401  (fixtures and helpers)
    PROJECT_A, PROJECT_B, TASKS, _click_break, _drain, _row, dashboard,
)
from tests.test_timer_lifecycle_reliability import (  # noqa: F401  (fixtures and fakes)
    _double_click, _pump, live_runtime,
)
from ui.action_banner import ActionBanner
from ui.dashboard_window import BREAK_ENDED_MESSAGE, BREAK_STARTED_MESSAGE
from ui.stat_cards import StatCardsRow
from ui.styles import SUCCESS, WARNING
from ui.timer_control import (
    CAPTION_HISTORY, CAPTION_ON_BREAK, CAPTION_SELECT_TASK, TimerControl,
)


@pytest.fixture(autouse=True)
def _finalize_dead_qobjects_between_tests(qapp):
    """See tests/test_break_in_out.py: finalize dead QObject graphs between
    tests, outside any Qt callback."""
    yield
    qapp.processEvents()
    gc.collect()
    qapp.processEvents()


# ── The circular control, alone ──────────────────────────────────────────────

@pytest.fixture
def control(qapp):
    widget = TimerControl()
    widget.show()
    _drain(qapp)
    yield widget
    widget.deleteLater()


def _state(control):
    return control.is_running, control.button.isEnabled(), control.caption.text()


def test_the_control_renders_play_pause_and_why_it_is_disabled(control):
    assert _state(control) == (False, False, CAPTION_SELECT_TASK), "nothing to start"

    control.set_state(running=False, can_start=True)
    assert _state(control) == (False, True, "")

    control.set_state(running=True, can_start=True)
    assert _state(control) == (True, True, "")

    control.set_state(running=False, can_start=True, break_status=BreakStatus.ON_BREAK)
    assert _state(control) == (False, False, CAPTION_ON_BREAK)

    control.set_state(running=False, can_start=True, break_status=BreakStatus.RESUMING)
    assert control.button.isEnabled() is False

    control.set_state(running=True, can_start=True, live_date=False)
    assert _state(control) == (True, False, CAPTION_HISTORY)


def test_the_disc_is_circular_and_wears_the_start_stop_gradient(control):
    button = control.button
    assert button.width() == button.height()
    control.set_state(running=False, can_start=True)
    assert f"border-radius: {button.width() // 2}px" in button.styleSheet()
    idle_style = button.styleSheet()
    control.set_state(running=True, can_start=True)
    assert button.styleSheet() != idle_style, "Pause reads differently from Play"
    assert not button.icon().isNull()


def test_play_and_pause_report_intent_and_a_double_click_counts_once(qapp, control):
    starts, stops = [], []
    control.start_requested.connect(lambda: starts.append(1))
    control.stop_requested.connect(lambda: stops.append(1))

    control.set_state(running=False, can_start=True)
    _double_click(qapp, control.button)
    assert (len(starts), len(stops)) == (1, 0)
    assert not control.button.isEnabled(), "held disabled until the state is re-rendered"

    control.set_state(running=True, can_start=True)
    assert _pump(qapp, lambda: control.button.isEnabled()), "re-enabled after the settle window"
    _double_click(qapp, control.button)
    assert (len(starts), len(stops)) == (1, 1)


def test_a_burst_of_clicks_does_one_thing(qapp, control):
    """Three fast taps on Play must not start, then stop, the task."""
    starts, stops = [], []
    control.start_requested.connect(lambda: starts.append(1))
    control.stop_requested.connect(lambda: stops.append(1))
    control.set_state(running=False, can_start=True)

    control.button.click()
    # The service has started the task and the window re-rendered before
    # the second tap lands...
    control.set_state(running=True, can_start=True)
    control.button.click()
    control.button.click()
    assert (len(starts), len(stops)) == (1, 0), "...and those taps must not stop it"

    assert _pump(qapp, lambda: control.button.isEnabled())
    control.button.click()
    assert (len(starts), len(stops)) == (1, 1)


@pytest.mark.parametrize("kwargs", [
    dict(running=False, can_start=False),
    dict(running=False, can_start=True, break_status=BreakStatus.ON_BREAK),
    dict(running=False, can_start=True, break_status=BreakStatus.RESUMING),
    dict(running=False, can_start=True, live_date=False),
    dict(running=True, can_start=True, live_date=False),
])
def test_a_disabled_control_reports_nothing(qapp, control, kwargs):
    starts, stops = [], []
    control.start_requested.connect(lambda: starts.append(1))
    control.stop_requested.connect(lambda: stops.append(1))
    control.set_state(**kwargs)
    _double_click(qapp, control.button)
    control.button.click()
    assert (starts, stops) == ([], [])


# ── The ACTIVE TASK card ─────────────────────────────────────────────────────

def _laid_out(qapp, row, width):
    row.resize(width, row.sizeHint().height())
    row.show()
    _drain(qapp)
    return row


def test_the_break_button_sits_on_the_right_of_the_active_task_card(qapp):
    row = _laid_out(qapp, StatCardsRow(), 1400)
    card, button = row.active_card, row.break_button
    assert button.parent() is card
    value = card._value
    assert value.x() + value.width() <= button.x(), "the name never runs under the button"
    assert button.x() + button.width() <= card.width() - 8, "inside the card, on its right"
    centre = button.y() + button.height() / 2
    assert abs(centre - card.height() / 2) <= 6, "vertically centred in the card"
    row.hide()
    row.deleteLater()


def test_the_active_card_is_wider_and_the_name_keeps_room_at_the_floor(qapp):
    """The button is paid for partly by the card and partly by the name,
    which elides; at the narrowest one-row width the name still has room,
    and above it the active column stretches faster than the others."""
    row = _laid_out(qapp, StatCardsRow(), StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH)
    assert row.columns() == 3
    assert row.active_card.width() > row.total_card.width()
    assert row.active_card._value.width() >= 118
    assert row.active_card._value.x() + row.active_card._value.width() <= row.break_button.x()

    _laid_out(qapp, row, StatCardsRow.SINGLE_ROW_MINIMUM_WIDTH + 200)
    assert row.active_card._value.width() >= 118 + 50, "slack goes to the name first"
    row.hide()
    row.deleteLater()


def test_a_1600px_window_still_shows_the_cards_in_one_row(qapp):
    """1600x900 leaves 1260px of content once the sidebar and margins are
    taken out; the row showed one line there before the button and must
    still."""
    row = _laid_out(qapp, StatCardsRow(), 1600 - 300 - 40)
    assert row.columns() == 3
    row.hide()
    row.deleteLater()


def test_the_card_never_shows_a_held_task_as_running(qapp):
    row = StatCardsRow()
    row.set_active_task_on_break("Write specs", "Project X")
    assert row.active_card._value.full_text() == "Write specs"
    sub = row.active_card._sub
    assert sub.full_text() == "On break · Project X"
    assert WARNING in sub.styleSheet() and SUCCESS not in sub.styleSheet()
    assert "Tracking" not in sub.full_text() and "In progress" not in sub.full_text()

    row.set_active_task_on_break(None, None)
    assert row.active_card._value.full_text() == "Previous task"
    assert row.active_card._sub.full_text() == "On break"
    row.deleteLater()


def test_the_card_renders_the_break_button_from_the_state_it_is_given(qapp):
    row = StatCardsRow()
    button = row.break_button
    assert (button.text(), button.isEnabled()) == ("Break In", False)
    row.set_break_control(BreakStatus.NONE, True)
    assert (button.text(), button.isEnabled()) == ("Break In", True)
    row.set_break_control(BreakStatus.ON_BREAK, False)
    assert (button.text(), button.isEnabled()) == ("Break Out", True)
    row.reset()
    assert (button.text(), button.isEnabled()) == ("Break In", False)
    row.deleteLater()


def test_the_card_forwards_the_buttons_intent(qapp):
    row = StatCardsRow()
    ins, outs = [], []
    row.break_in_requested.connect(lambda: ins.append(1))
    row.break_out_requested.connect(lambda: outs.append(1))
    row.set_break_control(BreakStatus.NONE, True)
    row.break_button.click()
    row.set_break_control(BreakStatus.ON_BREAK, False)
    assert _pump(qapp, lambda: row.break_button.isEnabled())
    row.break_button.click()
    assert (ins, outs) == ([1], [1])
    row.deleteLater()


# ── The top-of-content banner ────────────────────────────────────────────────

@pytest.fixture
def host(qapp):
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

    widget = QWidget()
    layout = QVBoxLayout(widget)
    content = QLabel("content", widget)
    layout.addWidget(content)
    widget.resize(900, 600)
    widget.show()
    _drain(qapp)
    widget.content = content
    yield widget
    widget.deleteLater()


def test_the_banner_shows_the_message_and_dismisses_itself(qapp, host):
    banner = ActionBanner(host)
    assert not banner.isVisible()
    banner._dismiss.setInterval(60)

    banner.show_message(BREAK_STARTED_MESSAGE, "warning")
    _drain(qapp)
    assert banner.isVisible()
    assert banner.message == BREAK_STARTED_MESSAGE
    assert banner.kind == "warning"
    assert WARNING in banner.styleSheet()

    assert _pump(qapp, lambda: not banner.isVisible(), timeout=3.0), "never dismissed itself"


def test_the_banner_is_an_overlay_that_moves_nothing(qapp, host):
    layout_count = host.layout().count()
    content_geometry = host.content.geometry()
    banner = ActionBanner(host)
    banner.show_message(BREAK_ENDED_MESSAGE, "success")
    _drain(qapp)

    assert host.layout().count() == layout_count, "not a row in the layout"
    assert host.content.geometry() == content_geometry, "nothing shifted"
    assert not banner.isWindow() and banner.focusPolicy() == Qt.FocusPolicy.NoFocus
    # Top-centre, inside the host.
    centre = banner.x() + banner.width() / 2
    assert abs(centre - host.width() / 2) <= 1
    assert banner.y() < 40
    assert banner.width() <= host.width()

    host.resize(500, 600)
    _drain(qapp)
    assert banner.width() <= 500 - 2 * 20
    assert abs(banner.x() + banner.width() / 2 - 250) <= 1
    banner.dismiss()
    assert not banner.isVisible()


# ── Task selection ───────────────────────────────────────────────────────────

def _click_row_body(qapp, row):
    QTest.mouseClick(row._name_label, Qt.MouseButton.LeftButton)
    _drain(qapp)


def test_clicking_a_row_selects_it_and_selection_starts_nothing(qapp, dashboard, runtime):
    section = dashboard._task_section
    selected = []
    section.task_selected.connect(selected.append)

    _click_row_body(qapp, _row(dashboard, 11))

    assert selected[-1] == {"project_id": 1, "task_id": 11, "task_name": "Task A2"}
    assert _row(dashboard, 11).is_selected and not _row(dashboard, 10).is_selected
    assert not runtime.timer.is_running()
    assert runtime.backend.started == []
    assert dashboard._sidebar.timer_control_state() == {
        "running": False, "enabled": True, "caption": "",
    }


def test_switching_projects_clears_the_selection_and_disables_play(qapp, dashboard):
    _click_row_body(qapp, _row(dashboard, 11))
    dashboard._on_project_selected(PROJECT_B)
    assert dashboard._task_section.selected_task() is None
    assert dashboard._sidebar.timer_control_state()["enabled"] is False
    assert dashboard._sidebar.timer_control_state()["caption"] == CAPTION_SELECT_TASK


def test_a_completed_task_cannot_be_the_play_target(qapp, dashboard):
    section = dashboard._task_section
    section._selected_task_id = 10
    for task in section._tasks:
        if task["id"] == 10:
            task["status"] = "completed"
    assert section.selected_task() is None


# ── The dashboard: every control drives one timer ────────────────────────────

def _circle(dashboard):
    return dashboard._sidebar._timer_control.button


def _started_tasks(runtime):
    """The task id of each *session* the backend was asked to start, in order.

    One session, one `client_op`. A stop queued before the backend has
    issued an entry id also queues the session's start (so the stop always
    has a start to wait for), and the backend answers that replay with the
    same entry -- so the recording backend may see one session's start
    twice. Distinct sessions are what "no duplicate session" means. A start
    that follows a stop travels through the durable queue, so callers pump
    until the expected sessions have arrived.
    """
    seen, sessions = set(), []
    for entry in runtime.backend.started:
        if entry["client_op"] in seen:
            continue
        seen.add(entry["client_op"])
        sessions.append(entry["task_id"])
    return sessions


def _wait_started(qapp, runtime, expected):
    """Wait for the backend to have been asked to start exactly `expected`.

    A start that follows a stop (Play after Pause, Break Out) travels through
    the durable queue, and the queue consumer polls on its own cadence. While
    we wait we also *drive* the queue -- bring any timer action that is
    waiting out a backoff forward and wake the consumer -- the same nudges
    `ApplicationRuntime.prepare_exit` gives a queued stop. Without this the
    resume start sat in the queue at the idle poll cadence, and on a heavily
    loaded CI runner (the Windows package job measured the suite at ~7.5x its
    local wall-clock) that outran the old fixed budget and failed the assert
    for no fault in the code under test. The assertion is unchanged: the
    backend must still see exactly the expected sessions, in order.
    """
    def ready() -> bool:
        try:
            runtime.cache.make_timer_actions_ready()
        except Exception:  # noqa: BLE001
            pass
        runtime.sync.wake()
        return _started_tasks(runtime) == expected

    assert _pump(qapp, ready, timeout=45.0), (
        f"backend starts: {_started_tasks(runtime)} != {expected}"
    )


def _banner_showing(dashboard) -> bool:
    """The fixture's window is never shown, so a child's `isVisible()` is
    always False; what the banner controls is whether it is hidden."""
    banner = dashboard._action_banner
    return not banner.isHidden() and banner.isVisibleTo(banner.parentWidget())


def _circle_enabled(qapp, dashboard) -> bool:
    """Enabled once the click's settle window has passed."""
    return _pump(qapp, lambda: dashboard._sidebar.timer_control_state()["enabled"])


def _click_circle(qapp, dashboard):
    button = _circle(dashboard)
    assert _pump(qapp, lambda: button.isEnabled()), "the circular control never became clickable"
    button.click()
    _drain(qapp)


def test_a_play_starts_the_selected_task_and_pause_stops_it(qapp, dashboard, runtime):
    """Tests A and B of the acceptance list, through the real widgets."""
    assert dashboard._sidebar.timer_control_state()["enabled"] is False
    _click_row_body(qapp, _row(dashboard, 10))

    _click_circle(qapp, dashboard)  # Play

    assert runtime.timer.is_running() and runtime.timer.task_id == 10
    assert _pump(qapp, lambda: runtime.timer.entry_id == 42), "the start never reached the backend"
    _wait_started(qapp, runtime, [10])
    assert runtime.backend.started[0]["project_id"] == 1
    assert _row(dashboard, 10)._is_running is True
    assert dashboard._task_section._running_task_id == 10
    assert dashboard._sidebar._status_text.text() == "Active"
    assert dashboard._sidebar.timer_control_state()["running"] is True
    assert dashboard._stat_cards.active_card._value.full_text() == "Task A1"
    assert dashboard._stat_cards.break_button.isEnabled()
    assert len(runtime.tracker.started) == 1, "activity capture started with the task"

    _click_circle(qapp, dashboard)  # Pause

    assert not runtime.timer.is_running()
    assert _pump(qapp, lambda: len(runtime.backend.stopped) == 1), "the stop never landed"
    assert len(runtime.backend.started) == 1, "no second session"
    assert _row(dashboard, 10)._is_running is False
    assert dashboard._sidebar._status_text.text() == "Idle"
    assert dashboard._sidebar.timer_control_state()["running"] is False
    assert dashboard._stat_cards.active_card._value.full_text() == "No active task"
    assert not dashboard._stat_cards.break_button.isEnabled()
    assert len(runtime.tracker.stopped) == 1
    # And Play is still live: it resumes the task that was just paused.
    assert _circle_enabled(qapp, dashboard)


def test_play_resumes_the_paused_task_even_from_another_project(qapp, dashboard, runtime):
    _click_row_body(qapp, _row(dashboard, 11))
    _click_circle(qapp, dashboard)
    assert runtime.timer.task_id == 11
    _click_circle(qapp, dashboard)
    assert not runtime.timer.is_running()

    dashboard._on_project_selected(PROJECT_B)
    assert dashboard._task_section.selected_task() is None
    assert _circle_enabled(qapp, dashboard), "the last task is still resumable"

    _click_circle(qapp, dashboard)

    assert runtime.timer.is_running() and runtime.timer.task_id == 11
    assert runtime.timer.active_session()["project_id"] == 1
    assert dashboard._current_project == PROJECT_A, "its project is brought back on screen"
    assert _row(dashboard, 11)._is_running is True
    _wait_started(qapp, runtime, [11, 11])


def test_the_row_start_and_the_circle_stay_in_step(qapp, dashboard, runtime):
    """Test G: the task-row Start button is kept, and the circle follows it."""
    dashboard._task_section._handle_start_request(_row(dashboard, 10))
    assert runtime.timer.task_id == 10
    assert dashboard._sidebar.timer_control_state() == {"running": True, "enabled": True, "caption": ""}
    assert _row(dashboard, 10).is_selected

    _click_circle(qapp, dashboard)  # Pause from the circle
    assert not runtime.timer.is_running()
    assert _row(dashboard, 10)._is_running is False
    assert dashboard._sidebar.timer_control_state()["running"] is False

    _click_circle(qapp, dashboard)  # Play from the circle resumes Task A1
    assert runtime.timer.task_id == 10 and _row(dashboard, 10)._is_running

    dashboard._task_section._handle_stop_request(_row(dashboard, 10))  # Stop from the row
    assert not runtime.timer.is_running()
    assert dashboard._sidebar.timer_control_state()["running"] is False
    _wait_started(qapp, runtime, [10, 10])


def test_starting_another_row_while_running_switches_and_the_circle_follows(qapp, dashboard, runtime):
    _click_row_body(qapp, _row(dashboard, 10))
    _click_circle(qapp, dashboard)
    dashboard._task_section._handle_start_request(_row(dashboard, 11))
    assert runtime.timer.task_id == 11
    assert _row(dashboard, 11)._is_running and not _row(dashboard, 10)._is_running
    assert dashboard._sidebar.timer_control_state()["running"] is True
    _click_circle(qapp, dashboard)
    assert not runtime.timer.is_running()
    assert _pump(qapp, lambda: len(runtime.backend.stopped) == 2)


def test_rapid_clicks_on_the_circle_do_one_thing_each(qapp, dashboard, runtime):
    """Test F: a burst on Play starts once; a burst on Pause stops once."""
    stopped, started = [], []
    runtime.timer.timer_stopped.connect(stopped.append)
    runtime.timer.timer_started.connect(started.append)
    _click_row_body(qapp, _row(dashboard, 10))

    button = _circle(dashboard)
    assert _pump(qapp, lambda: button.isEnabled())
    for _ in range(5):
        button.click()
    _drain(qapp)
    assert len(started) == 1 and len(stopped) == 0
    assert runtime.timer.is_running() and runtime.timer.task_id == 10
    assert _pump(qapp, lambda: len(runtime.backend.started) == 1)

    assert _pump(qapp, lambda: button.isEnabled())
    for _ in range(5):
        button.click()
    _drain(qapp)
    assert len(started) == 1 and len(stopped) == 1
    assert not runtime.timer.is_running()
    assert _pump(qapp, lambda: len(runtime.backend.stopped) == 1)
    assert len(runtime.backend.started) == 1, "no session was created by the burst"


# ── Break In / Break Out from the card, with the circle ──────────────────────

def test_break_in_disables_the_circle_and_break_out_resumes_the_task(qapp, dashboard, runtime):
    """Tests C and D of the acceptance list."""
    banner = dashboard._action_banner
    _click_row_body(qapp, _row(dashboard, 10))
    _click_circle(qapp, dashboard)
    assert runtime.timer.task_id == 10
    assert not _banner_showing(dashboard)

    _click_break(qapp, dashboard)  # Break In

    assert not runtime.timer.is_running()
    assert runtime.timer.break_status == BreakStatus.ON_BREAK
    assert dashboard._stat_cards.break_button.text() == "Break Out"
    assert dashboard._sidebar.timer_control_state() == {
        "running": False, "enabled": False, "caption": CAPTION_ON_BREAK,
    }
    assert _banner_showing(dashboard) and banner.message == BREAK_STARTED_MESSAGE
    assert banner.kind == "warning"
    card = dashboard._stat_cards.active_card
    assert card._value.full_text() == "Task A1"
    assert card._sub.full_text() == "On break · Project A"
    assert dashboard._stat_cards.total_card._sub.full_text() == "Not tracking"

    # The circle cannot start anything during the break, even if its click
    # is forced through.
    _circle(dashboard).click()
    dashboard._on_play_requested()
    _drain(qapp)
    assert not runtime.timer.is_running()
    assert runtime.timer.break_status == BreakStatus.ON_BREAK
    # The one session ever started is the first; nothing new reached the
    # backend (its start may still be landing on the pool -- wait for it).
    _wait_started(qapp, runtime, [10])
    _drain(qapp)
    assert _started_tasks(runtime) == [10]

    _click_break(qapp, dashboard)  # Break Out

    assert _pump(qapp, lambda: runtime.timer.is_running())
    _drain(qapp)
    assert runtime.timer.task_id == 10
    assert runtime.timer.break_status == BreakStatus.NONE
    assert dashboard._stat_cards.break_button.text() == "Break In"
    assert _pump(qapp, lambda: dashboard._stat_cards.break_button.isEnabled())
    assert dashboard._sidebar.timer_control_state()["running"] is True
    assert _pump(qapp, lambda: dashboard._sidebar.timer_control_state()["enabled"])
    assert _banner_showing(dashboard) and banner.message == BREAK_ENDED_MESSAGE
    assert banner.kind == "success"
    assert card._value.full_text() == "Task A1" and card._sub.full_text() == "Project A"
    _wait_started(qapp, runtime, [10, 10])


def test_selecting_another_task_during_a_break_does_not_change_what_resumes(qapp, dashboard, runtime):
    """Test E: Start A, Break In, select B, Break Out -> A resumes, not B."""
    _click_row_body(qapp, _row(dashboard, 10))
    _click_circle(qapp, dashboard)
    _click_break(qapp, dashboard)
    assert runtime.timer.pre_break_task()["task_id"] == 10

    _click_row_body(qapp, _row(dashboard, 11))
    assert dashboard._task_section.selected_task()["task_id"] == 11
    assert dashboard._sidebar.timer_control_state()["enabled"] is False, "still on break"
    assert runtime.timer.pre_break_task()["task_id"] == 10

    _click_break(qapp, dashboard)  # Break Out
    assert _pump(qapp, lambda: runtime.timer.is_running())
    _drain(qapp)
    assert runtime.timer.task_id == 10, "the held task resumed, not the selected one"
    assert _row(dashboard, 10)._is_running and not _row(dashboard, 11)._is_running
    _wait_started(qapp, runtime, [10, 10])


def test_a_break_out_that_cannot_resume_shows_no_resumed_message(qapp, dashboard, runtime, monkeypatch):
    banner = dashboard._action_banner
    _click_row_body(qapp, _row(dashboard, 10))
    _click_circle(qapp, dashboard)
    _click_break(qapp, dashboard)
    banner.dismiss()
    monkeypatch.setattr(
        runtime.task_service, "get_tasks_for_project",
        lambda project_id: [t for t in TASKS[project_id] if t["id"] != 10],
    )
    _click_break(qapp, dashboard)
    assert _pump(qapp, lambda: runtime.timer.break_status == BreakStatus.NONE)
    _drain(qapp)
    assert not runtime.timer.is_running()
    assert not _banner_showing(dashboard), "nothing resumed, so nothing says it did"
    assert dashboard._stat_cards.active_card._value.full_text() == "No active task"
    # Play is available again -- the last tracked task is still Task A1.
    assert _circle_enabled(qapp, dashboard)


def test_rapid_clicks_on_the_card_button_stay_consistent_with_the_circle(qapp, dashboard, runtime):
    _click_row_body(qapp, _row(dashboard, 10))
    _click_circle(qapp, dashboard)
    button = dashboard._stat_cards.break_button
    assert _pump(qapp, lambda: button.isEnabled())
    for _ in range(4):
        button.click()
    _drain(qapp)
    assert runtime.timer.break_status == BreakStatus.ON_BREAK
    assert dashboard._sidebar.timer_control_state()["enabled"] is False
    assert _pump(qapp, lambda: button.isEnabled())
    for _ in range(4):
        button.click()
    assert _pump(qapp, lambda: runtime.timer.is_running())
    _drain(qapp)
    assert runtime.timer.task_id == 10
    _wait_started(qapp, runtime, [10, 10])
    assert dashboard._sidebar.timer_control_state()["running"] is True


# ── The date rule, logout and a fresh window ─────────────────────────────────

def test_the_circle_is_disabled_on_any_day_but_today(qapp, dashboard, runtime):
    _click_row_body(qapp, _row(dashboard, 10))
    _click_circle(qapp, dashboard)
    dashboard._on_date_changed(ist_today() - timedelta(days=1))
    assert dashboard._sidebar.timer_control_state() == {
        "running": True, "enabled": False, "caption": CAPTION_HISTORY,
    }
    dashboard._on_pause_requested()
    assert runtime.timer.is_running(), "a browsed date must never stop a live session"
    dashboard._on_date_changed(ist_today())
    assert _pump(qapp, lambda: dashboard._sidebar.timer_control_state()["enabled"])
    _click_circle(qapp, dashboard)
    assert not runtime.timer.is_running()


def test_logging_out_resets_every_control(qapp, dashboard, runtime):
    _click_row_body(qapp, _row(dashboard, 10))
    _click_circle(qapp, dashboard)
    _click_break(qapp, dashboard)
    assert _banner_showing(dashboard)

    dashboard.reset_state()
    runtime.on_logout()

    assert dashboard._sidebar.timer_control_state() == {
        "running": False, "enabled": False, "caption": CAPTION_SELECT_TASK,
    }
    assert not _banner_showing(dashboard)
    button = dashboard._stat_cards.break_button
    assert (button.text(), button.isEnabled()) == ("Break In", False)
    assert dashboard._sidebar._status_text.text() == "Idle"


def test_a_fresh_window_shows_no_phantom_timer(qapp, dashboard, runtime):
    """Test I: nothing running, nothing on break, Play waits for a task."""
    assert not runtime.timer.is_running()
    assert dashboard._sidebar.timer_control_state() == {
        "running": False, "enabled": False, "caption": CAPTION_SELECT_TASK,
    }
    assert dashboard._sidebar._status_text.text() == "Idle"
    assert dashboard._stat_cards.active_card._value.full_text() == "No active task"
    assert not dashboard._stat_cards.break_button.isEnabled()
