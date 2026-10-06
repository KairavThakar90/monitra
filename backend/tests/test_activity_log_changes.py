"""What the activity trail says about a project or task change.

A row used to say "Updated the project (status, team)": the fields the form
*sent*, not what *changed*, and never from what to what or who. These tests run
the real services against real rows and pin the sentences the Logs page now
shows:

* a project's status, leader, owner and team each change as their own action,
  with the old value, the new value and the people by name;
* an edit that changed nothing records nothing, and a form that re-sends
  every field does not claim to have changed them;
* tasks: status, who was given the task, who was taken off it;
* every route that assigns people -- the project edit, the member routes, the
  task-assignee routes -- leaves the same kind of row;
* clients (invite, resend, access, deactivate) and screenshot deletion, which
  recorded nothing at all;
* a leader reads what anyone changed on a project they lead -- and only that.
"""
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.permissions import ROLE_PERMISSIONS
from app.models.activity_log import ActivityLog, ActivityLogAction, ActivityLogModule
from app.models.client import Client
from app.models.client_invitation import ClientInvitation
from app.models.client_project import ClientProject
from app.models.manual_time_entry import ManualTimeEntry
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.sso_handoff_token import SsoHandoffToken
from app.models.task import Task
from app.models.task_assignee import TaskAssignee
from app.models.time_entry import TimeEntry
from app.models.time_entry_adjustment import TimeEntryAdjustment
from app.models.user import User
from app.schemas.project_management import ProjectCreate, ProjectUpdate, TaskAssigneesSet, TaskCreate, TaskUpdate
from app.services.activity_log import ActivityLogService
from app.services.client_invitation_service import ClientInvitationService
from app.services.project_management import ProjectManagementService
from app.services.project_member import ProjectMemberService
from app.services.task_assignee import TaskAssigneeService
from tests.status_catalog_stub import rows, status_catalog
from tests.test_project_hours_summary import _sqlite_schema


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw):  # pragma: no cover - dialect shim
    return "INTEGER"


ORG = 1
ADMIN, LEADER, ALICE, BOB, CARA = 1, 2, 101, 102, 103
ALPHA, BETA = 10, 11
TASK = 100

PROJECT_STATUSES = rows((5, "Active"), (6, "Paused"), (7, "Completed"))
TASK_STATUSES = rows((1, "Todo"), (2, "In Progress"), (3, "Completed"))
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class _World(unittest.TestCase):
    """An organization: an administrator (Grace), a leader (Hank) who leads
    Alpha, and three employees; Alice is on Alpha, which has one task."""

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        _sqlite_schema(self.engine, User, Project, ProjectMember, Task, TaskAssignee, ActivityLog)
        self.db = Session(self.engine)
        self.admin = self._user(ADMIN, "administrator", "Grace")
        self.leader = self._user(LEADER, "leader", "Hank")
        self.alice = self._user(ALICE, "employee", "Alice")
        self.bob = self._user(BOB, "employee", "Bob")
        self.cara = self._user(CARA, "employee", "Cara")
        self.db.add(Project(
            id=ALPHA, organization_id=ORG, project_name="Alpha", status="active", status_id=5,
            leader_id=LEADER, billing_type="free", created_by=ADMIN,
        ))
        self.db.add(Project(
            id=BETA, organization_id=ORG, project_name="Beta", status="active", status_id=5,
            leader_id=ADMIN, billing_type="free", created_by=ADMIN,
        ))
        self.db.add(ProjectMember(project_id=ALPHA, organization_id=ORG, user_id=ALICE, created_by=ADMIN))
        self.db.add(Task(
            id=TASK, organization_id=ORG, project_id=ALPHA, task_name="Design", status="todo",
            status_id=1, created_by=ADMIN,
        ))
        self.db.commit()

        catalog = status_catalog(project_statuses=PROJECT_STATUSES, task_statuses=TASK_STATUSES)
        catalog.__enter__()
        self.addCleanup(catalog.__exit__, None, None, None)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _user(self, user_id, role, name) -> User:
        user = User(
            id=user_id, organization_id=ORG, username=name.lower(), email=f"{name.lower()}@example.com",
            name=name, role_name=role, permissions={p: True for p in ROLE_PERMISSIONS.get(role, set())},
            status="active", is_active=True, idle_enabled=True, idle_minutes=5, capture_frequency=10,
        )
        self.db.add(user)
        return user

    def put_on_alpha(self, *user_ids):
        for user_id in user_ids:
            self.db.add(ProjectMember(project_id=ALPHA, organization_id=ORG, user_id=user_id, created_by=ADMIN))
        self.db.commit()

    def give_task(self, *user_ids):
        task = self.db.get(Task, TASK)
        for user_id in user_ids:
            self.db.add(TaskAssignee(task_id=TASK, user_id=user_id, assigned_by=ADMIN))
        task.assignee_id = user_ids[0] if user_ids else None
        self.db.commit()

    def trail(self, actor=None):
        """(action, description) of every row written so far, oldest first."""
        statement = select(ActivityLog).order_by(ActivityLog.id)
        if actor is not None:
            statement = statement.where(ActivityLog.user_id == actor)
        self.db.expire_all()
        return [(row.action, row.description) for row in self.db.scalars(statement).all()]

    def rows(self):
        self.db.expire_all()
        return list(self.db.scalars(select(ActivityLog).order_by(ActivityLog.id)).all())

    def update_project(self, actor=None, **fields):
        return ProjectManagementService.update(self.db, actor or self.admin, ALPHA, ProjectUpdate(**fields))


