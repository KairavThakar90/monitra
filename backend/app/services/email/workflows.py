"""The events that produce an email, and the rules around each.

Every entry point here shares one contract, and it is the important part:
**they never raise into the caller, and they never affect the caller's
outcome.** A user is provisioned whether or not they can be welcomed; feedback
is saved whether or not Admin and HR can be told; a status change is committed
whether or not the submitter can be told about it. Each returns the queued
notification's id, or None, and the caller uses it only to schedule an
immediate delivery attempt.

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

from app.core.background import run_blocking
from app.core.config import settings
from app.models.email_notification import (
    TYPE_FEEDBACK, TYPE_FEEDBACK_STATUS, TYPE_RELEASE, TYPE_WELCOME,
    TYPE_MANUAL_TIME_DECISION, TYPE_MANUAL_TIME_RECEIPT, TYPE_MANUAL_TIME_REQUEST,
    TYPE_MONTHLY_PROJECT_SUMMARY, TYPE_MONTHLY_REPORT, TYPE_PROJECT_BUDGET_ALERT,
    TYPE_SCREENSHOT_NOTICE, TYPE_WEEKLY_REPORT,
)
from app.repositories.email_notification import EmailNotificationRepository
from app.repositories.user import UserRepository
from app.services.email import messages
from app.services.email.outbox import EmailOutboxService
from app.services.email.provider import get_email_provider
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


def feedback_status_dedupe_key(feedback_id: int, status: str) -> str:
    """The status update's identity: this feedback, in this state.

    The status is part of the key, not a detail inside the payload, and that is
    the whole duplicate-protection story for this workflow. Pressing Working
    twice, a double-click that fires two requests, a browser refresh that
    replays the last one, or two administrators acting on the same row at the
    same moment all compute `feedback:17:in_progress` — one row, one email.
    Moving the same feedback on to `resolved` computes a different key, so the
    second, genuinely different event is still delivered.

    Nothing in the key is a timestamp or a request identifier. Both would make
    every press a distinct event, which is exactly the bug this prevents.
    """
    return f"feedback:{feedback_id}:{status}"


def release_dedupe_key(version: str, user_id: int) -> str:
    """The announcement's identity: one version, one person.

    Keyed on the *version*, never on the release row. One version is several
    rows — Windows, macOS arm64, macOS x86_64, the portable zip — and each is
    published separately, so keying on the row would mail everybody once per
    artifact. Keyed on the version, publishing the second artifact of 2.0.0
    finds every announcement already queued and does nothing.
    """
    return f"release:{version}:user:{user_id}"


def release_test_dedupe_key(version: str, address: str) -> str:
    """A test announcement's identity: one version, one test address.

    Deliberately a different namespace from `release_dedupe_key`. A test run
    must never consume the real, once-per-user announcement for the same
    version -- otherwise rehearsing a release would silently stop it ever being
    announced to the people it is for.
    """
    return f"release-test:{version}:to:{address.strip().lower()}"


def _test_recipient_addresses() -> list[str]:
    """`RELEASE_EMAIL_TEST_RECIPIENTS` as usable addresses, or [] when unset."""
    raw = [part.strip() for part in (settings.RELEASE_EMAIL_TEST_RECIPIENTS or "").split(",")]
    usable: list[str] = []
    for part in raw:
        if not part:
            continue
        address = resolve_user_recipient(part)
        if address and address[0] not in usable:
            usable.append(address[0])
    return usable


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

        # Test mode. A configured test list REPLACES the audience: the mail goes
        # to those addresses and to no user, so a rehearsal cannot reach anyone
        # who did not ask for it. The list is read from configuration on every
        # call, never cached, so removing it is what takes a deployment live.
        if (settings.RELEASE_EMAIL_TEST_RECIPIENTS or "").strip():
            test_addresses = _test_recipient_addresses()
            if not test_addresses:
                # A list was configured but nothing in it is usable. Sending to
                # the real audience would be the opposite of what was asked for.
                logger.warning(
                    "RELEASE_EMAIL_SKIPPED: RELEASE_EMAIL_TEST_RECIPIENTS is set "
                    "but holds no usable address; nothing sent"
                )
                return queued
            test_payload = {**payload, "test": True, "name": "Tester"}
            test_subject = messages.release_subject(test_payload)
            for address in test_addresses:
                row = EmailOutboxService.enqueue(
                    db,
                    notification_type=TYPE_RELEASE,
                    dedupe_key=release_test_dedupe_key(version, address),
                    recipients=[address],
                    subject=test_subject,
                    payload=test_payload,
                )
                if row is not None:
                    queued.append(row.id)
            logger.info(
                "RELEASE_EMAIL_TEST_QUEUED: version=%s addresses=%d notifications=%d",
                version, len(test_addresses), len(queued),
            )
            return queued

        recipients = UserRepository.list_release_recipients(db)
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


def queue_feedback_status_notification(db: Session, feedback, submitter) -> Optional[int]:
    """Tell the submitter that their feedback is being worked on, or is resolved.

    The outbound half of the feedback workflow, and the mirror image of
    `queue_feedback_notification`: that one tells Admin and HR that something
    arrived, this one tells the person who wrote it what happened to it.

    **The recipient is `submitter`, and `submitter` comes from the feedback
    row's own `user_id`.** The caller loads it by joining `users` to the
    feedback inside the tenant scope — see
    `FeedbackRepository.get_with_submitter_for_organization` — so there is no
    argument anywhere on this path that a request could set to redirect the
    mail. An administrator can choose *which feedback* to act on, and that
    choice alone determines who is written to.

    Called after the status change is committed, and it cannot undo it: like
    every other entry point in this module it returns an id or None and never
    raises. An administrator's click succeeds because the status changed; the
    email is what follows from that, not what it depends on.
    """
    try:
        recipients = resolve_user_recipient(getattr(submitter, "email", "") or "")
        if not recipients:
            logger.warning(
                "FEEDBACK_STATUS_EMAIL_SKIPPED: feedback=%s user=%s "
                "reason=submitter_has_no_usable_email",
                feedback.id, getattr(submitter, "id", None),
            )
            return None

        submitted_at = getattr(feedback, "created_at", None) or datetime.now(timezone.utc)
        status = str(getattr(feedback, "status", "") or "")
        payload: dict[str, Any] = {
            # What the email shows, and nothing more. No message body: the
            # person reading this wrote it, they do not need it read back to
            # them, and a mailbox is a poor place to keep a second copy of it.
            # No administrator's name either — who handled it is an internal
            # detail of how the organisation works, and the update is from
            # Monitra rather than from a named individual.
            "feedback_id": feedback.id,
            "user_id": getattr(submitter, "id", None),
            "name": getattr(submitter, "name", None),
            "category": getattr(feedback, "category", None),
            "status": status,
            "submitted_at": (
                submitted_at.isoformat() if hasattr(submitted_at, "isoformat")
                else str(submitted_at)
            ),
        }
        row = EmailOutboxService.enqueue(
            db,
            notification_type=TYPE_FEEDBACK_STATUS,
            dedupe_key=feedback_status_dedupe_key(feedback.id, status),
            recipients=recipients,
            subject=messages.feedback_status_subject(payload),
            payload=payload,
            organization_id=getattr(feedback, "organization_id", None),
            user_id=getattr(submitter, "id", None),
        )
        if row is None:
            return None
        logger.info(
            "FEEDBACK_STATUS_EMAIL_QUEUED: feedback=%s user=%s status=%s "
            "notification=%s state=%s",
            feedback.id, getattr(submitter, "id", None), status, row.id, row.status,
        )
        return row.id
    except Exception:  # noqa: BLE001 - the status change is committed; the email is secondary
        logger.warning(
            "FEEDBACK_STATUS_EMAIL_QUEUE_FAILED: feedback=%s",
            getattr(feedback, "id", "?"), exc_info=True,
        )
        return None


# ----------------------------------------------------------------------
# Workflow 5 — weekly productivity report
# ----------------------------------------------------------------------

def weekly_report_dedupe_key(week_start, user_id: int) -> str:
    """The weekly report's identity: one person, one report week.

    The *week* is in the key, never the day the job ran and never a timestamp.
    That is the whole duplicate-protection story for this workflow: a Vercel
    retry, a scheduler that fires twice, a deployment part-way through the
    sweep, a manual re-run and a network failure that leaves the caller unsure
    all compute ``week:2026-09-08:user:42`` and collapse onto the single row
    that already exists. The following Monday computes a different key, so the
    next genuine report is still delivered.
    """
    week = week_start.isoformat() if hasattr(week_start, "isoformat") else str(week_start)
    return f"week:{week}:user:{user_id}"


def _day_payload(day) -> Optional[dict[str, Any]]:
    """One day figure as the template wants it: a weekday name and its number.

    The calendar date is deliberately dropped. "Wednesday" is what the email
    says, and carrying the date as well would only invite the two to disagree
    after a template edit.
    """
    if day is None:
        return None
    return {
        "name": day.weekday_name,
        "total_seconds": day.total_seconds,
        "activity": day.activity,
    }


def _weekly_report_payload(*, user, period, metrics) -> dict[str, Any]:
    """Everything the weekly email shows, and nothing else.

    **The figures belong to `user`, and that is checked rather than assumed.**
    The recipient's address and the aggregated week are paired here and only
    here, so the one way to mail somebody another employee's productivity data
    would be to pass a mismatched pair — which is refused below rather than
    rendered.
    """
    if getattr(metrics, "user_id", None) != getattr(user, "id", None):
        # A programming error, not a runtime condition — but the failure mode
        # is mailing somebody else's productivity data to this person, so it is
        # refused loudly here rather than rendered.
        raise ValueError(
            "Weekly report metrics do not belong to the recipient "
            f"(metrics user {getattr(metrics, 'user_id', None)!r}, "
            f"recipient {getattr(user, 'id', None)!r})."
        )

    return {
        # Only what the email shows. No project or task ids, no permission
        # map, no token — a queued row is a durable copy of whatever is put in
        # it, and this one describes a person's working week.
        "user_id": user.id,
        "name": getattr(user, "name", None),
        "week_start": period.start_date.isoformat(),
        "week_end": period.end_date.isoformat(),
        "period_label": period.label,
        "period_short": period.short_label,
        "timezone": period.timezone_name,
        "total_seconds": metrics.total_seconds,
        "active_seconds": metrics.active_seconds,
        "idle_seconds": metrics.idle_seconds,
        "average_activity": metrics.average_activity,
        "project_count": metrics.project_count,
        "has_activity": metrics.has_activity,
        "highest_activity_day": _day_payload(metrics.highest_activity_day),
        "lowest_activity_day": _day_payload(metrics.lowest_activity_day),
        # Which of the two Reports screens this person is allowed to open.
        # Both guards redirect rather than refuse, and an admin sent to the
        # member route loses the date range on the way — so the button has to
        # know. Stored as a plain boolean; the permission map itself never
        # goes into an outbox row.
        "can_view_all_time": bool(
            (getattr(user, "permissions", None) or {}).get("time_entries:view_all")
        ),
    }


def queue_weekly_report(db: Session, *, user, period, metrics) -> tuple[Optional[int], bool]:
    """Queue one user's weekly report. Returns ``(notification_id, created)``.

    `created` is False when this person's report for this week was already
    queued by an earlier run. It is read back before the insert purely so the
    run can *report* "already queued" separately from "queued" — the
    duplicate-prevention guarantee itself is the outbox's unique constraint,
    not this lookup, so a lost race costs an inaccurate tally and never a
    second email.

    One notification per user, addressed to that user alone. Never a shared
    message with everybody in the recipient list: a weekly report is somebody's
    own productivity data, a copied address list would hand each reader the
    roster, and a single failure would cost every recipient their report
    instead of one.
    """
    payload = _weekly_report_payload(user=user, period=period, metrics=metrics)

    recipients = resolve_user_recipient(getattr(user, "email", "") or "")
    if not recipients:
        logger.warning(
            "WEEKLY_REPORT_SKIPPED: user=%s reason=no_usable_email", getattr(user, "id", None),
        )
        return None, False

    dedupe_key = weekly_report_dedupe_key(period.start_date, user.id)
    existing = EmailNotificationRepository.get_by_event(
        db, notification_type=TYPE_WEEKLY_REPORT, dedupe_key=dedupe_key,
    )

    row = EmailOutboxService.enqueue(
        db,
        notification_type=TYPE_WEEKLY_REPORT,
        dedupe_key=dedupe_key,
        recipients=recipients,
        subject=messages.weekly_report_subject(payload),
        payload=payload,
        organization_id=getattr(user, "organization_id", None),
        user_id=user.id,
    )
    if row is None:
        return None, False

    if existing is not None:
        logger.info(
            "WEEKLY_REPORT_ALREADY_QUEUED: user=%s week=%s notification=%s status=%s",
            user.id, period.start_date, row.id, existing.status,
        )
        return row.id, False

    logger.info(
        "WEEKLY_REPORT_QUEUED: user=%s week=%s→%s notification=%s status=%s",
        user.id, period.start_date, period.end_date, row.id, row.status,
    )
    return row.id, True


def build_weekly_report_preview(db: Session, *, user_id: int, week_start=None) -> Optional[dict[str, Any]]:
    """The payload one user's weekly report *would* carry, without queueing it.

    Shares every step of the real path — the same eligibility rule, the same
    period resolution, the same aggregation, the same payload shape — and
    stops short of the outbox. That is what makes a preview worth looking at:
    a separate rendering path could look perfect while the mailed one was
    wrong.

    Returns None when the id does not name an eligible user, so a preview
    cannot be used to read the productivity figures of a disabled or deleted
    account.
    """
    from app.services.weekly_report import (
        WeeklyReportService, build_week_metrics, previous_week, week_containing,
    )

    users = WeeklyReportService.eligible_users(db, user_id=user_id)
    if not users:
        return None
    user = users[0]

    organization_id = getattr(user, "organization_id", None)
    if organization_id is None:
        return None

    period = week_containing(week_start) if week_start else previous_week()
    metrics = build_week_metrics(
        db,
        organization_id=organization_id,
        user_ids=[user.id],
        period=period,
    )[user.id]
    return _weekly_report_payload(user=user, period=period, metrics=metrics)


# ----------------------------------------------------------------------
# Workflow 8 — monthly productivity report
# ----------------------------------------------------------------------

def monthly_report_dedupe_key(month_start, user_id: int) -> str:
    """The monthly report's identity: one person, one calendar month.

    The *month* is in the key, never the day the job ran — the same
    duplicate-protection story as the weekly key, one level up.
    """
    month = month_start.isoformat() if hasattr(month_start, "isoformat") else str(month_start)
    return f"month:{month}:user:{user_id}"


def _month_day_payload(day) -> Optional[dict[str, Any]]:
    if day is None:
        return None
    return {"label": day.label, "total_seconds": day.total_seconds}


def _monthly_report_payload(*, user, period, metrics) -> dict[str, Any]:
    """Everything the monthly email shows, and nothing else.

    The figures are checked to belong to `user`, exactly as the weekly payload
    is: a mismatched pair is refused rather than rendered.
    """
    if getattr(metrics, "user_id", None) != getattr(user, "id", None):
        raise ValueError(
            "Monthly report metrics do not belong to the recipient "
            f"(metrics user {getattr(metrics, 'user_id', None)!r}, "
            f"recipient {getattr(user, 'id', None)!r})."
        )

    return {
        "user_id": user.id,
        "name": getattr(user, "name", None),
        "month_start": period.start_date.isoformat(),
        "month_end": period.end_date.isoformat(),
        "period_label": period.label,
        "period_short": period.short_label,
        "timezone": period.timezone_name,
        "total_seconds": metrics.total_seconds,
        "average_activity": metrics.average_activity,
        "project_count": metrics.project_count,
        "working_days": metrics.working_days,
        "average_seconds_per_working_day": metrics.average_seconds_per_working_day,
        "most_productive_day": _month_day_payload(metrics.most_productive_day),
        "has_activity": metrics.has_activity,
        "can_view_all_time": bool(
            (getattr(user, "permissions", None) or {}).get("time_entries:view_all")
        ),
    }


def queue_monthly_report(db: Session, *, user, period, metrics) -> tuple[Optional[int], bool]:
    """Queue one user's monthly report. Returns ``(notification_id, created)``.

    One notification per user, addressed to that user alone, keyed on the
    month — see `queue_weekly_report` for why each of those holds.
    """
    payload = _monthly_report_payload(user=user, period=period, metrics=metrics)

    recipients = resolve_user_recipient(getattr(user, "email", "") or "")
    if not recipients:
        logger.warning(
            "MONTHLY_REPORT_SKIPPED: user=%s reason=no_usable_email", getattr(user, "id", None),
        )
        return None, False

    dedupe_key = monthly_report_dedupe_key(period.start_date, user.id)
    existing = EmailNotificationRepository.get_by_event(
        db, notification_type=TYPE_MONTHLY_REPORT, dedupe_key=dedupe_key,
    )

    row = EmailOutboxService.enqueue(
        db,
        notification_type=TYPE_MONTHLY_REPORT,
        dedupe_key=dedupe_key,
        recipients=recipients,
        subject=messages.monthly_report_subject(payload),
        payload=payload,
        organization_id=getattr(user, "organization_id", None),
        user_id=user.id,
    )
    if row is None:
        return None, False

    if existing is not None:
        logger.info(
            "MONTHLY_REPORT_ALREADY_QUEUED: user=%s month=%s notification=%s status=%s",
            user.id, period.start_date, row.id, existing.status,
        )
        return row.id, False

    logger.info(
        "MONTHLY_REPORT_QUEUED: user=%s month=%s→%s notification=%s status=%s",
        user.id, period.start_date, period.end_date, row.id, row.status,
    )
    return row.id, True


def build_monthly_report_preview(db: Session, *, user_id: int, month_start=None) -> Optional[dict[str, Any]]:
    """The payload one user's monthly report *would* carry, without queueing it."""
    from app.services.monthly_report import (
        MonthlyReportService, build_month_metrics, month_containing, previous_month,
    )

    users = MonthlyReportService.eligible_users(db, user_id=user_id)
    if not users:
        return None
    user = users[0]

    organization_id = getattr(user, "organization_id", None)
    if organization_id is None:
        return None

    period = month_containing(month_start) if month_start else previous_month()
    metrics = build_month_metrics(
        db, organization_id=organization_id, user_ids=[user.id], period=period,
    )[user.id]
    return _monthly_report_payload(user=user, period=period, metrics=metrics)


