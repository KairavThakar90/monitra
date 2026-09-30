"""The activity trail: what is recorded, how safely, and who may read it.

Every user action that matters -- signing in and out, starting and stopping
the timer, the member switches, the desktop being opened and closed -- leaves
one row in ``activity_logs``, written by ``ActivityLogService``. The Logs page
reads those rows grouped by employee through ``GET /api/v1/activity-logs``.

These tests run against a **real SQLite database**, not a mocked session,
because the properties worth pinning are about what actually lands in the
table and what a query actually returns:

* a row is written for the action, with the actor, the module, the action, the
  project/task it concerned and the client that performed it;
* recording **never** fails, delays or alters the action -- not when the
  description cannot be built, not when there is no database to write to, and
  never by committing or rolling back the caller's own session;
* a replayed action records nothing the first attempt already recorded;
* the read is scoped like every other people-reading surface: a leader sees
  their team, an employee is refused outright.
"""
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.request_context import (
    RequestContext, client_address, describe_client, reset_request_context, set_request_context,
)
from app.core.security import get_current_user, hash_token
from app.main import app
from app.models.activity_log import ActivityLog, ActivityLogAction, ActivityLogModule
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.refresh_token import RefreshToken
from app.models.task import Task
from app.models.user import User
from app.services import activity_log as activity_log_module
from app.services.activity_log import ActivityLogService
from app.services.auth import AuthService
from app.services.member_service import MemberService
from app.services.time_entry import TimeEntryService


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


ORG = 7
ADMIN, HR, LEADER, ALICE, BOB = 1, 2, 3, 11, 12
PROJECT, TASK = 40, 400
LOGS_ROUTE = "/api/v1/activity-logs"
EVENTS_ROUTE = "/api/v1/activity-logs/client-events"


def _database() -> Session:
    """A real session over an in-memory database holding the trail's table
    and the ones its hooks and its read join against."""
    # One shared connection: the TestClient runs handlers on a worker thread,
    # and an in-memory SQLite database is otherwise per-connection.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    tables = [model.__table__ for model in (ActivityLog, User, Project, ProjectMember, Task, RefreshToken)]
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
    return Session(engine)


def _add_user(db: Session, user_id: int, role: str, name: str) -> User:
    user = User(
        id=user_id, organization_id=ORG, role_name=role, username=name.lower(),
        email=f"{name.lower()}@example.invalid", name=name, password_hash="x",
        permissions={p: True for p in ROLE_PERMISSIONS.get(role, set())},
        is_active=True, capture_frequency=10, designation=f"{name} designation",
    )
    db.add(user)
    return user


class TrailCase(unittest.TestCase):
    """An organization with an admin, HR, a leader who leads one project, and
    two employees -- Alice on the leader's project, Bob on nobody's."""

    def setUp(self):
        self.db = _database()
        self.admin = _add_user(self.db, ADMIN, "administrator", "Grace")
        self.hr = _add_user(self.db, HR, "hr", "Hana")
        self.leader = _add_user(self.db, LEADER, "leader", "Linus")
        self.alice = _add_user(self.db, ALICE, "employee", "Alice")
        self.bob = _add_user(self.db, BOB, "employee", "Bob")
        self.db.add(Project(id=PROJECT, organization_id=ORG, project_name="Apollo",
                            status="active", leader_id=LEADER, created_by=ADMIN))
        self.db.add(Task(id=TASK, organization_id=ORG, project_id=PROJECT,
                         task_name="Guidance", created_by=ADMIN))
        self.db.add(ProjectMember(organization_id=ORG, project_id=PROJECT, user_id=ALICE,
                                  created_by=ADMIN, joined_at=date(2026, 9, 1)))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def rows(self, **filters):
        self.db.expire_all()
        stmt = select(ActivityLog).order_by(ActivityLog.id)
        for column, value in filters.items():
            stmt = stmt.where(getattr(ActivityLog, column) == value)
        return list(self.db.execute(stmt).scalars())


# ── Writing ──────────────────────────────────────────────────────────────


