"""A task's description is plain text -- in the dialogs, and on screen.

Adding or editing a task on the desktop takes ordinary sentences and nothing
else: HTML, CSS, JavaScript, jQuery and other code are refused *in the dialog*,
where the person can fix them, with a message that says what to change -- the
dialog stays open and nothing is sent. (The backend refuses the same content, so
a build that skipped this check would only turn the refusal into a failed sync.)

On screen, a description is the person's own words shown literally: a stored one
that happens to contain markup reads as that text, never as bold type or an
image.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox

from ui.task_table import COLUMN_DEFAULT_WIDTHS, AddTaskDialog, EditTaskDialog, TaskRow

STATUSES = [{"id": 1, "name": "Todo"}, {"id": 2, "name": "Done"}]

CODE = [
    "<b>Important</b> task",
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "javascript:alert(1)",
    "$('#btn').click(function(){ alert(1) })",
    "jQuery('#a').hide()",
    "alert(document.cookie)",
    "document.write('x')",
    "function foo() { return 1; }",
    "body { color: red; }",
    "' OR 1=1 --",
]

PROSE = [
    "Cover Q3 and Q4",
    "Fix login bug: user can't sign in (see #123)",
    "Call the client at 5pm & confirm the budget; 5 < 10 pages",
    "Line one\nLine two",
    "ウェブサイトの再構築",
]


@pytest.fixture
def warnings(monkeypatch):
    """Every QMessageBox.warning the dialog raises, instead of a real modal."""
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda parent, title, text, *a, **k: shown.append(text)
    )
    return shown


def _add(description: str) -> AddTaskDialog:
    dialog = AddTaskDialog("Apollo")
    dialog.name_input.setText("Write the report")
    dialog.desc_input.setPlainText(description)
    return dialog


def _edit(description: str) -> EditTaskDialog:
    dialog = EditTaskDialog(
        {"id": 9, "name": "Write the report", "description": "old", "status": {"id": 1}},
        STATUSES,
    )
    dialog.desc_input.setPlainText(description)
    return dialog


# ── Add Task ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", CODE)
def test_add_task_refuses_code_and_stays_open(qapp, warnings, text):
    dialog = _add(text)
    dialog.accept()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert len(warnings) == 1
    assert warnings[0].startswith("Description:")
    assert "not allowed" in warnings[0]
    dialog.deleteLater()


@pytest.mark.parametrize("text", PROSE)
def test_add_task_accepts_and_returns_a_plain_description(qapp, warnings, text):
    dialog = _add(text)
    dialog.accept()

    assert warnings == []
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.get_data()["description"] == text
    assert dialog.get_data()["task_name"] == "Write the report"
    dialog.deleteLater()


def test_add_task_without_a_description_still_works(qapp, warnings):
    dialog = _add("")
    dialog.accept()

    assert warnings == []
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert not dialog.get_data()["description"]
    dialog.deleteLater()


def test_add_non_billable_task_is_held_to_the_same_rule(qapp, warnings):
    dialog = AddTaskDialog("Apollo", non_billable=True)
    dialog.name_input.setText("Internal tooling")
    dialog.desc_input.setPlainText("<script>x</script>")
    dialog.accept()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert len(warnings) == 1
    dialog.deleteLater()


# ── Edit Task ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", CODE)
def test_edit_task_refuses_code_and_stays_open(qapp, warnings, text):
    dialog = _edit(text)
    dialog.accept()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert len(warnings) == 1
    assert warnings[0].startswith("Description:")
    dialog.deleteLater()


@pytest.mark.parametrize("text", PROSE)
def test_edit_task_accepts_and_returns_a_plain_description(qapp, warnings, text):
    dialog = _edit(text)
    dialog.accept()

    assert warnings == []
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.get_data()["description"] == text
    dialog.deleteLater()


def test_edit_task_can_still_clear_the_description(qapp, warnings):
    dialog = _edit("")
    dialog.accept()

    assert warnings == []
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert not dialog.get_data()["description"]
    dialog.deleteLater()


def test_edit_task_with_a_valid_description_keeps_the_rest_of_the_edit(qapp, warnings):
    dialog = _edit("Cover Q3")
    dialog.status_combo.setCurrentIndex(1)
    dialog.accept()

    data = dialog.get_data()
    assert data["description"] == "Cover Q3"
    assert data["status_id"] == 2
    assert data["task_name"] == "Write the report"
    dialog.deleteLater()


# ── On screen ───────────────────────────────────────────────────────────────

def _row(description: str) -> TaskRow:
    row = TaskRow(
        task={"id": 1, "name": "Write the report", "description": description,
              "created_at": "2026-09-16T14:06:00Z"},
        project_id=1, project_name="P", project_color="#3B82F6",
        is_running=False, readonly=False,
        column_widths=dict(COLUMN_DEFAULT_WIDTHS),
    )
    return row


@pytest.mark.parametrize(
    "stored",
    ["<b>bold?</b> text", "<img src='http://evil.example/x.png'>", "<a href='x'>link</a>"],
)
def test_a_stored_description_with_markup_is_shown_as_literal_text(qapp, stored):
    """A row written before this rule existed must still read as what it says --
    a QLabel's default would render it as rich text."""
    row = _row(stored)
    label = row._desc_label

    assert label is not None
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert label.text() == stored
    row.deleteLater()


def test_an_ordinary_description_still_shows_and_is_shortened(qapp):
    row = _row("A perfectly ordinary description of the task that runs on for quite a while")
    assert row._desc_label is not None
    assert row._desc_label.text().endswith("…")
    assert len(row._desc_label.text()) == 61
    row.deleteLater()