# ----------------------------------------------------------------------
# Workflow 9 & 10 — manual time request, and its decision
#
# Submitting a request tells the people who can decide it (admins, and the
# requester's leaders) and gives the requester a receipt. Deciding it tells
# the requester the outcome. The request row is committed before any of this
# runs and nothing here can undo it or fail the request: like every other
# entry point in this module these log and return, never raise.
# ----------------------------------------------------------------------

def manual_time_dedupe_key(entry_id: int, suffix: str) -> str:
    return f"manual:{entry_id}:{suffix}"


def _iso(value) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _reason_of(entry) -> Optional[str]:
    """The request's stored reason, or None when none was given."""
    reason = getattr(entry, "reason", None)
    return reason if isinstance(reason, str) and reason else None


def _manual_time_payload(db: Session, entry, requester) -> dict[str, Any]:
    """Everything the three emails show about one request, exactly as filed.

    Project and task are resolved to their names here, inside the entry's own
    organisation, and frozen into the payload — so the email describes the
    request as it was submitted even if a project is renamed before delivery.
    The description is carried verbatim; the template escapes it on render.
    """
    from app.models.project import Project
    from app.models.task import Task

    project = db.get(Project, entry.project_id)
    task = db.get(Task, entry.task_id)
    if project is not None and project.organization_id != entry.organization_id:
        project = None
    if task is not None and getattr(task, "organization_id", entry.organization_id) != entry.organization_id:
        task = None

    permissions = getattr(requester, "permissions", None) or {}
    return {
        "request_id": entry.id,
        "user_id": getattr(requester, "id", None),
        "name": getattr(requester, "name", None),
        "email": getattr(requester, "email", None),
        "project_name": getattr(project, "project_name", None) or f"Project {entry.project_id}",
        "task_name": getattr(task, "task_name", None) or f"Task {entry.task_id}",
        "work_date": _iso(entry.work_date),
        "start_time": _iso(entry.start_time),
        "end_time": _iso(entry.end_time),
        "total_seconds": int(entry.total_seconds or 0),
        "is_billable": bool(entry.is_billable),
        # The stored value, not its wording: the label is looked up when the
        # email is rendered, so the payload stays the record of what was filed.
        "reason": _reason_of(entry),
        "description": entry.description or "",
        "submitted_at": _iso(getattr(entry, "created_at", None) or datetime.now(timezone.utc)),
        "status": str(entry.approval_status or "pending"),
        "decided_at": _iso(getattr(entry, "approved_at", None)),
        # Which Time Tracking screen the requester's own button should open.
        # A plain boolean — the permission map never goes into an outbox row.
        "requester_can_view_directory": bool(permissions.get("view_employees")),
    }


