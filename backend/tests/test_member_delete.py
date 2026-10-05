"""Deleting a member on the Members page deletes them -- it does not deactivate them.

Run against a real session over an in-memory database so the statements the
repository issues are the ones exercised, not mocks of them:

* the member, their tracked time, manual time, screenshot exclusions and
  sessions are gone, and a task's `time_tracked_seconds` is recomputed from
  what is left;
* what belongs to other people survives: their time, their assignments, the
  requests this member merely approved (the approver is cleared);
* what cannot be cleaned up automatically is refused with the reason and
  nothing is removed -- your own account, a running timer, a project the
  member owns or leads, clients they invited;
* the route answers 204, and the audit row is written by the administrator.
"""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, event, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.permissions import ROLE_PERMISSIONS
from app.core.security import get_current_user
from app.main import app
from app.models.activity_log import ActivityLog, ActivityLogAction, ActivityLogModule
from app.models.client import Client
from app.models.client_invitation import ClientInvitation
from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.project_status import ProjectStatus, TaskStatus
from app.models.refresh_token import RefreshToken
from app.models.screenshot_exclusion import ScreenshotExclusion
from app.models.task import Task
from app.models.task_assignee import TaskAssignee
from app.models.time_entry import TimeEntry
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.user import User
from app.repositories.member import MemberRepository
from app.services.member_service import MemberService


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


ORG, OTHER_ORG = 7, 8
ADMIN, ALICE, BOB, OUTSIDER = 1, 11, 12, 99
PROJECT = 40
TASK_A, TASK_B = 400, 401
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)

TABLES = [
    Base.metadata.tables["organizations"], User.__table__, Project.__table__, ProjectStatus.__table__,
    TaskStatus.__table__, Task.__table__, TaskAssignee.__table__, TimeEntry.__table__,
    TimeEntryAdjustment.__table__, ManualTimeEntry.__table__, ScreenshotExclusion.__table__,
    Client.__table__, ClientInvitation.__table__, RefreshToken.__table__, ActivityLog.__table__,
]


def _database() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _register_postgres_functions(dbapi_connection, _record):  # pragma: no cover - dialect shim
        # The rollup nets entries with Postgres's greatest().
        dbapi_connection.create_function("greatest", 2, lambda a, b: max(a, b))

    stripped = []
    for table in TABLES:
        for column in table.columns:
            default = column.server_default
            if default is not None and "::" in str(getattr(default, "arg", "")):
                stripped.append((column, default))
                column.server_default = None
    # "One running entry per user" is a Postgres partial unique index; on SQLite
    # it would become a full one and refuse a member's second entry.
    relaxed = [index for table in TABLES for index in table.indexes if index.unique]
    for index in relaxed:
        index.unique = False
    try:
        Base.metadata.create_all(engine, tables=TABLES)
    finally:
        for column, default in stripped:
            column.server_default = default
        for index in relaxed:
            index.unique = True
    return Session(engine)


def _user(db, user_id, role="employee", organization_id=ORG, name=None):
    user = User(
        id=user_id, organization_id=organization_id, username=f"u{user_id}",
        email=f"u{user_id}@example.invalid", name=name or f"User {user_id}", role_name=role,
        password_hash="x", permissions={p: True for p in ROLE_PERMISSIONS.get(role, set())},
        is_active=True, status="active", idle_enabled=True, idle_minutes=5, capture_frequency=10,
        created_at=T0, updated_at=T0,
    )
    db.add(user)
    db.flush()
    return user


def _entry(db, entry_id, user_id, task_id, seconds, running=False):
    start = T0 + timedelta(hours=entry_id)
    end = None if running else start + timedelta(seconds=seconds)
    db.add(TimeEntry(
        id=entry_id, organization_id=ORG, user_id=user_id, project_id=PROJECT, task_id=task_id,
        start_time=start, end_time=end, total_seconds=0 if running else seconds,
        status="running" if running else "stopped", is_manual=False, is_billable=False,
        created_at=start, updated_at=end or start,
    ))
    db.flush()


def _manual(db, manual_id, user_id, approved_by=None):
    db.add(ManualTimeEntry(
        id=manual_id, organization_id=ORG, user_id=user_id, project_id=PROJECT, task_id=TASK_A,
        work_date=date(2026, 9, 1), start_time=T0, end_time=T0 + timedelta(hours=1), total_seconds=3600,
        approval_status="approved", approved_by=approved_by, approved_at=T0, created_at=T0, updated_at=T0,
    ))
    db.flush()


