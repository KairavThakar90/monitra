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

    def test_the_run_reports_how_far_the_application_clock_is_from_the_databases(self):
        class FastClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=3)

        quiet = self._reconcile(apply=False)
        self.assertLess(abs(quiet["clock_skew_seconds"]), 5, "same machine, same clock")

        with patch("app.services.time_entry_reconciliation.datetime", FastClock), \
             self.assertLogs("app.services.time_entry_reconciliation", level="WARNING") as logs:
            skewed = self._reconcile(apply=False)
        self.assertAlmostEqual(skewed["clock_skew_seconds"], 3 * 3600, delta=5)
        self.assertTrue(any("CLOCK_SKEW" in line for line in logs.output))

    def test_a_pinned_instant_has_no_skew_to_report(self):
        """An operator replaying a past moment (`now=`) is not measuring the clocks."""
        report = self._reconcile(apply=False, now=datetime(2026, 10, 12, 5, 25, 54, tzinfo=UTC))
        self.assertIsNone(report["clock_skew_seconds"])

    def test_the_deep_health_probe_names_the_skew_between_the_application_and_the_database(self):
        from app import main as main_module

        with patch("app.core.database.get_engine", return_value=self.engine):
            result = main_module._probe_database()
        self.assertEqual(result["status"], "ok")
        self.assertLess(abs(result["clock_skew_seconds"]), 5)

    # ── what "report only" and "read-only" mean, proved at the statement level ──

    def _statements_during(self, work):
        from sqlalchemy import event

        seen = []

        def listener(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(self.engine, "before_cursor_execute", listener)
        try:
            work()
        finally:
            event.remove(self.engine, "before_cursor_execute", listener)
        return seen

    def _tables_written_during(self, work):
        """The tables any INSERT/UPDATE/DELETE named, as the statement text names them."""
        import re

        from sqlalchemy import event

        written = set()
        pattern = re.compile(r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([\w\".]+)", re.I)

        def listener(conn, cursor, statement, parameters, context, executemany):
            match = pattern.match(statement)
            if match:
                written.add(match.group(1).strip('"').split(".")[-1])

        event.listen(self.engine, "before_cursor_execute", listener)
        try:
            work()
        finally:
            event.remove(self.engine, "before_cursor_execute", listener)
        return written

    #: The scheduled endpoint records every run in the job ledger (`record_job_run`), whether or
    #: not the deployment lets it apply: that row is how /internal/jobs/status shows the scheduler
    #: is calling. It is the one write a report-only pass is allowed; no timer table is.
    LEDGER_ONLY = {"scheduled_job_runs"}

    def _digest(self):
        return self.db.execute(text(
            "select count(*), md5(string_agg(t::text, '|' order by id)) from time_entries t"
        )).one()

    def test_a_dry_run_executes_nothing_but_reads(self):
        now = self._db_now()
        self._running(now - timedelta(hours=6), last_seen=now - timedelta(hours=3))     # a candidate
        self._running(now - timedelta(days=2), last_seen=None, user_id=self._other_user())  # a legacy row
        before = self._digest()

        statements = self._statements_during(lambda: self._reconcile(apply=False))

        self.assertTrue(statements, "it did look")
        self.assertEqual(set(statements), {"SELECT"}, f"a report-only run wrote: {sorted(set(statements))}")
        self.assertEqual(self._digest(), before)

    def test_the_control_an_applying_run_is_seen_writing(self):
        """So the statement capture above is known to notice a write."""
        now = self._db_now()
        self._running(now - timedelta(hours=6), last_seen=now - timedelta(hours=3))
        statements = set(self._statements_during(lambda: self._reconcile(apply=True)))
        self.assertTrue({"INSERT", "UPDATE"} <= statements, sorted(statements))

    def test_the_scheduled_endpoint_with_the_job_off_executes_nothing_but_reads(self):
        from app.core.config import settings

        now = self._db_now()
        self._running(now - timedelta(hours=6), last_seen=now - timedelta(hours=3))
        before = self._digest()
        client = self._client()

        def call():
            with patch.object(settings, "TIMER_RECONCILER_ENABLED", False), \
                 patch.object(settings, "EMAIL_DISPATCH_TOKEN", "t" * 12):
                response = client.post("/internal/time-entries/reconcile", headers={"X-Email-Dispatch-Token": "t" * 12})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["dry_run"])

        written = self._tables_written_during(call)
        self.assertEqual(written, self.LEDGER_ONLY, f"a report-only pass wrote: {sorted(written)}")
        self.assertEqual(self._digest(), before)

    def test_the_get_alias_executes_nothing_but_reads_even_with_the_job_on(self):
        from app.core.config import settings

        now = self._db_now()
        self._running(now - timedelta(hours=6), last_seen=now - timedelta(hours=3))
        before = self._digest()
        client = self._client()

        def call():
            with patch.object(settings, "TIMER_RECONCILER_ENABLED", True), \
                 patch.object(settings, "EMAIL_DISPATCH_TOKEN", "t" * 12):
                response = client.get("/internal/time-entries/reconcile", headers={"X-Email-Dispatch-Token": "t" * 12})
            self.assertEqual(response.status_code, 200, response.text)

        written = self._tables_written_during(call)
        self.assertEqual(written, self.LEDGER_ONLY, f"the GET alias wrote: {sorted(written)}")
        self.assertEqual(self._digest(), before, "and the candidate is still running")

    def test_every_read_only_script_runs_inside_a_read_only_transaction(self):
        """If any of them could write, Postgres would refuse here."""
        import pathlib
        import re

        now = self._db_now()
        entry = self._running(now - timedelta(days=3), last_seen=None)
        scripts = pathlib.Path(__file__).resolve().parents[1] / "scripts"
        for name in ("stale_running_entries_report.sql", "timer_silence_evidence.sql",
                     "entry_reconciliation_preview.sql", "suspected_slept_through_days.sql",
                     "timer_rollout_monitor.sql"):
            sql = (scripts / name).read_text(encoding="utf-8")
            if name.startswith("entry_"):
                sql = re.sub(r"5958::bigint", f"{entry.id}::bigint", sql)
            with self.subTest(script=name), self.engine.connect() as conn:
                conn.execute(text("SET TRANSACTION READ ONLY"))
                rows = conn.execute(text(sql)).fetchall()
                conn.rollback()
                self.assertIsNotNone(rows)

    def test_the_rollout_monitor_counts_what_it_says_and_flags_a_cut_that_was_wrong(self):
        import pathlib

        from app.models.manual_time_entry import ManualTimeEntry

        now = self._db_now()
        sql = (pathlib.Path(__file__).resolve().parents[1] / "scripts"
               / "timer_rollout_monitor.sql").read_text(encoding="utf-8")
        live = self._running(now - timedelta(hours=30), last_seen=now - timedelta(seconds=20))
        quiet_start = now - timedelta(hours=8)
        quiet = self._running(quiet_start, last_seen=quiet_start + timedelta(minutes=20), user_id=self._other_user())
        self._reconcile()                                                   # closes `quiet`; the job is applying here
        self.assertIsNotNone(self._reload(quiet.id).end_time)
        closed_at = self._reload(quiet.id).end_time
        # The member then asks for the time after the cut.
        self.db.add(ManualTimeEntry(
            organization_id=1, user_id=quiet.user_id, project_id=self.ids.project_id, task_id=self.ids.task_id,
            work_date=now.date(), start_time=closed_at + timedelta(minutes=5), end_time=closed_at + timedelta(hours=2),
            total_seconds=7000, approval_status="pending", is_billable=False,
        ))
        self.db.commit()

        metrics = {r["metric"]: r["value"] for r in self.db.execute(text(sql)).mappings().all()}
        self.assertEqual(metrics["running entries"], "1")
        self.assertEqual(metrics["running over 24 h, reporting in"], "1", "the live 30-hour timer is counted, not closed")
        self.assertEqual(metrics["server closures, last 24 hours"], "1")
        self.assertEqual(metrics["  of which no_signal, last 7 days"], "1")
        self.assertTrue(metrics["last audit row written"].endswith("IST"))
        wrong_cut = [v for k, v in metrics.items() if k.startswith("wrong-cut indicator")]
        self.assertEqual(wrong_cut, ["1"], "one member asked for the time after the server's cut")
        self.assertIsNone(self._reload(live.id).end_time)

    def test_a_legacy_entry_with_a_fresh_upload_is_active_not_abandoned(self):
        """A missing last_seen_at is not evidence of anything. A legacy desktop that is uploading
        right now is a confirmed active timer, whatever the age of the entry."""
        import pathlib

        from app.models.time_entry_activity import TimeEntryActivity

        now = self._db_now()
        entry = self._running(now - timedelta(days=2), last_seen=None)
        self.db.add(TimeEntryActivity(
            organization_id=1, time_entry_id=entry.id, recorded_at=now - timedelta(minutes=3),
            keyboard_strokes=4, mouse_clicks=1, mouse_movements=9, activity_percentage=40, window_seconds=60,
        ))
        self.db.commit()
        sql = (pathlib.Path(__file__).resolve().parents[1] / "scripts"
               / "stale_running_entries_report.sql").read_text(encoding="utf-8")
        row = [r for r in self.db.execute(text(sql)).mappings().all() if r["time_entry_id"] == entry.id][0]

        self.assertEqual(row["category"], "manual_review", "alive, but running for two days: a person should look")
        self.assertIsNone(row["proposed_end"], "and nothing is proposed for an entry that is reporting in")
        self.assertEqual(row["signal_source"], "legacy upload evidence only")
        self.assertEqual(row["policy_action"], "leave open")

    def test_the_slept_through_days_script_finds_a_weekend_an_older_desktop_booked(self):
        """What 1.0.0 to 1.3.2 did on waking: contiguous entries on the IST midnights with nothing
        in them. A genuine overnight stretch -- which has activity windows -- is not listed."""
        import pathlib

        from app.models.time_entry import TimeEntry
        from app.models.time_entry_activity import TimeEntryActivity

        ist = timezone(timedelta(hours=5, minutes=30))
        # The three most recent midnights, comfortably inside the look-back.
        today = self._db_now().astimezone(ist).replace(hour=0, minute=0, second=0, microsecond=0)
        d1, d2, d3, d4 = (today - timedelta(days=n) for n in (6, 5, 4, 3))
        sql = (pathlib.Path(__file__).resolve().parents[1] / "scripts"
               / "suspected_slept_through_days.sql").read_text(encoding="utf-8")

        def add(user, start, end, op, *, activity=False):
            entry = TimeEntry(
                organization_id=1, user_id=user, project_id=self.ids.project_id, task_id=self.ids.task_id,
                start_time=start, end_time=end, total_seconds=round((end - start).total_seconds()),
                status="stopped", client_op=op,
            )
            self.db.add(entry)
            self.db.flush()
            if activity:
                self.db.add(TimeEntryActivity(
                    organization_id=1, time_entry_id=entry.id, recorded_at=start + timedelta(minutes=5),
                    keyboard_strokes=3, mouse_clicks=1, mouse_movements=9, activity_percentage=30, window_seconds=60,
                ))
            return entry

        # Friday 18:00 -> Monday: a stop and a start at each midnight, 24 h in the middle.
        first = add(self.ids.user_id, d1 - timedelta(hours=6, minutes=37), d1, "ru:1")                    # Friday evening, to midnight
        sat = add(self.ids.user_id, d1 + timedelta(milliseconds=40), d2, "ru:2")              # Saturday
        sun = add(self.ids.user_id, d2 + timedelta(milliseconds=35), d3, "ru:3")              # Sunday
        last = add(self.ids.user_id, d3, d3 + timedelta(hours=9), "ru:4")                     # Monday morning
        # A member who really worked through a midnight: the same shape, but with activity.
        other = self._other_user()
        real = add(other, d1 + timedelta(milliseconds=20), d2, "ru:5", activity=True)
        # An ordinary entry.
        ordinary = add(self.ids.user_id, d4 + timedelta(hours=10), d4 + timedelta(hours=12), "ru:6")
        self.db.commit()

        rows = {r["time_entry_id"]: r["reason"] for r in self.db.execute(text(sql)).mappings().all()}
        self.assertEqual(rows.get(sat.id), "full_day_no_activity")
        self.assertEqual(rows.get(sun.id), "full_day_no_activity")
        self.assertEqual(rows.get(first.id), "edge_of_a_run", "the evening that ran into the first midnight")
        self.assertEqual(rows.get(last.id), "edge_of_a_run", "and the morning that began at the last")
        self.assertNotIn(real.id, rows, "a day with activity in it is real work")
        self.assertNotIn(ordinary.id, rows)

    def _other_user(self):
        other = self.db.execute(text(
            "INSERT INTO users (organization_id, username, email, name, role_name, capture_frequency) "
            "VALUES (1, 'legacy', 'legacy@example.invalid', 'Legacy', 'employee', 3) RETURNING id"
        )).scalar()
        self.db.commit()
        return other

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

    def test_a_start_decides_staleness_on_the_database_clock_too(self):
        """The reconciler compares the database clock with itself (above); the start
        that replaces a silent entry made the same decision on the application clock.
        Three hours fast, it saw an entry heard from two minutes ago as silent for three
        hours, closed it, and started -- on a machine whose other half was still working."""
        from app.services.time_entry import TimeEntryService

        now = self._db_now()
        live = self._running(now - timedelta(hours=1), last_seen=now - timedelta(minutes=2))

        class FastClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=3)

        with patch("app.services.time_entry.datetime", FastClock), \
             patch("app.services.time_entry_reconciliation.datetime", FastClock):
            with self.assertRaises(HTTPException) as error:
                TimeEntryService.start_timer(
                    self.db, self.ids.project_id, self.ids.task2_id, None, None, self.user,
                    client_op="timer:2:fast-server", reports_liveness=True,
                )
        self.assertEqual(error.exception.status_code, 409)
        self.assertIsNone(self._reload(live.id).end_time, "the live entry was closed by a skewed clock")

    def test_a_new_entry_is_born_alive_on_the_database_clock(self):
        """`last_seen_at` is the database's `now()` for every heartbeat and upload; the
        start wrote the application's. A server a few minutes slow made every fresh entry
        look silent for that long -- stale, on a bad day, before its first heartbeat."""
        from app.services.time_entry import TimeEntryService

        class SlowClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) - timedelta(minutes=12)

        with patch("app.services.time_entry.datetime", SlowClock):
            entry, created = TimeEntryService.start_timer(
                self.db, self.ids.project_id, self.ids.task_id, None, None, self.user,
                client_op="timer:1:slow-server", reports_liveness=True,
            )
        self.assertTrue(created)
        self.assertAlmostEqual(
            (self._db_now() - self._reload(entry.id).last_seen_at).total_seconds(), 0, delta=5,
            msg="born on the application clock, not the database's",
        )

    def test_a_start_needs_one_request_connection_not_two(self):
        """The activity trail writes through a session of its own. Taken from the
        request pool while the start still held its own connection, a burst of starts
        larger than the pool convoyed: every request held one and waited for a second,
        until the pool timeout refused new requests with 503 and lost the audit row.
        A pool of one connection, no overflow, is that burst in miniature: the start
        must still land in well under the pool timeout, with its audit row."""
        import time

        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from app.models.activity_log import ActivityLog
        from app.services.time_entry import TimeEntryService

        one = create_engine(URL, pool_size=1, max_overflow=0, pool_timeout=3)
        self.addCleanup(one.dispose)
        with Session(one) as db:
            user = pg_support.user_object(db, self.ids.user_id)
            began = time.monotonic()
            entry, created = TimeEntryService.start_timer(
                db, self.ids.project_id, self.ids.task_id, None, None, user,
                client_op="timer:1:one-connection", reports_liveness=True,
            )
            took = time.monotonic() - began
            self.assertTrue(created)
            self.assertLess(took, 2.0, f"the start waited on the pool for {took:.1f}s")
            trail = db.scalars(select(ActivityLog).where(ActivityLog.entity_id == entry.id)).all()
            self.assertEqual(len(trail), 1, "the audit row was lost to the pool timeout")

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

    def test_active_users_labels_liveness_on_the_clock_its_figure_is_measured_with(self):
        """`elapsed_seconds` is measured in SQL with the database's `now()`; the label
        beside it was decided on the application clock. Three hours fast, a desktop heard
        from two minutes ago read "No signal" next to a figure that was still growing."""
        from app.services.time_tracking import TimeTrackingService

        now = self._db_now()
        self._running(now - timedelta(hours=1), last_seen=now - timedelta(minutes=2))
        admin = pg_support.user_object(self.db, self.ids.user_id)
        admin.permissions = {"time_entries:view_all": True}
        admin.role_name = "administrator"

        class FastClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=3)

        with patch("app.services.time_tracking.datetime", FastClock):
            item = TimeTrackingService.list_active(self.db, admin)["items"][0]
        self.assertEqual(item["liveness"], "live")
        self.assertAlmostEqual(item["elapsed_seconds"], 3600, delta=5)

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

    def _request_manual_time(self, start, end):
        """What the member's own manual-time request does, through the real service."""
        from app.schemas.manual_time_entry import ManualTimeEntryCreate
        from app.services.manual_time_entry import ManualTimeEntryService

        ist = timezone(timedelta(hours=5, minutes=30))
        return ManualTimeEntryService.create_manual_entry(
            self.db,
            ManualTimeEntryCreate(
                project_id=self.ids.project_id, task_id=self.ids.task_id,
                work_date=start.astimezone(ist).date(), total_seconds=int((end - start).total_seconds()),
                start_time=start, end_time=end, description="worked offline",
            ),
            self.user,
        )

    def test_the_manual_time_workflow_can_recover_what_a_server_closure_cut(self):
        """The recovery path for legitimate work outside the recovery window, end to end: the
        server closes a silent entry at last signal + 15 minutes; the member then files the hours
        they really worked after it, through the same service the web form and desktop use."""
        now = self._db_now()
        start = now - timedelta(days=3)
        seen = start + timedelta(minutes=37)
        entry = self._running(start, last_seen=seen)
        self._reconcile()
        closed_at = self._reload(entry.id).end_time
        self.assertEqual(closed_at, seen + timedelta(minutes=15))

        asked = self._request_manual_time(closed_at + timedelta(minutes=1), closed_at + timedelta(hours=5))

        self.assertEqual((asked.approval_status, asked.total_seconds), ("pending", 5 * 3600 - 60))
        # and a request that overlaps the time the entry DID keep is still refused
        with self.assertRaises(HTTPException) as refused:
            self._request_manual_time(start + timedelta(minutes=10), start + timedelta(minutes=20))
        self.assertEqual(refused.exception.status_code, 409)

    def test_until_it_is_closed_a_legacy_entry_that_never_reported_blocks_every_later_request(self):
        """The cost of leaving entry 5958 open (docs/TIMER_ENTRY_5958_RECONCILIATION_PLAN.md): a row
        that never reported is measured to now, so it occupies all of the time since it began."""
        now = self._db_now()
        entry = self._running(now - timedelta(days=3), last_seen=None)
        with self.assertRaises(HTTPException) as refused:
            self._request_manual_time(now - timedelta(days=1), now - timedelta(days=1) + timedelta(hours=2))
        self.assertEqual(refused.exception.status_code, 409)
        self.assertIsNone(self._reload(entry.id).end_time)

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