def queue_manual_time_request_notifications(db: Session, entry) -> list[int]:
    """Queue the review request to every approver and the receipt to the requester.

    Returns the ids of the rows this call created or found, for the caller to
    schedule an immediate delivery attempt. Never raises.

    One row per recipient rather than one message to everybody: an address
    that bounces costs that one person their copy, and each approver's copy
    retries on its own schedule.
    """
    from app.repositories.manual_time_notification import ManualTimeNotificationRepository

    ids: list[int] = []
    try:
        from app.models.user import User
        requester = db.get(User, entry.user_id)
        payload = _manual_time_payload(db, entry, requester)

        approvers = ManualTimeNotificationRepository.list_approvers(
            db,
            organization_id=entry.organization_id,
            requester_id=entry.user_id,
            project_id=entry.project_id,
        )
        if not approvers:
            logger.warning(
                "MANUAL_TIME_REQUEST_EMAIL_NO_APPROVERS: entry=%s user=%s org=%s",
                entry.id, entry.user_id, entry.organization_id,
            )

        for approver in approvers:
            try:
                recipients = resolve_user_recipient(getattr(approver, "email", "") or "")
                if not recipients:
                    continue
                row = EmailOutboxService.enqueue(
                    db,
                    notification_type=TYPE_MANUAL_TIME_REQUEST,
                    dedupe_key=manual_time_dedupe_key(entry.id, f"approver:{approver.id}"),
                    recipients=recipients,
                    subject=messages.manual_time_request_subject(payload),
                    payload={**payload, "recipient_name": getattr(approver, "name", None)},
                    organization_id=entry.organization_id,
                    user_id=approver.id,
                )
                if row is not None:
                    ids.append(row.id)
            except Exception:  # noqa: BLE001 - one approver must not cost the others
                logger.warning(
                    "MANUAL_TIME_REQUEST_EMAIL_QUEUE_FAILED: entry=%s approver=%s",
                    entry.id, getattr(approver, "id", "?"), exc_info=True,
                )

        receipt_to = resolve_user_recipient(getattr(requester, "email", "") or "")
        if receipt_to:
            row = EmailOutboxService.enqueue(
                db,
                notification_type=TYPE_MANUAL_TIME_RECEIPT,
                dedupe_key=manual_time_dedupe_key(entry.id, "receipt"),
                recipients=receipt_to,
                subject=messages.manual_time_receipt_subject(payload),
                payload=payload,
                organization_id=entry.organization_id,
                user_id=entry.user_id,
            )
            if row is not None:
                ids.append(row.id)

        logger.info(
            "MANUAL_TIME_REQUEST_EMAILS_QUEUED: entry=%s user=%s approvers=%d notifications=%d",
            entry.id, entry.user_id, len(approvers), len(ids),
        )
    except Exception:  # noqa: BLE001 - the request is saved; the email is secondary
        logger.warning(
            "MANUAL_TIME_REQUEST_EMAILS_FAILED: entry=%s", getattr(entry, "id", "?"), exc_info=True,
        )
    return ids


