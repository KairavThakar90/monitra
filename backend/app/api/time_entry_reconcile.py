"""The scheduler-facing endpoint that closes abandoned timers.

``/internal/time-entries/reconcile``
    Looks at every running entry and closes the ones whose desktop has gone
    quiet (``TIMER_NO_SIGNAL_STOP_AFTER_SECONDS``) or whose idle prompt nobody
    answered (``TIMER_IDLE_UNANSWERED_STOP_AFTER_SECONDS``), at the last instant
    the evidence supports. The policy is docs/TIMING_MODEL.md section 9.

It is a scheduler's entry point, authenticated like the rest of ``/internal``
by ``EMAIL_DISPATCH_TOKEN``. It **only reports** unless told to apply: a
deployment starts with ``TIMER_RECONCILER_ENABLED`` off, so the first run
shows the owner exactly what it would close, and nothing is rewritten until
they turn it on -- or ask for one explicit run with ``dry_run=false``.
"""
from __future__ import annotations

from typing import List, Optional

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.email_notifications import require_dispatch_token
from app.core.config import settings
from app.core.database import get_db
from app.schemas.time_entry import ReconcileRunResult
from app.services.time_entry_reconciliation import TimeEntryReconciliationService

router = APIRouter(tags=["Time entries"])
logger = logging.getLogger(__name__)


def _require_operator_token(presented: Optional[str]) -> None:
    """Closing an entry by hand is the operator's act, not the scheduler's.

    The scheduler's token runs the scheduled pass and reads reports. An explicit
    `dry_run=false` -- the way a named entry is closed -- also needs this second secret,
    which exists only while somebody has approved a correction. Compared in constant
    time; refused (403) when none is configured, when it is absent or wrong, and when it
    is the scheduler's own, which would make it no second secret at all.
    """
    expected = (settings.TIMER_OPERATOR_TOKEN or "").strip()
    if not expected:
        logger.warning("RECONCILE_OPERATOR_DISABLED: TIMER_OPERATOR_TOKEN is not configured")
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Closing entries by hand is disabled: TIMER_OPERATOR_TOKEN is not set.",
        )
    if hmac.compare_digest(expected, (settings.EMAIL_DISPATCH_TOKEN or "").strip()):
        logger.warning("RECONCILE_OPERATOR_REFUSED: TIMER_OPERATOR_TOKEN equals EMAIL_DISPATCH_TOKEN")
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "TIMER_OPERATOR_TOKEN must differ from EMAIL_DISPATCH_TOKEN.",
        )
    if not presented or not hmac.compare_digest(presented.strip(), expected):
        logger.warning("RECONCILE_OPERATOR_REFUSED: missing or incorrect operator token")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The operator token is missing or incorrect.")


#: What the last run in THIS process saw -- counts only, never an entry, a user or a project.
#: It is what `/health` shows, so an operator can tell that the scheduler is actually calling
#: the endpoint (an installed task is not evidence that it runs). Per process: with several
#: workers each answers for the runs it served.
_LAST_RUN: dict = {}


def last_run_snapshot() -> Optional[dict]:
    return dict(_LAST_RUN) if _LAST_RUN else None


def _remember(result: ReconcileRunResult) -> None:
    from datetime import datetime, timezone

    _LAST_RUN.clear()
    _LAST_RUN.update(
        at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dry_run=result.dry_run, examined=result.examined, candidates=result.candidates,
        closed=result.closed, skipped=result.skipped, failed=result.failed,
        unreported_running=result.unreported_running,
    )


