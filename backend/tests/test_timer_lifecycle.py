"""
The timer lifecycle: one clock, one entry, one duration.

What these tests defend, and the production symptom each maps to:

* **Skew cancels.** A client whose clock is minutes out must record the same
  entry as one whose clock is right. Before, the client's absolute instant was
  trusted when "plausible", so a fast clock had its start clamped to the
  server's arrival time while its (queued) stop was accepted verbatim -- an
  error equal to the skew, and a live web figure that disagreed with the
  desktop by the same amount.
* **Start is idempotent.** A start retried after a lost response is answered
  with the entry it already created. Before, it was refused with a bare 409,
  the desktop never learned the entry id, its queued stop was cancelled, and
  the entry ran on the server until the next launch adopted it with a start
  time hours in the past -- "the time jumped when I pressed Start".
* **Duplicate starts cannot slip between the check and the insert.** The
  unique index is the guard; its IntegrityError is answered, not leaked as a
  500.
* **Stop is idempotent and race-safe.** A second stop returns the finalized
  entry unchanged; two concurrent stops are settled by compare-and-set.
* **The response carries the server clock** so a client can show a running
  entry against the clock it was written with.
"""
import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.time_format import elapsed_seconds
from app.main import app
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.repositories.time_entry import TimeEntryRepository
from app.schemas.time_entry import TimeEntryRead, TimeEntryStart, TimeEntryStop
from app.services.time_entry import MAX_CLIENT_BACKDATE_SECONDS, TimeEntryService

UTC = timezone.utc
SVC = "app.services.time_entry"


def _user(user_id=54, **overrides) -> User:
    user = User()
    user.id = user_id
    user.organization_id = 1
    user.role_name = "employee"
    user.permissions = {}
    user.is_active = True
    user.name = "Test"
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


def _entry(**overrides) -> TimeEntry:
    now = datetime.now(UTC)
    fields = dict(
        id=1, organization_id=1, user_id=54, project_id=2, task_id=3,
        start_time=now - timedelta(minutes=10), end_time=None, total_seconds=0,
        status="running", is_manual=False, is_billable=False, description=None,
        client_op=None, created_at=now, updated_at=now,
    )
    fields.update(overrides)
    return TimeEntry(**fields)


def _integrity_error():
    return IntegrityError("INSERT", {}, Exception("uq_active_time_entry"))


# ── the timestamp contract ───────────────────────────────────────────────────