def queue_manual_time_decision_notification(db: Session, entry, reviewer=None) -> Optional[int]:
    """Tell the requester their request was approved or rejected. Never raises.

    The recipient is the entry's own `user_id`, never anything the reviewer's
    request supplies — so an approver chooses which request to decide, and
    that choice alone determines who is written to.
    """
    try:
        status = str(entry.approval_status or "")
        if status not in ("approved", "rejected"):
            return None
        from app.models.user import User
        requester = db.get(User, entry.user_id)
        recipients = resolve_user_recipient(getattr(requester, "email", "") or "")
        if not recipients:
            logger.warning(
                "MANUAL_TIME_DECISION_EMAIL_SKIPPED: entry=%s user=%s reason=no_usable_email",
                entry.id, entry.user_id,
            )
            return None

        payload = _manual_time_payload(db, entry, requester)
        payload["reviewer_name"] = getattr(reviewer, "name", None)
        row = EmailOutboxService.enqueue(
            db,
            notification_type=TYPE_MANUAL_TIME_DECISION,
            dedupe_key=manual_time_dedupe_key(entry.id, status),
            recipients=recipients,
            subject=messages.manual_time_decision_subject(payload),
            payload=payload,
            organization_id=entry.organization_id,
            user_id=entry.user_id,
        )
        if row is None:
            return None
        logger.info(
            "MANUAL_TIME_DECISION_EMAIL_QUEUED: entry=%s user=%s status=%s notification=%s",
            entry.id, entry.user_id, status, row.id,
        )
        return row.id
    except Exception:  # noqa: BLE001 - the decision is committed; the email is secondary
        logger.warning(
            "MANUAL_TIME_DECISION_EMAIL_QUEUE_FAILED: entry=%s", getattr(entry, "id", "?"), exc_info=True,
        )
        return None


