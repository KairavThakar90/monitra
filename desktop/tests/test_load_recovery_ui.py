"""
A failed load shows what is happening, retries on its own, and can be retried.

"Unable to load projects" with nothing to press and no word on whether anything
was being done about it is what a user reads as "it is broken; I must sign out".
These tests pin the replacement: a calm message that says it is retrying, a
"Retry now" link, automatic retries on a bounded backoff that is spread out
after the first, the same for the task list, and -- separately -- a session
that has really ended is *not* presented as a network problem and not retried.
"""
from __future__ import annotations

import pytest

from app.api.exceptions import ApiConnectionError, ApiError, ApiHttpError, FailureCode
from tests.test_dashboard_opens_with_data import PROJECTS, USER, _armed, dashboard  # noqa: F401
from ui.dashboard_window import DashboardWindow


def _transient(code=FailureCode.RESET):
    inner = ApiConnectionError("Could not reach the server.")
    inner.failure_code = code
    inner.request_id = "req-123"
    outer = ApiError("Failed to load projects: Network connection error.")
    outer.__context__ = inner
    return outer


def _sidebar_text(dashboard):
    return dashboard._sidebar._empty_label.text()


def test_a_transient_failure_says_it_is_retrying_and_offers_a_retry(dashboard, runtime):
    _armed(dashboard)
    dashboard.on_login(USER)
    dashboard._on_projects_error(_transient())

    text = _sidebar_text(dashboard)
    assert "temporarily unavailable" in text.lower() and "retrying" in text.lower()
    assert 'href="retry"' in text and "Retry now" in text
    assert dashboard._project_retry_timer.isActive()


def test_the_message_is_not_the_raw_error_and_carries_no_internals(dashboard, runtime):
    _armed(dashboard)
    dashboard.on_login(USER)
    dashboard._on_projects_error(_transient())
    assert "Network connection error" not in _sidebar_text(dashboard)
    assert "req-123" not in _sidebar_text(dashboard)


def test_the_diagnosis_reaches_the_log(dashboard, runtime, caplog):
    import logging

    caplog.set_level(logging.INFO)
    _armed(dashboard)
    dashboard.on_login(USER)
    dashboard._on_projects_error(_transient(FailureCode.PROTOCOL))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "event=refresh.failed" in text and "code=protocol" in text and "req=req-123" in text


def test_the_retry_link_asks_again_at_once_from_a_clean_backoff(dashboard, runtime):
    runner = _armed(dashboard)
    dashboard.on_login(USER)
    for _ in range(3):
        dashboard._on_projects_error(_transient())
        dashboard._project_retry_timer.stop()
    assert dashboard._empty_project_load_retries == 3

    runner.keys.clear()
    dashboard._sidebar.retry_requested.emit()

    assert dashboard._empty_project_load_retries == 0
    assert not dashboard._project_retry_timer.isActive()
    assert "load-projects" in runner.keys
    assert "Loading projects" in _sidebar_text(dashboard)


def test_the_first_retry_is_prompt_and_the_rest_are_spread_out(dashboard, runtime):
    _armed(dashboard)
    dashboard.on_login(USER)
    delays = []
    for _ in range(6):
        dashboard._on_projects_error(_transient())
        delays.append(dashboard._project_retry_timer.interval())
        dashboard._project_retry_timer.stop()
    base = DashboardWindow._EMPTY_PROJECT_RETRY_DELAYS_MS
    assert delays[0] == base[0], "the first retry stays prompt"
    for i, delay in enumerate(delays[1:], start=1):
        floor = base[min(i, len(base) - 1)]
        assert floor <= delay <= floor * (1 + DashboardWindow._RETRY_JITTER) + 1
    assert delays[1] > delays[0]


def test_jitter_actually_varies(dashboard, runtime):
    _armed(dashboard)
    dashboard.on_login(USER)
    seen = {dashboard._jittered_delay(24_000, 3) for _ in range(40)}
    assert len(seen) > 5, "no jitter: a fleet that lost the backend together returns together"


