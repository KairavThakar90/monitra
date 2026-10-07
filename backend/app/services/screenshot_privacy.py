"""Applying a new screenshot-privacy rule to members when it is created.

The privacy catalogue (`screenshot_applications`, `screenshot_urls`) is shared,
and a rule only takes effect for a member through a per-member
`ScreenshotExclusion` row; the desktop reads those rows from
`GET /screenshot/privacy-config`. Until now an administrator who added a rule
had to open each member and switch it on one by one. This is the part that does
it in the same save: *all members*, or the members chosen.

Three properties are the point of having it here rather than in the route:

* **It is an administrator's act.** Excluding other people from screenshots is
  a monitoring decision, so choosing who a rule applies to is refused for every
  other role -- checked before anything is written.
* **It is one transaction.** The rule and its exclusions are written together
  by the caller, and every refusal here happens *before* the first write, so a
  bad member list can never leave a rule created and half applied.
* **"All members" means the people who run the desktop app.** Active members of
  the caller's own organization; never a client account (a client sees reports,
  it does not capture) or the release pipeline's service account.
"""
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import resolve_role_alias
from app.models.screenshot_exclusion import ScreenshotExclusion
from app.models.user import User
from app.schemas.screenshot_privacy import ScreenshotRuleScope

#: Who may choose which members a rule applies to. The same three spellings of
#: administrator the rest of the admin settings use, matched on role name for
#: the reason `FEEDBACK_MANAGE_ROLES` is: a permission added to the role table
#: would only reach an existing administrator after they next sign in.
PRIVACY_MANAGE_ROLES = frozenset({"administrator", "org_admin", "super_admin"})

#: Accounts that never capture screenshots, so a rule is never applied to them.
NOT_CAPTURED_ROLES = frozenset({"client", "release_bot"})


class ScreenshotPrivacyService:
    @staticmethod
    def _require_manage(current_user: User) -> int:
        """The administrator gate, returning the organization they act in."""
        role = resolve_role_alias((current_user.role_name or "").strip().lower())
        if getattr(current_user, "is_service_principal", False) or role not in PRIVACY_MANAGE_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action",
            )
        if not current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account is not associated with an organization.",
            )
        return current_user.organization_id

    @staticmethod
    def members_for(
        db: Session, current_user: User, scope: Optional[ScreenshotRuleScope]
    ) -> list[int]:
        """The member ids a new rule is to be switched on for.

        ``None`` (no `apply_to`) means nobody, and needs no permission -- that
        is the unchanged "add to the catalogue" request. Anything else is the
        administrator's act, and every failure is raised here, before the caller
        writes a thing.
        """
        if scope is None:
            return []
        organization_id = ScreenshotPrivacyService._require_manage(current_user)

        people = select(User.id).where(
            User.organization_id == organization_id,
            User.role_name.not_in(NOT_CAPTURED_ROLES),
        )
        if scope.scope == "all":
            return list(
                db.scalars(
                    people.where(User.status == "active", User.is_active.is_(True)).order_by(User.id)
                ).all()
            )

        requested = list(dict.fromkeys(scope.user_ids or []))
        found = set(db.scalars(people.where(User.id.in_(requested))).all())
        missing = [member_id for member_id in requested if member_id not in found]
        if missing:
            # One answer for "no such member", "another organization's member" and
            # "not someone who captures", so it cannot be used to probe for ids.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"These members could not be found: {missing}.",
            )
        return [member_id for member_id in requested if member_id in found]

    @staticmethod
    def exclude_members(
        db: Session,
        user_ids: list[int],
        *,
        application_id: Optional[int] = None,
        url_id: Optional[int] = None,
    ) -> int:
        """Switch one rule on for each of these members. Flushes; never commits.

        The same upsert the per-member toggle performs, in bulk: a member who
        already has a row for this rule (including one switched *off*) has it
        switched on, and everyone else gets a new one, so applying a rule twice
        never doubles anything. Returns how many members the rule is now on for.
        """
        if not user_ids:
            return 0
        if (application_id is None) == (url_id is None):
            raise ValueError("Exactly one of application_id and url_id is required.")
        kind = "application" if application_id is not None else "url"
        column = ScreenshotExclusion.application_id if application_id is not None else ScreenshotExclusion.url_id
        reference = application_id if application_id is not None else url_id

        existing: dict[int, list[ScreenshotExclusion]] = {}
        for row in db.scalars(
            select(ScreenshotExclusion).where(
                ScreenshotExclusion.user_id.in_(user_ids),
                ScreenshotExclusion.exclusion_type == kind,
                column == reference,
            )
        ).all():
            existing.setdefault(row.user_id, []).append(row)

        for user_id in user_ids:
            rows = existing.get(user_id)
            if rows:
                for row in rows:
                    row.is_excluded = True
            else:
                db.add(
                    ScreenshotExclusion(
                        user_id=user_id,
                        application_id=application_id,
                        url_id=url_id,
                        exclusion_type=kind,
                        is_excluded=True,
                    )
                )
        db.flush()
        return len(user_ids)