# ----------------------------------------------------------------------
# Workflow 11 — monthly project summary
# ----------------------------------------------------------------------

def queue_monthly_project_summary(db: Session, *, user, period, payload: dict[str, Any]) -> tuple[Optional[int], bool]:
    """Queue one recipient's monthly project summary. Returns ``(id, created)``.

    `payload` was built for *this* user's scope by
    `monthly_project_summary.summary_payload`; the pairing is checked here so
    a leader can never be sent a payload built for somebody else's scope.
    One notification per recipient, addressed to them alone.
    """
    if payload.get("user_id") != getattr(user, "id", None):
        raise ValueError(
            "Monthly project summary payload does not belong to the recipient "
            f"(payload user {payload.get('user_id')!r}, recipient {getattr(user, 'id', None)!r})."
        )
    recipients = resolve_user_recipient(getattr(user, "email", "") or "")
    if not recipients:
        logger.warning(
            "MONTHLY_PROJECT_SUMMARY_SKIPPED: user=%s reason=no_usable_email", getattr(user, "id", None),
        )
        return None, False

    dedupe_key = monthly_report_dedupe_key(period.start_date, user.id)
    existing = EmailNotificationRepository.get_by_event(
        db, notification_type=TYPE_MONTHLY_PROJECT_SUMMARY, dedupe_key=dedupe_key,
    )
    row = EmailOutboxService.enqueue(
        db,
        notification_type=TYPE_MONTHLY_PROJECT_SUMMARY,
        dedupe_key=dedupe_key,
        recipients=recipients,
        subject=messages.monthly_project_summary_subject(payload),
        payload=payload,
        organization_id=getattr(user, "organization_id", None),
        user_id=user.id,
    )
    if row is None:
        return None, False
    if existing is not None:
        logger.info(
            "MONTHLY_PROJECT_SUMMARY_ALREADY_QUEUED: user=%s month=%s notification=%s status=%s",
            user.id, period.start_date, row.id, existing.status,
        )
        return row.id, False
    logger.info(
        "MONTHLY_PROJECT_SUMMARY_QUEUED: user=%s month=%s notification=%s projects=%d",
        user.id, period.start_date, row.id,
        len(payload.get("fixed_projects") or []) + len(payload.get("flexible_projects") or []),
    )
    return row.id, True


