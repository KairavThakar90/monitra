"""
Coverage for the dashboard's summary-card snapshot.

The four cards must be a readout of data the window already holds -- the
day's time entries, the selected project's tasks and TimerService's session
-- and must never invent a value. In particular the running session belongs
to today alone: folding it into a past date's totals would mix two different
days, the same trap `_on_timer_tick` already avoids.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from core.time_format import ist_today


@pytest.fixture
def dashboard(qapp, runtime):
    from ui.dashboard_window import DashboardWindow

    widget = DashboardWindow(
        runtime=runtime,
        session_manager=runtime.session_manager,
        project_service=runtime.project_service,
        task_service=runtime.task_service,
        time_entry_service=runtime.time_entry_service,
        api_client=runtime.api_client,
    )
    yield widget
    widget.reset_state()
    widget.deleteLater()


def _entries():
    day = ist_today().isoformat()
    return [
        {"id": 1, "task_id": 10, "status": "stopped", "total_seconds": 3600,
         "start_time": f"{day}T09:00:00+00:00", "end_time": f"{day}T10:00:00+00:00"},
        {"id": 2, "task_id": 11, "status": "stopped", "total_seconds": 1800,
         "start_time": f"{day}T11:30:00+00:00", "end_time": f"{day}T12:00:00+00:00"},
    ]


def test_total_card_reads_the_days_banked_seconds(dashboard):
    dashboard._today_time_entries = _entries()
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.total_card._value.full_text() == "01:30:00"


def test_todays_activity_is_zero_until_something_is_measured(dashboard):
    dashboard._today_time_entries = []
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.activity_card._value.full_text() == "0%"
    assert dashboard._stat_cards.activity_card._sub.full_text() == "No activity today"


def test_todays_activity_is_weighted_by_duration_not_averaged(dashboard, monkeypatch):
    """10 minutes at 90% plus an hour at 20% is 30%, not the 55% a plain
    average of the two percentages would give."""
    from background_services.activity.today_summary import ActivityTotals, TodaySnapshot

    monkeypatch.setattr(dashboard.api, "live_activity_totals", ActivityTotals)
    dashboard._activity_day = ist_today()
    dashboard._activity_snapshot = TodaySnapshot(
        totals=ActivityTotals(weighted=90 * 600 + 20 * 3600, measured=600 + 3600),
        remote_ok=True,
    )
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.activity_card._value.full_text() == "30%"


def test_todays_activity_adds_the_window_still_being_sampled(dashboard, monkeypatch):
    """The in-flight window exists only in the service; it is added, and it is
    added once -- it has not been written to the queue or uploaded."""
    from background_services.activity.today_summary import ActivityTotals, TodaySnapshot

    dashboard._activity_day = ist_today()
    dashboard._activity_snapshot = TodaySnapshot(
        totals=ActivityTotals(weighted=20 * 600, measured=600), remote_ok=True
    )
    monkeypatch.setattr(
        dashboard.api, "live_activity_totals",
        lambda: ActivityTotals(weighted=100 * 600, measured=600),
    )
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.activity_card._value.full_text() == "60%"


def test_a_snapshot_from_a_previous_day_is_not_shown_as_today(dashboard, monkeypatch):
    from background_services.activity.today_summary import ActivityTotals, TodaySnapshot

    monkeypatch.setattr(dashboard.api, "live_activity_totals", ActivityTotals)
    dashboard._activity_snapshot = TodaySnapshot(
        totals=ActivityTotals(weighted=90 * 3600, measured=3600), remote_ok=True
    )
    dashboard._activity_day = ist_today() - timedelta(days=1)
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.activity_card._value.full_text() == "0%"


def test_a_stale_activity_reply_cannot_overwrite_a_newer_one(dashboard):
    from background_services.activity.today_summary import ActivityTotals, TodaySnapshot

    day = ist_today()
    newer = TodaySnapshot(
        totals=ActivityTotals(weighted=80 * 600, measured=600), remote_ok=True
    )
    older = TodaySnapshot(
        totals=ActivityTotals(weighted=10 * 600, measured=600), remote_ok=True
    )
    dashboard._on_today_activity_loaded(2, day, newer)
    dashboard._on_today_activity_loaded(1, day, older)
    assert dashboard._activity_snapshot is newer


def test_a_failed_activity_read_keeps_the_last_known_value(dashboard):
    """A temporary outage must not collapse a real percentage to zero."""
    from background_services.activity.today_summary import ActivityTotals, TodaySnapshot

    day = ist_today()
    good = TodaySnapshot(
        totals=ActivityTotals(weighted=80 * 600, measured=600), remote_ok=True
    )
    dashboard._on_today_activity_loaded(1, day, good)
    dashboard._on_today_activity_loaded(2, day, TodaySnapshot(remote_ok=False))
    assert dashboard._activity_snapshot is good


def test_tasks_completed_counts_the_projects_own_statuses(dashboard):
    dashboard._current_project = {"id": 1, "project_name": "Apollo"}
    dashboard._project_tasks = [
        {"id": 10, "name": "A", "status": "completed"},
        {"id": 11, "name": "B", "status": "in_progress"},
        {"id": 12, "name": "C", "status": {"id": 3, "name": "Completed"}},
        {"id": 13, "name": "D", "status": "todo"},
    ]
    dashboard._update_stat_cards()

    assert dashboard._stat_cards.tasks_card._value.full_text() == "2 / 4"
    assert dashboard._stat_cards.tasks_card._progress.value() == 50


def test_tasks_card_without_a_project_says_so(dashboard):
    dashboard._current_project = None
    dashboard._project_tasks = []
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.tasks_card._value.full_text() == "—"


def test_a_past_date_never_shows_a_running_session(dashboard, monkeypatch):
    """A historical day must show completed hours only. The live session is
    today's; adding it to another day's total would silently mix the two."""
    monkeypatch.setattr(dashboard.api, "is_timer_running", lambda: True)
    monkeypatch.setattr(dashboard.api, "timer_elapsed_seconds", lambda: 600)

    dashboard._today_time_entries = _entries()
    dashboard._current_date = ist_today() - timedelta(days=1)
    dashboard._update_stat_cards()

    assert dashboard._stat_cards.total_card._value.full_text() == "01:30:00"
    assert dashboard._stat_cards.total_card._sub.full_text() == "Not tracking"


def test_todays_running_session_is_included(dashboard, monkeypatch):
    monkeypatch.setattr(dashboard.api, "is_timer_running", lambda: True)
    monkeypatch.setattr(dashboard.api, "timer_elapsed_seconds", lambda: 600)
    monkeypatch.setattr(
        dashboard.api, "active_session",
        lambda: {"task_id": 10, "task_name": "Write the report", "project_id": 1},
    )

    dashboard._today_time_entries = _entries()
    dashboard._current_date = ist_today()
    dashboard._update_stat_cards()

    assert dashboard._stat_cards.total_card._value.full_text() == "01:40:00"
    assert dashboard._stat_cards.total_card._sub.full_text() == "Tracking now"
    assert dashboard._stat_cards.active_card._value.full_text() == "Write the report"


def test_signing_out_clears_the_cards(dashboard):
    dashboard._today_time_entries = _entries()
    dashboard._update_stat_cards()
    dashboard.reset_state()

    assert dashboard._stat_cards.activity_card._sub.full_text() == "No activity today"
    assert dashboard._stat_cards.tasks_card._value.full_text() == "—"
