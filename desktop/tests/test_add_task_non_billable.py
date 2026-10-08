"""Add Non Billable Task, Edit Task, and the boxes around the Non billable marker.

Non billable is a naming convention, not a stored field: a task made with the
top bar's *Add Non Billable Task* button ends " - Non billable". Everything that
lists a task already shows its name, so nothing else has to learn about it.
These tests pin that:

* plain Add Task is exactly what it always was -- no tick, no marker, no change;
* the Add Non Billable Task dialog is the same dialog with a fixed "Non billable"
  tag, and hands back the name with the marker on it exactly once, in the
  wording the product chose ("Non billable", no hyphen inside it);
* Edit Task treats a marked task's marker as fixed -- only the rest of the name
  can change, and the marker comes back exactly as the task was created;
* the description boxes take plain text only (a rich paste used to bring its
  dark background along);
* a description starts where the task name starts in the task list.

The button itself (who sees it, when it is usable) is in
`test_add_billable_task_button.py`.
"""
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QDialog, QMessageBox

from core.task_marker import NON_BILLABLE_SUFFIX, split_non_billable, with_non_billable_suffix
from core.validation.rules import NAME_MAX_LENGTH
from ui.task_table import (
    COLUMN_DEFAULT_WIDTHS, AddTaskDialog, EditTaskDialog, ManualTimeEntryDialog,
    TaskRow, TaskSection,
)


def _dialog(name="Fix the login", description="", billable=False) -> AddTaskDialog:
    dialog = AddTaskDialog("Apollo", non_billable=billable)
    dialog.name_input.setText(name)
    dialog.desc_input.setPlainText(description)
    return dialog


STATUSES = [{"id": 1, "name": "Todo"}, {"id": 2, "name": "Done"}]


def _edit(name, description="d") -> EditTaskDialog:
    return EditTaskDialog(
        {"id": 9, "name": name, "description": description, "status": {"id": 1}}, STATUSES
    )


# ── the pure rule ────────────────────────────────────────────────────────────

def test_the_suffix_is_dash_then_non_billable_as_two_words():
    assert NON_BILLABLE_SUFFIX == " - Non billable"
    assert "Non-billable" not in NON_BILLABLE_SUFFIX


@pytest.mark.parametrize("typed, expected", [
    ("Fix the login", "Fix the login - Non billable"),
    ("  Fix the login  ", "Fix the login - Non billable"),
    ("A", "A - Non billable"),
    ("Fix - the login", "Fix - the login - Non billable"),
])
def test_the_marker_is_added(typed, expected):
    assert with_non_billable_suffix(typed) == expected


@pytest.mark.parametrize("already", [
    "Fix the login - Non billable",
    "Fix the login - non billable",
    "Fix the login - NON BILLABLE",
    "Fix the login - Non-billable",       # the earlier spelling
    "Fix the login - Nonbillable",
    "Fix the login -Non billable  ",
])
def test_a_name_that_already_ends_that_way_is_not_marked_twice_and_is_made_canonical(already):
    assert with_non_billable_suffix(already) == "Fix the login - Non billable"


def test_the_marker_is_only_recognised_at_the_end():
    assert with_non_billable_suffix("Non billable cleanup") == "Non billable cleanup - Non billable"
    assert with_non_billable_suffix("Fix - Non billable thing") == "Fix - Non billable thing - Non billable"


def test_an_empty_name_stays_empty():
    assert with_non_billable_suffix("") == ""
    assert with_non_billable_suffix("   ") == ""
    assert with_non_billable_suffix(" - Non billable") == ""     # a marker alone is not a name


def test_split_returns_the_marker_as_it_was_written():
    assert split_non_billable("Fix it - Non billable") == ("Fix it", " - Non billable")
    assert split_non_billable("Fix it - Non-billable") == ("Fix it", " - Non-billable")
    assert split_non_billable("Fix it") == ("Fix it", None)
    assert split_non_billable("") == ("", None)


# ── plain Add Task is unchanged ──────────────────────────────────────────────

def test_plain_add_task_has_no_tick_box_and_no_marker_tag(qapp):
    dialog = AddTaskDialog("Apollo")
    assert dialog.non_billable is False
    assert dialog.marker_label is None
    assert not hasattr(dialog, "non_billable_check")
    assert dialog.windowTitle() == "Add Task"


def test_plain_add_task_is_its_original_size(qapp):
    dialog = AddTaskDialog("Apollo")
    assert (dialog.width(), dialog.height()) == (400, 260)


def test_plain_add_task_hands_back_exactly_what_was_typed(qapp):
    data = _dialog("  Write the report  ", "Cover Q3").get_data()
    assert data == {"task_name": "Write the report", "description": "Cover Q3", "estimated_hours": None}


def test_plain_add_task_never_adds_the_marker_even_to_a_name_that_has_one(qapp):
    assert _dialog("Report - Non billable").get_data()["task_name"] == "Report - Non billable"
    assert _dialog("Report").get_data()["task_name"] == "Report"