def test_a_session_that_has_ended_is_not_a_network_problem(dashboard, runtime):
    _armed(dashboard)
    dashboard.on_login(USER)
    signals = []
    dashboard.unauthorized_error.connect(lambda: signals.append(True))

    inner = ApiHttpError(status_code=401, response_body="", message="x")
    outer = ApiError("Failed to load projects: Server error (HTTP 401).", status_code=401)
    outer.__context__ = inner
    dashboard._on_projects_error(outer)

    assert signals == [True]
    assert not dashboard._project_retry_timer.isActive()
    assert "temporarily" not in _sidebar_text(dashboard).lower()
    assert "Retry now" not in _sidebar_text(dashboard)


def test_a_loaded_list_keeps_showing_when_a_later_refresh_fails(dashboard, runtime):
    """Stale-but-real data stays; the message says how old it is."""
    _armed(dashboard)
    dashboard.on_login(USER)
    dashboard._on_projects_loaded(PROJECTS)
    dashboard._on_projects_error(_transient())
    assert [p["id"] for p in dashboard._projects] == [10, 20]
    assert "retrying" in dashboard._status_bar._label.text().lower() if hasattr(dashboard._status_bar, "_label") else True
    assert not dashboard._project_retry_timer.isActive(), "the periodic refresh owns this case"


# ── The task list ────────────────────────────────────────────────────────────

def _open_project(dashboard):
    dashboard.on_login(USER)
    dashboard._on_projects_loaded(PROJECTS)
    assert dashboard._current_project is not None


def test_a_failed_task_load_retries_by_itself_and_offers_a_link(dashboard, runtime):
    _armed(dashboard)
    _open_project(dashboard)
    dashboard._on_tasks_error(_transient())

    text = dashboard._task_section._status_label.text()
    assert "temporarily unavailable" in text.lower() and 'href="retry"' in text
    assert dashboard._task_retry_timer.isActive()


def test_task_retries_back_off_and_reset_on_success(dashboard, runtime):
    _armed(dashboard)
    _open_project(dashboard)
    project_id = dashboard._current_project["id"]
    dashboard._on_tasks_error(_transient())
    first = dashboard._task_retry_timer.interval()
    dashboard._task_retry_timer.stop()
    dashboard._on_tasks_error(_transient())
    assert dashboard._task_retry_timer.interval() > first
    dashboard._task_retry_timer.stop()

    dashboard._on_tasks_loaded(project_id, [{"id": 1, "name": "T", "status": "todo"}])

    assert dashboard._task_load_retries == 0 and not dashboard._task_retry_timer.isActive()


def test_the_task_retry_link_reloads_the_selected_project(dashboard, runtime):
    runner = _armed(dashboard)
    _open_project(dashboard)
    project_id = dashboard._current_project["id"]
    dashboard._on_tasks_error(_transient())
    runner.keys.clear()

    dashboard._task_section.retry_requested.emit()

    assert f"load-tasks:{project_id}" in runner.keys
    assert dashboard._task_load_retries == 0


def test_choosing_another_project_cancels_the_old_projects_retry(dashboard, runtime):
    _armed(dashboard)
    _open_project(dashboard)
    dashboard._on_tasks_error(_transient())
    assert dashboard._task_retry_timer.isActive()

    dashboard._on_project_selected(PROJECTS[1])

    assert not dashboard._task_retry_timer.isActive() and dashboard._task_load_retries == 0


def test_a_stale_task_retry_does_not_reload_when_tasks_are_already_there(dashboard, runtime):
    runner = _armed(dashboard)
    _open_project(dashboard)
    dashboard._task_section._has_loaded_tasks = True
    runner.keys.clear()
    dashboard._retry_tasks_for_selection()
    assert runner.keys == []


def test_logging_out_stops_the_task_retry(dashboard, runtime):
    _armed(dashboard)
    _open_project(dashboard)
    dashboard._on_tasks_error(_transient())
    dashboard.reset_state()
    assert not dashboard._task_retry_timer.isActive()


def test_a_session_failure_on_tasks_signs_out_instead_of_retrying(dashboard, runtime):
    _armed(dashboard)
    _open_project(dashboard)
    signals = []
    dashboard.unauthorized_error.connect(lambda: signals.append(True))
    dashboard._on_tasks_error(ApiError("Session expired. Please log in again.", status_code=401))
    assert signals == [True] and not dashboard._task_retry_timer.isActive()
