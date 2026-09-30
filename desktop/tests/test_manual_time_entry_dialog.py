"""
Regression coverage for the manual-time-entry dialog's task-loading bug:

ManualTimeEntryDialog.__init__ used to emit project_changed itself, before
the caller (TaskSection) had a chance to connect to it -- so the very first
emission, the one case that mattered when a project was already selected by
default, was silently lost. Changing the project afterward worked, because
that emission comes from currentIndexChanged, which fires after the caller
has already connected.

These tests exercise the actual TaskSection wiring pattern (construct the
dialog, connect, then fire the initial load) rather than the dialog alone,
since the bug was in the interaction between the two, not in either widget
by itself.
"""
from PySide6.QtCore import QTime
from PySide6.QtWidgets import QFormLayout

from ui.task_table import PLACEHOLDER_REASON, ManualTimeEntryDialog

PROJECTS = [
    {"id": 10, "project_name": "Website Redesign"},
    {"id": 20, "project_name": "Mobile App"},
]


def _open_and_wire(qapp, initial_project_id):
    """Mirrors TaskSection._on_manual_entry_clicked's own sequence."""
    dialog = ManualTimeEntryDialog(PROJECTS, initial_project_id)
    seen = []
    dialog.project_changed.connect(seen.append)
    if dialog.project_combo.currentData() is not None:
        seen.append(dialog.project_combo.currentData())
    return dialog, seen


def test_default_selected_project_triggers_a_task_load(qapp):
    """The exact bug: a project pre-selected via initial_project_id must
    still result in a task-load request once the caller is listening."""
    dialog, seen = _open_and_wire(qapp, initial_project_id=20)
    assert dialog.project_combo.currentData() == 20
    assert seen == [20]


def test_no_default_project_still_loads_tasks_for_the_first_item(qapp):
    """No initial_project_id -- the combo box still defaults to its first
    entry, and that must also trigger a task load."""
    dialog, seen = _open_and_wire(qapp, initial_project_id=None)
    assert dialog.project_combo.currentData() == 10
    assert seen == [10]


def test_changing_the_project_afterward_still_emits(qapp):
    """The already-working path must keep working: user-driven changes via
    the combo box fire project_changed after the dialog is open."""
    dialog, seen = _open_and_wire(qapp, initial_project_id=10)
    assert seen == [10]
    dialog.project_combo.setCurrentIndex(1)
    assert seen == [10, 20]


def test_dialog_init_alone_does_not_leave_project_changed_pending(qapp):
    """__init__ must not emit before anyone can be connected -- connecting
    right after construction should see zero emissions from construction
    itself (only from real subsequent interaction)."""
    dialog = ManualTimeEntryDialog(PROJECTS, initial_project_id=10)
    seen = []
    dialog.project_changed.connect(seen.append)
    assert seen == []


def _ready_dialog(qapp):
    """A dialog whose every other field already validates."""
    dialog = ManualTimeEntryDialog(PROJECTS, initial_project_id=10)
    dialog.set_tasks([{"id": 5, "task_name": "Design homepage"}])
    dialog.start_input.setTime(QTime(9, 0))
    dialog.end_input.setTime(QTime(10, 0))
    dialog.reason_combo.setCurrentIndex(dialog.reason_combo.findData("forgot_timer"))
    return dialog


def test_empty_description_blocks_submission(qapp):
    dialog = _ready_dialog(qapp)
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))

    dialog._on_save_clicked()

    assert accepted == []
    assert dialog.error_label.text() == "Description is required."
    assert not dialog.error_label.isHidden()


def test_whitespace_only_description_blocks_submission(qapp):
    dialog = _ready_dialog(qapp)
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))

    dialog.desc_input.setPlainText("   \n\t  \n ")
    dialog._on_save_clicked()

    assert accepted == []
    assert dialog.error_label.text() == "Description is required."


def test_valid_description_submits_and_is_trimmed(qapp):
    dialog = _ready_dialog(qapp)
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))

    dialog.desc_input.setPlainText("  Reviewed the client update  \n")
    dialog._on_save_clicked()

    assert accepted == [True]
    assert dialog.get_data()["description"] == "Reviewed the client update"