class EventTimeTests(unittest.TestCase):
    """`_event_time` with `client_time`: the age model."""

    def test_a_live_request_lands_on_the_server_arrival_time(self):
        now = datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)
        pressed = datetime(2026, 9, 15, 13, 29, 58, tzinfo=UTC)   # client clock, hours out
        sent = pressed + timedelta(milliseconds=40)               # sent 40ms after the click
        resolved = TimeEntryService._event_time(
            pressed, label="started_at", client_time=sent, now=now
        )
        self.assertEqual(resolved, now - timedelta(milliseconds=40))

    def test_client_clock_skew_cancels_out_of_the_recorded_interval(self):
        """The same 600-second session, from a clock 3 minutes fast and a
        clock 3 minutes slow, records the same 600 seconds."""
        server_start = datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)
        server_stop = server_start + timedelta(seconds=600)
        for skew in (timedelta(minutes=3), timedelta(minutes=-3), timedelta(0)):
            with self.subTest(skew=skew):
                start = TimeEntryService._event_time(
                    server_start + skew, label="started_at",
                    client_time=server_start + skew, now=server_start,
                )
                end = TimeEntryService._event_time(
                    server_stop + skew, label="stopped_at", not_before=start,
                    client_time=server_stop + skew, now=server_stop,
                )
                self.assertEqual(elapsed_seconds(start, end), 600)

    def test_a_queued_stop_keeps_the_interval_the_user_saw(self):
        """Stopped at 12m55s, replayed five minutes later: still 12m55s."""
        now = datetime(2026, 9, 15, 10, 17, 55, tzinfo=UTC)
        start = now - timedelta(seconds=1075)
        pressed = start + timedelta(seconds=775)          # client clock == server clock here
        sent = pressed + timedelta(minutes=5)            # the retry that finally landed
        end = TimeEntryService._event_time(
            pressed, label="stopped_at", not_before=start, client_time=sent, now=now
        )
        self.assertEqual(elapsed_seconds(start, end), 775)

    def test_a_negative_age_is_read_as_now(self):
        now = datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)
        resolved = TimeEntryService._event_time(
            now, label="stopped_at", client_time=now - timedelta(seconds=30), now=now
        )
        self.assertEqual(resolved, now)

    def test_an_implausibly_old_age_is_read_as_now(self):
        now = datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)
        resolved = TimeEntryService._event_time(
            now - timedelta(seconds=MAX_CLIENT_BACKDATE_SECONDS + 1), label="started_at",
            client_time=now, now=now,
        )
        self.assertEqual(resolved, now)

    def test_a_stop_can_never_precede_its_start(self):
        now = datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)
        start = now - timedelta(seconds=10)
        resolved = TimeEntryService._event_time(
            now - timedelta(minutes=5), label="stopped_at", not_before=start,
            client_time=now, now=now,
        )
        self.assertGreaterEqual(resolved, start)

    def test_without_client_time_the_legacy_rule_still_applies(self):
        """Older desktop builds send only the instant; that path is unchanged."""
        now = datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)
        pressed = now - timedelta(minutes=5)
        self.assertEqual(
            TimeEntryService._event_time(pressed, label="stopped_at", now=now), pressed
        )
        self.assertEqual(
            TimeEntryService._event_time(now + timedelta(hours=1), label="stopped_at", now=now),
            now,
        )


# ── start ────────────────────────────────────────────────────────────────────

class StartTimerTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = _user()
        self.get_task = patch(f"{SVC}.TaskService.get_task")
        self.get_task.start()
        self.addCleanup(self.get_task.stop)

    def test_a_retried_start_with_the_same_key_returns_the_existing_entry(self):
        existing = _entry(id=41, client_op="timer:3:abc")
        with patch(f"{SVC}.TimeEntryRepository.get_by_client_op", return_value=existing), \
             patch(f"{SVC}.TimeEntryRepository.create") as create:
            entry, created = TimeEntryService.start_timer(
                self.db, 2, 3, None, None, self.user, client_op="timer:3:abc",
            )
        self.assertIs(entry, existing)
        self.assertFalse(created)
        create.assert_not_called()

    def test_the_replay_answers_even_after_the_entry_was_stopped(self):
        """The session's entry is the answer whether or not it is still
        running: the client is asking "what did my start create", and a stop
        that already landed does not change that."""
        stopped = _entry(id=41, client_op="timer:3:abc",
                         end_time=datetime.now(UTC), total_seconds=600, status="stopped")
        with patch(f"{SVC}.TimeEntryRepository.get_by_client_op", return_value=stopped), \
             patch(f"{SVC}.TimeEntryRepository.get_active_for_user") as active:
            entry, created = TimeEntryService.start_timer(
                self.db, 2, 3, None, None, self.user, client_op="timer:3:abc",
            )
        self.assertIs(entry, stopped)
        self.assertFalse(created)
        active.assert_not_called()

    def test_a_new_start_while_another_entry_runs_is_refused_with_that_entry(self):
        running = _entry(id=40, task_id=9)
        with patch(f"{SVC}.TimeEntryRepository.get_by_client_op", return_value=None), \
             patch(f"{SVC}.TimeEntryRepository.get_active_for_user", return_value=running):
            with self.assertRaises(HTTPException) as error:
                TimeEntryService.start_timer(
                    self.db, 2, 3, None, None, self.user, client_op="timer:3:new",
                )
        self.assertEqual(error.exception.status_code, 409)
        detail = error.exception.detail
        self.assertEqual(detail["message"], "User already has an active timer")
        self.assertEqual(detail["active_entry"]["id"], 40)
        self.assertEqual(detail["active_entry"]["task_id"], 9)
        self.assertTrue(detail["active_entry"]["is_running"])

    def test_a_start_is_stamped_on_the_server_clock_not_the_clients(self):
        created_entry = _entry(id=42)
        pressed = datetime.now(UTC) + timedelta(minutes=7)     # client clock 7 min fast
        with patch(f"{SVC}.TimeEntryRepository.get_by_client_op", return_value=None), \
             patch(f"{SVC}.TimeEntryRepository.get_active_for_user", return_value=None), \
             patch(f"{SVC}.TimeEntryRepository.create", return_value=created_entry) as create:
            before = datetime.now(UTC)
            entry, created = TimeEntryService.start_timer(
                self.db, 2, 3, None, None, self.user,
                started_at=pressed, client_time=pressed, client_op="timer:3:k",
            )
            after = datetime.now(UTC)
        self.assertTrue(created)
        start_time = create.call_args.kwargs["start_time"]
        self.assertTrue(before <= start_time <= after, start_time)
        self.assertEqual(create.call_args.kwargs["client_op"], "timer:3:k")

    def test_a_race_lost_to_the_same_key_returns_the_winners_entry(self):
        """Two replays of one start arrive together: the index refuses the
        second insert, and it answers with the first's row instead of 500."""
        winner = _entry(id=43, client_op="timer:3:race")
        with patch(f"{SVC}.TimeEntryRepository.get_by_client_op", side_effect=[None, winner]), \
             patch(f"{SVC}.TimeEntryRepository.get_active_for_user", return_value=None), \
             patch(f"{SVC}.TimeEntryRepository.create", side_effect=_integrity_error()):
            entry, created = TimeEntryService.start_timer(
                self.db, 2, 3, None, None, self.user, client_op="timer:3:race",
            )
        self.assertIs(entry, winner)
        self.assertFalse(created)

    def test_a_race_lost_to_a_different_session_is_a_409_with_the_running_entry(self):
        """Both requests passed the active check; only one row can exist."""
        winner = _entry(id=44, client_op="timer:3:other")
        with patch(f"{SVC}.TimeEntryRepository.get_by_client_op", return_value=None), \
             patch(f"{SVC}.TimeEntryRepository.get_active_for_user", side_effect=[None, winner]), \
             patch(f"{SVC}.TimeEntryRepository.create", side_effect=_integrity_error()):
            with self.assertRaises(HTTPException) as error:
                TimeEntryService.start_timer(
                    self.db, 2, 3, None, None, self.user, client_op="timer:3:mine",
                )
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail["active_entry"]["id"], 44)

    def test_the_task_is_checked_before_the_key_is_looked_up(self):
        """Authorization first: an idempotent replay must not reveal an entry
        on a task the caller is no longer allowed to see."""
        self.get_task.stop()
        with patch(f"{SVC}.TaskService.get_task", side_effect=HTTPException(404)), \
             patch(f"{SVC}.TimeEntryRepository.get_by_client_op") as lookup:
            with self.assertRaises(HTTPException):
                TimeEntryService.start_timer(
                    self.db, 2, 3, None, None, self.user, client_op="timer:3:abc",
                )
        lookup.assert_not_called()
        self.get_task.start()


# ── stop ─────────────────────────────────────────────────────────────────────

class StopTimerTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.scalar.return_value = 0
        self.user = _user()
        self.idle = patch(
            "app.services.time_entry_idle_period.TimeEntryIdlePeriodService.resolve_pending_for_stop",
            return_value=[],
        )
        self.idle.start()
        self.addCleanup(self.idle.stop)

    def test_stopping_records_total_seconds_as_end_minus_start(self):
        entry = _entry(start_time=datetime.now(UTC) - timedelta(seconds=4375))
        pressed = datetime.now(UTC)
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=entry), \
             patch(f"{SVC}.TimeEntryRepository.stop", return_value=entry) as stop:
            result, finalized = TimeEntryService.stop_timer(
                self.db, 1, None, self.user, stopped_at=pressed, client_time=pressed,
            )
        self.assertTrue(finalized)
        kwargs = stop.call_args.kwargs
        self.assertEqual(kwargs["total_seconds"], elapsed_seconds(entry.start_time, kwargs["end_time"]))
        self.assertAlmostEqual(kwargs["total_seconds"], 4375, delta=1)

    def test_a_second_stop_returns_the_finalized_entry_unchanged(self):
        end = datetime.now(UTC) - timedelta(minutes=1)
        entry = _entry(end_time=end, total_seconds=540, status="stopped")
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=entry), \
             patch(f"{SVC}.TimeEntryRepository.stop") as stop:
            result, finalized = TimeEntryService.stop_timer(
                self.db, 1, None, self.user, stopped_at=datetime.now(UTC),
            )
        self.assertIs(result, entry)
        self.assertFalse(finalized)
        self.assertEqual(result.end_time, end)
        self.assertEqual(result.total_seconds, 540)
        stop.assert_not_called()

    def test_losing_the_stop_race_returns_the_winners_row(self):
        running = _entry(id=7)
        winner = _entry(id=7, end_time=datetime.now(UTC), total_seconds=600, status="stopped")
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", side_effect=[running, winner]), \
             patch(f"{SVC}.TimeEntryRepository.stop", return_value=None):
            result, finalized = TimeEntryService.stop_timer(self.db, 7, None, self.user)
        self.assertIs(result, winner)
        self.assertFalse(finalized)

    def test_another_users_entry_is_not_found(self):
        entry = _entry(user_id=99)
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=entry):
            with self.assertRaises(HTTPException) as error:
                TimeEntryService.stop_timer(self.db, 1, None, self.user)
        self.assertEqual(error.exception.status_code, 404)

    def test_the_task_rollup_is_refreshed_after_a_stop(self):
        entry = _entry(task_id=3, start_time=datetime.now(UTC) - timedelta(seconds=60))
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=entry), \
             patch(f"{SVC}.TimeEntryRepository.stop", return_value=entry), \
             patch(f"{SVC}.TimeEntryService.refresh_task_rollup") as rollup:
            TimeEntryService.stop_timer(self.db, 1, None, self.user)
        rollup.assert_called_once_with(self.db, 3)


class StopIsAtomicTests(unittest.TestCase):
    """The repository's stop is a compare-and-set on `end_time IS NULL`."""

    def test_the_update_is_conditioned_on_the_entry_still_running(self):
        captured = {}

        class Session:
            def execute(self, statement):
                captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))
                return SimpleNamespace(rowcount=1)

            def commit(self):
                captured["committed"] = True

            def refresh(self, obj):
                pass

            def rollback(self):
                captured["rolled_back"] = True

        entry = _entry(id=5)
        TimeEntryRepository.stop(Session(), entry, datetime.now(UTC), 60)
        self.assertIn("end_time IS NULL", captured["sql"])
        self.assertTrue(captured.get("committed"))

    def test_a_lost_race_rolls_back_and_returns_none(self):
        class Session:
            def __init__(self):
                self.rolled_back = False

            def execute(self, statement):
                return SimpleNamespace(rowcount=0)

            def rollback(self):
                self.rolled_back = True

            def commit(self):
                raise AssertionError("must not commit a stop that updated nothing")

        session = Session()
        self.assertIsNone(TimeEntryRepository.stop(session, _entry(id=5), datetime.now(UTC), 60))
        self.assertTrue(session.rolled_back)


class TaskRollupTests(unittest.TestCase):
    def test_the_rollup_nets_adjustments_like_every_report(self):
        captured = {}

        class Session:
            def scalar(self, statement):
                captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))
                return 1200

        self.assertEqual(TimeEntryRepository.task_net_tracked_seconds(Session(), 3), 1200)
        sql = captured["sql"]
        self.assertIn("time_entry_adjustments", sql)
        self.assertIn("greatest", sql)
        self.assertIn("end_time IS NOT NULL", sql)


