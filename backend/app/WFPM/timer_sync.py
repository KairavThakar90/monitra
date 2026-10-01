"""Monitra -> WFPM: a timer started here starts the timer there, and a timer
stopped here stops it.

    User starts (or stops) a task timer in Monitra
        -> the time entry is committed                    (TimeEntryService)
        -> the task's `wfpm_task_id` is looked up         (queue_timer_start / _stop)
        -> one `wfpm_timer_events` row is queued          (queue_timer_start / _stop)
        -> WFPM's endpoint is called with that id         (deliver_one)

The first step never waits for the last. Starting or stopping a timer is the
user's work and must succeed whether or not WFPM can be reached, so the event
is a row first and a request second, and delivery runs in two places:

* **Immediately after the response**, through FastAPI's `BackgroundTasks`. In
  the ordinary case WFPM is told within a moment of the timer starting, and a
  slow WFPM delays nobody. (A stop that happens outside a request -- a member
  being deactivated -- has no response to follow and waits for the sweeper.)
* **From the sweeper**, `POST /internal/wfpm/timer-events/dispatch`, on a
  timer. This is the guarantee: whatever the fast path missed -- the process
  restarted, WFPM was down, the network dropped -- is retried with backoff
  until WFPM accepts it, refuses it, or it runs out of attempts.

Nothing is queued for a task that has no `wfpm_task_id`, and nothing is
queued at all while an event's URL is unset -- `WFPM_TIMER_START_URL` for a
start, `WFPM_TIMER_STOP_URL` for a stop: the integration is off for that
event, and one queued against a URL that does not exist yet would be delivered
hours or days late the moment it did.

A stop is independent of its start: its own row, its own URL, its own
`event_id`. WFPM tolerates either arriving first (a stop with nothing running
is answered 200 and ignored; a late start already carries `stopped_at`), so
nothing here orders the two. docs/WFPM_INTEGRATION.md is the contract.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.WFPM import client as wfpm_client
from app.WFPM.client import WfpmDeliveryError, redact_error
from app.WFPM.models import (
    EVENT_TIMER_START, EVENT_TIMER_STOP, STATUS_FAILED, STATUS_PENDING, STATUS_REJECTED, WfpmTimerEvent,
)
from app.WFPM.repository import WfpmLinkRepository, WfpmTimerEventRepository

logger = logging.getLogger("uvicorn.error")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def url_for(event_type: str) -> str:
    """The endpoint an event of this type is POSTed to; empty when unset.

    The start and stop endpoints are separate URLs, each carrying its own key.
    """
    if event_type == EVENT_TIMER_STOP:
        return (settings.WFPM_TIMER_STOP_URL or "").strip()
    return (settings.WFPM_TIMER_START_URL or "").strip()


def unconfigured_reason(event_type: str = EVENT_TIMER_START) -> Optional[str]:
    """Why an event of this type is not sent to WFPM, or None if it is."""
    if event_type == EVENT_TIMER_STOP:
        if not settings.wfpm_timer_stop_configured:
            return "WFPM_TIMER_STOP_URL is not set"
    elif not settings.wfpm_timer_sync_configured:
        return "WFPM_TIMER_START_URL is not set"
    return None


def configured_event_types() -> list[str]:
    """The event types this deployment will currently send."""
    return [
        event_type for event_type in (EVENT_TIMER_START, EVENT_TIMER_STOP)
        if unconfigured_reason(event_type) is None
    ]


def describe_configuration() -> dict[str, Any]:
    """What this deployment will do, for the boot log and /health.

    Non-sensitive by construction: whether a URL and a token are present,
    never either value. `configured` is the start URL (the key existing
    monitors read); `stop_configured` is the stop URL.
    """
    configured = unconfigured_reason() is None
    description: dict[str, Any] = {
        "configured": configured,
        "stop_configured": unconfigured_reason(EVENT_TIMER_STOP) is None,
        "token_present": bool((settings.WFPM_API_TOKEN or "").strip()),
    }
    if not configured:
        description["reason"] = unconfigured_reason()
    return description


def retry_delay_seconds(attempt: int) -> float:
    """Seconds to wait before attempt number `attempt + 1`.

    Exponential, capped, and jittered. The jitter is the point: a WFPM outage
    queues every timer started during it behind the same clock, and without
    it they would all retry in the same second and land on a recovering
    server together.
    """
    base = max(1, settings.WFPM_TIMER_RETRY_BASE_DELAY_SECONDS)
    cap = max(base, settings.WFPM_TIMER_RETRY_MAX_DELAY_SECONDS)
    window = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(base / 2, window)


def event_id_for(event_type: str, time_entry_id: int) -> str:
    """The identity of an event, as WFPM sees it. Derived from the persisted
    time entry, so every attempt for one timer sends the same value."""
    return f"monitra:{event_type}:{time_entry_id}"


def build_payload(row: WfpmTimerEvent, entry: TimeEntry, user: Optional[User]) -> dict[str, Any]:
    """The JSON document WFPM receives. docs/WFPM_INTEGRATION.md is its
    contract -- change one and you must change the other.

    Built from stored facts at send time rather than frozen when queued, so a
    retry reports the entry as it then stands: `stopped_at` is null while the
    Monitra timer is still running and carries the stop instant when a delayed
    retry is delivered after the session has already ended, which lets WFPM
    record a finished session instead of starting a timer nothing will stop.

    A stop is a smaller document: WFPM finds the person by `monitra_user_id`
    (the `monitra_id` on their WFPM account) and does not use the email, so
    none is sent. `stopped_at` is required there and is read from the entry,
    which is the instant Monitra's own duration was computed from.
    """
    if row.event_type == EVENT_TIMER_STOP:
        if entry.end_time is None:
            # Queued only for an ended entry, and an ended entry stays ended.
            raise WfpmDeliveryError("The time entry has no end time to report", retryable=False)
        return {
            "event": row.event_type,
            "event_id": event_id_for(row.event_type, row.time_entry_id),
            "wfpm_task_id": row.wfpm_task_id,
            "wfpm_project_id": row.wfpm_project_id,
            "started_at": _iso(entry.start_time),
            "stopped_at": _iso(entry.end_time),
            "monitra_user_id": row.user_id,
            "monitra_time_entry_id": row.time_entry_id,
            "monitra_task_id": row.task_id,
            "monitra_project_id": row.project_id,
        }
    return {
        "event": row.event_type,
        "event_id": event_id_for(row.event_type, row.time_entry_id),
        "wfpm_task_id": row.wfpm_task_id,
        "wfpm_project_id": row.wfpm_project_id,
        "started_at": _iso(entry.start_time),
        "stopped_at": _iso(entry.end_time),
        "user_email": getattr(user, "email", None),
        "user_name": getattr(user, "name", None),
        "monitra_user_id": row.user_id,
        "monitra_time_entry_id": row.time_entry_id,
        "monitra_task_id": row.task_id,
        "monitra_project_id": row.project_id,
    }


class WfpmTimerSync:
    """Queue a timer event; deliver the queue. The only writer of its status."""

    # ------------------------------------------------------------------
    # Queueing
    # ------------------------------------------------------------------

    @staticmethod
    def queue_timer_start(db: Session, entry: TimeEntry) -> Optional[int]:
        """Record that WFPM must be told this timer started.

        Returns the id of the event to deliver now, or None when there is
        nothing to deliver: the integration is off, the task has no WFPM
        counterpart, or this entry's event already exists.

        **Never raises.** It is called from the timer-start request after the
        time entry is committed, and nothing about WFPM -- an unlinked task, a
        missing table, a database hiccup -- may turn a started timer into an
        error the user sees. A failure here is logged and the timer stands.
        """
        return WfpmTimerSync._queue(db, entry, EVENT_TIMER_START)

    @staticmethod
    def queue_timer_stop(db: Session, entry: TimeEntry) -> Optional[int]:
        """Record that WFPM must be told this timer stopped.

        Returns the id of the event to deliver now, or None when there is
        nothing to deliver: `WFPM_TIMER_STOP_URL` is unset, the task has no
        WFPM counterpart, the entry has not ended, or this entry's stop event
        already exists (one stop per time entry).

        **Never raises**, for the reason `queue_timer_start` does not: the
        stop has already been committed and is the user's work.
        """
        return WfpmTimerSync._queue(db, entry, EVENT_TIMER_STOP)

    @staticmethod
    def _queue(db: Session, entry: TimeEntry, event_type: str) -> Optional[int]:
        if unconfigured_reason(event_type) is not None:
            return None
        # Read before anything below can fail: a rollback expires the instance.
        entry_id = getattr(entry, "id", None)
        try:
            if event_type == EVENT_TIMER_STOP and entry.end_time is None:
                return None
            link = WfpmLinkRepository.timer_link(db, entry.task_id)
            if link is None:
                return None
            wfpm_task_id, wfpm_project_id = link
            row, created = WfpmTimerEventRepository.enqueue(
                db,
                event_type=event_type,
                organization_id=entry.organization_id,
                time_entry_id=entry.id,
                user_id=entry.user_id,
                project_id=entry.project_id,
                task_id=entry.task_id,
                wfpm_task_id=wfpm_task_id,
                wfpm_project_id=wfpm_project_id,
                max_attempts=max(1, settings.WFPM_TIMER_MAX_ATTEMPTS),
                next_attempt_at=_now(),
            )
            logger.info(
                "WFPM_TIMER_QUEUED: id=%s event=%s entry=%s task=%s wfpm_task=%s created=%s",
                row.id, event_type, entry_id, row.task_id, wfpm_task_id, created,
            )
            return row.id if created else None
        except Exception:  # noqa: BLE001 - a started or stopped timer must stay so
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                "WFPM_TIMER_QUEUE_FAILED: event=%s entry=%s (the timer %s; WFPM was not told)",
                event_type, entry_id, "stopped" if event_type == EVENT_TIMER_STOP else "started",
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    @staticmethod
    def deliver_one(db: Session, event_id: int) -> str:
        """Attempt one event. Returns what happened, for the logs and tally.

        One of: ``sent``, ``retrying``, ``failed`` (out of attempts),
        ``rejected`` (WFPM refused it), ``skipped`` (not due, or already
        handled by another worker) or ``unconfigured``.
        """
        now = _now()
        row = WfpmTimerEventRepository.get_by_id(db, event_id)
        if row is None or row.status != STATUS_PENDING:
            return "skipped"
        if unconfigured_reason(row.event_type) is not None:
            # Checked before claiming, so a deployment with the URL removed
            # does not spend an event's attempts on its own configuration.
            return "unconfigured"

        claimed = WfpmTimerEventRepository.claim(
            db,
            event_id=event_id,
            now=now,
            retry_at=now + timedelta(seconds=retry_delay_seconds(row.attempt_count + 1)),
        )
        if claimed is None:
            # Another worker got there first, or the row is not due yet.
            return "skipped"
        return WfpmTimerSync._send_claimed(db, claimed)

    @staticmethod
    def _send_claimed(db: Session, row: WfpmTimerEvent) -> str:
        """Send a row this worker already owns.

        Everything needed is read off the instance before the first write and
        nothing reads it afterwards: every `mark_*` commits, a commit expires
        the instance, and the next attribute access would re-SELECT a row that
        may since have been cascaded away.
        """
        event_id = row.id
        url = url_for(row.event_type)
        attempt = row.attempt_count
        max_attempts = row.max_attempts
        log_context = (
            f"id={event_id} event={row.event_type} entry={row.time_entry_id} "
            f"wfpm_task={row.wfpm_task_id} attempt={attempt}/{max_attempts}"
        )

        try:
            entry = db.get(TimeEntry, row.time_entry_id)
            if entry is None:
                # Deleted between queueing and sending. The cascade normally
                # removes the row with it; if it did not, there is still
                # nothing true left to tell WFPM.
                raise WfpmDeliveryError("The time entry no longer exists", retryable=False)
            payload = build_payload(row, entry, db.get(User, row.user_id))
            idempotency_key = payload["event_id"]
            response_status = wfpm_client.post_event(
                url,
                token=(settings.WFPM_API_TOKEN or "").strip(),
                payload=payload,
                idempotency_key=idempotency_key,
                timeout_seconds=settings.WFPM_REQUEST_TIMEOUT_SECONDS,
            )

        except WfpmDeliveryError as exc:
            error = redact_error(exc)
            if not exc.retryable:
                terminal = STATUS_REJECTED
            elif attempt >= max_attempts:
                terminal = STATUS_FAILED
            else:
                terminal = None
            WfpmTimerEventRepository.mark_attempt_failed(
                db, event_id=event_id, error=error,
                response_status=exc.status_code, terminal_status=terminal,
            )
            if terminal == STATUS_REJECTED:
                logger.error("WFPM_TIMER_REJECTED: %s error=%s", log_context, error)
                return "rejected"
            if terminal == STATUS_FAILED:
                logger.error("WFPM_TIMER_FAILED: %s error=%s", log_context, error)
                return "failed"
            logger.warning("WFPM_TIMER_RETRY: %s error=%s", log_context, error)
            return "retrying"

        except Exception as exc:  # noqa: BLE001 - a sweep must survive one bad row
            error = redact_error(exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            terminal = STATUS_FAILED if attempt >= max_attempts else None
            WfpmTimerEventRepository.mark_attempt_failed(
                db, event_id=event_id, error=error, response_status=None, terminal_status=terminal,
            )
            logger.error("WFPM_TIMER_UNEXPECTED_ERROR: %s error=%s", log_context, error, exc_info=True)
            return "failed" if terminal else "retrying"

        WfpmTimerEventRepository.mark_sent(
            db, event_id=event_id, now=_now(), response_status=response_status,
        )
        logger.info("WFPM_TIMER_SENT: %s status=%s", log_context, response_status)
        return "sent"

    @staticmethod
    def dispatch_pending(db: Session, limit: Optional[int] = None) -> dict:
        """Drain what is due, up to `limit` events. Never raises.

        Returns a tally by outcome. Called by the sweeper endpoint and safe to
        call concurrently with itself: every row is claimed before it is sent.
        """
        # Only events whose own URL is set: one left pending for want of a URL
        # must not sit at the head of the queue and starve the ones that can go.
        sendable = configured_event_types()
        if not sendable:
            return {"attempted": 0, "unconfigured": True}

        batch = limit or max(1, settings.WFPM_TIMER_DISPATCH_BATCH_SIZE)
        try:
            ids = WfpmTimerEventRepository.due_ids(
                db, now=_now(), limit=batch, event_types=sendable,
            )
        except Exception:  # noqa: BLE001
            logger.exception("WFPM_TIMER_SWEEP_QUERY_FAILED")
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return {"attempted": 0, "error": 1}

        tally: dict[str, int] = {}
        for event_id in ids:
            try:
                outcome = WfpmTimerSync.deliver_one(db, event_id)
            except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
                logger.exception("WFPM_TIMER_SWEEP_ROW_FAILED: id=%s", event_id)
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                outcome = "error"
            tally[outcome] = tally.get(outcome, 0) + 1

        result = {"attempted": len(ids), **tally}
        if ids:
            logger.info("WFPM_TIMER_SWEEP_COMPLETE: %s", result)
        return result


def deliver_in_background(event_id: int) -> None:
    """Deliver one event on its own database session.

    This is what `BackgroundTasks` runs after the timer-start or timer-stop
    response has been written. It opens its own session because the request's is closed by
    then, and it swallows everything: the row it was working on is still
    queued for the sweeper either way.
    """
    from app.core.database import get_session_local

    db = None
    try:
        db = get_session_local()()
        WfpmTimerSync.deliver_one(db, event_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "WFPM_TIMER_BACKGROUND_DISPATCH_FAILED: id=%s (the sweeper will retry it)",
            event_id, exc_info=True,
        )
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                logger.warning("WFPM_TIMER_BACKGROUND_SESSION_CLOSE_FAILED", exc_info=True)
