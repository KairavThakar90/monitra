"""The scheduled-job ledger answers "did the scheduler actually run?" -- durably.

Before 2026-10-19 the only heartbeat was a dict in the process: a scheduler that
had never been installed on the new production server looked exactly like one
that had just restarted, and every scheduled email stopped for weeks without a
single check saying so. These tests cover what replaced it: the ledger row each
job writes, the verdict drawn from it, the next-firing arithmetic, and the
detection of report periods that were never queued.
"""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models.email_notification import EmailNotification
from app.models.scheduled_job_run import STATUS_FAILED, STATUS_OK, ScheduledJobRun
from app.repositories.scheduled_job_run import ScheduledJobRunRepository
from app.services.scheduled_jobs import (
    JOBS_BY_NAME, counts_only, next_fire, prune_ledger, record_job_run, scheduler_status,
)
from tests.test_project_hours_summary import _sqlite_schema

UTC = timezone.utc


def _engine():
    engine = create_engine("sqlite://")
    _sqlite_schema(engine, ScheduledJobRun, EmailNotification)
    return engine


class LedgerCase(unittest.TestCase):

    def setUp(self):
        self.engine = _engine()
        self.factory = sessionmaker(bind=self.engine)

    def rows(self):
        with self.factory() as db:
            return db.scalars(select(ScheduledJobRun).order_by(ScheduledJobRun.id)).all()


class TestRecordJobRun(LedgerCase):

    def test_a_successful_run_leaves_an_ok_row_with_its_tally(self):
        with self.factory() as db, record_job_run(db, "email-dispatch") as run:
            run.record({"attempted": 3, "sent": 3, "notification_ids": [1, 2, 3], "reason": None})
        [row] = self.rows()
        self.assertEqual((row.job, row.status, row.dry_run), ("email-dispatch", STATUS_OK, False))
        self.assertIsNotNone(row.finished_at)
        self.assertGreaterEqual(row.duration_ms, 0)
        self.assertTrue(row.host)
        # Lists never reach the ledger: ids, recipients, anything shaped like content.
        self.assertEqual(json.loads(row.summary), {"attempted": 3, "sent": 3})

    def test_an_exception_is_recorded_as_failed_and_re_raised(self):
        with self.assertRaises(RuntimeError), self.factory() as db:
            with record_job_run(db, "weekly-report"):
                raise RuntimeError("boom")
        [row] = self.rows()
        self.assertEqual(row.status, STATUS_FAILED)
        self.assertIn("RuntimeError: boom", row.error)

    def test_the_error_text_is_redacted(self):
        from unittest.mock import patch

        from app.core.config import settings

        with patch.object(settings, "SMTP_PASSWORD", "hunter2secret"):
            with self.assertRaises(RuntimeError), self.factory() as db:
                with record_job_run(db, "email-dispatch"):
                    raise RuntimeError("login failed for hunter2secret")
        [row] = self.rows()
        self.assertNotIn("hunter2secret", row.error)
        self.assertIn("[redacted]", row.error)

    def test_a_dry_run_is_marked_as_such(self):
        with self.factory() as db, record_job_run(db, "monthly-report", dry_run=True):
            pass
        self.assertTrue(self.rows()[0].dry_run)

    def test_a_ledger_failure_never_fails_the_job(self):
        """A session without the table (a test double, an unmigrated database): the job
        still runs and returns; the failure is logged, and the session is left usable."""
        engine = create_engine("sqlite://")
        _sqlite_schema(engine, EmailNotification)  # no scheduled_job_runs table
        with sessionmaker(bind=engine)() as db:
            with record_job_run(db, "email-dispatch") as run:
                run.record({"sent": 1})
            self.assertIsNone(run.id)
            self.assertEqual(db.scalar(select(func.count(EmailNotification.id))), 0)

    def test_the_jobs_uncommitted_work_is_not_committed_by_the_ledger(self):
        """Exactly what `get_db`'s close() does today: anything the job left uncommitted is
        discarded. The ledger must not turn a dry run's uncommitted writes into real ones."""
        with self.factory() as db:
            with record_job_run(db, "budget-alerts", dry_run=True):
                db.add(EmailNotification(
                    notification_type="weekly_report", dedupe_key="week:2026-10-12:user:1", recipients="[]",
                    subject="s", payload="{}", status="pending", attempt_count=0, max_attempts=6,
                    next_attempt_at=datetime.now(UTC), created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
                ))
                db.flush()
        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count(EmailNotification.id))), 0)
        self.assertEqual(self.rows()[0].status, STATUS_OK)

    def test_prune_drops_only_rows_past_retention(self):
        with self.factory() as db:
            ScheduledJobRunRepository.start(db, job="email-dispatch", dry_run=False, host="h",
                                            now=datetime.now(UTC) - timedelta(days=120))
            ScheduledJobRunRepository.start(db, job="email-dispatch", dry_run=False, host="h", now=datetime.now(UTC))
            self.assertEqual(prune_ledger(db), 1)
        self.assertEqual(len(self.rows()), 1)