# ── Projects ────────────────────────────────────────────────────────────────


class ProjectStatusTests(_World):
    def test_a_status_change_says_from_what_to_what(self):
        self.update_project(status_id=6)
        self.assertEqual(self.trail(), [(
            ActivityLogAction.PROJECT_STATUS_CHANGED,
            'Changed the status of the project "Alpha" from Active to Paused',
        )])

    def test_the_row_belongs_to_the_administrator_and_names_the_project(self):
        self.update_project(status_id=7)
        [row] = self.rows()
        self.assertEqual((row.user_id, row.organization_id, row.module), (ADMIN, ORG, ActivityLogModule.PROJECT))
        self.assertEqual((row.project_id, row.entity_id), (ALPHA, ALPHA))

    def test_re_sending_the_current_status_records_nothing(self):
        self.update_project(status_id=5)
        self.assertEqual(self.trail(), [])

    def test_a_form_that_resends_every_field_only_claims_what_changed(self):
        # The web form always sends all of these; only the name differs.
        self.update_project(
            project_name="Alpha 2", status_id=5, leader_id=LEADER, employee_ids=[ALICE], billing_type="free",
        )
        self.assertEqual(self.trail(), [(
            ActivityLogAction.PROJECT_UPDATED, 'Updated the project "Alpha 2" (name: "Alpha" → "Alpha 2")',
        )])

    def test_an_edit_that_changes_nothing_records_nothing(self):
        self.update_project(project_name="Alpha", status_id=5, leader_id=LEADER, employee_ids=[ALICE])
        self.assertEqual(self.trail(), [])