class _World:
    """An organization with an administrator and two employees, one project and
    two tasks. Alice has a history; Bob has some time of his own on the same task."""

    def __init__(self):
        self.db = _database()
        self.admin = _user(self.db, ADMIN, "administrator", name="Grace")
        self.alice = _user(self.db, ALICE, name="Alice")
        self.bob = _user(self.db, BOB, name="Bob")
        self.db.add(Project(
            id=PROJECT, organization_id=ORG, project_name="Alpha", status="active", billing_type="free",
            is_billable=True, time_tracked_seconds=0, created_by=ADMIN, created_at=T0, updated_at=T0,
        ))
        for task_id in (TASK_A, TASK_B):
            self.db.add(Task(
                id=task_id, organization_id=ORG, project_id=PROJECT, task_name=f"Task {task_id}",
                status="in_progress", time_tracked_seconds=0, is_duplicate=False, created_by=ADMIN,
                created_at=T0, updated_at=T0,
            ))
        self.db.commit()

    def give_alice_a_history(self):
        _entry(self.db, 1, ALICE, TASK_A, 3600)
        _entry(self.db, 2, ALICE, TASK_A, 3600)
        _entry(self.db, 3, BOB, TASK_A, 1800)
        _manual(self.db, 10, ALICE)
        _manual(self.db, 11, BOB, approved_by=ALICE)
        self.db.add_all([
            TaskAssignee(id=1, task_id=TASK_A, user_id=ALICE, assigned_by=ADMIN),
            TaskAssignee(id=2, task_id=TASK_A, user_id=BOB, assigned_by=ADMIN),
        ])
        self.db.add(ScreenshotExclusion(id=1, user_id=ALICE, application_id=1, exclusion_type="application"))
        self.db.add(RefreshToken(id=1, user_id=ALICE, token_hash="h", expires_at=T0 + timedelta(days=30)))
        task = self.db.get(Task, TASK_B)
        task.assignee_id = ALICE
        self.db.get(Task, TASK_A).time_tracked_seconds = 9000   # stale: 3600 + 3600 + 1800 = 9000
        self.db.commit()

    def count(self, model, *where):
        return self.db.scalar(select(func.count()).select_from(model).where(*where))

    def delete(self, member_id=ALICE, actor=None):
        return MemberService.delete(self.db, actor or self.admin, member_id)


class DeleteRemovesTheMemberTests(unittest.TestCase):
    def setUp(self):
        self.world = _World()
        self.world.give_alice_a_history()
        self.db = self.world.db

    def test_the_member_is_deleted_not_deactivated(self):
        self.world.delete()
        self.db.expire_all()
        self.assertIsNone(self.db.get(User, ALICE))
        # Nothing is left behind as "inactive" either.
        self.assertEqual(self.world.count(User, User.status == "inactive"), 0)
        self.assertEqual(self.world.count(User), 2)

    def test_their_own_data_goes_with_them(self):
        self.world.delete()
        self.assertEqual(self.world.count(TimeEntry, TimeEntry.user_id == ALICE), 0)
        self.assertEqual(self.world.count(ManualTimeEntry, ManualTimeEntry.user_id == ALICE), 0)
        self.assertEqual(self.world.count(TaskAssignee, TaskAssignee.user_id == ALICE), 0)
        self.assertEqual(self.world.count(ScreenshotExclusion, ScreenshotExclusion.user_id == ALICE), 0)
        self.assertEqual(self.world.count(RefreshToken, RefreshToken.user_id == ALICE), 0)

    def test_other_peoples_data_is_untouched(self):
        self.world.delete()
        self.assertIsNotNone(self.db.get(User, BOB))
        self.assertEqual(self.world.count(TimeEntry, TimeEntry.user_id == BOB), 1)
        self.assertEqual(self.world.count(TaskAssignee, TaskAssignee.user_id == BOB), 1)
        self.assertEqual(self.world.count(Task), 2)
        self.assertEqual(self.world.count(Project), 1)

    def test_a_task_they_held_is_unassigned_not_deleted(self):
        self.world.delete()
        self.db.expire_all()
        self.assertIsNone(self.db.get(Task, TASK_B).assignee_id)

    def test_a_request_they_approved_stays_with_the_approver_cleared(self):
        self.world.delete()
        self.db.expire_all()
        kept = self.db.get(ManualTimeEntry, 11)
        self.assertIsNotNone(kept)
        self.assertIsNone(kept.approved_by)
        self.assertEqual(kept.approval_status, "approved")

    def test_the_task_total_is_recomputed_from_what_is_left(self):
        self.world.delete()
        self.db.expire_all()
        self.assertEqual(self.db.get(Task, TASK_A).time_tracked_seconds, 1800)   # only Bob's entry remains
        self.assertEqual(self.db.get(Task, TASK_B).time_tracked_seconds, 0)      # never had time; untouched

    def test_the_audit_row_belongs_to_the_administrator_and_names_the_member(self):
        self.world.delete()
        rows = self.db.scalars(select(ActivityLog).where(ActivityLog.action == ActivityLogAction.MEMBER_DELETED)).all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.user_id, row.organization_id, row.module, row.entity_id), (ADMIN, ORG, ActivityLogModule.MEMBER, ALICE))
        self.assertIn("Deleted the member Alice", row.description)

    def test_the_call_returns_nothing(self):
        self.assertIsNone(self.world.delete())


