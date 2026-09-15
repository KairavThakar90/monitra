"""
Task table — My Tasks section.

Displays tasks for the selected project with Start/Stop controls.
Supports Add/Edit/Duplicate/Delete and single-active-timer switching.

Ownership: this module is presentation only. It owns no threads, performs no
HTTP calls directly and holds no authoritative timer state. User actions are
expressed as intent through `BackgroundApi`; the TimerService and SyncService
own the state and its durability and publish results back through signals.

Optimistic UI still applies, but it is implemented in the service layer rather
than here: the timer commits locally the instant the user acts and reconciles
with the backend afterwards, so the UI is immediate without the widget having
to guess at, or duplicate, the authoritative state.
"""
import uuid
from datetime import date, datetime, timezone
from typing import Callable, Optional, List, Dict, Any, Tuple

from PySide6.QtCore import Qt, Signal, QByteArray, QDate, QTime
from PySide6.QtGui import QFont, QColor, QPainter
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QFrame, QScrollArea, QToolButton,
    QMenu, QMessageBox, QDialog, QTextEdit, QFormLayout,
    QGraphicsDropShadowEffect, QComboBox, QCheckBox, QDateEdit, QTimeEdit,
    QAbstractSpinBox,
)

from app.tasks.service import TaskService
from background_services.public_api import NotificationLevel
from core.validation import (
    DESCRIPTION_MAX_LENGTH,
    NAME_MAX_LENGTH,
    validate_description,
    validate_identifier,
    validate_name,
)
from ui import icons
from ui.styles import (
    PRIMARY, PRIMARY_HOVER, PRIMARY_LIGHT, SUCCESS_BG,
    ERROR, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED,
    BORDER_LIGHT, CARD_BG, CONTENT_BG, TASK_TABLE_QSS,
    MONITRA_MARK_SVG, BORDER_MID, BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER,
    BUTTON_GRADIENT_REVERSED, BUTTON_GRADIENT_REVERSED_HOVER,
    ACTIVE_ROW_BORDER,
)
from core.date_mode import as_calendar_day, is_live_date
from core.logging_setup import get_logger
from core.time_format import format_hms, ist_today


log = get_logger("ui.tasks")

#: The one authoritative duration formatter (core.time_format.format_hms).
#: Widgets must not keep private copies of duration formatting.
_fmt_seconds = format_hms


def _fmt_hours(h: Optional[float]) -> str:
    if h is None:
        return "--"
    total_min = int(h * 60)
    hh = total_min // 60
    mm = total_min % 60
    return f"{hh:02d}:{mm:02d}"


def _pct(tracked: int, budget_hours: Optional[float]) -> Optional[int]:
    if not budget_hours or budget_hours <= 0:
        return None
    pct = int((tracked / (budget_hours * 3600)) * 100)
    return min(pct, 100)


#: The backend's own name for a finished task. Task statuses come from the
#: server's task_statuses table ("todo", "in_progress", "completed" -- see
#: backend TASK_STATUS_NAMES), and the backend itself matches on the lowered
#: name (TeamsService._status_maps). This is that same value, not a new one.
COMPLETED_TASK_STATUS = "completed"


def _task_status_name(task: Dict[str, Any]) -> str:
    """Lowered status name of a task.

    Depending on which endpoint served the task, `status` is either the plain
    name string (TaskRead) or a {"id", "name", "color"} object. Both are read
    here so callers never have to care which one they got.
    """
    status = task.get("status")
    if isinstance(status, dict):
        status = status.get("name")
    return (status or "").strip().lower()


def is_task_completed(task: Dict[str, Any]) -> bool:
    """True when the server says this task is completed.

    Presentation only: the desktop hides completed tasks from its own list
    and never writes, clears or infers this status.
    """
    return _task_status_name(task) == COMPLETED_TASK_STATUS


def _fmt_created(created_at: Optional[str]) -> str:
    if not created_at:
        return "--"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return local_dt.strftime("%d-%m-%Y %H:%M")
    except Exception:
        return str(created_at)[:16]


# ─── Task table column model ───────────────────────────────────────────────────
#
# The task list is a column-header row lined up against each TaskRow's own
# independently-built row layout -- not a real QTableWidget/QHeaderView, so
# there is no native column-resize to turn on. This shared width model plus
# ColumnResizeHandle below is what makes dragging actually move both the
# header and every visible row's matching column in sync.

COLUMN_ORDER = ["task", "created", "tracked", "action"]
COLUMN_LABELS = {"task": "TASK", "created": "CREATE ON", "tracked": "HOURS", "action": "ACTION"}
# CREATE ON and ACTION carry a leading glyph and a kebab menu respectively,
# so their floors and defaults are wide enough to render both without
# clipping the date text or pushing the menu button off the row.
COLUMN_MIN_WIDTHS = {"task": 160, "created": 140, "tracked": 110, "action": 160}
COLUMN_DEFAULT_WIDTHS = {"task": 280, "created": 175, "tracked": 130, "action": 165}
#: The only column without a fixed pixel width -- it stretches to absorb
#: whatever space the other three don't use, so a wide window doesn't leave
#: a dead gap after ACTION. self._column_widths["task"] is still tracked and
#: still adjustable via its drag handle, just applied as a *minimum* width
#: instead of a fixed one (see _apply_column_extent below).
STRETCH_COLUMN = "task"
#: Width of the draggable divider between two header columns. TaskRow adds a
#: same-width, non-interactive spacer at the same positions so its columns
#: never drift out of alignment with the header's.
COLUMN_HANDLE_WIDTH = 6


def _apply_column_extent(widget: QWidget, key: str, width: int) -> None:
    """Set a column widget's width: fixed for every column except
    STRETCH_COLUMN, which only gets a floor and otherwise fills leftover
    layout space via its stretch factor."""
    if key == STRETCH_COLUMN:
        widget.setMinimumWidth(width)
    else:
        widget.setFixedWidth(width)


class ColumnResizeHandle(QFrame):
    """A thin drag handle between two header columns.

    Emits `dragged(delta_px)` on every mouse-move while pressed; the owner
    (TaskSection) decides how to split that delta between the two columns
    on either side and re-applies the resulting widths everywhere. This
    widget holds no width state of its own.
    """
    dragged = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(COLUMN_HANDLE_WIDTH)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._dragging = False
        self._last_x = 0.0
        self._set_idle_style()

    def _set_idle_style(self) -> None:
        self.setStyleSheet("background: transparent;")

    def enterEvent(self, event) -> None:
        self.setStyleSheet(f"background: {PRIMARY};")
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._dragging:
            self._set_idle_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._last_x = event.globalPosition().x()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            x = event.globalPosition().x()
            delta = x - self._last_x
            self._last_x = x
            if delta:
                self.dragged.emit(int(delta))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._dragging = False
        self._set_idle_style()
        super().mouseReleaseEvent(event)


# ─── Dialogs ─────────────────────────────────────────────────────────────────

