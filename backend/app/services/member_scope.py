"""Which people a caller is allowed to *see*.

``time_entries:view_all`` and ``view_employees`` answer "may this caller look
past themselves?" — they do not answer "at whom?". For an admin, an org_admin,
a manager or HR the answer is the whole organization. For a **leader** it is
their own team: the people an admin put on the projects that leader leads,
plus the leader themselves.

One helper, used by every read surface (member directory, member details,
dashboard, reports, time tracking, manual time entry listings), so a leader's
scope cannot be right on one screen and wrong on the next.

Two things this deliberately does *not* touch:

* **Assignable-member pickers.** When a leader creates a project they choose
  freely from the whole organization — narrowing the picker would make it
  impossible to build a team. Scoping applies to reading other people's
  recorded work, not to staffing.
* **Anyone without ``time_entries:view_all`` / ``view_employees``.** They are
  already pinned to themselves by the caller-side checks that were there
  before; this helper only narrows the "sees other people" case.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.client_project import ClientProject
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User

#: The role names whose visibility is their own team rather than the whole
#: organization. Both spellings of the leader role are covered, matching
#: ``ProjectMemberService.LEADER_ROLES``.
TEAM_SCOPED_ROLES = frozenset({"leader", "project_leader"})


def is_team_scoped(user: User) -> bool:
    """Whether this caller sees a team rather than the organization."""
    return getattr(user, "role_name", None) in TEAM_SCOPED_ROLES


def visible_member_ids(db: Optional[Session], user: User) -> Optional[set[int]]:
    """The member ids this caller may read, or ``None`` for "no restriction".

    ``None`` means the organization, and is what every non-leader role gets —
    callers can then skip the filter entirely rather than building an ``IN``
    list of the whole company.

    With no session to ask (``db is None``) a team-scoped caller falls back to
    just themselves. The fallback narrows and never widens: a missing session
    must not silently turn a leader into an org-wide reader.
    """
    if not is_team_scoped(user):
        return None
    if db is None:
        return {user.id}

    led_projects = select(Project.id).where(
        Project.leader_id == user.id,
        Project.organization_id == user.organization_id,
    )
    member_ids = set(
        db.scalars(
            select(ProjectMember.user_id).where(
                ProjectMember.project_id.in_(led_projects),
                ProjectMember.organization_id == user.organization_id,
            )
        ).all()
    )
    # A leader is always part of their own team, so their own dashboard and
    # their own time are never empty even before a project is assigned.
    member_ids.add(user.id)
    return member_ids


def may_view_member(db: Optional[Session], user: User, member_id: int) -> bool:
    """Whether ``user`` may read the person with id ``member_id``."""
    allowed = visible_member_ids(db, user)
    return allowed is None or member_id in allowed


def visible_directory_ids(db: Optional[Session], user: User) -> Optional[set[int]]:
    """Who appears in this caller's *member directory*, or ``None`` for everyone.

    The directory is wider than the team for one reason: a leader also sees
    their **clients** -- the client accounts an administrator shared one of
    the leader's own projects with. The link is the administrator's act
    (``client_projects``, written from the Clients screen) joined to the
    project's ``leader_id``; a leader cannot add a client to their directory,
    and a client shared only on somebody else's project is not in it.

    This is deliberately a separate set from ``visible_member_ids``. That one
    answers "whose recorded work may I read" -- dashboard, reports, time,
    screenshots, logs -- and a client has no recorded work and is not part of
    the team. Widening it would put clients in every one of those surfaces;
    this widens the roster and nothing else.
    """
    allowed = visible_member_ids(db, user)
    if allowed is None or db is None:
        return allowed

    led_projects = select(Project.id).where(
        Project.leader_id == user.id,
        Project.organization_id == user.organization_id,
    )
    client_user_ids = db.scalars(
        select(Client.user_id)
        .join(ClientProject, ClientProject.client_id == Client.id)
        .where(
            ClientProject.project_id.in_(led_projects),
            Client.organization_id == user.organization_id,
            # An invitation whose account was never created has no row in the
            # directory to show.
            Client.user_id.is_not(None),
        )
    ).all()
    return allowed | {user_id for user_id in client_user_ids if user_id is not None}


def may_view_in_directory(db: Optional[Session], user: User, member_id: int) -> bool:
    """Whether ``member_id`` is in ``user``'s member directory."""
    allowed = visible_directory_ids(db, user)
    return allowed is None or member_id in allowed
