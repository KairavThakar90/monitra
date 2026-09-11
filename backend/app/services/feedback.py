import math
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.permissions import resolve_role_alias
from app.models.feedback_request import FeedbackRequest
from app.models.user import User
from app.repositories.feedback import FeedbackRepository, FeedbackRow
from app.schemas.feedback import FeedbackCreate, FeedbackStatus
from app.services.email import deliver_in_background, queue_feedback_notification

#: The roles that may read the whole organization's feedback. Admin, HR and
#: Leader are the three the product asks for; `org_admin`/`super_admin` and
#: `project_leader` are the other spellings ROLE_PERMISSIONS already defines for
#: the same authority, so leaving them out would deny a user whose account
#: happens to carry the alternate name. `manager` and `employee` are not here:
#: a manager was never given directory-wide feedback visibility.
FEEDBACK_VIEW_ALL_ROLES = frozenset(
    {"admin", "org_admin", "super_admin", "hr", "leader", "project_leader"}
)


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
    # Read paths. All of them are view-only: there is no approval, no status
    # transition and no mutation of an existing row anywhere below.
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
    def _item(row: FeedbackRow) -> dict:
        feedback, employee_id, employee_name = row
        return {
            "id": feedback.id,
            "employee_id": employee_id,
            "employee_name": employee_name,
            "category": feedback.category,
            "message": feedback.message,
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
