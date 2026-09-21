"""
Coverage for the dashboard's summary-card snapshot.

The three cards must be a readout of data the window already holds -- the
day's time entries for the *selected project*, that project's own status row,
and TimerService's session -- and must never invent a value. In particular
the running session belongs to today alone: folding it into a past date's
totals would mix two different days, the same trap `_on_timer_tick` already
avoids. And it belongs to whichever project is actually being tracked: the
selected project's PROJECT HOURS card must not gain another project's live
session just because it happens to be the one on screen.
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
        {"id": 1, "task_id": 10, "project_id": 1, "status": "stopped", "total_seconds": 3600,
         "start_time": f"{day}T09:00:00+00:00", "end_time": f"{day}T10:00:00+00:00"},
        {"id": 2, "task_id": 11, "project_id": 1, "status": "stopped", "total_seconds": 1800,
         "start_time": f"{day}T11:30:00+00:00", "end_time": f"{day}T12:00:00+00:00"},
        {"id": 3, "task_id": 12, "project_id": 2, "status": "stopped", "total_seconds": 900,
         "start_time": f"{day}T13:00:00+00:00", "end_time": f"{day}T13:15:00+00:00"},
    ]


def test_total_card_reads_the_selected_projects_banked_seconds(dashboard):
    """Project 1 banked 01:30:00 today; project 2 banked 00:15:00. Selecting
    project 1 must show only its own figure, not the 01:45:00 combined total
    the old organisation-wide card would have shown."""
    dashboard._today_time_entries = _entries()
    dashboard._current_project = {"id": 1, "project_name": "Apollo"}
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.total_card._value.full_text() == "01:30:00"


def test_total_card_switches_with_the_selected_project(dashboard):
    dashboard._today_time_entries = _entries()
    dashboard._current_project = {"id": 2, "project_name": "Beta"}
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.total_card._value.full_text() == "00:15:00"


def test_total_card_without_a_project_shows_no_time(dashboard):
    dashboard._today_time_entries = _entries()
    dashboard._current_project = None
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.total_card._value.full_text() == "00:00:00"


def test_status_card_reads_the_selected_projects_own_status(dashboard):
    """The status is exactly what an admin set from the web frontend --
    already inline on the project dict this window loaded, via
    `projects.status_id` -> `project_statuses.name`/`color`."""
    dashboard._current_project = {
        "id": 1, "project_name": "Apollo",
        "status": {"id": 1, "name": "Active", "color": "#3B82F6"},
    }
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.status_card._value.full_text() == "Active"


def test_status_card_without_a_project_says_so(dashboard):
    dashboard._current_project = None
    dashboard._update_stat_cards()
    assert dashboard._stat_cards.status_card._value.full_text() == "—"
    assert dashboard._stat_cards.status_card._sub.full_text() == "No project selected"


def test_a_past_date_never_shows_a_running_session(dashboard, monkeypatch):
    """A historical day must show completed hours only. The live session is
    today's; adding it to another day's total would silently mix the two."""
    monkeypatch.setattr(dashboard.api, "is_timer_running", lambda: True)
    monkeypatch.setattr(dashboard.api, "timer_elapsed_seconds", lambda: 600)

    dashboard._today_time_entries = _entries()
    dashboard._current_project = {"id": 1, "project_name": "Apollo"}
    dashboard._current_date = ist_today() - timedelta(days=1)
    dashboard._update_stat_cards()

    assert dashboard._stat_cards.total_card._value.full_text() == "01:30:00"
    assert dashboard._stat_cards.total_card._sub.full_text() == "Not tracking"


def test_todays_running_session_is_included_for_its_own_project(dashboard, monkeypatch):
    monkeypatch.setattr(dashboard.api, "is_timer_running", lambda: True)
    monkeypatch.setattr(dashboard.api, "timer_elapsed_seconds", lambda: 600)
    monkeypatch.setattr(
        dashboard.api, "active_session",
        lambda: {"task_id": 10, "task_name": "Write the report", "project_id": 1},
    )

    dashboard._today_time_entries = _entries()
    dashboard._current_project = {"id": 1, "project_name": "Apollo"}
    dashboard._current_date = ist_today()
    dashboard._update_stat_cards()

    assert dashboard._stat_cards.total_card._value.full_text() == "01:40:00"
    assert dashboard._stat_cards.total_card._sub.full_text() == "Tracking now"
    assert dashboard._stat_cards.active_card._value.full_text() == "Write the report"


def test_a_running_session_on_another_project_does_not_inflate_this_ones_hours(dashboard, monkeypatch):
    """The timer is running against project 2 while project 1 is the one on
    screen: project 1's card must show its own banked time only, and must not
    claim to be tracking."""
    monkeypatch.setattr(dashboard.api, "is_timer_running", lambda: True)
    monkeypatch.setattr(dashboard.api, "timer_elapsed_seconds", lambda: 600)
    monkeypatch.setattr(
        dashboard.api, "active_session",
        lambda: {"task_id": 12, "task_name": "Design review", "project_id": 2},
    )

    dashboard._today_time_entries = _entries()
    dashboard._current_project = {"id": 1, "project_name": "Apollo"}
    dashboard._current_date = ist_today()
    dashboard._update_stat_cards()

    assert dashboard._stat_cards.total_card._value.full_text() == "01:30:00"
    assert dashboard._stat_cards.total_card._sub.full_text() == "Not tracking"


def test_signing_out_clears_the_cards(dashboard):
    dashboard._today_time_entries = _entries()
    dashboard._current_project = {"id": 1, "project_name": "Apollo"}
    dashboard._update_stat_cards()
    dashboard.reset_state()

    assert dashboard._stat_cards.status_card._value.full_text() == "—"
    assert dashboard._stat_cards.total_card._value.full_text() == "00:00:00"


# ── TODAY'S ACTIVITY ──────────────────────────────────────────────────────────
#
# The fourth card: a single duration-weighted percentage from
# background_services.activity.today_summary, combining whatever was last
# fetched from the backend (plus anything still queued locally) with the
# window currently being sampled -- added fresh on every call so the figure
# moves between refreshes rather than jumping only once every couple of
# minutes.

def test_before_anything_is_measured_the_card_is_an_honest_blank(dashboard):
    """Never a 0% before the first real measurement -- that would read as a
    broken feature rather than an accurate one."""
    dashboard._update_stat_cards()

    assert dashboard._stat_cards.activity_card._value.full_text() == "—"
    assert dashboard._stat_cards.activity_card._sub.full_text() == "Not tracking yet"


def test_a_fetched_snapshot_drives_the_cards_percentage(dashboard):
    from background_services.public_api import TodaySnapshot
    from background_services.activity.today_summary import totals_from_percent

    dashboard._on_today_activity_loaded(
        TodaySnapshot(totals=totals_from_percent(42, 600), remote_ok=True)
    )

    assert dashboard._stat_cards.activity_card._value.full_text() == "42%"


def test_a_failed_refresh_keeps_the_last_good_percentage(dashboard):
    """`remote_ok=False` must not blank a real figure already on screen --
    the same rule `today_activity_snapshot` documents for itself."""
    from background_services.public_api import TodaySnapshot
    from background_services.activity.today_summary import totals_from_percent

    dashboard._on_today_activity_loaded(
        TodaySnapshot(totals=totals_from_percent(55, 600), remote_ok=True)
    )
    dashboard._on_today_activity_loaded(TodaySnapshot(remote_ok=False))

    assert dashboard._stat_cards.activity_card._value.full_text() == "55%"


def test_the_live_window_is_added_on_top_of_the_last_fetch(dashboard, monkeypatch):
    from background_services.public_api import TodaySnapshot
    from background_services.activity.today_summary import ActivityTotals, totals_from_percent

    dashboard._on_today_activity_loaded(
        TodaySnapshot(totals=totals_from_percent(50, 600), remote_ok=True)
    )
    monkeypatch.setattr(
        dashboard.api, "live_activity_totals", lambda: totals_from_percent(100, 600)
    )

    dashboard._update_stat_cards()

    # (50*600 + 100*600) / 1200 == 75 -- weighted, not a plain average of the
    # two headline percentages (which would coincidentally also read 75 here,
    # but the arithmetic under test is the addable-totals one, not a mean).
    assert dashboard._stat_cards.activity_card._value.full_text() == "75%"


def test_today_activity_is_shown_as_tracking_only_while_a_timer_runs(dashboard, monkeypatch):
    from background_services.public_api import TodaySnapshot
    from background_services.activity.today_summary import totals_from_percent

    dashboard._on_today_activity_loaded(
        TodaySnapshot(totals=totals_from_percent(30, 600), remote_ok=True)
    )
    monkeypatch.setattr(dashboard.api, "is_timer_running", lambda: True)

    dashboard._update_stat_cards()

    assert dashboard._stat_cards.activity_card._sub.full_text() == "Tracking now"
