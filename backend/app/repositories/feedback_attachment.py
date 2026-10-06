from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.feedback_attachment import FeedbackAttachment
from app.models.feedback_request import FeedbackRequest


class FeedbackAttachmentRepository:
    """Data access for `feedback_attachments`. No business rules live here."""

    @staticmethod
    def list_for_feedback_ids(
        db: Session, feedback_ids: Iterable[int]
    ) -> Dict[int, List[FeedbackAttachment]]:
        """The attachments of many feedbacks in **one** statement.

        A page of N feedbacks costs one query here, never N: the list page asks
        for the whole page's ids at once. Only metadata columns are on the row —
        the bytes are in Drive and are not read until someone opens one — so
        this stays cheap however large the attachments are.
        """
        ids = list({int(i) for i in feedback_ids})
        grouped: Dict[int, List[FeedbackAttachment]] = {}
        if not ids:
            return grouped
        rows = db.scalars(
            select(FeedbackAttachment)
            .where(FeedbackAttachment.feedback_id.in_(ids))
            .order_by(FeedbackAttachment.feedback_id, FeedbackAttachment.position)
        ).all()
        for row in rows:
            grouped.setdefault(row.feedback_id, []).append(row)
        return grouped

    @staticmethod
    def get_with_feedback(
        db: Session, attachment_id: int
    ) -> Optional[Tuple[FeedbackAttachment, FeedbackRequest]]:
        """One attachment and the feedback it belongs to, in one round trip.

        Unscoped on purpose: the caller applies the organisation and role rules
        to the *feedback* it gets back, in one place
        (`FeedbackAttachmentService._may_read`), rather than this method
        guessing at them.
        """
        row = db.execute(
            select(FeedbackAttachment, FeedbackRequest)
            .join(FeedbackRequest, FeedbackRequest.id == FeedbackAttachment.feedback_id)
            .where(FeedbackAttachment.id == attachment_id)
        ).first()
        return (row[0], row[1]) if row else None