class ProjectTeamTests(_World):
    def test_assigning_employees_names_them(self):
        self.update_project(employee_ids=[ALICE, BOB, CARA])
        self.assertEqual(self.trail(), [(
            ActivityLogAction.PROJECT_MEMBER_ASSIGNED, 'Assigned Bob and Cara to the project "Alpha"',
        )])

    def test_removing_an_employee_names_them(self):
        self.put_on_alpha(BOB)
        self.update_project(employee_ids=[ALICE])
        self.assertEqual(self.trail(), [(
            ActivityLogAction.PROJECT_MEMBER_REMOVED, 'Removed Bob from the project "Alpha"',
        )])

    def test_a_swap_is_one_row_each_way(self):
        self.update_project(employee_ids=[BOB])
        self.assertEqual(
            sorted(self.trail()),
            sorted([
                (ActivityLogAction.PROJECT_MEMBER_ASSIGNED, 'Assigned Bob to the project "Alpha"'),
                (ActivityLogAction.PROJECT_MEMBER_REMOVED, 'Removed Alice from the project "Alpha"'),
            ]),
        )

    def test_the_same_team_records_nothing(self):
        self.update_project(employee_ids=[ALICE])
        self.assertEqual(self.trail(), [])

    def test_many_people_are_summarised(self):
        for user_id, name in ((201, "Dev1"), (202, "Dev2"), (203, "Dev3"), (204, "Dev4"), (205, "Dev5"), (206, "Dev6")):
            self._user(user_id, "employee", name)
        self.db.commit()
        self.update_project(employee_ids=[ALICE, 201, 202, 203, 204, 205, 206])
        [(action, text)] = self.trail()
        self.assertEqual(action, ActivityLogAction.PROJECT_MEMBER_ASSIGNED)
        self.assertEqual(text, 'Assigned Dev1, Dev2, Dev3, Dev4, Dev5 and 1 more to the project "Alpha"')

    def test_creating_a_project_with_a_team_records_the_assignment_too(self):
        ProjectManagementService.create(
            self.db, self.admin,
            ProjectCreate(project_name="Gamma", status_id=5, leader_id=LEADER, employee_ids=[ALICE, BOB], billing_type="free", category="kyle"),
            owner_required=False,
        )
        self.assertEqual(
            [action for action, _ in self.trail()],
            [ActivityLogAction.PROJECT_CREATED, ActivityLogAction.PROJECT_MEMBER_ASSIGNED],
        )
        self.assertEqual(self.trail()[1][1], 'Assigned Alice and Bob to the project "Gamma"')

    def test_creating_a_project_with_no_team_adds_no_assignment_row(self):
        ProjectManagementService.create(
            self.db, self.admin,
            ProjectCreate(project_name="Gamma", status_id=5, leader_id=LEADER, billing_type="free", category="kyle"),
            owner_required=False,
        )
        self.assertEqual([action for action, _ in self.trail()], [ActivityLogAction.PROJECT_CREATED])


class ProjectLeaderAndFieldTests(_World):
    def test_a_new_leader_is_named_both_ways(self):
        self._user(3, "leader", "Ivy")
        self.db.commit()
        self.update_project(leader_id=3)
        self.assertEqual(self.trail(), [(
            ActivityLogAction.PROJECT_LEADER_CHANGED, 'Changed the leader of the project "Alpha" from Hank to Ivy',
        )])

    def test_several_changes_in_one_edit_are_separate_rows(self):
        self.update_project(status_id=6, employee_ids=[ALICE, BOB], project_name="Alpha X")
        self.assertEqual(
            sorted(action for action, _ in self.trail()),
            sorted([
                ActivityLogAction.PROJECT_STATUS_CHANGED,
                ActivityLogAction.PROJECT_MEMBER_ASSIGNED,
                ActivityLogAction.PROJECT_UPDATED,
            ]),
        )

    def test_a_deadline_and_a_budget_show_old_and_new(self):
        tomorrow = date.today() + timedelta(days=1)
        self.update_project(deadline=tomorrow, billing_type="fixed", fixed_hours="40")
        [(action, text)] = self.trail()
        self.assertEqual(action, ActivityLogAction.PROJECT_UPDATED)
        self.assertIn(f"deadline: none → {tomorrow.isoformat()}", text)
        self.assertIn("billing: free time → fixed hours", text)
        self.assertIn("hour budget: none → 40", text)

    def test_a_leader_editing_their_own_project_is_the_actor(self):
        self.update_project(actor=self.leader, status_id=6)
        self.assertEqual([row.user_id for row in self.rows()], [LEADER])

    def test_an_edit_the_database_refused_records_nothing(self):
        with self.assertRaises(Exception):
            self.update_project(status_id=999)
        self.assertEqual(self.trail(), [])


# ── Tasks ───────────────────────────────────────────────────────────────────