class RecordTests(TrailCase):

    def test_an_action_is_recorded_with_who_what_and_where(self):
        row_id = ActivityLogService.record(
            self.db, self.alice,
            module=ActivityLogModule.TIMER, action=ActivityLogAction.TIMER_STARTED,
            description="Started the timer on Apollo", project_id=PROJECT, task_id=TASK, entity_id=99,
        )
        (row,) = self.rows()
        self.assertEqual(row.id, row_id)
        self.assertEqual((row.organization_id, row.user_id), (ORG, ALICE))
        self.assertEqual((row.module, row.action), ("timer", "timer_started"))
        self.assertEqual((row.project_id, row.task_id, row.entity_id), (PROJECT, TASK, 99))
        self.assertEqual(row.description, "Started the timer on Apollo")
        self.assertIsNotNone(row.created_at)

    def test_the_calling_client_and_address_are_recorded_from_the_request(self):
        token = set_request_context(RequestContext(client="desktop", client_version="1.3.0", ip_address="10.0.0.5"))
        try:
            ActivityLogService.record(
                self.db, self.alice, module=ActivityLogModule.AUTH,
                action=ActivityLogAction.LOGIN, description="Signed in",
            )
        finally:
            reset_request_context(token)
        (row,) = self.rows()
        self.assertEqual(row.ip_address, "10.0.0.5")
        self.assertEqual(ActivityLogService.split_source(row.description), ("Signed in", "desktop", "1.3.0"))

    def test_outside_a_request_the_row_simply_carries_no_client(self):
        ActivityLogService.record(
            self.db, self.alice, module=ActivityLogModule.AUTH,
            action=ActivityLogAction.LOGIN, description="Signed in",
        )
        (row,) = self.rows()
        self.assertIsNone(row.ip_address)
        self.assertEqual(ActivityLogService.split_source(row.description), ("Signed in", None, None))

    def test_recording_never_commits_rolls_back_or_adds_to_the_callers_session(self):
        """The trail writes through a session of its own.

        If it used the caller's, the audit row's fate would be tied to the
        caller's transaction in both directions: committing it would commit
        (and expire) whatever the caller still held, and a failed audit write
        would roll the caller's work back. Five existing suites assert that
        their service commits exactly once; this is the property that keeps
        that true with the trail switched on.
        """
        with patch.object(self.db, "commit") as commit, \
             patch.object(self.db, "rollback") as rollback, \
             patch.object(self.db, "add") as add:
            row_id = ActivityLogService.record(
                self.db, self.alice, module=ActivityLogModule.AUTH,
                action=ActivityLogAction.LOGIN, description="Signed in",
            )
        commit.assert_not_called()
        rollback.assert_not_called()
        add.assert_not_called()
        self.assertFalse(any(isinstance(item, ActivityLog) for item in self.db.new))
        self.assertIsNotNone(row_id)
        self.assertEqual(len(self.rows()), 1)     # and yet the row is durably there

    def test_a_failed_audit_write_leaves_the_callers_session_alone_too(self):
        with patch.object(activity_log_module.ActivityLogRepository, "add", side_effect=RuntimeError("disk full")), \
             patch.object(self.db, "commit") as commit, \
             patch.object(self.db, "rollback") as rollback:
            ActivityLogService.record(
                self.db, self.alice, module=ActivityLogModule.AUTH,
                action=ActivityLogAction.LOGIN, description="Signed in",
            )
        commit.assert_not_called()
        rollback.assert_not_called()


class NeverFailsTests(TrailCase):
    """The action is the product; the row is a record of it."""

    def test_a_description_that_cannot_be_built_is_swallowed(self):
        def build():
            raise RuntimeError("the task was deleted underneath us")

        self.assertIsNone(ActivityLogService.capture(self.db, build))
        self.assertEqual(self.rows(), [])

    def test_a_caller_without_a_real_session_records_nothing_and_is_not_asked(self):
        build = MagicMock()
        for not_a_session in (None, MagicMock(), SimpleNamespace()):
            self.assertIsNone(ActivityLogService.capture(not_a_session, build))
        build.assert_not_called()

    def test_a_missing_actor_records_nothing(self):
        self.assertIsNone(ActivityLogService.capture(self.db, lambda: {
            "actor": None, "module": "auth", "action": "logout", "description": "Signed out",
        }))
        self.assertEqual(self.rows(), [])

    def test_a_database_that_refuses_the_row_does_not_raise(self):
        with patch.object(activity_log_module.ActivityLogRepository, "add", side_effect=RuntimeError("disk full")):
            self.assertIsNone(ActivityLogService.record(
                self.db, self.alice, module="auth", action="login", description="Signed in",
            ))


