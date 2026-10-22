"""The scheduled jobs: what should fire, what did fire, and what that says about health.

Three things live here, and together they answer a question the backend could
not answer before 2026-10-19: *is the scheduler actually running?*

* **The registry** (`JOBS`). The schedule of record is the root `vercel.json`;
  the Linux timers and the Windows tasks are generated from it and tests keep
  them identical. This table is the same list, in code, so the backend knows
  how often each job is *supposed* to call it. `tests/test_scheduled_jobs.py`
  keeps it identical to `vercel.json` too.
* **The ledger** (`record_job_run`). Every `/internal/*` job wraps its work in
  it: a `scheduled_job_runs` row is written when the call starts and completed
  when it returns, on the job's own short session, so a job that dies half-way
  leaves a `running` row and a job that raises leaves `failed` with the reason.
  A ledger failure is logged and never fails the job -- the email matters more
  than the record of it.
* **The verdict** (`scheduler_status`). Latest real (non-dry-run) run of each
  job against how often it should fire. "Never ran", "stale" and "last run
  failed" are the three findings; `healthy` is true when there are none. It is
  what `/health?deep=1`, `/internal/jobs/status` and the preflight report.

Why a ledger and not a heartbeat in memory: the previous heartbeat was a dict in
the process. Every restart emptied it, every worker had its own, and a scheduler
that had never been installed on the new server looked exactly like one that had
just restarted. The ledger survives both, and records which host ran the job.
"""
from __future__ import annotations

import json
import logging
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional

from sqlalchemy.orm import Session

from app.models.scheduled_job_run import STATUS_FAILED, STATUS_OK, STATUS_RUNNING
from app.repositories.scheduled_job_run import ScheduledJobRunRepository

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class ScheduledJob:
    """One job as the timers know it: its name, its endpoint and its cron line (UTC)."""

    name: str
    endpoint: str
    cron: str
    #: How long after its last real run the job counts as not firing. Three
    #: missed firings for the frequent jobs; a day's grace for the rare ones.
    stale_after_seconds: int
    purpose: str


#: The job names are the ones `deploy/backend/scheduled-jobs-windows/Monitra-Job.ps1`
#: and `deploy/backend/scheduled-jobs/monitra-job.sh` use; the crons are vercel.json's.
JOBS: tuple[ScheduledJob, ...] = (
    ScheduledJob("email-dispatch", "/internal/email/dispatch", "*/5 * * * *", 15 * 60,
                 "deliver queued email: retries, and every scheduled report"),
    ScheduledJob("budget-alerts", "/internal/project-budget-alerts/run", "*/5 * * * *", 15 * 60,
                 "fixed-hours budget thresholds crossed by running timers"),
    ScheduledJob("weekly-report", "/internal/reports/weekly/run", "30 3 * * 1", 8 * 86400,
                 "queue the weekly productivity report (Monday 09:00 IST)"),
    ScheduledJob("monthly-report", "/internal/reports/monthly/run", "30 3 1 * *", 32 * 86400,
                 "queue the monthly productivity report (1st, 09:00 IST)"),
    ScheduledJob("monthly-project-summary", "/internal/reports/monthly-projects/run", "30 3 1 * *", 32 * 86400,
                 "queue the monthly project summary (1st, 09:00 IST)"),
    ScheduledJob("daily-rollup", "/internal/activity/daily-rollup", "50 18 * * *", 26 * 3600,
                 "store each user's end-of-day activity average"),
    ScheduledJob("wfpm-timer-dispatch", "/internal/wfpm/timer-events/dispatch", "*/1 * * * *", 10 * 60,
                 "deliver queued timer events to WFPM"),
    ScheduledJob("timer-reconcile", "/internal/time-entries/reconcile", "*/5 * * * *", 15 * 60,
                 "report (and, when enabled, close) timers whose desktop went quiet"),
)
JOBS_BY_NAME: dict[str, ScheduledJob] = {job.name: job for job in JOBS}

#: The rare jobs are allowed to have "never run" without the deployment being
#: unhealthy: a server installed on a Tuesday has no weekly run until Monday.
#: The frequent ones are not -- five minutes after installation they must exist.
_FREQUENT = frozenset(job.name for job in JOBS if job.cron.startswith("*/"))

