"""
Coverage for the running-task row's visual style: the brand's light tint as
its fill (the same tint every row shows under the pointer and the selected
row shows at rest), a brand-violet outline, and the small dot next to the
task name (_active_dot) in the same colour. No shadow, and everything else
-- text colors, geometry, the progress bar -- renders identically to the
idle state.

The outline was SUCCESS green, an accent that appeared nowhere else on the
row. It is now ACTIVE_ROW_BORDER, the stop the running "Stop" button leads
with, so the row and the control that put it in that state share a colour.
"""
from ui.styles import ACTIVE_ROW_BORDER, CARD_BG, PRIMARY_LIGHT, SUCCESS
from ui.task_table import TaskRow


def _make_row(**overrides):
    task = {
        "id": 1, "name": "Design homepage", "time_tracked_seconds": 120,
        "estimated_hours": 4, "description": "A short description",
    }
    task.update(overrides)
    return TaskRow(task, project_id=1, project_name="Test", project_color="#3B82F6")


def test_running_row_has_no_shadow_effect(qapp):
    row = _make_row()
    row.mark_running(entry_id=5)
    assert row.graphicsEffect() is None


def test_running_row_is_outlined_in_the_brand_accent(qapp):
    row = _make_row()
    assert ACTIVE_ROW_BORDER not in row.styleSheet()

    row.mark_running(entry_id=5)
    assert f"border: 2px solid {ACTIVE_ROW_BORDER};" in row.styleSheet()


def test_the_running_row_carries_no_green(qapp):
    """The old SUCCESS outline is gone from both states -- it was the only
    green on the row and matched nothing else in the theme."""
    row = _make_row()
    assert SUCCESS not in row.styleSheet()
    row.mark_running(entry_id=5)
    assert SUCCESS not in row.styleSheet()


def test_border_returns_to_normal_when_the_row_stops(qapp):
    row = _make_row()
    idle_style = row.styleSheet()

    row.mark_running(entry_id=5)
    row.mark_stopped(banked_seconds=60)
    assert row.styleSheet() == idle_style
    assert ACTIVE_ROW_BORDER not in row.styleSheet()


def test_idle_row_reserves_the_border_so_running_does_not_shift_geometry(qapp):
    """The active outline replaces a transparent border of the same width,
    so the row's contents do not move between the two states."""
    row = _make_row()
    assert "border: 2px solid transparent;" in row.styleSheet()


def test_running_row_is_tinted_like_the_hover_state(qapp):
    """The running row's fill is the brand's light surface -- the tint every
    row already shows under the pointer -- so the active task reads as one
    highlighted row. No gradient fill: the gradient stays on the outline."""
    row = _make_row()
    assert f"background: {CARD_BG};" in row.styleSheet()
    row.mark_running(entry_id=5)
    assert f"background: {PRIMARY_LIGHT};" in row.styleSheet()
    assert f"background: {CARD_BG};" not in row.styleSheet()
    assert "background: qlineargradient" not in row.styleSheet()
    row.mark_stopped(banked_seconds=60)
    assert f"background: {CARD_BG};" in row.styleSheet()


def test_active_dot_is_the_only_visible_running_indicator(qapp):
    row = _make_row()
    assert row._active_dot.isHidden()
    row.mark_running(entry_id=5)
    assert not row._active_dot.isHidden()
    assert row._active_dot.pixmap() is not None
    # The dot and the outline are one indicator of one state, so they are
    # drawn in one colour rather than the dot keeping the old green.
    dot = row._active_dot.pixmap().toImage()
    painted = {
        dot.pixelColor(x, y).name()
        for x in range(dot.width()) for y in range(dot.height())
    }
    assert ACTIVE_ROW_BORDER.lower() in painted
    assert SUCCESS.lower() not in painted
    # No background/border on the dot itself.
    assert row._active_dot.styleSheet() == "background: transparent;"

    row.mark_stopped(banked_seconds=60)
    assert row._active_dot.isHidden()


def test_running_row_text_colors_are_unchanged_from_idle(qapp):
    """Labels no longer relighten for a dark background -- there is no dark
    background to contrast against any more."""
    row = _make_row()
    idle_name_style = row._name_label.styleSheet()
    idle_desc_style = row._desc_label.styleSheet()
    idle_time_style = row._time_label.styleSheet()

    row.mark_running(entry_id=5)
    assert row._name_label.styleSheet() == idle_name_style
    assert row._desc_label.styleSheet() == idle_desc_style
    assert row._time_label.styleSheet() == idle_time_style


def test_progress_bar_stays_in_light_mode_regardless_of_running_state(qapp):
    row = _make_row()
    assert row._progress_bar is not None
    assert row._progress_bar._dark is False
    row.mark_running(entry_id=5)
    assert row._progress_bar._dark is False
    row.mark_stopped(banked_seconds=60)
    assert row._progress_bar._dark is False


def test_column_containers_stay_transparent_so_the_row_background_shows_through(qapp):
    """Regression: once TaskSection's stylesheet is applied anywhere in the
    app, a plain QWidget with no stylesheet of its own is promoted to an
    opaque, styled background and paints over whatever its parent drew.
    Every column container and inter-column spacer must declare
    "background: transparent" explicitly so the row's own background is
    what actually shows."""
    row = _make_row()
    row.mark_running(entry_id=5)

    for widget in (row._name_widget, row._tracked_widget, row._action_widget):
        assert "background: transparent" in widget.styleSheet()
    assert "background: transparent" in row._created_label.styleSheet()

    layout = row.layout()
    spacers = [
        layout.itemAt(i).widget()
        for i in range(layout.count())
        if layout.itemAt(i).widget() is not None
        and layout.itemAt(i).widget() not in (
            row._name_widget, row._created_label, row._tracked_widget, row._action_widget,
        )
    ]
    assert spacers, "expected at least one inter-column spacer widget"
    for spacer in spacers:
        assert "background: transparent" in spacer.styleSheet()


def test_stop_button_and_kebab_menu_have_breathing_room(qapp):
    """The Start/Stop button and the three-dot menu used to sit right next
    to each other (6px gap) -- widened so they read as two distinct
    controls, not one fused element."""
    row = _make_row()
    action_layout = row._action_widget.layout()
    assert action_layout.spacing() >= 12