class ClientDetectionTests(unittest.TestCase):

    def test_the_desktop_is_recognised_by_its_user_agent_with_its_version(self):
        self.assertEqual(describe_client("Monitra/1.3.0"), ("desktop", "1.3.0"))
        self.assertEqual(describe_client("Monitra/1.3.0 (win32; AMD64)"), ("desktop", "1.3.0"))

    def test_a_browser_is_the_web_dashboard(self):
        self.assertEqual(
            describe_client("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"), ("web", None)
        )

    def test_anything_else_is_an_api_client_and_nothing_is_nothing(self):
        self.assertEqual(describe_client("curl/8.4.0"), ("api", None))
        self.assertEqual(describe_client(None), (None, None))
        self.assertEqual(describe_client("  "), (None, None))

    def test_only_a_real_address_is_kept(self):
        """The column is `inet` and the proxy header is client-controlled: an
        address that does not parse would fail the insert and lose the row."""
        def address(forwarded=None, host=None):
            request = SimpleNamespace(
                headers={"x-forwarded-for": forwarded} if forwarded is not None else {},
                client=SimpleNamespace(host=host) if host is not None else None,
            )
            return client_address(request)

        self.assertEqual(address(forwarded="203.0.113.9, 10.0.0.1"), "203.0.113.9")
        self.assertEqual(address(forwarded="2001:db8::1"), "2001:db8::1")
        # Garbage in the header falls back to the socket's own address...
        self.assertEqual(address(forwarded="'; DROP TABLE users; --", host="198.51.100.4"), "198.51.100.4")
        # ...and when that is not an address either, to nothing at all.
        self.assertIsNone(address(forwarded="not-an-ip", host="testclient"))
        self.assertIsNone(address())


# ── The hooks ────────────────────────────────────────────────────────────


class SignInAndOutTests(TrailCase):

    def test_issuing_a_session_records_a_sign_in(self):
        AuthService._issue_token_pair(self.db, self.alice)
        (row,) = self.rows()
        self.assertEqual((row.module, row.action, row.user_id), ("auth", "login", ALICE))

    def test_signing_out_records_it_once_however_often_it_is_replayed(self):
        pair = AuthService._issue_token_pair(self.db, self.alice)
        AuthService.revoke_session(self.db, pair.refresh_token)
        AuthService.revoke_session(self.db, pair.refresh_token)   # a retried logout
        AuthService.revoke_session(self.db, "not-a-token")         # an unknown one
        self.assertEqual([row.action for row in self.rows(module="auth")], ["login", "logout"])
        revoked = self.db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(pair.refresh_token))
        )
        self.assertIsNotNone(revoked.revoked_at)   # the sign-out itself was unaffected

    def test_refreshing_a_session_is_not_a_sign_in(self):
        pair = AuthService._issue_token_pair(self.db, self.alice)
        AuthService.refresh_session(self.db, pair.refresh_token)
        self.assertEqual([row.action for row in self.rows(module="auth")], ["login"])


class TimerTests(TrailCase):
    """Start and stop, with the time-entry tables stood in for: what is under
    test is that the trail records each lifecycle outcome once."""

    def _entry(self, **overrides):
        values = dict(id=900, user_id=ALICE, project_id=PROJECT, task_id=TASK, client_op="timer:abc",
                      start_time=datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc), end_time=None,
                      total_seconds=None)
        values.update(overrides)
        return SimpleNamespace(**values)

    def _start(self, *, existing=None):
        with patch("app.services.time_entry.TaskService.get_task"), \
             patch("app.services.time_entry.TimeEntryRepository") as repo:
            repo.get_by_client_op.return_value = existing
            repo.get_active_for_user.return_value = None
            repo.create.return_value = self._entry()
            return TimeEntryService.start_timer(
                self.db, PROJECT, TASK, None, None, self.alice, client_op="timer:abc",
            )

    def test_starting_the_timer_records_the_project_and_task_by_name(self):
        _entry, created = self._start()
        self.assertTrue(created)
        (row,) = self.rows()
        self.assertEqual((row.module, row.action, row.user_id), ("timer", "timer_started", ALICE))
        self.assertEqual((row.project_id, row.task_id, row.entity_id), (PROJECT, TASK, 900))
        self.assertEqual(row.description, "Started the timer on Apollo › Guidance")

    def test_a_replayed_start_records_nothing_more(self):
        """The desktop retries a start whose response was lost. It is the same
        session; a second row would read as the user starting twice."""
        self._start()
        _entry, created = self._start(existing=self._entry())
        self.assertFalse(created)
        self.assertEqual(len(self.rows()), 1)

    def _stop(self, entry, *, stopped=None):
        with patch("app.services.time_entry.TimeEntryRepository") as repo, \
             patch("app.services.time_entry_idle_period.TimeEntryIdlePeriodService.resolve_pending_for_stop"), \
             patch.object(TimeEntryService, "refresh_task_rollup"):
            repo.get_by_id.return_value = entry
            repo.stop.return_value = stopped
            return TimeEntryService.stop_timer(self.db, entry.id, None, self.alice)

    def test_stopping_the_timer_records_how_long_it_ran(self):
        running = self._entry()
        stopped = self._entry(end_time=running.start_time + timedelta(seconds=5400), total_seconds=5400)
        with patch("app.services.time_entry.elapsed_seconds", return_value=5400):
            _entry, finalized = self._stop(running, stopped=stopped)
        self.assertTrue(finalized)
        (row,) = self.rows()
        self.assertEqual((row.module, row.action), ("timer", "timer_stopped"))
        self.assertEqual(row.description, "Stopped the timer on Apollo › Guidance after 01:30:00")

    def test_a_replayed_stop_records_nothing(self):
        already = self._entry(end_time=datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc), total_seconds=3600)
        _entry, finalized = self._stop(already)
        self.assertFalse(finalized)
        self.assertEqual(self.rows(), [])