# ----------------------------------------------------------------------
# Workflow 12 — fixed-hours budget alert
# ----------------------------------------------------------------------

def project_budget_alert_dedupe_key(project_id: int, budget_version: int, event: str, user_id: int) -> str:
    return f"project:{project_id}:v{budget_version}:{event}:user:{user_id}"


def queue_project_budget_alert(db: Session, *, user, project, event: str, payload: dict[str, Any]) -> Optional[int]:
    """Queue one recipient's copy of one budget event. Returns the outbox id.

    Only ever called for an event its caller has just claimed in
    `project_budget_alerts`; the per-recipient dedupe key additionally makes
    a re-queue after a crash collapse onto the row that already exists.
    """
    if payload.get("user_id") != getattr(user, "id", None) or payload.get("project_id") != project.id:
        raise ValueError("Budget alert payload does not belong to this recipient and project.")
    recipients = resolve_user_recipient(getattr(user, "email", "") or "")
    if not recipients:
        logger.warning("PROJECT_BUDGET_ALERT_SKIPPED: user=%s reason=no_usable_email", getattr(user, "id", None))
        return None
    row = EmailOutboxService.enqueue(
        db,
        notification_type=TYPE_PROJECT_BUDGET_ALERT,
        dedupe_key=project_budget_alert_dedupe_key(project.id, int(payload["budget_version"]), event, user.id),
        recipients=recipients,
        subject=messages.project_budget_alert_subject(payload),
        payload=payload,
        organization_id=project.organization_id,
        user_id=user.id,
    )
    if row is None:
        return None
    logger.info(
        "PROJECT_BUDGET_ALERT_QUEUED: project=%s event=%s user=%s notification=%s",
        project.id, event, user.id, row.id,
    )
    return row.id


