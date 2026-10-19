"""Data access for `scheduled_job_runs`. No business rules live here."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models.scheduled_job_run import STATUS_RUNNING, ScheduledJobRun


class ScheduledJobRunRepository:

    @staticmethod
    def start(db: Session, *, job: str, dry_run: bool, host: Optional[str], now: datetime) -> int:
        row = ScheduledJobRun(job=job, status=STATUS_RUNNING, dry_run=dry_run, started_at=now, host=host)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id

    @staticmethod
    def finish(
        db: Session, *, run_id: int, status: str, summary: Optional[str],
        error: Optional[str], now: datetime,
    ) -> None:
        started = db.scalar(select(ScheduledJobRun.started_at).where(ScheduledJobRun.id == run_id))
        duration_ms = None
        if started is not None:
            if started.tzinfo is None:
                started = started.replace(tzinfo=now.tzinfo)
            duration_ms = max(0, int((now - started).total_seconds() * 1000))
        db.execute(
            update(ScheduledJobRun)
            .where(ScheduledJobRun.id == run_id)
            .values(status=status, summary=summary, error=error, finished_at=now, duration_ms=duration_ms)
        )
        db.commit()

    @staticmethod
    def latest_per_job(db: Session, *, include_dry_runs: bool = False) -> dict[str, ScheduledJobRun]:
        """The most recent run of every job that has ever recorded one."""
        latest = select(func.max(ScheduledJobRun.id)).group_by(ScheduledJobRun.job)
        if not include_dry_runs:
            latest = latest.where(ScheduledJobRun.dry_run.is_(False))
        rows = db.scalars(
            select(ScheduledJobRun).where(ScheduledJobRun.id.in_(latest))
        ).all()
        return {row.job: row for row in rows}

    @staticmethod
    def failures_since(db: Session, *, since: datetime) -> dict[str, int]:
        rows = db.execute(
            select(ScheduledJobRun.job, func.count(ScheduledJobRun.id))
            .where(ScheduledJobRun.status == "failed", ScheduledJobRun.started_at >= since)
            .group_by(ScheduledJobRun.job)
        ).all()
        return {job: count for job, count in rows}

    @staticmethod
    def prune(db: Session, *, older_than: datetime) -> int:
        """Drop rows older than `older_than`. The ledger is evidence, not an archive."""
        result = db.execute(delete(ScheduledJobRun).where(ScheduledJobRun.started_at < older_than))
        db.commit()
        return result.rowcount or 0