class AddTaskDialog(QDialog):
    """Add Task: a name and an optional description, nothing else.

    There is deliberately no assignee field. An employee's task is assigned
    to them by the caller, and a task created by anyone else starts
    unassigned and is given an owner later through Edit Task, which already
    owns assignment. Putting the choice here as well was a second way to do
    one job.
    """

    def __init__(self, project_name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Task")
        self.setModal(True)
        self.setFixedSize(400, 260)
        self._build_ui(project_name)
        self._apply_style()

    def _build_ui(self, project_name: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        # Compact project display (read-only styled like a dropdown)
        proj_field = QLineEdit(project_name, self)
        proj_field.setReadOnly(True)
        proj_field.setFixedHeight(34)
        proj_field.setObjectName("ProjectReadOnly")
        form.addRow("Project", proj_field)

        self.name_input = QLineEdit(self)
        self.name_input.setPlaceholderText("Enter task name")
        # The shared limit, applied at the widget so the field simply stops
        # accepting text rather than failing on submit.
        self.name_input.setMaxLength(NAME_MAX_LENGTH)
        self.name_input.setFixedHeight(34)
        form.addRow("Task Name *", self.name_input)

        self.desc_input = QTextEdit(self)
        self.desc_input.setPlaceholderText("Enter task description (optional)")
        self.desc_input.setFixedHeight(68)
        form.addRow("Description", self.desc_input)

        layout.addLayout(form)
        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()

        self.cancel_btn = QPushButton("Cancel", self)
        self.cancel_btn.setFixedSize(80, 32)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("Save", self)
        self.save_btn.setObjectName("SaveBtn")
        self.save_btn.setFixedSize(80, 32)
        self.save_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.save_btn)

        layout.addLayout(btn_layout)

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QDialog {{
                background-color: #FFFFFF;
            }}
            QLabel {{
                font-size: 12px;
                font-weight: 600;
                color: #334155;
                background: transparent;
            }}
            QLineEdit, QTextEdit {{
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 4px 10px;
                background-color: #FFFFFF;
                font-size: 13px;
                color: #0F172A;
            }}
            QLineEdit#ProjectReadOnly {{
                background-color: #F8FAFC;
                color: #475569;
            }}
            QLineEdit:focus, QTextEdit:focus {{
                border-color: {PRIMARY};
            }}
            QPushButton {{
                border-radius: 6px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton#SaveBtn {{
                background: {BUTTON_GRADIENT};
                color: #FFFFFF;
                border: none;
            }}
            QPushButton#SaveBtn:hover {{
                background: {BUTTON_GRADIENT_HOVER};
            }}
            QPushButton[text="Cancel"] {{
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                color: #64748B;
            }}
            QPushButton[text="Cancel"]:hover {{
                background-color: #F8FAFC;
            }}
        """)

    def accept(self) -> None:
        """Validate before closing, so a mistake can be fixed in place.

        The caller used to check the name only after the dialog had already
        closed, which meant a typo threw away everything else the person had
        typed and made them reopen the form.
        """
        name = validate_name(self.name_input.text(), field_label="Task name")
        if not name.ok:
            QMessageBox.warning(self, "Validation Error", name.error)
            self.name_input.setFocus()
            return
        description = validate_description(
            self.desc_input.toPlainText(), field_label="Description"
        )
        if not description.ok:
            QMessageBox.warning(self, "Validation Error", description.error)
            self.desc_input.setFocus()
            return
        super().accept()

    def get_data(self) -> dict:
        # The normalised values, not the raw widget text: that is what carries
        # the trimming and line-ending conversion the backend expects.
        name = validate_name(self.name_input.text(), field_label="Task name")
        description = validate_description(
            self.desc_input.toPlainText(), field_label="Description"
        )
        return {
            "task_name": name.value if name.ok else "",
            "description": description.value if description.ok else "",
            "estimated_hours": None
        }


class EditTaskDialog(QDialog):
    def __init__(self, task: dict, statuses: list, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Task")
        self.setModal(True)
        self.setFixedSize(380, 310)
        self.estimated_hours = task.get("estimated_hours")
        self._statuses = statuses
        self._build_ui(task)
        self._apply_style()

    def _build_ui(self, task: dict) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.name_input = QLineEdit(self)
        task_name = task.get("name") or task.get("task_name") or ""
        self.name_input.setText(task_name)
        self.name_input.setPlaceholderText("Enter task name")
        self.name_input.setMaxLength(NAME_MAX_LENGTH)
        self.name_input.setFixedHeight(30)
        form.addRow("Task Name*:", self.name_input)

        self.desc_input = QTextEdit(self)
        orig_desc = task.get("description") or ""
        clean_desc = orig_desc.replace("[duplicate]", "").strip()
        self.desc_input.setPlainText(clean_desc)
        self.desc_input.setPlaceholderText("Enter task description (optional)")
        self.desc_input.setFixedHeight(70)
        form.addRow("Description:", self.desc_input)

        # Status dropdown
        self.status_combo = QComboBox(self)
        self.status_combo.setFixedHeight(30)
        
        current_status_id = None
        task_status = task.get("status")
        if isinstance(task_status, dict):
            current_status_id = task_status.get("id")
            
        selected_index = 0
        for idx, status in enumerate(self._statuses):
            self.status_combo.addItem(status.get("name"), status.get("id"))
            if current_status_id is not None and status.get("id") == current_status_id:
                selected_index = idx
        self.status_combo.setCurrentIndex(selected_index)
        form.addRow("Status:", self.status_combo)

        layout.addLayout(form)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()

        self.cancel_btn = QPushButton("Cancel", self)
        self.cancel_btn.setFixedSize(80, 30)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("Save", self)
        self.save_btn.setFixedSize(80, 30)
        self.save_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.save_btn)

        layout.addLayout(btn_layout)

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QDialog {{
                background-color: #FFFFFF;
            }}
            QLabel {{
                font-size: 12px;
                color: #334155;
                background: transparent;
            }}
            QLineEdit, QTextEdit {{
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 4px 8px;
                background-color: #FFFFFF;
                font-size: 12px;
            }}
            QLineEdit:focus, QTextEdit:focus {{
                border-color: {PRIMARY};
            }}
            QPushButton {{
                border-radius: 6px;
                font-size: 12px;
                font-weight: 600;
            }}
            QPushButton[text="Save"] {{
                background: {BUTTON_GRADIENT};
                color: #FFFFFF;
                border: none;
            }}
            QPushButton[text="Save"]:hover {{
                background: {BUTTON_GRADIENT_HOVER};
            }}
            QPushButton[text="Cancel"] {{
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                color: #64748B;
            }}
        """)

    def accept(self) -> None:
        """Validate before closing — same reasoning as ``AddTaskDialog``."""
        name = validate_name(self.name_input.text(), field_label="Task name")
        if not name.ok:
            QMessageBox.warning(self, "Validation Error", name.error)
            self.name_input.setFocus()
            return
        description = validate_description(
            self.desc_input.toPlainText(), field_label="Description"
        )
        if not description.ok:
            QMessageBox.warning(self, "Validation Error", description.error)
            self.desc_input.setFocus()
            return
        super().accept()

    def get_data(self) -> dict:
        status_id = self.status_combo.currentData()
        name = validate_name(self.name_input.text(), field_label="Task name")
        description = validate_description(
            self.desc_input.toPlainText(), field_label="Description"
        )
        return {
            "task_name": name.value if name.ok else "",
            "description": description.value if description.ok else "",
            "status_id": status_id,
            "estimated_hours": self.estimated_hours
        }