class MemberSwitchTests(TrailCase):

    def _update(self, member, **payload):
        body = MagicMock()
        body.model_dump.return_value = payload

        def save(_db, target, data):
            for key, value in data.items():
                setattr(target, key, value)
            return target

        with patch("app.services.member_service.MemberRepository") as repo, \
             patch.object(MemberService, "get", return_value=member), \
             patch.object(MemberService, "_end_member_access"):
            repo.get_by_email.return_value = None
            repo.save.side_effect = save
            return MemberService.update(self.db, self.admin, member.id, body)

    def test_excluding_a_member_from_signing_in_is_recorded_under_the_administrator(self):
        self._update(self.alice, can_login=False)
        (row,) = self.rows()
        self.assertEqual((row.module, row.action, row.user_id, row.entity_id),
                         ("member", "login_excluded", ADMIN, ALICE))
        self.assertEqual(row.description, "Excluded Alice from signing in")

    def test_resending_a_switch_in_its_current_position_records_nothing(self):
        self._update(self.alice, can_login=True, can_add_tasks=True)
        self.assertEqual(self.rows(), [])

    def test_each_switch_and_the_other_fields_are_separate_rows(self):
        self.alice.can_add_tasks = False
        self._update(self.alice, can_add_tasks=True, designation="Engineer")
        self.assertEqual([row.action for row in self.rows()], ["add_tasks_allowed", "member_updated"])
        self.assertEqual(self.rows()[1].description, "Updated the member Alice (designation)")


# ── Reading ──────────────────────────────────────────────────────────────