def test_plain_add_task_still_refuses_a_blank_name(qapp, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dialog = _dialog("   ")
    dialog.accept()
    assert warned and dialog.result() != QDialog.DialogCode.Accepted


# ── Add Non Billable Task ────────────────────────────────────────────────────────

def test_the_billable_dialog_shows_a_fixed_non_billable_tag_beside_the_name(qapp):
    dialog = AddTaskDialog("Apollo", non_billable=True)
    assert dialog.windowTitle() == "Add Non Billable Task"
    assert dialog.marker_label is not None and dialog.marker_label.text() == "Non billable"
    assert not dialog.name_input.text()                    # the person types only the name


def test_the_billable_dialog_has_no_box_to_untick_the_marker(qapp):
    dialog = AddTaskDialog("Apollo", non_billable=True)
    assert not hasattr(dialog, "non_billable_check")


def test_the_billable_dialog_is_the_same_size_as_add_task(qapp):
    dialog = AddTaskDialog("Apollo", non_billable=True)
    assert (dialog.width(), dialog.height()) == (400, 260)


def test_the_billable_dialog_adds_the_marker_automatically(qapp):
    assert _dialog("Write the report", billable=True).get_data()["task_name"] == \
        "Write the report - Non billable"
    assert _dialog("  Write the report  ", billable=True).get_data()["task_name"] == \
        "Write the report - Non billable"


def test_the_billable_dialog_changes_nothing_but_the_name(qapp):
    plain = _dialog("Report", "Cover Q3").get_data()
    marked = _dialog("Report", "Cover Q3", billable=True).get_data()
    assert marked["description"] == plain["description"] == "Cover Q3"
    assert marked["estimated_hours"] is plain["estimated_hours"] is None
    assert set(marked) == set(plain)


def test_a_marker_the_person_typed_is_not_doubled(qapp):
    for typed in ("Report - Non billable", "Report - Non-billable", "Report - non billable"):
        assert _dialog(typed, billable=True).get_data()["task_name"] == "Report - Non billable", typed


def test_saving_closes_the_billable_dialog(qapp):
    dialog = _dialog("Report", billable=True)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_a_blank_name_is_refused_and_is_never_just_the_marker(qapp, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dialog = _dialog("   ", billable=True)
    dialog.accept()
    assert warned and dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.get_data()["task_name"] == ""


def test_a_name_that_only_fits_without_the_marker_is_refused_not_truncated(qapp, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dialog = _dialog("x" * NAME_MAX_LENGTH, billable=True)
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert warned and "shorten" in warned[0]
    assert dialog.name_input.text() == "x" * NAME_MAX_LENGTH       # nothing quietly cut


def test_the_longest_name_that_fits_is_accepted_with_the_marker(qapp):
    dialog = _dialog("x" * (NAME_MAX_LENGTH - len(NON_BILLABLE_SUFFIX)), billable=True)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert len(dialog.get_data()["task_name"]) == NAME_MAX_LENGTH


# ── plain-text descriptions ──────────────────────────────────────────────────

def _rich_paste(widget, plain="Release readiness is NO for now."):
    mime = QMimeData()
    mime.setHtml(
        '<div style="background-color:#111111;color:#ffffff">Release readiness is <b>NO</b> for now.</div>'
    )
    mime.setText(plain)
    widget.insertFromMimeData(mime)


@pytest.mark.parametrize("make", [
    lambda: AddTaskDialog("Apollo"),
    lambda: _edit("Plain task"),
    lambda: ManualTimeEntryDialog([{"id": 1, "project_name": "P"}], 1),
])
def test_a_rich_paste_arrives_as_plain_text(qapp, make):
    dialog = make()
    dialog.desc_input.clear()
    _rich_paste(dialog.desc_input)
    assert not dialog.desc_input.acceptRichText()
    assert dialog.desc_input.toPlainText() == "Release readiness is NO for now."
    html = dialog.desc_input.toHtml().lower()
    assert "background" not in html.split("<body", 1)[1]
    assert "#ffffff" not in html.split("<body", 1)[1]


def test_a_long_paste_is_kept_whole_as_plain_text(qapp):
    dialog = AddTaskDialog("Apollo")
    dialog.desc_input.clear()
    _rich_paste(dialog.desc_input, "line one\n" * 200)
    assert dialog.get_data()["description"].count("line one") == 200


# ── Edit Task: a Non billable task keeps its marker ──────────────────────────

def test_edit_shows_only_the_changeable_part_of_a_marked_task(qapp):
    dialog = _edit("dsadsd - Non billable")
    assert dialog.name_input.text() == "dsadsd"
    assert dialog.marker_label is not None and dialog.marker_label.text() == "Non billable"


def test_edit_hands_back_the_marker_exactly_as_the_task_was_created(qapp):
    dialog = _edit("dsadsd - Non billable")
    assert dialog.get_data()["task_name"] == "dsadsd - Non billable"


def test_changing_the_name_keeps_the_marker(qapp):
    dialog = _edit("dsadsd - Non billable")
    dialog.name_input.setText("renamed task")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.get_data()["task_name"] == "renamed task - Non billable"


def test_the_marker_cannot_be_removed_by_editing(qapp):
    dialog = _edit("dsadsd - Non billable")
    for attempt in ("renamed", "renamed - Non billable", "renamed - non-billable", "  renamed  "):
        dialog.name_input.setText(attempt)
        assert dialog.get_data()["task_name"] == "renamed - Non billable", attempt


def test_the_marker_is_never_doubled(qapp):
    dialog = _edit("dsadsd - Non billable")
    dialog.name_input.setText("dsadsd - Non billable")
    assert dialog.get_data()["task_name"] == "dsadsd - Non billable"


def test_an_earlier_spelling_is_kept_as_it_was_created(qapp):
    dialog = _edit("old task - Non-billable")
    assert dialog.name_input.text() == "old task"
    assert dialog.get_data()["task_name"] == "old task - Non-billable"
    dialog.name_input.setText("renamed")
    assert dialog.get_data()["task_name"] == "renamed - Non-billable"


def test_a_marked_task_cannot_be_saved_with_an_empty_name(qapp, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dialog = _edit("dsadsd - Non billable")
    dialog.name_input.setText("   ")
    dialog.accept()
    assert warned and dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.get_data()["task_name"] == ""      # never a name that is only the marker


def test_a_name_too_long_with_the_marker_is_refused_not_cut(qapp, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dialog = _edit("dsadsd - Non billable")
    dialog.name_input.setText("x" * NAME_MAX_LENGTH)
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert warned and "shorten" in warned[0]
    assert dialog.name_input.text() == "x" * NAME_MAX_LENGTH


def test_status_and_description_still_edit_normally_on_a_marked_task(qapp):
    dialog = _edit("dsadsd - Non billable", "old")
    dialog.desc_input.setPlainText("new description")
    dialog.status_combo.setCurrentIndex(1)
    data = dialog.get_data()
    assert (data["description"], data["status_id"]) == ("new description", 2)
    assert data["task_name"] == "dsadsd - Non billable"


def test_a_plain_task_edits_exactly_as_before(qapp):
    dialog = _edit("Plain task")
    assert dialog.marker_label is None
    assert dialog.name_input.text() == "Plain task"
    dialog.name_input.setText("Renamed plain task")
    assert dialog.get_data()["task_name"] == "Renamed plain task"


def test_a_plain_task_is_not_given_a_marker_by_editing(qapp):
    dialog = _edit("Plain task")
    dialog.name_input.setText("Plain task two")
    assert split_non_billable(dialog.get_data()["task_name"])[1] is None


# ── through to the create call ───────────────────────────────────────────────

def _section(billable_allowed=True) -> TaskSection:
    api = MagicMock()
    api.timer_elapsed_seconds.return_value = 0
    api.is_timer_running.return_value = False
    section = TaskSection(api=api, task_service=MagicMock())
    section.set_user_role("employee")
    section.set_user_id(54)
    section.set_nonbillable_creation_allowed(billable_allowed)
    section._project = {"id": 7, "project_name": "Apollo"}
    return section


def _created_name(monkeypatch, *, billable: bool) -> str:
    """Run the real dialog (opened the way the button opens it) through the
    section and return the task name the create request would carry."""
    opened = {}
    real_init = AddTaskDialog.__init__

    def init(self, project_name, parent=None, *, non_billable=False):
        real_init(self, project_name, parent, non_billable=non_billable)
        self.name_input.setText("Write the report")
        self.desc_input.setPlainText("Cover Q3")
        opened["non_billable"] = non_billable

    monkeypatch.setattr(AddTaskDialog, "__init__", init)
    monkeypatch.setattr(AddTaskDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    section = _section()
    if billable:
        section.open_add_nonbillable_task_dialog()
    else:
        section.open_add_task_dialog()
    assert opened["non_billable"] is billable
    section.api.run_in_background.call_args.args[0]()
    return section.task_service.create_task.call_args.args[1]


def test_the_billable_button_creates_the_marked_name(qapp, monkeypatch):
    assert _created_name(monkeypatch, billable=True) == "Write the report - Non billable"


def test_the_plain_button_creates_the_name_as_typed(qapp, monkeypatch):
    assert _created_name(monkeypatch, billable=False) == "Write the report"


def test_the_update_request_carries_the_locked_marker(qapp, monkeypatch):
    section = _section()
    dialog = _edit("dsadsd - Non billable")
    dialog.name_input.setText("renamed")
    monkeypatch.setattr(EditTaskDialog, "__new__", lambda cls, *a, **k: dialog, raising=False)
    monkeypatch.setattr(EditTaskDialog, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(EditTaskDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    row = MagicMock()
    row.task = {"id": 9, "name": "dsadsd - Non billable"}
    row.project_id = 7
    section._handle_edit_request(row)
    section.api.run_in_background.call_args.args[0]()
    assert section.task_service.update_task.call_args.args[2] == "renamed - Non billable"


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
    assert desc_x > glyph_x
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
