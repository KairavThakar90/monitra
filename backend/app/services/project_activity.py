"""What a project or task change *was*, in words, for the activity trail.

``ActivityLogService`` records a row; this module is where a project, its team
or a task is compared with what it was a moment ago, so a row says "Changed the
status of the project "Apollo" from Active to On hold" rather than "Updated
the project (status)". It writes nothing itself: every function either reads
(a *before* snapshot, taken ahead of the change while the old values can still
be read) or returns the field dicts ``ActivityLogService.capture_many`` records.

Two rules keep it safe to call from the services:

* **Only real changes.** A form that re-sends every field leaves no trace of
  the ones it did not change, and an edit that changed nothing records nothing.
* **Never the action's problem.** The snapshot goes through
  ``ActivityLogService.snapshot`` (None when it cannot be read) and the rows
  are built inside ``capture_many``'s protection. A missing snapshot degrades
  to the plain "Updated the project" row; it never fails the edit.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.activity_log import ActivityLogAction, ActivityLogModule
from app.models.project_member import ProjectMember
from app.models.task_assignee import TaskAssignee
from app.models.user import User
from app.repositories.status_catalog import StatusCatalog
from app.services.activity_log import ActivityLogService

#: What an edit touched, in the words the trail shows, keyed on the payload's
#: field names.
PROJECT_FIELD_LABELS = {
    "project_name": "name", "description": "description", "status_id": "status",
    "owner_id": "owner", "leader_id": "leader", "employee_ids": "team",
    "deadline": "deadline", "billing_type": "billing", "fixed_hours": "hour budget",
    "category": "category",
}
TASK_FIELD_LABELS = {
    "name": "name", "description": "description", "status_id": "status",
    "assignee_id": "assignee", "estimated_hours": "estimated hours",
}

#: How `projects.billing_type` is said on screen.
_BILLING = {"free": "free time", "fixed": "fixed hours", "non_billing": "non-billing"}

#: How `projects.category` is said on screen; NULL is said as "none" by `_show`.
_CATEGORY = {"kyle": "Kyle project", "st": "ST project"}


def _people(db: Session, ids: Iterable[Optional[int]]) -> Dict[int, str]:
    """Names by id, in one query. Absent ids are simply not in the answer."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {uid: name for uid, name in db.execute(select(User.id, User.name).where(User.id.in_(wanted))).all()}


def _person(people: Dict[int, str], user_id: Optional[int]) -> str:
    """A name, ``nobody`` for no one, and the id for an account that is gone --
    never an invented name."""
    if user_id is None:
        return "nobody"
    return people.get(user_id) or f"member #{user_id}"


def _names(people: Dict[int, str], ids: Iterable[int]) -> str:
    return ActivityLogService.join_names(sorted(_person(people, i) for i in ids))


def _status_name(db: Session, kind: str, status_id: Optional[int]) -> str:
    if status_id is None:
        return "no status"
    row = StatusCatalog.project_status(db, status_id) if kind == "project" else StatusCatalog.task_status(db, status_id)
    return row.name if row else f"status #{status_id}"


def _show(value: Any) -> str:
    """A field value as a person would say it: ``none`` for nothing, dates as
    ISO, hours without a trailing ``.00``."""
    if value is None or value == "":
        return "none"
    if isinstance(value, Decimal):
        return f"{float(value):g}"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row(actor: User, module: str, action: str, description: str, **ids: Optional[int]) -> Dict[str, Any]:
    return {"actor": actor, "module": module, "action": action, "description": description, **ids}