class TaskTests(_World):
    def setUp(self):
        super().setUp()
        self.put_on_alpha(BOB)

    def update_task(self, **fields):
        return ProjectManagementService.update_task(self.db, self.admin, ALPHA, TASK, TaskUpdate(**fields))

    def set_assignees(self, user_ids, **extra):
        return ProjectManagementService.set_task_assignees(
            self.db, self.admin, ALPHA, TASK, TaskAssigneesSet(user_ids=user_ids, **extra),
        )

    def test_a_status_change_says_from_what_to_what(self):
        self.update_task(status_id=2)
        self.assertEqual(self.trail(), [(
            ActivityLogAction.TASK_STATUS_CHANGED, 'Changed the status of the task "Design" from Todo to In Progress',
        )])
        [row] = self.rows()
        self.assertEqual((row.module, row.project_id, row.task_id), (ActivityLogModule.TASK, ALPHA, TASK))

    def test_replacing_the_assignee_gives_one_and_removes_the_other(self):
        self.give_task(ALICE)
        self.update_task(assignee_id=BOB)
        self.assertEqual(
            sorted(self.trail()),
            sorted([
                (ActivityLogAction.TASK_ASSIGNED, 'Assigned the task "Design" to Bob'),
                (ActivityLogAction.TASK_UNASSIGNED, 'Removed Alice from the task "Design"'),
            ]),
        )

    def test_a_rename_and_an_estimate_show_old_and_new(self):
        self.update_task(name="Design v2", estimated_hours=3)
        [(action, text)] = self.trail()
        self.assertEqual(action, ActivityLogAction.TASK_UPDATED)
        self.assertIn('name: "Design" → "Design v2"', text)
        self.assertIn("estimated hours: none → 3", text)

    def test_an_edit_that_changes_nothing_records_nothing(self):
        self.update_task(status_id=1, name="Design")
        self.assertEqual(self.trail(), [])

    def test_the_assign_tasks_screen_names_who_was_given_the_task(self):
        self.set_assignees([ALICE, BOB])
        self.assertEqual(self.trail(), [(
            ActivityLogAction.TASK_ASSIGNED, 'Assigned the task "Design" to Alice and Bob',
        )])

    def test_saving_a_changed_set_is_the_difference_not_the_whole_set(self):
        self.give_task(ALICE, BOB)
        self.set_assignees([BOB])
        self.assertEqual(self.trail(), [(ActivityLogAction.TASK_UNASSIGNED, 'Removed Alice from the task "Design"')])

    def test_saving_the_same_set_records_nothing(self):
        self.give_task(ALICE)
        self.set_assignees([ALICE])
        self.assertEqual(self.trail(), [])

    def test_the_dialog_that_also_moves_the_status_records_both(self):
        self.set_assignees([ALICE], status_id=3)
        self.assertEqual(
            sorted(action for action, _ in self.trail()),
            sorted([ActivityLogAction.TASK_ASSIGNED, ActivityLogAction.TASK_STATUS_CHANGED]),
        )

    def test_unassigning_a_task_names_who_lost_it_and_a_repeat_records_nothing(self):
        self.give_task(ALICE)
        ProjectManagementService.unassign_task(self.db, self.admin, ALPHA, TASK)
        self.assertEqual(self.trail(), [(ActivityLogAction.TASK_UNASSIGNED, 'Removed Alice from the task "Design"')])
        ProjectManagementService.unassign_task(self.db, self.admin, ALPHA, TASK)
        self.assertEqual(len(self.trail()), 1)

    def test_a_new_task_given_to_someone_records_the_assignment(self):
        ProjectManagementService.create_task(
            self.db, self.admin, ALPHA, TaskCreate(name="Build", status_id=1, assignee_ids=[ALICE, BOB]),
        )
        self.assertEqual(
            [action for action, _ in self.trail()], [ActivityLogAction.TASK_CREATED, ActivityLogAction.TASK_ASSIGNED],
        )
        self.assertEqual(self.trail()[1][1], 'Assigned the task "Build" to Alice and Bob')

    def test_a_new_unassigned_task_records_only_the_creation(self):
        ProjectManagementService.create_task(self.db, self.admin, ALPHA, TaskCreate(name="Build", status_id=1))
        self.assertEqual([action for action, _ in self.trail()], [ActivityLogAction.TASK_CREATED])


