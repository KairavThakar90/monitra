"""Add Task: the optional Non-billable tick, and where a description starts.

Non-billable is a naming convention, not a stored field: ticking it puts
" - Non-billable" on the end of the name the dialog hands back. Everything that
lists a task already shows its name, so nothing else has to learn about it.
These tests pin that the marker is added exactly once, only when asked for,
never blocks a task that does not want it, and still respects the name limit.

The description alignment tests pin a visual defect from the task list: the
description sat under the leading glyph instead of under the task name.
"""
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QCheckBox, QDialog, QFormLayout, QMessageBox

from core.validation.rules import NAME_MAX_LENGTH
from ui.task_table import (
    COLUMN_DEFAULT_WIDTHS, NON_BILLABLE_SUFFIX, AddTaskDialog, TaskRow, TaskSection,
    with_non_billable_suffix,
)


def _dialog(name="Fix the login", description="", checked=False) -> AddTaskDialog:
    dialog = AddTaskDialog("Apollo")
    dialog.name_input.setText(name)
    dialog.desc_input.setPlainText(description)
    dialog.non_billable_check.setChecked(checked)
    return dialog


# ── the pure rule ────────────────────────────────────────────────────────────

def test_the_suffix_is_exactly_dash_non_billable():
    assert NON_BILLABLE_SUFFIX == " - Non-billable"


@pytest.mark.parametrize("typed, expected", [
    ("Fix the login", "Fix the login - Non-billable"),
    ("  Fix the login  ", "Fix the login - Non-billable"),
    ("A", "A - Non-billable"),
    ("Fix - the login", "Fix - the login - Non-billable"),
])
def test_the_marker_is_added(typed, expected):
    assert with_non_billable_suffix(typed) == expected


@pytest.mark.parametrize("already", [
    "Fix the login - Non-billable",
    "Fix the login - non-billable",
    "Fix the login - NON-BILLABLE",
    "Fix the login - Nonbillable",
    "Fix the login -Non-billable  ",
])
def test_a_name_that_already_ends_that_way_is_not_marked_twice(already):
    assert with_non_billable_suffix(already) == already.strip()


def test_the_marker_is_only_recognised_at_the_end():
    assert with_non_billable_suffix("Non-billable cleanup") == "Non-billable cleanup - Non-billable"
    assert with_non_billable_suffix("Fix - Non-billable thing") == "Fix - Non-billable thing - Non-billable"


def test_an_empty_name_stays_empty():
    assert with_non_billable_suffix("") == ""
    assert with_non_billable_suffix("   ") == ""


# ── the dialog ───────────────────────────────────────────────────────────────

def test_the_box_is_optional_and_off_by_default(qapp):
    dialog = AddTaskDialog("Apollo")
    assert isinstance(dialog.non_billable_check, QCheckBox)
    assert dialog.non_billable_check.text() == "Non-billable"
    assert not dialog.non_billable_check.isChecked()


def test_the_box_sits_directly_after_the_description(qapp):
    dialog = AddTaskDialog("Apollo")
    form = dialog.findChild(QFormLayout)
    rows = [form.itemAt(i, QFormLayout.ItemRole.FieldRole).widget() for i in range(form.rowCount())]
    assert rows.index(dialog.non_billable_check) == rows.index(dialog.desc_input) + 1
    assert rows.index(dialog.non_billable_check) == len(rows) - 1


def test_unticked_the_name_is_exactly_what_was_typed(qapp):
    data = _dialog("  Write the report  ").get_data()
    assert data["task_name"] == "Write the report"


def test_ticked_the_name_carries_the_marker(qapp):
    data = _dialog("Write the report", checked=True).get_data()
    assert data["task_name"] == "Write the report - Non-billable"


def test_ticking_changes_nothing_but_the_name(qapp):
    plain = _dialog("Report", "Cover Q3").get_data()
    marked = _dialog("Report", "Cover Q3", checked=True).get_data()
    assert marked["description"] == plain["description"] == "Cover Q3"
    assert marked["estimated_hours"] is plain["estimated_hours"] is None
    assert set(marked) == set(plain)


def test_unticking_again_removes_the_marker(qapp):
    dialog = _dialog("Report", checked=True)
    dialog.non_billable_check.setChecked(False)
    assert dialog.get_data()["task_name"] == "Report"


def test_a_marker_the_person_typed_is_not_doubled(qapp):
    data = _dialog("Report - Non-billable", checked=True).get_data()
    assert data["task_name"] == "Report - Non-billable"


