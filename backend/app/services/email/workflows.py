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
from app.models.email_notification import (
    TYPE_FEEDBACK, TYPE_RELEASE, TYPE_WELCOME,
)
from app.repositories.user import UserRepository
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


def release_dedupe_key(version: str, user_id: int) -> str:
    """The announcement's identity: one version, one person.

    Keyed on the *version*, never on the release row. One version is several
    rows — Windows, macOS arm64, macOS x86_64, the portable zip — and each is
    published separately, so keying on the row would mail everybody once per
    artifact. Keyed on the version, publishing the second artifact of 2.0.0
    finds every announcement already queued and does nothing.
    """
    return f"release:{version}:user:{user_id}"


def queue_release_announcements(db: Session, release) -> list[int]:
    """Tell every active user that a new desktop version is available.

    One notification per user rather than one message addressed to everybody:
    a single failure then affects one recipient instead of all of them, each
    row retries on its own, and "has this person been told about 2.1.0?" is a
    question the database can answer. The audience is small — this is a staff
    tool, not a mailing list — so the row count is not a concern.

    Called when a release becomes `published` and never otherwise: a draft is
    by definition not something users should be told about, and withdrawing a
    release cannot unsend mail, which is the strongest argument for only ever
    announcing on the transition *into* published.

    Never raises. Publishing a release must not fail because mail could not be
    queued — the release is live either way, and the announcement is secondary.
    """
    queued: list[int] = []
    try:
        if not settings.RELEASE_EMAIL_ENABLED:
            logger.info("RELEASE_EMAIL_DISABLED: not announcing %s", getattr(release, "version", "?"))
            return queued

        version = str(getattr(release, "version", "") or "").strip()
        if not version:
            logger.warning("RELEASE_EMAIL_SKIPPED: release has no version")
            return queued

        payload: dict[str, Any] = {
            "version": version,
            "release_notes": getattr(release, "release_notes", None),
            "release_notes_url": getattr(release, "release_notes_url", None),
        }
        subject = messages.release_subject(payload)

        recipients = UserRepository.list_announcement_recipients(db)
        if not recipients:
            logger.warning("RELEASE_EMAIL_SKIPPED: no active users with an address")
            return queued

        for user in recipients:
            address = resolve_user_recipient(user.email or "")
            if not address:
                logger.warning(
                    "RELEASE_EMAIL_SKIPPED: user %s has no usable email address", user.id
                )
                continue
            row = EmailOutboxService.enqueue(
                db,
                notification_type=TYPE_RELEASE,
                dedupe_key=release_dedupe_key(version, user.id),
                recipients=address,
                subject=subject,
                payload={**payload, "user_id": user.id, "name": user.name},
                organization_id=getattr(user, "organization_id", None),
                user_id=user.id,
            )
            if row is not None:
                queued.append(row.id)

        logger.info(
            "RELEASE_EMAIL_QUEUED: version=%s users=%d notifications=%d",
            version, len(recipients), len(queued),
        )
        return queued
    except Exception:  # noqa: BLE001 - publishing must not fail over email
        logger.warning(
            "RELEASE_EMAIL_QUEUE_FAILED: version=%s",
            getattr(release, "version", "?"), exc_info=True,
        )
        return queued


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
