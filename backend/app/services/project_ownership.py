"""Who may own a project, stated once.

A project's Owner (`projects.owner_id`) is a relationship between one member
and one project. It is *not* a role: the owner keeps whatever global role they
have, and being an owner grants nothing by itself -- every project permission
is still decided by `ROLE_PERMISSIONS` and the project scope helpers. Should
owners ever need rights over their own projects, that check belongs beside
`may_view_project` in `project_scope.py`, keyed on `Project.owner_id`, so it
never has to touch the role.

Eligibility is the per-member capability `users.can_own_projects`, granted
by an administrator. The Owner picker (`GET /api/v1/projects/assignable-owners`)
and the create/update validation both read `eligible_owner_condition`, so the
picker can never offer somebody the server then refuses, and a hand-built
request naming anybody else is refused whatever the client showed.
"""
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.validation import LIKE_ESCAPE_CHARACTER, like_pattern
from app.models.user import User


def eligible_owner_condition(organization_id: int):
    """The SQL condition a user must satisfy to be chosen as a project owner."""
    return and_(
        User.organization_id == organization_id,
        User.is_active.is_(True),
        User.can_own_projects.is_(True),
    )


def assignable_owners(db: Session, user: User, search: Optional[str] = None) -> list[User]:
    """Every member of the caller's organization who may own a project."""
    query = select(User).where(eligible_owner_condition(user.organization_id)).order_by(User.name)
    if search:
        query = query.where(User.name.ilike(like_pattern(search), escape=LIKE_ESCAPE_CHARACTER))
    return list(db.scalars(query).all())


def resolve_owner(db: Session, user: User, owner_id: int) -> User:
    """The user `owner_id` names, provided they may own a project here.

    Two refusals, kept distinct so the message says what is actually wrong: an
    id that is not a member of this organization at all, and a real member who
    is not eligible (never granted the capability, or since deactivated).
    Both are 400s, matching how an invalid leader is refused.
    """
    owner = db.scalar(select(User).where(User.id == owner_id, User.organization_id == user.organization_id))
    if owner is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected owner does not belong to this organization.")
    if not owner.is_active or owner.can_own_projects is not True:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selected owner is not eligible to own projects.")
    return owner
