"""
A failed *first* project load — nothing cached, so nothing to show — must
retry on its own, on a short bounded backoff.

The reported symptom: opening the app sometimes did not show the project
list, and the user learned to work around it by closing and reopening the
app until a launch happened to land after whatever caused the hiccup passed.

A transient failure here (a cold backend, a one-off timeout, a token that
needed one refresh) is not a connectivity *change* — `NetworkState` never
flips, so the reconnect-triggered retry in `_on_network_state_changed` never
fires — and the periodic refresh is two minutes away. Nothing retried sooner
than that, which is exactly "doesn't load; I have to reopen the app".

`_schedule_empty_project_retry` closes that gap: a bounded, backed-off retry
owned by `DashboardWindow` itself, armed only for the empty-state case (a
project already on screen has its own "showing cached data — retrying" path,
driven by the network-state and periodic refresh, which this must not
duplicate).
"""
from __future__ import annotations

from tests.test_dashboard_opens_with_data import PROJECTS, USER, _armed, dashboard  # noqa: F401
from ui.dashboard_window import DashboardWindow

FIRST_RETRY_DELAY_MS = DashboardWindow._EMPTY_PROJECT_RETRY_DELAYS_MS[0]


def test_a_failed_first_load_schedules_a_short_retry(dashboard, runtime):
    """No cache, the fetch fails: a retry must be armed, not left to the
    two-minute periodic refresh or a connectivity change that never happens."""
    _armed(dashboard)
    dashboard.on_login(USER)
    assert dashboard._projects == []

    dashboard._on_projects_error(RuntimeError("backend hiccup"))

    assert dashboard._project_retry_timer.isActive()
    assert dashboard._project_retry_timer.interval() == FIRST_RETRY_DELAY_MS


def test_repeated_failures_back_off(dashboard, runtime):
    _armed(dashboard)
    dashboard.on_login(USER)

    dashboard._on_projects_error(RuntimeError("one"))
    first_delay = dashboard._project_retry_timer.interval()
    dashboard._project_retry_timer.stop()  # as if the retry had just fired
    dashboard._on_projects_error(RuntimeError("two"))
    second_delay = dashboard._project_retry_timer.interval()

    assert second_delay > first_delay


def test_a_successful_load_resets_the_retry_and_stops_the_timer(dashboard, runtime):
    _armed(dashboard)
    dashboard.on_login(USER)
    dashboard._on_projects_error(RuntimeError("boom"))
    assert dashboard._project_retry_timer.isActive()

    dashboard._on_projects_loaded(PROJECTS)

    assert not dashboard._project_retry_timer.isActive()
    assert dashboard._empty_project_load_retries == 0


def test_an_empty_but_real_answer_is_not_treated_as_a_failure(dashboard, runtime):
    """The server answering "you have no projects" is a real answer, not a
    fetch that couldn't be made — it must not arm the retry."""
    _armed(dashboard)
    dashboard.on_login(USER)

    dashboard._on_projects_loaded([])

    assert not dashboard._project_retry_timer.isActive()


def test_a_failure_with_a_project_already_on_screen_does_not_use_this_retry(dashboard, runtime):
    """A later refresh failing with cached data already shown is the
    existing "showing cached data -- retrying" path (network-state and
    periodic refresh), not this one -- the two must not race each other."""
    _armed(dashboard)
    dashboard.on_login(USER)
    dashboard._on_projects_loaded(PROJECTS)  # something is now on screen

    dashboard._on_projects_error(RuntimeError("later refresh failed"))

    assert not dashboard._project_retry_timer.isActive()


def test_logging_out_cancels_any_pending_retry(dashboard, runtime):
    _armed(dashboard)
    dashboard.on_login(USER)
    dashboard._on_projects_error(RuntimeError("boom"))
    assert dashboard._project_retry_timer.isActive()

    dashboard.reset_state()

    assert not dashboard._project_retry_timer.isActive()
    assert dashboard._empty_project_load_retries == 0


def test_a_session_expired_error_does_not_schedule_a_pointless_retry(dashboard, runtime):
    """Retrying against a token the backend has already rejected would just
    fail again; the existing `unauthorized_error` path is what applies."""
    _armed(dashboard)
    dashboard.on_login(USER)
    signals = []
    dashboard.unauthorized_error.connect(lambda: signals.append(True))

    dashboard._on_projects_error(RuntimeError("Session expired. Please log in again."))

    assert signals == [True]
    assert not dashboard._project_retry_timer.isActive()
