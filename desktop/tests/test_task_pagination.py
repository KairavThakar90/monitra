"""
Coverage for the task list's pagination.

The task card pages its rows the way the sidebar pages projects
(`PROJECTS_PER_PAGE`): TASKS_PER_PAGE rows at a time, a prev / "n/N" / next
control that appears only when the filtered list runs past one page, and a
page that is reset on a project switch or a new search but kept -- clamped
into range -- across a refresh of the same project.

Pages are cut from the *filtered, ordered* list, so completed tasks and
search misses never occupy a slot, and the page boundary falls in the same
place the user sees the rows in.
"""
from unittest.mock import MagicMock

from ui.task_table import TASKS_PER_PAGE, TaskSection


def _make_section() -> TaskSection:
    api = MagicMock()
    api.timer_elapsed_seconds.return_value = 0
    api.is_timer_running.return_value = False
    return TaskSection(api=api, task_service=MagicMock())


def _tasks(count: int, status: str = "todo"):
    # Newest first once ordered: task `count` has the latest created_at.
    return [
        {
            "id": i,
            "name": f"Task {i:02d}",
            "status": status,
            "created_at": f"2026-09-{(i % 28) + 1:02d}T09:{i % 60:02d}:00Z",
        }
        for i in range(1, count + 1)
    ]


def _project(pid: int = 9):
    return {"id": pid, "project_name": f"P{pid}"}


def _rendered_ids(section):
    return [row.task.get("id") for row in section._task_rows]


def test_a_short_list_shows_every_row_and_no_pager(qapp):
    section = _make_section()
    section.set_tasks(_tasks(TASKS_PER_PAGE), _project(), "#3B82F6")

    assert len(section._task_rows) == TASKS_PER_PAGE
    assert section._pagination_widget.isHidden()


def test_a_long_list_is_cut_into_pages(qapp):
    section = _make_section()
    section.set_tasks(_tasks(24), _project(), "#3B82F6")

    assert section._current_page == 1
    assert not section._pagination_widget.isHidden()
    assert section._page_label.text() == "1/3"
    assert section._page_range_label.text() == "Showing 1–10 of 24"
    assert len(section._task_rows) == TASKS_PER_PAGE
    assert not section._prev_page_btn.isEnabled()
    assert section._next_page_btn.isEnabled()

    section._next_page()
    assert section._current_page == 2
    assert section._page_label.text() == "2/3"
    assert section._page_range_label.text() == "Showing 11–20 of 24"
    assert len(section._task_rows) == TASKS_PER_PAGE
    assert section._prev_page_btn.isEnabled()
    assert section._next_page_btn.isEnabled()

    section._next_page()
    assert section._current_page == 3
    assert section._page_label.text() == "3/3"
    assert section._page_range_label.text() == "Showing 21–24 of 24"
    assert len(section._task_rows) == 4
    assert not section._next_page_btn.isEnabled()

    section._next_page()                      # past the end: stays put
    assert section._current_page == 3

    section._prev_page()
    assert section._current_page == 2
    section._prev_page()
    section._prev_page()                      # before the start: stays put
    assert section._current_page == 1


def test_pages_are_cut_from_the_ordered_list(qapp):
    """Page 1 is the ten newest tasks; page 2 continues from the eleventh."""
    section = _make_section()
    section.set_tasks(_tasks(24), _project(), "#3B82F6")

    first = _rendered_ids(section)
    section._next_page()
    second = _rendered_ids(section)

    everything = sorted(
        section._visible_tasks(), key=section._task_order_key
    )
    ordered_ids = [t["id"] for t in everything]
    assert first == ordered_ids[:TASKS_PER_PAGE]
    assert second == ordered_ids[TASKS_PER_PAGE:2 * TASKS_PER_PAGE]


def test_completed_tasks_do_not_take_a_slot(qapp):
    section = _make_section()
    tasks = _tasks(12) + _tasks(30, status="completed")[12:]   # 12 open, 18 done
    section.set_tasks(tasks, _project(), "#3B82F6")

    assert section._page_label.text() == "1/2"
    assert section._page_range_label.text() == "Showing 1–10 of 12"
    section._next_page()
    assert len(section._task_rows) == 2


def test_a_search_starts_from_page_one_and_pages_the_matches(qapp):
    section = _make_section()
    section.set_tasks(_tasks(24), _project(), "#3B82F6")
    section._next_page()
    section._next_page()
    assert section._current_page == 3

    section.apply_search("task")              # every task matches -> 3 pages
    assert section._current_page == 1
    assert section._page_label.text() == "1/3"
    assert len(section._task_rows) == TASKS_PER_PAGE

    section.apply_search("Task 1")            # Task 10..19 -> exactly one page
    assert section._current_page == 1
    assert len(section._task_rows) == TASKS_PER_PAGE
    assert all("Task 1" in row.task["name"] for row in section._task_rows)
    assert section._pagination_widget.isHidden()

    section.apply_search("Task 2")            # Task 20..24 -> 5 matches
    assert len(section._task_rows) == 5
    assert section._pagination_widget.isHidden()

    section.apply_search("nothing like this")
    assert section._task_rows == []
    assert section._pagination_widget.isHidden()
    assert "No tasks match" in section._status_label.text()


def test_a_refresh_of_the_same_project_keeps_the_page(qapp):
    """A background reconcile must not yank the user back to page 1."""
    section = _make_section()
    section.set_tasks(_tasks(24), _project(), "#3B82F6")
    section._next_page()
    assert section._current_page == 2

    section.set_tasks(_tasks(24), _project(), "#3B82F6")
    assert section._current_page == 2
    assert section._page_label.text() == "2/3"


def test_a_shorter_refresh_falls_back_to_the_last_page(qapp):
    """Never an empty page: fewer tasks than the page needs clamps it."""
    section = _make_section()
    section.set_tasks(_tasks(24), _project(), "#3B82F6")
    section._next_page()
    section._next_page()
    assert section._current_page == 3

    section.set_tasks(_tasks(15), _project(), "#3B82F6")
    assert section._current_page == 2
    assert section._page_label.text() == "2/2"
    assert len(section._task_rows) == 5

    section.set_tasks(_tasks(3), _project(), "#3B82F6")
    assert section._current_page == 1
    assert len(section._task_rows) == 3
    assert section._pagination_widget.isHidden()


def test_switching_projects_starts_at_page_one(qapp):
    section = _make_section()
    section.set_tasks(_tasks(24), _project(1), "#3B82F6")
    section._next_page()
    assert section._current_page == 2

    section.set_tasks(_tasks(24), _project(2), "#3B82F6")
    assert section._current_page == 1
    assert section._page_label.text() == "1/3"


def test_clearing_the_list_resets_the_page(qapp):
    section = _make_section()
    section.set_tasks(_tasks(24), _project(), "#3B82F6")
    section._next_page()

    section.clear()
    assert section._current_page == 1
    assert section._task_rows == []
    assert section._pagination_widget.isHidden()


def test_the_pager_sits_at_the_bottom_of_the_card(qapp):
    """Below the rows, inside the card -- the last thing in its layout."""
    section = _make_section()
    section.set_tasks(_tasks(24), _project(), "#3B82F6")
    card_layout = section._pagination_widget.parent().layout()
    last = card_layout.itemAt(card_layout.count() - 1).widget()
    assert last is section._pagination_widget
