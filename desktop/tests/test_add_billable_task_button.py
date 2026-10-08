"""The top bar's second Add button: Add Non Billable Task.

An administrator allows or excludes it per member from the web's Members page
(`users.can_add_nonbillable_tasks`, on the `/auth/me` profile). A member who is
allowed sees a second button right after Add Task; everybody else sees the bar
they always had. Tasks made with it end " - Non billable".

What is pinned here:

* **Who sees it.** Hidden by default, and shown only for an explicit True -- a
  profile from an older backend has no such field and must show nothing. It
  follows the profile in both directions without a restart, and a new login
  starts hidden.
* **It is independent of Add Task.** A member whose Add Task is switched off can
  still have this button; one with Add Task on but this off never sees it.
* **Where it goes.** Right after Add Task, before Request and Refresh, with a
  colour of its own, and in the icon-only compact bar as well.
* **The three buttons share a highlighted border.**
* **It cannot overflow the bar** in either form.
* **The server's last word:** a 403 on a Non billable create hides the button
  at once, and leaves Add Task alone.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QDialog

from app.api.exceptions import ApiError
from background_services.public_api import NetworkState
from ui.task_table import NONBILLABLE_CREATION_BLOCKED_MESSAGE, AddTaskDialog, TaskSection
from ui.topbar import TopBar

ACTIVE_PROJECT = {"id": 7, "project_name": "Apollo", "status": {"id": 1, "name": "Active"}}
PAUSED_PROJECT = {"id": 8, "project_name": "Hermes", "status": {"id": 2, "name": "Paused"}}
TASKS = [{"id": 1, "name": "Alpha", "time_tracked_seconds": 0}]


def _section() -> TaskSection:
    api = MagicMock()
    api.timer_elapsed_seconds.return_value = 0
    api.is_timer_running.return_value = False
    return TaskSection(api=api, task_service=MagicMock())


# ── the top bar ──────────────────────────────────────────────────────────────

def _order(bar):
    layout = bar.layout()
    widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
    return [w for w in widgets if w is not None]


def test_the_button_sits_right_after_add_task_and_before_request_and_refresh(qapp):
    bar = TopBar()
    widgets = _order(bar)
    positions = [widgets.index(w) for w in (
        bar._search, bar._add_task_btn, bar._add_nonbillable_btn, bar._request_btn, bar._refresh_btn,
    )]
    assert positions == sorted(positions) and len(set(positions)) == 5
    assert widgets.index(bar._add_nonbillable_btn) == widgets.index(bar._add_task_btn) + 1


def test_it_is_hidden_until_something_shows_it(qapp):
    bar = TopBar()
    assert not bar.is_nonbillable_task_visible()
    assert not bar._add_nonbillable_btn.isEnabled()


def test_it_can_be_shown_and_taken_away(qapp):
    bar = TopBar()
    bar.set_nonbillable_task_visible(True)
    assert bar.is_nonbillable_task_visible()
    bar.set_nonbillable_task_visible(False)
    assert not bar.is_nonbillable_task_visible()


def test_it_is_off_until_a_project_is_ready_and_clicking_it_asks_for_the_dialog(qapp):
    bar = TopBar()
    bar.set_nonbillable_task_visible(True)
    asked = []
    bar.add_nonbillable_task_clicked.connect(lambda: asked.append(True))
    assert not bar._add_nonbillable_btn.isEnabled()
    bar.set_nonbillable_task_enabled(True)
    bar._add_nonbillable_btn.click()
    assert asked == [True]


def test_it_does_not_touch_add_task_or_its_signal(qapp):
    bar = TopBar()
    add_task = []
    bar.add_task_clicked.connect(lambda: add_task.append(True))
    bar.set_nonbillable_task_visible(True)
    bar.set_nonbillable_task_enabled(True)
    bar._add_nonbillable_btn.click()
    assert add_task == []
    assert not bar._add_task_btn.isEnabled()                 # still waiting for its own project state


def test_its_label_and_tooltip_say_what_it_is(qapp):
    bar = TopBar()
    assert bar._add_nonbillable_btn.text().strip() == "Add Non Billable Task"
    assert "Non billable" in bar._add_nonbillable_btn.toolTip()


def test_it_has_a_colour_of_its_own(qapp):
    bar = TopBar()
    css = bar.styleSheet()
    block = _rule(css, "HeaderAddNonBillableTaskBtn")
    other = _rule(css, "HeaderAddTaskBtn")
    request = _rule(css, "RequestBtn")
    assert "#0D9488" in block                                  # teal
    assert "#0D9488" not in other and "#0D9488" not in request
    background = lambda b: re.search(r"background:\s*([^;]+);", b).group(1).strip()
    assert len({background(block), background(other), background(request)}) == 3


def _rule(css: str, name: str) -> str:
    """The first plain `QPushButton#name { ... }` rule's body."""
    match = re.search(r"QPushButton#%s\s*\{\{?(.*?)\}\}?" % re.escape(name), css, re.S)
    assert match, name
    return match.group(1)