# ── the HTTP contract ────────────────────────────────────────────────────────

class RouteTests(unittest.TestCase):
    def setUp(self):
        self.user = _user()
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def test_a_created_start_is_201_and_a_replay_is_200(self):
        entry = _entry(id=61, client_op="timer:3:k")
        with patch("app.api.time_entry.TimeEntryService.start_timer", return_value=(entry, True)):
            created = self.client.post("/time-entries/start", json={
                "project_id": 2, "task_id": 3, "client_op": "timer:3:k",
                "started_at": "2026-09-15T10:00:00Z", "client_time": "2026-09-15T10:00:00.050Z",
            })
        with patch("app.api.time_entry.TimeEntryService.start_timer", return_value=(entry, False)):
            replayed = self.client.post("/time-entries/start", json={
                "project_id": 2, "task_id": 3, "client_op": "timer:3:k",
            })
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(created.json()["id"], replayed.json()["id"], 61)
        self.assertIn("server_time", created.json())

    def test_the_request_id_is_forwarded_for_correlation(self):
        entry = _entry(id=61)
        with patch("app.api.time_entry.TimeEntryService.start_timer",
                   return_value=(entry, True)) as start:
            self.client.post("/time-entries/start", json={"project_id": 2, "task_id": 3},
                             headers={"X-Request-ID": "req-123"})
        self.assertEqual(start.call_args.kwargs["request_id"], "req-123")

    def test_a_conflict_carries_the_running_entry(self):
        running = _entry(id=40, task_id=9)
        with patch("app.services.time_entry.TaskService.get_task"), \
             patch("app.services.time_entry.TimeEntryRepository.get_active_for_user",
                   return_value=running):
            response = self.client.post("/time-entries/start", json={"project_id": 2, "task_id": 3})
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["message"], "User already has an active timer")
        self.assertEqual(detail["active_entry"]["id"], 40)

    def test_stop_returns_the_entry_even_when_already_stopped(self):
        entry = _entry(id=61, end_time=datetime.now(UTC), total_seconds=30, status="stopped")
        with patch("app.api.time_entry.TimeEntryService.stop_timer", return_value=(entry, False)):
            response = self.client.post("/time-entries/61/stop", json={
                "stopped_at": "2026-09-15T10:00:30Z", "client_time": "2026-09-15T10:00:30Z",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total_seconds"], 30)
        self.assertFalse(response.json()["is_running"])

    def test_active_reports_the_callers_entry_and_the_server_clock(self):
        running = _entry(id=70)
        with patch("app.api.time_entry.TimeEntryService.get_active_entry", return_value=running):
            response = self.client.get("/time-entries/active")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["entry"]["id"], 70)
        self.assertTrue(body["entry"]["is_running"])
        self.assertIn("server_time", body)
        self.assertIn("server_time", body["entry"])

    def test_active_is_explicit_when_nothing_is_running(self):
        with patch("app.api.time_entry.TimeEntryService.get_active_entry", return_value=None):
            response = self.client.get("/api/v1/time-entries/active")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["entry"])
        self.assertIn("server_time", response.json())

    def test_a_malformed_client_op_is_rejected_not_scrubbed(self):
        with patch("app.api.time_entry.TimeEntryService.start_timer") as start:
            response = self.client.post("/time-entries/start", json={
                "project_id": 2, "task_id": 3, "client_op": "has spaces and <tags>",
            })
        self.assertEqual(response.status_code, 422, response.text)
        start.assert_not_called()


# ── schema and diagnostics ───────────────────────────────────────────────────