class ReadTests(TrailCase):

    def setUp(self):
        super().setUp()
        today = datetime.now(timezone.utc)
        self.today = today
        seed = [
            (ALICE, "auth", "login", "Signed in · via desktop 1.3.0", today - timedelta(hours=3), None, None),
            (ALICE, "timer", "timer_started", "Started the timer on Apollo › Guidance · via desktop 1.3.0",
             today - timedelta(hours=2), PROJECT, TASK),
            (BOB, "auth", "login", "Signed in · via web", today - timedelta(hours=1), None, None),
            (ADMIN, "member", "login_excluded", "Excluded Bob from signing in 100% · via web",
             today - timedelta(minutes=30), None, None),
            (ALICE, "auth", "login", "Signed in · via web", today - timedelta(days=20), None, None),
        ]
        for user_id, module, action, description, at, project_id, task_id in seed:
            self.db.add(ActivityLog(organization_id=ORG, user_id=user_id, module=module, action=action,
                                    description=description, created_at=at,
                                    project_id=project_id, task_id=task_id))
        # Another organization's row must never be returned to this one.
        self.db.add(ActivityLog(organization_id=ORG + 1, user_id=ALICE, module="auth", action="login",
                                description="Signed in", created_at=today))
        self.db.commit()

    def _names(self, payload):
        return [(group["name"], group["entry_count"]) for group in payload["members"]]

    def test_an_administrator_reads_the_organization_grouped_by_employee(self):
        payload = ActivityLogService.list_grouped(self.db, self.admin)
        self.assertEqual(self._names(payload), [("Alice", 2), ("Bob", 1), ("Grace", 1)])
        self.assertEqual(payload["total"], 4)
        self.assertFalse(payload["truncated"])
        alice = payload["members"][0]
        self.assertEqual((alice["email"], alice["role_name"]), ("alice@example.invalid", "employee"))
        # Newest first inside a group, with names joined in and the client split out.
        newest = alice["entries"][0]
        self.assertEqual(newest["action"], "timer_started")
        self.assertEqual(newest["description"], "Started the timer on Apollo › Guidance")
        self.assertEqual((newest["source"], newest["client_version"]), ("desktop", "1.3.0"))
        self.assertEqual((newest["project_name"], newest["task_name"]), ("Apollo", "Guidance"))
        self.assertEqual(alice["last_activity_at"], newest["created_at"])

    def test_hr_reads_the_whole_organization_too(self):
        self.assertEqual(len(ActivityLogService.list_grouped(self.db, self.hr)["members"]), 3)

    def test_a_leader_reads_only_their_own_team(self):
        """Alice is on the project Linus leads; Bob and Grace are not."""
        payload = ActivityLogService.list_grouped(self.db, self.leader)
        self.assertEqual(self._names(payload), [("Alice", 2)])

    def test_a_leader_asking_for_someone_outside_the_team_learns_nothing(self):
        payload = ActivityLogService.list_grouped(self.db, self.leader, member_id=BOB)
        self.assertEqual(payload["members"], [])
        self.assertEqual(payload["total"], 0)

    def test_the_default_window_is_the_last_seven_days(self):
        payload = ActivityLogService.list_grouped(self.db, self.admin)
        self.assertEqual((payload["end_date"] - payload["start_date"]).days, 6)
        wider = ActivityLogService.list_grouped(
            self.db, self.admin, start_day=payload["end_date"] - timedelta(days=30), end_day=payload["end_date"],
        )
        self.assertEqual(self._names(wider)[0], ("Alice", 3))   # the twenty-day-old row is now inside

    def test_the_filters_narrow_by_member_module_and_words(self):
        by_member = ActivityLogService.list_grouped(self.db, self.admin, member_id=BOB)
        self.assertEqual(self._names(by_member), [("Bob", 1)])
        by_module = ActivityLogService.list_grouped(self.db, self.admin, module="timer")
        self.assertEqual(self._names(by_module), [("Alice", 1)])
        by_words = ActivityLogService.list_grouped(self.db, self.admin, search="apollo")
        self.assertEqual(self._names(by_words), [("Alice", 1)])

    def test_a_percent_sign_in_a_search_is_a_percent_sign(self):
        """Unescaped, `%` is a wildcard and this search would return every row."""
        payload = ActivityLogService.list_grouped(self.db, self.admin, search="100%")
        self.assertEqual(self._names(payload), [("Grace", 1)])
        self.assertEqual(ActivityLogService.list_grouped(self.db, self.admin, search="%")["total"], 1)

    def test_a_window_with_more_rows_than_one_answer_holds_says_so(self):
        with patch.object(activity_log_module, "MAX_ROWS", 2):
            payload = ActivityLogService.list_grouped(self.db, self.admin)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["total"], 2)
        # The newest are the ones kept.
        self.assertEqual(self._names(payload), [("Bob", 1), ("Grace", 1)])

    def test_a_row_whose_account_is_gone_is_shown_as_such_not_under_an_invented_name(self):
        self.db.add(ActivityLog(organization_id=ORG, user_id=555, module="auth", action="login",
                                description="Signed in", created_at=self.today))
        self.db.commit()
        names = [group["name"] for group in ActivityLogService.list_grouped(self.db, self.admin)["members"]]
        self.assertIn("Former member #555", names)

    def test_an_inverted_an_oversized_and_an_unknown_request_are_refused(self):
        day = date(2026, 9, 30)
        for kwargs in (
            {"start_day": day, "end_day": day - timedelta(days=1)},
            {"start_day": day - timedelta(days=400), "end_day": day},
            {"module": "not-a-module"},
        ):
            with self.assertRaises(HTTPException) as raised:
                ActivityLogService.list_grouped(self.db, self.admin, **kwargs)
            self.assertEqual(raised.exception.status_code, 422)


# ── The desktop's own events ─────────────────────────────────────────────