def test_the_three_header_buttons_share_a_highlighted_border(qapp):
    css = TopBar().styleSheet()
    for name in ("HeaderAddTaskBtn", "HeaderAddNonBillableTaskBtn", "RequestBtn"):
        body = _rule(css, name)
        assert re.search(r"border:\s*1\.5px solid #[0-9A-Fa-f]{6}", body), name


def test_the_compact_bar_keeps_the_button_as_a_labelled_icon(qapp):
    bar = TopBar()
    bar.set_nonbillable_task_visible(True)
    bar._set_compact(True)
    button = bar._add_nonbillable_btn
    assert button.text() == "" and button.width() == 34
    assert button.accessibleName() == "Add Non Billable Task"
    assert not button.icon().isNull()
    assert bar.is_nonbillable_task_visible()                  # nothing is hidden, only the words go
    bar._set_compact(False)
    assert button.text().strip() == "Add Non Billable Task"


@pytest.mark.parametrize("width", [1400, 1000, 760, 640])
def test_the_four_actions_never_overlap_or_leave_the_bar(qapp, width):
    bar = TopBar()
    bar.set_nonbillable_task_visible(True)
    bar.set_nonbillable_task_enabled(True)
    bar.resize(width, 56)
    bar.show()
    qapp.processEvents()
    bar._measure_forms()
    qapp.processEvents()
    bar.resize(width, 56)
    qapp.processEvents()
    boxes = [b.geometry() for b in (bar._add_task_btn, bar._add_nonbillable_btn, bar._request_btn, bar._refresh_btn)]
    for box in boxes:
        assert box.left() >= 0 and box.right() <= bar.width(), (width, box)
    for earlier, later in zip(boxes, boxes[1:]):
        assert earlier.right() < later.left(), (width, earlier, later)


def test_showing_the_button_widens_the_bars_floor_so_it_cannot_be_squeezed_out(qapp):
    bar = TopBar()
    bar.show()
    qapp.processEvents()
    bar._measure_forms()
    without = bar._full_minimum_width
    bar.set_nonbillable_task_visible(True)
    # Nothing here calls `_measure_forms` itself: showing the button has to
    # cause the re-measure, one event-loop turn later.
    qapp.processEvents()
    assert bar._full_minimum_width > without


# ── the task section ─────────────────────────────────────────────────────────

def test_billable_creation_is_off_until_the_profile_grants_it(qapp):
    section = _section()
    assert not section.nonbillable_creation_allowed
    seen = []
    section.nonbillable_task_available.connect(seen.append)
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    assert seen == [False]


@pytest.mark.parametrize("value, expected", [
    (True, True), (False, False), (None, False), ("true", False), (1, False), (0, False),
])
def test_only_an_explicit_true_grants_it(qapp, value, expected):
    section = _section()
    section.set_nonbillable_creation_allowed(value)
    assert section.nonbillable_creation_allowed is expected


def test_granted_it_is_offered_for_an_active_project(qapp):
    section = _section()
    section.set_nonbillable_creation_allowed(True)
    seen = []
    section.nonbillable_task_available.connect(seen.append)
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    assert seen == [True]


def test_a_paused_project_does_not_offer_it(qapp):
    section = _section()
    section.set_nonbillable_creation_allowed(True)
    seen = []
    section.nonbillable_task_available.connect(seen.append)
    section.set_tasks(TASKS, PAUSED_PROJECT, "#3B82F6")
    assert seen == [False]