class TestCountsOnly(unittest.TestCase):

    def test_keeps_numbers_flags_and_short_labels_only(self):
        self.assertEqual(
            counts_only({"queued": 2, "dry_run": False, "week_start": "2026-10-12", "ids": [1], "nested": {"a": 1},
                         "long": "x" * 65, "ratio": 0.5}),
            {"queued": 2, "dry_run": False, "week_start": "2026-10-12", "ratio": 0.5},
        )

    def test_accepts_pydantic_models_and_rejects_non_dicts(self):
        from app.schemas.email_notification import DispatchResult

        self.assertEqual(counts_only(DispatchResult(sent=1))["sent"], 1)
        self.assertEqual(counts_only("nope"), {})


class TestNextFire(unittest.TestCase):

    def test_every_n_minutes(self):
        after = datetime(2026, 10, 19, 9, 3, 20, tzinfo=UTC)
        self.assertEqual(next_fire("*/5 * * * *", after), datetime(2026, 10, 19, 9, 5, tzinfo=UTC))
        self.assertEqual(next_fire("*/5 * * * *", datetime(2026, 10, 19, 9, 5, tzinfo=UTC)),
                         datetime(2026, 10, 19, 9, 10, tzinfo=UTC))
        self.assertEqual(next_fire("*/1 * * * *", after), datetime(2026, 10, 19, 9, 4, tzinfo=UTC))

    def test_daily(self):
        self.assertEqual(next_fire("50 18 * * *", datetime(2026, 10, 19, 9, 0, tzinfo=UTC)),
                         datetime(2026, 10, 19, 18, 50, tzinfo=UTC))
        self.assertEqual(next_fire("50 18 * * *", datetime(2026, 10, 19, 18, 50, tzinfo=UTC)),
                         datetime(2026, 10, 20, 18, 50, tzinfo=UTC))

    def test_weekly_uses_cron_sunday_first_numbering(self):
        # 2026-10-19 is a Monday. The run fires at 03:30 UTC; after that instant, next Monday.
        monday_0330 = datetime(2026, 10, 19, 3, 30, tzinfo=UTC)
        self.assertEqual(next_fire("30 3 * * 1", monday_0330 - timedelta(minutes=1)), monday_0330)
        self.assertEqual(next_fire("30 3 * * 1", monday_0330), datetime(2026, 10, 26, 3, 30, tzinfo=UTC))
        self.assertEqual(next_fire("30 3 * * 0", monday_0330), datetime(2026, 10, 25, 3, 30, tzinfo=UTC))

    def test_monthly_including_year_rollover(self):
        self.assertEqual(next_fire("30 3 1 * *", datetime(2026, 10, 19, tzinfo=UTC)),
                         datetime(2026, 11, 1, 3, 30, tzinfo=UTC))
        self.assertEqual(next_fire("30 3 1 * *", datetime(2026, 12, 1, 3, 30, tzinfo=UTC)),
                         datetime(2027, 1, 1, 3, 30, tzinfo=UTC))

    def test_unsupported_shapes_are_refused_not_guessed(self):
        for cron in ("0 9 * * 1-5", "0 9 1 6 *", "0 9 * *"):
            with self.subTest(cron=cron), self.assertRaises(ValueError):
                next_fire(cron, datetime(2026, 10, 19, tzinfo=UTC))

    def test_naive_timestamps_are_read_as_utc(self):
        self.assertEqual(next_fire("*/5 * * * *", datetime(2026, 10, 19, 9, 3)),
                         datetime(2026, 10, 19, 9, 5, tzinfo=UTC))