# ----------------------------------------------------------------------
# Workflow 6 & 7 — client invitation and passwordless login link
#
# Both carry a bearer secret (an invitation token, a login handoff token) in
# the message they render, so neither goes through `EmailOutboxService`: its
# payload is a durable, plaintext row, and `EmailNotification`'s own contract
# says a queued row holds "only what the email shows... never a token, a
# password or a session identifier". These two are sent immediately instead —
# still never raising into the caller, exactly like every workflow above, but
# without a durable retry. A failed send is recovered by the caller offering
# "resend invitation" / "send another sign-in link", each of which mints a
# fresh secret rather than retrying the one that already went out.
# ----------------------------------------------------------------------

def _send_immediately(message) -> bool:
    try:
        get_email_provider().send(message)
        return True
    except Exception:  # noqa: BLE001 - never fail the caller over email
        logger.warning("EMAIL_IMMEDIATE_SEND_FAILED: subject=%r", message.subject, exc_info=True)
        return False


async def _send_immediately_in_background(message) -> bool:
    """`_send_immediately` for `BackgroundTasks`: SMTP waits under the bounded
    background limiter (`app.core.background`), not on a request thread."""
    return await run_blocking(_send_immediately, message)


def queue_client_invitation_email(
    db: Session, *, invitation, client, token: str, project_names: list[str], background_tasks=None,
) -> bool:
    """Send one client invitation, with its Approve/Reject links.

    Returns whether a send was attempted (queued to run, or sent). Does not
    guarantee delivery -- there is no durable retry here, by design; see the
    module note above.
    """
    try:
        recipients = resolve_user_recipient(client.email or "")
        if not recipients:
            logger.warning(
                "CLIENT_INVITATION_EMAIL_SKIPPED: client=%s reason=no_usable_email", client.id,
            )
            return False

        payload = {"token": token, "project_names": project_names}
        message = messages.build_client_invitation_email(payload, recipients)

        if background_tasks is not None:
            background_tasks.add_task(_send_immediately_in_background, message)
        else:
            _send_immediately(message)
        logger.info(
            "CLIENT_INVITATION_EMAIL_QUEUED: client=%s invitation=%s", client.id, invitation.id,
        )
        return True
    except Exception:  # noqa: BLE001 - creating the invitation must not fail over email
        logger.warning(
            "CLIENT_INVITATION_EMAIL_QUEUE_FAILED: client=%s", getattr(client, "id", "?"), exc_info=True,
        )
        return False


