import logging
import math
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import resolve_role_alias
from app.models.feedback_request import FeedbackRequest
from app.models.user import User
from app.repositories.feedback import FeedbackRepository, FeedbackRow
from app.schemas.feedback import FeedbackCreate, FeedbackStatus, FeedbackStatusAction
from app.services.email import (
    deliver_in_background, queue_feedback_notification,
    queue_feedback_status_notification,
)

logger = logging.getLogger("uvicorn.error")

#: The roles that may read the whole organization's feedback. Admin, HR and
#: Leader are the three the product asks for; `org_admin`/`super_admin` and
#: `project_leader` are the other spellings ROLE_PERMISSIONS already defines for
#: the same authority, so leaving them out would deny a user whose account
#: happens to carry the alternate name. `manager` and `employee` are not here:
#: a manager was never given directory-wide feedback visibility.
FEEDBACK_VIEW_ALL_ROLES = frozenset(
    {"administrator", "org_admin", "super_admin", "hr", "leader", "project_leader"}
)

#: The roles that may move feedback through the workflow and thereby email the
#: person who submitted it. **Administrators only.**
#:
#: This is deliberately a strict subset of FEEDBACK_VIEW_ALL_ROLES above, and
#: the gap between the two sets is the whole permission model for this feature:
#: HR and Leader read every submission and change none of them. Reading the
#: organisation's feedback is oversight; sending mail to a colleague in the
#: product's name is a different kind of authority, and the product gives it to
#: Admin alone.
#:
#: Why a role set here rather than a new entry in ROLE_PERMISSIONS: the
#: `permissions` column is re-derived from that table at every sign-in, so a
#: permission added there reaches an existing administrator only after they
#: next log in — until then every click would come back 403 with nothing in the
#: code to explain it. `role_name` needs no such migration. It is also what the
#: sibling gate in this very service already matches on, and one module
#: answering "who may do this?" two different ways is how the two answers start
#: to disagree.
#:
#: `manager` is absent for the same reason it is absent above, and so is
#: `release_bot`: no service credential has any business writing to staff.
FEEDBACK_MANAGE_ROLES = frozenset({"administrator", "org_admin", "super_admin"})

#: Which statuses a piece of feedback may move to, from where.
#:
#: The lifecycle is new → in_progress → resolved, with two deliberate
#: decisions written into this table:
#:
#: * **new → resolved is allowed.** Not every submission needs a "we're working
#:   on it" first: a question answered in a two-minute conversation is resolved,
#:   and forcing the administrator through Working would send the employee an
#:   email promising future work that had in fact already been done.
#: * **resolved → in_progress is not.** Reopening is a workflow this product
#:   does not have — no screen offers it, nothing reads a reopened state — and
#:   inventing one here would mean inventing the email that goes with it and
#:   the "resolved twice" case underneath it. An administrator who needs to
#:   reopen something today asks the employee to submit it again, which is the
#:   existing answer, not a worse one.
#:
#: A status absent from this mapping as a *source* can be left by nothing;
#: `reviewing` and `closed` are such states. Neither is reachable — nothing in
#: the product sets them — so they are listed nowhere rather than given
#: speculative transitions.
ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    FeedbackStatus.new.value: frozenset(
        {FeedbackStatus.in_progress.value, FeedbackStatus.resolved.value}
    ),
    FeedbackStatus.in_progress.value: frozenset({FeedbackStatus.resolved.value}),
    FeedbackStatus.resolved.value: frozenset(),
}