class TestSchedulerStatus(LedgerCase):

    def _start(self, job, *, ago, status=STATUS_OK, dry_run=False, error=None):
        now = datetime.now(UTC)
        with self.factory() as db:
            run_id = ScheduledJobRunRepository.start(db, job=job, dry_run=dry_run, host="prod-1", now=now - ago)
            ScheduledJobRunRepository.finish(db, run_id=run_id, status=status, summary='{"sent": 1}',
                                             error=error, now=now - ago + timedelta(seconds=2))

    def test_a_backend_nothing_has_called_is_unhealthy_and_says_which_jobs_never_ran(self):
        with self.factory() as db:
            verdict = scheduler_status(db)
        self.assertFalse(verdict["healthy"])
        self.assertTrue(all(info["never_ran"] for info in verdict["jobs"].values()))
        # Frequent jobs are problems the moment they are missing; rare ones wait for their day.
        self.assertIn("email-dispatch has never run on this backend", verdict["problems"])
        self.assertNotIn("weekly-report has never run on this backend", verdict["problems"])
        self.assertIsNotNone(verdict["jobs"]["weekly-report"]["next_expected_at"])

    def test_fresh_runs_everywhere_is_healthy(self):
        for name in JOBS_BY_NAME:
            self._start(name, ago=timedelta(minutes=1))
        with self.factory() as db:
            verdict = scheduler_status(db)
        self.assertTrue(verdict["healthy"], verdict["problems"])
        self.assertEqual(verdict["jobs"]["email-dispatch"]["last_summary"], {"sent": 1})
        self.assertEqual(verdict["jobs"]["email-dispatch"]["last_host"], "prod-1")

    def test_a_stale_frequent_job_is_a_problem_and_a_recent_rare_one_is_not(self):
        for name in JOBS_BY_NAME:
            self._start(name, ago=timedelta(minutes=1))
        self._start("email-dispatch", ago=timedelta(hours=3))  # later row, older start: the newest wins
        with self.factory() as db:
            # Make the newest email-dispatch row the stale one.
            for row in db.scalars(select(ScheduledJobRun).where(ScheduledJobRun.job == "email-dispatch")):
                row.started_at = datetime.now(UTC) - timedelta(hours=3)
            db.commit()
            verdict = scheduler_status(db)
        self.assertTrue(verdict["jobs"]["email-dispatch"]["stale"])
        self.assertFalse(verdict["jobs"]["weekly-report"]["stale"])
        self.assertFalse(verdict["healthy"])
        self.assertTrue(any(p.startswith("email-dispatch last ran") for p in verdict["problems"]))

    def test_a_dry_run_does_not_count_as_the_scheduler_firing(self):
        for name in JOBS_BY_NAME:
            self._start(name, ago=timedelta(minutes=1))
        with self.factory() as db:
            db.query(ScheduledJobRun).filter(ScheduledJobRun.job == "weekly-report").delete()
            db.commit()
        self._start("weekly-report", ago=timedelta(minutes=1), dry_run=True)
        with self.factory() as db:
            verdict = scheduler_status(db)
        self.assertTrue(verdict["jobs"]["weekly-report"]["never_ran"])
        self.assertIn("last_dry_run_at", verdict["jobs"]["weekly-report"])

    def test_a_failed_last_run_is_reported_with_its_reason(self):
        for name in JOBS_BY_NAME:
            self._start(name, ago=timedelta(minutes=1))
        self._start("budget-alerts", ago=timedelta(seconds=30), status=STATUS_FAILED, error="ValueError: x")
        with self.factory() as db:
            verdict = scheduler_status(db)
        self.assertIn("budget-alerts last run failed: ValueError: x", verdict["problems"])
        self.assertEqual(verdict["jobs"]["budget-alerts"]["failures_last_24h"], 1)

    def test_on_demand_jobs_are_listed_separately(self):
        self._start("report-catch-up", ago=timedelta(minutes=1), dry_run=True)
        with self.factory() as db:
            verdict = scheduler_status(db)
        self.assertIn("report-catch-up", verdict["other_jobs"])
        self.assertNotIn("report-catch-up", verdict["jobs"])


