"""
The abandoned-timer lifecycle against a real PostgreSQL.

Everything in `test_timer_liveness.py` is a mock; this is the same behaviour
where the guarantees actually live: the partial unique index that allows one
running entry per user, the row lock the reconciler takes with SKIP LOCKED, the
compare-and-set that ends an entry exactly once, and the SQL that measures a
running entry in every report.

Skipped unless MONITRA_PG_TEST_URL names a loopback database (see
`tests/pg_support.py`). It truncates the timer tables.
"""
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import func, select, text

from tests import pg_support

URL = pg_support.pg_url()
UTC = timezone.utc


@unittest.skipUnless(URL, "set MONITRA_PG_TEST_URL to a loopback PostgreSQL to run")
class ReconciliationAgainstPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = pg_support.engine_for(URL)
        cls.Session = pg_support.sessions(cls.engine)

    def setUp(self):
        self.ids = pg_support.reset_and_seed(self.engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.user = pg_support.user_object(self.db, self.ids.user_id)
        task_patch = patch("app.services.time_entry.TaskService.get_task")
        task_patch.start()
        self.addCleanup(task_patch.stop)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _db_now(self) -> datetime:
        return self.db.execute(text("select now()")).scalar()

    def _running(self, start, last_seen=None, client_op=None, user_id=None):
        from app.models.time_entry import TimeEntry

        entry = TimeEntry(
            organization_id=1, user_id=user_id or self.ids.user_id, project_id=self.ids.project_id,
            task_id=self.ids.task_id, start_time=start, status="running", last_seen_at=last_seen,
            client_op=client_op,
        )
        self.db.add(entry)
        self.db.commit()
        return entry

    def _reload(self, entry_id):
        from app.models.time_entry import TimeEntry

        self.db.expire_all()
        return self.db.get(TimeEntry, entry_id)

    def _reconcile(self, **kwargs):
        from app.services.time_entry_reconciliation import TimeEntryReconciliationService

        kwargs.setdefault("apply", True)
        return TimeEntryReconciliationService.run(self.db, **kwargs)

    # ── the production row ───────────────────────────────────────────────────

    def test_friday_start_monday_morning_is_closed_at_last_signal_plus_grace(self):
        """user 260's row: running since Friday 17:23 IST, 65 hours on Monday.
        The desktop reported until 17:50; the entry ends at 18:05 -- 42 minutes
        -- not at Monday morning."""
        start = datetime(2026, 10, 9, 11, 53, 21, tzinfo=UTC)
        seen = start + timedelta(minutes=27)
        monday = datetime(2026, 10, 12, 5, 25, 54, tzinfo=UTC)
        entry = self._running(start, last_seen=seen, client_op="timer:5298:fri")

        dry = self._reconcile(apply=False, now=monday)
        self.assertEqual((dry["candidates"], dry["closed"]), (1, 0))
        self.assertIsNone(self._reload(entry.id).end_time, "a report-only run writes nothing")

        report = self._reconcile(now=monday)
        self.assertEqual(report["closed"], 1)
        closed = self._reload(entry.id)
        self.assertEqual(closed.end_time, seen + timedelta(minutes=15))
        self.assertEqual(closed.total_seconds, 42 * 60)
        self.assertEqual(closed.status, "stopped")
        self.assertEqual(closed.start_time, start, "the original start is preserved")

        from app.models.time_entry_reconciliation import TimeEntryReconciliation

        audit = self.db.scalars(select(TimeEntryReconciliation)).all()
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0].reason, audit[0].decided_by), ("no_signal", "system:reconciler"))
        self.assertEqual(audit[0].last_seen_at, seen)
        self.assertEqual(audit[0].ended_at, closed.end_time)
        self.assertEqual(audit[0].policy["no_signal_grace_seconds"], 900)

    def test_a_second_run_changes_nothing(self):
        start = datetime(2026, 10, 9, 11, 53, 21, tzinfo=UTC)
        entry = self._running(start, last_seen=start + timedelta(minutes=27))
        monday = datetime(2026, 10, 12, 5, 25, 54, tzinfo=UTC)
        self._reconcile(now=monday)
        again = self._reconcile(now=monday)
        self.assertEqual((again["candidates"], again["closed"]), (0, 0))

        from app.models.time_entry_reconciliation import TimeEntryReconciliation

        self.assertEqual(self.db.scalar(select(func.count()).select_from(TimeEntryReconciliation)), 1)
        self.assertEqual(self._reload(entry.id).total_seconds, 42 * 60)

    def test_it_never_touches_an_entry_that_is_reporting_in_or_never_reported(self):
        other = self.db.execute(text(
            "INSERT INTO users (organization_id, username, email, name, role_name, capture_frequency) "
            "VALUES (1, 'asha', 'asha@example.invalid', 'Asha', 'employee', 3) RETURNING id"
        )).scalar()
        self.db.commit()
        now = self._db_now()
        live = self._running(now - timedelta(days=2), last_seen=now - timedelta(seconds=45), user_id=self.ids.user_id)
        unreported = self._running(now - timedelta(days=3), last_seen=None, user_id=other)

        report = self._reconcile()
        self.assertEqual((report["candidates"], report["closed"], report["unreported_running"]), (0, 0, 1))
        self.assertIsNone(self._reload(live.id).end_time, "a long session that keeps reporting runs on")
        self.assertIsNone(self._reload(unreported.id).end_time)

    def test_an_operator_can_close_a_named_unreported_entry_on_its_newest_evidence(self):
        from app.models.time_entry_activity import TimeEntryActivity

        start = datetime(2026, 10, 9, 11, 53, 21, tzinfo=UTC)
        entry = self._running(start, last_seen=None)
        self.db.add(TimeEntryActivity(
            organization_id=1, time_entry_id=entry.id, recorded_at=start + timedelta(minutes=30),
            keyboard_strokes=10, mouse_clicks=2, mouse_movements=40, activity_percentage=50, window_seconds=60,
        ))
        self.db.commit()
        monday = datetime(2026, 10, 12, 5, 25, 54, tzinfo=UTC)

        report = self._reconcile(now=monday, entry_ids=[entry.id], include_unreported=True,
                                 decided_by="operator")
        self.assertEqual(report["closed"], 1)
        closed = self._reload(entry.id)
        # last window ended 31:00 after the start, plus the quarter hour
        self.assertEqual(closed.end_time, start + timedelta(minutes=31) + timedelta(minutes=15))

        from app.models.time_entry_reconciliation import TimeEntryReconciliation

        audit = self.db.scalar(select(TimeEntryReconciliation))
        self.assertEqual((audit.reason, audit.decided_by), ("operator", "operator"))
        self.assertIsNone(audit.last_seen_at)
        self.assertEqual(audit.evidence_at, start + timedelta(minutes=31))

    # ── unanswered idle prompt ───────────────────────────────────────────────

    def test_an_unanswered_idle_prompt_on_a_live_desktop_closes_and_discards_the_idle_stretch(self):
        """The person went home with Monitra running: heartbeats arrive, the
        prompt is never answered. Closed at the prompt, idle stretch discarded
        -- the owner pressing Stop on the unanswered prompt, exactly."""
        from app.models.time_entry_adjustment import TimeEntryAdjustment
        from app.models.time_entry_idle_period import TimeEntryIdlePeriod

        now = self._db_now()
        start = now - timedelta(hours=9)
        idle_started = now - timedelta(hours=6, minutes=10)
        detected = now - timedelta(hours=6)
        entry = self._running(start, last_seen=now - timedelta(seconds=30))
        self.db.add(TimeEntryIdlePeriod(
            organization_id=1, user_id=self.ids.user_id, time_entry_id=entry.id,
            original_project_id=self.ids.project_id, original_task_id=self.ids.task_id,
            idle_started_at=idle_started, idle_detected_at=detected, status="pending",
        ))
        self.db.commit()

        self.assertEqual(self._reconcile()["closed"], 1)
        closed = self._reload(entry.id)
        self.assertEqual(closed.end_time, detected)
        period = self.db.scalar(select(TimeEntryIdlePeriod))
        self.assertEqual((period.status, period.counted, period.action), ("resolved", False, "stop"))
        net = closed.total_seconds + sum(
            a.adjustment_seconds for a in self.db.scalars(select(TimeEntryAdjustment)).all()
        )
        self.assertAlmostEqual(net, (idle_started - start).total_seconds(), delta=1)

    # ── start replaces a silent entry ────────────────────────────────────────

    def test_a_new_start_replaces_a_silent_entry_instead_of_adopting_it(self):
        from app.models.time_entry import TimeEntry
        from app.services.time_entry import TimeEntryService

        now = self._db_now()
        old = self._running(now - timedelta(days=3), last_seen=now - timedelta(days=3, minutes=-27))
        entry, created = TimeEntryService.start_timer(
            self.db, self.ids.project_id, self.ids.task2_id, None, None, self.user,
            client_op="timer:2:monday", reports_liveness=True,
        )
        self.assertTrue(created)
        self.assertNotEqual(entry.id, old.id)
        running = self.db.scalars(select(TimeEntry).where(TimeEntry.end_time.is_(None))).all()
        self.assertEqual([r.id for r in running], [entry.id], "exactly one running entry")
        self.assertIsNotNone(entry.last_seen_at, "a heartbeat-capable client starts alive")
        old = self._reload(old.id)
        self.assertEqual(old.end_time, old.last_seen_at + timedelta(minutes=15))

    def test_a_new_start_is_still_refused_while_the_other_entry_reports_in(self):
        from app.services.time_entry import TimeEntryService

        now = self._db_now()
        self._running(now - timedelta(hours=1), last_seen=now - timedelta(minutes=2))
        with self.assertRaises(HTTPException) as error:
            TimeEntryService.start_timer(
                self.db, self.ids.project_id, self.ids.task2_id, None, None, self.user,
                client_op="timer:2:other-device",
            )
        self.assertEqual(error.exception.status_code, 409)

    def test_a_new_start_is_still_refused_for_an_entry_that_never_reported(self):
        from app.services.time_entry import TimeEntryService

        now = self._db_now()
        self._running(now - timedelta(days=3), last_seen=None)
        with self.assertRaises(HTTPException) as error:
            TimeEntryService.start_timer(
                self.db, self.ids.project_id, self.ids.task2_id, None, None, self.user,
                client_op="timer:2:monday",
            )
        self.assertEqual(error.exception.status_code, 409)

    # ── concurrency ──────────────────────────────────────────────────────────

    def test_two_reconcilers_racing_for_one_entry_close_it_once(self):
        start = datetime(2026, 10, 9, 11, 53, 21, tzinfo=UTC)
        entry = self._running(start, last_seen=start + timedelta(minutes=27))
        monday = datetime(2026, 10, 12, 5, 25, 54, tzinfo=UTC)
        results, barrier = [], threading.Barrier(2)

        def worker():
            from app.services.time_entry_reconciliation import TimeEntryReconciliationService

            db = self.Session()
            try:
                barrier.wait()
                results.append(TimeEntryReconciliationService.run(db, apply=True, now=monday)["closed"])
            finally:
                db.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        from app.models.time_entry_reconciliation import TimeEntryReconciliation

        self.assertEqual(sum(results), 1, results)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(TimeEntryReconciliation)), 1)
        self.assertEqual(self._reload(entry.id).total_seconds, 42 * 60)

    def test_a_heartbeat_during_the_run_keeps_the_entry_open(self):
        from app.services.time_entry_liveness import TimeEntryLivenessService

        now = self._db_now()
        entry = self._running(now - timedelta(hours=5), last_seen=now - timedelta(hours=3))
        TimeEntryLivenessService.heartbeat(self.db, entry.id, self.user)
        report = self._reconcile()
        self.assertEqual(report["closed"], 0)
        self.assertIsNone(self._reload(entry.id).end_time)
        self.assertGreater(self._reload(entry.id).last_seen_at, now - timedelta(minutes=1))

    def test_two_simultaneous_starts_leave_exactly_one_running_entry(self):
        from app.models.time_entry import TimeEntry
        from app.services.time_entry import TimeEntryService

        outcomes, barrier = [], threading.Barrier(2)

        def worker(op):
            db = self.Session()
            try:
                user = pg_support.user_object(db, self.ids.user_id)
                barrier.wait()
                try:
                    entry, created = TimeEntryService.start_timer(
                        db, self.ids.project_id, self.ids.task_id, None, None, user, client_op=op,
                    )
                    outcomes.append(("started", entry.id, created))
                except HTTPException as exc:
                    outcomes.append(("refused", exc.status_code, None))
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(f"timer:1:{i}",)) for i in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        self.assertEqual(sorted(o[0] for o in outcomes), ["refused", "started"], outcomes)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(TimeEntry).where(TimeEntry.end_time.is_(None))), 1,
        )

    def test_a_retried_start_after_a_stop_does_not_reopen_the_entry(self):
        from app.services.time_entry import TimeEntryService

        entry, _ = TimeEntryService.start_timer(
            self.db, self.ids.project_id, self.ids.task_id, None, None, self.user, client_op="timer:1:a",
        )
        TimeEntryService.stop_timer(self.db, entry.id, None, self.user)
        replay, created = TimeEntryService.start_timer(
            self.db, self.ids.project_id, self.ids.task_id, None, None, self.user, client_op="timer:1:a",
        )
        self.assertFalse(created)
        self.assertIsNotNone(self._reload(replay.id).end_time, "a delayed start never reopens a stopped entry")

    def test_a_stale_stop_cannot_end_a_newer_session(self):
        from app.services.time_entry import TimeEntryService

        first, _ = TimeEntryService.start_timer(
            self.db, self.ids.project_id, self.ids.task_id, None, None, self.user, client_op="timer:1:a",
        )
        TimeEntryService.stop_timer(self.db, first.id, None, self.user)
        second, _ = TimeEntryService.start_timer(
            self.db, self.ids.project_id, self.ids.task2_id, None, None, self.user, client_op="timer:2:b",
        )
        _, finalized = TimeEntryService.stop_timer(self.db, first.id, None, self.user)   # the old stop, replayed
        self.assertFalse(finalized)
        self.assertIsNone(self._reload(second.id).end_time, "the newer session is untouched")

    def test_two_stops_for_one_entry_end_it_once(self):
        from app.services.time_entry import TimeEntryService

        entry, _ = TimeEntryService.start_timer(
            self.db, self.ids.project_id, self.ids.task_id, None, None, self.user, client_op="timer:1:a",
        )
        results, barrier = [], threading.Barrier(2)

        def worker():
            db = self.Session()
            try:
                user = pg_support.user_object(db, self.ids.user_id)
                barrier.wait()
                _, finalized = TimeEntryService.stop_timer(db, entry.id, None, user)
                results.append(finalized)
            finally:
                db.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), [False, True])

    # ── the measurement in SQL ───────────────────────────────────────────────

    def test_the_sql_measurement_agrees_with_the_python_one(self):
        """`effective_now` (reports) and `effective_end` (responses) must give
        one number for one entry."""
        from app.core.timer_policy import TimerPolicy, effective_end
        from app.models.time_entry import TimeEntry
        from app.repositories.time_entry_liveness import effective_now

        policy = TimerPolicy.current()
        now = self._db_now()
        start = now - timedelta(days=3)
        cases = {
            "reporting": now - timedelta(minutes=2),
            "just_stale": now - timedelta(minutes=12),
            "silent_an_hour": now - timedelta(hours=1),
            "weekend": now - timedelta(days=2, hours=3),
            "never": None,
        }
        entries = {}
        for name, seen in cases.items():
            other = self.db.execute(text(
                "INSERT INTO users (organization_id, username, email, name, role_name, capture_frequency) "
                "VALUES (1, :n, :e, :n, 'employee', 3) RETURNING id"
            ), {"n": name, "e": f"{name}@example.invalid"}).scalar()
            self.db.commit()
            entries[name] = self._running(start, last_seen=seen, user_id=other)
        rows = self.db.execute(
            select(TimeEntry.id, effective_now(TimeEntry.last_seen_at, TimeEntry.start_time), func.now())
            .where(TimeEntry.id.in_([e.id for e in entries.values()]))
        ).all()
        by_id = {row[0]: row for row in rows}
        for name, entry in entries.items():
            with self.subTest(case=name):
                _, sql_end, db_now = by_id[entry.id]
                expected = effective_end(start, None, cases[name], db_now, policy)
                self.assertAlmostEqual((sql_end - expected).total_seconds(), 0, delta=2)

    def test_active_users_shows_the_silent_entry_as_stale_with_a_frozen_figure(self):
        """The screenshot in the report: "Running for 65:32:33". With a silent
        desktop the page says it has no signal and the figure stops at the
        last evidence."""
        from app.services.time_tracking import TimeTrackingService

        now = self._db_now()
        start = now - timedelta(hours=65, minutes=32)
        seen = start + timedelta(minutes=27)
        self._running(start, last_seen=seen)
        admin = pg_support.user_object(self.db, self.ids.user_id)
        admin.permissions = {"time_entries:view_all": True}
        admin.role_name = "administrator"

        payload = TimeTrackingService.list_active(self.db, admin)
        self.assertEqual(payload["total"], 1)
        item = payload["items"][0]
        self.assertEqual(item["liveness"], "stale")
        self.assertEqual(item["elapsed_seconds"], 42 * 60)
        self.assertEqual(item["elapsed_time"], "00:42:00")
        self.assertAlmostEqual((item["measured_until"] - (seen + timedelta(minutes=15))).total_seconds(), 0, delta=1)

    def test_a_stale_entry_adds_no_weekend_to_the_daily_report(self):
        from app.models.time_entry import TimeEntry
        from app.repositories.time_tracking import TimeTrackingRepository

        now = self._db_now()
        start = now - timedelta(days=3)
        self._running(start, last_seen=start + timedelta(minutes=27))
        total = self.db.scalar(
            select(func.sum(TimeTrackingRepository._duration_expression())).select_from(TimeEntry)
        )
        self.assertAlmostEqual(float(total), 42 * 60, delta=2)

    def test_a_closure_across_midnight_is_apportioned_to_both_days(self):
        """Midnight splitting and the policy together: started Friday 23:50
        IST, last heard from at 00:05 Saturday, closed 00:20. The day list
        books 10 minutes to Friday and 20 to Saturday and counts nothing
        for the rest of the weekend."""
        from app.repositories.time_tracking import TimeTrackingRepository

        ist = timezone(timedelta(hours=5, minutes=30))
        start = datetime(2026, 10, 9, 23, 50, tzinfo=ist)
        seen = datetime(2026, 10, 10, 0, 5, tzinfo=ist)
        entry = self._running(start, last_seen=seen)
        monday = datetime(2026, 10, 12, 10, 55, tzinfo=ist)
        self._reconcile(now=monday)
        closed = self._reload(entry.id)
        self.assertEqual(closed.end_time, datetime(2026, 10, 10, 0, 20, tzinfo=ist))

        rows, _ = TimeTrackingRepository.list_daily_totals(
            self.db, 1, datetime(2026, 10, 9, 0, 0, tzinfo=ist), datetime(2026, 10, 13, 0, 0, tzinfo=ist),
            None, None, 0, 50,
        )
        by_day = {str(r["work_date"]): r["total_seconds"] for r in rows}
        self.assertEqual(by_day, {"2026-10-09": 600.0, "2026-10-10": 1200.0})

    # ── heartbeat, uploads ───────────────────────────────────────────────────

    def test_a_heartbeat_never_reopens_or_alters_an_ended_entry(self):
        from app.services.time_entry import TimeEntryService
        from app.services.time_entry_liveness import TimeEntryLivenessService

        entry, _ = TimeEntryService.start_timer(
            self.db, self.ids.project_id, self.ids.task_id, None, None, self.user, client_op="timer:1:a",
        )
        stopped, _ = TimeEntryService.stop_timer(self.db, entry.id, None, self.user)
        end = stopped.end_time
        result = TimeEntryLivenessService.heartbeat(self.db, entry.id, self.user)
        self.assertIsNotNone(result["entry"].end_time)
        self.assertEqual(self._reload(entry.id).end_time, end)
        self.assertIsNone(self._reload(entry.id).last_seen_at)

    def test_an_upload_counts_as_proof_of_life(self):
        from app.services.time_entry_liveness import TimeEntryLivenessService

        now = self._db_now()
        entry = self._running(now - timedelta(hours=1), last_seen=None)
        TimeEntryLivenessService.touch(self.db, entry.id, self.ids.user_id)
        self.assertIsNotNone(self._reload(entry.id).last_seen_at)

    def test_an_upload_for_somebody_elses_entry_proves_nothing(self):
        from app.services.time_entry_liveness import TimeEntryLivenessService

        entry = self._running(self._db_now() - timedelta(hours=1), last_seen=None)
        TimeEntryLivenessService.touch(self.db, entry.id, self.ids.user_id + 999)
        self.assertIsNone(self._reload(entry.id).last_seen_at)


if __name__ == "__main__":
    unittest.main()
