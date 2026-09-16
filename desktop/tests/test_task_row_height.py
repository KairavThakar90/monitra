"""
Coverage for how tall a task row is.

Each of the row's three column layouts is installed on a QWidget, and a
layout installed on a widget takes the style's default 11px margins on every
side. Nothing asked for them, but they made every row about 22px taller than
its content -- the row's own padding, then 11px, then the 32px button, then
11px again -- and pushed the task name 11px to the right of the TASK header.
The margins are now zero, and the row's own padding is the only padding.

The bounds below are generous enough for the offscreen platform's fallback
fonts and tight enough that the default margins coming back (79px / 85px on
the same rows) fails them.
"""
from ui.task_table import COLUMN_DEFAULT_WIDTHS, TaskRow

ROW_MAX_HEIGHT = 66
ROW_WITH_DETAILS_MAX_HEIGHT = 72


def _row(task, running: bool = False) -> TaskRow:
    row = TaskRow(
        task=task, project_id=1, project_name="P", project_color="#3B82F6",
        is_running=running, readonly=False,
        column_widths=dict(COLUMN_DEFAULT_WIDTHS),
    )
    row.resize(1500, row.sizeHint().height())
    return row


def test_a_plain_row_is_compact(qapp):
    row = _row({"id": 1, "name": "test 5", "created_at": "2026-09-16T14:06:00Z"})
    assert row.sizeHint().height() <= ROW_MAX_HEIGHT
    row.deleteLater()


def test_a_row_with_a_description_and_progress_is_still_compact(qapp):
    row = _row({
        "id": 2, "name": "task", "description": "with a description",
        "estimated_hours": 2, "time_tracked_seconds": 3600,
        "created_at": "2026-09-15T15:00:00Z",
    })
    assert row._desc_label is not None and row._pct_label is not None
    assert row.sizeHint().height() <= ROW_WITH_DETAILS_MAX_HEIGHT
    row.deleteLater()


def test_the_controls_kept_their_size(qapp):
    """Smaller rows, not smaller buttons: the tap targets are unchanged."""
    row = _row({"id": 1, "name": "test 5"})
    assert row._timer_btn.size().toTuple() == (96, 32)
    assert row._menu_btn.size().toTuple() == (30, 34)
    row.deleteLater()


def test_the_column_layouts_carry_no_margins_of_their_own(qapp):
    row = _row({"id": 1, "name": "test 5"})
    for widget in (row._name_widget, row._tracked_widget, row._action_widget):
        margins = widget.layout().contentsMargins()
        assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (0, 0, 0, 0)
    row.deleteLater()


def test_the_task_name_starts_under_the_task_header(qapp):
    """The header's left padding is 16px; so is the row's. With the column
    layout's own margin gone, the leading icon sits at that edge plus the
    row's 3px coloured left accent border (which the header has none of).
    With the default margin it sat at 30."""
    row = _row({"id": 1, "name": "test 5"})
    row.show()                       # nested layouts only run once shown
    qapp.processEvents()
    icon_x = row._leading_icon.mapTo(row, row._leading_icon.rect().topLeft()).x()
    assert 16 <= icon_x <= 19, icon_x
    row.hide()
    row.deleteLater()