class TestMissingReportPeriods(LedgerCase):

    def _queue(self, notification_type, key):
        with self.factory() as db:
            db.add(EmailNotification(
                notification_type=notification_type, dedupe_key=key, recipients="[]", subject="s",
                payload="{}", status="sent", attempt_count=1, max_attempts=6,
                next_attempt_at=datetime.now(UTC), created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
            ))
            db.commit()

    def test_completed_periods_are_counted_back_from_the_previous_one(self):
        from app.services.report_catch_up import completed_months, completed_weeks

        # Reference Monday 2026-10-19: the previous completed week starts 2026-10-12.
        weeks = completed_weeks(weeks_back=3, reference=date(2026, 10, 19))
        self.assertEqual([w.start_date.isoformat() for w in weeks], ["2026-10-12", "2026-10-05", "2026-09-28"])
        months = completed_months(months_back=3, reference=date(2026, 10, 19))
        self.assertEqual([m.start_date.isoformat() for m in months], ["2026-09-01", "2026-08-01", "2026-07-01"])
        self.assertEqual(len(completed_weeks(weeks_back=99)), 12, "capped")

    def test_a_period_with_any_row_is_not_missing_and_one_with_none_is(self):
        from app.services.report_catch_up import missing_report_periods

        self._queue("weekly_report", "week:2026-10-05:user:7")
        self._queue("monthly_report", "month:2026-09-01:user:7")
        with self.factory() as db:
            missing = missing_report_periods(db, weeks_back=3, months_back=2, reference=date(2026, 10, 19))
        self.assertEqual(missing["weekly_report"], ["2026-10-12", "2026-09-28"])
        self.assertEqual(missing["monthly_report"], ["2026-08-01"])
        # The summary is a different type with the same key shape: its month is still missing.
        self.assertEqual(missing["monthly_project_summary"], ["2026-09-01", "2026-08-01"])

    def test_the_key_prefix_match_cannot_bleed_into_a_neighbouring_date(self):
        from app.services.report_catch_up import missing_report_periods

        self._queue("weekly_report", "week:2026-10-120:user:1")  # not a real key; must not satisfy 2026-10-12
        with self.factory() as db:
            missing = missing_report_periods(db, weeks_back=1, months_back=0, reference=date(2026, 10, 19))
        self.assertEqual(missing["weekly_report"], ["2026-10-12"])

    def test_catch_up_dry_run_runs_each_missing_period_through_the_real_services(self):
        from unittest.mock import patch

        from app.services import report_catch_up

        calls = []

        def fake_weekly(db, *, week_start, dry_run):
            calls.append(("weekly", week_start, dry_run))
            return {"week_start": week_start.isoformat(), "queued": 5, "notification_ids": [1, 2]}

        def fake_monthly(db, *, month_start, dry_run):
            calls.append(("monthly", month_start, dry_run))
            return {"month_start": month_start.isoformat(), "queued": 2, "notification_ids": []}

        self._queue("weekly_report", "week:2026-10-05:user:7")
        with patch("app.services.weekly_report.WeeklyReportService.run", side_effect=fake_weekly), \
             patch("app.services.monthly_report.MonthlyReportService.run", side_effect=fake_monthly), \
             patch("app.services.monthly_project_summary.MonthlyProjectSummaryService.run", side_effect=fake_monthly), \
             self.factory() as db:
            result = report_catch_up.catch_up(db, weeks_back=2, months_back=1, dry_run=True, reference=date(2026, 10, 19))
        self.assertEqual(result["missing"]["weekly_report"], ["2026-10-12"])
        self.assertEqual(calls, [
            ("weekly", date(2026, 10, 12), True),
            ("monthly", date(2026, 9, 1), True),
            ("monthly", date(2026, 9, 1), True),
        ])
        self.assertEqual(result["queued_total"], 9)
        self.assertNotIn("notification_ids", result["weekly"][0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