class ProjectActivity:

    # ── Projects ──────────────────────────────────────────────────────────

    @staticmethod
    def project_before(db: Session, project) -> Optional[Dict[str, Any]]:
        """The project as it is now, as plain values, ahead of an edit."""
        return ActivityLogService.snapshot(db, lambda: {
            "name": project.project_name,
            "description": project.description,
            "status_id": project.status_id,
            "leader_id": project.leader_id,
            "owner_id": project.owner_id,
            "deadline": project.deadline,
            "billing_type": project.billing_type,
            "category": project.category,
            "fixed_hours": project.fixed_hours,
            "member_ids": set(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == project.id)).all()),
        })

    @staticmethod
    def project_update_rows(db: Session, actor: User, project, before: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """One row per thing that changed, comparing ``project`` (saved) with
        ``before``. Status, leader, owner and team each get their own action so
        they can be told apart without reading the sentence."""
        label = f'"{project.project_name}"'
        about = {"project_id": project.id, "entity_id": project.id}
        if before is None:
            return [_row(actor, ActivityLogModule.PROJECT, ActivityLogAction.PROJECT_UPDATED, f"Updated the project {label}", **about)]

        rows: List[Dict[str, Any]] = []
        if project.status_id != before["status_id"]:
            rows.append(_row(
                actor, ActivityLogModule.PROJECT, ActivityLogAction.PROJECT_STATUS_CHANGED,
                f"Changed the status of the project {label} from {_status_name(db, 'project', before['status_id'])} "
                f"to {_status_name(db, 'project', project.status_id)}", **about,
            ))

        after_members: Set[int] = set(db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == project.id)).all())
        added, removed = after_members - before["member_ids"], before["member_ids"] - after_members
        people = _people(db, {before["leader_id"], project.leader_id, before["owner_id"], project.owner_id} | added | removed)
        for field, action, noun in (
            ("leader_id", ActivityLogAction.PROJECT_LEADER_CHANGED, "leader"),
            ("owner_id", ActivityLogAction.PROJECT_OWNER_CHANGED, "owner"),
        ):
            if getattr(project, field) != before[field]:
                rows.append(_row(
                    actor, ActivityLogModule.PROJECT, action,
                    f"Changed the {noun} of the project {label} from {_person(people, before[field])} "
                    f"to {_person(people, getattr(project, field))}", **about,
                ))
        rows.extend(ProjectActivity._team_rows(actor, project.id, project.project_name, people, added, removed))

        labels = PROJECT_FIELD_LABELS
        changes: List[str] = []
        if project.project_name != before["name"]:
            changes.append(f'{labels["project_name"]}: "{before["name"]}" → "{project.project_name}"')
        if (project.description or "") != (before["description"] or ""):
            changes.append(labels["description"])
        if project.deadline != before["deadline"]:
            changes.append(f"{labels['deadline']}: {_show(before['deadline'])} → {_show(project.deadline)}")
        if project.billing_type != before["billing_type"]:
            changes.append(
                f"{labels['billing_type']}: {_BILLING.get(before['billing_type'], before['billing_type'])} "
                f"→ {_BILLING.get(project.billing_type, project.billing_type)}"
            )
        if project.category != before["category"]:
            changes.append(
                f"{labels['category']}: {_CATEGORY.get(before['category'], _show(before['category']))} "
                f"→ {_CATEGORY.get(project.category, _show(project.category))}"
            )
        if project.fixed_hours != before["fixed_hours"]:
            changes.append(f"{labels['fixed_hours']}: {_show(before['fixed_hours'])} → {_show(project.fixed_hours)}")
        if changes:
            rows.append(_row(actor, ActivityLogModule.PROJECT, ActivityLogAction.PROJECT_UPDATED,
                             f"Updated the project {label} ({', '.join(changes)})", **about))
        return rows

    @staticmethod
    def project_team_rows(
        db: Session, actor: User, project_id: int, project_name: str, *,
        added_ids: Iterable[int] = (), removed_ids: Iterable[int] = (),
    ) -> List[Dict[str, Any]]:
        """Members put on, and taken off, a project -- by name."""
        added, removed = set(added_ids), set(removed_ids)
        return ProjectActivity._team_rows(actor, project_id, project_name, _people(db, added | removed), added, removed)

    @staticmethod
    def _team_rows(actor: User, project_id: int, project_name: str, people: Dict[int, str], added: Set[int], removed: Set[int]) -> List[Dict[str, Any]]:
        about = {"project_id": project_id, "entity_id": project_id}
        rows: List[Dict[str, Any]] = []
        if added:
            rows.append(_row(actor, ActivityLogModule.PROJECT, ActivityLogAction.PROJECT_MEMBER_ASSIGNED,
                             f'Assigned {_names(people, added)} to the project "{project_name}"', **about))
        if removed:
            rows.append(_row(actor, ActivityLogModule.PROJECT, ActivityLogAction.PROJECT_MEMBER_REMOVED,
                             f'Removed {_names(people, removed)} from the project "{project_name}"', **about))
        return rows

    # ── Tasks ─────────────────────────────────────────────────────────────

    @staticmethod
    def task_before(db: Session, task) -> Optional[Dict[str, Any]]:
        """The task as it is now, as plain values, ahead of an edit."""
        return ActivityLogService.snapshot(db, lambda: {
            "name": task.task_name,
            "description": task.description,
            "estimated_hours": task.estimated_hours,
            "status_id": task.status_id,
            "holder_ids": ProjectActivity._holders(db, task),
        })

    @staticmethod
    def _holders(db: Session, task) -> Set[int]:
        """Everyone holding a task: the many-member rows and the primary
        `assignee_id`, which the two representations keep in step."""
        held = set(db.scalars(select(TaskAssignee.user_id).where(TaskAssignee.task_id == task.id)).all())
        if task.assignee_id:
            held.add(task.assignee_id)
        return held

    @staticmethod
    def task_update_rows(db: Session, actor: User, task, before: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """One row per thing that changed on ``task`` since ``before``."""
        label = f'"{task.task_name}"'
        about = {"project_id": task.project_id, "task_id": task.id, "entity_id": task.id}
        if before is None:
            return [_row(actor, ActivityLogModule.TASK, ActivityLogAction.TASK_UPDATED, f"Updated the task {label}", **about)]

        rows: List[Dict[str, Any]] = []
        if task.status_id != before["status_id"]:
            rows.append(_row(
                actor, ActivityLogModule.TASK, ActivityLogAction.TASK_STATUS_CHANGED,
                f"Changed the status of the task {label} from {_status_name(db, 'task', before['status_id'])} "
                f"to {_status_name(db, 'task', task.status_id)}", **about,
            ))
        after = ProjectActivity._holders(db, task)
        added, removed = after - before["holder_ids"], before["holder_ids"] - after
        rows.extend(ProjectActivity.task_holder_rows(db, actor, task, added_ids=added, removed_ids=removed))

        labels = TASK_FIELD_LABELS
        changes: List[str] = []
        if task.task_name != before["name"]:
            changes.append(f'{labels["name"]}: "{before["name"]}" → "{task.task_name}"')
        if (task.description or "") != (before["description"] or ""):
            changes.append(labels["description"])
        if task.estimated_hours != before["estimated_hours"]:
            changes.append(f"{labels['estimated_hours']}: {_show(before['estimated_hours'])} → {_show(task.estimated_hours)}")
        if changes:
            rows.append(_row(actor, ActivityLogModule.TASK, ActivityLogAction.TASK_UPDATED,
                             f"Updated the task {label} ({', '.join(changes)})", **about))
        return rows

    @staticmethod
    def task_holder_rows(
        db: Session, actor: User, task, *, added_ids: Iterable[int] = (), removed_ids: Iterable[int] = (),
    ) -> List[Dict[str, Any]]:
        """Members given a task, and taken off it -- by name."""
        added, removed = set(added_ids), set(removed_ids)
        if not added and not removed:
            return []
        people = _people(db, added | removed)
        about = {"project_id": task.project_id, "task_id": task.id, "entity_id": task.id}
        rows: List[Dict[str, Any]] = []
        if added:
            rows.append(_row(actor, ActivityLogModule.TASK, ActivityLogAction.TASK_ASSIGNED,
                             f'Assigned the task "{task.task_name}" to {_names(people, added)}', **about))
        if removed:
            rows.append(_row(actor, ActivityLogModule.TASK, ActivityLogAction.TASK_UNASSIGNED,
                             f'Removed {_names(people, removed)} from the task "{task.task_name}"', **about))
        return rows