#: Rows older than this are pruned by the daily job. Ninety days is enough to
#: see a monthly job's last three runs and far more than any incident needs.
LEDGER_RETENTION_DAYS = 90


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """A stored timestamp as an aware UTC instant.

    SQLite hands back naive values (stored as UTC); PostgreSQL hands back aware
    values in the connection's zone (IST on the company server). Everything
    this module reports is normalised to UTC so two fields never show the
    same instant with different offsets.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _host() -> Optional[str]:
    try:
        return socket.gethostname()[:120]
    except OSError:  # pragma: no cover - a host without a name
        return None


# ----------------------------------------------------------------------
# Next firing, from the cron shapes this project uses
# ----------------------------------------------------------------------

def next_fire(cron: str, after: datetime) -> datetime:
    """The first instant strictly after `after` at which `cron` (UTC) fires.

    Only the four shapes the installers accept are understood -- every N
    minutes, daily, weekly, monthly -- and anything else is a `ValueError`
    here rather than a job that silently never fires, the same stance
    `Install-MonitraJobs.ps1` takes.
    """
    fields = cron.split()
    if len(fields) != 5 or fields[3] != "*":
        raise ValueError(f"Unsupported cron expression: {cron!r}")
    minute, hour, dom, _, dow = fields
    after = _as_utc(after)

    if minute.startswith("*/") and hour == dom == dow == "*":
        every = int(minute[2:])
        base = after.replace(second=0, microsecond=0)
        candidate = base + timedelta(minutes=every - (base.minute % every))
        return candidate

    at_hour, at_minute = int(hour), int(minute)
    candidate = after.replace(hour=at_hour, minute=at_minute, second=0, microsecond=0)
    if dom == "*" and dow == "*":
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate
    if dom == "*":
        # cron is Sunday-first (0); Python is Monday-first (0).
        target = (int(dow) - 1) % 7
        for _ in range(8):
            if candidate.weekday() == target and candidate > after:
                return candidate
            candidate += timedelta(days=1)
            candidate = candidate.replace(hour=at_hour, minute=at_minute)
        raise AssertionError("unreachable")  # pragma: no cover
    day = int(dom)
    for _ in range(3):
        try:
            attempt = candidate.replace(day=day)
        except ValueError:
            attempt = None
        if attempt is not None and attempt > after:
            return attempt
        year, month = (candidate.year + (candidate.month // 12), candidate.month % 12 + 1)
        candidate = candidate.replace(year=year, month=month, day=1)
    raise AssertionError("unreachable")  # pragma: no cover


# ----------------------------------------------------------------------
# The ledger
# ----------------------------------------------------------------------

def counts_only(result: Any) -> dict[str, Any]:
    """A tally reduced to what may be stored: numbers, flags and short labels.

    Lists (notification ids), nested objects and long strings are dropped, so
    whatever a job returns, the ledger never holds a recipient, a name or a
    figure from somebody's report.
    """
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    if not isinstance(result, dict):
        return {}
    kept: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, bool) or isinstance(value, (int, float)):
            kept[str(key)] = value
        elif isinstance(value, str) and len(value) <= 64:
            kept[str(key)] = value
    return kept


class JobRun:
    """Handle the job uses to attach its tally to the ledger row."""

    def __init__(self, job: str) -> None:
        self.job = job
        self.id: Optional[int] = None
        self.summary: Optional[dict[str, Any]] = None

    def record(self, result: Any) -> None:
        self.summary = counts_only(result)


def _write(db: Session, action: Callable[[Session], Any]) -> Any:
    """One ledger write on the job's own session. Never raises: the job matters more.

    The session is rolled back first. At the start of a request that is a
    no-op; at the end it discards whatever the job left uncommitted -- which
    is exactly what `get_db`'s `close()` would have done a moment later, so the
    job's transactional behaviour is unchanged and the ledger row is the only
    thing this commits. A failed write is rolled back and logged.

    It is deliberately the request's session and not a second one: a second
    session, opened from the global session factory, would reach the real
    configured database even where the route's session is a test double, and
    a unit test must never be able to write to the development database.
    """
    try:
        db.rollback()
        return action(db)
    except Exception:  # noqa: BLE001 - a ledger problem must not stop an email
        logger.warning("SCHEDULED_JOB_LEDGER_FAILED", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _describe(error: BaseException) -> str:
    from app.services.email.provider import redact_error

    return redact_error(error, limit=500)


@contextmanager
def record_job_run(db: Session, job: str, *, dry_run: bool = False) -> Iterator[JobRun]:
    """Record one execution of `job` around the block that performs it.

    The row is written before the work starts and completed after it ends, on
    the job's own session (see `_write` for why not a second one). An exception
    is recorded as a failed run and re-raised unchanged.
    """
    run = JobRun(job)
    started = _now()
    run.id = _write(db, lambda session: ScheduledJobRunRepository.start(
        session, job=job, dry_run=dry_run, host=_host(), now=started,
    ))
    try:
        yield run
    except BaseException as error:
        if run.id is not None:
            summary = json.dumps(run.summary) if run.summary else None
            _write(db, lambda session: ScheduledJobRunRepository.finish(
                session, run_id=run.id, status=STATUS_FAILED, summary=summary,
                error=_describe(error), now=_now(),
            ))
        logger.error(
            "SCHEDULED_JOB_FAILED: job=%s run=%s dry_run=%s error=%s",
            job, run.id, dry_run, _describe(error),
        )
        raise
    finished = _now()
    if run.id is not None:
        summary = json.dumps(run.summary) if run.summary else None
        _write(db, lambda session: ScheduledJobRunRepository.finish(
            session, run_id=run.id, status=STATUS_OK, summary=summary, error=None, now=finished,
        ))
    logger.info(
        "SCHEDULED_JOB_RUN: job=%s run=%s dry_run=%s duration_ms=%d summary=%s",
        job, run.id, dry_run, int((finished - started).total_seconds() * 1000), run.summary,
    )


def prune_ledger(db: Session) -> int:
    """Drop ledger rows past retention. Called by the daily job; never raises."""
    cutoff = _now() - timedelta(days=LEDGER_RETENTION_DAYS)
    removed = _write(db, lambda session: ScheduledJobRunRepository.prune(session, older_than=cutoff))
    return int(removed or 0)


# ----------------------------------------------------------------------
# The verdict
# ----------------------------------------------------------------------

def scheduler_status(db: Session, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """What the ledger says about every registered job, and whether that is fine.

    Counts and timestamps only. `problems` is the list a person reads; `healthy`
    is `not problems`; `jobs` carries the detail per job.
    """
    now = _as_utc(now) or _now()
    latest = ScheduledJobRunRepository.latest_per_job(db, include_dry_runs=False)
    latest_any = ScheduledJobRunRepository.latest_per_job(db, include_dry_runs=True)
    failures = ScheduledJobRunRepository.failures_since(db, since=now - timedelta(hours=24))

    problems: list[str] = []
    jobs: dict[str, dict[str, Any]] = {}
    for job in JOBS:
        row = latest.get(job.name)
        info: dict[str, Any] = {
            "endpoint": job.endpoint,
            "cron_utc": job.cron,
            "purpose": job.purpose,
            "stale_after_seconds": job.stale_after_seconds,
            "never_ran": row is None,
            "stale": False,
            "last_started_at": None,
            "last_finished_at": None,
            "last_status": None,
            "last_duration_ms": None,
            "last_host": None,
            "last_summary": None,
            "last_error": None,
            "failures_last_24h": failures.get(job.name, 0),
            "next_expected_at": None,
        }
        if row is None:
            preview = latest_any.get(job.name)
            if preview is not None:
                info["last_dry_run_at"] = _as_utc(preview.started_at).isoformat(timespec="seconds")
            if job.name in _FREQUENT:
                problems.append(f"{job.name} has never run on this backend")
            info["next_expected_at"] = next_fire(job.cron, now).isoformat(timespec="seconds")
        else:
            started = _as_utc(row.started_at)
            age = (now - started).total_seconds()
            info.update(
                last_started_at=started.isoformat(timespec="seconds"),
                last_finished_at=(_as_utc(row.finished_at).isoformat(timespec="seconds") if row.finished_at else None),
                last_status=row.status,
                last_duration_ms=row.duration_ms,
                last_host=row.host,
                last_summary=(json.loads(row.summary) if row.summary else None),
                last_error=row.error,
                next_expected_at=next_fire(job.cron, started).isoformat(timespec="seconds"),
            )
            if age > job.stale_after_seconds:
                info["stale"] = True
                if row.status == STATUS_RUNNING:
                    # The process died (or was killed by the task's time limit) mid-run.
                    problems.append(f"{job.name} started {int(age // 60)} min ago and never finished")
                else:
                    problems.append(
                        f"{job.name} last ran {int(age // 3600)}h ago (expected {job.cron} UTC)"
                    )
            elif row.status == STATUS_FAILED:
                problems.append(f"{job.name} last run failed: {row.error or 'no detail'}")
        jobs[job.name] = info

    other = {
        name: {
            "last_started_at": _as_utc(row.started_at).isoformat(timespec="seconds"),
            "last_status": row.status,
            "dry_run": row.dry_run,
            "last_summary": (json.loads(row.summary) if row.summary else None),
        }
        for name, row in latest_any.items() if name not in JOBS_BY_NAME
    }

    return {
        "checked_at": now.isoformat(timespec="seconds"),
        "healthy": not problems,
        "problems": problems,
        "jobs": jobs,
        "other_jobs": other,
    }


# ----------------------------------------------------------------------
# Schema revision, for the same status endpoint
# ----------------------------------------------------------------------

def schema_revisions(db: Session) -> dict[str, Any]:
    """The database's Alembic revision(s) against the code's head.

    Reported by `/internal/jobs/status` so that "is production migrated?" can be
    answered through the API, from outside the server -- the question that could
    not be answered on 2026-10-22 because the production database is reachable
    only from the server itself. Never raises: a database without an
    `alembic_version` table reports `database: None`.
    """
    from sqlalchemy import text

    code_heads: list[str] = []
    try:
        import os

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        backend = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        code_heads = sorted(ScriptDirectory.from_config(Config(os.path.join(backend, "alembic.ini"))).get_heads())
    except Exception:  # noqa: BLE001 - a deployment without the alembic tree still answers
        logger.warning("SCHEMA_STATUS_CODE_HEAD_UNAVAILABLE", exc_info=True)

    database: Optional[list[str]]
    try:
        database = sorted(row[0] for row in db.execute(text("SELECT version_num FROM alembic_version")).all())
    except Exception:  # noqa: BLE001
        db.rollback()
        database = None

    return {
        "database": database,
        "code_head": code_heads,
        # None when the code head cannot be read (a deployment without the
        # alembic tree): "unknown", never a false mismatch.
        "at_head": (database == code_heads) if code_heads else None,
    }
