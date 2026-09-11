"""The two events that produce an email, and the rules around each.

Both entry points here share one contract, and it is the important part: **they
never raise into the caller, and they never affect the caller's outcome.** A
user is provisioned whether or not they can be welcomed; feedback is saved
whether or not Admin and HR can be told. Each returns the queued notification's
id, or None, and the caller uses it only to schedule an immediate delivery
attempt.

That is enforced by construction — every path is inside a `try` that logs and
returns None — rather than by each call site remembering to guard. Email is a
side effect of these events, and a side effect must not be able to fail the
thing it is a side effect of.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.email_notification import TYPE_FEEDBACK, TYPE_WELCOME
from app.services.email import messages
from app.services.email.outbox import EmailOutboxService
from app.services.email.recipients import (
    resolve_feedback_recipients, resolve_user_recipient,
)

logger = logging.getLogger("uvicorn.error")


def welcome_dedupe_key(user_id: int) -> str:
    """The welcome email's identity: the user, and nothing else.

    Not the login, not the session, not the day. This is what makes "exactly
    once per user" true no matter how many times the account signs in, on how
    many devices, or how many times a request is replayed.
    """
    return f"user:{user_id}"


def feedback_dedupe_key(feedback_id: int) -> str:
    """The notification's identity: the persisted feedback row.

    Derived from the primary key of a committed row, so it exists exactly once
    per genuine submission. A backend retry, a network timeout that the desktop
    client retries, or a replayed request all resolve to the same key and
    therefore the same single email.
    """
    return f"feedback:{feedback_id}"


def queue_welcome_email(db: Session, user) -> Optional[int]:
    """Queue the one-time welcome for a newly provisioned account.

    Call this only where an account has just been created — see
    `AuthService`. It is safe to call more than once (the outbox's unique
    constraint collapses the duplicates), but calling it on every sign-in would
    make the *first* deploy of this feature mail every existing employee, which
    is why the trigger is provisioning and not authentication.
    """
    try:
        if not settings.WELCOME_EMAIL_ENABLED:
            return None

        recipients = resolve_user_recipient(getattr(user, "email", "") or "")
        if not recipients:
            logger.warning("WELCOME_EMAIL_SKIPPED: user %s has no usable email address", user.id)
            return None

        payload: dict[str, Any] = {
            # Only what the email shows. No token, no permission map, no
            # password hash — a queued row is a durable copy of whatever is put
            # in it, and it is read back by a dispatcher that renders it into a
            # message.
            "user_id": user.id,
            "name": getattr(user, "name", None),
            "username": getattr(user, "username", None),
        }
        row = EmailOutboxService.enqueue(
            db,
            notification_type=TYPE_WELCOME,
            dedupe_key=welcome_dedupe_key(user.id),
            recipients=recipients,
            subject=messages.welcome_subject(),
            payload=payload,
            organization_id=getattr(user, "organization_id", None),
            user_id=user.id,
        )
        if row is None:
            return None
        logger.info(
            "WELCOME_EMAIL_QUEUED: user=%s notification=%s status=%s",
            user.id, row.id, row.status,
        )
        return row.id
    except Exception:  # noqa: BLE001 - authentication must never fail over email
        logger.warning(
            "WELCOME_EMAIL_QUEUE_FAILED: user=%s",
            getattr(user, "id", "?"), exc_info=True,
        )
        return None


def queue_feedback_notification(db: Session, feedback, user) -> Optional[int]:
    """Queue the Admin/HR notification for one submitted feedback.

    The feedback row is already committed when this runs. Nothing below can
    roll it back, and nothing below can fail the request: a mail server that is
    down, or recipients nobody has configured, produce a log line and a saved
    feedback — never an error shown to the person who wrote it.
    """
    try:
        recipients = resolve_feedback_recipients()
        if not recipients:
            logger.warning(
                "FEEDBACK_EMAIL_SKIPPED: feedback=%s reason=no_recipients_configured "
                "(set FEEDBACK_ADMIN_EMAIL / FEEDBACK_HR_EMAIL)",
                feedback.id,
            )
            return None

        submitted_at = getattr(feedback, "created_at", None) or datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "feedback_id": feedback.id,
            "user_id": getattr(user, "id", None),
            "username": getattr(user, "username", None),
            "user_name": getattr(user, "name", None),
            "user_email": getattr(user, "email", None),
            "user_role": getattr(user, "role_name", None),
            "category": feedback.category,
            "message": feedback.message,
            "submitted_at": submitted_at.isoformat() if hasattr(submitted_at, "isoformat") else str(submitted_at),
            "source": "Monitra Desktop",
        }
        row = EmailOutboxService.enqueue(
            db,
            notification_type=TYPE_FEEDBACK,
            dedupe_key=feedback_dedupe_key(feedback.id),
            recipients=recipients,
            subject=messages.feedback_subject(payload),
            payload=payload,
            organization_id=getattr(feedback, "organization_id", None),
            user_id=getattr(user, "id", None),
        )
        if row is None:
            return None
        logger.info(
            "FEEDBACK_EMAIL_QUEUED: feedback=%s user=%s category=%s notification=%s "
            "recipients=%d status=%s",
            feedback.id, getattr(user, "id", None), feedback.category, row.id,
            len(recipients), row.status,
        )
        return row.id
    except Exception:  # noqa: BLE001 - feedback is saved; the email is secondary
        logger.warning(
            "FEEDBACK_EMAIL_QUEUE_FAILED: feedback=%s",
            getattr(feedback, "id", "?"), exc_info=True,
        )
        return None
