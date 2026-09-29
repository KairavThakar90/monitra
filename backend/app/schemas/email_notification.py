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


class ProjectBudgetAlertRunResult(BaseModel):
    """What one budget-alert evaluation did. Counts only -- no project names or figures."""

    execution_id: str
    source: str
    timestamp: str
    projects_scanned: int = Field(0, description="Monitored fixed-hours projects evaluated.")
    thresholds_detected: int = Field(0, description="Crossed events not yet recorded when this run started.")
    claimed: int = Field(0, description="Events this run claimed and notified.")
    baselined: int = Field(0, description="Events already passed when a budget was first evaluated; recorded silently.")
    would_notify: int = Field(0, description="Dry run only: events that would be notified.")
    queued: int = Field(0, description="Recipient emails queued.")
    requeued: int = Field(0, description="Earlier events whose emails were finished by this run.")
    sent: int = Field(0, description="Emails this run delivered itself.")
    send_failed: int = Field(0, description="Immediate deliveries that failed; the sweeper retries them.")
    skipped: int = Field(0, description="Projects another evaluation was initialising.")
    failed: int = Field(0, description="Projects or recipients that errored; logged.")
    dry_run: bool = False
    disabled: bool = Field(False, description="PROJECT_BUDGET_ALERTS_ENABLED is false.")
    duration_ms: int = 0


class MonthlyProjectSummaryRunResult(BaseModel):
    """What one monthly-project-summary run queued. Counts and a period only —
    never a recipient, a project name or a figure."""

    execution_id: str = Field(..., description="Identifies this run in the logs.")
    month_start: str = Field(..., description="First day of the reported month.")
    month_end: str = Field(..., description="Last day of the reported month.")
    timezone: str = Field(..., description="Calendar the period was cut on.")
    organizations: int = Field(0, description="Organisations summarised.")
    projects: int = Field(0, description="Projects with activity in the month, across organisations.")
    eligible_recipients: int = Field(0, description="Admins, owners and leaders to be sent a summary.")
    queued: int = Field(0, description="Summaries newly queued by this run.")
    already_queued: int = Field(0, description="Summaries this month already had — a retry or re-run.")
    skipped: int = Field(0, description="No usable address, or no organization.")
    failed: int = Field(0, description="Could not be queued. Logged, and retryable by re-running.")
    dry_run: bool = Field(False, description="Whether the run computed without queueing anything.")
    disabled: bool = Field(False, description="MONTHLY_PROJECT_SUMMARY_ENABLED is false.")
    duration_ms: int = Field(0, description="Wall-clock time the run took.")


class MonthlyReportRunResult(BaseModel):
    """What one monthly-report run queued. Counts and a period only."""

    month_start: str = Field(..., description="First day of the reported month.")
    month_end: str = Field(..., description="Last day of the reported month.")
    timezone: str = Field(..., description="Calendar the period was cut on.")
    eligible_users: int = Field(0, description="Active accounts with a usable address.")
    queued: int = Field(0, description="Reports newly queued by this run.")
    already_queued: int = Field(
        0, description="Reports this month already had — a retry or a re-run, and not an error.",
    )
    skipped: int = Field(0, description="No usable address, or no organization to report on.")
    failed: int = Field(0, description="Could not be queued. Logged, and retryable by re-running.")
    dry_run: bool = Field(False, description="Whether the run computed without queueing anything.")
    disabled: bool = Field(
        False, description="MONTHLY_REPORT_ENABLED is false, so nothing was queued.",
    )
