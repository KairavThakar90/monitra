from typing import Optional

from pydantic import BaseModel, Field


class DispatchResult(BaseModel):
    """What one sweep of the email outbox did.

    Counts only — never a recipient, a subject or any part of a message body.
    This endpoint is triggered by a scheduler whose logs are not a place for
    the contents of somebody's feedback.
    """

    attempted: int = Field(0, description="Notifications this sweep claimed and tried.")
    sent: int = Field(0, description="Delivered on this sweep.")
    retrying: int = Field(0, description="Failed this time; still within their attempt budget.")
    failed: int = Field(0, description="Out of attempts. Parked with the reason recorded.")
    skipped: int = Field(0, description="Not due, or already claimed by another sweep.")
    unconfigured: int = Field(
        0, description="Not attempted because this deployment cannot send email."
    )
    error: int = Field(0, description="Raised unexpectedly; logged and left queued.")
    reason: Optional[str] = Field(
        None, description="Why the sweep did nothing, when it did nothing."
    )


class WeeklyReportRunResult(BaseModel):
    """What one weekly-report run queued.

    Counts and a period only — never a recipient, a name, or any figure from
    anybody's report. A scheduler's logs are not a place for staff productivity
    data, and this body is written straight into them.
    """

    week_start: str = Field(..., description="First day of the reported week (inclusive).")
    week_end: str = Field(..., description="Last day of the reported week (inclusive).")
    timezone: str = Field(..., description="Calendar the period was cut on.")
    eligible_users: int = Field(0, description="Active accounts with a usable address.")
    queued: int = Field(0, description="Reports newly queued by this run.")
    already_queued: int = Field(
        0, description="Reports this week already had — a retry or a re-run, and not an error.",
    )
    skipped: int = Field(0, description="No usable address, or no organization to report on.")
    failed: int = Field(0, description="Could not be queued. Logged, and retryable by re-running.")
    dry_run: bool = Field(False, description="Whether the run computed without queueing anything.")
    disabled: bool = Field(
        False, description="WEEKLY_REPORT_ENABLED is false, so nothing was queued.",
    )
