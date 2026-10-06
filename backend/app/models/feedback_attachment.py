from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, ForeignKeyConstraint, Identity, Index, Integer, SmallInteger, String,
    TIMESTAMP, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FeedbackAttachment(Base):
    """One file attached to a piece of feedback.

    The bytes are **not** in this table. They live in the same private Google
    Drive store the screenshots use (`google_drive_service`), and this row is the
    only thing that points at them: the Drive object is never public, and every
    read goes through `GET /feedback/attachments/{id}/content`, which applies
    the feedback's own organisation and role rules before a byte is fetched.

    A feedback may carry several rows; the submission form limits how many
    (`FEEDBACK_ATTACHMENT_MAX_COUNT`) but the schema does not, so raising that
    limit later is a setting, not a migration. A feedback with none is the
    normal case — every row that existed before this table did.

    `file_name` is the **server-generated** object name
    (``u<user>_<client_op>_<n>.<ext>``). It is derived from identifiers the
    server validated, never from `original_filename`, which is the uploader's
    own text kept only so the dashboard can show what the file was called.
    """

    __tablename__ = 'feedback_attachments'

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    feedback_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: Order within the submission, starting at 1. Unique per feedback, which is
    #: also what makes the two halves of a replayed submission collide instead
    #: of both being stored.
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    #: The uploader's file name, reduced to a printable basename by the server.
    #: Display only — it is never used to build a path or an object name.
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The sniffed, validated type — one of `ALLOWED_ATTACHMENT_TYPES`. This is
    #: what the content endpoint serves, not what the client claimed.
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The server-generated object name inside the Drive folder.
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Human-readable location (``Feedback/2026-10/<file_name>``), for support
    #: tooling. Informational; the Drive file id is the reference.
    file_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    google_drive_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    google_drive_folder_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        # CASCADE: attachments have no meaning without their feedback, and the
        # feedback row itself already cascades from the user and organisation.
        ForeignKeyConstraint(
            ['feedback_id'], ['feedback_requests.id'],
            name='fk_feedback_attachments_feedback', ondelete='CASCADE',
        ),
        UniqueConstraint('feedback_id', 'position', name='uq_feedback_attachments_position'),
        Index('idx_feedback_attachments_feedback', 'feedback_id'),
    )