def test_the_grant_is_edge_triggered(qapp):
    section = _section()
    visible = []
    section.nonbillable_task_visible.connect(visible.append)
    for value in (True, True, True, False, False, True):
        section.set_nonbillable_creation_allowed(value)
    assert visible == [True, False, True]


def test_granting_after_a_project_is_loaded_enables_the_button_at_once(qapp):
    section = _section()
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    seen = []
    section.nonbillable_task_available.connect(seen.append)
    section.set_nonbillable_creation_allowed(True)
    assert seen == [True]


def test_clearing_the_section_makes_it_unavailable(qapp):
    section = _section()
    section.set_nonbillable_creation_allowed(True)
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    seen = []
    section.nonbillable_task_available.connect(seen.append)
    section.clear()
    assert seen == [False]


def test_it_is_independent_of_the_add_task_switch(qapp):
    """Add Task switched off by the administrator, Add Non Billable Task on: the
    one is blocked, the other offered."""
    section = _section()
    add_task, billable = [], []
    section.add_task_available.connect(add_task.append)
    section.nonbillable_task_available.connect(billable.append)
    section.set_task_creation_allowed(False)
    section.set_nonbillable_creation_allowed(True)
    section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    assert add_task[-1] is False and billable[-1] is True
    # ... and the reverse.
    section.set_task_creation_allowed(True)
    section.set_nonbillable_creation_allowed(False)
    assert add_task[-1] is True and billable[-1] is False


def test_without_the_grant_invoking_it_opens_no_dialog_and_says_why(qapp, monkeypatch):
    section = _section()
    section._project = ACTIVE_PROJECT
    opened = []
    monkeypatch.setattr(AddTaskDialog, "exec", lambda self: opened.append(self) or QDialog.DialogCode.Rejected)
    section.open_add_nonbillable_task_dialog()
    assert opened == []
    section.api.notify.assert_called_once()
    assert section.api.notify.call_args.args[0] == NONBILLABLE_CREATION_BLOCKED_MESSAGE


def test_with_the_grant_it_opens_the_billable_dialog(qapp, monkeypatch):
    section = _section()
    section.set_nonbillable_creation_allowed(True)
    section._project = ACTIVE_PROJECT
    seen = []
    monkeypatch.setattr(
        AddTaskDialog, "exec",
        lambda self: seen.append((self.non_billable, self.windowTitle())) or QDialog.DialogCode.Rejected,
    )
    section.open_add_nonbillable_task_dialog()
    assert seen == [(True, "Add Non Billable Task")]


def test_plain_add_task_still_opens_the_plain_dialog(qapp, monkeypatch):
    section = _section()
    section.set_nonbillable_creation_allowed(True)
    section._project = ACTIVE_PROJECT
    seen = []
    monkeypatch.setattr(
        AddTaskDialog, "exec",
        lambda self: seen.append((self.non_billable, self.windowTitle())) or QDialog.DialogCode.Rejected,
    )
    section.open_add_task_dialog()
    assert seen == [(False, "Add Task")]


def _refused(status_code):
    exc = ApiError("refused")
    exc.status_code = status_code
    return exc


def test_a_403_on_a_billable_create_hides_that_button_and_leaves_add_task_alone(qapp):
    section = _section()
    section.set_nonbillable_creation_allowed(True)
    section._on_nonbillable_create_refused(_refused(403))
    assert not section.nonbillable_creation_allowed
    assert section.task_creation_allowed


def test_other_billable_create_failures_leave_the_grant_alone(qapp):
    section = _section()
    section.set_nonbillable_creation_allowed(True)
    for code in (500, 502, 422, None):
        section._on_nonbillable_create_refused(_refused(code))
    assert section.nonbillable_creation_allowed


def test_a_403_on_a_plain_create_does_not_touch_the_billable_grant(qapp):
    section = _section()
    section.set_nonbillable_creation_allowed(True)
    section._on_create_refused(_refused(403))
    assert not section.task_creation_allowed
    assert section.nonbillable_creation_allowed


# ── the dashboard ────────────────────────────────────────────────────────────

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


def _shown(widget) -> bool:
    return widget._topbar.is_nonbillable_task_visible()


