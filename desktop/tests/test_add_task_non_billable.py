"""Add Task / Edit Task: the Non billable marker, and the boxes around it.

Non billable is a naming convention, not a stored field: ticking it in Add Task
puts " - Non billable" on the end of the name the dialog hands back. Everything
that lists a task already shows its name, so nothing else has to learn about it.
These tests pin that:

* the marker is added exactly once, only when asked for, in the wording the
  product chose ("Non billable", no hyphen inside it);
* Add Task never blocks a task that does not want it, and respects the name limit;
* Edit Task treats a marked task's marker as fixed -- only the rest of the name
  can change, and the marker comes back exactly as the task was created;
* the tick box draws a box *and* a tick when ticked (it used to lose its frame);
* the description boxes take plain text only (a rich paste used to bring its
  dark background along);
* a description starts where the task name starts in the task list.
"""
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QMimeData, Qt
from PySide6.QtWidgets import QDialog, QFormLayout, QMessageBox

from core.validation.rules import NAME_MAX_LENGTH
from ui.task_table import (
    COLUMN_DEFAULT_WIDTHS, NON_BILLABLE_SUFFIX, AddTaskDialog, EditTaskDialog,
    ManualTimeEntryDialog, TaskRow, TaskSection, split_non_billable,
    with_non_billable_suffix,
)
from ui.tick_checkbox import TickCheckBox


def _dialog(name="Fix the login", description="", checked=False) -> AddTaskDialog:
    dialog = AddTaskDialog("Apollo")
    dialog.name_input.setText(name)
    dialog.desc_input.setPlainText(description)
    dialog.non_billable_check.setChecked(checked)
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


# ── Add Task ─────────────────────────────────────────────────────────────────

def test_the_box_is_optional_and_off_by_default(qapp):
    dialog = AddTaskDialog("Apollo")
    assert isinstance(dialog.non_billable_check, TickCheckBox)
    assert dialog.non_billable_check.text() == "Non billable"
    assert not dialog.non_billable_check.isChecked()


def test_the_box_sits_directly_after_the_description(qapp):
    dialog = AddTaskDialog("Apollo")
    form = dialog.findChild(QFormLayout)
    rows = [form.itemAt(i, QFormLayout.ItemRole.FieldRole).widget() for i in range(form.rowCount())]
    assert rows.index(dialog.non_billable_check) == rows.index(dialog.desc_input) + 1
    assert rows.index(dialog.non_billable_check) == len(rows) - 1


def test_unticked_the_name_is_exactly_what_was_typed(qapp):
    assert _dialog("  Write the report  ").get_data()["task_name"] == "Write the report"


def test_ticked_the_name_carries_the_marker(qapp):
    assert _dialog("Write the report", checked=True).get_data()["task_name"] == \
        "Write the report - Non billable"


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
    assert _dialog("Report - Non billable", checked=True).get_data()["task_name"] == \
        "Report - Non billable"


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
    assert dialog.get_data()["task_name"] == ""      # never just " - Non billable"


def test_ticking_never_cuts_text_the_person_already_typed(qapp):
    dialog = AddTaskDialog("Apollo")
    dialog.name_input.setText("x" * NAME_MAX_LENGTH)
    dialog.non_billable_check.setChecked(True)
    assert dialog.name_input.text() == "x" * NAME_MAX_LENGTH
    assert dialog.name_input.maxLength() == NAME_MAX_LENGTH


def test_a_name_that_only_fits_without_the_marker_is_refused_not_truncated(qapp, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    dialog = _dialog("x" * NAME_MAX_LENGTH)
    dialog.non_billable_check.setChecked(True)
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert warned and "shorten" in warned[0]
    assert dialog.name_input.text() == "x" * NAME_MAX_LENGTH


def test_the_longest_name_that_fits_is_accepted_with_the_marker(qapp):
    dialog = _dialog("x" * (NAME_MAX_LENGTH - len(NON_BILLABLE_SUFFIX)), checked=True)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert len(dialog.get_data()["task_name"]) == NAME_MAX_LENGTH


# ── the tick box draws a box and a tick ──────────────────────────────────────

def _grab_indicator(dialog):
    """The pixels of the check box's own indicator square."""
    from PySide6.QtWidgets import QStyle, QStyleOptionButton
    box = dialog.non_billable_check
    option = QStyleOptionButton()
    box.initStyleOption(option)
    rect = box.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, box)
    return box.grab().toImage(), rect


def _is_whiteish(color) -> bool:
    return color.red() > 235 and color.green() > 235 and color.blue() > 235


def test_unticked_it_is_a_framed_empty_box(qapp):
    dialog = AddTaskDialog("Apollo")
    dialog.show(); qapp.processEvents()
    image, rect = _grab_indicator(dialog)
    centre = image.pixelColor(rect.center())
    edge = image.pixelColor(rect.left() + 1, rect.center().y())
    assert _is_whiteish(centre)                       # empty inside
    assert not _is_whiteish(edge)                     # but the frame is drawn


def test_ticked_it_is_a_filled_box_with_a_tick_not_a_bare_mark(qapp):
    dialog = AddTaskDialog("Apollo")
    dialog.non_billable_check.setChecked(True)
    dialog.show(); qapp.processEvents()
    image, rect = _grab_indicator(dialog)
    corner = image.pixelColor(rect.left() + 3, rect.top() + 3)
    assert not _is_whiteish(corner)                   # the box is filled, so it still reads as a box
    ticks = sum(
        _is_whiteish(image.pixelColor(x, y))
        for x in range(rect.left() + 2, rect.right() - 1)
        for y in range(rect.top() + 2, rect.bottom() - 1)
    )
    assert ticks >= 6                                 # a white tick is painted on the fill


def test_the_box_can_be_toggled_from_the_keyboard(qapp):
    from PySide6.QtTest import QTest
    dialog = AddTaskDialog("Apollo")
    dialog.show(); qapp.processEvents()
    dialog.non_billable_check.setFocus()
    QTest.keyClick(dialog.non_billable_check, Qt.Key.Key_Space)
    assert dialog.non_billable_check.isChecked()


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
    dialog = _dialog("Write the report", "Cover Q3", checked=checked)
    monkeypatch.setattr(AddTaskDialog, "__new__", lambda cls, *a, **k: dialog, raising=False)
    monkeypatch.setattr(AddTaskDialog, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(AddTaskDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    section = _section()
    section._on_add_task_clicked()
    section.api.run_in_background.call_args.args[0]()
    return section.task_service.create_task.call_args.args[1]


def test_the_create_request_carries_the_marked_name(qapp, monkeypatch):
    assert _created_name(monkeypatch, checked=True) == "Write the report - Non billable"


def test_the_create_request_is_unchanged_when_the_box_is_not_ticked(qapp, monkeypatch):
    assert _created_name(monkeypatch, checked=False) == "Write the report"


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
