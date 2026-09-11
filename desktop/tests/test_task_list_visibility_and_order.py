"""
Coverage for what the desktop task list shows and in which order.

Two presentation rules, both applied on every rebuild so a refresh, a project
switch or an adopted session can never leave the list in a different state:

    * a task the server marks "completed" is not rendered (it is untouched in
      self._tasks, in the local cache and on the backend);
    * tasks are ordered newest-created first.

The running timer is deliberately *not* part of the ordering. Starting a task
must leave its row exactly where it was: floating it to the top shifts every
other row under the user's cursor mid-click.
"""
from unittest.mock import MagicMock

from ui.task_table import TaskSection, is_task_completed


def _make_section() -> TaskSection:
    api = MagicMock()
    api.timer_elapsed_seconds.return_value = 0
    api.is_timer_running.return_value = False
    return TaskSection(api=api, task_service=MagicMock())


def _tasks():
    return [
        {"id": 1, "name": "Task A", "status": "todo", "created_at": "2026-09-10T09:00:00Z"},
        {"id": 2, "name": "Task B", "status": "in_progress", "created_at": "2026-09-11T09:00:00Z"},
        {"id": 3, "name": "Task C", "status": "completed", "created_at": "2026-09-12T09:00:00Z"},
        {"id": 4, "name": "Task D", "status": "todo", "created_at": "2026-09-08T09:00:00Z"},
    ]


def _rendered_ids(section):
    return [row.task.get("id") for row in section._task_rows]


def _running_ids(section):
    return [row.task.get("id") for row in section._task_rows if row._is_running]


def test_is_task_completed_reads_both_status_shapes():
    assert is_task_completed({"status": "completed"})
    assert is_task_completed({"status": "Completed"})
    assert is_task_completed({"status": {"id": 3, "name": "completed"}})
    assert not is_task_completed({"status": "in_progress"})
    assert not is_task_completed({"status": {"id": 1, "name": "todo"}})
    assert not is_task_completed({})


def test_completed_tasks_are_not_rendered(qapp):
    section = _make_section()
    section.set_tasks(_tasks(), {"id": 9, "project_name": "P"}, "#3B82F6")

    assert _rendered_ids(section) == [2, 1, 4]


def test_newest_task_is_rendered_at_the_top(qapp):
    section = _make_section()
    tasks = [
        {"id": 1, "name": "Older", "status": "todo", "created_at": "2026-09-10T09:00:00Z"},
        {"id": 2, "name": "Newest", "status": "todo", "created_at": "2026-09-11T09:00:00Z"},
    ]
    section.set_tasks(tasks, {"id": 9, "project_name": "P"}, "#3B82F6")

    assert _rendered_ids(section) == [2, 1]


def test_tasks_without_a_created_at_keep_the_backend_order_below_the_rest(qapp):
    """Cached rows from an older response must still land deterministically."""
    section = _make_section()
    tasks = [
        {"id": 1, "name": "Legacy A", "status": "todo"},
        {"id": 2, "name": "Legacy B", "status": "todo"},
        {"id": 3, "name": "Dated", "status": "todo", "created_at": "2026-09-01T09:00:00Z"},
    ]
    section.set_tasks(tasks, {"id": 9, "project_name": "P"}, "#3B82F6")

    assert _rendered_ids(section) == [3, 1, 2]


def test_completed_tasks_stay_in_the_underlying_task_list(qapp):
    """Hiding is presentation-only -- nothing is dropped or re-statused."""
    section = _make_section()
    section.set_tasks(_tasks(), {"id": 9, "project_name": "P"}, "#3B82F6")

    assert [t["id"] for t in section._tasks] == [1, 2, 3, 4]
    assert section._tasks[2]["status"] == "completed"


def test_a_tracked_task_is_not_pulled_to_the_top_on_a_rebuild(qapp):
    """Task D is the oldest, so a pinning rule would be visible immediately."""
    section = _make_section()
    section._running_task_id = 4
    section.set_tasks(_tasks(), {"id": 9, "project_name": "P"}, "#3B82F6")

    assert _rendered_ids(section) == [2, 1, 4]
    assert _running_ids(section) == [4]


def test_ordering_and_filtering_survive_a_reload(qapp):
    section = _make_section()
    section._running_task_id = 4
    section.set_tasks(_tasks(), {"id": 9, "project_name": "P"}, "#3B82F6")
    section.set_tasks(_tasks(), {"id": 9, "project_name": "P"}, "#3B82F6")

    assert _rendered_ids(section) == [2, 1, 4]


def test_starting_a_task_leaves_its_row_where_it_was(qapp):
    section = _make_section()
    section.set_tasks(_tasks(), {"id": 9, "project_name": "P"}, "#3B82F6")
    assert _rendered_ids(section) == [2, 1, 4]
    rows_before = list(section._task_rows)

    section._on_timer_started({"task_id": 4, "entry_id": 77, "task_name": "Task D"})

    # Same order, and the very same widget instances -- the row is marked
    # running in place, not moved and not rebuilt.
    assert _rendered_ids(section) == [2, 1, 4]
    assert section._task_rows == rows_before
    assert _running_ids(section) == [4]


def test_stopping_a_task_leaves_the_order_untouched(qapp):
    section = _make_section()
    section.set_tasks(_tasks(), {"id": 9, "project_name": "P"}, "#3B82F6")
    section._on_timer_started({"task_id": 4, "entry_id": 77, "task_name": "Task D"})
    section._on_timer_stopped({"session": {"task_id": 4}, "elapsed_seconds": 30})

    assert _rendered_ids(section) == [2, 1, 4]
    assert not any(row._is_running for row in section._task_rows)


def test_switching_the_tracked_task_moves_no_rows(qapp):
    section = _make_section()
    section.set_tasks(_tasks(), {"id": 9, "project_name": "P"}, "#3B82F6")

    section._on_timer_started({"task_id": 4, "entry_id": 77, "task_name": "Task D"})
    section._on_timer_started({"task_id": 1, "entry_id": 78, "task_name": "Task A"})

    assert _rendered_ids(section) == [2, 1, 4]
    assert _running_ids(section) == [1]


def test_an_adopted_running_session_leaves_its_row_in_place(qapp):
    """Resuming an existing timer on startup goes through sync_active_timer."""
    section = _make_section()
    section.set_tasks(_tasks(), {"id": 9, "project_name": "P"}, "#3B82F6")

    section.sync_active_timer(task_id=4, entry_id=88, elapsed=120)

    assert _rendered_ids(section) == [2, 1, 4]
    assert _running_ids(section) == [4]


def test_a_completed_task_that_is_being_tracked_stays_visible(qapp):
    """It would otherwise be hidden with a live timer and no Stop button."""
    section = _make_section()
    section._running_task_id = 3
    section.set_tasks(_tasks(), {"id": 9, "project_name": "P"}, "#3B82F6")

    # Task C is first because it is the newest, not because it is tracked.
    assert _rendered_ids(section) == [3, 2, 1, 4]


def test_search_still_filters_within_the_visible_tasks(qapp):
    section = _make_section()
    section.set_tasks(_tasks(), {"id": 9, "project_name": "P"}, "#3B82F6")
    section._search_text = "task"
    section._rebuild_rows()

    assert _rendered_ids(section) == [2, 1, 4]
