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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.email_notifications import require_dispatch_token
from app.core.config import settings
from app.core.database import get_db
from app.schemas.time_entry import ReconcileRunResult
from app.services.time_entry_reconciliation import TimeEntryReconciliationService

router = APIRouter(tags=["Time entries"])


def _run(
    db: Session, dry_run: Optional[bool], user_id: Optional[int], entry_id: Optional[List[int]],
    include_unreported: bool, limit: Optional[int], *, operator_allowed: bool = True,
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
    # Applies when explicitly asked (`dry_run=false`) or when the deployment
    # has switched the scheduled run on; otherwise it only reports.
    apply = dry_run is False or (dry_run is None and settings.TIMER_RECONCILER_ENABLED)
    result = TimeEntryReconciliationService.run(
        db, apply=apply, limit=limit, user_id=user_id, entry_ids=entry_id,
        include_unreported=include_unreported,
        decided_by="operator" if (include_unreported or dry_run is False) else "system:reconciler",
    )
    return ReconcileRunResult(**result)


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
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db, scope="function"),
):
    return _run(db, dry_run, user_id, entry_id, include_unreported, limit)


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
    """GET alias for schedulers that can only issue a GET (Vercel Cron).
    Kept out of the OpenAPI schema; it is the same authenticated,
    side-effecting scheduled pass as the POST -- but not the operator's
    named-entry closure, which is a POST only."""
    return _run(db, dry_run, user_id, entry_id, include_unreported, limit, operator_allowed=False)