# ── The routes that assign people ───────────────────────────────────────────


class MemberRouteTests(_World):
    def test_adding_members_names_only_those_actually_added(self):
        ProjectMemberService.add_members(self.db, ALPHA, [ALICE, BOB, CARA], self.admin)
        self.assertEqual(self.trail(), [(
            ActivityLogAction.PROJECT_MEMBER_ASSIGNED, 'Assigned Bob and Cara to the project "Alpha"',
        )])

    def test_adding_who_is_already_there_records_nothing(self):
        ProjectMemberService.add_members(self.db, ALPHA, [ALICE], self.admin)
        self.assertEqual(self.trail(), [])

    def test_the_single_add_and_remove_routes(self):
        ProjectMemberService.add_member(self.db, ALPHA, BOB, self.admin)
        ProjectMemberService.add_member(self.db, ALPHA, BOB, self.admin)   # a repeat is a no-op
        ProjectMemberService.remove_member(self.db, ALPHA, BOB, self.admin)
        self.assertEqual(self.trail(), [
            (ActivityLogAction.PROJECT_MEMBER_ASSIGNED, 'Assigned Bob to the project "Alpha"'),
            (ActivityLogAction.PROJECT_MEMBER_REMOVED, 'Removed Bob from the project "Alpha"'),
        ])

    def test_swapping_one_member_for_another(self):
        ProjectMemberService.update_member(self.db, ALPHA, ALICE, BOB, self.admin)
        self.assertEqual(
            sorted(self.trail()),
            sorted([
                (ActivityLogAction.PROJECT_MEMBER_ASSIGNED, 'Assigned Bob to the project "Alpha"'),
                (ActivityLogAction.PROJECT_MEMBER_REMOVED, 'Removed Alice from the project "Alpha"'),
            ]),
        )

    def test_the_task_assignee_routes(self):
        TaskAssigneeService.add_assignee(self.db, ALPHA, TASK, ALICE, self.admin)
        TaskAssigneeService.add_assignee(self.db, ALPHA, TASK, ALICE, self.admin)   # a repeat is a no-op
        TaskAssigneeService.remove_assignee(self.db, ALPHA, TASK, ALICE, self.admin)
        self.assertEqual(self.trail(), [
            (ActivityLogAction.TASK_ASSIGNED, 'Assigned the task "Design" to Alice'),
            (ActivityLogAction.TASK_UNASSIGNED, 'Removed Alice from the task "Design"'),
        ])


class NeverFailsTests(_World):
    def test_a_trail_that_cannot_be_written_does_not_fail_the_edit(self):
        with patch.object(ActivityLogService, "_write", side_effect=RuntimeError("log table gone")):
            result = self.update_project(status_id=6, employee_ids=[ALICE, BOB])
        self.assertEqual(result["status"].id, 6)
        self.assertEqual(self.db.get(Project, ALPHA).status_id, 6)
        self.assertEqual(self.trail(), [])

    def test_a_description_that_cannot_be_built_does_not_fail_the_edit(self):
        from app.services.project_activity import ProjectActivity

        with patch.object(ProjectActivity, "project_update_rows", side_effect=RuntimeError("bad name")):
            result = self.update_project(status_id=6)
        self.assertEqual(result["status"].id, 6)
        self.assertEqual(self.trail(), [])

    def test_one_row_that_cannot_be_written_does_not_cost_the_others(self):
        real_write = ActivityLogService._write
        calls = []

        def flaky(db, **fields):
            calls.append(fields["action"])
            if len(calls) == 1:
                raise RuntimeError("first write fails")
            return real_write(db, **fields)

        with patch.object(ActivityLogService, "_write", side_effect=flaky):
            self.update_project(status_id=6, employee_ids=[ALICE, BOB])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.trail()), 1)

    def test_a_snapshot_that_cannot_be_read_falls_back_to_the_plain_row(self):
        with patch.object(ActivityLogService, "snapshot", return_value=None):
            self.update_project(status_id=6)
        self.assertEqual(self.trail(), [(ActivityLogAction.PROJECT_UPDATED, 'Updated the project "Alpha"')])

    def test_a_stand_in_database_is_never_asked_for_a_snapshot(self):
        # Code that runs against a MagicMock must not be made to answer extra queries.
        self.assertIsNone(ActivityLogService.snapshot(MagicMock(), lambda: 1 / 0))
        self.assertEqual(ActivityLogService.capture_many(MagicMock(), lambda: [{"actor": None}]), [])


