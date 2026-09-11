"""Which tasks a caller is allowed to *see*.

The third member of the family with ``member_scope`` and ``project_scope``.
Those answer "at which people?" and "at which projects?"; this one answers "at
which **tasks inside** a project?", and it exists because the answer was
previously "all of them" on every route that returned a task.

``project_scope`` decides whether a caller may open a project at all. Passing
that check used to be the *whole* test: every task-returning query then filtered
on ``project_id`` alone, so two employees staffed onto the same project each saw
the other's tasks. One employee creating a task in the desktop client made it
appear in every other project member's client. That is the data-isolation defect
this module closes.

Two scopes, and the split is the product's own:

* **Task managers** -- admin, org_admin, super_admin, manager, HR and both
  leader spellings -- see every task in a project they may open. That is not an
  oversight to be tidied away: an admin assigns tasks, a leader runs the
  project, HR books manual time against other people's tasks, and the Task
  Listing and project-management screens are built on it. ``project_scope``
  has already refused a project outside their authority, so the tasks inside
  one they *can* open are theirs to manage.

* **Everyone else** -- an employee, and any role not named above -- sees their
  own work:

      a task they created,
      a task assigned to them,
      or a task assigned to nobody.

The third clause is not a loophole, it is the shared work. Every project is
created with ``DEFAULT_PROJECT_TASKS`` ("Project Setup / Understanding",
"Review Client Update", "Send Client Update", "Internal Discussion") owned by
nobody, and those are what the team tracks time against. Dropping them would
leave every employee looking at an empty project -- the same failure the leader
scope fix had to undo -- while fixing nothing, because a task an employee
creates is never unassigned: the desktop names the creator as assignee and
``create_task`` pins it server-side regardless of what the client sends.

Assignment is read from **both** representations deliberately. ``task_assignees``
is the real relationship, and ``tasks.assignee_id`` is the older single-assignee
column; the two creation paths write different subsets of them, so a rule that
consulted only one would call an assigned task unassigned and share it with the
project.

The unrestricted case returns ``None`` rather than a tautology, exactly as
``visible_project_ids`` does, so a manager's query is not burdened with a
filter that can never exclude anything.
"""

from typing import Optional

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.permissions import with_role_aliases
from app.models.task import Task
from app.models.task_assignee import TaskAssignee
from app.models.user import User

#: Roles whose task visibility is the project rather than their own work.
#:
#: Named explicitly, and everything absent from this set gets the narrow scope.
#: The list widens visibility, so an unrecognised or newly added role must fall
#: on the private side of it: a role nobody has thought about yet is not a role
#: that should silently inherit the whole project's tasks.
#:
#: Wrapped in ``with_role_aliases`` for the reason that helper exists:
#: ``users.role_name`` does not always hold the canonical name. WordPress sends
#: ``administrator``, and rows provisioned before the alias table existed still
#: carry it -- this deployment's own admin account is one. Matching the literal
#: spellings only would have quietly demoted a real administrator to their own
#: tasks, which is a failure that looks exactly like the bug being fixed.
TASK_MANAGER_ROLES = frozenset(with_role_aliases({
    "admin", "org_admin", "super_admin", "manager", "hr", "leader", "project_leader",
}))


def is_task_scoped(user: User) -> bool:
    """Whether this caller sees only their own tasks."""
    return getattr(user, "role_name", None) not in TASK_MANAGER_ROLES


def visible_task_condition(user: User) -> Optional[ColumnElement[bool]]:
    """A WHERE clause limiting ``Task`` rows to what ``user`` may see.

    ``None`` means "no restriction" -- every task in a project the caller may
    already open. Callers must treat ``None`` as "add no filter" rather than as
    "add nothing visible"; getting that backwards is how a scope becomes either
    a no-op or a blackout.

    The clause is a single expression evaluated by the database. Nothing is
    fetched and filtered in Python, so a project with ten thousand tasks costs
    the same one query as a project with ten, and the rows that reach the
    application are already the authorised ones.
    """
    if not is_task_scoped(user):
        return None
    assigned_to_me = select(TaskAssignee.task_id).where(TaskAssignee.user_id == user.id)
    assigned_to_anyone = select(TaskAssignee.task_id)
    return or_(
        Task.created_by == user.id,
        Task.assignee_id == user.id,
        Task.id.in_(assigned_to_me),
        # Shared project work: owned by nobody in either representation.
        and_(Task.assignee_id.is_(None), Task.id.notin_(assigned_to_anyone)),
    )


def scoped_task_query(query: Select, user: User) -> Select:
    """Apply `visible_task_condition` to an existing ``select(Task)``."""
    condition = visible_task_condition(user)
    return query if condition is None else query.where(condition)


def may_view_task(db: Optional[Session], user: User, task: Task) -> bool:
    """Whether ``user`` may read one already-loaded task.

    The single-row counterpart to `visible_task_condition`, for the detail,
    update and delete paths that fetch a task by id. One query at most, and
    only when the cheap column checks have not already settled it.

    With no session to ask, a scoped caller is allowed only what the task's own
    columns prove. Like the rest of this family the fallback narrows and never
    widens.
    """
    if not is_task_scoped(user):
        return True
    if task.created_by == user.id or task.assignee_id == user.id:
        return True
    if db is None:
        return task.assignee_id is None
    assignee_ids = set(db.scalars(
        select(TaskAssignee.user_id).where(TaskAssignee.task_id == task.id)
    ).all())
    if user.id in assignee_ids:
        return True
    # Unassigned in both representations: shared project work.
    return not assignee_ids and task.assignee_id is None
