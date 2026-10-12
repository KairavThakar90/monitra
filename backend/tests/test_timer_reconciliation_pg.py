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

    def test_the_closing_decision_compares_the_database_clock_with_itself(self):
        """`last_seen_at` is written with the database's `now()`. An application server
        whose clock is three hours fast used to see an entry silent for thirty minutes as
        silent for three and a half hours, and close it."""
        now = self._db_now()
        entry = self._running(now - timedelta(hours=6), last_seen=now - timedelta(minutes=30))

        class FastClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=3)

        with patch("app.services.time_entry_reconciliation.datetime", FastClock):
            report = self._reconcile()
        self.assertEqual((report["candidates"], report["closed"]), (0, 0), report)
        self.assertIsNone(self._reload(entry.id).end_time)

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

    def test_a_stop_racing_a_reconcile_of_an_entry_with_a_pending_prompt_cannot_deadlock(self):
        """The user's Stop takes the idle-period row first and the entry second (the
        order `resolve_pending_for_stop` then the compare-and-set give it). The reconciler
        used to take the entry first: with a prompt pending, each then waited on the
        other's row and PostgreSQL had to kill one of them. Held here by hand: the stop
        side owns the idle row and now wants the entry."""
        from app.models.time_entry_idle_period import TimeEntryIdlePeriod
        from app.repositories.time_entry_idle_period import TimeEntryIdlePeriodRepository

        now = self._db_now()
        start = now - timedelta(hours=6)
        entry = self._running(start, last_seen=now - timedelta(hours=3))
        self.db.add(TimeEntryIdlePeriod(
            organization_id=1, user_id=self.ids.user_id, time_entry_id=entry.id,
            original_project_id=self.ids.project_id, original_task_id=self.ids.task_id,
            idle_started_at=now - timedelta(hours=3, minutes=10), idle_detected_at=now - timedelta(hours=3),
            status="pending",
        ))
        self.db.commit()

        stop_side = self.Session()
        self.addCleanup(stop_side.close)
        TimeEntryIdlePeriodRepository.list_pending_for_entry(stop_side, entry.id)   # holds the idle row
        outcome = {}

        def reconciler():
            from app.services.time_entry_reconciliation import TimeEntryReconciliationService

            db = self.Session()
            try:
                outcome["report"] = TimeEntryReconciliationService.run(db, apply=True)
            except Exception as error:  # noqa: BLE001
                outcome["error"] = error
            finally:
                db.close()

        worker = threading.Thread(target=reconciler)
        worker.start()
        import time as _time

        _time.sleep(0.6)                                  # the reconciler is now waiting on the idle row
        stopped_at = now - timedelta(hours=2, minutes=40)
        stop_side.execute(text(
            "UPDATE time_entries SET end_time = :e, status = 'stopped', total_seconds = :t "
            "WHERE id = :i AND end_time IS NULL"
        ), {"e": stopped_at, "t": int((stopped_at - start).total_seconds()), "i": entry.id})
        stop_side.commit()                                # no deadlock: the entry was free
        worker.join(30)

        self.assertFalse(worker.is_alive(), "the reconciler never finished")
        self.assertNotIn("error", outcome, outcome.get("error"))
        report = outcome["report"]
        self.assertEqual((report["closed"], report["failed"]), (0, 0), report)
        self.assertEqual(self._reload(entry.id).end_time, stopped_at, "the user's stop stands")

    def test_a_closure_that_cannot_get_its_locks_fails_alone_and_is_retried(self):
        """A stuck transaction on the idle row costs one failed closure, not a hung run."""
        from app.models.time_entry_idle_period import TimeEntryIdlePeriod
        from app.repositories.time_entry_idle_period import TimeEntryIdlePeriodRepository
        from app.services import time_entry_reconciliation as module

        now = self._db_now()
        entry = self._running(now - timedelta(hours=6), last_seen=now - timedelta(hours=3))
        self.db.add(TimeEntryIdlePeriod(
            organization_id=1, user_id=self.ids.user_id, time_entry_id=entry.id,
            original_project_id=self.ids.project_id, original_task_id=self.ids.task_id,
            idle_started_at=now - timedelta(hours=3, minutes=10), idle_detected_at=now - timedelta(hours=3),
            status="pending",
        ))
        self.db.commit()
        stuck = self.Session()
        self.addCleanup(stuck.close)
        TimeEntryIdlePeriodRepository.list_pending_for_entry(stuck, entry.id)   # never released

        with patch.object(module, "LOCK_WAIT_MILLISECONDS", 300):
            report = self._reconcile()
        self.assertEqual((report["closed"], report["failed"]), (0, 1), report)
        self.assertIsNone(self._reload(entry.id).end_time)

        stuck.rollback()                                   # the stuck transaction ends
        self.assertEqual(self._reconcile()["closed"], 1, "the next run closes it")

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

    def test_the_screenshot_pages_time_worked_stops_where_a_silent_entry_does(self):
        """Both interval reads behind the screenshots page's "time worked" used to close a
        running entry at now, so the Friday row credited its owner with the weekend there
        too (they read the entries directly, not through `effective_now`)."""
        from app.repositories.time_entry_screenshot import TimeEntryScreenshotRepository as Repo

        now = self._db_now()
        start = now - timedelta(days=3)
        seen = start + timedelta(minutes=27)
        self._running(start, last_seen=seen)
        window = (start - timedelta(days=1), now + timedelta(days=1))

        by_user = Repo.list_tracked_intervals_by_user(self.db, 1, *window)
        by_project = Repo.list_tracked_intervals_by_projects(self.db, 1, [self.ids.project_id], *window)
        for rows in (by_user, by_project):
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][2], seen + timedelta(minutes=15))

    def test_the_screenshot_pages_time_worked_still_follows_a_live_entry_to_now(self):
        from app.repositories.time_entry_screenshot import TimeEntryScreenshotRepository as Repo

        now = self._db_now()
        self._running(now - timedelta(hours=2), last_seen=now - timedelta(seconds=20))
        rows = Repo.list_tracked_intervals_by_user(self.db, 1, now - timedelta(days=1), now + timedelta(days=1))
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual((rows[0][2] - datetime.now(UTC)).total_seconds(), 0, delta=5)

    def test_a_silent_entry_no_longer_blocks_the_owners_manual_time_after_its_evidence(self):
        """The recovery path for hours lost to a dead desktop is a manual-time request.
        A running entry used to be open-ended in the overlap check, so the forgotten
        Friday timer refused every request for the days after it."""
        from app.repositories.manual_time_entry import ManualTimeEntryRepository as Repo

        now = self._db_now()
        start = now - timedelta(days=3)
        seen = start + timedelta(minutes=27)
        self._running(start, last_seen=seen)

        def overlapping(slot_start, slot_end):
            return Repo.find_overlapping_time_entries(self.db, 1, self.ids.user_id, slot_start, slot_end)

        during = overlapping(start + timedelta(minutes=10), start + timedelta(minutes=20))
        after = overlapping(now - timedelta(days=2), now - timedelta(days=2) + timedelta(hours=2))
        self.assertEqual(len(during), 1, "inside the measured time it is still a conflict")
        self.assertEqual(after, [], "after the evidence ends, the time is free to request")

    def test_a_live_or_never_reported_entry_still_blocks_overlapping_manual_time(self):
        from app.repositories.manual_time_entry import ManualTimeEntryRepository as Repo

        now = self._db_now()
        other = self.db.execute(text(
            "INSERT INTO users (organization_id, username, email, name, role_name, capture_frequency) "
            "VALUES (1, 'asha', 'asha@example.invalid', 'Asha', 'employee', 3) RETURNING id"
        )).scalar()
        self.db.commit()
        self._running(now - timedelta(hours=3), last_seen=now - timedelta(seconds=30))
        self._running(now - timedelta(days=3), last_seen=None, user_id=other)
        slot = (now - timedelta(hours=2), now - timedelta(hours=1))
        self.assertEqual(len(Repo.find_overlapping_time_entries(self.db, 1, self.ids.user_id, *slot)), 1)
        self.assertEqual(len(Repo.find_overlapping_time_entries(
            self.db, 1, other, now - timedelta(days=1), now - timedelta(days=1) + timedelta(hours=1))), 1)

    def test_the_read_only_sql_report_sorts_every_running_entry_and_agrees_with_the_dry_run(self):
        """`scripts/stale_running_entries_report.sql` is what the owner runs in pgAdmin
        before anything is enabled, so it is executed here against real rows -- and its
        proposed closures must be the ones the application's own dry run reports."""
        import pathlib

        from app.models.time_entry_activity import TimeEntryActivity
        from app.models.time_entry_idle_period import TimeEntryIdlePeriod

        sql = (pathlib.Path(__file__).resolve().parents[1] / "scripts"
               / "stale_running_entries_report.sql").read_text(encoding="utf-8")
        now = self._db_now()
        users = {}
        for name in ("live", "stale30", "silent3h", "nothing", "oldupload", "future", "alive30h", "idle"):
            users[name] = self.db.execute(text(
                "INSERT INTO users (organization_id, username, email, name, role_name, capture_frequency) "
                "VALUES (1, :n, :e, :n, 'employee', 3) RETURNING id"
            ), {"n": name, "e": f"{name}@example.invalid"}).scalar()
        self.db.commit()
        entries = {
            "live": self._running(now - timedelta(hours=1), last_seen=now - timedelta(seconds=30), user_id=users["live"]),
            "stale30": self._running(now - timedelta(hours=5), last_seen=now - timedelta(minutes=30), user_id=users["stale30"]),
            "silent3h": self._running(now - timedelta(hours=6), last_seen=now - timedelta(hours=3), user_id=users["silent3h"]),
            "nothing": self._running(now - timedelta(days=3), last_seen=None, user_id=users["nothing"]),
            "oldupload": self._running(now - timedelta(days=3), last_seen=None, user_id=users["oldupload"]),
            "future": self._running(now - timedelta(hours=2), last_seen=now + timedelta(hours=1), user_id=users["future"]),
            "alive30h": self._running(now - timedelta(hours=30), last_seen=now - timedelta(seconds=20), user_id=users["alive30h"]),
            "idle": self._running(now - timedelta(hours=9), last_seen=now - timedelta(seconds=30), user_id=users["idle"]),
        }
        old_start = entries["oldupload"].start_time
        self.db.add(TimeEntryActivity(
            organization_id=1, time_entry_id=entries["oldupload"].id, recorded_at=old_start + timedelta(minutes=30),
            keyboard_strokes=10, mouse_clicks=2, mouse_movements=40, activity_percentage=50, window_seconds=60,
        ))
        self.db.add(TimeEntryIdlePeriod(
            organization_id=1, user_id=users["idle"], time_entry_id=entries["idle"].id,
            original_project_id=self.ids.project_id, original_task_id=self.ids.task_id,
            idle_started_at=now - timedelta(hours=6, minutes=10), idle_detected_at=now - timedelta(hours=6),
            status="pending",
        ))
        self.db.commit()

        rows = {r["time_entry_id"]: r for r in self.db.execute(text(sql)).mappings().all()}
        self.assertEqual(set(rows), {e.id for e in entries.values()}, "one row per running entry")
        category = {name: rows[e.id]["category"] for name, e in entries.items()}
        self.assertEqual(category, {
            "live": "confirmed_active", "stale30": "potentially_stale", "silent3h": "potentially_stale",
            "nothing": "unverified", "oldupload": "potentially_stale", "future": "manual_review",
            "alive30h": "manual_review", "idle": "confirmed_active",
        })
        action = {name: rows[e.id]["policy_action"] for name, e in entries.items()}
        self.assertEqual(action["live"], "leave open")
        self.assertEqual(action["stale30"], "leave open", "inside the stop-after window: could be offline-but-working")
        self.assertEqual(action["silent3h"], "close: no_signal")
        self.assertTrue(action["nothing"].startswith("operator only"), action["nothing"])
        self.assertTrue(action["oldupload"].startswith("operator only"), action["oldupload"])
        self.assertEqual(action["idle"], "close: idle_unanswered")
        self.assertIsNone(rows[entries["live"].id]["proposed_end"], "nothing to propose for a live entry")
        self.assertIsNone(rows[entries["alive30h"].id]["proposed_end"], "nor for one that is long but reporting in")
        self.assertEqual(rows[entries["live"].id]["can_report_liveness"], "yes: this entry has reported liveness")
        self.assertTrue(rows[entries["nothing"].id]["can_report_liveness"].startswith("unknown"))
        # What Active Users showed before the policy, and what it shows now.
        silent = rows[entries["silent3h"].id]
        self.assertAlmostEqual(silent["measured_before_policy_seconds"], 6 * 3600, delta=5)
        # Started 6 h ago, last heard from 3 h ago: it is measured to last signal + 15 min.
        self.assertAlmostEqual(silent["measured_with_policy_seconds"], 3 * 3600 + 15 * 60, delta=5)

        # The proposals are the application's own dry run, to the second.
        dry = {i["time_entry_id"]: i for i in self._reconcile(apply=False)["items"]}
        for name in ("silent3h", "idle"):
            proposed = rows[entries[name].id]["proposed_end"]
            would = datetime.fromisoformat(dry[entries[name].id]["would_end_at"])
            self.assertAlmostEqual((proposed - would).total_seconds(), 0, delta=2, msg=name)
        self.assertNotIn(entries["nothing"].id, dry, "the scheduled run never reaches a legacy entry")
        operator = self._reconcile(
            apply=False, include_unreported=True, entry_ids=[entries["oldupload"].id],
        )["items"][0]
        proposed = rows[entries["oldupload"].id]["proposed_end"]
        self.assertAlmostEqual(
            (proposed - datetime.fromisoformat(operator["would_end_at"])).total_seconds(), 0, delta=2,
        )

    def test_the_silence_evidence_script_separates_an_outage_from_a_sleeping_machine(self):
        """`scripts/timer_silence_evidence.sql` is what the owner runs on production to choose the
        no-signal threshold from their own data, so it is executed here on known silences."""
        import pathlib

        from app.models.time_entry_activity import TimeEntryActivity

        sql = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "timer_silence_evidence.sql"
               ).read_text(encoding="utf-8")
        now = self._db_now()
        t0 = now - timedelta(days=3)
        users = {}
        for name in ("outage", "asleep", "lunch"):
            users[name] = self.db.execute(text(
                "INSERT INTO users (organization_id, username, email, name, role_name, capture_frequency) "
                "VALUES (1, :n, :e, :n, 'employee', 3) RETURNING id"
            ), {"n": name, "e": f"{name}@example.invalid"}).scalar()
        self.db.commit()

        def entry_with_windows(name, received):
            entry = self._running(t0, last_seen=None, user_id=users[name])
            for received_at, recorded_at in received:
                self.db.add(TimeEntryActivity(
                    organization_id=1, time_entry_id=entry.id, recorded_at=recorded_at, created_at=received_at,
                    keyboard_strokes=1, mouse_clicks=1, mouse_movements=1, activity_percentage=50, window_seconds=60,
                ))
            self.db.commit()
            return entry

        three_hours = timedelta(hours=3)
        # A 3 h outage: the window that finally arrives was recorded long before (it was queued).
        entry_with_windows("outage", [
            (t0, t0), (t0 + timedelta(minutes=1), t0 + timedelta(minutes=1)),
            (t0 + three_hours + timedelta(minutes=1), t0 + timedelta(minutes=5)),
        ])
        # A 3 h sleep: the first window after the wake is fresh.
        entry_with_windows("asleep", [
            (t0, t0), (t0 + timedelta(minutes=1), t0 + timedelta(minutes=1)),
            (t0 + three_hours + timedelta(minutes=1), t0 + three_hours + timedelta(minutes=1)),
        ])
        # A 40 minute lunch.
        entry_with_windows("lunch", [(t0, t0), (t0 + timedelta(minutes=40), t0 + timedelta(minutes=40))])

        rows = {str(r["threshold"]): r for r in self.db.execute(text(sql)).mappings().all()}
        ten = rows["0:10:00"]
        self.assertEqual((ten["silences_longer"], ten["entries"], ten["members"]), (3, 3, 3))
        self.assertEqual((ten["alive_but_offline"], ten["asleep_or_off"]), (1, 2))
        two_hours = rows["2:00:00"]
        self.assertEqual((two_hours["silences_longer"], two_hours["alive_but_offline"], two_hours["asleep_or_off"]),
                         (2, 1, 1), "the lunch is not cut at 2 h; the outage is, and so is the sleep")
        self.assertAlmostEqual(float(two_hours["hours_past_the_grace"]), 5.5, delta=0.11)
        self.assertEqual(rows["4:00:00"]["silences_longer"], 0, "nothing in this data is cut at 4 h")

    def test_the_entry_preview_script_reproduces_the_incident_and_shows_the_effect_on_every_report(self):
        """The 5958 plan's numbers: started Friday 17:23:21 IST, never stopped, measured on Monday
        10:55:54 IST -- the 65:32:33 on the Active Users screenshot -- and what closing it changes."""
        import pathlib
        import re

        from app.models.time_entry_activity import TimeEntryActivity

        sql = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "entry_reconciliation_preview.sql"
               ).read_text(encoding="utf-8")
        start = datetime(2026, 10, 9, 11, 53, 21, tzinfo=UTC)
        monday = datetime(2026, 10, 12, 5, 25, 54, tzinfo=UTC)
        entry = self._running(start, last_seen=None)
        self.db.add(TimeEntryActivity(
            organization_id=1, time_entry_id=entry.id, recorded_at=start + timedelta(minutes=30),
            keyboard_strokes=10, mouse_clicks=2, mouse_movements=40, activity_percentage=50, window_seconds=60,
        ))
        self.db.commit()
        sql = re.sub(r"5958::bigint", f"{entry.id}::bigint", sql)
        sql = re.sub(r"now\(\)\s+AS as_of", "TIMESTAMPTZ '2026-10-12 05:25:54+00' AS as_of", sql)

        rows = self.db.execute(text(sql)).mappings().all()
        by = {}
        for r in rows:
            by.setdefault(r["report"], []).append(r)

        facts = by["entry"][0]["detail"]
        self.assertIn("started 2026-10-09 17:23 IST", facts)
        self.assertIn("would end 2026-10-09 18:09 IST", facts, "newest evidence 17:54 + 15 minutes")

        report_day = by["reports_day"][0]
        self.assertEqual((str(report_day["period"]), report_day["before_hms"], report_day["after_hms"]),
                         ("2026-10-09", "65:32:33", "00:46:00"),
                         "the whole 65:32:33 sits on the day it started; closing leaves 46 minutes there")
        self.assertEqual(str(by["reports_week"][0]["period"]), "2026-10-05")
        self.assertEqual(str(by["reports_month"][0]["period"]), "2026-10-01")
        self.assertEqual((by["reports_week"][0]["before_seconds"], by["reports_month"][0]["after_seconds"]),
                         (235953, 2760))

        apportioned = {str(r["period"]): (r["before_seconds"], r["after_seconds"]) for r in by["time_tracking_day"]}
        self.assertEqual(apportioned, {
            "2026-10-09": (23799, 2760), "2026-10-10": (86400, 0),
            "2026-10-11": (86400, 0), "2026-10-12": (39354, 0),
        })
        self.assertEqual(sum(b for b, _ in apportioned.values()), 235953, "the days add up to the one figure")
        self.assertIsNone(self._reload(entry.id).end_time, "the preview wrote nothing")

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

    # ── the real upload routes (not the service call) ─────────────────────────

    def _client(self):
        """The real app, its real routes and dependency chain, on this database."""
        from fastapi.testclient import TestClient

        from app.core.database import get_db
        from app.core.security import get_current_user
        from app.main import app

        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.addCleanup(app.dependency_overrides.clear)
        return TestClient(app)

    def test_an_activity_window_posted_to_the_real_route_counts_as_proof_of_life(self):
        """What a desktop that predates the heartbeat does every minute it runs."""
        now = self._db_now()
        entry = self._running(now - timedelta(hours=2), last_seen=None)
        response = self._client().post(f"/api/v1/time-entries/{entry.id}/activity/batch", json={"samples": [{
            "recorded_at": (now - timedelta(seconds=60)).isoformat(), "keyboard_strokes": 3,
            "mouse_clicks": 1, "mouse_movements": 10, "activity_percentage": 40, "window_seconds": 60,
            "client_event_id": "window-1",
        }]})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNotNone(self._reload(entry.id).last_seen_at)

    def test_an_application_usage_batch_posted_to_the_real_route_counts_as_proof_of_life(self):
        now = self._db_now()
        entry = self._running(now - timedelta(hours=2), last_seen=None)
        response = self._client().post(f"/api/v1/time-entries/{entry.id}/app-usage/batch", json={"records": [{
            "application_name": "Code", "window_title": "main.py", "duration_seconds": 30,
            "recorded_at": (now - timedelta(seconds=40)).isoformat(),
        }]})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNotNone(self._reload(entry.id).last_seen_at)

    def test_an_idle_report_posted_to_the_real_route_counts_as_proof_of_life(self):
        now = self._db_now()
        entry = self._running(now - timedelta(hours=2), last_seen=None)
        response = self._client().post("/api/v1/idle-periods", json={
            "time_entry_id": entry.id,
            "idle_started_at": (now - timedelta(minutes=30)).isoformat(),
            "idle_detected_at": (now - timedelta(minutes=1)).isoformat(),
            "client_event_id": f"idle:{entry.id}:1",
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNotNone(self._reload(entry.id).last_seen_at)

    def test_a_late_upload_for_an_entry_the_server_already_closed_does_not_reopen_it(self):
        """The backlog drains after the outage: the windows are kept, the entry stays closed."""
        start = datetime(2026, 10, 9, 11, 53, 21, tzinfo=UTC)
        seen = start + timedelta(minutes=27)
        entry = self._running(start, last_seen=seen)
        self._reconcile(now=datetime(2026, 10, 12, 5, 25, 54, tzinfo=UTC))
        closed = self._reload(entry.id)
        end, last_seen = closed.end_time, closed.last_seen_at
        self.assertIsNotNone(end)

        self._client().post(f"/api/v1/time-entries/{entry.id}/activity/batch", json={"samples": [{
            "recorded_at": (start + timedelta(minutes=20)).isoformat(), "keyboard_strokes": 3,
            "mouse_clicks": 1, "mouse_movements": 10, "activity_percentage": 40, "window_seconds": 60,
            "client_event_id": "late-window",
        }]})
        after = self._reload(entry.id)
        self.assertEqual((after.end_time, after.last_seen_at, after.status), (end, last_seen, "stopped"))

    def test_a_heartbeat_for_somebody_elses_entry_is_a_404_and_touches_nothing(self):
        other = self.db.execute(text(
            "INSERT INTO users (organization_id, username, email, name, role_name, capture_frequency) "
            "VALUES (1, 'asha', 'asha@example.invalid', 'Asha', 'employee', 3) RETURNING id"
        )).scalar()
        self.db.commit()
        entry = self._running(self._db_now() - timedelta(hours=1), last_seen=None, user_id=other)
        response = self._client().post(f"/api/v1/time-entries/{entry.id}/heartbeat")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIsNone(self._reload(entry.id).last_seen_at)

    def test_a_delayed_heartbeat_for_an_entry_the_server_closed_says_so_and_changes_nothing(self):
        start = datetime(2026, 10, 9, 11, 53, 21, tzinfo=UTC)
        entry = self._running(start, last_seen=start + timedelta(minutes=27))
        self._reconcile(now=datetime(2026, 10, 12, 5, 25, 54, tzinfo=UTC))
        closed = self._reload(entry.id)
        response = self._client().post(f"/api/v1/time-entries/{entry.id}/heartbeat")
        body = response.json()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(body["closed_by_server"])
        self.assertEqual(body["closed_reason"], "no_signal")
        self.assertIsNotNone(body["entry"]["end_time"])
        after = self._reload(entry.id)
        self.assertEqual((after.end_time, after.last_seen_at), (closed.end_time, closed.last_seen_at))

    def test_an_upload_for_somebody_elses_entry_proves_nothing(self):
        from app.services.time_entry_liveness import TimeEntryLivenessService

        entry = self._running(self._db_now() - timedelta(hours=1), last_seen=None)
        TimeEntryLivenessService.touch(self.db, entry.id, self.ids.user_id + 999)
        self.assertIsNone(self._reload(entry.id).last_seen_at)


if __name__ == "__main__":
    unittest.main()