def _usable(widget) -> bool:
    return _shown(widget) and widget._topbar._add_nonbillable_btn.isEnabled()


def test_login_with_the_grant_shows_the_button_for_an_active_project(dashboard):
    widget, _ = dashboard
    widget.on_login(_profile(can_add_nonbillable_tasks=True))
    widget._task_section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    assert _usable(widget)


def test_login_without_the_grant_shows_nothing(dashboard):
    widget, _ = dashboard
    widget.on_login(_profile(can_add_nonbillable_tasks=False))
    widget._task_section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    assert not _shown(widget)


def test_an_older_profile_with_no_such_field_shows_nothing(dashboard):
    widget, _ = dashboard
    widget.on_login(_profile())
    widget._task_section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    assert not _shown(widget)
    assert widget._topbar._add_task_btn.isEnabled()          # Add Task is exactly as before


def test_session_verification_applies_the_grant(dashboard):
    widget, _ = dashboard
    widget.on_login(_profile())
    widget.on_session_verified(_profile(can_add_nonbillable_tasks=True))
    assert _shown(widget)


def test_a_refresh_round_follows_the_admins_switch_both_ways(dashboard):
    widget, runner = dashboard
    widget.on_login(_profile())
    widget._task_section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    runner.calls.clear()
    widget._refresh_outstanding = 0

    widget.refresh_data()
    runner.succeed("load-profile", _profile(can_add_nonbillable_tasks=True))
    assert _usable(widget)

    runner.calls.clear()
    widget._refresh_outstanding = 0
    widget.refresh_data()
    runner.succeed("load-profile", _profile(can_add_nonbillable_tasks=False))
    assert not _shown(widget)


def test_add_task_blocked_and_billable_allowed_is_the_intended_pairing(dashboard):
    """The administrator excludes Add Task and allows Add Non Billable Task: the
    first is greyed and answers with its reason, the second is a live button."""
    widget, _ = dashboard
    widget.on_login(_profile(can_add_tasks=False, can_add_nonbillable_tasks=True))
    widget._task_section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    assert widget._topbar.is_add_task_blocked()
    assert _usable(widget)


def test_add_task_allowed_and_billable_not_is_the_default_bar(dashboard):
    widget, _ = dashboard
    widget.on_login(_profile(can_add_tasks=True, can_add_nonbillable_tasks=False))
    widget._task_section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    assert not widget._topbar.is_add_task_blocked()
    assert widget._topbar._add_task_btn.isEnabled()
    assert not _shown(widget)


def test_clicking_the_button_opens_the_billable_dialog(dashboard, monkeypatch):
    widget, _ = dashboard
    widget.on_login(_profile(can_add_nonbillable_tasks=True))
    widget._task_section.set_tasks(TASKS, ACTIVE_PROJECT, "#3B82F6")
    seen = []
    monkeypatch.setattr(
        AddTaskDialog, "exec",
        lambda self: seen.append(self.non_billable) or QDialog.DialogCode.Rejected,
    )
    widget._topbar._add_nonbillable_btn.click()
    assert seen == [True]


def test_logout_hides_the_button_for_the_next_user(dashboard):
    widget, _ = dashboard
    widget.on_login(_profile(can_add_nonbillable_tasks=True))
    assert _shown(widget)
    widget.reset_state()
    assert not _shown(widget)
    assert not widget._task_section.nonbillable_creation_allowed


# ── the marker is the same one the backend enforces ──────────────────────────

BACKEND_MARKER = Path(__file__).resolve().parents[2] / "backend" / "app" / "core" / "task_marker.py"


@pytest.mark.skipif(not BACKEND_MARKER.exists(), reason="the backend is not checked out beside the desktop")
def test_the_wording_and_the_recognised_endings_match_the_backends():
    from core.task_marker import NON_BILLABLE_ENDING_PATTERN, NON_BILLABLE_SUFFIX

    source = BACKEND_MARKER.read_text(encoding="utf-8")
    suffix = re.search(r'^NON_BILLABLE_SUFFIX\s*=\s*"([^"]*)"', source, re.M).group(1)
    ending = re.search(r're\.compile\(r"([^"]*)"', source).group(1)
    assert suffix == NON_BILLABLE_SUFFIX
    assert ending == NON_BILLABLE_ENDING_PATTERN