class JoinNamesTests(unittest.TestCase):
    def test_the_shapes(self):
        join = ActivityLogService.join_names
        self.assertEqual(join([]), "nobody")
        self.assertEqual(join(["Alice"]), "Alice")
        self.assertEqual(join(["Alice", "Bob"]), "Alice and Bob")
        self.assertEqual(join(["A", "B", "C"]), "A, B and C")
        self.assertEqual(join(list("ABCDEFG")), "A, B, C, D, E and 2 more")
        self.assertEqual(join(["Alice", "", None]), "Alice")


# ── Who may read it ─────────────────────────────────────────────────────────


class LeaderReadTests(_World):
    """A leader reads their team's actions, and what *anyone* changed on a
    project they lead -- an administrator moving it to On hold, assigning a
    member -- but not the rest of what that administrator did."""

    def setUp(self):
        super().setUp()
        self.day = date(2026, 10, 1)

    def add(self, user_id, module, action, description, project_id=None):
        self.db.add(ActivityLog(
            organization_id=ORG, user_id=user_id, project_id=project_id, module=module, action=action,
            description=description, created_at=NOW,
        ))
        self.db.commit()

    def read(self, viewer, **kwargs):
        payload = ActivityLogService.list_grouped(self.db, viewer, start_day=self.day, end_day=self.day, **kwargs)
        return {group["user_id"]: [e["description"] for e in group["entries"]] for group in payload["members"]}

    def populate(self):
        self.add(ADMIN, ActivityLogModule.PROJECT, ActivityLogAction.PROJECT_STATUS_CHANGED, "Alpha to Paused", ALPHA)
        self.add(ADMIN, ActivityLogModule.TASK, ActivityLogAction.TASK_ASSIGNED, "Alpha task given", ALPHA)
        self.add(ADMIN, ActivityLogModule.PROJECT, ActivityLogAction.PROJECT_STATUS_CHANGED, "Beta to Paused", BETA)
        self.add(ADMIN, ActivityLogModule.AUTH, ActivityLogAction.LOGIN, "Admin signed in")
        self.add(ADMIN, ActivityLogModule.TIMER, ActivityLogAction.TIMER_STARTED, "Admin timer on Alpha", ALPHA)
        self.add(ADMIN, ActivityLogModule.MEMBER, ActivityLogAction.MEMBER_UPDATED, "Admin edited a member")
        self.add(ALICE, ActivityLogModule.TIMER, ActivityLogAction.TIMER_STARTED, "Alice timer", ALPHA)
        self.add(BOB, ActivityLogModule.TIMER, ActivityLogAction.TIMER_STARTED, "Bob timer", BETA)
        self.add(LEADER, ActivityLogModule.PROJECT, ActivityLogAction.PROJECT_UPDATED, "Hank edited Alpha", ALPHA)

    def test_a_leader_reads_what_an_administrator_changed_on_their_project(self):
        self.populate()
        seen = self.read(self.leader)
        self.assertEqual(sorted(seen[ADMIN]), ["Alpha task given", "Alpha to Paused"])

    def test_and_nothing_else_the_administrator_did(self):
        self.populate()
        admin_rows = self.read(self.leader)[ADMIN]
        for private in ("Beta to Paused", "Admin signed in", "Admin timer on Alpha", "Admin edited a member"):
            self.assertNotIn(private, admin_rows)

    def test_their_team_and_themselves_are_read_as_before(self):
        self.populate()
        seen = self.read(self.leader)
        self.assertEqual(seen[ALICE], ["Alice timer"])
        self.assertEqual(seen[LEADER], ["Hank edited Alpha"])
        self.assertNotIn(BOB, seen)      # not on a project Hank leads

    def test_a_project_the_leader_does_not_lead_stays_invisible(self):
        self.populate()
        rows_seen = [text for texts in self.read(self.leader).values() for text in texts]
        self.assertNotIn("Beta to Paused", rows_seen)

    def test_an_administrator_and_hr_read_everything(self):
        self.populate()
        self.assertEqual(len(self.read(self.admin)[ADMIN]), 6)
        hr = self._user(7, "hr", "Hazel")
        self.db.commit()
        self.assertEqual(len(self.read(hr)[ADMIN]), 6)

    def test_a_leader_can_narrow_to_the_administrator_and_still_only_sees_the_project_rows(self):
        self.populate()
        seen = self.read(self.leader, member_id=ADMIN)
        self.assertEqual(sorted(seen[ADMIN]), ["Alpha task given", "Alpha to Paused"])

    def test_a_leader_with_no_project_and_no_team_reads_only_themselves(self):
        loner = self._user(8, "leader", "Lone")
        self.db.commit()
        self.populate()
        self.assertEqual(self.read(loner), {})
        self.assertEqual(self.read(loner, member_id=ADMIN), {})

    def test_the_search_still_narrows_what_a_leader_may_see(self):
        self.populate()
        seen = self.read(self.leader, search="Paused")
        self.assertEqual(seen, {ADMIN: ["Alpha to Paused"]})