class FeedbackService:
    """Business rules for Feedback & Help submissions.

    The one rule that matters: identity and tenancy come from the authenticated
    user, never from the request body, and a new submission is always ``new``.
    """

    @staticmethod
    def submit_feedback(
        db: Session,
        feedback_in: FeedbackCreate,
        current_user: User,
        background_tasks=None,
    ) -> FeedbackRequest:
        """Persist one submission, then notify Admin and HR about it.

        The order is the contract. The feedback row is committed first and on
        its own; queueing the notification happens afterwards and cannot
        influence it. `queue_feedback_notification` does not raise — it logs and
        returns None — so there is no path from "the mail server is down" or
        "nobody is configured to notify" to a person being told their feedback
        failed. It was saved; that is what the response reports.

        `background_tasks` is the fast path and nothing more. When the route
        supplies it, one delivery attempt runs after the response has been
        written, so the person submitting waits for the database and not for
        SMTP. When it is absent, or the attempt fails, or the platform freezes
        the invocation before the task runs, the row is still queued and the
        dispatch sweeper delivers it.
        """
        # The schema already trims and rejects a blank message; this guards the
        # service against a caller that builds the model some other way.
        message = feedback_in.message.strip()
        if not message:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Message must not be empty.",
            )

        if not current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account is not associated with an organization.",
            )

        feedback = FeedbackRepository.create(
            db=db,
            organization_id=current_user.organization_id,
            user_id=current_user.id,
            category=feedback_in.category.value,
            message=message,
            status=FeedbackStatus.new.value,
        )

        notification_id = queue_feedback_notification(db, feedback, current_user)
        if notification_id is not None and background_tasks is not None:
            background_tasks.add_task(deliver_in_background, notification_id)

        return feedback

    # ------------------------------------------------------------------
    # Read paths. Every one of them is view-only; the single write path on an
    # existing row is `update_status` at the bottom of this class, and it is
    # Admin-only.
    # ------------------------------------------------------------------

    @staticmethod
    def _organization_id(current_user: User) -> int:
        if not current_user.organization_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account is not associated with an organization.",
            )
        return current_user.organization_id

    @staticmethod
    def _require_view_all(current_user: User) -> int:
        """Admin / HR / Leader gate, returning the tenant they may read."""
        role = resolve_role_alias((current_user.role_name or "").strip().lower())
        if role not in FEEDBACK_VIEW_ALL_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action",
            )
        return FeedbackService._organization_id(current_user)

    @staticmethod
    def _require_manage(current_user: User) -> int:
        """Admin-only gate for the status workflow, returning the tenant.

        Separate from `_require_view_all` rather than a parameter on it. These
        are two different questions — may you read this, and may you act on it
        — and an HR account answers yes to the first and no to the second; a
        single gate with a flag is one edit away from answering both the same
        way.

        A service principal is refused here like any other non-administrator:
        its `role_name` is not in the set. Nothing that authenticates with an
        API key should be able to send mail to a member of staff.
        """
        role = resolve_role_alias((current_user.role_name or "").strip().lower())
        if role not in FEEDBACK_MANAGE_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action",
            )
        return FeedbackService._organization_id(current_user)

    @staticmethod
    def _item(row: FeedbackRow) -> dict:
        feedback, employee_id, employee_name = row
        return {
            "id": feedback.id,
            "employee_id": employee_id,
            "employee_name": employee_name,
            "category": feedback.category,
            "message": feedback.message,
            "status": feedback.status,
            "created_at": feedback.created_at,
            "updated_at": feedback.updated_at,
        }

    @staticmethod
    def _envelope(rows, total: int, page: int, limit: int) -> dict:
        return {
            "items": [FeedbackService._item(row) for row in rows],
            "page": page,
            "limit": limit,
            "total": total,
            "pages": math.ceil(total / limit) if total else 0,
        }

    @staticmethod
    def list_my_feedback(
        db: Session, current_user: User, page: int = 1, limit: int = 20
    ) -> dict:
        """Everything the caller submitted. The owner is the token's user, never a parameter."""
        rows, total = FeedbackRepository.list_for_user(
            db, user_id=current_user.id, page=page, limit=limit
        )
        return FeedbackService._envelope(rows, total, page, limit)

    @staticmethod
    def get_my_feedback(db: Session, current_user: User, feedback_id: int) -> dict:
        row = FeedbackRepository.get_for_user(
            db, feedback_id=feedback_id, user_id=current_user.id
        )
        if row is None:
            # Someone else's id and a non-existent id are answered identically,
            # so the endpoint cannot be used to probe for other users' rows.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found."
            )
        return FeedbackService._item(row)

    @staticmethod
    def list_all_feedback(
        db: Session,
        current_user: User,
        page: int = 1,
        limit: int = 20,
        category: Optional[str] = None,
    ) -> dict:
        organization_id = FeedbackService._require_view_all(current_user)
        rows, total = FeedbackRepository.list_for_organization_with_user(
            db,
            organization_id=organization_id,
            page=page,
            limit=limit,
            category=category,
        )
        return FeedbackService._envelope(rows, total, page, limit)

    @staticmethod
    def get_feedback(db: Session, current_user: User, feedback_id: int) -> dict:
        organization_id = FeedbackService._require_view_all(current_user)
        row = FeedbackRepository.get_for_organization(
            db, feedback_id=feedback_id, organization_id=organization_id
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found."
            )
        return FeedbackService._item(row)

    # ------------------------------------------------------------------
    # The Admin status workflow — the one write path on this resource.
    # ------------------------------------------------------------------

    @staticmethod
    def update_status(
        db: Session,
        current_user: User,
        feedback_id: int,
        new_status: FeedbackStatusAction,
        background_tasks=None,
    ) -> dict:
        """Move one feedback to Working or Resolved and tell the submitter.

        The order of the seven steps below is the contract, and it is the same
        order — and for the same reason — as `submit_feedback`: **the status
        change is committed first and on its own, and the email follows from
        it.** `queue_feedback_status_notification` does not raise, so a mail
        server that is down produces a log line and a feedback row in the state
        the administrator asked for. The alternative, failing the request when
        mail cannot be queued, would leave the button looking broken while the
        thing it was pressed to do had actually worked.

        Two properties are worth stating plainly, because they are what the
        endpoint is trusted for:

        * **The recipient is never supplied by the caller.** It is the `User`
          joined to this feedback row by `user_id`, loaded inside the tenant
          scope. The request body carries a status and nothing else.
        * **Pressing the same button twice sends one email.** The status is
          compared before anything is written and an unchanged status returns
          early; underneath that, the outbox's unique
          `(feedback_status, feedback:<id>:<status>)` makes a duplicate
          impossible even for two concurrent requests that both pass the check.
        """
        organization_id = FeedbackService._require_manage(current_user)

        loaded = FeedbackRepository.get_with_submitter_for_organization(
            db, feedback_id=feedback_id, organization_id=organization_id
        )
        if loaded is None:
            # Another organization's feedback and a non-existent id are answered
            # identically, so this cannot be used to discover that a row exists.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found."
            )
        feedback, submitter = loaded

        current_status = (feedback.status or FeedbackStatus.new.value).strip()
        target = new_status.value

        if current_status == target:
            # A double-click, a refresh of a page that re-posted, or a retried
            # request. Nothing is written and nothing is queued: the row is
            # already where the administrator wants it, and this is the first
            # of the two defences against a second email.
            logger.info(
                "FEEDBACK_STATUS_UNCHANGED: feedback=%s status=%s admin=%s",
                feedback.id, target, current_user.id,
            )
            return FeedbackService._response(feedback, submitter, notification_queued=False)

        if target not in ALLOWED_STATUS_TRANSITIONS.get(current_status, frozenset()):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Feedback in '{current_status}' cannot be moved to '{target}'."
                ),
            )

        feedback = FeedbackRepository.set_status(
            db,
            feedback=feedback,
            status=target,
            changed_by=current_user.id,
            changed_at=datetime.now(timezone.utc),
        )

        # The audit line. Who, what, from where to where, and when — the same
        # five facts the columns now carry, in the log that survives a row
        # being deleted with its user. There is no separate audit store in this
        # system to write to, and inventing one for a single workflow is not
        # the change this task calls for.
        logger.info(
            "FEEDBACK_STATUS_CHANGED: feedback=%s from=%s to=%s admin=%s "
            "submitter=%s organization=%s at=%s",
            feedback.id, current_status, target, current_user.id,
            submitter.id, organization_id, feedback.status_changed_at,
        )

        notification_id = queue_feedback_status_notification(db, feedback, submitter)
        if notification_id is not None and background_tasks is not None:
            # The fast path only. If this never runs — a frozen serverless
            # invocation, a process that dies — the row is still queued and the
            # dispatch sweeper delivers it with backoff.
            background_tasks.add_task(deliver_in_background, notification_id)

        return FeedbackService._response(
            feedback, submitter, notification_queued=notification_id is not None
        )

    @staticmethod
    def _response(
        feedback: FeedbackRequest, submitter: User, *, notification_queued: bool
    ) -> dict:
        """The updated row in the same shape the list returns, plus what happened."""
        return {
            **FeedbackService._item((feedback, submitter.id, submitter.name)),
            "notification_queued": notification_queued,
        }