class SchemaTests(unittest.TestCase):
    def test_the_payloads_carry_the_clients_clock_and_key(self):
        moment = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
        start = TimeEntryStart(project_id=1, task_id=2, started_at=moment,
                               client_time=moment, client_op="timer:2:20260915T100000Z")
        self.assertEqual(start.client_time, moment)
        self.assertEqual(start.client_op, "timer:2:20260915T100000Z")
        stop = TimeEntryStop(stopped_at=moment, client_time=moment)
        self.assertEqual(stop.client_time, moment)
        # All optional: an older client is unaffected.
        self.assertIsNone(TimeEntryStart(project_id=1, task_id=2).client_time)
        self.assertIsNone(TimeEntryStart(project_id=1, task_id=2).client_op)

    def test_server_time_is_the_current_utc_instant(self):
        before = datetime.now(UTC)
        read = TimeEntryRead.model_validate(_entry())
        self.assertTrue(before <= read.server_time <= datetime.now(UTC))
        self.assertIsNotNone(read.server_time.tzinfo)

    def test_the_model_declares_both_uniqueness_guards(self):
        names = {index.name for index in TimeEntry.__table__.indexes}
        self.assertIn("uq_active_time_entry", names)
        self.assertIn("uq_time_entries_user_client_op", names)


class TimingLogTests(unittest.TestCase):
    def test_every_stop_writes_one_correlatable_line(self):
        db = MagicMock()
        db.scalar.return_value = 0
        entry = _entry(id=88, client_op="timer:3:log", task_id=3, project_id=2,
                       start_time=datetime.now(UTC) - timedelta(seconds=90))

        def fake_stop(db, time_entry, end_time, total_seconds, description=None):
            time_entry.end_time = end_time
            time_entry.total_seconds = total_seconds
            return time_entry

        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=entry), \
             patch(f"{SVC}.TimeEntryRepository.stop", side_effect=fake_stop), \
             patch("app.services.time_entry_idle_period.TimeEntryIdlePeriodService.resolve_pending_for_stop",
                   return_value=[]), \
             self.assertLogs("app.timing", level=logging.INFO) as captured:
            TimeEntryService.stop_timer(db, 88, None, _user(), request_id="req-9",
                                        stopped_at=datetime.now(UTC), client_time=datetime.now(UTC))
        line = captured.output[-1]
        for token in ("event=stop.finalized", "user=54", "entry=88", "task=3", "project=2",
                      "client_op=timer:3:log", "request_id=req-9", "start_time=", "end_time=",
                      "total_seconds=", "client_stopped_at=", "client_time=", "server_now="):
            self.assertIn(token, line)

    def test_the_log_never_carries_the_description(self):
        db = MagicMock()
        db.scalar.return_value = 0
        entry = _entry(id=89, description="secret project notes",
                       start_time=datetime.now(UTC) - timedelta(seconds=5))
        with patch(f"{SVC}.TimeEntryRepository.get_by_id", return_value=entry), \
             patch(f"{SVC}.TimeEntryRepository.stop", return_value=entry), \
             patch("app.services.time_entry_idle_period.TimeEntryIdlePeriodService.resolve_pending_for_stop",
                   return_value=[]), \
             self.assertLogs("app.timing", level=logging.INFO) as captured:
            TimeEntryService.stop_timer(db, 89, "secret project notes", _user())
        self.assertNotIn("secret", "\n".join(captured.output))


class DayGroupingTests(unittest.TestCase):
    def test_daily_totals_group_by_the_ist_calendar_day(self):
        from app.repositories.time_tracking import TimeTrackingRepository

        captured = []

        class Session:
            def scalar(self, statement):
                captured.append(statement)
                return 0

            def execute(self, statement):
                captured.append(statement)
                return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: []))

        TimeTrackingRepository.list_daily_totals(
            Session(), 1, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 8, tzinfo=UTC),
            None, None, 0, 50,
        )
        compiled = captured[-1].compile(dialect=postgresql.dialect())
        self.assertIn("date(timezone(", str(compiled))
        self.assertIn("Asia/Kolkata", compiled.params.values())


if __name__ == "__main__":
    unittest.main()