def test_saving_with_the_box_ticked_closes_the_dialog(qapp):
    dialog = _dialog("Report", checked=True)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_a_blank_name_is_still_refused_with_the_box_ticked(qapp, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dialog = _dialog("   ", checked=True)
    dialog.accept()
    assert warned and dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.get_data()["task_name"] == ""      # never just " - Non-billable"


# ── the name limit ───────────────────────────────────────────────────────────

def test_ticking_never_cuts_text_the_person_already_typed(qapp):
    dialog = AddTaskDialog("Apollo")
    dialog.name_input.setText("x" * NAME_MAX_LENGTH)
    dialog.non_billable_check.setChecked(True)
    assert dialog.name_input.text() == "x" * NAME_MAX_LENGTH
    assert dialog.name_input.maxLength() == NAME_MAX_LENGTH


def test_a_name_that_only_fits_without_the_marker_is_refused_not_truncated(qapp, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dialog = _dialog("x" * NAME_MAX_LENGTH)              # typed first, ticked after
    dialog.non_billable_check.setChecked(True)
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert warned and "shorten" in warned[0]
    assert dialog.name_input.text() == "x" * NAME_MAX_LENGTH     # nothing quietly cut


def test_the_longest_name_that_fits_is_accepted_with_the_marker(qapp):
    longest = "x" * (NAME_MAX_LENGTH - len(NON_BILLABLE_SUFFIX))
    dialog = _dialog(longest, checked=True)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert len(dialog.get_data()["task_name"]) == NAME_MAX_LENGTH


# ── through to the create call ───────────────────────────────────────────────

def _section() -> TaskSection:
    api = MagicMock()
    api.timer_elapsed_seconds.return_value = 0
    api.is_timer_running.return_value = False
    section = TaskSection(api=api, task_service=MagicMock())
    section.set_user_role("employee")
    section.set_user_id(54)
    section._project = {"id": 7, "project_name": "Apollo"}
    return section


def _created_name(monkeypatch, *, checked: bool) -> str:
    """Run the real dialog's get_data through `_on_add_task_clicked` and return
    the task name the create request would carry."""
    dialog = _dialog("Write the report", "Cover Q3", checked=checked)
    monkeypatch.setattr(AddTaskDialog, "__new__", lambda cls, *a, **k: dialog, raising=False)
    monkeypatch.setattr(AddTaskDialog, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(AddTaskDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    section = _section()
    section._on_add_task_clicked()
    section.api.run_in_background.call_args.args[0]()
    return section.task_service.create_task.call_args.args[1]


def test_the_create_request_carries_the_marked_name(qapp, monkeypatch):
    assert _created_name(monkeypatch, checked=True) == "Write the report - Non-billable"


def test_the_create_request_is_unchanged_when_the_box_is_not_ticked(qapp, monkeypatch):
    assert _created_name(monkeypatch, checked=False) == "Write the report"


# ── a description starts where the task name starts ──────────────────────────

def _row(task) -> TaskRow:
    row = TaskRow(
        task=task, project_id=1, project_name="P", project_color="#3B82F6",
        is_running=False, readonly=False, column_widths=dict(COLUMN_DEFAULT_WIDTHS),
    )
    row.resize(1500, row.sizeHint().height())
    row.show()
    return row


def _left_edge(widget, ancestor) -> int:
    return widget.mapTo(ancestor, widget.rect().topLeft()).x()


def test_the_description_starts_under_the_task_name_not_the_glyph(qapp):
    row = _row({"id": 1, "name": "deploy the project", "description": "hello"})
    qapp.processEvents()
    name_x = _left_edge(row._name_label, row._name_widget)
    desc_x = _left_edge(row._desc_label, row._name_widget)
    glyph_x = _left_edge(row._leading_icon, row._name_widget)
    assert desc_x == name_x
    assert desc_x > glyph_x                     # and it is no longer under the glyph
    row.deleteLater()


def test_the_alignment_holds_for_a_long_description(qapp):
    row = _row({"id": 2, "name": "task", "description": "word " * 40})
    qapp.processEvents()
    assert _left_edge(row._desc_label, row._name_widget) == _left_edge(row._name_label, row._name_widget)
    row.deleteLater()


def test_a_row_without_a_description_is_unchanged(qapp):
    row = _row({"id": 3, "name": "just a name"})
    assert row._desc_label is None
    row.deleteLater()
