"""One cheap answer to "has anything I can see changed since I last looked?"

The desktop client renders projects, the selected project's tasks and the
day's time entries, and it has to notice when any of those change on the
server -- a project created on the web, a task reassigned, a membership
removed. Re-downloading all of it on a timer is the obvious way to find out
and the expensive one: at fleet scale it is the bulk of the backend's read
load, almost all of it returning what the client already has.

This module computes a *fingerprint* of the caller's visible state instead.
Each component is one aggregate query -- ``COUNT``, ``MAX(updated_at)``,
``MAX(id)`` -- under exactly the scope the corresponding list endpoint
applies, so it moves when and only when that endpoint's answer would. The
three numbers together catch every kind of change:

* an insert raises the count and ``MAX(id)``;
* an update raises ``MAX(updated_at)`` (every mutable table here has
  ``onupdate=func.now()``);
* a delete, or an archive that the scope filters out, lowers the count -- and
  a delete followed by an insert in the same instant, which leaves the count
  alone, still raises ``MAX(id)``.

Membership rows carry no ``updated_at`` and are never updated in place (a
membership change is a delete and an insert), so ``created_at`` stands in for
it there; assignments likewise use ``assigned_at``.

The fingerprint is opaque to the client. It compares it with the last value
it saw and re-reads the real endpoints only when the two differ; it never
tries to interpret it. ``components`` is returned alongside so that a log
line can say *which* part moved.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.task import Task
from app.models.task_assignee import TaskAssignee
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.services.project_scope import visible_project_ids
from app.services.task_scope import visible_task_condition


def _visible_project_filters(db: Session, user: User) -> list:
    """The same scope `ProjectManagementService.list` applies, stated once.

    Kept identical on purpose: the fingerprint must move exactly when the
    project list would, so it has to be computed over exactly the same rows.
    """
    filters = [Project.organization_id == user.organization_id, Project.status != "archived"]
    if user.role_name == "employee":
        filters.append(Project.id.in_(
            select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
        ))
    else:
        allowed = visible_project_ids(db, user)
        if allowed is not None:
            filters.append(Project.id.in_(allowed))
    return filters


def _fingerprint(db: Session, statement: Select) -> str:
    """`count:max_timestamp:max_id` for one aggregate statement."""
    count, latest, max_id = db.execute(statement).one()
    stamp = latest.isoformat() if isinstance(latest, datetime) else (str(latest) if latest else "")
    return f"{int(count or 0)}:{stamp}:{int(max_id or 0)}"


def scope_components(db: Session, user: User) -> Dict[str, str]:
    """Every component of the caller's fingerprint, by name."""
    project_filters = _visible_project_filters(db, user)
    visible_projects = select(Project.id).where(*project_filters)

    task_filters = [Task.project_id.in_(visible_projects), Task.status != "archived"]
    task_condition = visible_task_condition(user)
    if task_condition is not None:
        task_filters.append(task_condition)

    return {
        "projects": _fingerprint(db, select(
            func.count(Project.id), func.max(Project.updated_at), func.max(Project.id),
        ).where(*project_filters)),
        "memberships": _fingerprint(db, select(
            func.count(ProjectMember.id), func.max(ProjectMember.created_at), func.max(ProjectMember.id),
        ).where(ProjectMember.project_id.in_(visible_projects))),
        "tasks": _fingerprint(db, select(
            func.count(Task.id), func.max(Task.updated_at), func.max(Task.id),
        ).where(*task_filters)),
        "assignments": _fingerprint(db, select(
            func.count(TaskAssignee.id), func.max(TaskAssignee.assigned_at), func.max(TaskAssignee.id),
        ).where(TaskAssignee.task_id.in_(
            select(Task.id).where(Task.project_id.in_(visible_projects))
        ))),
        # The caller's own entries only: the desktop shows one person's day.
        "time_entries": _fingerprint(db, select(
            func.count(TimeEntry.id), func.max(TimeEntry.updated_at), func.max(TimeEntry.id),
        ).where(TimeEntry.user_id == user.id)),
    }


def revision_of(components: Dict[str, str]) -> str:
    """The opaque token a client compares. Stable for equal components."""
    digest = hashlib.sha1(json.dumps(components, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def scope_revision(db: Session, user: User) -> Dict[str, Any]:
    components = scope_components(db, user)
    return {
        "revision": revision_of(components),
        "components": components,
        "server_time": datetime.now(timezone.utc),
    }