# ── Clients ─────────────────────────────────────────────────────────────────


class ClientTrailTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        _sqlite_schema(
            self.engine, User, Project, ProjectMember, Task, TimeEntry, ManualTimeEntry, TimeEntryAdjustment,
            SsoHandoffToken, Client, ClientInvitation, ClientProject, ActivityLog,
        )
        self.db = Session(self.engine)
        self.admin = User(
            id=1, organization_id=ORG, username="admin", email="admin@example.com", name="Grace",
            role_name="administrator", permissions={}, is_active=True, status="active", capture_frequency=10,
        )
        self.db.add(self.admin)
        self.db.add_all([
            Project(id=10, organization_id=ORG, project_name="Alpha", created_by=1),
            Project(id=11, organization_id=ORG, project_name="Beta", created_by=1),
        ])
        self.db.commit()
        send = patch("app.services.email.workflows._send_immediately", return_value=True)
        send.start()
        self.addCleanup(send.stop)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def trail(self):
        self.db.expire_all()
        return [(r.module, r.action, r.description, r.user_id) for r in self.db.scalars(select(ActivityLog).order_by(ActivityLog.id)).all()]

    #: Every switch, as the service requires; tests override the ones they move.
    PERMISSIONS = {
        "share_member_details": True, "share_screenshots": False, "share_tasks": True,
        "share_timing": True, "share_billing": False,
    }

    def invite(self, projects=(10,)):
        return ClientInvitationService.create_invitation(self.db, self.admin, "client@example.com", list(projects))

    def test_inviting_a_client_names_the_projects(self):
        self.invite((10, 11))
        self.assertEqual(self.trail(), [(
            ActivityLogModule.CLIENT, ActivityLogAction.CLIENT_INVITED,
            "Invited the client client@example.com to Alpha and Beta", 1,
        )])

    def test_a_resend_is_its_own_row_and_not_also_an_invite(self):
        client = self.invite()
        ClientInvitationService.resend_invitation(self.db, self.admin, client.id)
        self.assertEqual(
            [action for _, action, _, _ in self.trail()],
            [ActivityLogAction.CLIENT_INVITED, ActivityLogAction.CLIENT_INVITATION_RESENT],
        )
        self.assertEqual(self.trail()[1][2], "Resent the invitation to the client client@example.com")

    def test_changing_access_says_what_moved(self):
        client = self.invite((10,))
        ClientInvitationService.update_client_access(
            self.db, self.admin, client.id, [11], {**self.PERMISSIONS, "share_screenshots": True},
        )
        [_, (module, action, text, actor)] = self.trail()
        self.assertEqual((module, action, actor), (ActivityLogModule.CLIENT, ActivityLogAction.CLIENT_ACCESS_CHANGED, 1))
        self.assertEqual(text, "Changed the access of the client client@example.com (added Beta; removed Alpha; screenshots shared)")

    def test_saving_access_unchanged_records_nothing(self):
        client = self.invite((10,))
        ClientInvitationService.update_client_access(self.db, self.admin, client.id, [10], dict(self.PERMISSIONS))
        self.assertEqual(len(self.trail()), 1)     # only the invitation

    def test_deactivating_a_client(self):
        client = self.invite()
        client.status = "active"
        self.db.commit()
        ClientInvitationService.deactivate_client(self.db, self.admin, client.id)
        self.assertEqual(self.trail()[-1][1:3], (
            ActivityLogAction.CLIENT_DEACTIVATED, "Deactivated the client client@example.com",
        ))

    def test_a_refused_invitation_records_nothing(self):
        with self.assertRaises(Exception):
            self.invite((999,))
        self.assertEqual(self.trail(), [])