def test_earlier_field_validation_still_runs_first(qapp):
    """A missing description must not mask the existing time validation."""
    dialog = _ready_dialog(qapp)
    dialog.end_input.setTime(QTime(8, 0))

    dialog._on_save_clicked()

    assert dialog.error_label.text() == "End time cannot be before start time."


def test_description_is_marked_required_in_the_form(qapp):
    dialog = ManualTimeEntryDialog(PROJECTS, initial_project_id=10)
    outer = dialog.layout()
    form = next(
        outer.itemAt(i).layout()
        for i in range(outer.count())
        if isinstance(outer.itemAt(i).layout(), QFormLayout)
    )
    label = form.labelForField(dialog.desc_input)
    assert label.text() == "Description *"
    assert "optional" not in dialog.desc_input.placeholderText().lower()


def test_duration_updates_and_rejects_end_before_start(qapp):
    dialog = ManualTimeEntryDialog(PROJECTS, initial_project_id=10)
    dialog.start_input.setTime(QTime(9, 0))
    dialog.end_input.setTime(QTime(10, 30))
    assert dialog.duration_label.text() == "Duration: 1h 30m"

    dialog.end_input.setTime(QTime(8, 0))
    assert dialog.duration_label.text() == "Duration: —"


# ── A reason, in place of the Billable box ───────────────────────────────────
#
# The dialog used to show a "Billable" box for fixed-hours projects. It asked
# the requester something the project already answers, so it is gone: the
# owner asked for a Reason drop-down in its place (2026-09-30). Billable is no
# longer sent at all, and the backend takes it from the project -- billable on
# a fixed-hours project, not on a flexible one.

BILLING_PROJECTS = [
    {"id": 1, "project_name": "Client Retainer", "billing_type": "fixed", "fixed_hours": "120.00"},
    {"id": 2, "project_name": "Beta Launch", "billing_type": "free", "fixed_hours": None},
    {"id": 3, "project_name": "Legacy", "fixed_hours": None},  # no billing_type at all
]

REASONS = [
    ("forgot_timer", "Forgot to start/stop timer"),
    ("wrong_task_project", "Used wrong task/project"),
    ("other", "Other"),
]


def _form_of(dialog) -> QFormLayout:
    outer = dialog.layout()
    return next(
        outer.itemAt(i).layout()
        for i in range(outer.count())
        if isinstance(outer.itemAt(i).layout(), QFormLayout)
    )


def _row_labels(dialog) -> list:
    form = _form_of(dialog)
    labels = []
    for row in range(form.rowCount()):
        item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        labels.append(item.widget().text() if item is not None and item.widget() is not None else "")
    return labels


def test_the_dialog_offers_exactly_the_three_reasons(qapp):
    dialog = ManualTimeEntryDialog(PROJECTS, initial_project_id=10)
    combo = dialog.reason_combo

    offered = [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]

    assert offered == [(None, PLACEHOLDER_REASON)] + REASONS


def test_nothing_is_chosen_for_the_user(qapp):
    """A reason is always a choice somebody made, never a default."""
    dialog = ManualTimeEntryDialog(PROJECTS, initial_project_id=10)

    assert dialog.reason_combo.currentData() is None
    assert dialog.reason_combo.currentText() == PLACEHOLDER_REASON


def test_the_reason_row_stands_where_billable_was(qapp):
    """Between the duration and the description, marked required."""
    dialog = ManualTimeEntryDialog(PROJECTS, initial_project_id=10)
    form = _form_of(dialog)

    assert form.labelForField(dialog.reason_combo).text() == "Reason *"
    labels = _row_labels(dialog)
    assert labels.index("Reason *") == labels.index("Description *") - 1
    assert labels.index("Reason *") > labels.index("End Time *")


def test_there_is_no_billable_box_for_any_kind_of_project(qapp):
    """Not for a fixed-hours project, a flexible one, or one with no type."""
    from PySide6.QtWidgets import QCheckBox

    for project_id in (1, 2, 3):
        dialog = ManualTimeEntryDialog(BILLING_PROJECTS, initial_project_id=project_id)

        assert dialog.findChildren(QCheckBox) == []
        assert not hasattr(dialog, "billable_check")
        assert "Billable" not in " ".join(_row_labels(dialog))
        # The Reason row is there whichever project is selected.
        assert _form_of(dialog).isRowVisible(dialog.reason_combo)