def queue_client_login_link_email(db: Session, user, handoff_token: str, background_tasks=None) -> bool:
    """Send one passwordless sign-in link to an approved client."""
    try:
        recipients = resolve_user_recipient(getattr(user, "email", "") or "")
        if not recipients:
            logger.warning(
                "CLIENT_LOGIN_LINK_EMAIL_SKIPPED: user=%s reason=no_usable_email",
                getattr(user, "id", None),
            )
            return False

        payload = {"handoff_token": handoff_token}
        message = messages.build_client_login_link_email(payload, recipients)

        if background_tasks is not None:
            background_tasks.add_task(_send_immediately_in_background, message)
        else:
            _send_immediately(message)
        logger.info("CLIENT_LOGIN_LINK_EMAIL_QUEUED: user=%s", getattr(user, "id", None))
        return True
    except Exception:  # noqa: BLE001 - requesting a link must not fail over email
        logger.warning(
            "CLIENT_LOGIN_LINK_EMAIL_QUEUE_FAILED: user=%s", getattr(user, "id", "?"), exc_info=True,
        )
        return False


# ----------------------------------------------------------------------
# Workflow 13 -- a notice about an employee's screenshot
# ----------------------------------------------------------------------


def screenshot_notice_dedupe_key(screenshot_id: int, sender_id: int, message: str, at: datetime) -> str:
    """One notice, however many times the button was pressed.

    Keyed on the screenshot, the sender and the text, bucketed to the minute:
    a double click or a retried request lands on the same row, while the same
    person sending a *different* notice about the same screenshot -- or the
    same words again tomorrow -- is a new email. Hashed, so the key stays short
    and never holds the text itself.
    """
    import hashlib

    digest = hashlib.sha256(" ".join((message or "").split()).encode("utf-8")).hexdigest()[:16]
    return f"screenshot:{screenshot_id}:from:{sender_id}:{digest}:{at.strftime('%Y%m%d%H%M')}"


def queue_screenshot_notice(
    db: Session,
    *,
    screenshot,
    recipient,
    sender,
    message: str,
    project_name: Optional[str] = None,
    task_name: Optional[str] = None,
) -> Optional[int]:
    """Queue a notice about `screenshot` to `recipient` (its owner).

    **The recipient is whoever the screenshot belongs to**, resolved by the
    caller from the screenshot's own time entry -- there is no address anywhere
    on this path for a request to set. The sender chooses *which screenshot*,
    and that choice alone decides who is written to.

    Like every entry point here it never raises into the caller and returns the
    queued id or None. The caller has already recorded that the notice was
    sent; delivery is what follows from it.
    """
    try:
        recipients = resolve_user_recipient(getattr(recipient, "email", "") or "")
        if not recipients:
            logger.warning(
                "SCREENSHOT_NOTICE_SKIPPED: screenshot=%s user=%s reason=recipient_has_no_usable_email",
                getattr(screenshot, "id", None), getattr(recipient, "id", None),
            )
            return None

        now = datetime.now(timezone.utc)
        captured_at = getattr(screenshot, "captured_at", None) or now
        payload: dict[str, Any] = {
            "screenshot_id": screenshot.id,
            # Read at send time to attach the picture; not the picture itself.
            "drive_file_id": getattr(screenshot, "google_drive_file_id", None),
            "captured_at": captured_at.isoformat() if hasattr(captured_at, "isoformat") else str(captured_at),
            "recipient_name": getattr(recipient, "name", None),
            "sender_name": getattr(sender, "name", None),
            "sender_role": (getattr(sender, "role_name", "") or "").replace("_", " ").title(),
            "sender_email": getattr(sender, "email", None),
            "message": message,
            "project_name": project_name,
            "task_name": task_name,
        }
        row = EmailOutboxService.enqueue(
            db,
            notification_type=TYPE_SCREENSHOT_NOTICE,
            dedupe_key=screenshot_notice_dedupe_key(screenshot.id, sender.id, message, now),
            recipients=recipients,
            subject=messages.screenshot_notice_subject(payload),
            payload=payload,
            organization_id=getattr(screenshot, "organization_id", None),
            user_id=getattr(recipient, "id", None),
        )
        if row is None:
            return None
        logger.info(
            "SCREENSHOT_NOTICE_QUEUED: screenshot=%s from=%s to=%s notification=%s state=%s",
            screenshot.id, getattr(sender, "id", None), getattr(recipient, "id", None), row.id, row.status,
        )
        return row.id
    except Exception:  # noqa: BLE001 - the notice is recorded; the email is what follows from it
        logger.warning(
            "SCREENSHOT_NOTICE_QUEUE_FAILED: screenshot=%s", getattr(screenshot, "id", "?"), exc_info=True,
        )
        return None
