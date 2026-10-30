"""Splitting a recorded time entry between two projects/tasks.

What is pinned here, and why each matters:

* **Time is divided, never created or lost.** The two halves cover exactly the
  original start..end and their `total_seconds` add up to the original's. This is
  checked on a real (SQLite) database across whole-second, fractional and exactly
  half-way (`x.5`) durations, because rounding each half separately can disagree
  with rounding the whole by a second.
* **Each half obeys the one duration rule** (`total_seconds == round(end - start)`),
  so every report that re-derives from the columns agrees with the stored total.
* **Only the owner, only a finished, ordinary entry.** A running entry, somebody
  else's entry and an entry made from reassigned idle time are refused. A manual entry
  can be split, and its new half stays manual.
* **You cannot move all of it, nor more than is left after deductions** -- the
  original must keep a positive, non-negative-net amount.
* **It is audited and rolled up**: a transfer row naming the entry it was split
  from, and both tasks' totals refreshed.
* **A lost race is not a double cut**: the compare-and-set on the entry finds
  nothing to update when it already changed.
"""
import itertools
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import get_current_user
from app.core.time_format import elapsed_seconds
from app.main import app
from app.models.time_entry import TimeEntry
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.time_entry_idle_period import TimeEntryIdlePeriod
from app.models.time_entry_transfer import TimeEntryTransfer
from app.models.user import User
from app.repositories.time_entry import TimeEntryRepository
from app.schemas.time_entry import TimeEntrySplitRequest
from app.services.time_entry import TimeEntryService

UTC = timezone.utc
SVC = "app.services.time_entry"


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


def _user(user_id=54) -> User:
    user = User()
    user.id = user_id
    user.organization_id = 1
    user.role_name = "employee"
    user.permissions = {}
    user.is_active = True
    user.name = "Test"
    return user