def test_a_missing_reason_blocks_submission(qapp):
    dialog = _ready_dialog(qapp)
    dialog.reason_combo.setCurrentIndex(0)
    dialog.desc_input.setPlainText("Reviewed the client update")
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))

    dialog._on_save_clicked()

    assert accepted == []
    assert dialog.error_label.text() == "Select a reason."
    assert not dialog.error_label.isHidden()


def test_each_reason_is_submitted_as_its_value(qapp):
    for value, label in REASONS:
        dialog = _ready_dialog(qapp)
        dialog.desc_input.setPlainText("Reviewed the client update")
        dialog.reason_combo.setCurrentIndex(dialog.reason_combo.findText(label))
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(True))

        dialog._on_save_clicked()

        assert accepted == [True], label
        assert dialog.get_data()["reason"] == value


def test_billable_is_not_sent_for_any_kind_of_project(qapp):
    """The backend decides it from the project. Sending a value from here --
    even the right one -- is a second opinion that can only ever disagree."""
    for project_id in (1, 2, 3):
        dialog = ManualTimeEntryDialog(BILLING_PROJECTS, initial_project_id=project_id)

        assert "is_billable" not in dialog.get_data()


def test_changing_the_project_keeps_the_chosen_reason(qapp):
    """The reason is about the request, not about the project."""
    dialog = ManualTimeEntryDialog(BILLING_PROJECTS, initial_project_id=1)
    dialog.reason_combo.setCurrentIndex(dialog.reason_combo.findData("wrong_task_project"))

    dialog.project_combo.setCurrentIndex(dialog.project_combo.findData(2))

    assert dialog.reason_combo.currentData() == "wrong_task_project"


def test_the_form_data_is_exactly_what_the_service_accepts(qapp):
    """`TaskSection` passes the dialog's data straight to the service as
    keyword arguments; a key the service does not take is a TypeError at the
    moment the user presses Save."""
    import inspect

    from app.time_entries.service import TimeEntryService

    dialog = _ready_dialog(qapp)
    dialog.desc_input.setPlainText("Reviewed the client update")
    accepted = set(inspect.signature(TimeEntryService.create_manual_time_entry).parameters) - {"self"}

    assert set(dialog.get_data()) <= accepted


def test_the_service_sends_the_reason_and_leaves_billable_to_the_backend():
    from unittest.mock import MagicMock

    from app.time_entries.service import TimeEntryService

    client = MagicMock()
    client.post.return_value.json.return_value = {"id": 1}
    service = TimeEntryService(client)

    service.create_manual_time_entry(
        project_id=1, task_id=2, work_date="2026-09-30", total_seconds=3600,
        start_time="2026-09-30T03:30:00+00:00", end_time="2026-09-30T04:30:00+00:00",
        description="Reviewed the client update", reason="forgot_timer",
    )

    path = client.post.call_args.args[0]
    body = client.post.call_args.kwargs["json_data"]
    assert path == "/manual-time-entries"
    assert body["reason"] == "forgot_timer"
    assert "is_billable" not in body


def test_the_reasons_are_the_backends_own_set():
    """Kept in step mechanically, the way the validation limits are: a value
    offered here that the backend's enum does not have is a request refused
    with 422, and one it has that is missing here can never be chosen."""
    import ast
    from pathlib import Path

    from app.time_entries.service import MANUAL_ENTRY_REASONS

    schema = (
        Path(__file__).resolve().parents[2] / "backend" / "app" / "schemas" / "manual_time_entry.py"
    )
    tree = ast.parse(schema.read_text(encoding="utf-8"))
    enum = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ManualEntryReason"
    )
    backend_values = [
        node.value.value for node in enum.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
    ]
    labels = next(
        node for node in tree.body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "MANUAL_ENTRY_REASON_LABELS"
    )
    backend_labels = [value.value for value in labels.value.values]

    assert [value for value, _ in MANUAL_ENTRY_REASONS] == backend_values
    assert [label for _, label in MANUAL_ENTRY_REASONS] == backend_labels
    assert MANUAL_ENTRY_REASONS == tuple(REASONS)
