"""The end-of-day activity roll-up: the scheduler's trigger and the read.

``/internal/activity/daily-rollup`` is called by the platform scheduler once a
night (see the root ``vercel.json``), authenticated with the same dispatch
token as the other internal jobs. ``/activity/daily-summaries``
is the authenticated read of what it stored.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.email_notifications import require_dispatch_token
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.daily_activity_summary import (
    DailyActivityRollupResult,
    DailyActivitySummaryList,
    DailyActivitySummaryRead,
)
from app.services.daily_activity_summary import (
    DEFAULT_DAYS_PER_RUN, MAX_DAYS_PER_RUN, DailyActivitySummaryService,
)

router = APIRouter(tags=["Time Entry Activity"])


def _run(day, days, user_id, dry_run, db):
    return DailyActivityRollupResult(**DailyActivitySummaryService.rollup(
        db, day=day, days=days, user_id=user_id, dry_run=dry_run,
    ))


@router.post(
    "/internal/activity/daily-rollup",
    response_model=DailyActivityRollupResult,
    summary="Store each user's duration-weighted activity for the closed day(s) (scheduler only).",
    description=(
        "Recomputes, from the day's `time_entry_activity` windows, one row per "
        "user per day: the duration-weighted average activity and the counts it "
        "was made from. Defaults to yesterday and the day before in the reporting "
        "timezone, so a desktop that uploaded late is corrected on the next run. "
        "Idempotent: a retry rewrites the same rows.\n\n"
        "Authenticate with `EMAIL_DISPATCH_TOKEN`, in either the "
        "`X-Email-Dispatch-Token` header or as a bearer token."
    ),
    responses={
        401: {"description": "Missing or incorrect dispatch token."},
        422: {"description": "The requested day has not ended yet."},
        503: {"description": "EMAIL_DISPATCH_TOKEN is not configured."},
    },
)
def run_daily_rollup(
    day: Optional[date] = Query(None, description="The last (most recent) day to roll up. Defaults to yesterday."),
    days: int = Query(DEFAULT_DAYS_PER_RUN, ge=1, le=MAX_DAYS_PER_RUN, description="How many days back from `day`, inclusive."),
    user_id: Optional[int] = Query(None, ge=1, description="Restrict the run to one user."),
    dry_run: bool = Query(False, description="Compute and report without writing."),
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db),
):
    return _run(day, days, user_id, dry_run, db)


@router.get(
    "/internal/activity/daily-rollup",
    response_model=DailyActivityRollupResult,
    include_in_schema=False,
    summary="Store the closed day's activity averages (scheduler only).",
)
def run_daily_rollup_get(
    day: Optional[date] = Query(None),
    days: int = Query(DEFAULT_DAYS_PER_RUN, ge=1, le=MAX_DAYS_PER_RUN),
    user_id: Optional[int] = Query(None, ge=1),
    dry_run: bool = Query(False),
    _: None = Depends(require_dispatch_token),
    db: Session = Depends(get_db),
):
    """GET alias for schedulers that can only issue a GET (Vercel Cron)."""
    return _run(day, days, user_id, dry_run, db)


@router.get(
    "/activity/daily-summaries",
    response_model=DailyActivitySummaryList,
    summary="The stored end-of-day activity averages for a member.",
    description=(
        "One row per closed day: the duration-weighted average activity and the "
        "keyboard/mouse counts it was computed from. A member reads their own; "
        "other members are visible under the same rule as the member directory."
    ),
)
def list_daily_summaries(
    start_date: date = Query(..., description="First day, inclusive."),
    end_date: date = Query(..., description="Last day, inclusive."),
    user_id: Optional[int] = Query(None, ge=1, description="Defaults to the caller."),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = DailyActivitySummaryService.list_summaries(
        db, current_user, user_id=user_id, start=start_date, end=end_date,
    )
    return DailyActivitySummaryList(items=[DailyActivitySummaryRead.model_validate(r) for r in rows])