class DeleteConfirmDialog(QDialog):
    def __init__(self, task_name: str, is_admin: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Delete Task")
        # Frameless dialog — no native black border/frame
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedSize(440, 280)
        self._build_ui(task_name, is_admin)
        self._apply_style()

    def _build_ui(self, task_name: str, is_admin: bool) -> None:
        # Transparent outer layout to support drop shadow
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(12, 12, 12, 12)

        # White main dialog card container
        self.card = QFrame(self)
        self.card.setObjectName("DialogCard")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(28, 28, 28, 24)
        card_layout.setSpacing(14)

        # Drop shadow — soft, no black frame
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setColor(QColor(0, 0, 0, 45))
        shadow.setOffset(0, 6)
        self.card.setGraphicsEffect(shadow)

        # ── Branding row (logo + wordmark) ────────────────────────
        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        brand_row.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.logo_mark = QSvgWidget(self.card)
        self.logo_mark.load(QByteArray(MONITRA_MARK_SVG.encode()))
        self.logo_mark.setFixedSize(44, 44)
        brand_row.addWidget(self.logo_mark)

        wordmark = QLabel("Monitra", self.card)
        wordmark.setObjectName("BrandWordmark")
        wordmark.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        wordmark.setStyleSheet(f"color: {TEXT_PRIMARY}; letter-spacing: -0.5px;")
        brand_row.addWidget(wordmark)
        brand_row.addStretch()

        card_layout.addLayout(brand_row)

        # Thin divider
        divider = QFrame(self.card)
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {BORDER_LIGHT}; border: none;")
        card_layout.addWidget(divider)

        # ── Title + body ──────────────────────────────────────────
        title_label = QLabel("Delete Task?", self.card)
        title_label.setObjectName("DialogTitle")
        title_label.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        title_label.setStyleSheet(f"color: {TEXT_PRIMARY};")
        card_layout.addWidget(title_label)

        if is_admin:
            body_text = (
                f"Are you sure you want to delete <b>{task_name}</b>?<br><br>"
                "Any tracked hours or task-related records associated with this task may be affected. "
                "This action cannot be undone."
            )
        else:
            body_text = f"Are you sure you want to delete <b>{task_name}</b>?"

        body_label = QLabel(body_text, self.card)
        body_label.setObjectName("DialogBody")
        body_label.setFont(QFont("Segoe UI", 11))
        body_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        body_label.setWordWrap(True)
        card_layout.addWidget(body_label)

        # ── Buttons ───────────────────────────────────────────────
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(8)
        buttons_layout.addStretch()

        self.cancel_btn = QPushButton("Cancel", self.card)
        self.cancel_btn.setObjectName("SecondaryBtn")
        self.cancel_btn.setFixedSize(80, 34)
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.reject)
        buttons_layout.addWidget(self.cancel_btn)

        delete_btn_text = "Delete Task" if is_admin else "Delete"
        self.delete_btn = QPushButton(delete_btn_text, self.card)
        self.delete_btn.setObjectName("DestructiveBtn")
        self.delete_btn.setFixedSize(90, 34)
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.clicked.connect(self.accept)
        buttons_layout.addWidget(self.delete_btn)

        card_layout.addLayout(buttons_layout)
        outer_layout.addWidget(self.card)

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QFrame#DialogCard {{
                background-color: #FFFFFF;
                border: none;
                border-radius: 14px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            QPushButton {{
                border-radius: 7px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton#SecondaryBtn {{
                background-color: #FFFFFF;
                border: 1.5px solid {BORDER_LIGHT};
                color: {TEXT_SECONDARY};
            }}
            QPushButton#SecondaryBtn:hover {{
                background-color: #F8FAFC;
                border-color: {BORDER_MID};
                color: {TEXT_PRIMARY};
            }}
            QPushButton#SecondaryBtn:pressed {{
                background-color: #F1F5F9;
            }}
            QPushButton#DestructiveBtn {{
                background-color: {ERROR};
                border: none;
                color: #FFFFFF;
            }}
            QPushButton#DestructiveBtn:hover {{
                background-color: #DC2626;
            }}
            QPushButton#DestructiveBtn:pressed {{
                background-color: #B91C1C;
            }}
        """)


class ManualTimeEntryDialog(QDialog):
    """
    Log time for a project/task after the fact, distinct from the live
    Start/Stop timer.

    Presentation only, matching every other dialog in this file: it owns no
    threads and makes no API calls. Project selection is local (the caller
    already has the full project list); task selection is cascading and
    async, so the owner listens for `project_changed` and calls
    `set_tasks_loading()` / `set_tasks()` once the fetch completes.
    """
    project_changed = Signal(int)

    def __init__(
        self,
        projects: List[Dict[str, Any]],
        initial_project_id: Optional[int] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Manual Time Entry")
        self.setModal(True)
        self.setFixedSize(420, 480)
        self._projects = projects
        self._build_ui()
        self._apply_style()

        if initial_project_id is not None:
            idx = self.project_combo.findData(initial_project_id)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
        # No project_changed emit here: __init__ runs before the caller has
        # had a chance to connect to this signal, so an emit at this point
        # is silently lost -- exactly why the task dropdown never populated
        # when a project was already selected by default. The caller kicks
        # off the initial task load itself, after connecting, once this
        # dialog is constructed.

    def _build_time_field(self, initial_time: QTime) -> "tuple[QWidget, QTimeEdit]":
        """
        A clock-icon-prefixed time field with no visible spin-box arrows --
        the up/down buttons on a plain QTimeEdit were the "clunky" control
        being replaced. Segments (hour/minute/AM-PM) are still editable by
        clicking a segment and typing or scrolling the mouse wheel over it;
        only the built-in increment/decrement buttons are hidden.

        Returns (wrapper_widget_for_the_form, the_actual_QTimeEdit) -- the
        caller keeps using the QTimeEdit directly for .time()/.timeChanged/
        get_data(), only how it's added to the form layout changes.
        """
        wrapper = QFrame(self)
        wrapper.setObjectName("TimeFieldWrapper")
        wrapper.setFixedHeight(34)
        field_layout = QHBoxLayout(wrapper)
        field_layout.setContentsMargins(10, 0, 8, 0)
        field_layout.setSpacing(6)

        icon_label = QLabel(wrapper)
        icon_label.setPixmap(icons.pixmap("timer", TEXT_MUTED, 15))
        field_layout.addWidget(icon_label)

        time_edit = QTimeEdit(initial_time, wrapper)
        time_edit.setObjectName("TimeEditInner")
        time_edit.setDisplayFormat("hh:mm AP")
        time_edit.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        time_edit.setFrame(False)
        field_layout.addWidget(time_edit, 1)

        return wrapper, time_edit

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.project_combo = QComboBox(self)
        self.project_combo.setFixedHeight(34)
        for project in self._projects:
            self.project_combo.addItem(
                project.get("project_name") or project.get("name") or "Unnamed", project.get("id")
            )
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        form.addRow("Project *", self.project_combo)

        self.task_combo = QComboBox(self)
        self.task_combo.setFixedHeight(34)
        self.task_combo.setEnabled(False)
        form.addRow("Task *", self.task_combo)

        today = QDate.currentDate()
        self.date_input = QDateEdit(today, self)
        self.date_input.setCalendarPopup(True)
        self.date_input.setMaximumDate(today)
        self.date_input.setDisplayFormat("MMM d, yyyy")
        self.date_input.setFixedHeight(34)
        form.addRow("Work Date *", self.date_input)

        now = QTime.currentTime()
        start_field, self.start_input = self._build_time_field(QTime(max(0, now.hour() - 1), now.minute()))
        self.start_input.timeChanged.connect(self._update_duration)
        form.addRow("Start Time *", start_field)

        end_field, self.end_input = self._build_time_field(now)
        self.end_input.timeChanged.connect(self._update_duration)
        form.addRow("End Time *", end_field)

        self.duration_label = QLabel("Duration: 1h 0m", self)
        self.duration_label.setObjectName("DurationLabel")
        form.addRow("", self.duration_label)

        self.billable_check = QCheckBox("Billable", self)
        self.billable_check.setChecked(True)
        form.addRow("", self.billable_check)

        self.desc_input = QTextEdit(self)
        self.desc_input.setPlaceholderText("What did you work on?")
        self.desc_input.setFixedHeight(64)
        form.addRow("Description *", self.desc_input)

        layout.addLayout(form)

        self.error_label = QLabel("", self)
        self.error_label.setObjectName("ErrorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        btn_layout.addStretch()

        self.cancel_btn = QPushButton("Cancel", self)
        self.cancel_btn.setFixedSize(80, 32)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("Save Entry", self)
        self.save_btn.setObjectName("SaveBtn")
        self.save_btn.setFixedSize(110, 32)
        self.save_btn.clicked.connect(self._on_save_clicked)
        btn_layout.addWidget(self.save_btn)

        layout.addLayout(btn_layout)
        self._update_duration()

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QDialog {{
                background-color: #FFFFFF;
            }}
            QLabel {{
                font-size: 12px;
                font-weight: 600;
                color: #334155;
                background: transparent;
            }}
            QLabel#DurationLabel {{
                color: {PRIMARY};
                font-weight: 700;
            }}
            QLabel#ErrorLabel {{
                color: {ERROR};
                font-weight: 600;
                font-size: 12px;
            }}
            QLineEdit, QTextEdit, QComboBox, QDateEdit {{
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 4px 10px;
                background-color: #FFFFFF;
                font-size: 13px;
                color: #0F172A;
            }}
            QComboBox:disabled, QLineEdit:disabled {{
                background-color: #F8FAFC;
                color: #94A3B8;
            }}
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QDateEdit:focus {{
                border-color: {PRIMARY};
            }}
            QFrame#TimeFieldWrapper {{
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                background-color: #FFFFFF;
            }}
            QTimeEdit#TimeEditInner {{
                border: none;
                background: transparent;
                font-size: 13px;
                color: #0F172A;
            }}
            QPushButton {{
                border-radius: 6px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton#SaveBtn {{
                background: {BUTTON_GRADIENT};
                color: #FFFFFF;
                border: none;
            }}
            QPushButton#SaveBtn:hover {{
                background: {BUTTON_GRADIENT_HOVER};
            }}
            QPushButton#SaveBtn:disabled {{
                background: #93C5FD;
            }}
            QPushButton[text="Cancel"] {{
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                color: #64748B;
            }}
            QPushButton[text="Cancel"]:hover {{
                background-color: #F8FAFC;
            }}
        """)

    def _on_project_changed(self, _index: int) -> None:
        self.task_combo.clear()
        self.task_combo.setEnabled(False)
        project_id = self.project_combo.currentData()
        if project_id is not None:
            self.project_changed.emit(project_id)

    def set_tasks_loading(self) -> None:
        self.task_combo.clear()
        self.task_combo.addItem("Loading tasks…", None)
        self.task_combo.setEnabled(False)

    def set_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        self.task_combo.clear()
        for task in tasks:
            name = task.get("name") or task.get("task_name") or "Unnamed"
            self.task_combo.addItem(name, task.get("id"))
        self.task_combo.setEnabled(bool(tasks))
        if not tasks:
            self.task_combo.addItem("No tasks in this project", None)

    def _update_duration(self) -> None:
        start = self.start_input.time()
        end = self.end_input.time()
        seconds = start.secsTo(end)
        if seconds < 0:
            self.duration_label.setText("Duration: —")
            self.duration_label.setStyleSheet(f"color: {ERROR}; font-weight: 700;")
        else:
            hours, rem = divmod(seconds, 3600)
            minutes = rem // 60
            self.duration_label.setText(f"Duration: {hours}h {minutes}m")
            self.duration_label.setStyleSheet(f"color: {PRIMARY}; font-weight: 700;")

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _on_save_clicked(self) -> None:
        self.error_label.hide()

        project = validate_identifier(
            self.project_combo.currentData(), field_label="Project"
        )
        if not project.ok:
            self._show_error("Select a project.")
            return

        task = validate_identifier(self.task_combo.currentData(), field_label="Task")
        if not task.ok:
            self._show_error("Select a task.")
            return

        if self.start_input.time().secsTo(self.end_input.time()) < 0:
            self._show_error("End time cannot be before start time.")
            return

        # Description is required for a manual entry: unlike a tracked
        # session there is no activity record behind it, so the note is the
        # only account of what the time was spent on. Whitespace-only text
        # is no description at all. The shared rule also bounds the length and
        # refuses markup, matching what the backend will accept -- catching it
        # here means the entry never reaches the sync queue only to be
        # rejected on upload.
        description = validate_description(
            self.desc_input.toPlainText(), field_label="Description", required=True
        )
        if not description.ok:
            self._show_error(description.error)
            self.desc_input.setFocus()
            return

        self.accept()

    def description(self) -> str:
        """The typed description, normalised. Empty means "not provided"."""
        result = validate_description(self.desc_input.toPlainText())
        return result.value if result.ok and result.value else ""

    def get_data(self) -> Dict[str, Any]:
        """
        Validated form data, with start/end converted from the local
        wall-clock selection to real UTC-aware timestamps (the naive
        QDateEdit/QTimeEdit values represent this machine's local time, not
        UTC, so they're localized before being sent to the backend).
        """
        work_date = self.date_input.date().toPython()
        start_local = datetime.combine(work_date, self.start_input.time().toPython()).astimezone()
        end_local = datetime.combine(work_date, self.end_input.time().toPython()).astimezone()
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)
        return {
            "project_id": self.project_combo.currentData(),
            "task_id": self.task_combo.currentData(),
            "work_date": work_date.isoformat(),
            "start_time": start_utc.isoformat(),
            "end_time": end_utc.isoformat(),
            "total_seconds": int((end_utc - start_utc).total_seconds()),
            "description": self.desc_input.toPlainText().strip() or None,
            "is_billable": self.billable_check.isChecked(),
        }


# ─── Task Row ─────────────────────────────────────────────────────────────────

