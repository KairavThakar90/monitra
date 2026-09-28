"""
The Members directory's Allow / Not allow switch for adding tasks.

Every member may add tasks by default. An administrator switches it off for
one person from the web's Members page (`users.can_add_tasks`), and from
then on the backend refuses that person's creates with 403 and its own
sentence. This client's job is to stop *offering* what the server will
refuse -- the top bar's Add Task button goes off with a tooltip that says
why -- and to notice the switch moving in either direction while the window
is open, without a restart:

* the `/auth/me` profile seeds it at login and session verification;
* the profile is re-read with every refresh round (the sync-revision probe
  triggers one when the admin edits the member's row);
* a 403 on a create switches it off on the spot.

Only an explicit False withdraws. A profile from an older backend, which has
no such field, must leave Add Task available.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.api.client import ApiClient
from app.api.exceptions import ApiError, ApiHttpError
from app.tasks.service import TaskService
from background_services.public_api import NetworkState
from ui.task_table import TASK_CREATION_BLOCKED_MESSAGE, TaskSection

BLOCKED_DETAIL = "Adding tasks has been turned off for your account by an administrator."

ACTIVE_PROJECT = {"id": 7, "project_name": "Apollo", "status": {"id": 1, "name": "Active"}}
PAUSED_PROJECT = {"id": 8, "project_name": "Hermes", "status": {"id": 2, "name": "Paused"}}
TASKS = [{"id": 1, "name": "Alpha", "time_tracked_seconds": 0}]


def _section() -> TaskSection:
    api = MagicMock()
    api.timer_elapsed_seconds.return_value = 0
    api.is_timer_running.return_value = False
    return TaskSection(api=api, task_service=MagicMock())


# ── The task section ────────────────────────────────────────────────────────


def test_allowed_by_default_until_the_profile_says_otherwise(qapp):
    section = _section()
    assert section.task_creation_allowed
    seen = []
    section.add_task_available.connect(seen.append)
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    assert seen == [True]


def test_switched_off_the_button_goes_off_for_an_active_project(qapp):
    section = _section()
    availability, reasons = [], []
    section.add_task_available.connect(availability.append)
    section.task_creation_blocked.connect(reasons.append)
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")

    section.set_task_creation_allowed(False)

    assert availability == [True, False]
    assert reasons == [TASK_CREATION_BLOCKED_MESSAGE]
    assert TASK_CREATION_BLOCKED_MESSAGE == BLOCKED_DETAIL, "must match the backend's sentence"


def test_a_project_loaded_while_switched_off_never_offers_the_button(qapp):
    section = _section()
    seen = []
    section.add_task_available.connect(seen.append)
    section.set_task_creation_allowed(False)

    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")

    assert seen == [False]


def test_switching_back_on_restores_the_button_and_clears_the_reason(qapp):
    section = _section()
    availability, reasons = [], []
    section.add_task_available.connect(availability.append)
    section.task_creation_blocked.connect(reasons.append)
    section.set_task_creation_allowed(False)
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")

    section.set_task_creation_allowed(True)

    assert availability == [False, True]
    assert reasons == [TASK_CREATION_BLOCKED_MESSAGE, ""]


def test_switching_on_does_not_override_a_paused_project(qapp):
    """The switch is one condition of two; a paused or completed project
    still cannot take a task."""
    section = _section()
    seen = []
    section.add_task_available.connect(seen.append)
    section.set_task_creation_allowed(False)
    section.set_tasks(TASKS, PAUSED_PROJECT, "#3B82F6")
    section.set_task_creation_allowed(True)
    assert seen == [False, False]


def test_the_switch_is_edge_triggered(qapp):
    """A profile re-read every refresh round must not re-publish an
    unchanged answer -- that is exactly the level-triggered signal
    DO_NOT_DO.md warns about."""
    section = _section()
    availability, reasons = [], []
    section.add_task_available.connect(availability.append)
    section.task_creation_blocked.connect(reasons.append)
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")

    for _ in range(5):
        section.set_task_creation_allowed(True)
    section.set_task_creation_allowed(False)
    for _ in range(5):
        section.set_task_creation_allowed(False)

    assert availability == [True, False]
    assert reasons == [TASK_CREATION_BLOCKED_MESSAGE]


def test_only_an_explicit_false_withdraws(qapp):
    """An older backend's profile has no `can_add_tasks`; None must read as
    allowed, or every user of such a backend loses Add Task."""
    section = _section()
    section.set_task_creation_allowed(False)
    section.set_task_creation_allowed(None)
    assert section.task_creation_allowed
    section.set_task_creation_allowed(False)
    section.set_task_creation_allowed(True)
    assert section.task_creation_allowed


def test_clicking_while_switched_off_opens_no_dialog_and_says_why(qapp, monkeypatch):
    section = _section()
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    section.set_task_creation_allowed(False)
    opened = []
    monkeypatch.setattr("ui.task_table.AddTaskDialog", lambda *a, **k: opened.append(a) or MagicMock())

    section.open_add_task_dialog()

    assert opened == []
    section.api.run_in_background.assert_not_called()
    message = section.api.notify.call_args.args[0]
    assert message == TASK_CREATION_BLOCKED_MESSAGE


def test_a_refused_create_switches_the_button_off_at_once(qapp):
    """The admin flipped the switch between the last profile read and the
    click. The server refuses; the button must not keep inviting a second
    attempt until the next refresh round."""
    section = _section()
    availability = []
    section.add_task_available.connect(availability.append)
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    section.task_service.create_task.side_effect = ApiError(BLOCKED_DETAIL, status_code=403)

    def run_inline(call, *, on_success=None, on_error=None, key=None):
        try:
            on_success(call())
        except Exception as exc:  # noqa: BLE001
            on_error(exc)
        return object()

    section.api.run_in_background.side_effect = run_inline
    section._run_task_mutation(
        lambda: section.task_service.create_task(7, "Write the report"),
        success_message="Task created successfully.", key="create-task:7:Write the report",
        kind="created", project_id=7, after_failure=section._on_create_refused,
    )

    assert not section.task_creation_allowed
    assert availability == [True, False]
    # The backend's own sentence reaches the user.
    assert any(call.args[0] == BLOCKED_DETAIL for call in section.api.notify.call_args_list)


def test_other_create_failures_leave_the_switch_alone(qapp):
    section = _section()
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    section._on_create_refused(ApiError("Failed to create task: Network connection error."))
    section._on_create_refused(ApiError("Creating the task failed: bad request", status_code=400))
    assert section.task_creation_allowed


# ── The task service ────────────────────────────────────────────────────────


def test_a_403_carries_the_backends_reason():
    client = MagicMock(spec=ApiClient)
    client.post.side_effect = ApiHttpError(403, json.dumps({"detail": BLOCKED_DETAIL}))
    with pytest.raises(ApiError) as excinfo:
        TaskService(client).create_task(7, "Write the report")
    assert str(excinfo.value) == BLOCKED_DETAIL
    assert excinfo.value.status_code == 403


def test_a_403_without_a_body_keeps_the_generic_wording():
    client = MagicMock(spec=ApiClient)
    client.post.side_effect = ApiHttpError(403, "")
    with pytest.raises(ApiError) as excinfo:
        TaskService(client).create_task(7, "Write the report")
    assert str(excinfo.value) == "You do not have permission to create tasks."


# ── The dashboard ───────────────────────────────────────────────────────────


class FakeRunner:
    """Records submissions instead of running them, de-duplicating by key
    exactly as TaskRunner does."""

    def __init__(self):
        self.calls = {}

    def __call__(self, fn, *, on_success=None, on_error=None, key=None):
        if key in self.calls:
            return None
        self.calls[key] = (fn, on_success, on_error)
        return object()

    def succeed(self, key, result):
        self.calls[key][1](result)


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
    runner = FakeRunner()
    widget.api.run_in_background = runner
    widget.api.network_state = lambda: NetworkState.BACKEND_REACHABLE
    yield widget, runner
    widget.reset_state()
    widget.deleteLater()


def _profile(**overrides):
    profile = {"id": 1, "name": "Kairav", "email": "k@example.com", "role_name": "employee"}
    profile.update(overrides)
    return profile


def _button(dashboard):
    return dashboard._topbar._add_task_btn


def test_login_seeds_the_switch_from_the_profile(dashboard):
    widget, _ = dashboard
    widget.on_login(_profile(can_add_tasks=False))
    widget._task_section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")

    assert not _button(widget).isEnabled()
    assert _button(widget).toolTip() == TASK_CREATION_BLOCKED_MESSAGE


def test_login_with_an_older_profile_keeps_add_task(dashboard):
    widget, _ = dashboard
    widget.on_login(_profile())
    widget._task_section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    assert _button(widget).isEnabled()
    assert _button(widget).toolTip() == "Add a task to the selected project"


def test_session_verification_applies_the_switch(dashboard):
    widget, _ = dashboard
    widget.on_login(_profile())
    widget._task_section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    assert _button(widget).isEnabled()

    widget.on_session_verified(_profile(can_add_tasks=False))

    assert not _button(widget).isEnabled()


def test_a_refresh_round_re_reads_the_profile_and_applies_it_both_ways(dashboard):
    """The admin flips the switch on the web while the desktop is open. The
    refresh round (triggered by the sync probe or the timer) re-reads the
    profile, and the button follows -- off, then on again."""
    widget, runner = dashboard
    widget.on_login(_profile())
    widget._task_section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    # Login started its own round; end it so the next one is not "in flight".
    runner.calls.clear()
    widget._refresh_outstanding = 0

    widget.refresh_data()
    assert "load-profile" in runner.calls
    runner.succeed("load-profile", _profile(can_add_tasks=False))
    assert not _button(widget).isEnabled()
    assert _button(widget).toolTip() == TASK_CREATION_BLOCKED_MESSAGE

    runner.calls.clear()
    widget._refresh_outstanding = 0
    widget.refresh_data()
    runner.succeed("load-profile", _profile(can_add_tasks=True))
    assert _button(widget).isEnabled()
    assert _button(widget).toolTip() == "Add a task to the selected project"


def test_the_profile_read_hits_auth_me(dashboard):
    widget, runner = dashboard
    widget.on_login(_profile())
    runner.calls.clear()
    widget._refresh_outstanding = 0
    widget.refresh_data()
    response = MagicMock()
    response.json.return_value = _profile(can_add_tasks=False)
    widget.api_client.get = MagicMock(return_value=response)

    assert runner.calls["load-profile"][0]() == _profile(can_add_tasks=False)
    widget.api_client.get.assert_called_once_with("/auth/me")


def test_logout_resets_the_switch_for_the_next_user(dashboard):
    widget, _ = dashboard
    widget.on_login(_profile(can_add_tasks=False))
    widget.reset_state()
    assert widget._task_section.task_creation_allowed
    assert _button(widget).toolTip() == "Add a task to the selected project"