class ClientEventTests(TrailCase):

    def test_a_close_is_recorded_at_the_instant_it_happened(self):
        at = datetime.now(timezone.utc) - timedelta(minutes=10)
        result = ActivityLogService.record_client_event(self.db, self.alice, event="app_closed", occurred_at=at)
        self.assertTrue(result["recorded"])
        (row,) = self.rows()
        self.assertEqual((row.module, row.action, row.user_id), ("desktop", "app_closed", ALICE))
        self.assertEqual(row.description, "Closed the Monitra desktop application")
        self.assertEqual(row.created_at.replace(tzinfo=None), at.replace(tzinfo=None))

    def test_the_same_event_delivered_twice_is_one_row(self):
        """The desktop's durable queue retries until it is answered."""
        at = datetime.now(timezone.utc) - timedelta(minutes=10)
        first = ActivityLogService.record_client_event(self.db, self.alice, event="app_opened", occurred_at=at)
        again = ActivityLogService.record_client_event(self.db, self.alice, event="app_opened", occurred_at=at)
        self.assertEqual((first["recorded"], again["recorded"]), (True, False))
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.rows()), 1)

    def test_two_people_closing_at_the_same_instant_are_two_rows(self):
        at = datetime.now(timezone.utc) - timedelta(minutes=10)
        ActivityLogService.record_client_event(self.db, self.alice, event="app_closed", occurred_at=at)
        ActivityLogService.record_client_event(self.db, self.bob, event="app_closed", occurred_at=at)
        self.assertEqual(len(self.rows()), 2)

    def test_a_clock_far_ahead_is_recorded_as_now_not_as_the_future(self):
        ahead = datetime.now(timezone.utc) + timedelta(days=2)
        ActivityLogService.record_client_event(self.db, self.alice, event="app_closed", occurred_at=ahead)
        (row,) = self.rows()
        self.assertLess(row.created_at.replace(tzinfo=None), (ahead - timedelta(days=1)).replace(tzinfo=None))


# ── Over HTTP ────────────────────────────────────────────────────────────


class RouteTests(TrailCase):

    def setUp(self):
        super().setUp()
        self.user = self.admin
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        super().tearDown()

    def test_the_trail_is_refused_without_a_session_and_without_the_permission(self):
        self.user = self.alice          # an employee holds no `view_employees`
        self.assertEqual(self.client.get(LOGS_ROUTE).status_code, 403)
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual(self.client.get(LOGS_ROUTE).status_code, 401)

    def test_a_desktop_event_is_recorded_with_the_client_that_sent_it(self):
        """End to end through the middleware: the `User-Agent` and the address
        of the request reach a row written by a handler on a worker thread."""
        self.user = self.alice
        at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        headers = {"User-Agent": "Monitra/1.3.0", "X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
        first = self.client.post(EVENTS_ROUTE, json={"event": "app_closed", "occurred_at": at}, headers=headers)
        again = self.client.post(EVENTS_ROUTE, json={"event": "app_closed", "occurred_at": at}, headers=headers)
        self.assertEqual((first.status_code, first.json()["recorded"]), (200, True))
        self.assertEqual((again.status_code, again.json()["recorded"]), (200, False))

        self.user = self.admin
        body = self.client.get(LOGS_ROUTE, headers={"User-Agent": "Mozilla/5.0"}).json()
        (group,) = body["members"]
        (entry,) = group["entries"]
        self.assertEqual((group["name"], entry["module"], entry["action"]), ("Alice", "desktop", "app_closed"))
        self.assertEqual((entry["source"], entry["client_version"], entry["ip_address"]),
                         ("desktop", "1.3.0", "203.0.113.9"))
        self.assertIn("desktop", body["modules"])

    def test_an_unknown_event_and_a_malformed_instant_are_422s(self):
        self.user = self.alice
        now = datetime.now(timezone.utc).isoformat()
        self.assertEqual(self.client.post(EVENTS_ROUTE, json={"event": "app_crashed", "occurred_at": now}).status_code, 422)
        self.assertEqual(self.client.post(EVENTS_ROUTE, json={"event": "app_closed", "occurred_at": "soon"}).status_code, 422)
        self.assertEqual(self.rows(), [])

    def test_a_bad_query_is_a_422_before_any_row_is_read(self):
        self.assertEqual(self.client.get(LOGS_ROUTE, params={"start": "yesterday"}).status_code, 422)
        self.assertEqual(self.client.get(LOGS_ROUTE, params={"module": "nope"}).status_code, 422)
        self.assertEqual(self.client.get(LOGS_ROUTE, params={"member_id": 0}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
