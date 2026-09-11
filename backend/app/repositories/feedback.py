from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.feedback_request import FeedbackRequest
from app.models.user import User

#: A listing row: the feedback plus the submitter's id and display name. The
#: submitter is joined in the same statement rather than fetched per row, so a
#: page of N rows is one query, not N + 1. `feedback_requests` declares no ORM
#: relationship to `users`, so the join is written out explicitly.
FeedbackRow = Tuple[FeedbackRequest, int, str]


class FeedbackRepository:
    """Data access for `feedback_requests`. No business rules live here."""

    @staticmethod
    def create(
        db: Session,
        *,
        organization_id: int,
        user_id: int,
        category: str,
        message: str,
        status: str,
    ) -> FeedbackRequest:
        feedback = FeedbackRequest(
            organization_id=organization_id,
            user_id=user_id,
            category=category,
            message=message,
            status=status,
        )
        db.add(feedback)
        db.commit()
        db.refresh(feedback)
        return feedback

    @staticmethod
    def get_by_id(db: Session, feedback_id: int) -> Optional[FeedbackRequest]:
        return db.scalar(
            select(FeedbackRequest).where(FeedbackRequest.id == feedback_id)
        )

    @staticmethod
    def list_by_organization(
        db: Session, organization_id: int, limit: int = 100
    ) -> List[FeedbackRequest]:
        return list(
            db.scalars(
                select(FeedbackRequest)
                .where(FeedbackRequest.organization_id == organization_id)
                .order_by(FeedbackRequest.created_at.desc())
                .limit(limit)
            ).all()
        )

    # ------------------------------------------------------------------
    # Read paths for the dashboard's view-only feedback list.
    #
    # Every one of these takes the scoping value (owner or organization) as a
    # required argument rather than filtering after the fact, so a row outside
    # the caller's scope is never loaded in the first place.
    # ------------------------------------------------------------------

    @staticmethod
    def _select_rows():
        return select(
            FeedbackRequest, User.id.label("employee_id"), User.name.label("employee_name")
        ).join(User, User.id == FeedbackRequest.user_id)

    @staticmethod
    def _page(db: Session, stmt, count_filters, page: int, limit: int):
        total = db.scalar(
            select(func.count(FeedbackRequest.id))
            .join(User, User.id == FeedbackRequest.user_id)
            .where(*count_filters)
        ) or 0
        rows = db.execute(
            stmt.order_by(FeedbackRequest.created_at.desc(), FeedbackRequest.id.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        ).all()
        return [(row[0], row[1], row[2]) for row in rows], total

    @staticmethod
    def list_for_user(
        db: Session, *, user_id: int, page: int, limit: int
    ) -> Tuple[List[FeedbackRow], int]:
        """A page of the feedback this user submitted, newest first."""
        filters = [FeedbackRequest.user_id == user_id]
        return FeedbackRepository._page(
            db, FeedbackRepository._select_rows().where(*filters), filters, page, limit
        )

    @staticmethod
    def get_for_user(
        db: Session, *, feedback_id: int, user_id: int
    ) -> Optional[FeedbackRow]:
        """One feedback row, only if this user submitted it."""
        row = db.execute(
            FeedbackRepository._select_rows().where(
                FeedbackRequest.id == feedback_id, FeedbackRequest.user_id == user_id
            )
        ).first()
        return (row[0], row[1], row[2]) if row else None

    @staticmethod
    def list_for_organization_with_user(
        db: Session,
        *,
        organization_id: int,
        page: int,
        limit: int,
        category: Optional[str] = None,
    ) -> Tuple[List[FeedbackRow], int]:
        """A page of every feedback filed inside one organization, newest first."""
        filters = [FeedbackRequest.organization_id == organization_id]
        if category:
            filters.append(FeedbackRequest.category == category)
        return FeedbackRepository._page(
            db, FeedbackRepository._select_rows().where(*filters), filters, page, limit
        )

    @staticmethod
    def get_for_organization(
        db: Session, *, feedback_id: int, organization_id: int
    ) -> Optional[FeedbackRow]:
        """One feedback row, only if it belongs to this organization."""
        row = db.execute(
            FeedbackRepository._select_rows().where(
                FeedbackRequest.id == feedback_id,
                FeedbackRequest.organization_id == organization_id,
            )
        ).first()
        return (row[0], row[1], row[2]) if row else None

    # ------------------------------------------------------------------
    # The Admin status workflow.
    # ------------------------------------------------------------------

    @staticmethod
    def get_with_submitter_for_organization(
        db: Session, *, feedback_id: int, organization_id: int
    ) -> Optional[Tuple[FeedbackRequest, User]]:
        """One feedback row and the whole user who submitted it, scoped to a tenant.

        The full `User` rather than the (id, name) pair the listing rows carry,
        because this is what the notification's recipient is derived from: the
        address comes off the joined user record and therefore off the
        database, never off the request. The join is an inner join, so a
        feedback whose submitter has been deleted simply does not load — and
        that is the right answer for a workflow whose entire output is an email
        to that person.

        Scoped by `organization_id` in the same statement as the id, so a row
        belonging to another tenant is never loaded and is answered as a 404
        rather than as a permission error.
        """
        row = db.execute(
            select(FeedbackRequest, User)
            .join(User, User.id == FeedbackRequest.user_id)
            .where(
                FeedbackRequest.id == feedback_id,
                FeedbackRequest.organization_id == organization_id,
            )
        ).first()
        return (row[0], row[1]) if row else None

    @staticmethod
    def set_status(
        db: Session,
        *,
        feedback: FeedbackRequest,
        status: str,
        changed_by: Optional[int],
        changed_at: datetime,
    ) -> FeedbackRequest:
        """Move one already-loaded row to a new status, recording who and when.

        Takes the instance rather than an id because the caller has already
        loaded it under the tenant scope and has already decided the transition
        is allowed; re-fetching here would only offer a second, unscoped way to
        reach the same row.
        """
        feedback.status = status
        feedback.status_changed_at = changed_at
        feedback.status_changed_by = changed_by
        db.commit()
        db.refresh(feedback)
        return feedback