class DeleteRefusalTests(unittest.TestCase):
    """Every refusal is a 409 with the reason, and removes nothing."""

    def setUp(self):
        self.world = _World()
        self.world.give_alice_a_history()
        self.db = self.world.db

    def assertRefused(self, member_id=ALICE, actor=None, containing=""):
        with self.assertRaises(HTTPException) as caught:
            self.world.delete(member_id, actor)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn(containing, caught.exception.detail)
        self.db.rollback()
        self.assertIsNotNone(self.db.get(User, member_id))
        return caught.exception.detail

    def assertNothingRemoved(self):
        self.assertEqual(self.world.count(TimeEntry, TimeEntry.user_id == ALICE), 2)
        self.assertEqual(self.world.count(ManualTimeEntry, ManualTimeEntry.user_id == ALICE), 1)
        self.assertEqual(self.world.count(ActivityLog, ActivityLog.action == ActivityLogAction.MEMBER_DELETED), 0)

    def test_you_cannot_delete_yourself(self):
        self.assertRefused(ADMIN, containing="own account")
        self.assertNothingRemoved()

    def test_a_running_timer_must_be_stopped_first(self):
        _entry(self.db, 20, ALICE, TASK_B, 0, running=True)
        self.db.commit()
        detail = self.assertRefused(containing="timer running")
        self.assertIn("Alice", detail)
        self.assertEqual(self.world.count(TimeEntry, TimeEntry.id == 20), 1)

    def test_a_project_leader_must_be_replaced_first(self):
        self.db.get(Project, PROJECT).leader_id = ALICE
        self.db.commit()
        self.assertRefused(containing="Alpha")
        self.assertNothingRemoved()

    def test_a_project_owner_must_be_replaced_first(self):
        self.db.get(Project, PROJECT).owner_id = ALICE
        self.db.commit()
        self.assertRefused(containing="Alpha")
        self.assertNothingRemoved()

    def test_many_projects_are_summarised(self):
        for project_id, name in ((41, "Bravo"), (42, "Charlie"), (43, "Delta"), (44, "Echo")):
            self.db.add(Project(
                id=project_id, organization_id=ORG, project_name=name, status="active", billing_type="free",
                is_billable=True, time_tracked_seconds=0, leader_id=ALICE, created_by=ADMIN, created_at=T0, updated_at=T0,
            ))
        self.db.commit()
        detail = self.assertRefused(containing="Bravo, Charlie, Delta")
        self.assertIn("and 1 more", detail)

    def test_clients_they_invited_must_be_dealt_with_first(self):
        self.db.add(Client(id=5, organization_id=ORG, name="Acme", email="acme@example.invalid", status="active", invited_by=ALICE))
        self.db.commit()
        self.assertRefused(containing="invited clients")
        self.assertEqual(self.world.count(Client), 1)
        self.assertNothingRemoved()

    def test_a_pending_invitation_counts_too(self):
        self.db.add(Client(id=5, organization_id=ORG, name="Acme", email="acme@example.invalid", status="pending", invited_by=ADMIN))
        self.db.add(ClientInvitation(
            id=1, client_id=5, email="acme@example.invalid", status="pending", token_hash="t",
            expires_at=T0 + timedelta(days=7), invited_by=ALICE,
        ))
        self.db.commit()
        self.assertRefused(containing="invited clients")
        self.assertEqual(self.world.count(ClientInvitation), 1)

    def test_someone_in_another_organization_is_not_found(self):
        _user(self.db, OUTSIDER, organization_id=OTHER_ORG)
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            self.world.delete(OUTSIDER)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIsNotNone(self.db.get(User, OUTSIDER))

    def test_an_unknown_member_is_not_found(self):
        with self.assertRaises(HTTPException) as caught:
            self.world.delete(12345)
        self.assertEqual(caught.exception.status_code, 404)

    def test_a_reference_the_database_still_enforces_becomes_a_409_and_rolls_back(self):
        with patch.object(MemberRepository, "delete_with_dependents", side_effect=IntegrityError("DELETE", {}, Exception("fk"))):
            self.assertRefused(containing="still referenced")
        self.assertNothingRemoved()

    def test_nothing_is_committed_when_the_rollup_fails(self):
        with patch("app.services.time_entry.TimeEntryService.refresh_task_rollup", side_effect=IntegrityError("UPDATE", {}, Exception("fk"))):
            self.assertRefused(containing="still referenced")
        self.assertNothingRemoved()