class TaskRow(QFrame):
    """
    A single task row widget.

    Presentation only. The row owns no threads, performs no API calls and
    holds no authoritative timer state: it renders whatever the TimerService
    reports and emits intent upwards. Previously each row created its own
    Start/Stop QThread workers and maintained its own `_local_tick` counter,
    which is how the displayed time could disagree with the tracked time.

    Emits: start_requested(row), stop_requested(row), edit_requested(row),
    duplicate_requested(row), delete_requested(row)
    """
    start_requested = Signal(object)
    stop_requested = Signal(object)
    edit_requested = Signal(object)
    duplicate_requested = Signal(object)
    delete_requested = Signal(object)

    timer_started = Signal(int, int)   # task_id, entry_id
    timer_stopped = Signal(int)        # task_id
    error_occurred = Signal(str)
    active_timer_conflict = Signal()

    def __init__(
        self,
        task: Dict[str, Any],
        project_id: int,
        project_name: str,
        project_color: str,
        is_running: bool = False,
        readonly: bool = False,
        column_widths: Optional[Dict[str, int]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.task = task
        self.project_id = project_id
        self.project_name = project_name
        self.project_color = project_color
        self._is_running = is_running
        self._entry_id: Optional[int] = None
        #: Time already banked against this task today, from the backend/cache.
        self._elapsed_seconds = task.get("time_tracked_seconds", 0)
        #: Seconds elapsed in the *current* session, supplied by the
        #: TimerService. The row never increments this itself.
        self._session_elapsed = 0
        #: True whenever the viewed day is not today: Start/Stop is hidden for
        #: every task, since only the live day may be tracked against. Set at
        #: construction and kept current afterwards via set_readonly().
        self._readonly = readonly
        #: Column pixel widths shared with the header (see COLUMN_* at the
        #: top of this file) -- kept current afterwards via
        #: set_column_widths() so a drag mid-session doesn't require
        #: rebuilding every row.
        self._column_widths = dict(column_widths or COLUMN_DEFAULT_WIDTHS)

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("TaskRow")
        self._build_ui()
        self._apply_row_style()

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 12, 12)
        layout.setSpacing(0)

        task_name = self.task.get("name") or self.task.get("task_name") or "Unnamed Task"
        desc = self.task.get("description") or ""
        estimated = self.task.get("estimated_hours")
        tracked_s = self._elapsed_seconds

        name_col = QVBoxLayout()
        name_col.setSpacing(2)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)

        # Leading state glyph, in the project's own colour. Purely a marker
        # for the row -- it carries no completion state and is not clickable
        # (task status is changed through the row's menu -> Edit, as before).
        self._leading_icon = QLabel(self)
        self._leading_icon.setPixmap(icons.pixmap("task_alt", self.project_color, 17))
        self._leading_icon.setStyleSheet("background: transparent;")
        self._leading_icon.setToolTip(self.project_name)
        name_row.addWidget(self._leading_icon)

        self._name_label = QLabel(task_name, self)
        self._name_label.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self._name_label.setWordWrap(False)
        name_row.addWidget(self._name_label)

        # A small, plain dot is the row's only "this one is running"
        # signal -- no pill, no background, no border, no shadow.
        self._active_dot = QLabel(self)
        self._active_dot.setObjectName("ActiveDot")
        # Same colour as the running row's outline: the dot and the border
        # are one indicator of one state, and sit inches apart.
        self._active_dot.setPixmap(icons.pixmap("circle_filled", ACTIVE_ROW_BORDER, 8))
        self._active_dot.setStyleSheet("background: transparent;")
        self._active_dot.setVisible(self._is_running)
        name_row.addWidget(self._active_dot)
        name_row.addStretch()
        name_col.addLayout(name_row)

        self._desc_label: Optional[QLabel] = None
        if desc:
            clean_desc = desc.replace("[duplicate]", "").strip()
            if clean_desc:
                self._desc_label = QLabel(clean_desc[:60] + ("…" if len(clean_desc) > 60 else ""), self)
                self._desc_label.setFont(QFont("Segoe UI", 10))
                name_col.addWidget(self._desc_label)

        self._name_widget = QWidget(self)
        self._name_widget.setStyleSheet("background: transparent;")
        self._name_widget.setLayout(name_col)
        _apply_column_extent(self._name_widget, "task", self._column_widths["task"])
        layout.addWidget(self._name_widget, 1)  # the only stretchy column
        layout.addWidget(self._make_column_spacer())

        created_str = _fmt_created(self.task.get("created_at"))
        # The glyph is part of the label's own rich text rather than a
        # second widget, so this stays the single width-carrying widget for
        # the CREATE ON column (see set_column_widths).
        self._created_label = QLabel(
            f"{icons.img_tag('calendar_month', TEXT_MUTED, 13)} {created_str}", self
        )
        self._created_label.setFont(QFont("Courier New", 10))
        self._created_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._created_label.setFixedWidth(self._column_widths["created"])
        layout.addWidget(self._created_label)
        layout.addWidget(self._make_column_spacer())

        tracked_col = QVBoxLayout()
        tracked_col.setSpacing(3)
        tracked_col.setAlignment(Qt.AlignmentFlag.AlignCenter)

        time_row = QHBoxLayout()
        time_row.setSpacing(6)
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._time_icon = QLabel(self)
        self._time_icon.setPixmap(icons.pixmap("timer", TEXT_MUTED, 14))
        self._time_icon.setStyleSheet("background: transparent;")
        time_row.addWidget(self._time_icon)

        self._time_label = QLabel(_fmt_seconds(tracked_s), self)
        self._time_label.setFont(QFont("Courier New", 13, QFont.Weight.Bold))
        self._time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        time_row.addWidget(self._time_label)
        tracked_col.addLayout(time_row)

        self._pct_label: Optional[QLabel] = None
        self._progress_bar: Optional[ProgressBar] = None
        pct = _pct(tracked_s, estimated)
        if pct is not None:
            self._pct_label = QLabel(f"{pct}%", self)
            self._pct_label.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
            self._pct_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tracked_col.addWidget(self._pct_label)

            self._progress_bar = ProgressBar(pct, self, dark=self._is_running)
            self._progress_bar.setFixedWidth(80)
            tracked_col.addWidget(self._progress_bar, alignment=Qt.AlignmentFlag.AlignCenter)

        self._tracked_widget = QWidget(self)
        self._tracked_widget.setStyleSheet("background: transparent;")
        self._tracked_widget.setLayout(tracked_col)
        self._tracked_widget.setFixedWidth(self._column_widths["tracked"])
        layout.addWidget(self._tracked_widget)
        layout.addWidget(self._make_column_spacer())

        # Centered within its column (addStretch on both sides) rather than
        # left-packed, so the buttons line up under the centered ACTION
        # header instead of hugging the column's left edge with dead space
        # to the right of them.
        action_col = QHBoxLayout()
        # Gap between the Start/Stop button and the kebab menu -- was 6px,
        # which read as the two controls touching.
        action_col.setSpacing(14)
        action_col.addStretch()

        self._timer_btn = QPushButton(self)
        self._timer_btn.setFixedHeight(32)
        self._timer_btn.setFixedWidth(96)
        self._timer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._timer_btn.clicked.connect(self._on_timer_clicked)
        action_col.addWidget(self._timer_btn)

        self._menu_btn = QToolButton(self)
        self._menu_btn.setIcon(icons.icon("more_vert", TEXT_SECONDARY, 18))
        self._menu_btn.setFixedSize(30, 34)
        self._menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._menu_btn.clicked.connect(self._show_context_menu)
        action_col.addWidget(self._menu_btn)
        action_col.addStretch()

        self._action_widget = QWidget(self)
        self._action_widget.setStyleSheet("background: transparent;")
        self._action_widget.setLayout(action_col)
        self._action_widget.setFixedWidth(self._column_widths["action"])
        layout.addWidget(self._action_widget)
        # No trailing addStretch(): the TASK column above is the stretchy
        # one and already absorbs whatever space these fixed columns don't
        # use, so the row fills the table's full width edge to edge.

        self._update_timer_button()
        self._timer_btn.setVisible(not self._readonly)

    def _make_column_spacer(self) -> QWidget:
        """A fixed, non-interactive gap matching ColumnResizeHandle's width,
        so this row's columns land exactly under the header's -- only the
        header handle is draggable; this is purely a spacer."""
        spacer = QWidget(self)
        spacer.setStyleSheet("background: transparent;")
        spacer.setFixedWidth(COLUMN_HANDLE_WIDTH)
        return spacer

    def set_column_widths(self, widths: Dict[str, int]) -> None:
        """Live-resize this row's columns to match a header drag, without
        rebuilding the row (mark_running/mark_stopped etc. must keep working
        on the same widget instances)."""
        self._column_widths = dict(widths)
        _apply_column_extent(self._name_widget, "task", widths["task"])
        self._created_label.setFixedWidth(widths["created"])
        self._tracked_widget.setFixedWidth(widths["tracked"])
        self._action_widget.setFixedWidth(widths["action"])

    def _apply_row_style(self) -> None:
        # No background tint and no shadow in either state. The actively
        # tracked row is outlined in the brand gradient -- the same blue to
        # violet as the Start/Stop button that put it in that state, so the
        # row and its control read as one thing. It was SUCCESS green, an
        # accent that appeared nowhere else on the row. Every other row keeps
        # the plain bottom divider. The outline is driven purely by
        # self._is_running, which only mark_running()/mark_stopped() set, and
        # those are only ever called from the TimerService's signals -- never
        # from a hardcoded task id.
        #
        # Both states declare a 2px border on all four edges (transparent
        # when idle), so switching between them does not move the row's
        # contents; only the 1px bottom divider grows to the 2px outline.
        if self._is_running:
            border = f"border: 2px solid {ACTIVE_ROW_BORDER};"
        else:
            # A 3px brand accent down the left edge -- the only difference
            # from the audited style, which left that edge transparent. The
            # running row keeps its full green outline, unchanged.
            border = (
                f"border: 2px solid transparent; "
                f"border-left: 3px solid {PRIMARY}; "
                f"border-bottom: 1px solid {BORDER_LIGHT};"
            )
        self.setStyleSheet(f"""
            QFrame#TaskRow {{
                background: {CARD_BG};
                {border}
                border-radius: 0px;
            }}
            QFrame#TaskRow:hover {{
                background: #FAFBFF;
            }}
        """)

    def _update_timer_button(self) -> None:
        self._apply_row_style()
        self._active_dot.setVisible(self._is_running)
        running = self._is_running

        self._name_label.setStyleSheet(f"color: {TEXT_PRIMARY};")
        if self._desc_label is not None:
            self._desc_label.setStyleSheet("color: #64748B;")
        self._created_label.setStyleSheet(
            f"background: transparent; color: {TEXT_SECONDARY};"
        )
        self._time_label.setStyleSheet(f"color: {TEXT_PRIMARY}; font-weight: 900;")
        if self._pct_label is not None:
            self._pct_label.setStyleSheet(f"color: {PRIMARY};")
        if self._progress_bar is not None:
            self._progress_bar.set_dark(False)

        self._menu_btn.setIcon(icons.icon("more_vert", TEXT_SECONDARY, 18))
        self._menu_btn.setStyleSheet(f"""
            QToolButton {{
                background: transparent; border: none; border-radius: 6px;
            }}
            QToolButton:hover {{
                background: {CONTENT_BG};
            }}
        """)

        # Same gradient colors either way; only the direction flips while
        # running, so "Stop" reads as visually distinct from its own idle
        # "Start" state without a different color, shadow, or border.
        if running:
            self._timer_btn.setText("Stop")
            gradient, gradient_hover = BUTTON_GRADIENT_REVERSED, BUTTON_GRADIENT_REVERSED_HOVER
        else:
            self._timer_btn.setText("Start")
            gradient, gradient_hover = BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER
        self._timer_btn.setStyleSheet(f"""
            QPushButton {{
                background: {gradient}; color: white;
                border: none; border-radius: 9px;
                font-size: 11.5px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {gradient_hover}; }}
            QPushButton:disabled {{ background: #E2E8F0; color: #94A3B8; }}
        """)

    def set_readonly(self, readonly: bool) -> None:
        """Show/hide Start/Stop for this row without touching timer state.

        Called when the viewed date changes to or from today. A timer that is
        already running keeps running regardless -- this only controls whether
        this row's own button is reachable. Browsing dates must never start,
        stop, reassign or reset tracking.
        """
        if readonly == self._readonly:
            return
        self._readonly = readonly
        self._timer_btn.setVisible(not readonly)

    def _on_timer_clicked(self) -> None:
        if self._is_running:
            self.stop_requested.emit(self)
        else:
            self.start_requested.emit(self)

    # ── Presentation (driven by TimerService; the row decides nothing) ─────

    def mark_running(self, entry_id: Optional[int] = None) -> None:
        """Render this row as the actively tracked task."""
        self._is_running = True
        self._entry_id = entry_id
        self._session_elapsed = 0
        self._timer_btn.setEnabled(True)
        self._update_timer_button()

    def mark_stopped(self, banked_seconds: Optional[int] = None) -> None:
        """
        Render this row as stopped.

        :param banked_seconds: Session seconds to fold into the task's running
            total. Supplied by the TimerService, which is the only component
            that knows the authoritative elapsed value.
        """
        self._is_running = False
        self._entry_id = None
        if banked_seconds:
            self._elapsed_seconds += banked_seconds
        self._session_elapsed = 0
        self._timer_btn.setEnabled(True)
        self._update_timer_button()
        self._time_label.setText(_fmt_seconds(self._elapsed_seconds))

    def set_pending(self, label: str) -> None:
        """Show a transient in-progress label without changing timer state."""
        self._timer_btn.setEnabled(False)
        self._timer_btn.setText(label)

    def set_running(self, running: bool, entry_id: Optional[int] = None, elapsed: int = 0) -> None:
        """Set the rendered running state and session elapsed in one call."""
        if running:
            self.mark_running(entry_id)
            self.update_elapsed_seconds(elapsed)
        else:
            self.mark_stopped()

    def update_elapsed_seconds(self, session_elapsed: int) -> None:
        """
        Render the elapsed time reported by the TimerService.

        This is the only path by which the displayed time changes while a
        timer runs. The row does not count seconds of its own, so a UI
        refresh, a rebuild of the row, or a missed tick cannot make the
        displayed value drift from the tracked value.
        """
        self._session_elapsed = session_elapsed
        self._time_label.setText(_fmt_seconds(self._elapsed_seconds + session_elapsed))

    def set_banked_seconds(self, seconds: int) -> None:
        """Update the task's already-tracked total (e.g. after a refresh)."""
        self._elapsed_seconds = seconds
        self._time_label.setText(
            _fmt_seconds(self._elapsed_seconds + (self._session_elapsed if self._is_running else 0))
        )

    def _show_context_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: white; border: 1px solid {BORDER_LIGHT};
                border-radius: 10px; padding: 4px;
            }}
            QMenu::item {{
                padding: 8px 18px; border-radius: 6px;
                color: {TEXT_PRIMARY}; font-size: 13px;
            }}
            QMenu::item:selected {{ background: {CONTENT_BG}; }}
            QMenu::separator {{
                height: 1px;
                background: {BORDER_LIGHT};
                margin: 4px 10px;
            }}
        """)
        edit_action = menu.addAction(icons.icon("edit", TEXT_PRIMARY), "Edit")
        dup_action = menu.addAction(icons.icon("content_copy", TEXT_PRIMARY), "Duplicate")
        menu.addSeparator()
        del_action = menu.addAction(icons.icon("delete", ERROR), "Delete")
        # Style the delete action text red
        del_action.setProperty("class", "destructive")
        del_widget = menu.widgetForAction(del_action) if hasattr(menu, 'widgetForAction') else None
        pos = self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft())
        selected = menu.exec(pos)
        if selected == edit_action: self.edit_requested.emit(self)
        elif selected == dup_action: self.duplicate_requested.emit(self)
        elif selected == del_action: self.delete_requested.emit(self)


# ─── Progress Bar ─────────────────────────────────────────────────────────────

class ProgressBar(QWidget):
    def __init__(self, percent: int, parent: Optional[QWidget] = None, dark: bool = False) -> None:
        super().__init__(parent)
        self._pct = max(0, min(100, percent))
        self._dark = dark
        self.setFixedHeight(4)
        self.setMinimumWidth(80)

    def set_dark(self, dark: bool) -> None:
        """Switch the track color for the running task's dark background --
        the light-gray track used on the normal white row is invisible there."""
        if dark == self._dark:
            return
        self._dark = dark
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        painter.setBrush(QColor("rgba(255,255,255,60)" if self._dark else "#E2E8F0"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, w, 4, 2, 2)
        fill_w = int(w * self._pct / 100)
        if fill_w > 0:
            color = "#F97316" if self._pct > 90 else ("#93C5FD" if self._dark else PRIMARY)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(0, 0, fill_w, 4, 2, 2)
        painter.end()


# ─── Task Table Section ───────────────────────────────────────────────────────

class TaskSection(QWidget):
    """
    Full My Tasks section: header, search, task rows.
    Manages the single-timer-at-a-time rule and CRUD operations.
    Emits timer_state_changed(is_active) for sidebar total-time updates.
    """
    timer_state_changed = Signal(bool)   # True = timer started, False = stopped
    error_occurred = Signal(str)
    active_timer_conflict = Signal()
    task_action_succeeded = Signal(str)  # Success notification message
    #: A task CRUD call succeeded: (kind, project_id, server payload), where
    #: kind is "created", "updated" or "deleted". DashboardWindow owns the task
    #: list and the cache, so it applies this and then reconciles; this widget
    #: does not write either. Replaces a blanket `refresh_requested`, which
    #: re-fetched projects, statuses, the day's time entries and the tasks
    #: before the user could see their own edit.
    task_mutated = Signal(str, int, object)
    #: Whether adding a task is possible right now (a project is loaded).
    #: The Add Task button lives in the top bar; this is how its enabled
    #: state follows the selection without the top bar knowing about tasks.
    add_task_available = Signal(bool)

    def __init__(
        self,
        api,
        task_service: TaskService,
        time_entry_service=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        """
        :param api: `background_services.public_api.BackgroundApi`. The only
            channel through which this widget reaches background work. It owns
            no threads and holds no timer state of its own.
        :param time_entry_service: `app.time_entries.service.TimeEntryService`.
            Optional so existing construction sites without it keep working;
            required in practice for the manual-time-entry Save action.
        """
        super().__init__(parent)
        self.api = api
        self.task_service = task_service
        self.time_entry_service = time_entry_service
        self._tasks: List[Dict[str, Any]] = []
        self._project: Optional[Dict[str, Any]] = None
        self._project_color = "#3B82F6"
        self._task_rows: List[TaskRow] = []
        # Mirrors of the TimerService's state, kept only for rendering.
        self._running_task_id: Optional[int] = None
        self._running_entry_id: Optional[int] = None
        self._running_task_name: Optional[str] = None
        self._user_id: Optional[int] = None
        self._search_text = ""
        self.user_role = None
        self._has_loaded_tasks = False
        #: The calendar day the window is showing, and whether that makes this
        #: list read-only. Only today is live: a past day is finished history
        #: and a future day cannot be tracked against at all, so both hide
        #: Start/Stop on every row regardless of whether a timer happens to be
        #: running elsewhere.
        #:
        #: The flag is the *rendered* state; every action re-derives the rule
        #: from `self._viewing_date` at the moment it runs, so a window left
        #: open across midnight cannot act on a stale verdict.
        self._viewing_date: date = ist_today()
        self._readonly_date = False
        #: Every project the user can see, for the manual-entry dialog's
        #: project dropdown -- distinct from self._tasks/self._project, which
        #: only ever cover the one project currently displayed.
        self._all_projects: List[Dict[str, Any]] = []
        self._manual_entry_dialog: Optional["ManualTimeEntryDialog"] = None
        #: The idempotency key of each task creation that has not succeeded
        #: yet, by (project id, task name). A create whose reply was lost is
        #: retried by the user typing the same name again; reusing the key
        #: lets the backend answer with the task it already created instead
        #: of a second one. Cleared on success, so creating a genuinely new
        #: task with the same name later gets a fresh key.
        self._pending_create_ops: Dict[Tuple[int, str], str] = {}

        # Subscribe to the authoritative timer. No local tick timer exists:
        # elapsed time is published by the service, never counted here.
        timer = self.api.timer
        timer.timer_started.connect(self._on_timer_started)
        timer.timer_stopped.connect(self._on_timer_stopped)
        timer.timer_tick.connect(self._on_timer_tick)
        timer.timer_error.connect(self._on_timer_error)
        self.api.sync.action_failed.connect(self._on_sync_action_failed)

        self._build_ui()

    @property
    def _local_cache(self):
        """Read-only repository access for cached lookups (e.g. task statuses)."""
        return self.api.cache

    def set_user_role(self, role_name: str) -> None:
        self.user_role = role_name

    def set_user_id(self, user_id: int) -> None:
        self._user_id = user_id

    def set_all_projects(self, projects: List[Dict[str, Any]]) -> None:
        """Every project the user can see, for the manual-entry dialog's
        project dropdown. Called by DashboardWindow whenever its own project
        list changes -- this section otherwise only knows the one project
        currently displayed."""
        self._all_projects = projects or []

    @property
    def is_admin(self) -> bool:
        return self.user_role in ["administrator", "org_admin", "super_admin"]

    def _run_task_mutation(
        self, call, success_message: str, key: str, *, kind: str, project_id: int,
        after_success: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Run a task CRUD call on the shared bounded pool.

        `key` de-duplicates: a double-click cannot produce two creates. On
        success the server's own answer is handed to DashboardWindow through
        `task_mutated`, which applies it to the list already on screen and
        then reconciles with a targeted reload.

        This used to emit `refresh_requested`, which ran the whole refresh
        round -- projects, task statuses, the day's time entries and the tasks
        -- and the new row appeared only when the last of those returned. The
        widget is still not guessing at what the server did: it is showing
        exactly what the server just said, which is the response to the very
        request that made the change.
        """
        def on_success(result) -> None:
            if after_success is not None:
                after_success()
            self.api.notify(success_message, NotificationLevel.SUCCESS, key=f"task-mut-{key}")
            self.task_action_succeeded.emit(success_message)
            self.task_mutated.emit(kind, project_id, result)

        def on_error(exc: BaseException) -> None:
            self.api.notify(str(exc), NotificationLevel.ERROR, key=f"task-mut-err-{key}")
            self.error_occurred.emit(str(exc))

        self.api.run_in_background(call, on_success=on_success, on_error=on_error, key=key)

    def _client_op_for_create(self, project_id: int, task_name: str) -> str:
        """The idempotency key for creating `task_name` in `project_id`.

        The same key is handed out again until a create with it succeeds, so
        a retry after a lost reply names the submission the backend already
        applied. See `_pending_create_ops`.
        """
        op_key = (project_id, task_name)
        client_op = self._pending_create_ops.get(op_key)
        if client_op is None:
            client_op = f"task:{uuid.uuid4().hex}"
            self._pending_create_ops[op_key] = client_op
        return client_op

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Card container ─────────────────────────────────────────
        card = QFrame(self)
        card.setObjectName("TaskCard")
        card.setStyleSheet(f"""
            QFrame#TaskCard {{
                background: {CARD_BG};
                border-radius: 12px;
                border: 1px solid {BORDER_LIGHT};
            }}
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        # No section header: the card starts straight at the column
        # headers. Everything that row used to carry now lives where it is
        # not duplicated -- the project name in the sidebar's selection, the
        # day's totals and the running task in the summary cards above, and
        # search / Add Task / Request in the top bar.

        # ── Column headers ─────────────────────────────────────────
        # Fixed pixel widths (COLUMN_DEFAULT_WIDTHS) plus a real drag handle
        # between each pair of columns, rather than stretch factors -- this
        # is what makes the columns actually resizable. self._column_widths
        # is the single source of truth every visible TaskRow reads from
        # too (see TaskSection._resize_columns / _rebuild_rows).
        self._column_widths: Dict[str, int] = dict(COLUMN_DEFAULT_WIDTHS)
        self._column_header_labels: Dict[str, QLabel] = {}

        col_header = QWidget(card)
        col_header.setFixedHeight(38)
        col_header.setStyleSheet(f"background: #F8FAFC; border-bottom: 1px solid {BORDER_LIGHT};")
        col_layout = QHBoxLayout(col_header)
        col_layout.setContentsMargins(16, 0, 12, 0)
        col_layout.setSpacing(0)

        def make_col_header(key: str) -> QLabel:
            lbl = QLabel(COLUMN_LABELS[key], col_header)
            lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            lbl.setStyleSheet(f"color: {TEXT_SECONDARY}; letter-spacing: 0.8px;")
            # TASK (the task name/description column) reads naturally
            # left-aligned; every other column's content is centered in its
            # row, so its header is too -- including ACTION, which used to
            # default to left-aligned while its buttons rendered elsewhere.
            if key != "task":
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _apply_column_extent(lbl, key, self._column_widths[key])
            self._column_header_labels[key] = lbl
            # TASK's header label carries the same stretch factor as the
            # TASK column widget in every row, so the header stays aligned
            # with the rows as the table fills the available width.
            col_layout.addWidget(lbl, 1 if key == STRETCH_COLUMN else 0)
            return lbl

        for i, key in enumerate(COLUMN_ORDER):
            make_col_header(key)
            if i < len(COLUMN_ORDER) - 1:
                next_key = COLUMN_ORDER[i + 1]
                handle = ColumnResizeHandle(col_header)
                handle.dragged.connect(
                    lambda delta, left=key, right=next_key: self._resize_columns(left, right, delta)
                )
                col_layout.addWidget(handle)
        # No trailing addStretch(): TASK's stretch factor above already
        # fills whatever width the fixed columns don't use.

        card_layout.addWidget(col_header)

        # ── Scrollable task list ───────────────────────────────────
        self._scroll = QScrollArea(card)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._rows_container = QWidget()
        self._rows_container.setStyleSheet(f"background: {CARD_BG};")
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.addStretch()

        self._scroll.setWidget(self._rows_container)
        card_layout.addWidget(self._scroll)

        # Status / empty / loading label
        self._status_label = QLabel("Select a project to see tasks.", self)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet(f"color: {TEXT_MUTED}; padding: 40px; font-size: 13px;")
        self._rows_layout.insertWidget(0, self._status_label)

        layout.addWidget(card)

        # TASK_TABLE_QSS carries the card, column-header and row styling.
        # The Add Task button's gradient rules left with the button itself
        # (it is the top bar's now -- see ui/topbar.py).
        self.setStyleSheet(TASK_TABLE_QSS)


    # ── Public API ─────────────────────────────────────────────────────────────

    def set_loading(self, project_name: str) -> None:
        """Show loading state."""
        self._clear_rows()
        self._status_label.setText(f"Loading tasks for {project_name}...")
        self._status_label.show()
        self._has_loaded_tasks = False

    def set_tasks(
        self,
        tasks: List[Dict[str, Any]],
        project: Dict[str, Any],
        color: str,
    ) -> None:
        """Populate rows from real API task data."""
        self._tasks = tasks or []
        self._project = project
        self._project_color = color
        self._search_text = ""
        self.add_task_available.emit(True)

        # _rebuild_rows() sets the title (name + task count) below -- no
        # need to set it here too and have it immediately overwritten.
        self._rebuild_rows()
        self._has_loaded_tasks = True

    def update_tasks_tracked_times(self, task_time_map: dict) -> None:
        """Update tasks list and rows with today's accumulated tracked times."""
        # 1. Update master tasks list
        for t in self._tasks:
            tid = t.get("id")
            if tid in task_time_map:
                t["time_tracked_seconds"] = task_time_map[tid]
            else:
                t["time_tracked_seconds"] = 0

        # 2. Update visible TaskRow widgets
        for row in self._task_rows:
            tid = row.task.get("id")
            base_elapsed = task_time_map.get(tid, 0)
            row._elapsed_seconds = base_elapsed
            if row._is_running:
                row._time_label.setText(_fmt_seconds(base_elapsed + getattr(row, "_session_elapsed", 0)))
            else:
                row._time_label.setText(_fmt_seconds(base_elapsed))


    def set_error(self, message: str) -> None:
        self._clear_rows()
        self._status_label.setText(f"{icons.img_tag('warning', ERROR)} {message}")
        self._status_label.show()
        self._has_loaded_tasks = False

    def clear(self) -> None:
        self._clear_rows()
        self._status_label.setText("Select a project to see tasks.")
        self._status_label.show()
        self.add_task_available.emit(False)
        self._tasks = []
        self._project = None
        self._has_loaded_tasks = False

    def apply_search(self, text: str) -> None:
        """Filter the list by task name.

        Driven by the top bar's search field. The text is held here rather
        than in a widget of this card's own, so there is exactly one filter
        state and no way for two search boxes to disagree.
        """
        text = text or ""
        if text == self._search_text:
            return
        self._search_text = text
        self._rebuild_rows()

    def set_viewing_date(self, target_date) -> None:
        """Called whenever the top bar's selected date changes.

        Only today is live. A past date is a read-only view of history and a
        future date is one nothing could have been tracked on, so both hide
        Start/Stop on every row. This used to test `target_date < ist_today()`,
        which is false for tomorrow — so a future day read as "not history" and
        kept the live controls. `is_live_date` partitions all three cases.

        Rows already on screen are updated in place; rows built afterwards (a
        search, a project switch) pick the current value up from
        `self._readonly_date`.

        An unreadable date is refused rather than defaulted: the viewed day
        gates tracking, and quietly treating a value nobody could parse as
        today is exactly the kind of fallback that hands a live Start button to
        a day the user never selected.
        """
        day = as_calendar_day(target_date)
        if day is None:
            log.warning("ignoring an unreadable viewing date: %r", target_date)
            return
        self._viewing_date = day
        readonly = not is_live_date(day)
        if readonly == self._readonly_date:
            return
        self._readonly_date = readonly
        for row in self._task_rows:
            row.set_readonly(readonly)

    def sync_active_timer(self, task_id: int, entry_id: int, elapsed: int = 0) -> None:
        """
        Render an already-running timer (e.g. after tasks load for a project
        whose task is being tracked).

        Purely presentational — it does not start, stop or re-anchor anything.
        The elapsed value is read from the service rather than the caller's
        argument when a session is live, so a stale argument cannot make the
        displayed time regress.
        """
        self._running_task_id = task_id
        self._running_entry_id = entry_id
        live_elapsed = self.api.timer_elapsed_seconds() if self.api.is_timer_running() else elapsed
        for row in self._task_rows:
            if row.task.get("id") == task_id:
                row.set_running(True, entry_id, live_elapsed)
            else:
                row.set_running(False)
        self.timer_state_changed.emit(True)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _visible_tasks(self) -> List[Dict[str, Any]]:
        """The tasks this list renders.

        Completed tasks are hidden here and only here: self._tasks, the local
        cache and the backend all keep them untouched, and nothing about the
        task's status is written or changed. The one exception is the task
        being tracked right now -- hiding it would strand its running timer
        with no Stop button, so it stays visible until it is stopped.
        """
        return [
            t for t in self._tasks
            if not is_task_completed(t) or t.get("id") == self._running_task_id
        ]

    def _task_order_key(self, task: Dict[str, Any]):
        """Sort key: newest tasks first.

        ``created_at`` is the durable ordering signal for task creation. Rows
        from older cached responses may not carry it, so their original
        backend position remains the deterministic fallback.

        The running timer is deliberately absent from this key. Ordering is a
        property of the task list, not of the timer, so starting a task leaves
        its row exactly where the user last saw it instead of jumping it to the
        top and shifting every row the user was about to click.
        """
        created_at = task.get("created_at") or task.get("createdAt")
        created_timestamp = 0.0
        if created_at:
            try:
                created_timestamp = datetime.fromisoformat(
                    str(created_at).replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError, OverflowError):
                created_timestamp = 0.0
        try:
            position = [t.get("id") for t in self._tasks].index(task.get("id"))
        except ValueError:
            position = len(self._tasks)
        # Prefixing with ``not created_at`` keeps timestamped rows ahead of
        # legacy rows; the numeric timestamp gives newest-first ordering.
        return (not bool(created_at), -created_timestamp, position)

    def _running_task_display_name(self) -> Optional[str]:
        """Name of the task being tracked, if any.

        The tracked task may belong to a project that is not the one on
        screen, so the service's own record is the fallback -- never a guess
        from whatever happens to be listed.
        """
        if self._running_task_id is None:
            return None
        task = next((t for t in self._tasks if t.get("id") == self._running_task_id), None)
        if task:
            return task.get("name") or task.get("task_name") or None
        session = self.api.active_session() or {}
        if session.get("task_id") == self._running_task_id:
            return session.get("task_name") or self._running_task_name
        return self._running_task_name

    def _rebuild_rows(self) -> None:
        self._clear_rows()

        filtered = [
            t for t in self._visible_tasks()
            if self._search_text.lower() in (t.get("name") or t.get("task_name") or "").lower()
        ]
        # Newest tasks first. The tracked task is not special-cased: it keeps
        # whatever position it already has.
        filtered.sort(key=self._task_order_key)

        project_name = (
            self._project.get("project_name", "Project") if self._project else "Project"
        )

        if not filtered:
            msg = "No tasks match your search." if self._search_text else "No tasks found for this project."
            self._status_label.setText(msg)
            self._status_label.show()
            return

        self._status_label.hide()

        for i, task in enumerate(filtered):
            color = self._project_color
            row = TaskRow(
                task=task,
                project_id=self._project.get("id", 0) if self._project else 0,
                project_name=project_name,
                project_color=color,
                is_running=(task.get("id") == self._running_task_id),
                readonly=self._readonly_date,
                column_widths=self._column_widths,
                parent=self._rows_container,
            )
            if task.get("id") == self._running_task_id:
                # Re-read the live value from the service rather than a cached
                # mirror, so a rebuilt row shows the true elapsed time.
                row.set_running(
                    True, self._running_entry_id, self.api.timer_elapsed_seconds()
                )

            row.start_requested.connect(self._handle_start_request)
            row.stop_requested.connect(self._handle_stop_request)
            row.edit_requested.connect(self._handle_edit_request)
            row.duplicate_requested.connect(self._handle_duplicate_request)
            row.delete_requested.connect(self._handle_delete_request)
            row.error_occurred.connect(self.error_occurred)

            self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
            self._task_rows.append(row)

    def _clear_rows(self) -> None:
        for row in self._task_rows:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._task_rows.clear()

    def _resize_columns(self, left_key: str, right_key: str, delta: int) -> None:
        """
        Handle a drag on the divider between two adjacent header columns.

        The two columns trade width one-for-one (widening one narrows its
        neighbor by the same amount, like a real splitter) and neither is
        allowed below its COLUMN_MIN_WIDTHS floor -- dragging past that
        point simply stops moving that edge rather than shrinking the
        column further. Applied to the header labels and every visible row
        in the same call, so they can never drift out of sync mid-drag.
        """
        new_left = self._column_widths[left_key] + delta
        new_right = self._column_widths[right_key] - delta

        if new_left < COLUMN_MIN_WIDTHS[left_key]:
            shortfall = COLUMN_MIN_WIDTHS[left_key] - new_left
            new_left = COLUMN_MIN_WIDTHS[left_key]
            new_right -= shortfall
        if new_right < COLUMN_MIN_WIDTHS[right_key]:
            shortfall = COLUMN_MIN_WIDTHS[right_key] - new_right
            new_right = COLUMN_MIN_WIDTHS[right_key]
            new_left -= shortfall
        new_left = max(new_left, COLUMN_MIN_WIDTHS[left_key])
        new_right = max(new_right, COLUMN_MIN_WIDTHS[right_key])

        if new_left == self._column_widths[left_key] and new_right == self._column_widths[right_key]:
            return  # both columns already at their floor; nothing to do

        self._column_widths[left_key] = new_left
        self._column_widths[right_key] = new_right
        _apply_column_extent(self._column_header_labels[left_key], left_key, new_left)
        _apply_column_extent(self._column_header_labels[right_key], right_key, new_right)
        for row in self._task_rows:
            row.set_column_widths(self._column_widths)

    # ── Timer workflow ────────────────────────────────────────────────────────
    #
    # There is exactly one path here. The audited version carried three
    # competing implementations side by side — a TrackingManager path, a
    # SyncQueue "optimistic" path that reached into `sync_queue._cache` to
    # enqueue rows itself, and a "legacy" path that spawned QThread workers
    # from the widget — selected by whichever collaborator happened to be
    # injected. They maintained separate notions of the running task, the
    # entry id and the elapsed seconds, which is how the UI could disagree
    # with the cache and with the backend simultaneously.
    #
    # All three are gone. This widget expresses intent; TimerService owns the
    # state and the durability, and publishes it back through signals.

    def _handle_start_request(self, row: TaskRow) -> None:
        # Three layers, and the button is only the first of them. It is hidden
        # unless today is on screen; this check re-derives the same rule from
        # the clock, so a stray or queued signal cannot ride a stale flag; and
        # `for_date` carries the day the user is acting on down to TimerService,
        # which refuses anything that is not today at the action layer. A
        # disabled control is a courtesy, not a guarantee.
        if not is_live_date(self._viewing_date):
            log.info("refusing to start tracking: %s is not today", self._viewing_date)
            return
        task_id = row.task.get("id")
        if task_id is None:
            return
        task_name = row.task.get("name") or row.task.get("task_name") or "Unnamed Task"
        row.set_pending("Starting…")
        # switch() handles both "nothing running" and "something else running";
        # the service serialises stop-then-start so the two can never race.
        self.api.switch_timer(
            row.project_id, task_id, task_name, for_date=self._viewing_date
        )

    def _handle_stop_request(self, row: TaskRow) -> None:
        if not is_live_date(self._viewing_date):
            log.info("refusing to stop tracking: %s is not today", self._viewing_date)
            return
        row.set_pending("Stopping…")
        self.api.stop_timer(for_date=self._viewing_date)

    # ── TimerService subscriptions ────────────────────────────────────────────

    def _on_timer_started(self, session: dict) -> None:
        """The authoritative timer began (or was recovered/adopted)."""
        task_id = session.get("task_id")
        entry_id = session.get("entry_id")
        task_name = session.get("task_name") or "Task"
        self._running_task_id = task_id
        self._running_entry_id = entry_id
        self._running_task_name = task_name

        for row in self._task_rows:
            if row.task.get("id") == task_id:
                row.mark_running(entry_id)
            elif row._is_running:
                row.mark_stopped()

        self.timer_state_changed.emit(True)
        self._on_timer_tick(self.api.timer_elapsed_seconds())
        self.api.notify(f"Timer started for '{task_name}'", NotificationLevel.SUCCESS, key=f"timer-started-{task_id}")

    def _on_timer_stopped(self, payload: dict) -> None:
        """
        The authoritative timer stopped.
        """
        session = payload.get("session") or {}
        task_id = session.get("task_id")
        elapsed = payload.get("elapsed_seconds", 0)

        for row in self._task_rows:
            if row.task.get("id") == task_id:
                row.mark_stopped(banked_seconds=elapsed)
            elif row._is_running:
                row.mark_stopped()

        # Fold the finished session into the task's banked total, the same
        # value and from the same source the row just folded into its own
        # display. Without this the header's project total would drop by the
        # whole session the moment the timer stopped, until the next refresh
        # re-read the day's entries.
        for task in self._tasks:
            if task.get("id") == task_id:
                task["time_tracked_seconds"] = int(task.get("time_tracked_seconds") or 0) + elapsed
                break

        if self._running_task_id == task_id:
            self._running_task_id = None
            self._running_entry_id = None
            self._running_task_name = None

        self.timer_state_changed.emit(False)
        self.api.notify("Timer stopped. Time entry saved.", NotificationLevel.INFO, key=f"timer-stopped-{task_id}")

    def _on_timer_tick(self, elapsed: int) -> None:
        """
        Render the elapsed seconds reported by the service.

        Skipped while any day other than today is on screen: that row's base is
        the completed-hours total for the viewed date (set by
        update_tasks_tracked_times), and folding today's live `elapsed` onto
        it would mix the two. The row keeps showing its completed total,
        unticking, until the user navigates back to today.
        """
        if self._running_task_id is None or not is_live_date(self._viewing_date):
            return
        for row in self._task_rows:
            if row.task.get("id") == self._running_task_id:
                row.update_elapsed_seconds(elapsed)
                break

    def _on_timer_error(self, message: str) -> None:
        for row in self._task_rows:
            row.set_running(row.task.get("id") == self._running_task_id,
                            self._running_entry_id)
        self.api.notify(f"Timer error: {message}", NotificationLevel.ERROR, key="timer-error")
        self.error_occurred.emit(message)

    # ── Sync feedback ─────────────────────────────────────────────────────────

    def _on_sync_action_failed(self, action_id: str, action_type: str,
                               error: str, will_retry: bool) -> None:
        """
        Surface a durable-sync failure without corrupting local state.

        A failed API call must not reset valid local state: the user's action
        already happened and is recorded durably. Only a permanent failure of
        a timer operation is worth telling the user about, and even then the
        local timer keeps its own truth.
        """
        if will_retry:
            return
        if action_type in ("start_timer", "stop_timer", "switch_timer"):
            self.error_occurred.emit(
                "Could not sync your timer to the server yet. "
                "It is saved locally and will retry automatically."
            )

    # ── Task CRUD operations ──────────────────────────────────────────────────

    def open_add_task_dialog(self) -> None:
        """Open the Add Task dialog.

        The public entry point for the top bar's Add Task button; the dialog
        and the create call stay owned here.
        """
        self._on_add_task_clicked()

    def _on_add_task_clicked(self) -> None:
        if not self._project:
            return
        project_id = self._project.get("id")
        if project_id is None:
            return

        proj_name = self._project.get("project_name", "Project")
        dialog = AddTaskDialog(proj_name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        data = dialog.get_data()
        if not data["task_name"]:
            QMessageBox.warning(self, "Validation Error", "Task Name is required.")
            return

        # An employee's own task is assigned to them, which is what they
        # expect and what already worked. Anyone else -- admin, leader --
        # creates the task unassigned and gives it an owner through Edit
        # Task afterwards. Self-assigning for them is what produced the
        # HTTP 400: the backend only accepts an employee as an assignee, so
        # an admin's own id was always refused.
        assignee_id = self._user_id if self.user_role == "employee" else None
        task_name = data["task_name"]
        client_op = self._client_op_for_create(project_id, task_name)

        self._run_task_mutation(
            lambda: self.task_service.create_task(
                project_id, task_name, assignee_id, client_op=client_op
            ),
            success_message="Task created successfully.",
            key=f"create-task:{project_id}:{task_name}",
            kind="created",
            project_id=project_id,
            after_success=lambda: self._pending_create_ops.pop((project_id, task_name), None),
        )

    # ── Manual time entry ─────────────────────────────────────────────────────
    #
    # Distinct from the live Start/Stop timer above: this logs a completed
    # session after the fact, through the backend's existing manual-entry
    # endpoint (the same one TimerService's own overlap checking guards
    # against), never the timer's own start/stop path.

    def open_manual_entry_dialog(self) -> None:
        """Open the manual time-entry ("Request") dialog.

        The public entry point for the top bar's Request button; the dialog
        and everything it submits stay owned here.
        """
        self._on_manual_entry_clicked()

    def _on_manual_entry_clicked(self) -> None:
        if not self._all_projects:
            QMessageBox.information(
                self, "No Projects",
                "No projects are available yet. Try again once projects have loaded."
            )
            return
        initial_project_id = self._project.get("id") if self._project else None
        dialog = ManualTimeEntryDialog(self._all_projects, initial_project_id, self)
        self._manual_entry_dialog = dialog
        dialog.project_changed.connect(self._on_manual_entry_project_changed)
        # Load tasks for whichever project ends up selected when the dialog
        # opens -- whether that came from initial_project_id or just the
        # combo box's own default first item -- now that something is
        # actually listening for it.
        if dialog.project_combo.currentData() is not None:
            self._on_manual_entry_project_changed(dialog.project_combo.currentData())

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._submit_manual_entry(dialog.get_data())
        self._manual_entry_dialog = None

    def _on_manual_entry_project_changed(self, project_id: int) -> None:
        dialog = self._manual_entry_dialog
        if dialog is None:
            return
        dialog.set_tasks_loading()

        def on_success(tasks: list) -> None:
            if self._manual_entry_dialog is dialog:
                dialog.set_tasks(tasks)

        def on_error(exc: BaseException) -> None:
            if self._manual_entry_dialog is dialog:
                dialog.set_tasks([])

        self.api.run_in_background(
            lambda: self.task_service.get_tasks_for_project(project_id),
            on_success=on_success,
            on_error=on_error,
            key=f"manual-entry-tasks:{project_id}",
        )

    def _submit_manual_entry(self, data: Dict[str, Any]) -> None:
        if self.time_entry_service is None:
            self.api.notify(
                "Manual time entry is unavailable right now.",
                NotificationLevel.ERROR, key="manual-entry-unavailable",
            )
            return

        def on_success(_result) -> None:
            message = "Manual time entry submitted for approval."
            self.api.notify(message, NotificationLevel.SUCCESS, key="manual-entry-created")
            self.task_action_succeeded.emit(message)

        def on_error(exc: BaseException) -> None:
            message = getattr(exc, "message", None) or str(exc)
            self.api.notify(message, NotificationLevel.ERROR, key="manual-entry-error")
            self.error_occurred.emit(message)

        self.api.run_in_background(
            lambda: self.time_entry_service.create_manual_time_entry(**data),
            on_success=on_success,
            on_error=on_error,
            key=f"manual-entry-create:{data['project_id']}:{data['task_id']}:{data['start_time']}",
        )

    def _handle_edit_request(self, row: TaskRow) -> None:
        statuses = []
        if self._local_cache:
            statuses = self._local_cache.get_cached_task_statuses() or []
        dialog = EditTaskDialog(row.task, statuses, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            data = dialog.get_data()
            if not data["task_name"]:
                QMessageBox.warning(self, "Validation Error", "Task Name is required.")
                return

            task_id = row.task.get("id")
            self._run_task_mutation(
                lambda: self.task_service.update_task(
                    row.project_id, task_id, data["task_name"], data["status_id"]
                ),
                success_message="Task updated successfully.",
                key=f"update-task:{task_id}",
                kind="updated",
                project_id=row.project_id,
            )

    def _handle_duplicate_request(self, row: TaskRow) -> None:
        orig_name = row.task.get("name") or row.task.get("task_name") or "Task"
        orig_desc = row.task.get("description") or ""
        new_desc = orig_desc
        if "[duplicate]" not in orig_desc:
            new_desc = f"{orig_desc}\n[duplicate]".strip()

        assignee_id = row.task.get("assignee_id") or 1
        self._run_task_mutation(
            lambda: self.task_service.create_task(
                row.project_id, f"{orig_name} (Copy)", assignee_id
            ),
            success_message="Task duplicated successfully.",
            key=f"duplicate-task:{row.task.get('id')}",
            kind="created",
            project_id=row.project_id,
        )

    def _handle_delete_request(self, row: TaskRow) -> None:
        task_name = row.task.get("name") or row.task.get("task_name") or "Task"
        dialog = DeleteConfirmDialog(task_name, is_admin=self.is_admin, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            task_id = row.task.get("id")
            self._run_task_mutation(
                lambda: self.task_service.delete_task(row.project_id, task_id),
                success_message="Task deleted successfully.",
                key=f"delete-task:{task_id}",
                kind="deleted",
                project_id=row.project_id,
            )