# ── Screenshots ─────────────────────────────────────────────────────────────


class ScreenshotDeletionTrailTests(unittest.TestCase):
    """A destructive action HR can take, and the record of it."""

    def _delete(self, *, detail=True, fail_drive=False):
        from app.services.time_entry_screenshot import TimeEntryScreenshotService
        from tests.test_screenshot_delete import SVC, _entry, _shot, _user

        captured = []
        db = MagicMock()
        db.get.return_value = SimpleNamespace(name="Alice")
        drive = MagicMock()
        if fail_drive:
            from app.services.google_drive_service import GoogleDriveError
            drive.delete_file_strict.side_effect = GoogleDriveError("boom")
        entry = _entry()
        entry.user_id, entry.project_id, entry.task_id = ALICE, ALPHA, TASK
        shot = _shot(captured_at=datetime(2026, 9, 18, 5, 50, tzinfo=timezone.utc))
        snapshot = (lambda db_, read: read()) if detail else (lambda db_, read: None)
        with patch(f"{SVC}.TimeEntryScreenshotRepository.get_with_entry", return_value=(shot, entry)), \
                patch(f"{SVC}.TimeEntryScreenshotRepository.delete"), \
                patch(f"{SVC}.drive_service", drive), \
                patch.object(ActivityLogService, "snapshot", side_effect=snapshot), \
                patch.object(ActivityLogService, "capture", side_effect=lambda db_, build: captured.append(build())):
            try:
                TimeEntryScreenshotService.delete_screenshot(db=db, screenshot_id=77, current_user=_user("hr"))
            except Exception:  # noqa: BLE001 - the failing case is asserted on `captured`
                pass
        return captured

    def test_the_deletion_names_whose_screenshot_and_when(self):
        [row] = self._delete()
        self.assertEqual((row["module"], row["action"]), (ActivityLogModule.SCREENSHOT, ActivityLogAction.SCREENSHOT_DELETED))
        self.assertEqual(row["description"], "Deleted a screenshot of Alice taken on 18 Sep 2026, 11:20 AM IST")
        self.assertEqual((row["project_id"], row["task_id"], row["entity_id"]), (ALPHA, TASK, 77))

    def test_without_the_detail_it_still_records_that_one_was_deleted(self):
        [row] = self._delete(detail=False)
        self.assertEqual(row["description"], "Deleted a screenshot")
        self.assertEqual(row["entity_id"], 77)

    def test_a_deletion_that_failed_is_not_recorded(self):
        self.assertEqual(self._delete(fail_drive=True), [])


if __name__ == "__main__":
    unittest.main()
