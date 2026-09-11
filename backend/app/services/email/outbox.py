"""Queueing and delivering the outbox.

This is the one place that decides *when* a message goes out, how often it is
retried, and what happens when it does not. Everything above it queues; nothing
above it sends.

Delivery runs in two places, and the division between them is deliberate:

* **Immediately after the response**, through FastAPI's `BackgroundTasks`. The
  request that queued the notification has already been answered by then, so a
  slow mail server delays nobody. This is an optimisation, not a guarantee: on
  a serverless platform the invocation can be frozen the moment the response is
  written, and the task may not run at all.
* **From the sweeper**, `POST /internal/email/dispatch`, driven by a scheduler.
  This is the guarantee. Anything the fast path missed — because it was frozen,
  because the process died, because the mail server was down — is picked up
  here and retried with backoff until it is delivered or runs out of attempts.

Nothing was introduced to run this: no queue broker, no worker process, no new
dependency. The outbox table *is* the queue, and an HTTP endpoint on a timer is
the worker, which is what suits a backend deployed as serverless functions.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.email_notification import (
    STATUS_FAILED, STATUS_PENDING, EmailNotification,
)
from app.repositories.email_notification import EmailNotificationRepository
from app.services.email.messages import BUILDERS
from app.services.email.provider import (
    EmailAddressError, EmailDeliveryError, EmailNotConfiguredError,
    get_email_provider, redact_error, unconfigured_reason,
)

logger = logging.getLogger("uvicorn.error")

#: How long a row is parked when this deployment simply cannot send. Short,
#: because the fix is a configuration change that could land at any moment,
#: and the attempt was never counted against the row anyway.
UNCONFIGURED_RETRY_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def retry_delay_seconds(attempt: int) -> float:
    """Seconds to wait before attempt number `attempt + 1`.

    Exponential, capped, and jittered. The jitter is not decoration: an outage
    queues every notification behind the same clock, and without it they all
    retry in the same second and hit a recovering mail server together. Full
    jitter — a uniform draw over the whole window — spreads them properly.
    """
    base = max(1, settings.EMAIL_RETRY_BASE_DELAY_SECONDS)
    cap = max(base, settings.EMAIL_RETRY_MAX_DELAY_SECONDS)
    window = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(base / 2, window)


class EmailOutboxService:
    """Queue an email; deliver the queue. The only writer of delivery status."""

    # ------------------------------------------------------------------
    # Queueing
    # ------------------------------------------------------------------

    @staticmethod
    def enqueue(
        db: Session,
        *,
        notification_type: str,
        dedupe_key: str,
        recipients: list[str],
        subject: str,
        payload: dict[str, Any],
        organization_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> Optional[EmailNotification]:
        """Record the intent to send one email. Returns the row, or None.

        None means nothing was queued because there was nobody to send to —
        which is a configuration state, not an error, and the caller carries on.
        """
        if not recipients:
            logger.warning(
                "EMAIL_QUEUE_SKIPPED: type=%s key=%s reason=no_recipients_configured",
                notification_type, dedupe_key,
            )
            return None

        row, created = EmailNotificationRepository.enqueue(
            db,
            notification_type=notification_type,
            dedupe_key=dedupe_key,
            recipients=json.dumps(recipients),
            subject=subject,
            payload=json.dumps(payload, default=str),
            max_attempts=max(1, settings.EMAIL_MAX_ATTEMPTS),
            next_attempt_at=_now(),
            organization_id=organization_id,
            user_id=user_id,
        )
        logger.info(
            "EMAIL_QUEUED: id=%s type=%s key=%s user=%s recipients=%d created=%s",
            row.id, notification_type, dedupe_key, user_id, len(recipients), created,
        )
        return row

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    @staticmethod
    def deliver_one(db: Session, notification_id: int) -> str:
        """Attempt one notification. Returns what happened, for the logs.

        One of: ``sent``, ``retrying``, ``failed``, ``skipped`` (not due, or
        already handled by another worker) or ``unconfigured``.
        """
        reason = unconfigured_reason()
        if reason is not None:
            # Checked before claiming, so an unsendable deployment does not
            # consume attempts. Logged at warning because a production
            # deployment reaching this is misconfigured, not idle.
            logger.warning(
                "EMAIL_DISPATCH_UNCONFIGURED: id=%s reason=%s", notification_id, reason,
            )
            return "unconfigured"

        now = _now()
        row = EmailNotificationRepository.get_by_id(db, notification_id)
        if row is None or row.status != STATUS_PENDING:
            return "skipped"

        claimed = EmailNotificationRepository.claim(
            db,
            notification_id=notification_id,
            now=now,
            retry_at=now + timedelta(seconds=retry_delay_seconds(row.attempt_count + 1)),
        )
        if claimed is None:
            # Another worker got there first, or the row is not due yet.
            return "skipped"

        return EmailOutboxService._send_claimed(db, claimed)

    @staticmethod
    def _send_claimed(db: Session, row: EmailNotification) -> str:
        """Render and send a row this worker already owns.

        Every value this method needs is read off the instance *before* the
        first write, and nothing reads it afterwards. That is not tidiness:
        `mark_sent` commits, a commit expires every instance in the session, and
        the next attribute access silently re-SELECTs the row. Logging the
        recipient count after marking the row sent therefore issued a second
        query per delivery — and raised `ObjectDeletedError` if the row had
        since gone (a cascade from a deleted user is enough), turning a delivery
        that had already succeeded into a logged error.
        """
        notification_id = row.id
        attempt = row.attempt_count
        notification_type = row.notification_type
        payload_json = row.payload
        recipients_json = row.recipients
        max_attempts = row.max_attempts
        log_context = (
            f"id={notification_id} type={notification_type} "
            f"key={row.dedupe_key} user={row.user_id} attempt={attempt}/{max_attempts}"
        )

        try:
            builder = BUILDERS.get(notification_type)
            if builder is None:
                raise EmailAddressError(
                    f"No builder is registered for {notification_type!r}."
                )
            recipients = json.loads(recipients_json)
            message = builder(json.loads(payload_json), recipients)
            get_email_provider().send(message)

        except EmailNotConfiguredError as exc:
            # Became unsendable between the check above and the send. Give the
            # attempt back rather than spending it on a configuration problem.
            EmailNotificationRepository.release(
                db,
                notification_id=notification_id,
                next_attempt_at=_now() + timedelta(seconds=UNCONFIGURED_RETRY_SECONDS),
            )
            logger.warning("EMAIL_DISPATCH_UNCONFIGURED: %s reason=%s", log_context, exc)
            return "unconfigured"

        except (EmailDeliveryError, EmailAddressError, ValueError, KeyError) as exc:
            error = redact_error(exc)
            terminal = STATUS_FAILED if attempt >= max_attempts else None
            EmailNotificationRepository.mark_attempt_failed(
                db, notification_id=notification_id, error=error, terminal_status=terminal,
            )
            if terminal:
                logger.error("EMAIL_DISPATCH_FAILED: %s error=%s", log_context, error)
                return "failed"
            logger.warning("EMAIL_DISPATCH_RETRY: %s error=%s", log_context, error)
            return "retrying"

        except Exception as exc:  # noqa: BLE001 - a sweep must survive one bad row
            error = redact_error(exc)
            terminal = STATUS_FAILED if attempt >= max_attempts else None
            EmailNotificationRepository.mark_attempt_failed(
                db, notification_id=notification_id, error=error, terminal_status=terminal,
            )
            logger.error(
                "EMAIL_DISPATCH_UNEXPECTED_ERROR: %s error=%s", log_context, error,
                exc_info=True,
            )
            return "failed" if terminal else "retrying"

        EmailNotificationRepository.mark_sent(db, notification_id=notification_id, now=_now())
        logger.info("EMAIL_SENT: %s recipients=%d", log_context, len(recipients))
        return "sent"

    @staticmethod
    def dispatch_pending(db: Session, limit: Optional[int] = None) -> dict:
        """Drain what is due, up to `limit` notifications. Never raises.

        Returns a tally by outcome. Called by the sweeper endpoint and safe to
        call concurrently with itself: every row is claimed before it is sent.
        """
        reason = unconfigured_reason()
        if reason is not None:
            logger.warning("EMAIL_SWEEP_SKIPPED: %s", reason)
            return {"attempted": 0, "unconfigured": True, "reason": reason}

        batch = limit or max(1, settings.EMAIL_DISPATCH_BATCH_SIZE)
        try:
            ids = EmailNotificationRepository.due_ids(db, now=_now(), limit=batch)
        except Exception:  # noqa: BLE001
            logger.exception("EMAIL_SWEEP_QUERY_FAILED")
            return {"attempted": 0, "error": "query_failed"}

        tally: dict[str, int] = {}
        for notification_id in ids:
            try:
                outcome = EmailOutboxService.deliver_one(db, notification_id)
            except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
                logger.exception("EMAIL_SWEEP_ROW_FAILED: id=%s", notification_id)
                outcome = "error"
            tally[outcome] = tally.get(outcome, 0) + 1

        result = {"attempted": len(ids), **tally}
        if ids:
            logger.info("EMAIL_SWEEP_COMPLETE: %s", result)
        return result


def deliver_in_background(notification_id: int) -> None:
    """Deliver one notification on its own database session.

    This is what `BackgroundTasks` runs after a response has been written. It
    opens its own session because the request's session is closed by the time
    it executes, and it swallows everything: a background task that raises is
    logged by the framework and helps nobody, while the outbox row it was
    working on is still queued for the sweeper either way.
    """
    from app.core.database import get_session_local

    db = None
    try:
        db = get_session_local()()
        EmailOutboxService.deliver_one(db, notification_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "EMAIL_BACKGROUND_DISPATCH_FAILED: id=%s (the sweeper will retry it)",
            notification_id, exc_info=True,
        )
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                logger.warning("EMAIL_BACKGROUND_SESSION_CLOSE_FAILED", exc_info=True)