def _run(
    db: Session, dry_run: Optional[bool], user_id: Optional[int], entry_id: Optional[List[int]],
    include_unreported: bool, limit: Optional[int], *, operator_allowed: bool = True,
    read_only: bool = False, operator_token: Optional[str] = None,
) -> ReconcileRunResult:
    if (include_unreported or dry_run is False) and not operator_allowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Closing a named entry is an operator action: send it as a POST. "
            "A GET only runs the scheduled pass.",
        )
    if include_unreported and not entry_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "include_unreported closes a timer that never reported liveness, so it must "
            "name the entries (entry_id=...) it is approved for.",
        )
    if include_unreported and dry_run is None:
        # Omitted `dry_run` follows TIMER_RECONCILER_ENABLED, which would make a
        # request that was meant to look at one legacy row close it the day the
        # scheduled job is switched on.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "include_unreported must say what it means: dry_run=true to look, "
            "dry_run=false to close the named entries.",
        )
    if dry_run is False:
        _require_operator_token(operator_token)
    # Applies when explicitly asked (`dry_run=false`) or when the deployment
    # has switched the scheduled run on; otherwise it only reports.
    apply = dry_run is False or (dry_run is None and settings.TIMER_RECONCILER_ENABLED and not read_only)
    from app.services.scheduled_jobs import record_job_run

    # The scheduled pass is a run whether or not the deployment lets it apply
    # (report-only is its shipped mode); only an explicit `dry_run=true` is a
    # preview that must not count as the scheduler having fired.
    with record_job_run("timer-reconcile", dry_run=dry_run is True) as run:
        result = TimeEntryReconciliationService.run(
            db, apply=apply, limit=limit, user_id=user_id, entry_ids=entry_id,
            include_unreported=include_unreported,
            decided_by="operator" if (include_unreported or dry_run is False) else "system:reconciler",
        )
        result = ReconcileRunResult(**result)
        run.record({**result.model_dump(), "applied": apply, "read_only": read_only})
    _remember(result)
    return result


@router.post(
    "/internal/time-entries/reconcile",
    response_model=ReconcileRunResult,
    summary="Close timers whose desktop has gone quiet (scheduler only).",
    description=(
        "Reports -- and, when `TIMER_RECONCILER_ENABLED` is on or `dry_run=false` is "
        "passed, applies -- the closure of running entries that have stopped "
        "reporting in or whose idle prompt was never answered. Each closure is "
        "one transaction under a row lock, writes an audit row "
        "(`time_entry_reconciliations`) and ends the entry through the same path "
        "as a user's Stop. Safe to repeat and to run concurrently.\n\n"
        "An entry that has never reported liveness is never closed by a run that "
        "does not name it: `include_unreported=true` requires `entry_id` and an "
        "explicit `dry_run` (true to look, false to close) and is the operator's way "
        "to correct a specific legacy row after reviewing the report.\n\n"
        "Authenticate with `EMAIL_DISPATCH_TOKEN`."
    ),
    responses={
        400: {"description": "include_unreported without entry_id or without an explicit dry_run."},
        401: {"description": "Missing or incorrect dispatch token."},
        503: {"description": "EMAIL_DISPATCH_TOKEN is not configured."},
    },
)
def reconcile_time_entries(
    dry_run: Optional[bool] = Query(
        None, description="true: report only. false: apply. Omitted: follows TIMER_RECONCILER_ENABLED.",
    ),
    user_id: Optional[int] = Query(None, ge=1, description="Restrict to one member's entry."),
    entry_id: Optional[List[int]] = Query(None, description="Restrict to these entries."),
    include_unreported: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Most entries to close."),
    x_timer_operator_token: Optional[str] = Header(
        default=None,
        description="Required with dry_run=false: the operator secret (TIMER_OPERATOR_TOKEN).",
    ),
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db, scope="function"),
):
    return _run(db, dry_run, user_id, entry_id, include_unreported, limit, operator_token=x_timer_operator_token)


@router.get(
    "/internal/time-entries/reconcile",
    response_model=ReconcileRunResult,
    include_in_schema=False,
)
def reconcile_time_entries_get(
    dry_run: Optional[bool] = Query(None),
    user_id: Optional[int] = Query(None, ge=1),
    entry_id: Optional[List[int]] = Query(None),
    include_unreported: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1, le=1000),
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db, scope="function"),
):
    """GET alias for schedulers that can only issue a GET (Vercel Cron). Kept out of the
    OpenAPI schema. A GET is a safe method, so this one only ever REPORTS -- the same body as a
    dry run, even with `TIMER_RECONCILER_ENABLED` on. Applying (the scheduled pass or an
    operator's closure) is a POST: a deployment whose only scheduler can GET reports and leaves
    the closing to one that can POST."""
    return _run(
        db, dry_run, user_id, entry_id, include_unreported, limit,
        operator_allowed=False, read_only=True,
    )
