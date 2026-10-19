"""The scheduled-job ledger: one row per execution of an `/internal/*` job.

A scheduled job is an HTTP endpoint that a timer on the host calls. Nothing in
the backend owns a clock, so nothing in the backend could say whether the timer
exists, whether it fires, or whether it fires on the right server -- the only
heartbeat was a per-process dict that every restart emptied. This table is the
durable answer to "did the job actually run": written at the start of every
call and completed at the end, by the job itself, on its own short session.

Counts only. `summary` holds the tally the endpoint returned (how many queued,
sent, retried, closed), never a recipient, a project name or a figure from
anybody's report.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Identity, Index, Integer, String, Text, TIMESTAMP, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Written when the run begins; the row stays like this if the process died.
STATUS_RUNNING = "running"
#: The endpoint returned normally. Its tally is in `summary`.
STATUS_OK = "ok"
#: The endpoint raised. What it raised (redacted) is in `error`.
STATUS_FAILED = "failed"


class ScheduledJobRun(Base):
    """One execution of one scheduled job, from first instruction to return."""

    __tablename__ = "scheduled_job_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    #: The job name the timers use (`email-dispatch`, `weekly-report`, ...),
    #: as registered in `app.services.scheduled_jobs.JOBS`.
    job: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'running'"),
    )
    #: A dry run computes and changes nothing, so it must not count as the
    #: scheduler having fired: an operator's preview would otherwise hide a
    #: missing timer for a week.
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
    )
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(),
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: JSON object of counts, as returned by the endpoint. Never content.
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Why the run failed, redacted and truncated. Never a credential.
    error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    #: The machine the backend process ran on, so a job fired against the
    #: wrong server (a developer's copy pointed at production) is visible.
    host: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    __table_args__ = (
        # The only query: the latest run of each job.
        Index("idx_scheduled_job_runs_job_started", "job", "started_at"),
    )