def _session() -> Session:
    """A real session over the tables a split reads and writes."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [
        TimeEntry.__table__, TimeEntryTransfer.__table__, TimeEntryAdjustment.__table__,
        TimeEntryIdlePeriod.__table__,
    ]
    stripped = []
    for table in tables:
        for column in table.columns:
            default = column.server_default
            if default is not None and "::" in str(getattr(default, "arg", "")):
                stripped.append((column, default))
                column.server_default = None
    try:
        Base.metadata.create_all(engine, tables=tables)
    finally:
        for column, default in stripped:
            column.server_default = default
    # SQLite ignores the Postgres-only `postgresql_where`, which turns the one-running-entry
    # index into a plain unique index on user_id. Recreate it as the partial index it is.
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX IF EXISTS uq_active_time_entry")
        conn.exec_driver_sql("CREATE UNIQUE INDEX uq_active_time_entry ON time_entries (user_id) WHERE end_time IS NULL")
    return Session(engine, expire_on_commit=False)


_OPS = itertools.count(1)


def _add_entry(db: Session, *, start, seconds: float, user_id=54, project_id=2, task_id=3, **overrides) -> TimeEntry:
    end = start + timedelta(seconds=seconds)
    entry = TimeEntry(
        organization_id=1, user_id=user_id, project_id=project_id, task_id=task_id,
        start_time=start, end_time=end, total_seconds=elapsed_seconds(start, end),
        status="stopped", is_manual=False, is_billable=True, description="work",
        client_op=f"op-{next(_OPS)}",
    )
    for key, value in overrides.items():
        setattr(entry, key, value)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


START = datetime(2026, 10, 27, 9, 10, 0, tzinfo=UTC)


class SplitMath(unittest.TestCase):
    """The arithmetic, over many durations, on a real database."""

    def test_half_and_half_covers_the_same_interval(self):
        db = _session()
        entry = _add_entry(db, start=START, seconds=3600)
        with patch(f"{SVC}.TaskService.get_task"), patch(f"{SVC}.TimeEntryService.refresh_task_rollup"), \
                patch(f"{SVC}.ActivityLogService.capture"):
            original, created = TimeEntryService.split_entry(db, entry.id, 7, 8, 1800, None, _user())

        self.assertEqual(original.total_seconds, 1800)
        self.assertEqual(created.total_seconds, 1800)
        # One continuous interval: the original keeps the front, the new entry the back.
        self.assertEqual(_aware(original.start_time), START)
        self.assertEqual(_aware(original.end_time), START + timedelta(minutes=30))
        self.assertEqual(_aware(created.start_time), START + timedelta(minutes=30))
        self.assertEqual(_aware(created.end_time), START + timedelta(hours=1))
        self.assertEqual((created.project_id, created.task_id), (7, 8))
        self.assertEqual((original.project_id, original.task_id), (2, 3))

    def test_every_split_of_every_kind_of_duration_adds_up_and_obeys_the_duration_rule(self):
        db = _session()
        with patch(f"{SVC}.TaskService.get_task"), patch(f"{SVC}.TimeEntryService.refresh_task_rollup"), \
                patch(f"{SVC}.ActivityLogService.capture"):
            checked = 0
            # Whole seconds, fractions, and instants exactly half-way between two seconds,
            # which is where rounding each half separately can disagree with the whole.
            for fraction in (0.0, 0.25, 0.5, 0.75, 0.999999):
                for whole in (2, 3, 59, 3600, 12645):
                    for move in sorted({1, 2, whole // 2, whole - 1} - {0}):
                        if move < 1 or move > whole - 1:
                            continue
                        entry = _add_entry(db, start=START, seconds=whole + fraction)
                        total = entry.total_seconds
                        if move > total - 1:
                            continue
                        original, created = TimeEntryService.split_entry(db, entry.id, 7, 8, move, None, _user())
                        label = f"{whole}+{fraction} move {move}"
                        self.assertEqual(original.total_seconds + created.total_seconds, total, label)
                        self.assertEqual(created.total_seconds, move, label)
                        self.assertEqual(
                            original.total_seconds, elapsed_seconds(original.start_time, original.end_time), label)
                        self.assertEqual(
                            created.total_seconds, elapsed_seconds(created.start_time, created.end_time), label)
                        self.assertEqual(_aware(created.end_time) - _aware(created.start_time) >= timedelta(0), True)
                        self.assertEqual(_aware(original.start_time), START, label)
                        checked += 1
            self.assertGreater(checked, 50)

    def test_halfway_instants_that_round_differently_are_still_exact(self):
        """A 5401.5 s entry rounds up to 5402; cutting 1801 s off its end leaves 3600.5, which
        rounds down to 3600. Together that is 5401, a lost second, unless the cut is nudged."""
        db = _session()
        entry = _add_entry(db, start=START, seconds=5401.5)
        self.assertEqual(entry.total_seconds, 5402)
        with patch(f"{SVC}.TaskService.get_task"), patch(f"{SVC}.TimeEntryService.refresh_task_rollup"), \
                patch(f"{SVC}.ActivityLogService.capture"):
            original, created = TimeEntryService.split_entry(db, entry.id, 7, 8, 1801, None, _user())
        self.assertEqual(original.total_seconds + created.total_seconds, 5402)
        self.assertEqual(original.total_seconds, elapsed_seconds(original.start_time, original.end_time))
        self.assertEqual(created.total_seconds, 1801)


class SplitRules(unittest.TestCase):

    def setUp(self):
        self.db = _session()
        self.user = _user()
        patches = [
            patch(f"{SVC}.TaskService.get_task"),
            patch(f"{SVC}.TimeEntryService.refresh_task_rollup"),
            patch(f"{SVC}.ActivityLogService.capture"),
        ]
        self.get_task, self.rollup, self.capture = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def split(self, entry_id, move=600, project=7, task=8, reason=None, user=None):
        return TimeEntryService.split_entry(self.db, entry_id, project, task, move, reason, user or self.user)

    def refused(self, status_code, entry_id, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            self.split(entry_id, **kwargs)
        self.assertEqual(caught.exception.status_code, status_code, caught.exception.detail)
        return caught.exception.detail

    def test_a_running_entry_is_refused(self):
        entry = _add_entry(self.db, start=START, seconds=3600, end_time=None, total_seconds=0, status="running")
        self.assertIn("Stop it first", self.refused(409, entry.id))

    def test_someone_elses_entry_is_not_found(self):
        entry = _add_entry(self.db, start=START, seconds=3600, user_id=99)
        self.refused(404, entry.id)

    def test_an_unknown_entry_is_not_found(self):
        self.refused(404, 12345)

    def test_a_manual_entry_can_be_split_and_the_new_half_stays_manual(self):
        """Approved manual time is an ordinary row once mirrored (it cannot be edited or withdrawn,
        and a transfer already changes it). The dashboard and the activity log classify time by
        `is_manual`, so a manual half that turned into "tracked" would count as active working time."""
        entry = _add_entry(self.db, start=START, seconds=3050, is_manual=True)  # 00:50:50
        original, created = self.split(entry.id, move=1500)
        self.assertTrue(original.is_manual)
        self.assertTrue(created.is_manual)
        self.assertEqual((original.total_seconds, created.total_seconds), (1550, 1500))
        self.assertEqual(original.total_seconds + created.total_seconds, 3050)
        self.assertEqual(_aware(original.end_time), _aware(created.start_time))

    def test_an_entry_made_from_reassigned_idle_time_is_refused(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        with patch.object(TimeEntryRepository, "has_reassigned_idle_period", return_value=True):
            self.assertIn("idle", self.refused(409, entry.id).lower())

    def test_the_same_project_and_task_is_refused(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        self.refused(400, entry.id, project=2, task=3)

    def test_the_same_project_with_a_different_task_is_allowed(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        original, created = self.split(entry.id, project=2, task=9)
        self.assertEqual((created.project_id, created.task_id), (2, 9))
        self.assertEqual(original.task_id, 3)

    def test_the_destination_must_be_one_the_caller_may_use(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        self.get_task.side_effect = HTTPException(404, "Task not found")
        self.refused(404, entry.id)
        self.assertEqual(self.db.get(TimeEntry, entry.id).total_seconds, 3600)

    def test_all_of_the_entry_cannot_be_moved_because_some_must_stay(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        self.assertIn("at most 00:59:59", self.refused(400, entry.id, move=3600))
        self.assertIn("at most 00:59:59", self.refused(400, entry.id, move=4000))
        original, created = self.split(entry.id, move=3599)
        self.assertEqual((original.total_seconds, created.total_seconds), (1, 3599))

    def test_a_one_second_entry_is_too_short_to_split(self):
        entry = _add_entry(self.db, start=START, seconds=1)
        self.assertIn("too short", self.refused(400, entry.id, move=1))

    def test_what_is_already_deducted_caps_how_much_can_move(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        self.db.add(TimeEntryAdjustment(
            organization_id=1, user_id=54, project_id=2, task_id=3, time_entry_id=entry.id,
            adjustment_seconds=-1800, reason="idle time discarded",
        ))
        self.db.commit()
        self.assertIn("at most 00:30:00", self.refused(400, entry.id, move=1801))
        original, created = self.split(entry.id, move=1800)
        # The deduction stays with the original, which is left with exactly nothing net.
        self.assertEqual(original.total_seconds, 1800)
        self.assertEqual(created.total_seconds, 1800)

    def test_the_new_entry_is_audited_with_where_it_came_from(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        original, created = self.split(entry.id, move=900, reason="helped a colleague")
        rows = list(self.db.scalars(select(TimeEntryTransfer)))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.time_entry_id, created.id)
        self.assertEqual((row.from_project_id, row.from_task_id, row.to_project_id, row.to_task_id), (2, 3, 7, 8))
        self.assertEqual(row.reason, f"Split from entry #{entry.id}: helped a colleague")
        self.assertEqual(row.transferred_by_user_id, 54)

    def test_without_a_reason_the_audit_still_names_the_source_entry(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        self.split(entry.id, move=900)
        row = self.db.scalars(select(TimeEntryTransfer)).one()
        self.assertEqual(row.reason, f"Split from entry #{entry.id}")

    def test_both_tasks_totals_are_refreshed(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        self.split(entry.id, move=900)
        refreshed = [call.args[1] for call in self.rollup.call_args_list]
        self.assertEqual(sorted(refreshed), [3, 8])

    def test_the_new_entry_is_an_ordinary_finished_entry_of_its_own(self):
        entry = _add_entry(self.db, start=START, seconds=3600, client_op="session-9")
        original, created = self.split(entry.id, move=900)
        self.assertNotEqual(created.id, original.id)
        self.assertIsNone(created.client_op)
        self.assertIsNone(created.last_seen_at)
        self.assertEqual(created.is_manual, original.is_manual)
        self.assertEqual(created.status, "stopped")
        self.assertEqual((created.is_billable, created.description), (True, "work"))
        self.assertEqual(original.client_op, "session-9")
        self.assertEqual(created.user_id, original.user_id)
        self.assertEqual(created.organization_id, original.organization_id)

    def test_two_splits_cannot_both_cut_the_same_entry(self):
        """The compare-and-set finds nothing to update when the entry already changed."""
        entry = _add_entry(self.db, start=START, seconds=3600)
        # What the slower of two splits read before the faster one committed.
        stale = SimpleNamespace(
            id=entry.id, organization_id=1, user_id=54, status="stopped", is_billable=True,
            description="work", end_time=entry.end_time, total_seconds=entry.total_seconds,
        )
        self.split(entry.id, move=900)
        result = TimeEntryRepository.split(self.db, stale, START + timedelta(minutes=10), 600, 3000, 9, 10)
        self.assertIsNone(result)
        self.db.rollback()
        # One cut only: the original and the first split's half.
        self.assertEqual(len(list(self.db.scalars(select(TimeEntry)))), 2)
        self.assertEqual(self.db.scalars(select(TimeEntry).where(TimeEntry.project_id == 9)).first(), None)

    def test_a_lost_race_answers_conflict_and_leaves_one_cut(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        with patch.object(TimeEntryRepository, "split", return_value=None):
            detail = self.refused(409, entry.id)
        self.assertIn("changed", detail)
        self.assertEqual(len(list(self.db.scalars(select(TimeEntry)))), 1)
        self.assertEqual(len(list(self.db.scalars(select(TimeEntryTransfer)))), 0)

    def test_a_split_can_be_split_again_into_three(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        original, first = self.split(entry.id, move=1200, project=7, task=8)
        original, second = self.split(original.id, move=600, project=9, task=10)
        parts = sorted([original, first, second], key=lambda e: _aware(e.start_time))
        self.assertEqual(sum(p.total_seconds for p in parts), 3600)
        for before, after in zip(parts, parts[1:]):
            self.assertEqual(_aware(before.end_time), _aware(after.start_time))
        self.assertEqual(_aware(parts[0].start_time), START)
        self.assertEqual(_aware(parts[-1].end_time), START + timedelta(hours=1))


class SplitRoute(unittest.TestCase):
    """Through HTTP, the real service and a real database."""

    def setUp(self):
        self.db = _session()
        self.user = _user()
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        patches = [
            patch(f"{SVC}.TaskService.get_task"),
            patch(f"{SVC}.TimeEntryService.refresh_task_rollup"),
            patch(f"{SVC}.ActivityLogService.capture"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def test_the_route_returns_both_halves(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        response = self.client.post(
            f"/time-entries/{entry.id}/split",
            json={"to_project_id": 7, "to_task_id": 8, "move_seconds": 1800, "reason": "other project"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["original"]["id"], entry.id)
        self.assertEqual(body["original"]["total_seconds"], 1800)
        self.assertEqual(body["created"]["total_seconds"], 1800)
        self.assertEqual((body["created"]["project_id"], body["created"]["task_id"]), (7, 8))
        self.assertNotEqual(body["created"]["id"], entry.id)
        self.assertEqual(body["original"]["end_time"][:19], body["created"]["start_time"][:19])

    def test_a_refusal_carries_the_servers_words(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        response = self.client.post(
            f"/time-entries/{entry.id}/split",
            json={"to_project_id": 7, "to_task_id": 8, "move_seconds": 3600},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("at most 00:59:59", response.json()["detail"])

    def test_the_amount_must_be_a_positive_whole_number(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        for bad in (0, -5, "abc", 1.5, None):
            response = self.client.post(
                f"/time-entries/{entry.id}/split",
                json={"to_project_id": 7, "to_task_id": 8, "move_seconds": bad},
            )
            self.assertEqual(response.status_code, 422, f"{bad!r}: {response.text}")
        self.assertEqual(len(list(self.db.scalars(select(TimeEntry)))), 1)

    def test_the_destination_is_required(self):
        entry = _add_entry(self.db, start=START, seconds=3600)
        response = self.client.post(f"/time-entries/{entry.id}/split", json={"move_seconds": 60})
        self.assertEqual(response.status_code, 422)

    def test_the_request_has_no_timing_fields(self):
        """A split can only say how much moves, never rewrite the entry's start, end or total."""
        self.assertEqual(
            set(TimeEntrySplitRequest.model_fields),
            {"to_project_id", "to_task_id", "move_seconds", "reason"},
        )


if __name__ == "__main__":
    unittest.main()
