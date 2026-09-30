"""
Reassign time — the secondary dialog opened from the idle alert.

It collects a project and a task and hands them to its parent, which asks the
idle service to perform the reassignment. It performs no HTTP call of its own
and holds no idle state: it is a form, and it is transient.

Cancel writes nothing. That is not a nicety — it is the contract in the
product spec: the idle period stays pending, no reassignment record is
created, and the mandatory alert underneath is still waiting for a real
answer.

Project and task lists come from the same loaders the dashboard uses, so the
dropdowns show exactly what this user is authorised for. The backend
re-validates both (and that the task belongs to the chosen project) — this
dialog additionally prevents the invalid combination from being submitted at
all, by clearing and reloading tasks whenever the project changes.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QByteArray, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout, QWidget,
)

from ui.dropdown import PickerComboBox
from ui.styles import (
    BORDER_LIGHT, BORDER_MID, BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER,
    CONTENT_BG, ERROR, MONITRA_MARK_SVG, PRIMARY, TEXT_MUTED, TEXT_PRIMARY,
    TEXT_SECONDARY,
)

#: Sentinel shown while nothing is chosen. Carries no id, so it can never be
#: submitted as a selection.
PLACEHOLDER_PROJECT = "Select project"
PLACEHOLDER_TASK = "Select task"


def project_name_of(project: Dict[str, Any]) -> str:
    return project.get("project_name") or project.get("name") or "Unnamed project"


def task_name_of(task: Dict[str, Any]) -> str:
    return task.get("name") or task.get("task_name") or "Unnamed task"


class ReassignTimeDialog(QDialog):
    """Project/task picker for reassigning an idle period.

    Signals:
        reassign_requested(int, int) — project_id, task_id
    """

    reassign_requested = Signal(int, int)

    def __init__(
        self,
        api,
        *,
        duration_text: str,
        project_loader: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        task_loader: Optional[Callable[[int], List[Dict[str, Any]]]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.api = api
        self._project_loader = project_loader
        self._task_loader = task_loader
        #: Cleared on close. Every background callback checks it, so a reply
        #: that arrives after the user cancelled cannot touch a dead widget.
        self._alive = True
        self._busy = False
        #: Monotonic ticket for task loads. A reply for a project the user has
        #: since navigated away from is discarded rather than rendered.
        self._task_request = 0

        self.setWindowTitle("Reassign time")
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedWidth(440)

        self._build_ui(duration_text)
        self._apply_style()
        self._load_projects()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_ui(self, duration_text: str) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)

        self.card = QFrame(self)
        self.card.setObjectName("ReassignCard")
        card = QVBoxLayout(self.card)
        card.setContentsMargins(26, 22, 26, 20)
        card.setSpacing(14)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(0, 0, 0, 50))
        shadow.setOffset(0, 6)
        self.card.setGraphicsEffect(shadow)

        brand = QHBoxLayout()
        brand.setSpacing(9)
        mark = QSvgWidget(self.card)
        mark.load(QByteArray(MONITRA_MARK_SVG.encode()))
        mark.setFixedSize(30, 30)
        brand.addWidget(mark)
        wordmark = QLabel("Monitra", self.card)
        wordmark.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        wordmark.setStyleSheet(f"color: {TEXT_PRIMARY}; letter-spacing: -0.4px;")
        brand.addWidget(wordmark)
        brand.addStretch()
        card.addLayout(brand)

        divider = QFrame(self.card)
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {BORDER_LIGHT}; border: none;")
        card.addWidget(divider)

        title = QLabel("Reassign time", self.card)
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.DemiBold))
        title.setStyleSheet(f"color: {TEXT_PRIMARY};")
        card.addWidget(title)

        self.duration_label = QLabel(f"Reassign time: {duration_text}", self.card)
        self.duration_label.setFont(QFont("Segoe UI", 11))
        self.duration_label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        card.addWidget(self.duration_label)

        card.addWidget(self._field_label("Project"))
        # Searchable, both of them: the user is choosing among every project
        # they can see, with an idle alert waiting behind the dialog.
        self.project_combo = PickerComboBox(
            self.card, searchable=True, search_placeholder="Search projects…"
        )
        self.project_combo.setObjectName("PickerCombo")
        self.project_combo.setMinimumHeight(38)
        self.project_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        card.addWidget(self.project_combo)

        card.addWidget(self._field_label("Task"))
        self.task_combo = PickerComboBox(
            self.card, searchable=True, search_placeholder="Search tasks…"
        )
        self.task_combo.setObjectName("PickerCombo")
        self.task_combo.setMinimumHeight(38)
        self.task_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        # Disabled until a project is chosen, so an orphan task can never be
        # selected before the project it must belong to.
        self.task_combo.setEnabled(False)
        self.task_combo.currentIndexChanged.connect(self._update_submit_state)
        card.addWidget(self.task_combo)

        self.status_label = QLabel("", self.card)
        self.status_label.setFont(QFont("Segoe UI", 10))
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        card.addWidget(self.status_label)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()

        self.cancel_btn = QPushButton("Cancel", self.card)
        self.cancel_btn.setObjectName("SecondaryBtn")
        self.cancel_btn.setMinimumSize(96, 36)
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self._on_cancel)
        actions.addWidget(self.cancel_btn)

        self.reassign_btn = QPushButton("Reassign", self.card)
        self.reassign_btn.setObjectName("PrimaryBtn")
        self.reassign_btn.setMinimumSize(112, 36)
        self.reassign_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reassign_btn.setEnabled(False)
        self.reassign_btn.clicked.connect(self._on_reassign)
        actions.addWidget(self.reassign_btn)

        card.addLayout(actions)
        outer.addWidget(self.card)

        self._reset_projects(PLACEHOLDER_PROJECT)
        self._reset_tasks(PLACEHOLDER_TASK)

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text, self.card)
        label.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        label.setStyleSheet(f"color: {TEXT_SECONDARY};")
        return label

    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QFrame#ReassignCard {{
                background-color: #FFFFFF;
                border: none;
                border-radius: 14px;
            }}
            QComboBox#PickerCombo {{
                border: 1.5px solid {BORDER_LIGHT};
                border-radius: 9px;
                padding: 6px 12px;
                background: {CONTENT_BG};
                font-size: 13px;
                color: {TEXT_PRIMARY};
            }}
            QComboBox#PickerCombo:hover {{
                border-color: {BORDER_MID};
                background: #FFFFFF;
            }}
            QComboBox#PickerCombo:focus {{
                border-color: {PRIMARY};
                background: #FFFFFF;
            }}
            QComboBox#PickerCombo:disabled {{
                color: {TEXT_MUTED};
                background: #F8FAFC;
            }}
            /* The arrow and the list belong to PickerComboBox (ui/dropdown.py).
               This sheet used to set `::drop-down {{ border: none }}` itself,
               which makes Qt stop painting the arrow: these two fields had
               no sign that they opened. */
            QPushButton {{
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton#PrimaryBtn {{
                background: {BUTTON_GRADIENT};
                border: none;
                color: #FFFFFF;
                padding: 0 16px;
            }}
            QPushButton#PrimaryBtn:hover {{
                background: {BUTTON_GRADIENT_HOVER};
            }}
            QPushButton#PrimaryBtn:disabled {{
                background: {BORDER_MID};
                color: #FFFFFF;
            }}
            QPushButton#SecondaryBtn {{
                background-color: #FFFFFF;
                border: 1.5px solid {BORDER_LIGHT};
                color: {TEXT_SECONDARY};
                padding: 0 16px;
            }}
            QPushButton#SecondaryBtn:hover {{
                background-color: #F8FAFC;
                border-color: {BORDER_MID};
                color: {TEXT_PRIMARY};
            }}
        """)

    # ── Public surface (driven by the idle alert) ─────────────────────────────

    def set_duration_text(self, text: str) -> None:
        if self._alive:
            self.duration_label.setText(f"Reassign time: {text}")

    def show_error(self, message: str) -> None:
        """Render a failed reassignment and let the user try again."""
        if not self._alive:
            return
        self._set_busy(False)
        self._set_status(message, ERROR)

    def close_after_success(self) -> None:
        """Close because the reassignment landed (or the alert is finishing)."""
        self._alive = False
        self._cancel_pending_loads()
        self.accept()

    # ── Loading ───────────────────────────────────────────────────────────────

    def _reset_projects(self, placeholder: str) -> None:
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        self.project_combo.addItem(placeholder, None)
        self.project_combo.blockSignals(False)

    def _reset_tasks(self, placeholder: str) -> None:
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        self.task_combo.addItem(placeholder, None)
        self.task_combo.blockSignals(False)

    def _load_projects(self) -> None:
        # Paint whatever is cached first, so the dropdown is usable
        # immediately and a failed refresh leaves real data on screen rather
        # than blanking it.
        cached = None
        try:
            cached = self.api.cache.get_cached_projects()
        except Exception:  # noqa: BLE001
            cached = None
        if cached:
            self._render_projects(cached)

        if self._project_loader is None:
            if not cached:
                self._set_status("No projects are available to reassign to.", TEXT_SECONDARY)
            return

        if not cached:
            self._reset_projects("Loading projects…")
            self.project_combo.setEnabled(False)

        self.api.run_in_background(
            self._project_loader,
            on_success=self._on_projects_loaded,
            on_error=self._on_projects_error,
            key="idle-reassign-projects",
        )

    def _on_projects_loaded(self, projects: Any) -> None:
        if not self._alive:
            return
        self.project_combo.setEnabled(True)
        self._render_projects(projects if isinstance(projects, list) else [])

    def _on_projects_error(self, exc: BaseException) -> None:
        if not self._alive:
            return
        self.project_combo.setEnabled(True)
        if self.project_combo.count() <= 1 and self.project_combo.itemData(0) is None:
            self._reset_projects(PLACEHOLDER_PROJECT)
        self._set_status(f"Could not load projects: {exc}", ERROR)

    def _render_projects(self, projects: List[Dict[str, Any]]) -> None:
        previous = self.project_combo.currentData()
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        self.project_combo.addItem(PLACEHOLDER_PROJECT, None)
        for project in projects:
            project_id = project.get("id")
            if project_id is None:
                continue
            name = project_name_of(project)
            self.project_combo.addItem(name, int(project_id))
            self.project_combo.setItemData(
                self.project_combo.count() - 1, name, Qt.ItemDataRole.ToolTipRole
            )
        if previous is not None:
            index = self.project_combo.findData(previous)
            if index >= 0:
                self.project_combo.setCurrentIndex(index)
        self.project_combo.blockSignals(False)
        if not projects and self.project_combo.count() == 1:
            self._set_status("No projects are available to reassign to.", TEXT_SECONDARY)
        self._update_submit_state()

    def _on_project_changed(self, _index: int) -> None:
        """A different project invalidates the task selection entirely."""
        project_id = self.project_combo.currentData()
        self._set_status("", TEXT_SECONDARY)
        # Clear first, unconditionally: a task from the previously selected
        # project must never remain selectable under the new one.
        self._reset_tasks(PLACEHOLDER_TASK)
        self.task_combo.setEnabled(False)
        self._update_submit_state()
        if project_id is None:
            return

        self._task_request += 1
        ticket = self._task_request
        cached = None
        try:
            cached = self.api.cache.get_cached_tasks(int(project_id))
        except Exception:  # noqa: BLE001
            cached = None
        if cached:
            self._render_tasks(cached, int(project_id), ticket)
        else:
            self._reset_tasks("Loading tasks…")

        if self._task_loader is None:
            return
        loader, pid = self._task_loader, int(project_id)
        self.api.run_in_background(
            lambda: loader(pid),
            on_success=lambda tasks: self._on_tasks_loaded(tasks, pid, ticket),
            on_error=lambda exc: self._on_tasks_error(exc, ticket),
            key=f"idle-reassign-tasks:{pid}",
        )

    def _on_tasks_loaded(self, tasks: Any, project_id: int, ticket: int) -> None:
        if not self._alive or ticket != self._task_request:
            return  # the user changed project; this reply is stale
        self._render_tasks(tasks if isinstance(tasks, list) else [], project_id, ticket)

    def _on_tasks_error(self, exc: BaseException, ticket: int) -> None:
        if not self._alive or ticket != self._task_request:
            return
        if self.task_combo.count() <= 1:
            self._reset_tasks(PLACEHOLDER_TASK)
        self._set_status(f"Could not load tasks: {exc}", ERROR)
        self._update_submit_state()

    def _render_tasks(self, tasks: List[Dict[str, Any]], project_id: int, ticket: int) -> None:
        if ticket != self._task_request or self.project_combo.currentData() != project_id:
            return
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        self.task_combo.addItem(PLACEHOLDER_TASK, None)
        for task in tasks:
            task_id = task.get("id")
            if task_id is None:
                continue
            name = task_name_of(task)
            self.task_combo.addItem(name, int(task_id))
            self.task_combo.setItemData(
                self.task_combo.count() - 1, name, Qt.ItemDataRole.ToolTipRole
            )
        self.task_combo.blockSignals(False)
        has_tasks = self.task_combo.count() > 1
        self.task_combo.setEnabled(has_tasks)
        if not has_tasks:
            self._set_status("This project has no tasks to reassign to.", TEXT_SECONDARY)
        self._update_submit_state()

    # ── Submission ────────────────────────────────────────────────────────────

    def _update_submit_state(self) -> None:
        """Both fields are mandatory; the button says so by staying disabled."""
        ready = (
            self.project_combo.currentData() is not None
            and self.task_combo.currentData() is not None
        )
        self.reassign_btn.setEnabled(ready and not self._busy)

    def _on_reassign(self) -> None:
        if self._busy:
            return
        project_id = self.project_combo.currentData()
        task_id = self.task_combo.currentData()
        # Validated here as well as by the disabled button, so no code path
        # can submit an incomplete pair even if the state were ever wrong.
        if project_id is None or task_id is None:
            self._set_status("Select both a project and a task.", ERROR)
            return
        self._set_busy(True)
        self._set_status("Reassigning…", TEXT_SECONDARY)
        self.reassign_requested.emit(int(project_id), int(task_id))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.project_combo.setEnabled(not busy)
        self.task_combo.setEnabled(not busy and self.task_combo.count() > 1)
        self.cancel_btn.setEnabled(not busy)
        self._update_submit_state()

    def _set_status(self, message: str, color: str) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {color};")
        self.status_label.setVisible(bool(message))

    # ── Dismissal ─────────────────────────────────────────────────────────────

    def _on_cancel(self) -> None:
        """Cancel: no request, no record, the idle period stays pending."""
        if self._busy:
            return
        self.reject()

    def reject(self) -> None:
        if self._busy:
            return  # a reassignment is in flight; do not abandon it mid-request
        self._alive = False
        self._cancel_pending_loads()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._alive = False
        self._cancel_pending_loads()
        super().closeEvent(event)

    def _cancel_pending_loads(self) -> None:
        """Drop in-flight loads so no callback outlives this dialog."""
        try:
            self.api.cancel_key("idle-reassign-projects")
            project_id = self.project_combo.currentData()
            if project_id is not None:
                self.api.cancel_key(f"idle-reassign-tasks:{int(project_id)}")
        except Exception:  # noqa: BLE001
            pass
