"""
Date access control: FUTURE blocked, TODAY live, HISTORY read-only.

The desktop shows one day at a time, and exactly what may be *done* on that day
depends on which of the three it is. Two defects motivated this file:

  * the task list decided "read-only" with ``target_date < ist_today()``, which
    is false for tomorrow — so a future day read as "not history" and kept the
    live Start/Stop controls, on a day nothing could have been tracked on;
  * `TimerService` had no date rule at all. The only thing standing between a
    browsed historical date and a live time entry was a hidden button, and a
    hidden button is a presentation detail — a queued signal or a later caller
    reaches the method regardless.

So the rule is tested at both layers here: once where the widget decides what
to show, and once where tracked time is actually mutated. The mode itself
(`core/date_mode.py`) is tested directly, because every other assertion in this
file depends on it partitioning all three cases rather than two.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from core.date_mode import (
    DateMode, as_calendar_day, clamp_to_today, date_mode, is_live_date,
    is_selectable,
)
from core.time_format import IST, ist_today

TODAY = ist_today()
YESTERDAY = TODAY - timedelta(days=1)
OLDER = TODAY - timedelta(days=4)
TOMORROW = TODAY + timedelta(days=1)


# ── the mode itself ──────────────────────────────────────────────────────────

def test_the_three_modes_partition_every_date():
    assert date_mode(TOMORROW) == DateMode.FUTURE
    assert date_mode(TODAY) == DateMode.TODAY
    assert date_mode(YESTERDAY) == DateMode.HISTORY
    assert date_mode(OLDER) == DateMode.HISTORY


def test_only_today_is_live():
    assert is_live_date(TODAY)
    assert not is_live_date(YESTERDAY)
    assert not is_live_date(TOMORROW)


def test_today_is_the_maximum_selectable_date():
    assert is_selectable(TODAY)
    assert is_selectable(OLDER)
    assert not is_selectable(TOMORROW)
    assert not is_selectable(TODAY + timedelta(days=365))


def test_an_unreadable_date_is_neither_live_nor_selectable():
    """It fails closed. A date nobody can identify must not hand out a live
    Start button by defaulting to today."""
    for value in (None, "", "not a date", object(), 20260911):
        assert date_mode(value) is None
        assert not is_live_date(value)
        assert not is_selectable(value)


def test_a_calendar_day_is_read_from_an_instant_in_ist_not_in_utc():
    """00:30 IST on the 11th is the 11th, even though it is the 10th in UTC.
    Comparing UTC timestamps instead puts the user five and a half hours out
    for part of every single day."""
    just_after_ist_midnight = datetime(2026, 9, 11, 0, 30, tzinfo=IST)
    assert as_calendar_day(just_after_ist_midnight) == date(2026, 9, 11)
    assert just_after_ist_midnight.astimezone(timezone.utc).date() == date(2026, 9, 10)


def test_a_naive_timestamp_is_read_as_utc_then_converted():
    # 20:00 UTC on the 10th is 01:30 IST on the 11th.
    assert as_calendar_day(datetime(2026, 9, 10, 20, 0)) == date(2026, 9, 11)


def test_an_iso_string_is_the_day_it_names():
    assert as_calendar_day("2026-09-11") == date(2026, 9, 11)
    assert as_calendar_day("2026-09-11T23:59:59") == date(2026, 9, 11)


def test_the_boundary_is_tested_against_an_injected_today():
    """`today` is injectable on every rule so the midnight boundary is
    assertable without waiting for it -- the same arrangement
    background_services/activity/retention.py uses."""
    reference = date(2026, 9, 11)
    assert date_mode(date(2026, 9, 12), today=reference) == DateMode.FUTURE
    assert date_mode(date(2026, 9, 11), today=reference) == DateMode.TODAY
    assert date_mode(date(2026, 9, 10), today=reference) == DateMode.HISTORY


def test_clamping_resolves_a_future_or_unreadable_date_to_today():
    assert clamp_to_today(TOMORROW) == TODAY
    assert clamp_to_today(None) == TODAY
    assert clamp_to_today("rubbish") == TODAY
    assert clamp_to_today(YESTERDAY) == YESTERDAY


# ── the action layer: TimerService ───────────────────────────────────────────
#
# Reuses the fakes from test_timer_service.py rather than restating them: the
# point here is the date rule, not a second description of the service's
# collaborators.

from tests.test_timer_service import (  # noqa: E402 - after the module docstring
    FakeRuntime, FakeTimeEntryService,
)
from background_services.timer.timer_service import TimerService  # noqa: E402


@pytest.fixture
def timer(qapp, cache):
    backend = FakeTimeEntryService(entry_id=42)
    runtime = FakeRuntime(cache, backend)
    service = TimerService(runtime, backend, cache)
    runtime.timer = service
    yield service
    service.stop(timeout_ms=500)


def _errors(service) -> list:
    seen = []
    service.timer_error.connect(seen.append)
    return seen


def test_starting_on_today_is_allowed(timer):
    timer.start_tracking(1, 7, "Task", for_date=TODAY)
    assert timer.is_running()


def test_stopping_on_today_is_allowed(timer):
    timer.start_tracking(1, 7, "Task", for_date=TODAY)
    timer.stop_tracking(for_date=TODAY)
    assert not timer.is_running()


def test_starting_on_yesterday_is_refused_at_the_action_layer(timer):
    errors = _errors(timer)
    timer.start_tracking(1, 7, "Task", for_date=YESTERDAY)

    assert not timer.is_running()
    assert timer.active_session() is None
    assert len(errors) == 1
    assert "past date" in errors[0]


def test_starting_on_a_future_date_is_refused(timer):
    errors = _errors(timer)
    timer.start_tracking(1, 7, "Task", for_date=TOMORROW)

    assert not timer.is_running()
    assert len(errors) == 1
    assert "future date" in errors[0]


def test_switching_on_a_historical_date_is_refused(timer):
    errors = _errors(timer)
    timer.switch_tracking(1, 7, "Task", for_date=OLDER)

    assert not timer.is_running()
    assert errors


def test_a_refused_switch_does_not_stop_the_running_timer(timer):
    """The date is checked before anything moves. Checking it only inside
    start_tracking would let a refused switch stop the live session first and
    then decline to start the new one -- so browsing a date would end a
    session the user never asked to end."""
    timer.start_tracking(1, 7, "Task", for_date=TODAY)
    entry_before = timer.active_session()

    timer.switch_tracking(1, 9, "Other", for_date=YESTERDAY)

    assert timer.is_running()
    assert timer.task_id == 7
    assert timer.active_session()["started_at_utc"] == entry_before["started_at_utc"]


def test_stopping_for_a_historical_date_is_refused_and_the_timer_keeps_running(timer):
    timer.start_tracking(1, 7, "Task", for_date=TODAY)
    errors = _errors(timer)

    timer.stop_tracking(for_date=YESTERDAY)

    assert timer.is_running()
    assert errors


def test_a_programmatic_action_with_an_unreadable_date_is_refused(timer):
    errors = _errors(timer)
    timer.start_tracking(1, 7, "Task", for_date="the day before yesterday")

    assert not timer.is_running()
    assert errors


def test_a_system_initiated_stop_carries_no_date_and_is_never_blocked(timer):
    """The idle popup's "Stop timer", crash recovery and reconciliation are not
    scoped to a browsed day. Blocking them would strand a running entry rather
    than protect anything."""
    timer.start_tracking(1, 7, "Task", for_date=TODAY)
    timer.stop_tracking(notify_backend=False)
    assert not timer.is_running()


def test_a_refused_start_writes_no_durable_timer_record(timer, cache):
    """Nothing is persisted, queued or synced for a refused action -- the
    refusal happens before any state is committed, so there is no half-started
    session for recovery to adopt on the next launch."""
    from background_services.timer.timer_service import TIMER_STATE_KEY

    timer.start_tracking(1, 7, "Task", for_date=YESTERDAY)

    assert cache.load_app_state(TIMER_STATE_KEY) is None
    assert timer.runtime.sync.enqueued == []
    assert timer.runtime.tasks.submitted == []


# ── the widget layer: TaskSection ────────────────────────────────────────────

from ui.task_table import TaskSection  # noqa: E402


def _section(qapp) -> TaskSection:
    """A task list wired to a stub BackgroundApi.

    The api is a MagicMock so the calls this section makes -- and, more to the
    point, the calls it must *not* make -- are assertable without a runtime.
    """
    api = MagicMock()
    api.is_timer_running.return_value = False
    api.timer_elapsed_seconds.return_value = 0
    section = TaskSection(api=api, task_service=MagicMock())
    section.set_tasks(
        [{"id": 7, "name": "Write the report", "time_tracked_seconds": 0}],
        {"id": 1, "project_name": "Monitra"},
        "#3B82F6",
    )
    return section


def test_today_shows_the_start_control(qapp):
    section = _section(qapp)
    section.set_viewing_date(TODAY)
    assert all(not row._readonly for row in section._task_rows)
    assert section._task_rows[0]._timer_btn.isVisibleTo(section)


def test_selecting_yesterday_makes_every_row_read_only(qapp):
    section = _section(qapp)
    section.set_viewing_date(YESTERDAY)
    assert all(row._readonly for row in section._task_rows)


def test_selecting_a_future_date_makes_every_row_read_only(qapp):
    """The defect this file exists for: `target_date < ist_today()` is false
    for tomorrow, so a future day kept the live controls."""
    section = _section(qapp)
    section.set_viewing_date(TOMORROW)
    assert all(row._readonly for row in section._task_rows)


def test_returning_to_today_restores_the_live_controls(qapp):
    section = _section(qapp)
    section.set_viewing_date(YESTERDAY)
    section.set_viewing_date(TODAY)
    assert all(not row._readonly for row in section._task_rows)


def test_rows_built_while_a_past_date_is_shown_are_read_only(qapp):
    """A search or a project switch rebuilds the rows. They must pick the
    current mode up rather than defaulting to live."""
    section = _section(qapp)
    section.set_viewing_date(YESTERDAY)
    section.apply_search("report")
    assert section._task_rows
    assert all(row._readonly for row in section._task_rows)


def test_a_start_request_on_a_past_date_reaches_no_timer_call(qapp):
    section = _section(qapp)
    section.set_viewing_date(YESTERDAY)

    section._handle_start_request(section._task_rows[0])

    section.api.switch_timer.assert_not_called()
    section.api.start_timer.assert_not_called()


def test_a_stop_request_on_a_past_date_reaches_no_timer_call(qapp):
    section = _section(qapp)
    section.set_viewing_date(YESTERDAY)

    section._handle_stop_request(section._task_rows[0])

    section.api.stop_timer.assert_not_called()


def test_a_start_request_on_today_names_the_date_it_is_acting_on(qapp):
    """The widget's own guard is the first layer; passing `for_date` is what
    lets the service refuse independently of it."""
    section = _section(qapp)
    section.set_viewing_date(TODAY)

    section._handle_start_request(section._task_rows[0])

    assert section.api.switch_timer.call_args.kwargs["for_date"] == TODAY


def test_a_stop_request_on_today_names_the_date_it_is_acting_on(qapp):
    section = _section(qapp)
    section.set_viewing_date(TODAY)

    section._handle_stop_request(section._task_rows[0])

    assert section.api.stop_timer.call_args.kwargs["for_date"] == TODAY


def test_an_unreadable_viewing_date_is_refused_not_treated_as_today(qapp):
    section = _section(qapp)
    section.set_viewing_date(YESTERDAY)
    section.set_viewing_date(None)

    assert section._viewing_date == YESTERDAY
    section._handle_start_request(section._task_rows[0])
    section.api.switch_timer.assert_not_called()


def test_browsing_dates_never_touches_the_timer(qapp):
    """Navigation is a filter over data. It must not start, stop, switch or
    reassign tracking, and it must not reach the screenshot, activity or
    URL trackers -- all of which are driven by the timer's own lifecycle."""
    section = _section(qapp)
    for day in (YESTERDAY, OLDER, TODAY, TOMORROW, TODAY):
        section.set_viewing_date(day)

    section.api.start_timer.assert_not_called()
    section.api.stop_timer.assert_not_called()
    section.api.switch_timer.assert_not_called()


def test_the_live_tick_is_ignored_while_a_past_date_is_shown(qapp):
    """The row's base is the viewed day's completed total; folding today's
    live elapsed seconds onto it would mix two days in one number."""
    section = _section(qapp)
    section._running_task_id = 7
    section.set_viewing_date(YESTERDAY)
    row = section._task_rows[0]
    before = row._time_label.text()

    section._on_timer_tick(3600)

    assert row._time_label.text() == before