class RepositoryTests(unittest.TestCase):
    def test_a_member_with_nothing_attached_is_deleted_cleanly(self):
        world = _World()
        task_ids = MemberRepository.delete_with_dependents(world.db, world.bob)
        world.db.commit()
        self.assertEqual(task_ids, [])
        self.assertIsNone(world.db.get(User, BOB))

    def test_it_reports_each_task_once_for_the_rollup(self):
        world = _World()
        world.give_alice_a_history()
        _entry(world.db, 30, ALICE, TASK_B, 600)
        world.db.commit()
        self.assertEqual(sorted(MemberRepository.delete_with_dependents(world.db, world.alice)), [TASK_A, TASK_B])

    def test_blocking_references_are_scoped_to_the_organization(self):
        world = _World()
        world.db.add(Project(
            id=60, organization_id=OTHER_ORG, project_name="Elsewhere", status="active", billing_type="free",
            is_billable=True, time_tracked_seconds=0, leader_id=ALICE, created_by=ADMIN, created_at=T0, updated_at=T0,
        ))
        world.db.commit()
        self.assertEqual(MemberRepository.blocking_references(world.db, ALICE, ORG), {"projects": [], "invited_clients": 0})


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.world = _World()
        self.world.give_alice_a_history()
        app.dependency_overrides[get_current_user] = lambda: self.world.admin
        app.dependency_overrides[get_db] = lambda: self.world.db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_delete_answers_204_with_no_body_and_the_member_is_gone(self):
        response = self.client.delete(f"/api/v1/members/{ALICE}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.world.db.expire_all()
        self.assertIsNone(self.world.db.get(User, ALICE))

    def test_a_refusal_reaches_the_client_with_its_reason(self):
        self.world.db.get(Project, PROJECT).leader_id = ALICE
        self.world.db.commit()
        response = self.client.delete(f"/api/v1/members/{ALICE}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Alpha", response.json()["detail"])
        self.assertIsNotNone(self.world.db.get(User, ALICE))

    def test_an_unknown_member_is_404(self):
        self.assertEqual(self.client.delete("/api/v1/members/777").status_code, 404)

    def test_the_deleted_member_no_longer_appears_in_the_directory_in_any_status(self):
        self.client.delete(f"/api/v1/members/{ALICE}")
        for query in ("", "?status=inactive", "?status=active"):
            names = [m["name"] for m in self.client.get(f"/api/v1/members{query}").json()["items"]]
            self.assertNotIn("Alice", names, query)

    def test_openapi_documents_a_204_delete(self):
        operation = app.openapi()["paths"]["/api/v1/members/{member_id}"]["delete"]
        self.assertIn("204", operation["responses"])
        self.assertNotIn("200", operation["responses"])
        self.assertEqual(operation["summary"], "Delete a member")


if __name__ == "__main__":
    unittest.main()
