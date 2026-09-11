"""Data access for `email_notifications`. No business rules live here.

Two of these methods carry the guarantees the whole feature rests on, so they
are worth reading closely:

* `enqueue` inserts in a nested transaction and treats a unique-constraint
  violation as success-by-someone-else. Checking first and inserting second
  cannot be made safe — two concurrent requests both see "no row" — so the
  database decides, and the loser of the race reads back the winner's row.
* `claim_due` moves a row out of contention with a conditional UPDATE before
  anything is sent. Two sweepers running at once therefore cannot both deliver
  the same notification: the second one's UPDATE matches nothing.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.email_notification import (
    STATUS_PENDING, STATUS_SENT, EmailNotification,
)


class EmailNotificationRepository:

    @staticmethod
    def get_by_event(
        db: Session, *, notification_type: str, dedupe_key: str
    ) -> Optional[EmailNotification]:
        return db.scalar(
            select(EmailNotification).where(
                EmailNotification.notification_type == notification_type,
                EmailNotification.dedupe_key == dedupe_key,
            )
        )

    @staticmethod
    def get_by_id(db: Session, notification_id: int) -> Optional[EmailNotification]:
        return db.scalar(
            select(EmailNotification).where(EmailNotification.id == notification_id)
        )

    @staticmethod
    def enqueue(
        db: Session,
        *,
        notification_type: str,
        dedupe_key: str,
        recipients: str,
        subject: str,
        payload: str,
        max_attempts: int,
        next_attempt_at: datetime,
        organization_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> tuple[EmailNotification, bool]:
        """Queue one notification. Returns (row, created).

        `created` is False when this event was already queued — by an earlier
        request, by a retry of this one, or by another worker a microsecond
        ago. The caller treats that as success: the email is somebody's
        responsibility either way, and it is exactly one email.
        """
        row = EmailNotification(
            notification_type=notification_type,
            dedupe_key=dedupe_key,
            recipients=recipients,
            subject=subject,
            payload=payload,
            status=STATUS_PENDING,
            attempt_count=0,
            max_attempts=max_attempts,
            next_attempt_at=next_attempt_at,
            organization_id=organization_id,
            user_id=user_id,
        )
        try:
            # A SAVEPOINT, so that losing this race rolls back only the failed
            # INSERT. Letting the IntegrityError reach the outer transaction
            # would poison a session that the caller is still using — the
            # feedback row it just committed is fine, but anything it did after
            # would fail on a session needing a rollback.
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            existing = EmailNotificationRepository.get_by_event(
                db, notification_type=notification_type, dedupe_key=dedupe_key
            )
            if existing is None:
                # The unique constraint was not what rejected this. Nothing
                # here can repair that, and swallowing it would hide a real
                # schema problem behind a silent non-delivery.
                raise
            return existing, False

        db.commit()
        db.refresh(row)
        return row, True

    @staticmethod
    def due_ids(db: Session, *, now: datetime, limit: int) -> List[int]:
        """Ids of pending notifications whose next attempt is due, oldest first."""
        return list(
            db.scalars(
                select(EmailNotification.id)
                .where(
                    EmailNotification.status == STATUS_PENDING,
                    EmailNotification.next_attempt_at <= now,
                )
                .order_by(EmailNotification.next_attempt_at, EmailNotification.id)
                .limit(limit)
            ).all()
        )

    @staticmethod
    def claim(
        db: Session,
        *,
        notification_id: int,
        now: datetime,
        retry_at: datetime,
    ) -> Optional[EmailNotification]:
        """Take ownership of one due notification, or return None.

        The attempt is counted and the next attempt pushed out *before* the
        message is sent, not after. That ordering is the point: if this process
        dies mid-send, the row is already parked until `retry_at` instead of
        being picked up immediately by the next sweep and sent again. It
        chooses "possibly never delivered, and visibly so" over "possibly
        delivered twice".
        """
        claimed = db.execute(
            update(EmailNotification)
            .where(
                EmailNotification.id == notification_id,
                EmailNotification.status == STATUS_PENDING,
                EmailNotification.next_attempt_at <= now,
            )
            .values(
                attempt_count=EmailNotification.attempt_count + 1,
                last_attempt_at=now,
                next_attempt_at=retry_at,
            )
            .returning(EmailNotification.id)
        ).scalar()
        db.commit()
        if claimed is None:
            return None
        return EmailNotificationRepository.get_by_id(db, notification_id)

    @staticmethod
    def mark_sent(db: Session, *, notification_id: int, now: datetime) -> None:
        db.execute(
            update(EmailNotification)
            .where(EmailNotification.id == notification_id)
            .values(status=STATUS_SENT, sent_at=now, last_error=None)
        )
        db.commit()

    @staticmethod
    def mark_attempt_failed(
        db: Session, *, notification_id: int, error: str, terminal_status: Optional[str]
    ) -> None:
        """Record why an attempt failed, and park the row if it was the last one.

        `terminal_status` is None while retries remain — the row keeps the
        `pending` status and the `next_attempt_at` that `claim` already set.
        """
        values: dict = {"last_error": error}
        if terminal_status is not None:
            values["status"] = terminal_status
        db.execute(
            update(EmailNotification)
            .where(EmailNotification.id == notification_id)
            .values(**values)
        )
        db.commit()

    @staticmethod
    def release(db: Session, *, notification_id: int, next_attempt_at: datetime) -> None:
        """Hand a claimed row back without counting the attempt against it.

        Used when the failure was this deployment's configuration rather than a
        delivery problem: an unset SMTP_HOST must not consume the six attempts
        a notification gets, or fixing the configuration an hour later would
        find every queued message already marked failed.
        """
        db.execute(
            update(EmailNotification)
            .where(EmailNotification.id == notification_id)
            .values(
                attempt_count=EmailNotification.attempt_count - 1,
                next_attempt_at=next_attempt_at,
            )
        )
        db.commit()

    @staticmethod
    def counts_by_status(db: Session) -> dict:
        """How much is queued, sent and failed. For health and diagnostics."""
        from sqlalchemy import func

        rows = db.execute(
            select(EmailNotification.status, func.count(EmailNotification.id))
            .group_by(EmailNotification.status)
        ).all()
        return {status: count for status, count in rows}
